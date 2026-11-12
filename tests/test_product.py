from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from test_app import ControllerTests as BaseControllerTests
from chatterbox.api import create_service, ApiError, LocalDictationService
from chatterbox.destination import collect_destination
from chatterbox.history import HistoryStore
from chatterbox.pipeline import DictationResult, run_pipeline, State
from chatterbox.settings import Settings, Store


class ProductControllerTests(BaseControllerTests):
    # Reuse fixture only, not the inherited tests (loader removes them below).
    def test_named_session_auto_appends_and_keeps_both_versions(self):
        self.app._new_session()
        session = self.app.active_session
        self.app._toggle(self.target, None)
        self.app._stop_recording()
        self.drain()
        rows = self.app.history.list_dictations(session)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["raw"], rows[0]["polished"]), ("um hello", "Hello."))
        self.assertTrue(rows[0]["insertion_sent"])
        self.assertIsNotNone(rows[0]["transcription_seconds"])
        self.assertIsNotNone(rows[0]["refinement_seconds"])

    def test_quick_dictation_does_not_create_history(self):
        self.app._toggle(self.target, None)
        self.app._stop_recording()
        self.drain()
        self.assertEqual(self.app.history.list_sessions(), [])
        self.assertIsNone(self.app.history.get_active_session())

    def test_session_context_does_not_move_after_capture(self):
        self.app._new_session()
        original = self.app.active_session
        self.app._toggle(self.target, None)
        other = self.app.history.create_session("Other")
        self.app.active_session = other  # Simulate a later unrelated UI change.
        self.app._stop_recording()
        self.drain()
        self.assertEqual(len(self.app.history.list_dictations(original)), 1)
        self.assertEqual(self.app.history.list_dictations(other), [])

    def test_navigation_and_settings_are_blocked_while_recording(self):
        self.app._toggle(self.target, None)
        self.assertFalse(self.app._select_session(None))
        self.assertFalse(self.app._save_settings())
        self.assertFalse(self.app._set_output_mode("raw"))

    def test_tracking_choice_is_frozen_at_capture(self):
        self.app._new_session()
        with patch("chatterbox.sessions.collect_destination", return_value={"application": "notepad.exe", "window_title": None}) as track:
            self.app._toggle(self.target, None)
        track.assert_called_once_with(self.target, True, False)
        self.app._stop_recording()
        self.drain()
        row = self.app.history.list_dictations(self.app.active_session)[0]
        self.assertEqual(row["application"], "notepad.exe")
        self.assertIsNone(row["window_title"])

    def test_uncertain_result_does_not_allow_raw_or_retry(self):
        result = DictationResult("raw", insertion_error="cleanup", possibly_sent=True)
        self.app._remember_result(result)
        self.app.pending_raw = result
        self.app._insert_raw()
        self.app._retry_insertion()
        self.paste_text.assert_not_called()

    def test_recovery_hidden_for_different_session(self):
        self.app._remember_result(DictationResult("raw", insertion_error="focus"))
        self.app._new_session()
        self.assertFalse(self.app.view.workspace.recovery_visible())
        self.app._retry_insertion()
        self.paste_text.assert_not_called()

    def test_delete_session_requires_confirmation(self):
        self.app._new_session()
        selected = self.app.active_session
        with patch("chatterbox.sessions.messagebox.askyesno", return_value=False):
            self.app._delete_session()
        self.assertEqual(self.app.active_session, selected)
        self.assertEqual(len(self.app.history.list_sessions()), 1)
        with patch("chatterbox.sessions.messagebox.askyesno", return_value=True):
            self.app._delete_session()
        self.assertEqual(self.app.history.list_sessions(), [])
        self.assertIsNone(self.app.active_session)

    def test_history_review_preserves_raw_polished_edited(self):
        self.app._new_session()
        result = DictationResult("raw", polished="Polished.")
        self.app.dictation_context = {"chat_session_id": self.app.active_session, "provider": "openrouter"}
        self.app._remember_result(result)
        self.app.history.edit_dictation(result.session_id, "Edited.")
        for version, expected in (("Raw", "raw"), ("Polished", "Polished."), ("Edited", "Edited.")):
            self.app._review_transcript(result.session_id, version)
            self.assertEqual(self.app.preview.get("1.0", "end-1c"), expected)

    def test_local_missing_model_refuses_microphone(self):
        self.app.settings = replace(self.app.settings, transcription_backend="local")
        self.app.backend_var.set("Local Whisper")
        with patch.object(self.app, "_get_local") as local:
            local.return_value.check_available.side_effect = RuntimeError("model absent")
            self.app._toggle(self.target, None)
        self.Recorder.return_value.start.assert_not_called()
        self.assertEqual(self.app.state, State.ERROR)


