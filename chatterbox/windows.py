"""Small, explicitly typed Win32 surface; no keyboard hook or key logging."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import os
import threading
import time

if os.name != "nt":
    raise RuntimeError("Chatterbox requires Windows.")

user32 = C.WinDLL("user32", use_last_error=True)
kernel32 = C.WinDLL("kernel32", use_last_error=True)
ULONG_PTR = C.c_size_t


def bind(dll, name, restype, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(args)
    return fn


GetForegroundWindow = bind(user32, "GetForegroundWindow", W.HWND)
IsWindow = bind(user32, "IsWindow", W.BOOL, W.HWND)
IsIconic = bind(user32, "IsIconic", W.BOOL, W.HWND)
ShowWindow = bind(user32, "ShowWindow", W.BOOL, W.HWND, C.c_int)
SetForegroundWindow = bind(user32, "SetForegroundWindow", W.BOOL, W.HWND)
GetWindowThreadProcessId = bind(user32, "GetWindowThreadProcessId", W.DWORD, W.HWND, C.POINTER(W.DWORD))
GetAsyncKeyState = bind(user32, "GetAsyncKeyState", C.c_short, C.c_int)
GetCurrentThreadId = bind(kernel32, "GetCurrentThreadId", W.DWORD)
RegisterHotKey = bind(user32, "RegisterHotKey", W.BOOL, W.HWND, C.c_int, W.UINT, W.UINT)
UnregisterHotKey = bind(user32, "UnregisterHotKey", W.BOOL, W.HWND, C.c_int)
GetMessage = bind(user32, "GetMessageW", C.c_int, C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
PeekMessage = bind(user32, "PeekMessageW", W.BOOL, C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT)
PostThreadMessage = bind(user32, "PostThreadMessageW", W.BOOL, W.DWORD, W.UINT, W.WPARAM, W.LPARAM)
GetAncestor = bind(user32, "GetAncestor", W.HWND, W.HWND, W.UINT)
GetClassName = bind(user32, "GetClassNameW", C.c_int, W.HWND, W.LPWSTR, C.c_int)


def window_class(hwnd):
    value = C.create_unicode_buffer(256)
    GetClassName(hwnd, value, len(value))
    return value.value


def enable_system_dpi_awareness():
    """Best-effort: declare the process DPI-aware before Tk initializes.

    Tcl/Tk on Windows reads the process's DPI-awareness state once, at
    interpreter startup, and self-configures its internal pixel scaling from
    it. Calling this before tk.Tk() is constructed is what makes that happen;
    calling it after has no effect. System (not per-monitor) awareness is
    used deliberately: Tk does not react to WM_DPICHANGED, so per-monitor
    awareness would leave the window stale (not crisp) after being dragged
    to a differently scaled monitor until restarted — system awareness avoids
    that half-updated state by never trying to track the change at all.
    """
    try:
        shcore = C.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except (OSError, AttributeError):
        pass


def set_taskbar_identity():
    """Give the process its own taskbar identity instead of inheriting
    pythonw.exe's embedded file description ("IDLE (Python 3.12 64-bit)"),
    which Windows shows in the taskbar/Alt-Tab tooltip for any process that
    hasn't declared its own AppUserModelID. Must be called before tk.Tk()
    creates the first window.
    """
    try:
        shell32 = C.WinDLL("shell32", use_last_error=True)
        shell32.SetCurrentProcessExplicitAppUserModelID(W.LPCWSTR("Chatterbox.App"))
    except (OSError, AttributeError):
        pass


def set_titlebar_theme(widget_hwnd, dark):
    """Best-effort DWM decoration hint; never required for app functionality."""
    try:
        dwm = C.WinDLL("dwmapi", use_last_error=True)
        setter = bind(dwm, "DwmSetWindowAttribute", C.c_long, W.HWND, W.DWORD, C.c_void_p, W.DWORD)
        value = W.BOOL(dark)
        hwnd = GetAncestor(widget_hwnd, 2)  # GA_ROOT: Tk's decorated top-level wrapper.
        return setter(hwnd, 20, C.byref(value), C.sizeof(value)) == 0
    except (OSError, AttributeError):
        return False


class GUIThreadInfo(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("flags", W.DWORD), ("hwndActive", W.HWND),
                ("hwndFocus", W.HWND), ("hwndCapture", W.HWND), ("hwndMenuOwner", W.HWND),
                ("hwndMoveSize", W.HWND), ("hwndCaret", W.HWND), ("rcCaret", W.RECT)]


GetGUIThreadInfo = bind(user32, "GetGUIThreadInfo", W.BOOL, W.DWORD, C.POINTER(GUIThreadInfo))


def focus_info(thread_id):
    info = GUIThreadInfo(cbSize=C.sizeof(GUIThreadInfo))
    if not GetGUIThreadInfo(thread_id, C.byref(info)):
        raise RuntimeError("Cannot determine the target input focus.")
    return info


def focused_window(thread_id):
    return focus_info(thread_id).hwndFocus or 0


@dataclass(frozen=True)
class WindowTarget:
    hwnd: int
    process_id: int
    thread_id: int
    focus_hwnd: int
    root_class: str = ""
    focus_class: str = ""
    field_process_id: int = 0
    field_thread_id: int = 0
    caret_hwnd: int = 0
    caret_rect: tuple | None = None
    guard_gui: bool = False

    @classmethod
    def capture(cls):
        hwnd = GetForegroundWindow()
        if not hwnd:
            raise RuntimeError("No foreground application is available.")
        pid = W.DWORD()
        tid = GetWindowThreadProcessId(hwnd, C.byref(pid))
        info = focus_info(tid)
        focus = info.hwndFocus or 0
        field_pid = W.DWORD()
        field_tid = GetWindowThreadProcessId(focus, C.byref(field_pid)) if focus else 0
        caret = (info.rcCaret.left, info.rcCaret.top, info.rcCaret.right, info.rcCaret.bottom) if info.hwndCaret else None
        return cls(hwnd, pid.value, tid, focus, window_class(hwnd), window_class(focus) if focus else "", field_pid.value, field_tid,
                   info.hwndCaret or 0, caret, True)

    def validate(self):
        pid = W.DWORD()
        tid = GetWindowThreadProcessId(self.hwnd, C.byref(pid))
        if not IsWindow(self.hwnd) or pid.value != self.process_id or tid != self.thread_id:
            raise RuntimeError("The original application was closed or replaced. Text was not pasted.")
        if self.focus_hwnd and not IsWindow(self.focus_hwnd):
            raise RuntimeError("The original input field was closed. Text was not pasted.")
        if self.root_class and window_class(self.hwnd) != self.root_class:
            raise RuntimeError("The original window identity changed. Text was not pasted.")
        if self.focus_class and window_class(self.focus_hwnd) != self.focus_class:
            raise RuntimeError("The original input identity changed. Text was not pasted.")
        if self.field_process_id:
            field_pid = W.DWORD()
            field_tid = GetWindowThreadProcessId(self.focus_hwnd, C.byref(field_pid))
            if field_pid.value != self.field_process_id or field_tid != self.field_thread_id:
                raise RuntimeError("The original input was replaced. Text was not pasted.")

    def restore(self):
        self.validate()
        if IsIconic(self.hwnd):
            ShowWindow(self.hwnd, 9)  # SW_RESTORE; no AttachThreadInput/Alt-key tricks.
        SetForegroundWindow(self.hwnd)
        deadline = time.monotonic() + 0.8
        while GetForegroundWindow() != self.hwnd and time.monotonic() < deadline:
            time.sleep(0.02)
        self.ensure_focused()

    def ensure_focused(self):
        self.validate()
        if GetForegroundWindow() != self.hwnd:
            raise RuntimeError("Windows refused focus restoration. Nothing was pasted; use the text preview.")
        if not self.focus_hwnd or focused_window(self.thread_id) != self.focus_hwnd:
            raise RuntimeError("The target input focus changed. Nothing was pasted; use the text preview.")
        if self.guard_gui:
            info = focus_info(self.thread_id)
            if info.flags & 0x1E:  # Move/size, menu, system menu, popup menu; ignore blink bit.
                raise RuntimeError("The original app is in a menu or move operation. Nothing was pasted; use Copy.")
            # Only the caret's owning control is checked, not its position: the
            # cursor moving within the same still-focused field (typing, scrolling
            # while dictation was processing) is expected, not a target change.
            if self.caret_hwnd and info.hwndCaret != self.caret_hwnd:
                raise RuntimeError("The original caret moved to a different control. Nothing was pasted; review and use Copy or Retry.")

    def still_focused(self):
        """Non-destructive staleness check: no foreground switch, no raising."""
        try:
            self.validate()
        except RuntimeError:
            return False
        return bool(self.focus_hwnd) and focused_window(self.thread_id) == self.focus_hwnd


_MODIFIERS = {"ctrl": 0x0002, "alt": 0x0001, "shift": 0x0004, "win": 0x0008}
_KEYS = {"space": 0x20, **{f"f{i}": 0x6F + i for i in range(1, 25)}}


def parse_hotkey(value):
    parts = [part.strip().lower() for part in value.split("+")]
    if len(parts) < 2 or len(set(parts)) != len(parts):
        raise ValueError("Use a modifier and one key, e.g. Ctrl+Alt+Space or Ctrl+I.")
    modifiers = 0
    for part in parts[:-1]:
        if part not in _MODIFIERS:
            raise ValueError("Supported modifiers: Ctrl, Alt, Shift, Win.")
        modifiers |= _MODIFIERS[part]
    key = parts[-1]
    vk = _KEYS.get(key)
    if vk is None and len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    if vk is None:
        raise ValueError("Use a letter, digit, Space, or F1-F24 as the shortcut key.")
    return modifiers | 0x4000, vk  # MOD_NOREPEAT


class GlobalHotkey:
    """A thread-owned RegisterHotKey message loop, with conflict reporting."""
    def __init__(self, shortcut, callback):
        self.modifiers, self.vk = parse_hotkey(shortcut)
        self.callback = callback
        self.ready = threading.Event()
        self.error = None
        self.thread_id = 0
        self.thread = threading.Thread(target=self._run, name="global-hotkey", daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(3):
            raise RuntimeError("Global hotkey registration timed out.")
        if self.error:
            raise RuntimeError(self.error)

    def _run(self):
        self.thread_id = GetCurrentThreadId()
        msg = W.MSG()
        PeekMessage(C.byref(msg), None, 0, 0, 0)  # Create message queue before ready.
        if not RegisterHotKey(None, 1, self.modifiers, self.vk):
            self.error = "Shortcut is already in use or reserved by Windows. Choose a different shortcut."
            self.ready.set()
            return
        self.ready.set()
        try:
            while GetMessage(C.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    try:
                        self.callback(WindowTarget.capture(), None)
                    except Exception as exc:
                        self.callback(None, str(exc))
        finally:
            UnregisterHotKey(None, 1)

    def close(self):
        if self.thread.is_alive():
            PostThreadMessage(self.thread_id, 0x0012, 0, 0)  # WM_QUIT
            self.thread.join(timeout=2)


class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD),
                ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ULONG_PTR)]


class INPUTUNION(C.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", W.DWORD), ("value", INPUTUNION)]


SendInput = bind(user32, "SendInput", W.UINT, W.UINT, C.POINTER(INPUT), C.c_int)


def wait_for_modifiers(cancel=None, timeout=3):
    deadline = time.monotonic() + timeout
    while any(GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
        if cancel and cancel.is_set():
            raise RuntimeError("Insertion cancelled.")
        if time.monotonic() >= deadline:
            raise RuntimeError("Release Ctrl/Alt/Shift/Windows before insertion. Text was not pasted.")
        time.sleep(0.02)


class KeyboardInjectionError(RuntimeError):
    def __init__(self, message, possibly_sent=False):
        super().__init__(message)
        self.possibly_sent = possibly_sent


def send_paste():
    events = (INPUT * 4)()
    for event, vk, flags in zip(events, (0x11, 0x56, 0x56, 0x11), (0, 0, 2, 2)):
        event.type = 1
        event.ki = KEYBDINPUT(wVk=vk, dwFlags=flags)
    accepted = SendInput(4, events, C.sizeof(INPUT))
    if accepted != 4:
        # Always attempt to release synthetic keys after a partially rejected input batch.
        releases = (INPUT * 2)()
        for event, vk in zip(releases, (0x56, 0x11)):
            event.type = 1
            event.ki = KEYBDINPUT(wVk=vk, dwFlags=2)
        SendInput(2, releases, C.sizeof(INPUT))
        raise KeyboardInjectionError("Windows blocked keyboard input, possibly to an elevated application.", possibly_sent=accepted >= 2)
