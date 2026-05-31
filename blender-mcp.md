# BlenderMCP — Reference Analysis

> Analysis of **BlenderMCP** (`blender-mcp`, v1.5.5) by Siddharth Ahuja, for use as a foundation for a larger application.
>
> - **GitHub:** https://github.com/ahujasid/blender-mcp
> - **License:** MIT
>
> *(Note: the request referred to this project as "Wand"; the repository itself is named BlenderMCP / `blender-mcp`. This document describes that repository.)*

---

## App Summary

**BlenderMCP** connects **Blender** (the open-source 3D creation suite) to AI assistants — Claude, Cursor, VS Code, or any **Model Context Protocol (MCP)** client — so the AI can directly inspect, create, and manipulate 3D scenes through natural-language prompts. It turns Blender into an "agent-controllable" application: the user describes what they want ("create a low-poly dungeon with a dragon guarding a pot of gold"), and the AI drives Blender to build it.

Its primary purpose is **prompt-assisted 3D modeling, scene creation, and manipulation**. Rather than wrapping Blender behind a fixed API, it exposes a small, powerful set of MCP *tools* — including an `execute_blender_code` escape hatch that runs arbitrary Python inside Blender — plus first-class integrations with several asset/generation providers (Poly Haven, Sketchfab, Hyper3D Rodin, Tencent Hunyuan3D).

Architecturally it is a **two-process, socket-bridged system**:

1. A **Blender addon** (`addon.py`) runs *inside* Blender and hosts a TCP socket server that receives JSON commands and executes them on Blender's main thread using the `bpy` API.
2. An **MCP server** (`src/blender_mcp/server.py`) runs as a separate process (launched by the AI client via `uvx blender-mcp`), exposes MCP tools, and forwards each tool call to the Blender addon over the socket.

The AI client ↔ MCP server link speaks MCP (over stdio); the MCP server ↔ Blender link speaks a simple JSON-over-TCP protocol.

---

## Core Abilities

**Scene & object inspection**
- `get_scene_info` — summary of the current scene (name, object count, first 10 objects with type/location, material count).
- `get_object_info` — full detail for a named object: transform, visibility, materials, mesh stats (verts/edges/faces), and world-space axis-aligned bounding box.
- `get_viewport_screenshot` — captures the 3D viewport as a PNG so the AI can *visually* see the scene.

**Direct creation & manipulation**
- `execute_blender_code` — runs arbitrary Python (`bpy`) inside Blender; the universal mechanism for creating/deleting/transforming objects, applying materials and colors, lighting, cameras, etc.

**Poly Haven asset library** (optional, toggled in the Blender sidebar)
- `get_polyhaven_categories`, `search_polyhaven_assets` — browse/search HDRIs, textures, models.
- `download_polyhaven_asset` — download & import a model, apply a texture as a PBR material, or set an HDRI as the world environment.
- `set_texture` — apply a previously downloaded Poly Haven texture to an object.

**Sketchfab model library** (optional)
- `search_sketchfab_models`, `get_sketchfab_model_preview` (returns a thumbnail Image), `download_sketchfab_model` — search, visually confirm, then download/import a model, auto-normalized to a target real-world size.

**AI 3D-model generation — Hyper3D Rodin** (optional)
- `get_hyper3d_status`, `generate_hyper3d_model_via_text`, `generate_hyper3d_model_via_images`, `poll_rodin_job_status`, `import_generated_asset` — text- or image-to-3D generation with two backends (Rodin "MAIN_SITE" and "FAL_AI"), polled asynchronously, then imported as GLB.

**AI 3D-model generation — Tencent Hunyuan3D** (optional)
- `get_hunyuan3d_status`, `generate_hunyuan3d_model`, `poll_hunyuan_job_status`, `import_generated_asset_hunyuan` — text/image-to-3D via Tencent Cloud's official API (TC3-HMAC-SHA256 signed) or a local API endpoint.

**Workflow guidance**
- An MCP **prompt** (`asset_creation_strategy`) that teaches the AI the preferred order of operations (check scene → prefer libraries → fall back to generation → fall back to scripting) and bounding-box discipline.

