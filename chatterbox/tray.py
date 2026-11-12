"""Optional Windows notification-area icon, with no Tk or third-party dependency.

SystemTray(emit).start() returns availability (and never raises a native failure).
Call update(mode, state, busy) from the controller and close() during shutdown.
The emit callback must be nonblocking/thread-safe, normally queue.put: callbacks
are tray_show/settings/exit with None, tray_mode with raw/polished, or tray_error
with a fixed, non-sensitive status. The controller owns all UI and mode changes.

Importing this module does not load DLLs or create a window. The hidden top-level
window (NOT a message-only window) receives TaskbarCreated broadcasts. All native
resource creation/destruction and menus run on its dedicated message thread.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import threading

ICON_PATH = Path(__file__).resolve().parent / "assets" / "icon.ico"
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x0010, 0x0040

WM_NULL = 0x0000
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_USER = 0x0400
WM_APP = 0x8000
WM_TRAY = WM_APP + 41
WM_UPDATE = WM_APP + 42
WM_STOP = WM_APP + 43
NIN_SELECT = WM_USER
NIN_KEYSELECT = WM_USER + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 0x01, 0x02, 0x04, 0x80
NOTIFYICON_VERSION_4 = 4
ICON_ID = 1
CMD_SHOW, CMD_SETTINGS, CMD_RAW, CMD_POLISHED, CMD_EXIT = 101, 102, 103, 104, 105
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0, 1, 0x0800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100
WS_EX_TOOLWINDOW = 0x00000080

LRESULT = C.c_ssize_t
WNDPROC = getattr(C, "WINFUNCTYPE", C.CFUNCTYPE)(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class _GUID(C.Structure):
    _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD),
                ("Data4", W.BYTE * 8)]


class _Version(C.Union):
    _fields_ = [("uTimeout", W.UINT), ("uVersion", W.UINT)]


class _NOTIFYICONDATAW(C.Structure):
    _anonymous_ = ("version",)
    _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT),
                ("uFlags", W.UINT), ("uCallbackMessage", W.UINT), ("hIcon", W.HICON),
                ("szTip", W.WCHAR * 128), ("dwState", W.DWORD), ("dwStateMask", W.DWORD),
                ("szInfo", W.WCHAR * 256), ("version", _Version),
                ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD),
                ("guidItem", _GUID), ("hBalloonIcon", W.HICON)]


class _WNDCLASSW(C.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", C.c_int),
                ("cbWndExtra", C.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]


class _Win32:
    """Lazy, explicitly pointer-sized ABI surface; replaceable in unit tests."""
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows only")
        self.user32 = C.WinDLL("user32", use_last_error=True)
        self.kernel32 = C.WinDLL("kernel32", use_last_error=True)
        self.shell32 = C.WinDLL("shell32", use_last_error=True)

        def bind(dll, name, result, *args):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = result, list(args)
            setattr(self, name, fn)

        u, k, s = self.user32, self.kernel32, self.shell32
        bind(k, "GetModuleHandleW", W.HMODULE, W.LPCWSTR)
        bind(k, "GetCurrentThreadId", W.DWORD)
        bind(u, "RegisterClassW", W.ATOM, C.POINTER(_WNDCLASSW))
        bind(u, "UnregisterClassW", W.BOOL, W.LPCWSTR, W.HINSTANCE)
        bind(u, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
             C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p)
        bind(u, "DefWindowProcW", LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        bind(u, "DestroyWindow", W.BOOL, W.HWND)
        bind(u, "RegisterWindowMessageW", W.UINT, W.LPCWSTR)
        bind(u, "LoadIconW", W.HICON, W.HINSTANCE, W.LPCWSTR)
        bind(u, "LoadImageW", W.HICON, W.HINSTANCE, W.LPCWSTR, W.UINT, C.c_int, C.c_int, W.UINT)
        bind(u, "DestroyIcon", W.BOOL, W.HICON)
        bind(u, "GetMessageW", C.c_int, C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
        bind(u, "PeekMessageW", W.BOOL, C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT)
        bind(u, "TranslateMessage", W.BOOL, C.POINTER(W.MSG))
        bind(u, "DispatchMessageW", LRESULT, C.POINTER(W.MSG))
        bind(u, "PostMessageW", W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        bind(u, "PostThreadMessageW", W.BOOL, W.DWORD, W.UINT, W.WPARAM, W.LPARAM)
        bind(u, "PostQuitMessage", None, C.c_int)
        bind(u, "EndMenu", W.BOOL)
        bind(u, "CreatePopupMenu", W.HMENU)
        bind(u, "AppendMenuW", W.BOOL, W.HMENU, W.UINT, C.c_size_t, W.LPCWSTR)
        bind(u, "CheckMenuRadioItem", W.BOOL, W.HMENU, W.UINT, W.UINT, W.UINT, W.UINT)
        bind(u, "DestroyMenu", W.BOOL, W.HMENU)
        bind(u, "GetCursorPos", W.BOOL, C.POINTER(W.POINT))
        bind(u, "SetForegroundWindow", W.BOOL, W.HWND)
        bind(u, "TrackPopupMenuEx", W.UINT, W.HMENU, W.UINT, C.c_int, C.c_int,
             W.HWND, C.c_void_p)
        bind(s, "Shell_NotifyIconW", W.BOOL, W.DWORD, C.POINTER(_NOTIFYICONDATAW))


_STATE_LABELS = {label.casefold(): label for label in (
    "Ready", "Recording", "Transcribing", "Refining", "Polishing", "Inserting",
    "Cancelling", "Cancelled", "Error", "Processing", "Closing", "Starting",
    "Checking API", "Listening")}
_STATE_LABELS["idle"] = "Ready"
_UNAVAILABLE = "Windows notification-area icon is unavailable; use the main window."
_RUNTIME_ERROR = "Windows notification-area icon stopped working; use the main window."
_CLEANUP_ERROR = "Windows tray cleanup was incomplete; use the main window."


class SystemTray:
    """An optional one-shot tray lifecycle. close() is safe before/after start().

    available is true only after icon registration and false on failure/close.
    error is an empty string on success or a safe status on failure. Updates are
    asynchronous, coalesced and accepted before start; only fixed known status
    labels are stored/displayed. Unknown state text becomes Ready/Processing.
    """
    def __init__(self, emit):
        self.emit = emit
        self.available = False
        self.error = ""
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._native = None
        self._thread_id = 0
        self._hwnd = None
        self._instance = None
        self._class_name = f"ChatterboxTray_{os.getpid()}_{id(self):x}"
        self._registered = False
        self._wndproc = None  # Keep the ctypes callback alive until class cleanup.
        self._icon = None  # LoadIconW returns a shared stock icon, never DestroyIcon.
        self._icon_owned = False  # True only for our own LoadImageW-loaded icon.
        self._icon_added = False
        self._version4 = False
        self._taskbar_created = 0
        self._mode, self._state, self._busy = "polished", "Ready", False

    def start(self):
        """Start once, waiting at most three seconds. Native failure is nonfatal."""
        with self._lock:
            if self._stop.is_set():
                return False
            if self._thread is None:
                try:
                    self._thread = threading.Thread(target=self._run, name="system-tray", daemon=True)
                    self._thread.start()
                except Exception:
                    self._stop.set()
                    self._fail(_UNAVAILABLE)
                    self._ready.set()
        if not self._ready.wait(3):
            self._stop.set()
            self._fail("Windows notification-area startup timed out; use the main window.")
            self._request_stop()
        return self.available

    def update(self, mode: str, state: str, busy: bool = False):
        """Only mode and allowlisted status reach Shell; never arbitrary content."""
        mode = "raw" if isinstance(mode, str) and mode.strip().casefold() == "raw" else "polished"
        key = state.strip().casefold() if isinstance(state, str) else ""
        label = _STATE_LABELS.get(key, "Processing" if busy else "Ready")
        with self._lock:
            if self._stop.is_set():
                return
            self._mode, self._state, self._busy = mode, label, bool(busy)
            native, hwnd, available = self._native, self._hwnd, self.available
        if available and hwnd:
            try:
                if not native.PostMessageW(hwnd, WM_UPDATE, 0, 0):
                    raise OSError("PostMessage failed")
            except Exception:
                self._stop.set()
                self._fail(_RUNTIME_ERROR)
                self._request_stop()

    def close(self):
        """Request owner-thread cleanup; idempotent, including during startup."""
        with self._lock:
            self._stop.set()
            self.available = False
            thread = self._thread
        self._request_stop()
        if thread and thread.ident is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
            if thread.is_alive():
                self._fail(_CLEANUP_ERROR)

    def _emit(self, kind, value=None):
        try:
            self.emit(kind, value)
        except Exception:
            # A shutting-down controller must not crash a ctypes callback.
            pass

    def _fail(self, message):
        with self._lock:
            changed = self.error != message
            self.available = False
            self.error = message
        if changed:
            self._emit("tray_error", message)

    def _request_stop(self):
        with self._lock:
            native, hwnd, tid = self._native, self._hwnd, self._thread_id
        if native:
            try:
                if hwnd and native.PostMessageW(hwnd, WM_STOP, 0, 0):
                    return
            except Exception:
                pass
            if tid:
                try:
                    native.PostThreadMessageW(tid, WM_QUIT, 0, 0)
                except Exception:
                    pass

    def _tooltip(self):
        with self._lock:
            mode, state, busy = self._mode, self._state, self._busy
        return f"Chatterbox | {mode.title()} | {state}" + (" (busy)" if busy else "")

    def _data(self, flags=0):
        data = _NOTIFYICONDATAW()
        data.cbSize = C.sizeof(data)
        data.hWnd, data.uID, data.uFlags = self._hwnd, ICON_ID, flags
        data.uCallbackMessage, data.hIcon = WM_TRAY, self._icon
        data.szTip = self._tooltip()[:127]
        return data

    def _add_icon(self):
        data = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        if not self._native.Shell_NotifyIconW(NIM_ADD, C.byref(data)):
            raise OSError("Shell registration failed")
        self._icon_added = True
        data.uVersion = NOTIFYICON_VERSION_4
        # Shells rejecting v4 keep the original callback protocol.
        self._version4 = bool(self._native.Shell_NotifyIconW(NIM_SETVERSION, C.byref(data)))
        with self._lock:
            self.available = not self._stop.is_set()
            changed = self.available and data.szTip != self._tooltip()
        # An update racing initialization saw available=False and posted nothing.
        # Refresh that snapshot; later updates see available=True and post normally.
        if changed:
            fresh = self._data(NIF_TIP | NIF_SHOWTIP)
            if not self._native.Shell_NotifyIconW(NIM_MODIFY, C.byref(fresh)):
                raise OSError("Initial tooltip synchronization failed")

    def _run(self):
        try:
            self._native = native = _Win32()
            self._thread_id = native.GetCurrentThreadId()
            msg = W.MSG()
            native.PeekMessageW(C.byref(msg), None, 0, 0, 0)  # Ensure thread queue exists.
            if self._stop.is_set():
                return
            self._instance = native.GetModuleHandleW(None)
            if not self._instance:
                raise OSError("Module handle unavailable")
            self._taskbar_created = native.RegisterWindowMessageW("TaskbarCreated")
            if not self._taskbar_created:
                raise OSError("TaskbarCreated registration failed")
            self._wndproc = WNDPROC(self._window_proc)
            wc = _WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=self._instance,
                            lpszClassName=self._class_name)
            if not native.RegisterClassW(C.byref(wc)):
                raise OSError("Window class registration failed")
            self._registered = True
            # A hidden top-level tool window receives shell broadcasts, unlike HWND_MESSAGE.
            self._hwnd = native.CreateWindowExW(WS_EX_TOOLWINDOW, self._class_name,
                                                "Chatterbox Tray", 0, 0, 0, 0, 0,
                                                None, None, self._instance, None)
            if not self._hwnd:
                raise OSError("Hidden window creation failed")
                self._icon = None
            if ICON_PATH.is_file():
                self._icon = native.LoadImageW(None, str(ICON_PATH), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
                self._icon_owned = bool(self._icon)
            if not self._icon:
                self._icon = native.LoadIconW(None, C.cast(C.c_void_p(32512), W.LPCWSTR))  # IDI_APPLICATION fallback
                self._icon_owned = False
            if not self._icon:
                raise OSError("Stock icon unavailable")
            if self._stop.is_set():
                return
            self._add_icon()
            self._ready.set()
            while not self._stop.is_set():
                result = native.GetMessageW(C.byref(msg), None, 0, 0)
                if result == -1:
                    raise OSError("Message loop failed")
                if result == 0 or self._stop.is_set():
                    break
                native.TranslateMessage(C.byref(msg))
                native.DispatchMessageW(C.byref(msg))
            if not self._stop.is_set():
                self._fail(_RUNTIME_ERROR)
        except Exception:
            self._fail(_RUNTIME_ERROR if self._ready.is_set() else _UNAVAILABLE)
        finally:
            self._stop.set()
            with self._lock:
                self.available = False
            self._cleanup()
            self._ready.set()

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            return self._dispatch(hwnd, message, wparam, lparam)
        except Exception:
            self._stop.set()
            self._fail(_RUNTIME_ERROR)
            try:
                self._native.EndMenu()
                self._native.PostQuitMessage(0)
            except Exception:
                pass
            return 0

    def _dispatch(self, hwnd, message, wparam, lparam):
        if message in (WM_STOP, WM_CLOSE):
            self._stop.set()
            self._native.EndMenu()  # Unblock a context menu's nested native message loop.
            self._native.PostQuitMessage(0)
            return 0
        if message == WM_DESTROY:
            self._native.PostQuitMessage(0)
            return 0
        if self._stop.is_set():
            return self._native.DefWindowProcW(hwnd, message, wparam, lparam)
        if self._taskbar_created and message == self._taskbar_created:
            # Explorer has already discarded the previous icon and its version setting.
            with self._lock:
                self.available = False
            self._icon_added = False
            self._version4 = False
            self._add_icon()
            return 0
        if message == WM_UPDATE:
            if self._icon_added:
                data = self._data(NIF_TIP | NIF_SHOWTIP)
                if not self._native.Shell_NotifyIconW(NIM_MODIFY, C.byref(data)):
                    raise OSError("Tooltip update failed")
            return 0
        if message == WM_TRAY:
            if self._version4:
                if (int(lparam) >> 16) & 0xFFFF != ICON_ID:
                    return 0
                event = int(lparam) & 0xFFFF
                if event in (NIN_SELECT, NIN_KEYSELECT):
                    self._emit("tray_show")
                elif event == WM_CONTEXTMENU:
                    self._show_menu(self._signed_word(wparam), self._signed_word(int(wparam) >> 16))
            else:
                if int(wparam) != ICON_ID:
                    return 0
                if lparam == WM_LBUTTONUP:
                    self._emit("tray_show")
                elif lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
            return 0
        return self._native.DefWindowProcW(hwnd, message, wparam, lparam)

    @staticmethod
    def _signed_word(value):
        return C.c_short(int(value) & 0xFFFF).value

    def _show_menu(self, x=None, y=None):
        native = self._native
        if x is None or y is None or (x == -1 and y == -1):
            point = W.POINT()
            if not native.GetCursorPos(C.byref(point)):
                raise OSError("Cursor position unavailable")
            x, y = point.x, point.y
        menu = native.CreatePopupMenu()
        if not menu:
            raise OSError("Menu creation failed")
        try:
            with self._lock:
                mode, busy = self._mode, self._busy
            items = ((CMD_SHOW, "Show", MF_STRING), (CMD_SETTINGS, "Settings", MF_STRING),
                     (0, None, MF_SEPARATOR),
                     (CMD_RAW, "Raw", MF_GRAYED if busy else MF_STRING),
                     (CMD_POLISHED, "Polished", MF_GRAYED if busy else MF_STRING),
                     (0, None, MF_SEPARATOR), (CMD_EXIT, "Exit", MF_STRING))
            for command, label, flags in items:
                if not native.AppendMenuW(menu, flags, command, label):
                    raise OSError("Menu item creation failed")
            selected = CMD_RAW if mode == "raw" else CMD_POLISHED
            if not native.CheckMenuRadioItem(menu, CMD_RAW, CMD_POLISHED, selected, 0):
                raise OSError("Menu radio selection failed")
            native.SetForegroundWindow(self._hwnd)
            command = native.TrackPopupMenuEx(menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
                                              x, y, self._hwnd, None)
        finally:
            destroyed = native.DestroyMenu(menu)
            # Microsoft's menu dismissal workaround after SetForegroundWindow.
            native.PostMessageW(self._hwnd, WM_NULL, 0, 0)
            if not destroyed:
                raise OSError("Menu cleanup failed")
        self._select(command)

    def _select(self, command):
        with self._lock:
            if self._stop.is_set():
                return
            if command in (CMD_RAW, CMD_POLISHED):
                # Recheck: controller updates may arrive while TrackPopupMenu is running.
                if not self._busy:
                    self._emit("tray_mode", "raw" if command == CMD_RAW else "polished")
                return
        event = {CMD_SHOW: "tray_show", CMD_SETTINGS: "tray_settings", CMD_EXIT: "tray_exit"}.get(command)
        if event:
            self._emit(event)

    def _cleanup(self):
        native = self._native
        if native is None:
            return
        failed = False
        if self._icon_added:
            try:
                data = self._data()
                failed = not native.Shell_NotifyIconW(NIM_DELETE, C.byref(data)) or failed
            except Exception:
                failed = True
            self._icon_added = False
        if self._hwnd:
            try:
                failed = not native.DestroyWindow(self._hwnd) or failed
            except Exception:
                failed = True
            self._hwnd = None
        if self._registered:
            try:
                failed = not native.UnregisterClassW(self._class_name, self._instance) or failed
            except Exception:
                failed = True
            self._registered = False
        if self._icon and self._icon_owned:
            try:
                native.DestroyIcon(self._icon)
            except Exception:
                pass
        self._icon = None  # Stock fallback is a shared handle: Windows, not this instance, owns it.
        self._icon_owned = False
        self._thread_id = 0
        if failed:
            self._fail(_CLEANUP_ERROR)
