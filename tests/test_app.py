import os
from pathlib import Path
import threading
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from chatterbox.app import App
from chatterbox.audio import Recording, CaptureStatus
from chatterbox.pipeline import State
from chatterbox.settings import Settings


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.store = Mock()
        self.folder = tempfile.TemporaryDirectory()
        self.store.directory = Path(self.folder.name)
        self.store.load.return_value = Settings()
        self.store.key_path = Path("missing-synthetic-key.dpapi")
        self.store.key_path_for.return_value = self.store.key_path
        self.store.get_key.return_value = "synthetic-test-key"
        self.patchers = []
        dialog_patch = patch("chatterbox.app.messagebox.askyesno", return_value=True)
        dialog_patch.start()
        self.patchers.append(dialog_patch)
        for module, name in (("app", "GlobalHotkey"), ("app", "Recorder"), ("recording_controller", "create_service"),
                             ("recording_controller", "paste_text"), ("recording_controller", "WindowTarget"),
                             ("app", "FloatingOverlay"), ("app", "SystemTray")):
            patcher = patch(f"chatterbox.{module}.{name}")
            value = patcher.start()
            self.patchers.append(patcher)
            setattr(self, name, value)
        self.WindowTarget.capture.return_value = Mock(process_id=os.getpid(), focus_hwnd=1)
        self.paste_text.return_value = ""
        self.service = self.create_service.return_value
        self.service.transcribe.return_value = "um hello"
        self.service.refine.return_value = "Hello."
        self.Recorder.return_value.stop.return_value = Recording(b"RIFFsynthetic", 1, 1000)
        self.Recorder.return_value.snapshot.return_value = CaptureStatus(True, 0.1, 0.03, time.monotonic())
        self.SystemTray.return_value.available = False
        self.app = App(self.root, self.store)
        self.target = Mock(process_id=os.getpid() + 100)

    def tearDown(self):
        self.app.cancel_event.set()
        if self.app.worker:
            self.app.worker.join(timeout=2)
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.app.close()
        self.folder.cleanup()
        for patcher in reversed(self.patchers):
            patcher.stop()

    def drain(self):
        deadline = time.monotonic() + 2
        while (self.app.worker and self.app.worker.is_alive()) and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.app._poll()

    def test_startup_never_records(self):
        self.Recorder.return_value.start.assert_not_called()
        self.assertEqual(self.app.state, State.IDLE)

    def test_missing_key_refuses_microphone_start(self):
        self.store.get_key.side_effect = RuntimeError("Set your OpenAI API key")
        self.app._toggle(self.target, None)
        self.assertEqual(self.app.state, State.ERROR)
        self.Recorder.return_value.start.assert_not_called()

    def test_controller_foreground_refuses_recording(self):
        self.app._toggle(Mock(process_id=os.getpid()), None)
        self.assertEqual(self.app.state, State.ERROR)
        self.Recorder.return_value.start.assert_not_called()

    def test_second_hotkey_stops_and_keeps_start_target(self):
        self.app._toggle(self.target, None)
        self.assertEqual(self.app.state, State.RECORDING)
        self.app._toggle(Mock(process_id=os.getpid() + 200), None)
        self.drain()
        self.paste_text.assert_called_once()
        self.assertIs(self.paste_text.call_args.args[1], self.target)
        self.assertEqual(self.app.preview.get("1.0", "end-1c"), "Hello.")
        self.assertEqual(self.app.state, State.IDLE)

    def test_cancel_recording_discards_without_upload(self):
        self.app._toggle(self.target, None)
        self.app.cancel()
        self.Recorder.return_value.cancel.assert_called_once()
        self.create_service.assert_not_called()
        self.assertEqual(self.app.state, State.IDLE)

    def test_processing_refuses_new_recordings(self):
        self.app.state = State.REFINING
        self.app._toggle(self.target, None)
        self.Recorder.return_value.start.assert_not_called()

    def test_apply_conflicting_hotkey_retains_old_listener(self):
        old = self.app.hotkey
        self.app.hotkey_var.set("Ctrl+I")
        failed = Mock()
        failed.start.side_effect = RuntimeError("shortcut conflict")
        self.GlobalHotkey.return_value = failed
        self.app._save_settings()
        self.assertIs(self.app.hotkey, old)
        old.close.assert_not_called()
        self.assertEqual(self.app.settings.hotkey, "Ctrl+Alt+Space")
        self.assertEqual(self.app.state, State.ERROR)

    def select_openrouter(self):
        self.app.provider_var.set("OpenRouter")
        self.app._provider_changed()

    def test_provider_selection_fills_models_without_moving_credentials(self):
        self.select_openrouter()
        self.assertEqual(self.app.transcribe_var.get(), "openai/gpt-transcribe")
        self.assertEqual(self.app.refine_var.get(), "openai/gpt-5.6-terra")
        self.store.save_key.assert_not_called()
        self.store.get_key.assert_not_called()
        self.assertTrue(self.app._save_settings())
        self.assertEqual(self.app.settings.provider, "openrouter")
        self.assertIn("openrouter.ai/api/v1", self.app.api_url.get())
        self.assertIn("upstream provider", self.app.privacy_message.get())

    def test_unapplied_provider_prevents_recording(self):
        self.select_openrouter()
        self.app._toggle(self.target, None)
        self.Recorder.return_value.start.assert_not_called()
        self.assertEqual(self.app.state, State.ERROR)

    def test_key_dialog_targets_selected_provider_and_applies_selection(self):
        self.select_openrouter()
        with patch("chatterbox.settings_controller.simpledialog.askstring", return_value="synthetic-openrouter-key") as dialog:
            self.app._set_key()
        self.assertEqual(self.app.settings.provider, "openrouter")
        self.assertEqual(dialog.call_args.args[0], "OpenRouter API key")
        self.store.save_key.assert_called_once_with("synthetic-openrouter-key", "openrouter")

    def test_delete_removes_only_selected_provider_key(self):
        self.select_openrouter()
        self.app._delete_key()
        self.store.delete_key.assert_called_once_with("openrouter")

    def test_openrouter_recording_checks_only_openrouter_key(self):
        self.select_openrouter()
        self.app._save_settings()
        self.app._toggle(self.target, None)
        self.store.get_key.assert_called_once_with("openrouter")
        self.assertEqual(self.app.state, State.RECORDING)

    def test_main_view_hides_settings_and_branding(self):
        self.assertEqual(self.app.view.page, "main")
        self.assertEqual(self.app.view.settings_page.winfo_manager(), "")
        self.assertEqual(self.app.root.title(), "Chatterbox — Ready")
        self.assertEqual(self.app.record_button.text, "Record")
        self.assertEqual(self.app.view.error_label.winfo_manager(), "")

    def test_settings_navigation_preserves_preview(self):
        self.app._preview("Existing text.")
        self.app.view.toggle_settings()
        self.assertEqual(self.app.view.page, "settings")
        self.assertEqual(self.app.view.main_page.winfo_manager(), "")
        self.app.view._escape(None)
        self.assertEqual(self.app.view.page, "main")
        self.assertEqual(self.app.preview.get("1.0", "end-1c"), "Existing text.")

    def test_theme_toggle_persists_without_touching_api_configuration(self):
        before = self.app.settings
        self.app._toggle_theme()
        saved = self.store.save.call_args.args[0]
        self.assertEqual(saved.theme, "dark")
        self.assertEqual(saved.provider, before.provider)
        self.assertEqual(saved.hotkey, before.hotkey)
        self.assertEqual(saved.transcription_model, before.transcription_model)
        self.assertEqual(saved.refinement_model, before.refinement_model)
        self.store.save_key.assert_not_called()
        self.assertEqual(self.app.preview.cget("bg"), "#222222")
        self.app._toggle_theme()
        self.assertEqual(self.app.settings.theme, "light")

    def test_apply_settings_preserves_theme(self):
        self.app._toggle_theme()
        self.select_openrouter()
        self.assertTrue(self.app._save_settings())
        self.assertEqual(self.app.settings.theme, "dark")

    def test_theme_save_failure_preserves_current_theme(self):
        self.store.save.side_effect = OSError("synthetic save failure")
        self.app._toggle_theme()
        self.assertEqual(self.app.settings.theme, "light")
        self.assertEqual(self.app.preview.cget("bg"), "#FFFFFF")
        self.assertEqual(self.app.state, State.ERROR)

    def test_record_button_missing_target_refuses_microphone(self):
        self.app.last_external_target = None
        self.app.record_button.invoke()
        self.Recorder.return_value.start.assert_not_called()
        self.assertEqual(self.app.state, State.ERROR)

    def test_record_button_stale_target_refuses_microphone(self):
        self.app.last_external_target = self.target
        self.app.last_external_seen = time.monotonic() - 31
        self.app.record_button.invoke()
        self.Recorder.return_value.start.assert_not_called()
        self.target.restore.assert_not_called()

    def test_record_button_focus_refusal_precedes_microphone(self):
        self.app.last_external_target = self.target
        self.app.last_external_seen = time.monotonic()
        self.target.restore.side_effect = RuntimeError("focus refused")
        self.app.record_button.invoke()
        self.Recorder.return_value.start.assert_not_called()
        self.assertIn("focus refused", self.app.message.get())

    def test_record_button_start_stop_keeps_verified_original_target(self):
        self.app.last_external_target = self.target
        self.app.last_external_seen = time.monotonic()
        self.app.record_button.invoke()
        self.target.restore.assert_called_once()
        self.Recorder.return_value.start.assert_called_once()
        self.assertEqual(self.app.record_button.text, "Stop")
        self.app.last_external_target = Mock(process_id=os.getpid() + 200)
        self.app.record_button.invoke()
        self.drain()
        self.assertIs(self.paste_text.call_args.args[1], self.target)
        self.assertEqual(self.app.record_button.text, "Record")

    def test_processing_disables_record_button_and_exposes_cancel(self):
        self.app._set_state(State.REFINING, "Synthetic processing")
        self.app.record_button.invoke()
        self.Recorder.return_value.start.assert_not_called()
        self.assertEqual(self.app.record_button.state, "disabled")
        self.assertEqual(self.app.cancel_button.winfo_manager(), "pack")

    def test_tracker_observes_external_focus_without_recording(self):
        self.WindowTarget.capture.return_value = self.target
        self.app._observe_target()
        self.assertIs(self.app.last_external_target, self.target)
        self.Recorder.return_value.start.assert_not_called()

    def test_tracker_does_not_replace_target_with_controller(self):
        self.app.last_external_target = self.target
        self.app._observe_target()
        self.assertIs(self.app.last_external_target, self.target)

    def test_tracker_failure_invalidates_ambiguous_target(self):
        self.app.last_external_target = self.target
        self.WindowTarget.capture.side_effect = RuntimeError("focus unavailable")
        self.app._observe_target()
        self.assertIsNone(self.app.last_external_target)

    def test_preview_placeholder_and_actionable_error_visibility(self):
        self.assertEqual(self.app.view.placeholder.winfo_manager(), "place")
        self.app._preview("Hello.")
        self.assertEqual(self.app.view.placeholder.winfo_manager(), "")
        self.app._set_state(State.ERROR, "Actionable error")
        self.assertEqual(self.app.view.error_label.winfo_manager(), "pack")
        self.app._preview("")
        self.assertEqual(self.app.view.placeholder.winfo_manager(), "place")

    def test_settings_help_remains_scrollable_in_small_window(self):
        self.root.geometry("520x540")
        self.root.deiconify()
        self.app.view.toggle_settings()
        self.root.update()
        canvas = self.app.view.settings_area.canvas
        self.assertGreater(canvas.bbox("all")[3], canvas.winfo_height())
        canvas.yview_moveto(1)
        self.assertGreater(canvas.yview()[0], 0)

    def test_external_window_without_input_invalidates_prior_target(self):
        self.app.last_external_target = self.target
        self.WindowTarget.capture.return_value = Mock(process_id=os.getpid() + 100, focus_hwnd=0)
        self.app._observe_target()
        self.assertIsNone(self.app.last_external_target)

    def test_record_button_missing_key_does_not_change_focus(self):
        self.app.last_external_target = self.target
        self.app.last_external_seen = time.monotonic()
        self.store.get_key.side_effect = RuntimeError("Missing key")
        self.app.record_button.invoke()
        self.target.restore.assert_not_called()
        self.Recorder.return_value.start.assert_not_called()

    def test_main_mode_selection_persists_without_touching_credentials(self):
        self.assertTrue(self.app._set_output_mode("raw"))
        self.assertEqual(self.app.settings.output_mode, "raw")
        self.assertEqual(self.app.mode_var.get(), "Raw")
        self.store.save_key.assert_not_called()
        self.assertEqual(self.store.save.call_args.args[0].output_mode, "raw")

    def test_mode_save_failure_rolls_back_selection(self):
        self.store.save.side_effect = OSError("cannot save")
        self.assertFalse(self.app._set_output_mode("raw"))
        self.assertEqual(self.app.settings.output_mode, "polished")
        self.assertEqual(self.app.mode_var.get(), "Polished")

    def test_invalid_mode_is_rejected_before_save(self):
        self.assertFalse(self.app._set_output_mode("other"))
        self.store.save.assert_not_called()

    def test_mode_is_fixed_during_recording_and_raw_skips_refine(self):
        self.app._set_output_mode("raw")
        self.app._toggle(self.target, None)
        self.assertFalse(self.app._set_output_mode("polished"))
        self.assertEqual(self.app.mode_var.get(), "Raw")
        self.app._stop_recording()
        self.drain()
        self.service.refine.assert_not_called()
        self.assertEqual(self.app.last_result.raw, "um hello")
        self.assertIsNone(self.app.last_result.polished)
        self.assertEqual(self.paste_text.call_args.args[0], "um hello")

    def test_polished_result_preserves_raw_and_polished_in_memory(self):
        self.app._toggle(self.target, None)
        self.app._stop_recording()
        self.drain()
        self.assertEqual(self.app.last_result.raw, "um hello")
        self.assertEqual(self.app.last_result.polished, "Hello.")
        self.assertEqual(len(self.app.recent_results), 1)
        self.assertTrue(self.app.last_result.insertion_sent)

    def test_refinement_failure_offers_explicit_raw_insertion_only(self):
        self.service.refine.side_effect = RuntimeError("polish failed")
        self.app._toggle(self.target, None)
        self.app._stop_recording()
        self.drain()
        self.paste_text.assert_not_called()
        self.assertEqual(self.app.state, State.ERROR)
        self.assertEqual(self.app.pending_raw.raw, "um hello")
        self.assertEqual(self.app.insert_raw_button.winfo_manager(), "pack")
        self.app._insert_raw()
        self.drain()
        self.paste_text.assert_called_once_with("um hello", self.target, self.app.owner, self.app.cancel_event)
        self.assertIsNone(self.app.pending_raw)
        self.assertEqual(self.app.last_result.used_text, "um hello")
        self.service.transcribe.assert_called_once()
        self.service.refine.assert_called_once()

    def test_failed_raw_insertion_keeps_recovery_action_and_original_target(self):
        self.service.refine.side_effect = RuntimeError("polish failed")
        self.app._toggle(self.target, None)
        self.app._stop_recording()
        self.drain()
        self.paste_text.side_effect = RuntimeError("original focus changed")
        self.app._insert_raw()
        self.drain()
        self.assertEqual(self.app.state, State.ERROR)
        self.assertIsNotNone(self.app.pending_raw)
        self.assertEqual(self.app.insert_raw_button.winfo_manager(), "pack")
        self.assertIs(self.paste_text.call_args.args[1], self.target)

    def test_recording_requires_confirmed_microphone_samples(self):
        self.Recorder.return_value.snapshot.return_value = CaptureStatus(False, 0, 0, 0)
        self.app._toggle(self.target, None)
        self.assertEqual(self.app.state, State.ERROR)
        self.FloatingOverlay.return_value.begin.assert_not_called()
        self.Recorder.return_value.cancel.assert_called_once()

    def test_silence_with_active_capture_is_recording_not_error(self):
        self.Recorder.return_value.snapshot.return_value = CaptureStatus(True, 0.04, 0, time.monotonic())
        self.app._toggle(self.target, None)
        self.assertEqual(self.app.state, State.RECORDING)
        self.FloatingOverlay.return_value.begin.assert_called_once()

    def test_capture_stall_cancels_without_upload(self):
        self.app._toggle(self.target, None)
        self.Recorder.return_value.snapshot.return_value = CaptureStatus(False, 1, 0, 0, "stale capture")
        self.app._poll()
        self.assertEqual(self.app.state, State.ERROR)
        self.create_service.assert_not_called()
        self.Recorder.return_value.cancel.assert_called_once()

    def test_close_window_hides_when_tray_available_without_stopping_capture(self):
        self.SystemTray.return_value.available = True
        self.app._toggle(self.target, None)
        self.app._on_window_close()
        self.assertEqual(self.root.state(), "withdrawn")
        self.assertEqual(self.app.state, State.RECORDING)
        self.Recorder.return_value.cancel.assert_not_called()

    def test_apply_and_theme_preserve_mode_and_overlay_preferences(self):
        self.app._set_output_mode("raw")
        self.app.overlay_position_var.set("Top Center")
        self.app.overlay_margin_var.set("36")
        self.assertTrue(self.app._save_settings())
        self.app._toggle_theme()
        self.assertEqual(self.app.settings.output_mode, "raw")
        self.assertEqual(self.app.settings.overlay_position, "top-center")
        self.assertEqual(self.app.settings.overlay_margin, 36)

    def test_overlay_error_does_not_break_polling(self):
        self.app._toggle(self.target, None)
        self.FloatingOverlay.return_value.phase.side_effect = RuntimeError("indicator failure")
        self.app._set_state(State.RECORDING, "Recording")
        self.app._poll()
        self.assertEqual(self.app.state, State.ERROR)
        self.create_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
