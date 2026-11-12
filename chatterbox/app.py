"""Desktop controller core: wiring, lifecycle, and event dispatch.

Recording/transcription state machine lives in recording_controller.py;
settings-page actions live in settings_controller.py; session/history
actions live in sessions.py. This module owns only what ties them together.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from collections import deque
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox

from .audio import MAX_SECONDS, Recorder
from .crash_log import log_crash
from .overlay import FloatingOverlay
from .pipeline import State
from .recording_controller import RecordingMixin
from .settings import Settings, Store
from .settings_controller import SettingsMixin
from .sessions import SessionsMixin
from .tray import SystemTray
from .ui import DesktopView
from .windows import GlobalHotkey, bind, kernel32, enable_system_dpi_awareness, set_taskbar_identity, GetAncestor, SetForegroundWindow

CreateMutex = bind(kernel32, "CreateMutexW", W.HANDLE, C.c_void_p, W.BOOL, W.LPCWSTR)
CloseHandle = bind(kernel32, "CloseHandle", W.BOOL, W.HANDLE)


class App(SettingsMixin, RecordingMixin, SessionsMixin):
    def __init__(self, root, store=None):
        self.root, self.store = root, store or Store()
        self.settings = self.store.load()
        self.state = State.IDLE
        self.events = queue.Queue()
        self.recorder = Recorder()
        self.hotkey = None
        self.worker = None
        self.cancel_event = threading.Event()
        self.target = None
        self.started = 0.0
        self.closing = False
        self.testing_api = False
        self.owner = None
        self.last_external_target = None
        self.last_external_seen = 0.0
        self.target_stale = False
        self._poll_job = None
        self._destroyed = False
        self.overlay = self.tray = None
        self.overlay_error = ""
        self.session_mode = self.settings.output_mode
        self.pending_raw = self.last_result = None
        self.recent_results = deque(maxlen=20)
        self.local_transcriber = None
        self.local_refiner = None
        self.download_worker = None
        self.download_cancel = threading.Event()
        self.stage_latencies = {}
        self._init_history()
        self.message = tk.StringVar(master=root)
        self._event_handlers = self._build_event_handlers()
        self._observe_target()
        self._build_ui()
        root.update_idletasks()
        self.owner = root.winfo_id()
        self._register(self.settings.hotkey)
        try:
            self.overlay = FloatingOverlay(root, self.recorder, lambda error: self.events.put(("capture_error", error)))
        except Exception as exc:
            self.overlay_error = str(exc)
        self.tray = SystemTray(lambda kind, value: self.events.put((kind, value)))
        self.tray.start()
        self._update_key_status()
        self._set_state(State.IDLE, "Click in another app's input field, then press the shortcut.")
        root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self._poll_job = root.after(50, self._poll)

    def _build_ui(self):
        self.view = DesktopView(self)
        for name in DesktopView.EXPORTED:
            setattr(self, name, getattr(self.view, name))
        self.view.workspace.refresh()
        self._update_key_status()

    def is_busy(self):
        return self.closing or self.testing_api or self.state not in (State.IDLE, State.ERROR) or bool(self.worker and self.worker.is_alive()) or bool(self.download_worker and self.download_worker.is_alive())

    def _on_window_close(self):
        if self.tray and self.tray.available:
            self.root.withdraw()  # Native overlay and global shortcut remain active.
        else:
            self.close()

    def _show_window(self, settings=False):
        self.root.deiconify()  # Explicit tray interaction, never an overlay side effect.
        SetForegroundWindow(GetAncestor(self.root.winfo_id(), 2))
        if settings and self.view.page != "settings":
            self.view.toggle_settings()

    def _set_state(self, state, message):
        self.state = state
        self.message.set(message)
        self.state_label.configure(text=state.value)
        self.root.title(f"Chatterbox — {state.value}")
        busy = self.is_busy()
        self._ui_busy = busy
        for widget in self.setting_widgets:
            widget.configure(state="disabled" if busy else ("readonly" if widget.winfo_class() == "TCombobox" else "normal"))
        self.view._update_cloud_field_state()
        self.view.state_changed(state, busy)
        if self.overlay:
            try:
                self.overlay.phase(state)
            except Exception as exc:
                self.overlay_error = str(exc)
                if state == State.RECORDING:
                    self.events.put(("capture_error", "Recording indicator error: " + str(exc)))
        if self.tray:
            self.tray.update(self.settings.output_mode, state.value, busy)

    def _register(self, shortcut):
        listener = GlobalHotkey(shortcut, lambda target, error: self.events.put(("hotkey", (target, error))))
        listener.start()
        self.hotkey = listener

    def _preview(self, text):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")
        self.view.preview_changed(text)

    def _build_event_handlers(self):
        def timing(value):
            self.stage_latencies[value["stage"]] = value["seconds"]
            self.latency_status.set(" · ".join(f"{stage.title()} {seconds:.2f}s" for stage, seconds in self.stage_latencies.items()))

        def local_progress(value):
            self.local_status.set(value)

        def model_event(kind):
            def handler(value):
                self.local_status.set("Model downloaded. Select Local Whisper and Apply settings." if kind == "model_ready" else value)
                self._set_state(State.IDLE if kind == "model_ready" or self.download_cancel.is_set() else State.ERROR,
                                "Model download complete." if kind == "model_ready" else "Model download cancelled; files were kept." if self.download_cancel.is_set() else value)
                self.view._refresh_model_status()
            return handler

        def fallback(value):
            if self.cancel_event.is_set() or self.closing:
                return
            self.pending_raw = value
            self._set_state(State.ERROR, "Polishing failed. Raw text is kept below. Choose Insert raw, or copy it manually. " + (value.refinement_error or ""))

        def phase(value):
            if self.cancel_event.is_set() or self.closing:
                return
            text = {State.TRANSCRIBING: "Transcribing recorded audio…", State.REFINING: "Correcting punctuation and removing meaningless fillers…", State.INSERTING: "Restoring the original input focus and inserting text…"}[value]
            if self.target_stale:
                text += " Target field changed; will not auto-insert when done — use the preview."
            self._set_state(value, text)

        def finished(kind):
            def handler(value):
                self.testing_api = False
                if kind == "done":
                    self.pending_raw = None
                    if self.history_error:
                        value += " " + self.history_error + ". Use Copy to preserve the preview."
                self._set_state(State.IDLE, value)
                if kind == "done" and self.overlay and not self.closing:
                    self.overlay.success()
            return handler

        def failed(value):
            self.testing_api = False
            self._set_state(State.ERROR, value)

        def tray_error(value):
            self._update_key_status()
            if self.root.state() == "withdrawn":
                self.root.iconify()  # Keep a taskbar recovery path without activating it.

        def file_transcribed(value):
            import os
            path, segments = value
            source_name = os.path.basename(path)
            message = f"File transcription complete: {len(segments)} segment(s)."
            try:
                self.transcripts.save(source_name, segments)
                self.view.workspace.refresh_file_transcripts()
            except Exception as exc:
                message += " Could not save to File Transcripts: " + str(exc)
            self._set_state(State.IDLE, message)
            self.view.show_file_transcript(source_name, segments)

        return {
            "hotkey": lambda value: self._toggle(*value),
            "preview": self._preview,
            "result": self._remember_result,
            "timing": timing,
            "local_progress": local_progress,
            "model_ready": model_event("model_ready"),
            "model_error": model_event("model_error"),
            "fallback": fallback,
            "capture_error": self._capture_failed,
            "tray_show": lambda value: self._show_window(),
            "tray_settings": lambda value: self._show_window(settings=True),
            "tray_mode": self._set_output_mode,
            "tray_exit": lambda value: self.close(),
            "tray_error": tray_error,
            "file_transcribed": file_transcribed,
            "phase": phase,
            "done": finished("done"),
            "cancelled": finished("cancelled"),
            "api_ok": finished("api_ok"),
            "error": failed,
            "api_error": failed,
        }

    def _cancel_poll(self):
        # Without this, a _poll tick already scheduled via root.after() can
        # still fire 50ms after root.destroy(), raising TclError against the
        # now-destroyed widget tree.
        if self._poll_job:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None

    def _finish_close(self):
        # close() can run synchronously from inside a _poll event dispatch
        # (e.g. the tray "Exit" event); guard so that _poll's own closing
        # branch, reached right after that same dispatch returns, doesn't
        # destroy an already-destroyed root.
        if self._destroyed:
            return
        self._destroyed = True
        self._cancel_poll()
        self.history.close()
        self.transcripts.close()
        self.root.destroy()

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                handler = self._event_handlers.get(kind)
                if handler:
                    handler(value)
        except queue.Empty:
            pass
        if self._destroyed:
            return
        if not self.closing:
            self._observe_target()
        if self.state in (State.TRANSCRIBING, State.REFINING) and self.target and not self.target_stale:
            if not self.target.still_focused():
                self.target_stale = True
                self.message.set(self.message.get() + " Target field changed; will not auto-insert when done — use the preview.")
        if self.state == State.RECORDING:
            capture = self.recorder.snapshot()
            self.state_label.configure(text=f"Recording · {int(capture.seconds) // 60}:{int(capture.seconds) % 60:02d}")
            if capture.limit_reached or time.monotonic() - self.started >= MAX_SECONDS:
                self._stop_recording()
            elif capture.error or not capture.active:
                self._capture_failed(capture.error or "Microphone stopped delivering captured audio.")
        if self.closing and not (self.worker and self.worker.is_alive()) and not (self.download_worker and self.download_worker.is_alive()):
            self._finish_close()
            return
        if self.is_busy() != self._ui_busy:
            self._ui_busy = self.is_busy()
            self.view.state_changed(self.state, self._ui_busy)
        self._poll_job = self.root.after(50, self._poll)

    def close(self):
        if self.closing:
            return
        self.closing = True
        if self.hotkey:
            self.hotkey.close()
        if self.tray:
            self.tray.close()
        self.download_cancel.set()
        self.cancel()
        try:
            self.recorder.cancel()  # Also retry retained resources after a failed start/stop.
        except Exception as exc:
            self.message.set("Microphone cleanup failed: " + str(exc))
        if self.overlay:
            self.overlay.close()
        if (self.worker and self.worker.is_alive()) or (self.download_worker and self.download_worker.is_alive()):
            self._set_state(State.CANCELLING, "Closing safely… waiting for the current request and clipboard cleanup. No new recording can start.")
        else:
            self._finish_close()


def _show_fatal(title, exc):
    # pythonw has no console to print to; a message box is the only way the
    # user sees this at all, so this must never itself raise.
    try:
        messagebox.showerror(title, str(exc))
    except Exception:
        pass
    log_crash(exc)


def run(smoke=False, background=False):
    mutex = CreateMutex(None, False, "Local\\Chatterbox-" + os.environ.get("USERNAME", "user"))
    if not mutex:
        raise RuntimeError("Cannot create application instance lock.")
    if C.get_last_error() == 183:
        CloseHandle(mutex)
        _show_fatal("Chatterbox", RuntimeError("Chatterbox is already running. Restore it from the system tray or taskbar."))
        return
    enable_system_dpi_awareness()
    set_taskbar_identity()
    root = tk.Tk()
    try:
        root.iconbitmap(default=str(Path(__file__).resolve().parent / "assets" / "icon.ico"))
    except Exception:
        pass
    root.report_callback_exception = lambda *exc_info: _show_fatal("Chatterbox error", exc_info[1])
    if background:
        root.withdraw()
    try:
        app = App(root)
        if background and not app.tray.available:
            root.iconify()
        if smoke:
            root.after(1000, app.close)
        root.mainloop()
    except Exception as exc:
        _show_fatal("Chatterbox startup error", exc)
        root.destroy()
        raise
    finally:
        CloseHandle(mutex)
