"""JARVIS mini: a native, always-on-top macOS capsule and three recent turns.

Run with /usr/bin/python3 (PyObjC Cocoa). Cmd+Ctrl+J toggles the input;
Escape collapses replies, while Cmd+W hides without quitting.
The daemon owns persistent history. This view retains only the last three turns.
Socket and command helpers remain importable without AppKit, for headless use.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import math
import queue
import socket
import sys
import threading
import time
from collections.abc import Mapping
from typing import Optional

COMMANDS = (
    "24thoughts",
    "status",
    "beliefs",
    "location",
    "health",
    "actions",
    "goals",
    "sessions",
    "evolution",
    "tools",
    "skills",
    "memory",
)
COMMAND_ALIASES = {"goal": "goals"}
_LABELS = {
    "/24thoughts": "/24thoughts — 查看 24 小时内的自动思考",
    "/status": "/status — 当前运行状态",
    "/beliefs": "/beliefs — JARVIS 的核心信念",
    "/location": "/location — 我的当前位置",
    "/health": "/health — 健康数据概览",
    "/actions": "/actions — 最近执行的动作",
    "/goals": "/goals — 当前目标与进度",
    "/sessions": "/sessions — 可恢复的会话",
    "/evolution": "/evolution — 自进化提案与周期",
    "/tools": "/tools — 我能用的工具",
    "/skills": "/skills — 已安装技能",
    "/memory": "/memory — 我对你的记忆概况",
}
DEFAULT_SOCKET = "/tmp/jarvis.sock"
INPUT_WIDTH = 680
INPUT_HEIGHT = 56
HISTORY_GAP = 8
MAX_HISTORY_HEIGHT = 300
OFFLINE_MESSAGE = "JARVIS 尚未连接"


def parse_input(text: str) -> Optional[dict[str, str]]:
    """Map exact slash commands to queries; all other messages stream."""
    text = text.strip()
    if not text:
        return None
    if text.startswith("/"):
        command = COMMAND_ALIASES.get(text[1:], text[1:])
        if command not in COMMANDS:
            raise ValueError("Unknown command. Use: " + ", ".join("/" + c for c in COMMANDS))
        return {"type": "query", "query": command}
    return {"type": "stream_chat", "text": text}


def command_suggestions(text: str) -> list[str]:
    return ["/" + c for c in COMMANDS if ("/" + c).startswith(text)] if text.startswith("/") else []


def _pretty(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def render_response(response: Mapping, query: Optional[str] = None) -> str:
    """Preserve structured query values instead of inventing a summary."""
    if response.get("error") is not None:
        return f"Error: {response['error']}"
    if "reply" in response:
        return str(response["reply"])
    if query == "status":
        names = {
            "pid": "PID",
            "uptime_seconds": "Uptime (seconds)",
            "entry_count": "Entries",
            "latest_entry_at": "Latest entry",
        }
        return "\n".join(f"{names.get(k, k)}: {_pretty(v)}" for k, v in response.items())
    if isinstance(response.get("entries"), list):
        return (
            "\n\n".join(f"{i}. {_pretty(v)}" for i, v in enumerate(response["entries"], 1))
            or response.get("message")
            or "(empty)"
        )
    if "entry" in response:
        return _pretty(response["entry"])
    return _pretty(dict(response))


def panel_frame(screen: tuple, width: float = INPUT_WIDTH, height: float = INPUT_HEIGHT) -> tuple:
    """Cocoa bottom-left coordinates; INPUT center is 72% down the screen.

    Holding the bottom fixed lets replies grow upwards without moving the input.
    Screen origins are retained, including secondary displays with negative x/y.
    """
    sx, sy, sw, sh = screen
    width = min(width, sw - 32)
    # NSWindow integralizes frame edges outwards; fractional origins turn a
    # nominal 56pt capsule into 57pt. Pixel-aligned origins preserve its size.
    y = round(sy + sh * 0.28 - INPUT_HEIGHT / 2)
    height = min(height, sy + sh - y - 16)
    return round(sx + (sw - width) / 2), y, width, height


class SocketError(RuntimeError):
    """A transport/protocol failure. A sent chat is never automatically retried."""


class SocketClient:
    """One request per Unix connection, with bounded JSON/NDJSON responses."""

    def __init__(self, path=DEFAULT_SOCKET, timeout=600.0, max_bytes=4 * 1024 * 1024):
        if not math.isfinite(timeout) or timeout <= 0 or max_bytes <= 0:
            raise ValueError("timeout and max_bytes must be positive")
        self.path, self.timeout, self.max_bytes = path, timeout, max_bytes

    @staticmethod
    def _decode_frame(raw):
        try:
            frame = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SocketError("JARVIS returned invalid JSON") from exc
        if not isinstance(frame, dict):
            raise SocketError("JARVIS reply must be a JSON object")
        if "text" in frame and not isinstance(frame["text"], str):
            raise SocketError("JARVIS stream text must be a string")
        return frame

    def stream(self, payload: Mapping):
        """Yield deltas immediately, including frames split inside a UTF-8 codepoint."""
        if payload.get("type") != "stream_chat":
            yield self.request(payload)
            return
        wire = (json.dumps(dict(payload), ensure_ascii=False, allow_nan=False) + "\n").encode()
        deadline = time.monotonic() + self.timeout
        pending = bytearray()
        received = 0
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.path)
                connection.sendall(wire)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SocketError("JARVIS response timed out; not retried")
                    connection.settimeout(remaining)
                    chunk = connection.recv(65536)
                    if not chunk:
                        if pending:
                            frame = self._decode_frame(bytes(pending))
                            yield frame
                            if frame.get("done"):
                                return
                        raise SocketError("JARVIS stream ended without a final frame; not retried")
                    received += len(chunk)
                    if received > self.max_bytes:
                        raise SocketError("JARVIS reply exceeds the size limit")
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending = bytearray(rest)
                        if not line.strip():
                            continue
                        frame = self._decode_frame(line)
                        yield frame
                        if frame.get("done"):
                            return
        except OSError as exc:
            raise SocketError(f"Cannot communicate with {self.path}: {exc}. Not retried.") from exc

    def request(self, payload: Mapping) -> dict:
        """A query is a single JSON object; a streaming request is collected for CLI use."""
        if payload.get("type") == "stream_chat":
            parts = []
            for frame in self.stream(payload):
                parts.append(frame.get("text", ""))
                if frame.get("error"):
                    return {"error": frame["error"], "reply": "".join(parts)}
            return {"reply": "".join(parts)}
        wire = (json.dumps(dict(payload), ensure_ascii=False, allow_nan=False) + "\n").encode()
        data = bytearray()
        deadline = time.monotonic() + self.timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.path)
                connection.sendall(wire)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SocketError("JARVIS response timed out; not retried")
                    connection.settimeout(remaining)
                    chunk = connection.recv(min(65536, self.max_bytes + 1 - len(data)))
                    if not chunk:
                        raise SocketError("JARVIS closed before a complete JSON reply")
                    data.extend(chunk)
                    if len(data) > self.max_bytes:
                        raise SocketError("JARVIS reply exceeds the size limit")
                    framed = b"\n" in data
                    raw = bytes(data).split(b"\n", 1)[0] if framed else bytes(data)
                    try:
                        return self._decode_frame(raw)
                    except SocketError:
                        if framed:
                            raise
        except OSError as exc:
            raise SocketError(f"Cannot communicate with {self.path}: {exc}. Not retried.") from exc


class CarbonHotKey:
    """Cmd+J without Accessibility permission; retain the C callback until removal."""

    def __init__(self, callback):
        class EventType(ctypes.Structure):
            _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

        class HotKeyID(ctypes.Structure):
            _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

        self.lib = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        self.handler, self.reference = ctypes.c_void_p(), ctypes.c_void_p()
        callback_type = ctypes.CFUNCTYPE(
            ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )

        def receive(_next, _event, _context):
            callback()  # Queue only: Carbon may dispatch while AppKit is in a nested loop.
            return 0

        self.callback = callback_type(receive)
        self.lib.GetApplicationEventTarget.restype = ctypes.c_void_p
        self.lib.InstallEventHandler.argtypes = [
            ctypes.c_void_p,
            callback_type,
            ctypes.c_uint32,
            ctypes.POINTER(EventType),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.lib.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            HotKeyID,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.lib.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        self.lib.RemoveEventHandler.argtypes = [ctypes.c_void_p]
        target = self.lib.GetApplicationEventTarget()
        kind = EventType(int.from_bytes(b"keyb", "big"), 6)
        status = self.lib.InstallEventHandler(
            target, self.callback, 1, ctypes.byref(kind), None, ctypes.byref(self.handler)
        )
        if status:
            raise RuntimeError(f"InstallEventHandler failed ({status})")
        status = self.lib.RegisterEventHotKey(
            38,
            (1 << 8) | (1 << 12),
            HotKeyID(int.from_bytes(b"JVUI", "big"), 1),
            target,
            0,
            ctypes.byref(self.reference),
        )
        if status:
            self.close()
            raise RuntimeError(f"Cmd+J unavailable: RegisterEventHotKey returned {status}")

    def close(self):
        if self.reference.value:
            self.lib.UnregisterEventHotKey(self.reference)
            self.reference = ctypes.c_void_p()
        if self.handler.value:
            self.lib.RemoveEventHandler(self.handler)
            self.handler = ctypes.c_void_p()


def run_panel(client: SocketClient, *, hidden: bool = False, smoke_seconds: float = 0) -> int:
    """Native Cocoa only. Workers perform I/O; the main-thread timer paints deltas."""
    import AppKit as AK
    import Foundation as FN
    import objc

    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("AppKit must run on the main thread")
    # Apple's bundled PyObjC has Cocoa but not Quartz. Register the CF bridge so
    # layer colors are owned CGColor objects, not untyped pointers/warnings.
    core_graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    core_graphics.CGColorGetTypeID.restype = ctypes.c_ulong
    objc.registerCFSignature("CGColorRef", b"^{CGColor=}", core_graphics.CGColorGetTypeID())

    def color(red, green, blue, alpha=1):
        return AK.NSColor.colorWithSRGBRed_green_blue_alpha_(
            red / 255, green / 255, blue / 255, alpha
        )

    class JarvisMiniPanel(AK.NSPanel):
        def canBecomeKeyWindow(self):
            return True

        def canBecomeMainWindow(self):
            return False

        def performClose_(self, _sender):
            self.orderOut_(None)

        def close(self):
            self.orderOut_(None)

        def cancelOperation_(self, sender):
            self.delegate().collapse_(sender)

        def keyDown_(self, event):
            if event.keyCode() == 53:
                self.delegate().collapse_(None)
            else:
                objc.super(JarvisMiniPanel, self).keyDown_(event)

        def performKeyEquivalent_(self, event):
            if event.modifierFlags() & AK.NSEventModifierFlagCommand:
                if str(event.charactersIgnoringModifiers()).lower() == "w":
                    self.orderOut_(None)
                    return True
            return objc.super(JarvisMiniPanel, self).performKeyEquivalent_(event)

    class JarvisMiniInput(AK.NSTextField):
        def needsPanelToBecomeKey(self):
            return True

    class JarvisBubbleDocument(AK.NSView):
        def isFlipped(self):
            return True

    class JarvisMiniController(FN.NSObject):
        def applicationDidFinishLaunching_(self, _notification):
            self.events = queue.Queue()
            self.turns = []
            self.busy = False
            self.expanded = False
            self.suggestions = []
            self.selected = 0
            self.hotkey = None
            self.anchor_screen = AK.NSScreen.mainScreen()
            if self.anchor_screen is None:
                raise RuntimeError("No graphical macOS screen available")
            r = self.anchor_screen.frame()
            x, y, w, h = panel_frame((r.origin.x, r.origin.y, r.size.width, r.size.height))
            self.width = w
            self.base_y = y
            mask = AK.NSWindowStyleMaskBorderless | AK.NSWindowStyleMaskNonactivatingPanel
            self.panel = JarvisMiniPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                AK.NSMakeRect(x, y, w, h), mask, AK.NSBackingStoreBuffered, False
            )
            self.panel.setTitle_("JARVIS mini")
            self.panel.setDelegate_(self)
            self.panel.setFloatingPanel_(True)
            self.panel.setLevel_(AK.NSFloatingWindowLevel)
            self.panel.setHidesOnDeactivate_(False)
            self.panel.setCanHide_(False)
            self.panel.setBecomesKeyOnlyIfNeeded_(False)
            self.panel.setReleasedWhenClosed_(False)
            self.panel.setOpaque_(False)
            self.panel.setBackgroundColor_(AK.NSColor.clearColor())
            self.panel.setHasShadow_(True)
            self.panel.setMovableByWindowBackground_(False)
            self.panel.setCollectionBehavior_(
                AK.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AK.NSWindowCollectionBehaviorStationary
                | AK.NSWindowCollectionBehaviorIgnoresCycle
                | AK.NSWindowCollectionBehaviorFullScreenAuxiliary
            )
            self.panel.setAppearance_(AK.NSAppearance.appearanceNamed_(AK.NSAppearanceNameDarkAqua))
            self.root = AK.NSView.alloc().initWithFrame_(AK.NSMakeRect(0, 0, w, h))
            self.panel.setContentView_(self.root)
            # Two independent surfaces: the 8pt gap is genuinely transparent.
            # Tint is a SIBLING above the visual effect, not a vibrant child;
            # HUD material would otherwise brighten #3a3a3c behind our back.
            self.capsule_effect = self.effect(AK.NSMakeRect(0, 0, w, INPUT_HEIGHT), 28)
            self.root.addSubview_(self.capsule_effect)
            self.capsule = AK.NSView.alloc().initWithFrame_(AK.NSMakeRect(0, 0, w, INPUT_HEIGHT))
            self.capsule.setWantsLayer_(True)
            self.capsule.layer().setCornerRadius_(28)
            self.capsule.layer().setMasksToBounds_(True)
            self.capsule.layer().setBackgroundColor_(color(58, 58, 60).CGColor())
            self.capsule.layer().setBorderWidth_(1)
            self.capsule.layer().setBorderColor_(color(90, 90, 92).CGColor())
            self.root.addSubview_(self.capsule)
            self.history_effect = self.effect(AK.NSMakeRect(0, 64, w, 1), 20)
            self.history_effect.layer().setBackgroundColor_(color(28, 28, 30, 0.56).CGColor())
            self.history_effect.layer().setBorderWidth_(0.5)
            self.history_effect.layer().setBorderColor_(color(90, 90, 92, 0.65).CGColor())
            self.history_effect.setHidden_(True)
            self.root.addSubview_(self.history_effect)
            self.scroll = AK.NSScrollView.alloc().initWithFrame_(AK.NSMakeRect(0, 0, w, 1))
            self.scroll.setBorderType_(AK.NSNoBorder)
            self.scroll.setDrawsBackground_(False)
            self.scroll.setHasVerticalScroller_(True)
            self.scroll.setAutohidesScrollers_(True)
            self.scroll.setScrollerStyle_(AK.NSScrollerStyleOverlay)
            self.document = JarvisBubbleDocument.alloc().initWithFrame_(AK.NSMakeRect(0, 0, w, 1))
            self.scroll.setDocumentView_(self.document)
            self.history_effect.addSubview_(self.scroll)
            self.slash_button = self.button(
                "/", AK.NSMakeRect(12, 12, 32, 32), "commands:", color(76, 76, 78), 16
            )
            self.slash_button.setFont_(
                AK.NSFont.systemFontOfSize_weight_(19, AK.NSFontWeightMedium)
            )
            self.slash_button.setToolTip_("Commands")
            self.slash_button.setAccessibilityLabel_("JARVIS commands")
            self.input = JarvisMiniInput.alloc().initWithFrame_(AK.NSMakeRect(57, 16, w - 126, 24))
            self.input.setEditable_(True)
            self.input.setSelectable_(True)
            self.input.setBezeled_(False)
            self.input.setDrawsBackground_(False)
            self.input.setTextColor_(color(245, 245, 247))
            self.input.setFont_(AK.NSFont.systemFontOfSize_(16))
            self.input.setAlignment_(AK.NSTextAlignmentLeft)
            self.input.setFocusRingType_(AK.NSFocusRingTypeNone)
            self.input.setPlaceholderString_("Message JARVIS...")
            self.input.setAccessibilityLabel_("Message JARVIS")
            self.input.cell().setUsesSingleLineMode_(True)
            self.input.cell().setScrollable_(True)
            self.input.setDelegate_(self)
            self.input.setTarget_(self)
            self.input.setAction_("submit:")
            self.capsule.addSubview_(self.input)
            self.send_button = self.button(
                "↑", AK.NSMakeRect(w - 56, 11, 34, 34), "submit:", color(10, 132, 255), 17
            )
            self.send_button.setFont_(
                AK.NSFont.systemFontOfSize_weight_(23, AK.NSFontWeightSemibold)
            )
            self.send_button.setToolTip_("Send message (Return)")
            self.send_button.setAccessibilityLabel_("Send message")

            self.menu = AK.NSMenu.alloc().initWithTitle_("JARVIS commands")
            self.menu.setAutoenablesItems_(False)
            self.timer = (
                FN.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    0.03, self, "drain:", None, True
                )
            )
            # Keep stream painting and Cmd+J responsive in menu tracking modes too.
            FN.NSRunLoop.mainRunLoop().addTimer_forMode_(self.timer, FN.NSRunLoopCommonModes)
            try:
                self.hotkey = CarbonHotKey(
                    lambda: self.events.put(
                        ("hide", None) if self.panel.isVisible() else ("show", None)
                    )
                )
            except (OSError, RuntimeError) as exc:
                self.slash_button.setToolTip_(str(exc))
                print(str(exc), file=sys.stderr, flush=True)
            if not hidden:
                self.showPanel_(None)
            if smoke_seconds:
                self.smoke_timer = (
                    FN.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                        smoke_seconds, self, "stop:", None, False
                    )
                )

        @objc.python_method
        def effect(self, frame, radius):
            view = AK.NSVisualEffectView.alloc().initWithFrame_(frame)
            view.setMaterial_(AK.NSVisualEffectMaterialHUDWindow)
            view.setBlendingMode_(AK.NSVisualEffectBlendingModeBehindWindow)
            view.setState_(AK.NSVisualEffectStateActive)
            view.setWantsLayer_(True)
            view.layer().setCornerRadius_(radius)
            view.layer().setMasksToBounds_(True)
            return view

        @objc.python_method
        def button(self, title, frame, action, background, radius):
            button = AK.NSButton.alloc().initWithFrame_(frame)
            button.setTitle_(title)
            button.setBordered_(False)
            button.setBezelStyle_(AK.NSBezelStyleRegularSquare)
            button.setButtonType_(AK.NSButtonTypeMomentaryPushIn)
            button.setTarget_(self)
            button.setAction_(action)
            button.setFocusRingType_(AK.NSFocusRingTypeNone)
            button.setContentTintColor_(AK.NSColor.whiteColor())
            button.setWantsLayer_(True)
            button.layer().setBackgroundColor_(background.CGColor())
            button.layer().setCornerRadius_(radius)
            self.capsule.addSubview_(button)
            return button

        def showPanel_(self, _sender):
            # Cmd+Ctrl+J toggles: show when hidden, hide when visible.
            self.panel.setLevel_(AK.NSFloatingWindowLevel)
            self.panel.orderFrontRegardless()
            self.panel.makeKeyWindow()
            self.panel.makeFirstResponder_(self.input)

        def hidePanel_(self, _sender):
            self.panel.orderOut_(None)

        def windowShouldClose_(self, _sender):
            self.hidePanel_(None)
            return False

        def applicationShouldTerminateAfterLastWindowClosed_(self, _application):
            return False

        def applicationShouldHandleReopen_hasVisibleWindows_(self, _application, _visible):
            self.showPanel_(None)
            return True

        def collapse_(self, _sender):
            self.suggestions = []
            self.menu.cancelTracking()
            self.expanded = False
            self.layoutHistory_(None)
            self.panel.makeFirstResponder_(self.input)

        def commands_(self, _sender):
            prefix = str(self.input.stringValue())
            self.suggestions = command_suggestions(prefix if prefix.startswith("/") else "/")
            self.selected = 0
            self.menu.removeAllItems()
            for index, command in enumerate(self.suggestions):
                label = _LABELS.get(command, command)
                item = self.menu.addItemWithTitle_action_keyEquivalent_(label, "chooseCommand:", "")
                item.setTarget_(self)
                item.setTag_(index)
                item.setEnabled_(not self.busy)
            # NSMenu is a system floating list; it does not inflate the 56pt capsule.
            self.menu.popUpMenuPositioningItem_atLocation_inView_(
                None, AK.NSMakePoint(12, INPUT_HEIGHT + HISTORY_GAP), self.capsule
            )
            self.suggestions = []

        def chooseCommand_(self, sender):
            self.input.setStringValue_(self.suggestions[int(sender.tag())])
            self.suggestions = []
            self.menu.cancelTracking()
            self.submit_(None)

        def controlTextDidChange_(self, _notification):
            self.suggestions = command_suggestions(str(self.input.stringValue()))
            self.selected = 0

        @objc.typedSelector(b"Z@:@@:")
        def control_textView_doCommandBySelector_(self, _control, text_view, selector):
            name = selector.decode("ascii") if isinstance(selector, bytes) else str(selector)
            if name == "cancelOperation:":
                self.collapse_(None)
                return True
            if name == "insertNewline:":
                if (
                    text_view.hasMarkedText()
                ):  # Return first commits a Chinese/Japanese IME candidate.
                    return False
                self.submit_(None)
                return True
            if name == "complete:" and self.suggestions:
                self.input.setStringValue_(self.suggestions[0])
                return True
            return False

        @objc.python_method
        def bubble(self, text, user, y):
            max_text_width = self.width - 126
            font = AK.NSFont.systemFontOfSize_(14)
            paragraph = AK.NSMutableParagraphStyle.alloc().init()
            paragraph.setLineBreakMode_(AK.NSLineBreakByWordWrapping)
            paragraph.setLineSpacing_(2)
            attributes = {
                AK.NSFontAttributeName: font,
                AK.NSParagraphStyleAttributeName: paragraph,
                AK.NSForegroundColorAttributeName: color(248, 248, 250),
            }
            attributed = FN.NSAttributedString.alloc().initWithString_attributes_(
                text or "…", attributes
            )
            options = AK.NSStringDrawingUsesLineFragmentOrigin | AK.NSStringDrawingUsesFontLeading
            measured = attributed.boundingRectWithSize_options_(
                AK.NSMakeSize(max_text_width, 1000000), options
            )
            text_width = min(max_text_width, max(24, math.ceil(measured.size.width) + 2))
            text_height = max(19, math.ceil(measured.size.height) + 2)
            width, height = text_width + 26, text_height + 18
            x = self.width - width - 14 if user else 14
            view = AK.NSView.alloc().initWithFrame_(AK.NSMakeRect(x, y, width, height))
            view.setWantsLayer_(True)
            view.layer().setCornerRadius_(16)
            view.layer().setBackgroundColor_(
                (color(10, 115, 235) if user else color(58, 58, 60)).CGColor()
            )
            label = AK.NSTextField.alloc().initWithFrame_(
                AK.NSMakeRect(12, 8, width - 24, text_height + 2)
            )
            label.setBezeled_(False)
            label.setDrawsBackground_(False)
            label.setEditable_(False)
            label.setSelectable_(True)
            label.setFocusRingType_(AK.NSFocusRingTypeNone)
            label.cell().setWraps_(True)
            label.cell().setScrollable_(False)
            label.setAttributedStringValue_(attributed)
            label.setAccessibilityLabel_(("Sir: " if user else "JARVIS: ") + (text or "…"))
            view.addSubview_(label)
            self.document.addSubview_(view)
            return height

        def layoutHistory_(self, _sender):
            # Bound by three rounds, not three individual messages.
            for view in list(self.document.subviews()):
                view.removeFromSuperview()
            y = 12
            for turn in self.turns:
                y += self.bubble(turn["user"], True, y) + 8
                y += self.bubble(turn["reply"], False, y) + 12
            document_height = max(1, y)
            available = self.anchor_screen.frame().size.height * 0.72 - 52
            shown_height = (
                min(MAX_HISTORY_HEIGHT, document_height, max(1, available)) if self.expanded else 0
            )
            height = INPUT_HEIGHT + (HISTORY_GAP + shown_height if shown_height else 0)
            frame = self.panel.frame()
            self.panel.setFrame_display_(
                AK.NSMakeRect(frame.origin.x, self.base_y, self.width, height), True
            )
            self.root.setFrame_(AK.NSMakeRect(0, 0, self.width, height))
            self.history_effect.setHidden_(not bool(shown_height))
            if shown_height:
                self.history_effect.setFrame_(AK.NSMakeRect(0, 64, self.width, shown_height))
                self.scroll.setFrame_(AK.NSMakeRect(0, 0, self.width, shown_height))
                self.document.setFrame_(AK.NSMakeRect(0, 0, self.width, document_height))
                self.scroll.contentView().scrollToPoint_(
                    AK.NSMakePoint(0, max(0, document_height - shown_height))
                )
                self.scroll.reflectScrolledClipView_(self.scroll.contentView())

        def submit_(self, _sender):
            if self.busy:
                AK.NSBeep()
                return
            text = str(self.input.stringValue()).strip()
            if not text:
                return
            try:
                payload = parse_input(text)
            except ValueError as exc:
                self.turns.append({"user": text, "reply": str(exc)})
                self.turns = self.turns[-3:]
                self.expanded = True
                self.layoutHistory_(None)
                return
            self.turns.append({"user": text, "reply": ""})
            self.turns = self.turns[-3:]
            self.input.setStringValue_("")
            self.suggestions = []
            self.expanded = True
            self.busy = True
            self.send_button.setEnabled_(False)
            self.send_button.setTitle_("⋯")
            self.slash_button.setEnabled_(False)
            self.layoutHistory_(None)

            def work():
                try:
                    for frame in client.stream(payload):
                        if payload["type"] == "query":
                            self.events.put(("delta", render_response(frame, payload.get("query"))))
                        else:
                            if frame.get("text"):
                                self.events.put(("delta", frame["text"]))
                            if frame.get("error"):
                                self.events.put(("error", "JARVIS：" + str(frame["error"])))
                except SocketError as exc:
                    cause = exc.__cause__
                    offline = isinstance(cause, OSError) and cause.errno in (
                        errno.ENOENT,
                        errno.ECONNREFUSED,
                        errno.ENOTSOCK,
                    )
                    self.events.put(("error", OFFLINE_MESSAGE if offline else str(exc)))
                except Exception as exc:
                    self.events.put(("error", str(exc)))
                finally:
                    self.events.put(("done", None))

            threading.Thread(target=work, name="jarvis-mini-socket", daemon=True).start()

        def drain_(self, _timer):
            dirty = False
            for _ in range(512):  # Coalesce a burst of tokens into one layout per display tick.
                try:
                    kind, value = self.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "show":
                    self.showPanel_(None)
                elif kind == "hide":
                    self.hidePanel_(None)
                elif kind == "delta" and self.turns:
                    self.turns[-1]["reply"] += value
                    dirty = True
                elif kind == "error" and self.turns:
                    prefix = "\n\n" if self.turns[-1]["reply"] else ""
                    self.turns[-1]["reply"] += prefix + value
                    dirty = True
                elif kind == "done":
                    self.busy = False
                    self.send_button.setEnabled_(True)
                    self.send_button.setTitle_("↑")
                    self.slash_button.setEnabled_(True)
                    if self.turns and not self.turns[-1]["reply"]:
                        self.turns[-1]["reply"] = "JARVIS 未返回内容"
                        dirty = True
            # Escape stays collapsed even when more deltas arrive.
            if dirty:
                self.layoutHistory_(None)

        def stop_(self, _sender):
            AK.NSApplication.sharedApplication().terminate_(None)

        def applicationWillTerminate_(self, _notification):
            if self.hotkey is not None:
                self.hotkey.close()
            self.timer.invalidate()

    application = AK.NSApplication.sharedApplication()
    application.setActivationPolicy_(AK.NSApplicationActivationPolicyAccessory)
    menu = AK.NSMenu.alloc().init()
    item = AK.NSMenuItem.alloc().init()
    submenu = AK.NSMenu.alloc().init()
    submenu.addItemWithTitle_action_keyEquivalent_("Hide JARVIS", "hidePanel:", "w")
    submenu.addItemWithTitle_action_keyEquivalent_("Quit JARVIS mini", "terminate:", "q")
    item.setSubmenu_(submenu)
    menu.addItem_(item)
    application.setMainMenu_(menu)
    delegate = JarvisMiniController.alloc().init()
    application.setDelegate_(delegate)
    application.run()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--hidden", action="store_true", help="Wait for Cmd+J without showing")
    parser.add_argument(
        "--smoke-seconds",
        type=float,
        default=0,
        help="Run the real UI briefly, without sending any requests",
    )
    parser.add_argument("--request", metavar="TEXT", help="Headless one-shot chat or slash command")
    args = parser.parse_args(argv)
    if (
        not math.isfinite(args.timeout)
        or not math.isfinite(args.smoke_seconds)
        or args.timeout <= 0
        or args.smoke_seconds < 0
    ):
        parser.error("timeout must be positive and smoke-seconds nonnegative")
    client = SocketClient(args.socket, args.timeout)
    if args.request is not None:
        try:
            payload = parse_input(args.request)
            if payload:
                print(render_response(client.request(payload), payload.get("query")))
            return 0
        except (ValueError, SocketError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if sys.platform != "darwin":
        print("The native panel requires macOS.", file=sys.stderr)
        return 2
    try:
        return run_panel(client, hidden=args.hidden, smoke_seconds=args.smoke_seconds)
    except ImportError as exc:
        print(
            f"PyObjC unavailable in {sys.executable}: {exc}. Use /usr/bin/python3.", file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


def self_evaluate():
    """Evaluate public UI geometry and local-socket entry configuration."""
    from datetime import datetime, timezone

    valid = all(type(v) is int and v > 0 for v in (INPUT_HEIGHT, MAX_HISTORY_HEIGHT))
    return {
        "module": "jarvis_ui",
        "domain": "macOS native mini-panel chat interface over local socket",
        "current_version": "Cocoa panel with keyboard entry and bounded history geometry",
        "known_limitations": [
            "Native panel requires macOS and PyObjC.",
            "Evaluation is static and does not launch AppKit or connect to a daemon.",
        ],
        "improvement_directions": [
            "Expose native UI availability separately from headless request support."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "checks": {"positive_geometry": valid, "socket_configured": bool(DEFAULT_SOCKET)},
        "ok": valid and bool(DEFAULT_SOCKET),
    }
