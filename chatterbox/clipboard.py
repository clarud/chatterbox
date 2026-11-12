"""Temporary clipboard replacement with supported-format snapshots and race checks."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import time
import uuid

from .windows import bind, kernel32, user32, send_paste, wait_for_modifiers

ole32 = C.WinDLL("ole32", use_last_error=True)
gdi32 = C.WinDLL("gdi32", use_last_error=True)
OpenClipboard = bind(user32, "OpenClipboard", W.BOOL, W.HWND)
CloseClipboard = bind(user32, "CloseClipboard", W.BOOL)
EmptyClipboard = bind(user32, "EmptyClipboard", W.BOOL)
EnumClipboardFormats = bind(user32, "EnumClipboardFormats", W.UINT, W.UINT)
GetClipboardData = bind(user32, "GetClipboardData", W.HANDLE, W.UINT)
SetClipboardData = bind(user32, "SetClipboardData", W.HANDLE, W.UINT, W.HANDLE)
GetClipboardSequenceNumber = bind(user32, "GetClipboardSequenceNumber", W.DWORD)
RegisterClipboardFormat = bind(user32, "RegisterClipboardFormatW", W.UINT, W.LPCWSTR)
GetClipboardFormatName = bind(user32, "GetClipboardFormatNameW", C.c_int, W.UINT, W.LPWSTR, C.c_int)
GlobalAlloc = bind(kernel32, "GlobalAlloc", W.HGLOBAL, W.UINT, C.c_size_t)
GlobalLock = bind(kernel32, "GlobalLock", C.c_void_p, W.HGLOBAL)
GlobalUnlock = bind(kernel32, "GlobalUnlock", W.BOOL, W.HGLOBAL)
GlobalSize = bind(kernel32, "GlobalSize", C.c_size_t, W.HGLOBAL)
GlobalFree = bind(kernel32, "GlobalFree", W.HGLOBAL, W.HGLOBAL)
OleDuplicateData = bind(ole32, "OleDuplicateData", W.HANDLE, W.HANDLE, W.WORD, W.UINT)
DeleteObject = bind(gdi32, "DeleteObject", W.BOOL, W.HANDLE)
CopyEnhMetaFile = bind(gdi32, "CopyEnhMetaFileW", W.HANDLE, W.HANDLE, W.LPCWSTR)
DeleteEnhMetaFile = bind(gdi32, "DeleteEnhMetaFile", W.BOOL, W.HANDLE)

_BITMAPS = {2, 9, 0x82}  # CF_BITMAP, CF_PALETTE, CF_DSPBITMAP
_ENH_METAFILES = {14, 0x8E}
_UNSUPPORTED = {3, 0x80, 0x83}  # METAFILEPICT, OWNERDISPLAY, DSPMETAFILEPICT
_OWNER_BOUND_NAMES = {"ole private data", "dataobject", "dataobjectattributes", "dataobjectattributesrequiringelevation"}
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


def format_name(fmt):
    if fmt < 0xC000:
        return ""
    name = C.create_unicode_buffer(256)
    GetClipboardFormatName(fmt, name, len(name))
    return name.value.lower()


@contextmanager
def opened(owner):
    deadline = time.monotonic() + 1.5
    while not OpenClipboard(owner):
        if time.monotonic() >= deadline:
            raise RuntimeError("Clipboard is busy. Nothing was pasted; try again.")
        time.sleep(0.02)
    try:
        yield
    finally:
        CloseClipboard()


def allocate(data):
    handle = GlobalAlloc(0x42, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
    if not handle:
        raise MemoryError("Cannot allocate clipboard memory.")
    address = GlobalLock(handle)
    if not address:
        GlobalFree(handle)
        raise RuntimeError("Cannot lock clipboard memory.")
    try:
        C.memmove(address, data, len(data))
    finally:
        GlobalUnlock(handle)
    return handle


def free_handle(fmt, handle):
    if fmt in _BITMAPS:
        DeleteObject(handle)
    elif fmt in _ENH_METAFILES:
        DeleteEnhMetaFile(handle)
    else:
        GlobalFree(handle)


class Snapshot:
    """Owns duplicate native handles until transferred back to the clipboard."""
    def __init__(self):
        self.entries = []

    @classmethod
    def capture(cls):
        # Caller holds OpenClipboard for the entire snapshot/replacement transaction.
        result = cls()
        total = 0
        fmt = 0
        try:
            while True:
                C.set_last_error(0)
                fmt = EnumClipboardFormats(fmt)
                if not fmt:
                    if C.get_last_error():
                        raise RuntimeError("Cannot enumerate the original clipboard safely.")
                    break
                if fmt in _UNSUPPORTED or 0x200 <= fmt <= 0x3FF or format_name(fmt) in _OWNER_BOUND_NAMES:
                    raise RuntimeError("Clipboard has an unsupported owner/private format. It was left unchanged; use the preview.")
                handle = GetClipboardData(fmt)
                if not handle:
                    raise RuntimeError("Cannot preserve one clipboard format. Clipboard was left unchanged.")
                if fmt not in _BITMAPS | _ENH_METAFILES:
                    size = GlobalSize(handle)
                    total += size
                    if size == 0 or total > MAX_SNAPSHOT_BYTES:
                        raise RuntimeError("Clipboard is too large or has an unsupported format. It was left unchanged.")
                duplicate = CopyEnhMetaFile(handle, None) if fmt in _ENH_METAFILES else OleDuplicateData(handle, fmt, 2)
                if not duplicate:
                    raise RuntimeError("Cannot duplicate the clipboard safely. It was left unchanged.")
                result.entries.append((fmt, duplicate))
            return result
        except Exception:
            result.close()
            raise

    def restore_locked(self):
        if not EmptyClipboard():
            raise RuntimeError("Cannot restore the original clipboard.")
        for index, (fmt, handle) in enumerate(self.entries):
            if handle and not SetClipboardData(fmt, handle):
                raise RuntimeError("Windows failed to restore a clipboard format. Check the clipboard before continuing.")
            self.entries[index] = (fmt, None)  # Ownership transferred to Windows.

    def close(self):
        for fmt, handle in self.entries:
            if handle:
                free_handle(fmt, handle)
        self.entries.clear()


_transaction_format = None


def transaction_format():
    global _transaction_format
    if _transaction_format is None:
        _transaction_format = RegisterClipboardFormat("Chatterbox.ClipboardTransaction")
        if not _transaction_format:
            raise RuntimeError("Cannot register clipboard transaction ownership.")
    return _transaction_format


def owns_transaction(token):
    """Caller holds OpenClipboard; read only our fixed-size private marker."""
    handle = GetClipboardData(transaction_format())
    if not handle or GlobalSize(handle) < len(token):
        return False
    address = GlobalLock(handle)
    if not address:
        return False
    try:
        return C.string_at(address, len(token)) == token
    finally:
        GlobalUnlock(handle)


class InsertionError(RuntimeError):
    def __init__(self, message, possibly_sent=False):
        super().__init__(message)
        self.possibly_sent = possibly_sent


def _install_text(text, token=None):
    # Allocate all handles before EmptyClipboard so allocation failure is non-destructive.
    entries = [(13, allocate((text + "\0").encode("utf-16-le")))]
    try:
        for name in ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"):
            fmt = RegisterClipboardFormat(name)
            if not fmt:
                raise RuntimeError("Cannot register clipboard privacy flags.")
            entries.append((fmt, allocate(b"\0" * 4)))
        if token is not None:
            entries.append((transaction_format(), allocate(token)))
        if not EmptyClipboard():
            raise RuntimeError("Cannot set temporary clipboard text.")
        for index, (fmt, handle) in enumerate(entries):
            if not SetClipboardData(fmt, handle):
                raise RuntimeError("Cannot set temporary clipboard text.")
            entries[index] = (fmt, None)
    finally:
        for fmt, handle in entries:
            if handle:
                free_handle(fmt, handle)


def paste_text(text, target, owner, cancel=None, restore_delay=0.8):
    """Guarded original-target paste; ownership token supplements sequence checks.

    InsertionError.possibly_sent means NEVER offer an automatic retry: Ctrl+V may
    already have been consumed even if later keyboard/clipboard cleanup failed.
    """
    if not text or "\0" in text:
        raise ValueError("Insertion requires nonempty text without null characters.")
    wait_for_modifiers(cancel)
    if cancel and cancel.is_set():
        raise RuntimeError("Insertion cancelled.")
    target.restore()
    snapshot = None
    sequence = None
    installed = False
    pasted = False
    warning = ""
    token = uuid.uuid4().bytes
    try:
        try:
            with opened(owner):
                target.ensure_focused()
                snapshot = Snapshot.capture()
                try:
                    _install_text(text, token)
                    installed = True
                except Exception:
                    snapshot.restore_locked()
                    raise
            # Sequence can commit at CloseClipboard. A concurrent update before
            # this read is detected by the unique marker, not mistaken for ours.
            sequence = GetClipboardSequenceNumber()
            wait_for_modifiers(cancel)
            if cancel and cancel.is_set():
                raise RuntimeError("Insertion cancelled.")
            target.ensure_focused()
            with opened(owner):
                if GetClipboardSequenceNumber() != sequence or not owns_transaction(token):
                    raise RuntimeError("Clipboard changed before insertion. Nothing was pasted; the newer clipboard was kept.")
            try:
                send_paste()
                pasted = True
            except Exception as exc:
                pasted = bool(getattr(exc, "possibly_sent", False))
                raise
        finally:
            if snapshot is not None:
                try:
                    if installed:
                        if pasted:
                            time.sleep(restore_delay)
                        with opened(owner):
                            same_sequence = sequence is None or GetClipboardSequenceNumber() == sequence
                            if same_sequence and owns_transaction(token):
                                snapshot.restore_locked()
                            elif pasted:
                                warning = "Paste shortcut sent. Clipboard changed during insertion; the newer clipboard was kept."
                finally:
                    snapshot.close()
    except Exception as exc:
        message = str(exc)
        if pasted:
            message += " Text may already have been inserted. Do not retry automatically; check the target and use Copy if needed."
        raise InsertionError(message, possibly_sent=pasted) from exc
    return warning
