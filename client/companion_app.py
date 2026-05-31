"""client.companion_app

Overlay-only macOS companion window for Forge (Step 4). Lifted from Wand and
stripped of the browser: there is no `BrowserView`, no Playwright/CDP screencast,
no headless-browser geometry. What remains is the **sidebar** (connection +
agent/tool status, webcam preview, conversation log, control buttons) plus the
floating hand-cursor overlay drawn by `HandCursorProvider`.

On launch it brings Blender up via `client.blender_launcher` (auto-starting the
addon socket server), opens a `BlenderConnection`, and runs `CompanionRuntime`
(mic in / audio out / 20 Hz cursor sender / reconnect). Tool calls from the
server are relayed to Blender by `LocalToolExecutor`.

Run:  python -m client.companion_app  [--ws-url ws://127.0.0.1:8000/ws]
"""

from __future__ import annotations

import argparse

import AppKit  # type: ignore
import Foundation  # type: ignore
import cv2  # type: ignore
import objc  # type: ignore

from client.blender_bridge import BlenderConnection
from client.blender_launcher import DEFAULT_BLENDER_APP, ensure_blender_running
from client.companion_runtime import CompanionRuntime
from client.companion_state import CompanionState, EventEntry
from client.cursor.displays import get_builtin_display_geometry
from client.cursor.mapper import get_main_display_size
from client.cursor.provider import HandCursorProvider
from client.session_ids import generate_session_id, get_stable_user_id, normalize_ws_root_url


def _get_builtin_nsscreen():
    """Return the NSScreen for the Mac's built-in display (fallback: mainScreen)."""
    try:
        import Quartz  # type: ignore  # noqa: F401

        geom = get_builtin_display_geometry()
        for screen in AppKit.NSScreen.screens():
            desc = screen.deviceDescription()
            sid = desc.get("NSScreenNumber")
            if sid is not None and int(sid) == geom.display_id:
                return screen
    except Exception:
        pass
    return AppKit.NSScreen.mainScreen()


# Local scaffold server by default (Step 4 WS sink); the user-id is appended.
DEFAULT_WS_BASE = "ws://127.0.0.1:8000/ws"

PANEL_W = 380  # sidebar width in points

