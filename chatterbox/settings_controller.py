"""Settings-page actions: provider/key/model/theme/output-mode; no recording state."""
from __future__ import annotations

from dataclasses import replace
import threading

from tkinter import messagebox, simpledialog

from .pipeline import State
from .settings import PROVIDER_NAMES, PROVIDER_URLS, Settings


class SettingsMixin:
    def _update_key_status(self):
        provider = self.settings.provider
        name = PROVIDER_NAMES[provider]
        stored = self.store.key_path_for(provider).exists()
        self.key_status.set(f"{name} key: " + ("stored with Windows user encryption" if stored else "not configured — use Set API key"))
        self.api_url.set("API: " + PROVIDER_URLS[provider])
        destination = "OpenRouter and its upstream provider" if provider == "openrouter" else "OpenAI"
        path = "Local Whisper on CPU" if self.settings.transcription_backend == "local" else destination
        refinement = ("local rule-based cleanup, no network" if self.settings.refinement_backend == "local"
                      else "a local offline LLM, no network" if self.settings.refinement_backend == "local_llm"
                      else f"the configured cloud provider ({destination})")
        self.privacy_message.set(f"Shortcut: {self.settings.hotkey}. Mode: {self.settings.output_mode.title()}. Source: {path}.\nStop transcribes your explicit recording; Cancel discards it. Raw skips rewriting. Polished mode uses {refinement}.\nQuick Dictation is not saved to history. Sessions save text locally, never audio. Window titles are opt-in. No automatic Enter. Maximum recording: 120 seconds.")
        available = self.tray and self.tray.available
        self.tray_status.set("Background mode: closing hides to the tray. Use tray Exit to quit." if available else "Tray unavailable: minimize to keep running; closing exits.")
        if self.overlay_error:
            self.tray_status.set(self.tray_status.get() + " Indicator error: " + self.overlay_error)

    def _download_model(self):
        if self.is_busy() or not self._save_settings():
            return
        model = self.settings.local_model
        if not messagebox.askyesno("Download local model", f"Download the multilingual {model} Whisper model from Systran on Hugging Face?\nThis may download hundreds of MB. No audio, transcription or API key is sent.\nDependencies must already be installed; this does not install packages.", parent=self.root):
            return
        self.download_cancel = threading.Event()
        self.local_status.set("Preparing explicit model download…")
        def work():
            try:
                from .local_stt import download_model
                path = download_model(model, self.store.directory / "models",
                    lambda text: self.events.put(("local_progress", text)), self.download_cancel)
                self.events.put(("model_ready", str(path)))
            except Exception as exc:
                self.events.put(("model_error", str(exc)))
        self.download_worker = threading.Thread(target=work, name="local-model-download", daemon=True)
        self.download_worker.start()
        if self.overlay:
            self.overlay.hide()
        self._set_state(State.DOWNLOADING, "Downloading the explicitly selected local model; no audio or API key is sent.")

    def _download_refine_model(self):
        if self.is_busy() or not self._save_settings():
            return
        if not messagebox.askyesno("Download refinement model", "Download the local refinement LLM (Qwen2.5-1.5B-Instruct, roughly 1 GB) from its public Hugging Face repo?\nNo transcript, audio, or API key is sent.\nllama-cpp-python must already be installed; this does not install packages.", parent=self.root):
            return
        self.download_cancel = threading.Event()
        self.local_status.set("Preparing explicit refinement model download…")
        def work():
            try:
                from .local_llm import download_model
                path = download_model(self.store.directory / "models",
                    lambda text: self.events.put(("local_progress", text)), self.download_cancel)
                self.events.put(("model_ready", str(path)))
            except Exception as exc:
                self.events.put(("model_error", str(exc)))
        self.download_worker = threading.Thread(target=work, name="local-refine-model-download", daemon=True)
        self.download_worker.start()
        if self.overlay:
            self.overlay.hide()
        self._set_state(State.DOWNLOADING, "Downloading the local refinement model; no transcript or API key is sent.")

    def _set_output_mode(self, mode):
        if self.is_busy():
            self.mode_var.set(self.settings.output_mode.title())
            return False
        try:
            updated = replace(self.settings, output_mode=mode)
            updated.validate()
            self.store.save(updated)
            self.settings = updated
            self.mode_var.set(mode.title())
            self._update_key_status()
            self._set_state(self.state, self.message.get())
            return True
        except Exception as exc:
            self.mode_var.set(self.settings.output_mode.title())
            self._set_state(State.ERROR, "Could not save output mode: " + str(exc))
            return False

    def _toggle_theme(self):
        if self.closing:
            return
        try:
            updated = replace(self.settings, theme="dark" if self.settings.theme == "light" else "light")
            self.store.save(updated)
            self.settings = updated
            self.view.apply_theme(updated.theme)
            if self.overlay:
                self.overlay.theme = updated.theme
        except Exception as exc:
            self._set_state(State.ERROR, "Could not save appearance: " + str(exc))

    def _provider_changed(self, event=None):
        provider = self.provider_var.get().lower()
        defaults = Settings.for_provider(provider, self.hotkey_var.get())
        self.transcribe_var.set(defaults.transcription_model)
        self.refine_var.set(defaults.refinement_model)
        self.api_url.set("Selected API: " + PROVIDER_URLS[provider])
        self.message.set("Provider/model defaults selected. Apply settings to activate; credentials are never moved between providers.")

    def _ensure_provider_applied(self):
        if self.is_busy():
            return False
        return self.provider_var.get().lower() == self.settings.provider or self._save_settings()

    def _save_settings(self):
        if self.is_busy():
            return False
        old = self.settings
        old_listener = self.hotkey
        try:
            new = replace(self.settings, hotkey=self.hotkey_var.get().strip(),
                          transcription_model=self.transcribe_var.get().strip(), refinement_model=self.refine_var.get().strip(),
                          provider=self.provider_var.get().lower(),
                          overlay_position=self.overlay_position_var.get().lower().replace(" ", "-"),
                          overlay_margin=int(self.overlay_margin_var.get().strip()),
                          transcription_backend="local" if self.backend_var.get() == "Local Whisper" else "cloud",
                          refinement_backend=("local" if self.refine_backend_var.get() == "Local (rule-based)"
                                              else "local_llm" if self.refine_backend_var.get() == "Local (LLM)" else "cloud"),
                          local_model=self.local_model_var.get(), track_application=self.tracking_var.get(),
                          collect_window_title=self.title_tracking_var.get())
            new.validate()
            if new.hotkey.lower() != old.hotkey.lower():
                # Try the new key before releasing the working old key.
                self._register(new.hotkey)
            self.store.save(new)
            if self.hotkey is not old_listener and old_listener:
                old_listener.close()
            self.settings = new
            self._update_key_status()
            self._set_state(State.IDLE, f"Settings saved. Provider: {PROVIDER_NAMES[new.provider]}. Shortcut: {new.hotkey}")
            return True
        except Exception as exc:
            if self.hotkey is not old_listener:
                self.hotkey.close()
                self.hotkey = old_listener
            self._set_state(State.ERROR, str(exc))
            return False

    def _set_key(self):
        if not self._ensure_provider_applied():
            return
        provider = self.settings.provider
        name = PROVIDER_NAMES[provider]
        key = simpledialog.askstring(f"{name} API key", f"Enter your own {name} API key.\nEncrypted for this Windows account; saved only for {name}.", show="*", parent=self.root)
        if key is None:
            return
        try:
            self.store.save_key(key, provider)
            self._update_key_status()
            self._set_state(State.IDLE, "API key stored securely. Use Check API, then try a short dictation.")
        except Exception as exc:
            self._set_state(State.ERROR, str(exc))
        finally:
            key = None

    def _delete_key(self):
        if not self._ensure_provider_applied():
            return
        if not messagebox.askyesno("Delete stored key", f"Delete the saved {PROVIDER_NAMES[self.settings.provider]} API key?\nYou can enter it again. Other provider keys are not affected.", parent=self.root):
            return
        try:
            self.store.delete_key(self.settings.provider)
            self._update_key_status()
            self._set_state(State.IDLE, f"Stored {PROVIDER_NAMES[self.settings.provider]} key removed. Other provider keys were kept.")
        except Exception as exc:
            self._set_state(State.ERROR, str(exc))

    def _check_api(self):
        if not self._ensure_provider_applied():
            return
        try:
            service = self._create_service()
        except Exception as exc:
            self._set_state(State.ERROR, str(exc))
            return
        self.testing_api = True
        self._set_state(State.TRANSCRIBING, "Checking credentials and model listing (no audio is recorded or uploaded)…")
        def work():
            try:
                self.events.put(("api_ok", service.check_models()))
            except Exception as exc:
                self.events.put(("api_error", str(exc)))
        self.worker = threading.Thread(target=work, name="api-check")
        self.worker.start()
