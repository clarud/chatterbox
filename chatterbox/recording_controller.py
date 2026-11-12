"""Record -> transcribe -> insert state machine; UI/settings/session glue lives elsewhere."""
from __future__ import annotations

from dataclasses import replace
import os
import threading
import time
from tkinter import filedialog

from .api import create_service
from .clipboard import paste_text
from .pipeline import State, run_pipeline
from .settings import PROVIDER_NAMES
from .windows import WindowTarget


class RecordingMixin:
    def _get_local(self):
        if self.local_transcriber is None or self.local_transcriber.model_name != self.settings.local_model:
            if self.local_transcriber:
                self.local_transcriber.release()
            from .local_stt import LocalTranscriber
            self.local_transcriber = LocalTranscriber(self.settings.local_model, self.store.directory / "models",
                lambda text: self.events.put(("local_progress", text)))
        return self.local_transcriber

    def _get_local_refiner(self):
        if self.local_refiner is None:
            from .local_llm import LocalRefiner
            self.local_refiner = LocalRefiner(self.store.directory / "models",
                lambda text: self.events.put(("local_progress", text)))
        return self.local_refiner

    def _create_service(self):
        if self.settings.transcription_backend != "local":
            return create_service(self.store, self.settings)
        local_refiner = self._get_local_refiner() if self.settings.refinement_backend == "local_llm" else None
        return create_service(self.store, self.settings, local=self._get_local(), local_refiner=local_refiner)

    def _capture_preflight(self):
        refine_choice = self.refine_backend_var.get()
        refine_backend_selected = "local" if refine_choice == "Local (rule-based)" else "local_llm" if refine_choice == "Local (LLM)" else "cloud"
        mismatches = []
        if self.provider_var.get().lower() != self.settings.provider:
            mismatches.append(f"Cloud provider (selected {self.provider_var.get()}, applied {PROVIDER_NAMES[self.settings.provider]})")
        if self.backend_var.get() != ("Cloud API" if self.settings.transcription_backend == "cloud" else "Local Whisper"):
            applied = "Local Whisper" if self.settings.transcription_backend == "local" else "Cloud API"
            mismatches.append(f"Transcription source (selected {self.backend_var.get()}, applied {applied})")
        if self.local_model_var.get() != self.settings.local_model:
            mismatches.append(f"Local CPU model (selected {self.local_model_var.get()}, applied {self.settings.local_model})")
        if refine_backend_selected != self.settings.refinement_backend:
            applied = {"cloud": "Cloud", "local": "Local (rule-based)", "local_llm": "Local (LLM)"}[self.settings.refinement_backend]
            mismatches.append(f"Refinement method (selected {refine_choice}, applied {applied})")
        if mismatches:
            raise RuntimeError("Apply settings before recording — unapplied changes: " + "; ".join(mismatches) + ".")
        if self.settings.refinement_backend != "cloud" and self.settings.transcription_backend != "local":
            raise RuntimeError("Local refinement requires Local Whisper transcription. Set the transcription source to Local Whisper, or switch refinement back to Cloud.")
        if self.settings.transcription_backend == "local":
            self._get_local().check_available()  # Explicit action, no download/model loading.
            if self.settings.refinement_backend == "local_llm":
                self._get_local_refiner().check_available()
        else:
            self.store.get_key(self.settings.provider)

    def _retry_insertion(self):
        result = self.last_result
        if not result or not result.can_retry or not result.insertion_error or not self.view.workspace.recovery_visible():
            return
        self._insert_result(result, result.polished if result.polished is not None else result.raw,
                            mode="polished" if result.polished is not None else "raw")

    def _remember_result(self, result):
        self.last_result = result
        if self.pending_raw and self.pending_raw.session_id == result.session_id:
            self.pending_raw = result if result.can_retry else None
        self._persist_result(result)
        for index, previous in enumerate(self.recent_results):
            if previous.session_id == result.session_id:
                self.recent_results[index] = result
                return
        self.recent_results.append(result)

    def _insert_raw(self):
        result = self.pending_raw
        if not result or not result.can_retry or not self.view.workspace.recovery_visible():
            return
        self._insert_result(result, result.raw, mode="raw")

    def _insert_result(self, result, text, mode="raw"):
        if self.is_busy() or not self.target or not result.can_retry:
            return
        self.cancel_event = threading.Event()
        target, owner, cancel = self.target, self.owner, self.cancel_event
        try:
            if self.overlay:
                self.overlay.resume_insertion(target, self.settings)
                self.overlay.mode = mode
        except Exception as exc:
            self._set_state(State.ERROR, "Could not start raw insertion: " + str(exc))
            return
        self._set_state(State.INSERTING, "Retrying the original input with preserved text…")
        def work():
            start = time.perf_counter()
            try:
                warning = paste_text(text, target, owner, cancel)
                self.events.put(("result", replace(result, used_text=text, insertion_sent=True, insertion_error=None,
                    insertion_seconds=time.perf_counter() - start, warning=warning or "")))
                self.events.put(("done", warning or "Preserved text paste sent to the original input. Clipboard restored."))
            except Exception as exc:
                self.events.put(("result", replace(result, insertion_error=str(exc), possibly_sent=bool(getattr(exc, "possibly_sent", False)),
                    insertion_seconds=time.perf_counter() - start)))
                self.events.put(("error", str(exc)))
        self.worker = threading.Thread(target=work, name="raw-recovery-paste")
        self.worker.start()

    def _observe_target(self):
        # Only native focus metadata is inspected, never input text or keystrokes.
        try:
            target = WindowTarget.capture()
            if target.process_id != os.getpid():
                self.last_external_target = target if target.process_id and target.focus_hwnd else None
                self.last_external_seen = time.monotonic()
        except RuntimeError:
            self.last_external_target = None

    def _record_clicked(self):
        if self.state == State.RECORDING:
            self._stop_recording()
            return
        if self.closing or self.testing_api or self.state not in (State.IDLE, State.ERROR) or (self.worker and self.worker.is_alive()):
            return
        try:
            target = self.last_external_target
            if not target or time.monotonic() - self.last_external_seen > 30:
                raise RuntimeError("Focus the intended input in another app, then click Record. Or use the global shortcut directly in that input.")
            self._capture_preflight()
            target.restore()  # Verify the remembered input before opening the microphone.
            self._toggle(target, None)
        except Exception as exc:
            self._set_state(State.ERROR, str(exc))

    def _toggle(self, target, error):
        if self.closing or self.testing_api or (self.download_worker and self.download_worker.is_alive()):
            return
        if self.state == State.RECORDING:
            self._stop_recording()
            return
        if self.state not in (State.IDLE, State.ERROR) or (self.worker and self.worker.is_alive()):
            return
        attempted_capture = False
        try:
            if error:
                raise RuntimeError(error)
            if target.process_id == os.getpid():
                raise RuntimeError("Focus another application's input field before starting dictation.")
            target.validate()
            self._capture_preflight()
            if not self.overlay:
                raise RuntimeError("The recording indicator is unavailable. Restart the app. " + self.overlay_error)
            self._preview("")
            self.cancel_event = threading.Event()
            self.pending_raw = None
            self.last_result = None
            self.history_error = ""
            self.stage_latencies = {}
            self.session_mode = self.settings.output_mode
            if self.active_session:
                self.view.workspace.clear_filters()
            self._freeze_context(target)
            attempted_capture = True
            self.recorder.start()
            capture = self.recorder.snapshot()
            if capture.error or not capture.active:
                raise RuntimeError(capture.error or "Microphone capture did not become active.")
            self.target = target
            self.target_stale = False
            self.started = time.monotonic() - capture.seconds
            self.overlay.begin(target, self.session_mode, self.settings)
            self._set_state(State.RECORDING, "Recording… press the shortcut again to stop, or Cancel to discard.")
        except Exception as exc:
            # A failed start may retain resources; retry cleanup only for this attempt.
            if attempted_capture:
                try:
                    self.recorder.cancel()
                except Exception as cleanup:
                    exc = RuntimeError(str(exc) + " Microphone cleanup: " + str(cleanup))
            self._set_state(State.ERROR, str(exc))
            if self.overlay:
                try:
                    self.overlay.failure(target, self.settings.output_mode, self.settings)
                except Exception as indicator_error:
                    self.overlay_error = str(indicator_error)

    def _stop_recording(self):
        if self.state != State.RECORDING:
            return
        try:
            recording = self.recorder.stop()
            service = self._create_service()
            if self.overlay:
                self.overlay.elapsed = recording.seconds
            self._set_state(State.TRANSCRIBING, "Transcribing locally on CPU; audio is not uploaded…" if self.settings.transcription_backend == "local" else "Uploading explicit recording for transcription…")
            target, owner, cancel = self.target, self.owner, self.cancel_event
            def paste(text, destination, cancellation):
                return paste_text(text, destination, owner, cancellation)
            self.worker = threading.Thread(target=run_pipeline,
                args=(recording, service, target, paste, lambda kind, value: self.events.put((kind, value)), cancel),
                kwargs={"mode": self.session_mode},
                name="dictation-pipeline")  # Non-daemon: clipboard cleanup must finish even on close.
            self.worker.start()
        except Exception as exc:
            try:
                self.recorder.cancel()
            except Exception:
                pass
            self._set_state(State.ERROR, str(exc))

    def cancel(self):
        self.cancel_event.set()
        self.download_cancel.set()
        if self.download_worker and self.download_worker.is_alive():
            self.local_status.set("Cancellation requested. The active download may finish its current transfer first; files are kept.")
        if self.state == State.RECORDING:
            try:
                self.recorder.cancel()
                self._set_state(State.IDLE, "Recording discarded. No audio was uploaded.")
            except Exception as exc:
                self._set_state(State.ERROR, str(exc))
        elif self.worker and self.worker.is_alive():
            self._set_state(State.CANCELLING, "Cancelling local transcription… waiting for the current CPU stage. Nothing else will be inserted." if self.settings.transcription_backend == "local" else "Cancelling… an in-flight API request may take up to its 60-second network timeout. Already-sent audio cannot be recalled.")

    def _transcribe_file(self):
        if self.is_busy():
            return
        path = filedialog.askopenfilename(title="Select an audio or video file to transcribe",
            filetypes=[("Audio/video", "*.mp3 *.wav *.m4a *.mp4 *.mov *.mkv *.aac *.flac *.ogg *.webm"), ("All files", "*.*")],
            parent=self.root)
        if not path:
            return
        try:
            local = self._get_local()
            local.check_available()
        except Exception as exc:
            self._set_state(State.ERROR, "File transcription unavailable: " + str(exc))
            return
        self.cancel_event = threading.Event()
        cancel = self.cancel_event
        self._set_state(State.TRANSCRIBING, "Transcribing file locally on CPU; nothing is uploaded…")
        def work():
            from .local_stt import LocalTranscriptionCancelled
            try:
                segments = local.transcribe_file(path, cancel=cancel)
                self.events.put(("file_transcribed", (path, segments)))
            except LocalTranscriptionCancelled as exc:
                self.events.put(("cancelled", str(exc)))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        self.worker = threading.Thread(target=work, name="file-transcription")
        self.worker.start()

    def _capture_failed(self, error):
        if self.state == State.RECORDING:
            try:
                self.recorder.cancel()
            except Exception as exc:
                error += " Microphone cleanup: " + str(exc)
            self._set_state(State.ERROR, error + " Recording was not uploaded.")
