"""Small local browser chooser for Google reference images."""

from __future__ import annotations

import html
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol, Sequence


class ReferenceLike(Protocol):
    title: str
    image_url: str
    thumbnail_url: str
    context_url: str


class ReferencePickerUnavailable(RuntimeError):
    """Raised when the local browser picker cannot be opened."""


def choose_reference_image(
    references: Sequence[ReferenceLike],
    *,
    title: str = "Choose Reference Image",
    timeout_seconds: float = 300.0,
) -> int | None:
    """Open a local image picker in the default browser and return the chosen index."""

    if not references:
        return None

    selected: dict[str, int | None] = {"index": None}
    chosen = threading.Event()
    html_body = _build_picker_html(references, title=title)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/choose":
                params = urllib.parse.parse_qs(parsed.query)
                try:
                    index = int((params.get("index") or [""])[0])
                except ValueError:
                    index = -1
                if 0 <= index < len(references):
                    selected["index"] = index
                chosen.set()
                self._send_html(_selection_done_html())
                return
            if parsed.path == "/cancel":
                chosen.set()
                self._send_html(_selection_done_html(cancelled=True))
                return
            self._send_html(html_body)

        def log_message(self, _format: str, *_args) -> None:
            return None

        def _send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except OSError as exc:
        raise ReferencePickerUnavailable(str(exc)) from exc

    thread = threading.Thread(target=server.serve_forever, name="reference-picker", daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        opened = webbrowser.open(url, new=1, autoraise=True)
        if not opened:
            raise ReferencePickerUnavailable("Default browser did not open.")
        chosen.wait(timeout=max(1.0, float(timeout_seconds)))
        return selected["index"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def best_reference_image_url(reference: ReferenceLike) -> str:
    return reference.thumbnail_url or reference.image_url


def _build_picker_html(references: Sequence[ReferenceLike], *, title: str) -> str:
    escaped_title = html.escape(title)
    cards = "\n".join(_reference_card(reference, index) for index, reference in enumerate(references))
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #1f2933;
      background: #f5f7fa;
    }}
    body {{
      margin: 0;
      padding: 18px;
      max-width: 780px;
    }}
    h1 {{
      font-size: 18px;
      font-weight: 650;
      margin: 0 0 12px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
    }}
    .card {{
      display: block;
      min-height: 212px;
      padding: 8px;
      border: 1px solid #c8d0da;
      border-radius: 8px;
      background: #ffffff;
      color: inherit;
      text-decoration: none;
    }}
    .card:hover {{
      border-color: #3777d6;
      box-shadow: 0 3px 12px rgba(31, 41, 55, 0.16);
    }}
    img {{
      width: 100%;
      height: 138px;
      object-fit: contain;
      background: #eef2f6;
      border-radius: 6px;
    }}
    .title {{
      margin-top: 8px;
      font-size: 13px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .source {{
      margin-top: 5px;
      font-size: 11px;
      color: #5b6876;
      overflow-wrap: anywhere;
    }}
    .footer {{
      margin-top: 14px;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: #5b6876;
      font-size: 12px;
    }}
    .cancel {{
      color: #b42318;
      text-decoration: none;
    }}
  </style>
</head>
<body>
  <script>
    try {{ window.resizeTo(820, 620); window.focus(); }} catch (_error) {{}}
  </script>
  <h1>Choose a reference image</h1>
  <div class="grid">
    {cards}
  </div>
  <div class="footer">
    <span>Click an image to refine the selected Blender target.</span>
    <a class="cancel" href="/cancel">Cancel</a>
  </div>
</body>
</html>
"""


def _reference_card(reference: ReferenceLike, index: int) -> str:
    title = _trim_title(reference.title)
    image_url = html.escape(best_reference_image_url(reference), quote=True)
    source = html.escape(_host_label(reference.context_url), quote=False)
    return f"""<a class="card" href="/choose?index={index}">
  <img src="{image_url}" alt="">
  <div class="title">{index + 1}. {html.escape(title)}</div>
  <div class="source">{source}</div>
</a>"""


def _selection_done_html(*, cancelled: bool = False) -> str:
    message = "Cancelled. You can close this window." if cancelled else "Selected. Returning to Forge..."
    return f"""<!doctype html>
<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;">
  <p>{html.escape(message)}</p>
  <script>setTimeout(() => window.close(), 250);</script>
</body>
</html>"""


def _host_label(url: str) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc or url


def _trim_title(title: str, *, limit: int = 42) -> str:
    normalized = " ".join(str(title or "Untitled image").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."