HEADER_H = 50
AGENT_H = 70
WEBCAM_H = int(PANEL_W * 9 / 16)  # ≈ 213px for 16:9 aspect ratio
CALIB_H = 28
BUTTON_H = 44
DEBUG_H = 150
LOG_BOTTOM = BUTTON_H + DEBUG_H + 4  # y of conversation log bottom edge


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Forge companion window (overlay + Blender bridge)")
    p.add_argument("--ws-url", default=DEFAULT_WS_BASE,
                   help="WebSocket base URL up to and including /ws (user-id appended automatically)")
    p.add_argument("--user-id", default=None,
                   help="Stable user ID for this machine (auto-generated from hostname on first run)")
    p.add_argument("--camera-index", type=int, default=0)
    p.add_argument("--hand-smoothing", type=float, default=0.25)
    p.add_argument("--cursor-stale-ms", type=int, default=400)
    p.add_argument("--cursor-send-hz", type=float, default=20.0)
    p.add_argument("--hand-overlay", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--hand-overlay-radius", type=int, default=10)
    p.add_argument("--hand-mirror", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--landmark", type=int, default=8,
                   help="8=index fingertip (wide range), 9=palm (steady)")
    p.add_argument("--blender-port", type=int, default=9876)
    p.add_argument("--blender-app", default=DEFAULT_BLENDER_APP)
    p.add_argument("--no-launch-blender", action="store_true",
                   help="Do not spawn Blender; attach only if its socket is already up")
    return p


def _make_label(frame, text: str, *, font_size: float = 12.0, bold: bool = False, color=None):
    label = AppKit.NSTextField.alloc().initWithFrame_(frame)
    label.setStringValue_(text)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setBordered_(False)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    font = AppKit.NSFont.boldSystemFontOfSize_(font_size) if bold else AppKit.NSFont.systemFontOfSize_(font_size)
    label.setFont_(font)
    if color is not None:
        label.setTextColor_(color)
    return label


def _ns_image_from_bgr(frame):
    ok, encoded = cv2.imencode(".jpg", frame)
    if not ok:
        return None
    payload = encoded.tobytes()
    data = Foundation.NSData.dataWithBytes_length_(payload, len(payload))
    return AppKit.NSImage.alloc().initWithData_(data)


def _format_event_log(events: list[EventEntry]) -> str:
    # Collapse consecutive speech events of the same type — streaming sends
    # incremental partials followed by a final complete transcript, so we keep
    # only the last entry in each consecutive run of user_spoke/agent_spoke.
    SPEECH = {"user_spoke", "agent_spoke"}
    collapsed: list[EventEntry] = []
    for e in events:
        if e.event in {"cursor_sent", "cursor_received"}:
            continue
        if collapsed and collapsed[-1].event == e.event and e.event in SPEECH:
            collapsed[-1] = e  # replace partial with newer (longer) version
        else:
            collapsed.append(e)

    lines: list[str] = []
    for e in collapsed:
        if e.event == "session_connected":
            lines.append("─── connected ───")
        elif e.event == "session_disconnected":
            lines.append("─── disconnected ───")
        elif e.event == "session_error":
            lines.append(f"  ✗ {e.summary[:60]}")
        elif e.event == "user_spoke":
            lines.append(f"You: {e.summary}")
        elif e.event == "agent_spoke":
            name = e.agent_name or "Agent"
            lines.append(f"{name}: {e.summary}")
        elif e.event == "agent_started" and e.agent_name:
            lines.append(f"  → {e.agent_name}")
        elif e.tool_name:
            icon = "✓" if e.status == "ok" else "✗"
            lines.append(f"  {icon} {e.tool_name}: {e.summary[:50]}")
    return "\n".join(lines[-40:])


# ---------------------------------------------------------------------------
# Bridge (NSObject with selector methods)
# ---------------------------------------------------------------------------

class _ControllerBridge(AppKit.NSObject):  # type: ignore[misc, valid-type]
    def initWithOwner_(self, owner):
        self = objc.super(_ControllerBridge, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def tick_(self, timer) -> None:
        self.owner.tick()

    def toggleMute_(self, sender) -> None:
        self.owner.toggle_mute()

    def reconnect_(self, sender) -> None:
        self.owner.request_reconnect()

    def recalibrate_(self, sender) -> None:
        self.owner.recalibrate()

    def toggleDebug_(self, sender) -> None:
        self.owner.toggle_debug()

    def quit_(self, sender) -> None:
        AppKit.NSApp.terminate_(None)

    def windowWillClose_(self, notification) -> None:
        AppKit.NSApp.terminate_(None)


# ---------------------------------------------------------------------------
# Main controller
# ---------------------------------------------------------------------------

class CompanionWindowController:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        user_id = args.user_id or get_stable_user_id()
        full_ws_url = f"{args.ws_url.rstrip('/')}/{user_id}"
        self.ws_root_url = normalize_ws_root_url(full_ws_url)
        self.state = CompanionState(session_id=generate_session_id())
        self.provider = HandCursorProvider(
            camera_index=args.camera_index,
            smoothing=args.hand_smoothing,
            stale_timeout_s=max(0.0, args.cursor_stale_ms / 1000.0),
            tracker_start_timeout_s=8.0,
            mirror=args.hand_mirror,
            preview=True,
            preview_window=False,
            landmark_id=args.landmark,
            overlay=args.hand_overlay,
            overlay_radius=args.hand_overlay_radius,
        )
        self.bridge = BlenderConnection(port=args.blender_port)
        self.blender_handle = None
        self.runtime: CompanionRuntime | None = None
        self.debug_visible = False
        self._bridge = _ControllerBridge.alloc().initWithOwner_(self)
        self._timer = None

        self.window = None
        self.connection_label = None
        self.session_label = None
        self.agent_label = None
        self.tool_label = None
        self.webcam_view = None
        self.calibration_label = None
        self.log_scroll = None
        self.log_text = None
        self.mute_button = None
        self.debug_button = None
        self.debug_scroll = None
        self.debug_text = None

    def start(self) -> None:
        if not self.provider.start():
            raise RuntimeError(f"failed to start cursor provider: {self.provider.status()}")

        # Bring Blender up (or attach) so the socket bridge is live on connect.
        try:
            self.blender_handle = ensure_blender_running(
                port=self.args.blender_port,
                blender_app=self.args.blender_app,
                spawn=not self.args.no_launch_blender,
            )
            mode = "attached to" if self.blender_handle.attached else "launched"
            print(f"[forge] {mode} Blender on :{self.args.blender_port}")
            self.state.record_local_event(
                request_id=self.state.session_id,
                event="blender_ready",
                status="ok",
                summary=f"{mode} Blender on :{self.args.blender_port}",
            )
        except Exception as exc:  # keep the overlay/app usable even if Blender fails
            print(f"[forge] WARNING: Blender not available: {exc}")
            self.state.record_local_event(
                request_id=self.state.session_id,
                event="blender_ready",
                status="error",
                summary=str(exc),
            )

        self.runtime = CompanionRuntime(
            ws_url=self.ws_root_url,
            provider=self.provider,
            state=self.state,
            bridge=self.bridge,
            cursor_send_hz=self.args.cursor_send_hz,
        )

        self._build_window()
        self.window.makeKeyAndOrderFront_(None)
        self.runtime.start()
        self._timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1,
            self._bridge,
            "tick:",
            None,
            True,
        )

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        if self.runtime is not None:
            self.runtime.stop()
        self.provider.stop()
        self.bridge.disconnect()
        # Deliberately do NOT terminate Blender — leave the user's session intact.

    def toggle_mute(self) -> None:
        if self.runtime is None:
            return
        muted = self.runtime.toggle_mute()
        if self.mute_button is not None:
            self.mute_button.setTitle_("Unmute" if muted else "Mute")

    def request_reconnect(self) -> None:
        if self.runtime is not None:
            self.runtime.request_reconnect()

    def recalibrate(self) -> None:
        """Anchor the hand cursor to the Blender viewport (for region picking).

        The overlay dot already tracks the hand without calibration (absolute
        linear inner-area map); this 2-corner anchoring is what makes
        `pick_object_at` land correctly, exercised by Step 5. Blocking, on the
        main thread — the user parks the dot on the viewport's TL then BR corner.
        """
        self.state.set_calibration_state("uncalibrated", "Anchoring to the Blender viewport…")
        try:
            region = self.bridge.send_command("get_view_geometry")["region"]
        except Exception as exc:
            self.state.set_calibration_state("uncalibrated", f"Blender unavailable: {exc}")
            return
        ok, msg = self.provider.calibrate_viewport_anchors(
            region["width"], region["height"], announce=self._calibration_announce
        )
        self.state.set_calibration_state("calibrated" if ok else "uncalibrated", msg)
        self.state.record_local_event(
            request_id=self.state.session_id,
            event="cursor_calibration",
            status="ok" if ok else "error",
            summary=msg,
        )

    def toggle_debug(self) -> None:
        self.debug_visible = not self.debug_visible
        if self.debug_scroll is not None:
            self.debug_scroll.setHidden_(not self.debug_visible)
        if self.debug_button is not None:
            self.debug_button.setTitle_("Hide Debug" if self.debug_visible else "Debug")

    def tick(self) -> None:
        if self.runtime is None:
            return
        self.runtime.poll_capture()
        snapshot = self.state.snapshot()

        # --- Header ---
        if self.connection_label is not None:
            dot = "● " if snapshot.connected else "○ "
            status = "Connected" if snapshot.connected else "Reconnecting..."
            self.connection_label.setStringValue_(dot + status)
        if self.session_label is not None:
            sid = snapshot.session_id
            if "_" in sid:
                sid = sid.rsplit("_", 1)[-1]
            self.session_label.setStringValue_(f"session:{sid[:4]}")

        # --- Agent / Tool ---
        if self.agent_label is not None:
            self.agent_label.setStringValue_(snapshot.current_agent or "–")
        if self.tool_label is not None:
            self.tool_label.setStringValue_(snapshot.current_tool or "")

        # --- Calibration ---
        if self.calibration_label is not None:
            self.calibration_label.setStringValue_(
                f"{snapshot.calibration_state}  {snapshot.calibration_message or ''}"
            )

        # --- Conversation log ---
        if self.log_text is not None:
            text = _format_event_log(snapshot.latest_events)
            self.log_text.setString_(text)
            self.log_text.scrollRangeToVisible_(Foundation.NSMakeRange(len(text), 0))

        # --- Debug ---
        if self.debug_text is not None and self.debug_visible:
            lines = [
                f"[{e.source}] {e.event} {e.status}  {e.summary[:40]}"
                for e in list(snapshot.latest_events)[-16:]
            ]
            self.debug_text.setString_("\n".join(lines))

        # --- Webcam preview in sidebar ---
        self._update_webcam_preview(snapshot)

    def _build_window(self) -> None:
        screen = _get_builtin_nsscreen()  # always the MacBook's built-in display
        visible = screen.visibleFrame()

        # Sidebar-only window: PANEL_W wide, full height, parked at the right edge
        # so it doesn't cover the Blender viewport.
        wh = int(visible.size.height)
        wx = int(visible.origin.x + visible.size.width - PANEL_W)
        wy = int(visible.origin.y)

        mask = (
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskMiniaturizable
        )
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, PANEL_W, wh),
            mask,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        window.setFrame_display_(AppKit.NSMakeRect(wx, wy, PANEL_W, wh), False)
        window.setTitle_("Forge")
        window.setDelegate_(self._bridge)
        content = window.contentView()

        ch = int(content.frame().size.height)
        pad = 10  # horizontal padding

        # --- 1. Header bar (top 50px) ---
        header_y = ch - HEADER_H
        self.connection_label = _make_label(
            AppKit.NSMakeRect(pad, header_y + 16, PANEL_W - 120, 22),
            "● Connecting...",
            bold=True,
            font_size=13.0,
        )
        self.session_label = _make_label(
            AppKit.NSMakeRect(PANEL_W - 110, header_y + 18, 100, 16),
            "",
            font_size=10.0,
            color=AppKit.NSColor.secondaryLabelColor(),
        )
        content.addSubview_(self.connection_label)
        content.addSubview_(self.session_label)

        hsep = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(0, header_y - 1, PANEL_W, 1))
        hsep.setBoxType_(AppKit.NSBoxSeparator)
        content.addSubview_(hsep)

        # --- 2. Agent / Tool (70px) ---
        agent_y = header_y - AGENT_H
        self.agent_label = _make_label(
            AppKit.NSMakeRect(pad, agent_y + 36, PANEL_W - pad * 2, 26),
            "–",
            bold=True,
            font_size=18.0,
        )
        self.tool_label = _make_label(
            AppKit.NSMakeRect(pad, agent_y + 14, PANEL_W - pad * 2, 20),
            "",
            font_size=12.0,
            color=AppKit.NSColor.secondaryLabelColor(),
        )
        content.addSubview_(self.agent_label)
        content.addSubview_(self.tool_label)

        # --- 3. Webcam preview (213px ≈ 380 * 9/16) ---
        webcam_y = agent_y - WEBCAM_H
        self.webcam_view = AppKit.NSImageView.alloc().initWithFrame_(
            AppKit.NSMakeRect(pad, webcam_y, PANEL_W - pad * 2, WEBCAM_H)
        )
        self.webcam_view.setImageScaling_(AppKit.NSImageScaleAxesIndependently)
        content.addSubview_(self.webcam_view)

        # --- 4. Calibration strip (28px) ---
        calib_y = webcam_y - CALIB_H
        self.calibration_label = _make_label(
            AppKit.NSMakeRect(pad, calib_y + 6, PANEL_W - pad * 2, 18),
            "Calibration: uncalibrated",
            font_size=10.0,
            color=AppKit.NSColor.secondaryLabelColor(),
        )
        content.addSubview_(self.calibration_label)

        mid_sep = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(0, calib_y - 2, PANEL_W, 1))
        mid_sep.setBoxType_(AppKit.NSBoxSeparator)
        content.addSubview_(mid_sep)

        # --- 5. Button bar (bottom 44px) ---
        btn_y = 0
        btn_w = (PANEL_W - pad * 2 - 4 * 3) // 5  # 5 buttons with small gaps

        self.mute_button = _make_small_button("Mute", AppKit.NSMakeRect(pad, btn_y + 7, btn_w, 28))
        self.mute_button.setTarget_(self._bridge)
        self.mute_button.setAction_("toggleMute:")
        content.addSubview_(self.mute_button)

        reconnect_btn = _make_small_button("Reconnect", AppKit.NSMakeRect(pad + btn_w + 3, btn_y + 7, btn_w + 10, 28))
        reconnect_btn.setTarget_(self._bridge)
        reconnect_btn.setAction_("reconnect:")
        content.addSubview_(reconnect_btn)

        calibrate_btn = _make_small_button("Calibrate", AppKit.NSMakeRect(pad + btn_w * 2 + 16, btn_y + 7, btn_w + 10, 28))
        calibrate_btn.setTarget_(self._bridge)
        calibrate_btn.setAction_("recalibrate:")
        content.addSubview_(calibrate_btn)

        self.debug_button = _make_small_button("Debug", AppKit.NSMakeRect(pad + btn_w * 3 + 30, btn_y + 7, btn_w, 28))
        self.debug_button.setTarget_(self._bridge)
        self.debug_button.setAction_("toggleDebug:")
        content.addSubview_(self.debug_button)

        quit_btn = _make_small_button("Quit", AppKit.NSMakeRect(PANEL_W - pad - btn_w, btn_y + 7, btn_w, 28))
        quit_btn.setTarget_(self._bridge)
        quit_btn.setAction_("quit:")
        content.addSubview_(quit_btn)

        btn_sep = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(0, BUTTON_H, PANEL_W, 1))
        btn_sep.setBoxType_(AppKit.NSBoxSeparator)
        content.addSubview_(btn_sep)

        # --- 6. Debug console (150px above buttons, hidden by default) ---
        self.debug_text = AppKit.NSTextView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, PANEL_W - 14, DEBUG_H)
        )
        self.debug_text.setEditable_(False)
        self.debug_text.setFont_(
            AppKit.NSFont.monospacedSystemFontOfSize_weight_(9.0, AppKit.NSFontWeightRegular)
        )
        self.debug_scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            AppKit.NSMakeRect(pad, BUTTON_H + 2, PANEL_W - pad * 2, DEBUG_H)
        )
        self.debug_scroll.setDocumentView_(self.debug_text)
        self.debug_scroll.setHasVerticalScroller_(True)
        self.debug_scroll.setHidden_(True)
        content.addSubview_(self.debug_scroll)

        # --- 7. Conversation log (flexible middle area) ---
        log_y = LOG_BOTTOM
        log_h = calib_y - 6 - log_y
        if log_h < 60:
            log_h = 60

        self.log_text = AppKit.NSTextView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, PANEL_W - 14, max(log_h, 200))
        )
        self.log_text.setEditable_(False)
        self.log_text.setFont_(AppKit.NSFont.systemFontOfSize_(11.0))
        self.log_scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            AppKit.NSMakeRect(pad, log_y, PANEL_W - pad * 2, log_h)
        )
        self.log_scroll.setDocumentView_(self.log_text)
        self.log_scroll.setHasVerticalScroller_(True)
        content.addSubview_(self.log_scroll)

        self.window = window

    def _update_webcam_preview(self, snapshot) -> None:
        if self.webcam_view is None or self.runtime is None:
            return
        frame = self.runtime.get_preview_frame()
        if frame is None:
            return
        composed = frame.copy()
        height, width = composed.shape[:2]
        screen_w, screen_h = get_main_display_size()

        if snapshot.fingertip is not None:
            px = int(snapshot.fingertip.x * (width - 1))
            py = int(snapshot.fingertip.y * (height - 1))
            cv2.circle(composed, (px, py), 10, (0, 255, 255), -1)

        def draw_cursor(point, color) -> None:
            if point is None:
                return
            px = int((point.x / max(1, screen_w - 1)) * (width - 1))
            py = int((point.y / max(1, screen_h - 1)) * (height - 1))
            cv2.circle(composed, (px, py), 9, color, 2)

        draw_cursor(snapshot.local_cursor, (0, 255, 0))
        draw_cursor(snapshot.server_cursor, (0, 100, 255))

        image = _ns_image_from_bgr(composed)
        if image is not None:
            self.webcam_view.setImage_(image)

    def _calibration_announce(self, message: str) -> None:
        self.state.set_calibration_state("uncalibrated", message)