# Avoid duplicating 42 inherited controller tests in discovery; keep shared fixture.
for _name in dir(BaseControllerTests):
    if _name.startswith("test_") and _name not in ProductControllerTests.__dict__:
        setattr(ProductControllerTests, _name, None)
del BaseControllerTests


class SettingsValidationTests(unittest.TestCase):
    def test_local_refinement_requires_local_transcription(self):
        for backend in ("local", "local_llm"):
            with self.subTest(backend=backend):
                settings = replace(Settings.for_provider("openai"), refinement_backend=backend, transcription_backend="cloud")
                with self.assertRaisesRegex(ValueError, "requires Local Whisper transcription"):
                    settings.validate()

    def test_local_refinement_with_local_transcription_is_valid(self):
        for backend in ("local", "local_llm"):
            with self.subTest(backend=backend):
                replace(Settings.for_provider("openai"), refinement_backend=backend, transcription_backend="local").validate()


class LocalCompositionTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.local = Mock()
        self.local.check_available.return_value = "Local files and CPU dependencies available."
        self.settings = replace(Settings.for_provider("openrouter"), transcription_backend="local", output_mode="raw")

    def test_raw_local_factory_does_not_read_key_or_construct_cloud(self):
        with patch("chatterbox.api.OpenRouterService") as cloud:
            service = create_service(self.store, self.settings, local=self.local)
            service.transcribe(b"RIFFfixture")
            service.check_models()
        self.local.transcribe.assert_called_once_with(b"RIFFfixture")
        self.store.get_key.assert_not_called()
        cloud.assert_not_called()

    def test_local_polished_only_creates_cloud_at_refinement(self):
        settings = replace(self.settings, output_mode="polished")
        with patch("chatterbox.api.OpenRouterService") as cloud:
            service = create_service(self.store, settings, local=self.local)
            service.transcribe(b"RIFFfixture")
            cloud.assert_not_called()
            service.refine("raw")
        self.store.get_key.assert_called_once_with("openrouter")
        cloud.return_value.refine.assert_called_once_with("raw")

    def test_local_check_ignores_cloud_speech_model(self):
        settings = replace(self.settings, output_mode="polished")
        with patch("chatterbox.api.OpenRouterService") as cloud:
            cloud.return_value._request.return_value = {"data": {}}
            cloud.return_value._models.return_value = {settings.refinement_model: {"architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}}}
            create_service(self.store, settings, local=self.local).check_models()
            cloud.return_value._models.assert_called_once_with()

    def test_local_refinement_backend_skips_cloud_entirely(self):
        settings = replace(self.settings, output_mode="polished", refinement_backend="local")
        with patch("chatterbox.api.OpenRouterService") as cloud, patch("chatterbox.api.OpenAIService") as cloud2:
            service = create_service(self.store, settings, local=self.local)
            service.transcribe(b"RIFFfixture")
            text = service.refine("um so  this is , a test")
        self.assertEqual(text, "So this is, a test.")
        self.store.get_key.assert_not_called()
        cloud.assert_not_called()
        cloud2.assert_not_called()

    def test_local_refinement_backend_check_models_needs_no_key(self):
        settings = replace(self.settings, output_mode="polished", refinement_backend="local")
        message = create_service(self.store, settings, local=self.local).check_models()
        self.assertIn("local rule-based", message)
        self.store.get_key.assert_not_called()

    def test_local_llm_refinement_backend_skips_cloud_entirely(self):
        settings = replace(self.settings, output_mode="polished", refinement_backend="local_llm")
        refiner = Mock()
        refiner.refine.return_value = "Refined by local model."
        with patch("chatterbox.api.OpenRouterService") as cloud, patch("chatterbox.api.OpenAIService") as cloud2:
            service = create_service(self.store, settings, local=self.local, local_refiner=refiner)
            service.transcribe(b"RIFFfixture")
            text = service.refine("um so this is a test")
        self.assertEqual(text, "Refined by local model.")
        refiner.refine.assert_called_once_with("um so this is a test")
        self.store.get_key.assert_not_called()
        cloud.assert_not_called()
        cloud2.assert_not_called()

    def test_local_llm_refinement_backend_check_models_checks_refiner(self):
        settings = replace(self.settings, output_mode="polished", refinement_backend="local_llm")
        refiner = Mock()
        refiner.check_available.return_value = "Local refinement model files available."
        message = create_service(self.store, settings, local=self.local, local_refiner=refiner).check_models()
        self.assertIn("local refinement model", message)
        refiner.check_available.assert_called_once_with()
        self.store.get_key.assert_not_called()

    def test_local_llm_refinement_without_refiner_configured_is_a_clear_error(self):
        settings = replace(self.settings, output_mode="polished", refinement_backend="local_llm")
        service = LocalDictationService(self.store, settings, self.local)
        with self.assertRaisesRegex(ApiError, "no local refiner is configured"):
            service.refine("text")

    def test_local_refinement_auth_error_preserves_raw_fallback(self):
        self.store.get_key.side_effect = RuntimeError("key absent")
        self.local.transcribe.return_value = "raw technical API_v2"
        service = LocalDictationService(self.store, replace(self.settings, output_mode="polished"), self.local)
        events, paste = [], Mock()
        run_pipeline(Mock(wav=b"RIFFfixture"), service, Mock(), paste, lambda *event: events.append(event), threading.Event())
        self.assertEqual(events[-1][0], "fallback")
        self.assertEqual(events[-1][1].raw, "raw technical API_v2")
        paste.assert_not_called()


class ProvenanceAndReceiptsTests(unittest.TestCase):
    def test_disabled_tracking_makes_no_native_metadata_calls(self):
        with patch("chatterbox.destination.OpenProcess") as opened, patch("chatterbox.destination.GetWindowText") as title:
            self.assertEqual(collect_destination(Mock(), False, True), {"application": None, "window_title": None})
        opened.assert_not_called()
        title.assert_not_called()

    def test_title_not_read_without_explicit_opt_in(self):
        with patch("chatterbox.destination.OpenProcess", return_value=None), patch("chatterbox.destination.GetWindowText") as title:
            collect_destination(Mock(process_id=123), True, False)
        title.assert_not_called()

    def test_paste_cleanup_error_is_retained_and_not_retryable(self):
        from chatterbox.clipboard import InsertionError
        service = Mock()
        service.transcribe.return_value = "raw"
        paste = Mock(side_effect=InsertionError("cleanup", possibly_sent=True))
        events = []
        run_pipeline(Mock(wav=b"fixture"), service, Mock(), paste, lambda *event: events.append(event), threading.Event(), mode="raw")
        result = [value for kind, value in events if kind == "result"][-1]
        self.assertTrue(result.possibly_sent)
        self.assertFalse(result.can_retry)
        self.assertIsNotNone(result.insertion_seconds)
        self.assertIsNotNone(result.transcription_seconds)
        self.assertIsNone(result.refinement_seconds)

    def test_recent_matches_remain_chronological_and_include_latest(self):
        with tempfile.TemporaryDirectory() as folder:
            history = HistoryStore(Path(folder) / "history.sqlite3")
            try:
                session = history.create_session()
                for i in range(4):
                    history.upsert_dictation(session, {"session_id": f"id{i}", "raw": str(i)}, {"created_at": f"2026-11-11T00:00:0{i}+00:00"})
                self.assertEqual([row["id"] for row in history.list_dictations(session, limit=2, recent=True)], ["id2", "id3"])
                self.assertEqual([row["id"] for row in history.list_dictations(session, limit=2)], ["id0", "id1"])
            finally:
                history.close()


if __name__ == "__main__":
    unittest.main()