**Operational features**
- Persistent, auto-reconnecting socket connection; configurable host/port via env vars (`BLENDER_HOST`, `BLENDER_PORT`).
- Per-integration enable/disable from a Blender sidebar panel.
- Anonymous, consent-aware **telemetry** (opt-out via `DISABLE_TELEMETRY`).
- Remote-host support (the MCP server can connect to Blender on another machine, e.g. `host.docker.internal`).

---

## Technical Implementation

This section walks through how the system is built, end to end, with file paths, function names, and code snippets.

### 0. Component & Protocol Overview

```
AI client (Claude/Cursor/VS Code)
        │  MCP over stdio
        ▼
MCP server  ── src/blender_mcp/server.py  (FastMCP, launched via `uvx blender-mcp`)
        │  JSON over TCP  (default localhost:9876)
        ▼
Blender addon ── addon.py  (socket server inside Blender, runs bpy on main thread)
```

- **Commands** (client → Blender): JSON `{"type": <command>, "params": {…}}`.
- **Responses** (Blender → client): JSON `{"status": "success", "result": {…}}` or `{"status": "error", "message": "…"}`.

Packaging is defined in `pyproject.toml`: the console script `blender-mcp = "blender_mcp.server:main"` is the entry point, with `main.py` simply re-exporting it. Dependencies are `mcp[cli]>=1.3.0`, `supabase` (telemetry), and `tomli`.

### 1. The MCP Server (`src/blender_mcp/server.py`)

**Server creation & lifespan.** The server is a `FastMCP` instance with an async lifespan context manager that records startup telemetry and probes the Blender connection:

```python
# src/blender_mcp/server.py
from mcp.server.fastmcp import FastMCP, Context, Image

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    try:
        logger.info("BlenderMCP server starting up")
        record_startup()
        try:
            blender = get_blender_connection()   # verify Blender is reachable
            logger.info("Successfully connected to Blender on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Blender on startup: {str(e)}")
        yield {}
    finally:
        global _blender_connection
        if _blender_connection:
            _blender_connection.disconnect()
            _blender_connection = None

mcp = FastMCP("BlenderMCP", lifespan=server_lifespan)

def main():
    """Run the MCP server"""
    mcp.run()
```

**The socket client — `BlenderConnection`.** A dataclass that owns the TCP socket to the Blender addon. Its `send_command()` serializes the command, sets a 180-second timeout, and reads a complete JSON reply:

```python
# src/blender_mcp/server.py
@dataclass
class BlenderConnection:
    host: str
    port: int
    sock: socket.socket = None

    def connect(self) -> bool:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        return True

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Blender")
        command = {"type": command_type, "params": params or {}}
        self.sock.sendall(json.dumps(command).encode('utf-8'))
        self.sock.settimeout(180.0)
        response_data = self.receive_full_response(self.sock)
        response = json.loads(response_data.decode('utf-8'))
        if response.get("status") == "error":
            raise Exception(response.get("message", "Unknown error from Blender"))
        return response.get("result", {})
```

**Chunked, framing-free receive.** Because the protocol has no length prefix, `receive_full_response()` reads chunks and treats the message as complete only when the accumulated bytes parse as valid JSON:

```python
def receive_full_response(self, sock, buffer_size=8192):
    chunks = []
    sock.settimeout(180.0)
    while True:
        chunk = sock.recv(buffer_size)
        if not chunk:
            if not chunks:
                raise Exception("Connection closed before receiving any data")
            break
        chunks.append(chunk)
        try:
            data = b''.join(chunks)
            json.loads(data.decode('utf-8'))   # parses? then it's complete
            return data
        except json.JSONDecodeError:
            continue                            # incomplete — keep reading
```

**Persistent global connection with health check.** Resources/tools share one connection. `get_blender_connection()` validates the existing socket by issuing a lightweight `get_polyhaven_status` "ping" (which also caches whether Poly Haven is enabled), and lazily reconnects using `BLENDER_HOST`/`BLENDER_PORT`:

```python
_blender_connection = None
_polyhaven_enabled = False

def get_blender_connection():
    global _blender_connection, _polyhaven_enabled
    if _blender_connection is not None:
        try:
            result = _blender_connection.send_command("get_polyhaven_status")  # ping
            _polyhaven_enabled = result.get("enabled", False)
            return _blender_connection
        except Exception:
            _blender_connection.disconnect()
            _blender_connection = None
    if _blender_connection is None:
        host = os.getenv("BLENDER_HOST", DEFAULT_HOST)   # "localhost"
        port = int(os.getenv("BLENDER_PORT", DEFAULT_PORT))  # 9876
        _blender_connection = BlenderConnection(host=host, port=port)
        if not _blender_connection.connect():
            raise Exception("Could not connect to Blender. Make sure the Blender addon is running.")
    return _blender_connection
```

**Tools are thin RPC stubs.** Every MCP tool follows the same shape: get the connection, `send_command(...)`, format the result. Decorators stack `@telemetry_tool(...)` on top of `@mcp.tool()`. The two simplest examples:

```python
@telemetry_tool("get_scene_info")
@mcp.tool()
def get_scene_info(ctx: Context) -> str:
    """Get detailed information about the current Blender scene"""
    blender = get_blender_connection()
    result = blender.send_command("get_scene_info")
    return json.dumps(result, indent=2)

@telemetry_tool("execute_blender_code")
@mcp.tool()
def execute_blender_code(ctx: Context, code: str) -> str:
    """Execute arbitrary Python code in Blender..."""
    blender = get_blender_connection()
    result = blender.send_command("execute_code", {"code": code})
    return f"Code executed successfully: {result.get('result', '')}"
```

**Returning images to the model.** `get_viewport_screenshot` and `get_sketchfab_model_preview` return MCP `Image` objects so the AI can see pixels. The screenshot tool round-trips through a temp file:

```python
@telemetry_tool("get_viewport_screenshot")
@mcp.tool()
def get_viewport_screenshot(ctx: Context, max_size: int = 800) -> Image:
    blender = get_blender_connection()
    temp_path = os.path.join(tempfile.gettempdir(), f"blender_screenshot_{os.getpid()}.png")
    result = blender.send_command("get_viewport_screenshot",
                                  {"max_size": max_size, "filepath": temp_path, "format": "png"})
    with open(temp_path, 'rb') as f:
        image_bytes = f.read()
    os.remove(temp_path)
    return Image(data=image_bytes, format="png")
```

**Client-side gating.** Some tools guard on cached state — e.g. `get_polyhaven_categories` refuses to call Blender unless the cached `_polyhaven_enabled` flag is set, telling the model to enable it in the sidebar.

**The strategy prompt.** A `@mcp.prompt()` named `asset_creation_strategy` injects a long playbook so the model reliably checks the scene first, prefers asset libraries over scripting, and always verifies `world_bounding_box` to avoid clipping:

```python
@mcp.prompt()
def asset_creation_strategy() -> str:
    return """When creating 3D content in Blender, always start by checking if integrations are available:
    0. Before anything, always check the scene from get_scene_info()
    1. First use the following tools to verify if the following integrations are enabled:
        1. PolyHaven ...
        2. Sketchfab ...
        3. Hyper3D(Rodin) ...
        4. Hunyuan3D ...
    Only fall back to scripting when ... all disabled / a simple primitive is requested ..."""
```

### 2. The Blender Addon (`addon.py`)

This is the half that runs inside Blender. It is a standard Blender addon (`bl_info` dict, `register()`/`unregister()`) that embeds a threaded TCP server.

**Socket server in a background thread.** `BlenderMCPServer.start()` binds the listening socket (with `SO_REUSEADDR`) and spawns a daemon thread running `_server_loop()`:

```python
# addon.py
class BlenderMCPServer:
    def start(self):
        self.running = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.host, self.port))
        self.socket.listen(1)
        self.server_thread = threading.Thread(target=self._server_loop)
        self.server_thread.daemon = True
        self.server_thread.start()
```

**The critical threading trick — execute on Blender's main thread.** Blender's `bpy` API is *not* thread-safe; calling it from the socket thread would crash Blender. So `_handle_client()` parses a command on the socket thread but **schedules** the actual execution onto the main thread using `bpy.app.timers.register(...)`, then sends the reply back to the client from inside that callback:

```python
# addon.py — _handle_client()
command = json.loads(buffer.decode('utf-8'))
buffer = b''

def execute_wrapper():
    try:
        response = self.execute_command(command)
        response_json = json.dumps(response)
        client.sendall(response_json.encode('utf-8'))
    except Exception as e:
        error_response = {"status": "error", "message": str(e)}
        client.sendall(json.dumps(error_response).encode('utf-8'))
    return None

# Schedule execution in Blender's main thread
bpy.app.timers.register(execute_wrapper, first_interval=0.0)
```

**Command dispatch with feature gating.** `_execute_command_internal()` builds a `handlers` dict mapping command strings to bound methods. Base handlers are always present; integration handlers are added only if the corresponding sidebar checkbox (a Scene property) is enabled:

```python
# addon.py — _execute_command_internal()
handlers = {
    "get_scene_info": self.get_scene_info,
    "get_object_info": self.get_object_info,
    "get_viewport_screenshot": self.get_viewport_screenshot,
    "execute_code": self.execute_code,
    "get_telemetry_consent": self.get_telemetry_consent,
    "get_polyhaven_status": self.get_polyhaven_status,
    "get_hyper3d_status": self.get_hyper3d_status,
    "get_sketchfab_status": self.get_sketchfab_status,
    "get_hunyuan3d_status": self.get_hunyuan3d_status,
}
if bpy.context.scene.blendermcp_use_polyhaven:
    handlers.update({ "get_polyhaven_categories": self.get_polyhaven_categories,
                      "search_polyhaven_assets": self.search_polyhaven_assets,
                      "download_polyhaven_asset": self.download_polyhaven_asset,
                      "set_texture": self.set_texture })
if bpy.context.scene.blendermcp_use_hyper3d:
    handlers.update({ "create_rodin_job": self.create_rodin_job,
                      "poll_rodin_job_status": self.poll_rodin_job_status,
                      "import_generated_asset": self.import_generated_asset })
# ... Sketchfab and Hunyuan3D handlers added the same way ...

handler = handlers.get(cmd_type)
if handler:
    result = handler(**params)
    return {"status": "success", "result": result}
return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
```

> Note how the JSON `params` are splatted directly as keyword arguments (`handler(**params)`), which is why every handler's signature mirrors the `params` the MCP tool sends.

### 3. Reading the scene (`bpy` introspection)

`get_scene_info()` deliberately limits payload size (first 10 objects, rounded coordinates) to keep responses small. `get_object_info()` returns transforms, mesh statistics, and — for meshes — a world-space AABB computed by transforming the local bounding box corners through `matrix_world`:

```python
# addon.py
@staticmethod
def _get_aabb(obj):
    if obj.type != 'MESH':
        raise TypeError("Object must be a mesh")
    local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]
    world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]
    min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
    max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))
    return [[*min_corner], [*max_corner]]
```

The viewport screenshot uses a **context override** to target the 3D view, captures with `bpy.ops.screen.screenshot_area`, then loads/resizes via `bpy.data.images`:

```python
# addon.py — get_viewport_screenshot()
area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
with bpy.context.temp_override(area=area):
    bpy.ops.screen.screenshot_area(filepath=filepath)
img = bpy.data.images.load(filepath)
if max(img.size) > max_size:
    scale = max_size / max(img.size)
    img.scale(int(img.size[0]*scale), int(img.size[1]*scale))
    img.file_format = format.upper(); img.save()
bpy.data.images.remove(img)
```

### 4. The arbitrary-code escape hatch

`execute_code()` is the most powerful (and most dangerous) primitive. It `exec()`s the model's Python in a namespace seeded with `bpy`, capturing stdout so the model gets feedback:

```python
# addon.py — execute_code()
def execute_code(self, code):
    namespace = {"bpy": bpy}
    capture_buffer = io.StringIO()
    with redirect_stdout(capture_buffer):
        exec(code, namespace)
    captured_output = capture_buffer.getvalue()
    return {"executed": True, "result": captured_output}
```

> **Security note (from the README):** this runs unsandboxed Python with full Blender/file-system access. It is the reason most object creation, materials, lighting, and camera work "just works" — but it must be treated as a privileged capability when building on top of this project.

### 5. Asset library integrations (Poly Haven, Sketchfab)

