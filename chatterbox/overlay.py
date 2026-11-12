"""Native, non-activating recording indicator. Never opens an audio device."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import math
import os
import time

from .pipeline import State
from .ui import PALETTES
from .windows import bind, GetForegroundWindow

# Independent DLL wrappers avoid changing ctypes signatures used by other modules.
u32 = C.WinDLL("user32", use_last_error=True)
gdi = C.WinDLL("gdi32", use_last_error=True)
k32 = C.WinDLL("kernel32", use_last_error=True)
LRESULT = C.c_ssize_t
WNDPROC = C.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class WNDCLASS(C.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", C.c_int),
                ("cbWndExtra", C.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HANDLE),
                ("hCursor", W.HANDLE), ("hbrBackground", W.HANDLE),
                ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]


class PAINTSTRUCT(C.Structure):
    _fields_ = [("hdc", W.HDC), ("fErase", W.BOOL), ("rcPaint", W.RECT),
                ("fRestore", W.BOOL), ("fIncUpdate", W.BOOL), ("rgbReserved", W.BYTE * 32)]


class MONITORINFO(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]


RegisterClass = bind(u32, "RegisterClassW", W.WORD, C.POINTER(WNDCLASS))
UnregisterClass = bind(u32, "UnregisterClassW", W.BOOL, W.LPCWSTR, W.HINSTANCE)
CreateWindow = bind(u32, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                    C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p)
DefWindowProc = bind(u32, "DefWindowProcW", LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
DestroyWindow = bind(u32, "DestroyWindow", W.BOOL, W.HWND)
SetWindowPos = bind(u32, "SetWindowPos", W.BOOL, W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT)
ShowWindow = bind(u32, "ShowWindow", W.BOOL, W.HWND, C.c_int)
SetLayeredWindowAttributes = bind(u32, "SetLayeredWindowAttributes", W.BOOL, W.HWND, W.DWORD, W.BYTE, W.DWORD)
SetWindowRgn = bind(u32, "SetWindowRgn", C.c_int, W.HWND, W.HANDLE, W.BOOL)
InvalidateRect = bind(u32, "InvalidateRect", W.BOOL, W.HWND, C.c_void_p, W.BOOL)
BeginPaint = bind(u32, "BeginPaint", W.HDC, W.HWND, C.POINTER(PAINTSTRUCT))
EndPaint = bind(u32, "EndPaint", W.BOOL, W.HWND, C.POINTER(PAINTSTRUCT))
FillRect = bind(u32, "FillRect", C.c_int, W.HDC, C.POINTER(W.RECT), W.HANDLE)
DrawText = bind(u32, "DrawTextW", C.c_int, W.HDC, W.LPCWSTR, C.c_int, C.POINTER(W.RECT), W.UINT)
MonitorFromWindow = bind(u32, "MonitorFromWindow", W.HANDLE, W.HWND, W.DWORD)
GetMonitorInfo = bind(u32, "GetMonitorInfoW", W.BOOL, W.HANDLE, C.POINTER(MONITORINFO))
GetModuleHandle = bind(k32, "GetModuleHandleW", W.HMODULE, W.LPCWSTR)
SetThreadDpiAwarenessContext = bind(u32, "SetThreadDpiAwarenessContext", C.c_void_p, C.c_void_p)
GetDpiForWindow = bind(u32, "GetDpiForWindow", W.UINT, W.HWND)
CreateCompatibleDC = bind(gdi, "CreateCompatibleDC", W.HDC, W.HDC)
CreateCompatibleBitmap = bind(gdi, "CreateCompatibleBitmap", W.HBITMAP, W.HDC, C.c_int, C.c_int)
SelectObject = bind(gdi, "SelectObject", W.HANDLE, W.HDC, W.HANDLE)
DeleteObject = bind(gdi, "DeleteObject", W.BOOL, W.HANDLE)
DeleteDC = bind(gdi, "DeleteDC", W.BOOL, W.HDC)
CreateSolidBrush = bind(gdi, "CreateSolidBrush", W.HANDLE, W.DWORD)
CreatePen = bind(gdi, "CreatePen", W.HANDLE, C.c_int, C.c_int, W.DWORD)
RoundRect = bind(gdi, "RoundRect", W.BOOL, W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int)
CreateRoundRectRgn = bind(gdi, "CreateRoundRectRgn", W.HANDLE, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int)
Polyline = bind(gdi, "Polyline", W.BOOL, W.HDC, C.POINTER(W.POINT), C.c_int)
Arc = bind(gdi, "Arc", W.BOOL, W.HDC, *([C.c_int] * 8))
SetBkMode = bind(gdi, "SetBkMode", C.c_int, W.HDC, C.c_int)
SetTextColor = bind(gdi, "SetTextColor", W.DWORD, W.HDC, W.DWORD)
BitBlt = bind(gdi, "BitBlt", W.BOOL, W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, W.HDC, C.c_int, C.c_int, W.DWORD)
CreateFont = bind(gdi, "CreateFontW", W.HFONT, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int,
                  W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.LPCWSTR)
try:
    shcore = C.WinDLL("shcore", use_last_error=True)
    GetScaleFactorForMonitor = bind(shcore, "GetScaleFactorForMonitor", C.c_long, W.HANDLE, C.POINTER(C.c_int))
except (OSError, AttributeError):
    GetScaleFactorForMonitor = None

EX_STYLE = 0x08000000 | 0x00000080 | 0x00080000 | 0x00000020 | 0x00000008
# NOACTIVATE | TOOLWINDOW | LAYERED | TRANSPARENT | TOPMOST
FRAME_MS = 40  # 25 Hz; audio collection runs independently.


@contextmanager
def monitor_dpi_context():
    """Native overlay is per-monitor aware without changing Tk's default context."""
    previous = SetThreadDpiAwarenessContext(C.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    try:
        yield
    finally:
        if previous:
            SetThreadDpiAwarenessContext(previous)


def color(value):
    r, g, b = (int(value[i:i + 2], 16) for i in (1, 3, 5))
    return r | (g << 8) | (b << 16)


def overlay_bounds(work, dpi=96, position="bottom-center", margin=24):
    """Physical-pixel bounds within a monitor's work area; supports negative origins."""
    left, top, right, bottom = work
    scale = max(0.75, min(4.0, dpi / 96))
    pad = round(margin * scale)
    width = min(round(380 * scale), max(1, right - left - 2 * pad))
    height = min(round(58 * scale), max(1, bottom - top - 2 * pad))
    if position == "bottom-left":
        x = left + pad
    elif position == "bottom-right":
        x = right - pad - width
    else:
        x = left + (right - left - width) // 2
    y = top + pad if position == "top-center" else bottom - pad - height
    x = max(left, min(x, right - width))
    y = max(top, min(y, bottom - height))
    return x, y, width, height


class Waveform:
    """Short RMS history with time-based attack/release smoothing; no fake motion."""
    def __init__(self, bars=13):
        self.level = 0.0
        self.values = deque([0.0] * bars, maxlen=bars)

    def update(self, rms, dt=0.04):
        target = min(1.0, max(0.0, (float(rms) - 0.0015) * 14))
        tau = 0.055 if target > self.level else 0.11
        alpha = 1 - math.exp(-max(0.0, min(dt, 0.25)) / tau)
        self.level += (target - self.level) * alpha
        self.values.append(self.level)
        if target == 0 and self.level < 0.012:
            self.level = 0.0
            self.values = deque([0.0] * self.values.maxlen, maxlen=self.values.maxlen)
        return tuple(self.values)


class FloatingOverlay:
    """UI-thread-only native window; reads snapshots from the existing recorder."""
    def __init__(self, root, recorder, on_error):
        self.root, self.recorder, self.on_error = root, recorder, on_error
        self.hwnd = None
        self.visible = self.active = self.closed = False
        self.state = State.IDLE
        self.mode, self.theme, self.position, self.margin = "polished", "light", "bottom-center", 24
        self.elapsed = 0.0
        self.anchor = None
        self.geometry = None
        self.scale = 1.0
        self.wave = Waveform()
        self.deadline = 0.0
        self.last_tick = time.monotonic()
        self.error_reported = False
        self.timer = None
        self.instance = GetModuleHandle(None)
        self.class_name = f"ChatterboxOverlay-{os.getpid()}-{id(self)}"
        self.callback = WNDPROC(self._window_proc)  # Hold for the full HWND lifetime.
        window_class = WNDCLASS(lpfnWndProc=self.callback, hInstance=self.instance, lpszClassName=self.class_name)
        if not RegisterClass(C.byref(window_class)):
            raise RuntimeError("Windows could not register the recording indicator.")
        try:
            with monitor_dpi_context():
                self.hwnd = CreateWindow(EX_STYLE, self.class_name, "Chatterbox recording indicator", 0x80000000,
                                         0, 0, 380, 58, None, None, self.instance, None)
            if not self.hwnd or not SetLayeredWindowAttributes(self.hwnd, 0, 255, 2):
                raise RuntimeError("Windows could not create the recording indicator.")
            self.timer = root.after(FRAME_MS, self._tick)
        except Exception:
            self.close()
            raise

    def begin(self, target, mode, settings):
        self.anchor = target.hwnd
        self.mode, self.theme = mode, settings.theme
        self.position, self.margin = settings.overlay_position, settings.overlay_margin
        self.active, self.error_reported = True, False
        self.deadline, self.elapsed = 0.0, 0.0
        self.wave = Waveform()
        self.phase(State.RECORDING)

    def failure(self, target, mode, settings):
        self.anchor = target.hwnd if target else GetForegroundWindow()
        self.mode, self.theme = mode, settings.theme
        self.position, self.margin = settings.overlay_position, settings.overlay_margin
        self.active = True
        self.phase(State.ERROR)  # Never claim Recording if capture could not start.

    def resume_insertion(self, target, settings):
        self.anchor = target.hwnd
        self.mode, self.theme = "raw", settings.theme
        self.position, self.margin = settings.overlay_position, settings.overlay_margin
        self.active, self.error_reported = True, False
        self.deadline = 0.0
        self.phase(State.INSERTING)

    def phase(self, state):
        if self.closed or not self.active:
            return
        if state == State.IDLE:
            self.hide()
            return
        self.state = state
        if state == State.ERROR:
            self.deadline = time.monotonic() + 8
        self._position()
        if not self.visible:
            # Never deiconify a Tk window, set focus, or activate the overlay.
            ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
            self.visible = True
        InvalidateRect(self.hwnd, None, False)

    def success(self):
        if self.closed:
            return
        self.active = True
        self.phase(State.SUCCESS)
        self.deadline = time.monotonic() + 1.3

    def hide(self):
        if self.hwnd and self.visible:
            ShowWindow(self.hwnd, 0)
        self.visible, self.active = False, False
        self.deadline = 0.0

    def _position(self):
        with monitor_dpi_context():
            monitor = MonitorFromWindow(self.anchor or GetForegroundWindow(), 2)
            info = MONITORINFO(cbSize=C.sizeof(MONITORINFO))
            if not monitor or not GetMonitorInfo(monitor, C.byref(info)):
                raise RuntimeError("Cannot determine the recording monitor's work area.")
            dpi = GetDpiForWindow(self.anchor) if self.anchor else 96
            factor = C.c_int(100)
            if GetScaleFactorForMonitor and GetScaleFactorForMonitor(monitor, C.byref(factor)) == 0:
                dpi = round(96 * factor.value / 100)
            dpi = dpi or 96
            bounds = overlay_bounds((info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom),
                                    dpi, self.position, self.margin)
            geometry = (*bounds, dpi)
            if geometry != self.geometry:
                x, y, width, height = bounds
                if not SetWindowPos(self.hwnd, C.c_void_p(-1), x, y, width, height, 0x0010 | 0x0200):
                    raise RuntimeError("Windows could not position the recording indicator.")
                region = CreateRoundRectRgn(0, 0, width + 1, height + 1, height, height)
                if not region:
                    raise RuntimeError("Windows could not shape the recording indicator.")
                if not SetWindowRgn(self.hwnd, region, True):
                    DeleteObject(region)
                    raise RuntimeError("Windows could not shape the recording indicator.")
                # Region ownership transferred to Windows.
                self.geometry = geometry
                self.scale = min(dpi / 96, width / 380, height / 58)

    def _tick(self):
        self.timer = None
        if self.closed:
            return
        try:
            now = time.monotonic()
            if self.active:
                if self.deadline and now >= self.deadline:
                    self.hide()
                else:
                    if self.state == State.RECORDING:
                        status = self.recorder.snapshot()
                        if status.error or (not status.active and not status.limit_reached):
                            raise RuntimeError(status.error or "Microphone capture is no longer active.")
                        self.elapsed = status.seconds
                        self.wave.update(status.rms, now - self.last_tick)
                    self._position()
                    InvalidateRect(self.hwnd, None, False)
            self.last_tick = now
        except Exception as exc:
            if not self.error_reported:
                self.error_reported = True
                self.on_error(str(exc))
        self.timer = self.root.after(FRAME_MS, self._tick)

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == 0x0021:  # WM_MOUSEACTIVATE
            return 3  # MA_NOACTIVATE
        if message == 0x0084:  # WM_NCHITTEST; layered TRANSPARENT additionally passes across processes.
            return -1  # HTTRANSPARENT
        if message == 0x0014:  # WM_ERASEBKGND; paint double-buffered.
            return 1
        if message in (0x000F, 0x0317, 0x0318):  # PAINT / PRINT / PRINTCLIENT
            try:
                self._paint(hwnd, wparam if message != 0x000F else None)
            except Exception:
                if not self.error_reported:
                    self.error_reported = True
                    self.on_error("The recording indicator could not be drawn.")
            return 0
        return DefWindowProc(hwnd, message, wparam, lparam)

    def _paint(self, hwnd, print_dc=None):
        paint = None if print_dc else PAINTSTRUCT()
        dc = print_dc or BeginPaint(hwnd, C.byref(paint))
        memory = bitmap = old_bitmap = None
        try:
            if not dc or not self.geometry:
                return
            width, height = self.geometry[2:4]
            memory = CreateCompatibleDC(dc)
            bitmap = CreateCompatibleBitmap(dc, width, height)
            if not memory or not bitmap:
                raise RuntimeError("Cannot allocate indicator drawing surface.")
            old_bitmap = SelectObject(memory, bitmap)
            p = PALETTES[self.theme]
            brush = CreateSolidBrush(color(p["surface"]))
            try:
                FillRect(memory, C.byref(W.RECT(0, 0, width, height)), brush)
            finally:
                DeleteObject(brush)
            self._box(memory, 0, 0, width - 1, height - 1, height, p["surface"], p["border"], physical=True)
            accent = p["error"] if self.state in (State.RECORDING, State.ERROR) else p["ready"] if self.state == State.SUCCESS else p["processing"]
            self._glyph(memory, accent)
            self._text(memory, self.state.value, (50, 9, 196, 31), p["text"], 13, 600)
            subtitle = self.mode.title() + (" · see app" if self.state == State.ERROR else "")
            self._text(memory, subtitle, (50, 31, 198, 49), p["muted"], 10)
            if self.state == State.RECORDING:
                for index, amplitude in enumerate(self.wave.values):
                    bar_height = 2 + 26 * amplitude
                    self._box(memory, 205 + index * 5, 29 - bar_height / 2,
                              208 + index * 5, 29 + bar_height / 2, 3, p["text"], p["text"])
            elif self.state in (State.TRANSCRIBING, State.REFINING, State.INSERTING, State.CANCELLING):
                # Processing dots are separate from the microphone waveform.
                current = int(time.monotonic() * 4) % 3
                for index in range(3):
                    shade = accent if current == index else p["border"]
                    self._box(memory, 220 + index * 13, 26, 226 + index * 13, 32, 6, shade, shade)
            seconds = max(0, int(self.elapsed))
            self._text(memory, f"{seconds // 60}:{seconds % 60:02d}", (289, 15, 358, 44), p["muted"], 13, align=2)
            BitBlt(dc, 0, 0, width, height, memory, 0, 0, 0x00CC0020)
        finally:
            if old_bitmap:
                SelectObject(memory, old_bitmap)
            if bitmap:
                DeleteObject(bitmap)
            if memory:
                DeleteDC(memory)
            if paint is not None:
                EndPaint(hwnd, C.byref(paint))

    def _box(self, dc, x1, y1, x2, y2, radius, fill, outline, physical=False):
        factor = 1 if physical else self.scale
        values = [round(value * factor) for value in (x1, y1, x2, y2, radius)]
        brush = CreateSolidBrush(color(fill))
        pen = CreatePen(0, 1, color(outline))
        old_brush, old_pen = SelectObject(dc, brush), SelectObject(dc, pen)
        try:
            RoundRect(dc, *values[:4], values[4], values[4])
        finally:
            SelectObject(dc, old_brush)
            SelectObject(dc, old_pen)
            DeleteObject(brush)
            DeleteObject(pen)

    def _line(self, dc, points, shade):
        pen = CreatePen(0, max(1, round(1.8 * self.scale)), color(shade))
        old = SelectObject(dc, pen)
        try:
            values = (W.POINT * len(points))(*(W.POINT(round(x * self.scale), round(y * self.scale)) for x, y in points))
            Polyline(dc, values, len(points))
        finally:
            SelectObject(dc, old)
            DeleteObject(pen)

    def _glyph(self, dc, shade):
        if self.state == State.SUCCESS:
            self._line(dc, ((16, 29), (23, 36), (33, 23)), shade)
        elif self.state == State.ERROR:
            self._line(dc, ((24, 17), (24, 32)), shade)
            self._box(dc, 22.5, 37, 25.5, 40, 3, shade, shade)
        else:
            self._box(dc, 20, 15, 28, 31, 8, shade, shade)
            self._line(dc, ((16, 25), (16, 31), (18, 35), (24, 38), (30, 35), (32, 31), (32, 25)), shade)
            self._line(dc, ((24, 38), (24, 44)), shade)
            self._line(dc, ((20, 44), (28, 44)), shade)

    def _text(self, dc, text, bounds, shade, size, weight=400, align=0):
        font = CreateFont(-round(size * self.scale), 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
        old = SelectObject(dc, font)
        try:
            SetBkMode(dc, 1)
            SetTextColor(dc, color(shade))
            rect = W.RECT(*(round(v * self.scale) for v in bounds))
            DrawText(dc, text, -1, C.byref(rect), 0x20 | 0x04 | 0x8000 | align)  # single line, vertical center, ellipsis
        finally:
            SelectObject(dc, old)
            DeleteObject(font)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.timer:
            self.root.after_cancel(self.timer)
            self.timer = None
        if self.hwnd:
            DestroyWindow(self.hwnd)
            self.hwnd = None
        UnregisterClass(self.class_name, self.instance)
        self.visible = self.active = False
