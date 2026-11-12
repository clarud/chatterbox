"""Opt-in real desktop UI test; use scratch windows, fake microphone/API only.

Do not type or copy while running. A child process provides an external input;
the real controller remembers/restores it when its Record button is clicked.
"""
from array import array
import argparse
import ctypes as C
from ctypes import wintypes as W
from dataclasses import replace
import json
import multiprocessing as mp
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chatterbox.app import App
from chatterbox.audio import encode_recording, CaptureStatus
from chatterbox.clipboard import opened, GetClipboardData
from chatterbox.pipeline import State
from chatterbox.settings import Settings, Store
from chatterbox.windows import GetForegroundWindow, GetAncestor, SetForegroundWindow, WindowTarget, INPUT, SendInput, bind, user32
from chatterbox.overlay import monitor_dpi_context
from native_smoke import raw_clipboard

GetWindowRect = bind(user32, "GetWindowRect", W.BOOL, W.HWND, C.POINTER(W.RECT))
GetSystemMetrics = bind(user32, "GetSystemMetrics", C.c_int, C.c_int)
WindowFromPoint = bind(user32, "WindowFromPoint", W.HWND, W.POINT)
GetCursorPos = bind(user32, "GetCursorPos", W.BOOL, C.POINTER(W.POINT))
SetCursorPos = bind(user32, "SetCursorPos", W.BOOL, C.c_int, C.c_int)


def click_owned_window(hwnd):
    with monitor_dpi_context():
        rect = W.RECT()
        if not GetWindowRect(hwnd, C.byref(rect)):
            raise RuntimeError("Cannot locate the owned scratch window.")
        x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        if GetAncestor(WindowFromPoint(W.POINT(x, y)), 2) != hwnd:
            raise RuntimeError("The scratch click location is obscured; no click was sent.")
        left, top, width, height = (GetSystemMetrics(i) for i in (76, 77, 78, 79))
        events = (INPUT * 3)()
        events[0].mi.dx = round((x - left) * 65535 / max(1, width - 1))
        events[0].mi.dy = round((y - top) * 65535 / max(1, height - 1))
        events[0].mi.dwFlags = 0xC001  # MOVE | ABSOLUTE | VIRTUALDESK
        events[1].mi.dwFlags = 0x0002
        events[2].mi.dwFlags = 0x0004
        if SendInput(3, events, C.sizeof(INPUT)) != 3:
            release = INPUT()
            release.mi.dwFlags = 0x0004
            SendInput(1, C.byref(release), C.sizeof(INPUT))
            raise RuntimeError("Windows blocked the owned scratch-window click.")


def scratch(connection):
    root = tk.Tk()
    root.title("Voice Input UI test — external scratch input")
    root.geometry("520x260+80+90")
    root.attributes("-topmost", True)
    text = tk.Text(root)
    text.pack(fill="both", expand=True)
    root.update()
    hwnd = GetAncestor(root.winfo_id(), 2)
    connection.send(hwnd)
    def poll():
        if connection.poll():
            command = connection.recv()
            if command == "raise":
                root.lift()
                root.update()
                connection.send("raised")
            elif command == "focus":
                SetForegroundWindow(hwnd)
                text.focus_force()
                root.update()
                connection.send(("focused", WindowTarget.capture().focus_hwnd))
            elif isinstance(command, tuple) and command[0] == "controller":
                SetForegroundWindow(command[1])  # The foreground scratch process cooperates with the test.
            elif command == "read":
                connection.send(text.get("1.0", "end-1c"))
            elif command == "close":
                root.destroy()
                return
        root.after(10, poll)
    root.after(10, poll)
    root.mainloop()


