"""Opt-in interactive-desktop test. No microphone or network is used.

Temporarily focuses two scratch windows, sends a test hotkey, pastes into
one scratch input, and restores the prior clipboard and foreground window.
Do not interact with the keyboard/clipboard while this test is running.
"""
from array import array
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import sys
import threading
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chatterbox.api import OpenAIService, OpenRouterService
from chatterbox.audio import encode_recording
from chatterbox.clipboard import (Snapshot, allocate, opened, EmptyClipboard, SetClipboardData,
    RegisterClipboardFormat, GetClipboardData, GlobalLock, GlobalUnlock, GlobalSize, paste_text)
from chatterbox.pipeline import run_pipeline
from chatterbox.settings import Settings
from chatterbox.windows import (GetForegroundWindow, GlobalHotkey, INPUT, KEYBDINPUT,
    SendInput, SetForegroundWindow, WindowTarget, bind, user32)

GetAncestor = bind(user32, "GetAncestor", W.HWND, W.HWND, W.UINT)


class Response:
    def __init__(self, result):
        self.data = json.dumps(result).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, limit):
        return self.data[:limit]


class SyntheticOpener:
    def __init__(self):
        self.paths = []
    def open(self, req, timeout):
        self.paths.append(req.full_url)
        if req.full_url.endswith("/audio/transcriptions"):
            return Response({"text": "um hello world café"})
        if req.full_url.endswith("/chat/completions"):
            return Response({"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Hello, world. Café."}}]})
        return Response({"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Hello, world. Café."}]}]})


def raw_clipboard(fmt):
    handle = GetClipboardData(fmt)
    address = GlobalLock(handle)
    if not address:
        raise RuntimeError("Cannot read synthetic test clipboard.")
    try:
        return C.string_at(address, GlobalSize(handle))
    finally:
        GlobalUnlock(handle)


def main(provider="openai"):
    previous = GetForegroundWindow()
    root = tk.Tk()
    root.title("Voice Input native test — scratch destination")
    root.geometry("460x180+100+100")
    text = tk.Text(root)
    text.pack(fill="both", expand=True)
    other = tk.Toplevel(root)
    other.title("Voice Input native test — scratch non-target")
    other.geometry("460x120+600+100")
    other_input = tk.Text(other)
    other_input.pack(fill="both", expand=True)
    root.update()
    owner = root.winfo_id()
    top = GetAncestor(owner, 2)
    other_top = GetAncestor(other.winfo_id(), 2)
    original_clipboard = None
    hotkey = None
    worker = None
    results = {}
    try:
        with opened(owner):
            original_clipboard = Snapshot.capture()
            if not EmptyClipboard():
                raise RuntimeError("Cannot seed test clipboard.")
            html = RegisterClipboardFormat("HTML Format")
            sentinel = ("Voice Input test clipboard sentinel\0").encode("utf-16-le")
            html_value = b"<html><body>synthetic test HTML</body></html>\0"
            for fmt, data in ((13, sentinel), (html, html_value)):
                if not SetClipboardData(fmt, allocate(data)):
                    raise RuntimeError("Cannot seed test format.")
            expected_text = raw_clipboard(13)
            expected_html = raw_clipboard(html)
        SetForegroundWindow(top)
        text.focus_force()
        root.update()
        deadline = time.monotonic() + 2
        while GetForegroundWindow() != top and time.monotonic() < deadline:
            SetForegroundWindow(top)
            text.focus_force()
            root.update()
            time.sleep(0.02)
        target = WindowTarget.capture()
        if target.hwnd != top:
            raise RuntimeError("Desktop did not permit test foreground focus.")
        event = threading.Event()
        captured = []
        def callback(destination, error):
            captured.append((destination, error))
            event.set()
        hotkey = GlobalHotkey("Ctrl+Shift+F12", callback)
        hotkey.start()
        duplicate = GlobalHotkey("Ctrl+Shift+F12", callback)
        try:
            duplicate.start()
            raise AssertionError("Duplicate hotkey registration unexpectedly succeeded.")
        except RuntimeError:
            results["native_hotkey_conflict"] = "passed"
        finally:
            duplicate.close()
        events = (INPUT * 6)()
        for item, vk, flags in zip(events, (0x11, 0x10, 0x7B, 0x7B, 0x10, 0x11), (0, 0, 0, 2, 2, 2)):
            item.type = 1
            item.ki = KEYBDINPUT(wVk=vk, dwFlags=flags)
        if SendInput(6, events, C.sizeof(INPUT)) != 6:
            raise RuntimeError("Cannot send native test hotkey.")
        deadline = time.monotonic() + 2
        while not event.is_set() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        if not event.is_set() or captured[0][1] or captured[0][0].hwnd != top:
            raise AssertionError("Native hotkey callback/foreground capture failed.")
        results["native_hotkey_callback"] = "passed"
        hotkey.close()
        hotkey = None
        SetForegroundWindow(other_top)
        other_input.focus_force()
        root.update()
        deadline = time.monotonic() + 2
        while GetForegroundWindow() != other_top and time.monotonic() < deadline:
            SetForegroundWindow(other_top)
            other_input.focus_force()
            root.update()
            time.sleep(0.02)
        if GetForegroundWindow() != other_top:
            raise RuntimeError("Cannot focus scratch non-target window.")
        opener = SyntheticOpener()
        service_class = OpenRouterService if provider == "openrouter" else OpenAIService
        service = service_class("synthetic-not-real-key", Settings.for_provider(provider), opener)
        recording = encode_recording(array("h", [1000, -1000] * 8000).tobytes(), 16000)
        emitted = []
        cancel = threading.Event()
        worker = threading.Thread(target=run_pipeline, args=(recording, service, target,
            lambda value, destination, cancellation: paste_text(value, destination, owner, cancellation),
            lambda kind, value: emitted.append((kind, value)), cancel))
        worker.start()
        deadline = time.monotonic() + 8
        while worker.is_alive() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        worker.join(timeout=2)
        if worker.is_alive():
            cancel.set()
            raise RuntimeError("Native pipeline test timed out.")
        root.update()
        if not emitted or emitted[-1][0] != "done":
            raise AssertionError("Pipeline failed: " + repr(emitted[-1:]))
        if text.get("1.0", "end-1c") != "Hello, world. Café.":
            raise AssertionError("Native paste did not insert the expected Unicode text.")
        if other_input.get("1.0", "end-1c"):
            raise AssertionError("Text was pasted into the wrong scratch window.")
        results["native_focus_restore_and_unicode_paste"] = "passed"
        with opened(owner):
            if raw_clipboard(13) != expected_text or raw_clipboard(html) != expected_html:
                raise AssertionError("Text/HTML clipboard formats were not restored exactly.")
        results["native_text_and_html_clipboard_restore"] = "passed"
        if len(opener.paths) != 2:
            raise AssertionError("Both synthetic API stages did not execute.")
        results["synthetic_api_to_native_paste_pipeline"] = "passed"
        results["api_provider"] = provider
        results["microphone_recorded"] = False
        results["network_used"] = False
        print(json.dumps(results, indent=2))
    finally:
        if worker and worker.is_alive():
            worker.join(timeout=5)
        if hotkey:
            hotkey.close()
        if original_clipboard:
            with opened(owner):
                original_clipboard.restore_locked()
            original_clipboard.close()
        root.destroy()
        if previous:
            SetForegroundWindow(previous)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scratch-window integration smoke test; no microphone/network.")
    parser.add_argument("--provider", choices=("openai", "openrouter"), default="openai")
    main(parser.parse_args().provider)