def _make_small_button(title: str, frame) -> AppKit.NSButton:
    btn = AppKit.NSButton.alloc().initWithFrame_(frame)
    btn.setTitle_(title)
    btn.setFont_(AppKit.NSFont.systemFontOfSize_(11.0))
    btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
    return btn


# ---------------------------------------------------------------------------
# App delegate
# ---------------------------------------------------------------------------

class _AppDelegate(AppKit.NSObject):  # type: ignore[misc, valid-type]
    def initWithArgs_(self, args):
        self = objc.super(_AppDelegate, self).init()
        if self is None:
            return None
        self.args = args
        self.controller = None
        return self

    def applicationDidFinishLaunching_(self, notification) -> None:
        self.controller = CompanionWindowController(self.args)
        self.controller.start()
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        if self.controller.window is not None:
            self.controller.window.makeKeyAndOrderFront_(None)
            self.controller.window.orderFrontRegardless()

    def applicationWillTerminate_(self, notification) -> None:
        if self.controller is not None:
            self.controller.stop()


def main() -> None:
    args = build_arg_parser().parse_args()
    app = AppKit.NSApplication.sharedApplication()
    dark = AppKit.NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")
    if dark is not None:
        app.setAppearance_(dark)
    delegate = _AppDelegate.alloc().initWithArgs_(args)
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