def pump(root, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    root.update()
    if not predicate():
        raise RuntimeError("Desktop UI test condition timed out. Keep focus/keyboard/clipboard idle and retry.")


def click(widget):
    widget.invoke()  # Same controller callback; does not simulate an unrelated desktop pointer location.


def text_clipboard():
    return raw_clipboard(13) if GetClipboardData(13) else None


def press_test_hotkey():
    events = (INPUT * 6)()
    for event, key, flags in zip(events, (0x11, 0x10, 0x7B, 0x7B, 0x10, 0x11), (0, 0, 0, 2, 2, 2)):
        event.type = 1
        event.ki.wVk, event.ki.dwFlags = key, flags
    if SendInput(6, events, C.sizeof(INPUT)) != 6:
        raise RuntimeError("Windows blocked the registered test shortcut.")


def main(mode="polished", trigger="hotkey"):
    previous = GetForegroundWindow()
    previous_cursor = W.POINT()
    cursor_known = GetCursorPos(C.byref(previous_cursor))
    context = mp.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=scratch, args=(child,))
    process.start()
    app = root = None
    recorder = Mock()
    recorder.stop.return_value = encode_recording(array("h", [1000, -1000] * 8000).tobytes(), 16000)
    recorder.snapshot.return_value = CaptureStatus(True, 0.5, 0, time.monotonic())
    service = Mock()
    service.transcribe.return_value = "um hello from the record button"
    service.refine.return_value = "Hello from the record button."
    try:
        if not parent.poll(8):
            raise RuntimeError("Cannot create external scratch process.")
        hwnd = parent.recv()
        with tempfile.TemporaryDirectory() as folder, patch("chatterbox.app.Recorder", return_value=recorder), \
                patch("chatterbox.recording_controller.create_service", return_value=service), patch("chatterbox.app.SystemTray"):
            store = Store(folder)
            store.save(replace(Settings.for_provider("openrouter", "Ctrl+Shift+F12"), output_mode=mode))
            store.save_key("synthetic-test-not-a-provider-key", "openrouter")
            root = tk.Tk()
            app = App(root, store)
            root.geometry("640x660+650+90")
            root.attributes("-topmost", True)
            root.update()
            parent.send("raise")
            pump(root, parent.poll)
            if parent.recv() != "raised":
                raise AssertionError("Scratch window did not acknowledge being raised.")
            time.sleep(0.15)  # Let native positioning/composition settle before hit testing.
            root.update()
            click_owned_window(hwnd)
            parent.send("focus")
            pump(root, parent.poll)
            acknowledgment, field = parent.recv()
            if acknowledgment != "focused" or not field:
                raise AssertionError("Scratch input did not confirm native focus.")
            pump(root, lambda: GetForegroundWindow() == hwnd and WindowTarget.capture().focus_hwnd == field)
            app._observe_target()
            if not app.last_external_target or app.last_external_target.hwnd != hwnd:
                raise AssertionError("Controller did not remember the external input.")
            top = GetAncestor(root.winfo_id(), 2)
            if trigger == "button":
                root.lift()
                root.update()
                time.sleep(0.15)
                click_owned_window(top)
                pump(root, lambda: GetForegroundWindow() == top)
                root.focus_force()
            with opened(app.owner):
                before_clipboard = text_clipboard()  # Read-only comparison; never seed or replace it.
            if trigger == "button":
                click(app.record_button)
            else:
                WindowTarget.capture().ensure_focused()
                if GetForegroundWindow() != hwnd:
                    raise AssertionError("Scratch input is no longer active; no shortcut sent.")
                press_test_hotkey()
            pump(root, lambda: app.state in (State.RECORDING, State.ERROR))
            if app.state != State.RECORDING:
                raise AssertionError(app.message.get())
            pump(root, lambda: app.state == State.RECORDING)
            recorder.start.assert_called_once()
            if app.target.hwnd != hwnd or GetForegroundWindow() != hwnd:
                raise AssertionError("Record did not restore and lock the original external input.")
            if not app.overlay.visible:
                raise AssertionError("The native recording indicator was not shown.")
            def watch(seconds):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    root.update()
                    app.target.ensure_focused()
                    time.sleep(0.01)
                if app.state != State.RECORDING:
                    raise AssertionError(app.message.get())
            watch(0.65)
            if max(app.overlay.wave.values) > 0.01:
                raise AssertionError("Silence did not produce flat waveform bars.")
            recorder.snapshot.return_value = CaptureStatus(True, 1, recorder.stop.return_value.rms / 32768, time.monotonic())
            watch(0.65)
            if max(app.overlay.wave.values) < 0.15:
                raise AssertionError("Waveform did not respond to the synthetic PCM RMS.")
            recorder.snapshot.return_value = CaptureStatus(True, 1.6, 0, time.monotonic())
            watch(0.8)
            if max(app.overlay.wave.values) > 0.015:
                raise AssertionError("Waveform did not return to nearly flat during silence.")
            app._toggle_theme()
            if app.settings.theme != "dark":
                raise AssertionError("Theme did not change during recording.")
            if trigger == "button":
                root.lift()
                root.update()
                time.sleep(0.15)
                click_owned_window(top)
                pump(root, lambda: GetForegroundWindow() == top)
                root.focus_force()
                click(app.record_button)
            else:
                app.target.ensure_focused()
                press_test_hotkey()
            pump(root, lambda: app.state in (State.IDLE, State.ERROR) and not app.worker.is_alive(), timeout=8)
            if app.state != State.IDLE:
                raise AssertionError(app.message.get())
            parent.send("read")
            pump(root, parent.poll)
            expected = "um hello from the record button" if mode == "raw" else "Hello from the record button."
            if parent.recv() != expected:
                raise AssertionError("Synthetic dictation was not inserted into the external scratch input.")
            with opened(app.owner):
                if text_clipboard() != before_clipboard:
                    raise AssertionError("Clipboard text changed during the test; newer content was not overwritten.")
            service.transcribe.assert_called_once()
            if mode == "raw":
                service.refine.assert_not_called()
            else:
                service.refine.assert_called_once()
            pump(root, lambda: not app.overlay.visible, timeout=2)
            app.close()  # Release SQLite before TemporaryDirectory.__exit__ on Windows.
            print(json.dumps({"external_target_tracking": "passed", "trigger": trigger,
                              "record_button_restore_before_capture": "passed" if trigger == "button" else "not exercised",
                              "original_target_retained": "passed", "native_cross_process_paste": "passed",
                              "clipboard_restore": "passed", "theme_during_recording": "passed",
                              "mode": mode, "overlay_silence_speech_silence": "passed",
                              "overlay_focus_unchanged": "passed", "success_auto_hide": "passed",
                              "microphone_recorded": False, "network_used": False}, indent=2))
    finally:
        try:
            if app:
                app.close()
                if app.worker:
                    app.worker.join(3)
            elif root:
                root.destroy()
        finally:
            try:
                parent.send("close")
            except (EOFError, BrokenPipeError):
                pass
            process.join(3)
            if process.is_alive():
                process.terminate()
                process.join(2)
            parent.close()
            child.close()
            if cursor_known:
                SetCursorPos(previous_cursor.x, previous_cursor.y)
            if previous:
                SetForegroundWindow(previous)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("raw", "polished"), default="polished")
    parser.add_argument("--trigger", choices=("hotkey", "button"), default="hotkey")
    options = parser.parse_args()
    main(options.mode, options.trigger)