These handlers call third-party REST APIs *from inside Blender* (using `requests`), download files, and import them with Blender's importers. Poly Haven requires a custom User-Agent header:

```python
# addon.py
REQ_HEADERS = requests.utils.default_headers()
REQ_HEADERS.update({"User-Agent": "blender-mcp"})

def get_polyhaven_categories(self, asset_type):
    response = requests.get(f"https://api.polyhaven.com/categories/{asset_type}", headers=REQ_HEADERS)
    return {"categories": response.json()}
```

`download_polyhaven_asset()` branches by asset type — importing models, building a material from texture maps, or setting an HDRI as the world environment (full logic spans `addon.py` ~lines 485–807). Sketchfab's `download_sketchfab_model()` downloads the model, imports it, then **normalizes** it so its largest dimension equals the caller-supplied `target_size` (so a "chair" comes in at ~1 m, a "car" at ~4.5 m). The MCP-side tool forces normalization on:

```python
# src/blender_mcp/server.py — download_sketchfab_model()
result = blender.send_command("download_sketchfab_model", {
    "uid": uid,
    "normalize_size": True,   # always normalize
    "target_size": target_size,
})
```

### 6. AI generation: Hyper3D Rodin (async job pattern)

Generation is asynchronous, exposed to the model as a **create → poll → import** sequence of three separate tools. The addon supports two backends selected by a Scene enum (`blendermcp_hyper3d_mode`), dispatched via `match`:

```python
# addon.py
def create_rodin_job(self, *args, **kwargs):
    match bpy.context.scene.blendermcp_hyper3d_mode:
        case "MAIN_SITE": return self.create_rodin_job_main_site(*args, **kwargs)
        case "FAL_AI":    return self.create_rodin_job_fal_ai(*args, **kwargs)
        case _:           return "Error: Unknown Hyper3D Rodin mode!"

def create_rodin_job_main_site(self, text_prompt=None, images=None, bbox_condition=None):
    files = [ *[("images", (f"{i:04d}{sfx}", img)) for i,(sfx,img) in enumerate(images or [])],
              ("tier", (None, "Sketch")), ("mesh_mode", (None, "Raw")) ]
    if text_prompt:    files.append(("prompt", (None, text_prompt)))
    if bbox_condition: files.append(("bbox_condition", (None, json.dumps(bbox_condition))))
    response = requests.post("https://hyperhuman.deemos.com/api/v2/rodin",
        headers={"Authorization": f"Bearer {bpy.context.scene.blendermcp_hyper3d_api_key}"},
        files=files)
    return response.json()
```

On the MCP-server side, the corresponding tool passes a normalized bbox and returns the job handles the model must keep for polling:

```python
# src/blender_mcp/server.py
def _process_bbox(original_bbox):
    if original_bbox is None: return None
    if all(isinstance(i, int) for i in original_bbox): return original_bbox
    if any(i <= 0 for i in original_bbox):
        raise ValueError("Incorrect number range: bbox must be bigger than zero!")
    return [int(float(i) / max(original_bbox) * 100) for i in original_bbox]

@telemetry_tool("generate_hyper3d_model_via_text")
@mcp.tool()
def generate_hyper3d_model_via_text(ctx, text_prompt, bbox_condition=None) -> str:
    result = blender.send_command("create_rodin_job",
        {"text_prompt": text_prompt, "images": None, "bbox_condition": _process_bbox(bbox_condition)})
    if result.get("submit_time", False):
        return json.dumps({"task_uuid": result["uuid"],
                           "subscription_key": result["jobs"]["subscription_key"]})
    return json.dumps(result)
```

**GLB import & cleanup.** After polling reports "Done", `import_generated_asset` downloads the GLB to a temp file and runs `_clean_imported_glb()`, which diffs the object set before/after import to find new objects, unparents a single mesh from its wrapper Empty, removes the Empty, and renames the mesh:

```python
# addon.py — _clean_imported_glb()
existing_objects = set(bpy.data.objects)
bpy.ops.import_scene.gltf(filepath=filepath)
bpy.context.view_layer.update()
imported_objects = list(set(bpy.data.objects) - existing_objects)
# ... if [Empty -> single MESH child]: unparent child, remove Empty, keep mesh ...
mesh_obj.name = mesh_name
```

