"""Best-effort destination metadata, independent of native focus safety.

Only an executable basename is collected by default. Window caption collection is
explicitly opt-in; no input text, accessibility value, screenshot or memory is read.
"""
import ctypes as C
from ctypes import wintypes as W
from pathlib import PureWindowsPath

from .windows import bind

k32 = C.WinDLL("kernel32", use_last_error=True)
u32 = C.WinDLL("user32", use_last_error=True)
OpenProcess = bind(k32, "OpenProcess", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
CloseHandle = bind(k32, "CloseHandle", W.BOOL, W.HANDLE)
QueryFullProcessImageName = bind(k32, "QueryFullProcessImageNameW", W.BOOL, W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD))
GetWindowText = bind(u32, "GetWindowTextW", C.c_int, W.HWND, W.LPWSTR, C.c_int)


def collect_destination(target, enabled=True, collect_title=False):
    result = {"application": None, "window_title": None}
    if not enabled or not target:
        return result
    handle = OpenProcess(0x1000, False, target.process_id)  # QUERY_LIMITED_INFORMATION only.
    if handle:
        try:
            path = C.create_unicode_buffer(32768)
            size = W.DWORD(len(path))
            if QueryFullProcessImageName(handle, 0, path, C.byref(size)):
                result["application"] = PureWindowsPath(path.value).name or None
        finally:
            CloseHandle(handle)
    if collect_title:
        title = C.create_unicode_buffer(1024)
        if GetWindowText(target.hwnd, title, len(title)):
            result["window_title"] = title.value or None
    return result