### 7. AI generation: Tencent Hunyuan3D

Same async create/poll/import pattern, but with a notable detail: when using Tencent's official API, the addon implements the full **TC3-HMAC-SHA256** request signing in `get_tencent_cloud_sign_headers()` (nested `sign()` HMAC helper, canonical request, credential scope) — i.e. it hand-rolls Tencent Cloud auth in pure Python rather than depending on Tencent's SDK. A "LOCAL_API" mode is also supported for self-hosted Hunyuan3D endpoints.

### 8. Blender UI: panel, preferences, operators, registration

A sidebar panel (`BLENDERMCP_PT_Panel`, category `'BlenderMCP'`, in the `VIEW_3D` UI region) exposes the port, per-integration toggles, API-key fields, and Connect/Disconnect buttons:

```python
# addon.py — BLENDERMCP_PT_Panel.draw()
layout.prop(scene, "blendermcp_port")
layout.prop(scene, "blendermcp_use_polyhaven", text="Use assets from Poly Haven")
layout.prop(scene, "blendermcp_use_hyper3d", text="Use Hyper3D Rodin 3D model generation")
# ... conditional API-key / mode props per integration ...
if not scene.blendermcp_server_running:
    layout.operator("blendermcp.start_server", text="Connect to MCP server")
else:
    layout.operator("blendermcp.stop_server", text="Disconnect from MCP server")
```

The Connect button is an Operator that instantiates and starts the server, stashing it on `bpy.types` so it survives across operator calls:

```python
# addon.py — BLENDERMCP_OT_StartServer.execute()
if not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server:
    bpy.types.blendermcp_server = BlenderMCPServer(port=scene.blendermcp_port)
bpy.types.blendermcp_server.start()
scene.blendermcp_server_running = True
```

`register()` declares all configuration as `bpy.types.Scene` properties (port, the `use_*` integration booleans, mode enums, API keys), and an `AddonPreferences` class holds the `telemetry_consent` boolean (default `True`) shown in Edit > Preferences > Add-ons. There is also a free-trial key baked in (`RODIN_FREE_TRIAL_KEY`) that a button can populate.

### 9. Telemetry (anonymous, consent-aware)

Telemetry lives in two files and is layered on with a decorator so it never changes tool logic.

**The decorator** (`src/blender_mcp/telemetry_decorator.py`) wraps each tool, timing it and recording success/failure regardless of outcome (it detects sync vs async functions):

```python
# telemetry_decorator.py
def telemetry_tool(tool_name: str):
    def decorator(func):
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            start_time = time.time(); success = False; error = None
            try:
                result = func(*args, **kwargs); success = True; return result
            except Exception as e:
                error = str(e); raise
            finally:
                record_tool_usage(tool_name, success, (time.time()-start_time)*1000, error)
        ...
        return async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper
    return decorator
```

**The collector** (`src/blender_mcp/telemetry.py`) is a `TelemetryCollector` that: persists an anonymous per-machine UUID under an OS-appropriate data dir; runs a background worker thread draining a bounded `queue.Queue`; and inserts events into a **Supabase** table. Two opt-out / privacy layers exist:

- **Env-var kill switch** — `DISABLE_TELEMETRY` (and aliases) disables everything:

  ```python
  def _is_disabled(self) -> bool:
      for var in ("DISABLE_TELEMETRY", "BLENDER_MCP_DISABLE_TELEMETRY", "MCP_DISABLE_TELEMETRY"):
          if os.environ.get(var, "").lower() in ("true", "1", "yes", "on"):
              return True
      return False
  ```

- **Consent gate** — for *private* data (prompts, code, screenshots, metadata), the collector asks the Blender addon for the user's consent flag (`get_telemetry_consent`); without consent it strips prompts/metadata and sanitizes error messages, keeping only tool name / success / duration:

  ```python
  user_consent = self._check_user_consent()   # -> blender.send_command("get_telemetry_consent")
  if not user_consent:
      prompt_text = None
      metadata = None
      if error_message:
          error_message = "Error occurred (details withheld without consent)"
  ```

Events are sent off the hot path by the worker via `supabase.table("telemetry_events").insert(...)`.

### 10. Installation / runtime model

- **MCP server:** installed and launched by the AI client. e.g. Claude Desktop config runs `uvx blender-mcp`; Claude Code uses `claude mcp add blender uvx blender-mcp`. `uvx` fetches and runs the package on demand — the user does **not** run it manually in a terminal.
- **Blender addon:** the single `addon.py` is installed via Edit > Preferences > Add-ons > Install, enabled, and started with "Connect to Claude" in the sidebar.
- **Connection order:** start the Blender addon server first; the MCP server connects to it on first tool use (and reconnects automatically if the socket dies).

---

## Architecture Notes & Implications for Building On Top

- **The bridge pattern is the reusable core.** "MCP tool stub → JSON/TCP → main-thread `bpy` handler" cleanly separates *what the AI wants* from *how the host app does it*. The same pattern generalizes to controlling any scriptable desktop app.
- **`bpy.app.timers.register` is the linchpin** for safely marshaling work from the network thread onto Blender's main thread — essential for any single-threaded host application.
- **No message framing.** The protocol relies on "is it valid JSON yet?" rather than a length prefix. It works, but is fragile for very large or streamed payloads — worth hardening (length-prefixed frames) if you build something bigger on it.
- **`execute_blender_code` is both the superpower and the risk.** Most capability comes from arbitrary Python; a larger/production app should consider sandboxing, allow-lists, or confirmation flows around it.
- **Async generation is modeled as create/poll/import tools**, pushing long-running work out of the request/response timeout and letting the AI orchestrate the wait — a good template for any slow backend.
- **Feature-gating via host-side toggles** keeps the tool surface honest: the MCP server advertises tools, but the addon refuses commands for disabled integrations.

## Key Files Index

| File | Responsibility |
| --- | --- |
| `src/blender_mcp/server.py` | MCP server: `FastMCP` app, `BlenderConnection` socket client, all `@mcp.tool()` definitions, the `asset_creation_strategy` prompt, `main()` entry point |
| `addon.py` | Blender addon: `BlenderMCPServer` (threaded TCP server), main-thread dispatch via `bpy.app.timers`, all command handlers (`bpy` work + REST integrations), UI panel, operators, preferences, `register()`/`unregister()` |
| `src/blender_mcp/telemetry.py` | `TelemetryCollector`, UUID persistence, consent/env gating, Supabase sink, background worker |
| `src/blender_mcp/telemetry_decorator.py` | `telemetry_tool` decorator wrapping each MCP tool with timing + success tracking |
| `main.py` | Package entry shim → `blender_mcp.server.main` |
| `pyproject.toml` | Package metadata, `blender-mcp` console script, deps (`mcp[cli]`, `supabase`, `tomli`) |
| `README.md` | Install/usage, protocol summary, capabilities, telemetry & security notes |

## Tool ↔ Command Reference

| MCP tool (`server.py`) | Socket command (`addon.py` handler) | Purpose |
| --- | --- | --- |
| `get_scene_info` | `get_scene_info` | Scene summary |
| `get_object_info` | `get_object_info` | Object detail + AABB |
| `get_viewport_screenshot` | `get_viewport_screenshot` | Viewport PNG → `Image` |
| `execute_blender_code` | `execute_code` | Run arbitrary `bpy` Python |
| `get_polyhaven_status` / `get_polyhaven_categories` / `search_polyhaven_assets` / `download_polyhaven_asset` / `set_texture` | same names | Poly Haven assets |
| `get_sketchfab_status` / `search_sketchfab_models` / `get_sketchfab_model_preview` / `download_sketchfab_model` | same names | Sketchfab models |
| `get_hyper3d_status` / `generate_hyper3d_model_via_text` / `generate_hyper3d_model_via_images` / `poll_rodin_job_status` / `import_generated_asset` | `get_hyper3d_status` / `create_rodin_job` / `poll_rodin_job_status` / `import_generated_asset` | Hyper3D Rodin generation |
| `get_hunyuan3d_status` / `generate_hunyuan3d_model` / `poll_hunyuan_job_status` / `import_generated_asset_hunyuan` | `get_hunyuan3d_status` / `create_hunyuan_job` / `poll_hunyuan_job_status` / `import_generated_asset_hunyuan` | Tencent Hunyuan3D generation |
