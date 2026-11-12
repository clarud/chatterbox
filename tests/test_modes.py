from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from chatterbox.api import OpenAIService, OpenRouterService
from chatterbox.audio import Recording
from chatterbox.pipeline import DictationResult, State, run_pipeline
from chatterbox.settings import Settings, Store


class ModePipelineTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.cancel = threading.Event()
        self.service = Mock()
        self.raw = "um use PostgreSQL 17, not eighteen\n keep the API_v2 name "
        self.service.transcribe.return_value = self.raw
        self.service.refine.return_value = "Use PostgreSQL 17, not eighteen. Keep the API_v2 name."
        self.paste = Mock(return_value="")
        self.target = object()

    def run_mode(self, mode):
        run_pipeline(Recording(b"RIFFsynthetic", 1, 1000), self.service, self.target, self.paste,
                     lambda kind, value: self.events.append((kind, value)), self.cancel, mode)

    def test_raw_preserves_stt_exactly_and_skips_refinement(self):
        self.run_mode("raw")
        self.service.refine.assert_not_called()
        self.paste.assert_called_once_with(self.raw, self.target, self.cancel)
        phases = [value for kind, value in self.events if kind == "phase"]
        self.assertEqual(phases, [State.TRANSCRIBING, State.INSERTING])
        result = [value for kind, value in self.events if kind == "result"][-1]
        self.assertEqual(result.raw, self.raw)
        self.assertIsNone(result.polished)
        self.assertEqual(result.used_text, self.raw)
        self.assertTrue(result.insertion_sent)

    def test_polished_keeps_both_results_with_one_session_id(self):
        self.run_mode("polished")
        results = [value for kind, value in self.events if kind == "result"]
        self.assertEqual(len({result.session_id for result in results}), 1)
        self.assertEqual(results[-1].raw, self.raw)
        self.assertEqual(results[-1].polished, self.service.refine.return_value)
        self.assertEqual(results[-1].used_text, self.service.refine.return_value)
        self.assertEqual(results[-1].to_dict()["mode"], "polished")

    def test_failed_polish_requires_explicit_fallback_and_keeps_raw(self):
        self.service.refine.side_effect = RuntimeError("Refinement unavailable")
        self.run_mode("polished")
        self.paste.assert_not_called()
        kind, result = self.events[-1]
        self.assertEqual(kind, "fallback")
        self.assertEqual(result.raw, self.raw)
        self.assertIsNone(result.polished)
        self.assertFalse(result.insertion_sent)
        self.assertEqual(result.refinement_error, "Refinement unavailable")

    def test_invalid_polish_outputs_preserve_raw_without_insertion(self):
        for output in ("", " ", None, "bad\0text"):
            with self.subTest(output=output):
                self.events.clear()
                self.service.refine.return_value = output
                self.run_mode("polished")
                self.assertEqual(self.events[-1][0], "fallback")
                self.paste.assert_not_called()

    def test_cancellation_during_failed_polish_does_not_offer_insertion(self):
        def fail(_):
            self.cancel.set()
            raise RuntimeError("late error")
        self.service.refine.side_effect = fail
        self.run_mode("polished")
        self.assertEqual(self.events[-1][0], "cancelled")
        self.assertNotIn("fallback", [kind for kind, _ in self.events])
        self.paste.assert_not_called()

    def test_cancel_raw_after_transcription_skips_paste(self):
        def transcribe(_):
            self.cancel.set()
            return self.raw
        self.service.transcribe.side_effect = transcribe
        self.run_mode("raw")
        self.paste.assert_not_called()
        self.service.refine.assert_not_called()
        self.assertEqual(self.events[-1][0], "cancelled")

    def test_invalid_mode_stops_before_api(self):
        self.run_mode("other")
        self.service.transcribe.assert_not_called()
        self.assertEqual(self.events[-1][0], "error")

    def test_raw_paste_failure_keeps_text_and_reports_error(self):
        self.paste.side_effect = RuntimeError("focus changed")
        self.run_mode("raw")
        self.assertIn(("preview", self.raw), self.events)
        self.assertEqual(self.events[-1], ("error", "focus changed"))

    def test_invalid_transcription_never_reaches_refinement_or_paste(self):
        for value in (None, "", " ", "bad\0text"):
            self.service.transcribe.return_value = value
            self.run_mode("raw")
        self.service.refine.assert_not_called()
        self.paste.assert_not_called()


class ModeSettingsTests(unittest.TestCase):
    def test_legacy_configuration_defaults_to_polished_without_rewrite(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            original = json.dumps({"provider": "openrouter", "hotkey": "Ctrl+I", "transcription_model": "openai/gpt-transcribe",
                                   "refinement_model": "openai/gpt-5.6-terra", "theme": "dark"})
            store.config_path.write_text(original, encoding="utf-8")
            settings = store.load()
            self.assertEqual(settings.output_mode, "polished")
            self.assertEqual(settings.overlay_position, "bottom-center")
            self.assertEqual(settings.overlay_margin, 24)
            self.assertEqual(store.config_path.read_text(encoding="utf-8"), original)

    def test_mode_and_position_persist_without_touching_key_file(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            key_path = store.key_path_for("openrouter")
            key_path.write_bytes(b"not-a-real-encrypted-key-for-test")
            settings = replace(Settings.for_provider("openrouter"), output_mode="raw", overlay_position="top-center", overlay_margin=40)
            store.save(settings)
            self.assertEqual(store.load(), settings)
            self.assertEqual(key_path.read_bytes(), b"not-a-real-encrypted-key-for-test")

    def test_invalid_mode_position_and_margin_rejected(self):
        for values in ({"output_mode": "other"}, {"overlay_position": "offscreen"}, {"overlay_margin": 7},
                       {"overlay_margin": 161}, {"overlay_margin": True}, {"overlay_margin": 24.5}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(Settings(), **values).validate()


class RawModelCheckTests(unittest.TestCase):
    def test_direct_openai_raw_check_does_not_require_refinement_model(self):
        service = OpenAIService("synthetic-not-real", replace(Settings(), output_mode="raw"), opener=Mock())
        service._models = Mock(return_value={service.settings.transcription_model: {}})
        self.assertIn("Raw mode skips", service.check_models())
        service._models.assert_called_once_with()

    def test_openrouter_raw_check_authenticates_and_skips_text_catalog(self):
        settings = replace(Settings.for_provider("openrouter"), output_mode="raw")
        service = OpenRouterService("synthetic-not-real", settings, opener=Mock())
        service._request = Mock(return_value={"data": {}})
        service._models = Mock(return_value={settings.transcription_model: {"architecture": {
            "input_modalities": ["audio"], "output_modalities": ["transcription"]}}})
        self.assertIn("Raw mode skips", service.check_models())
        service._request.assert_called_once_with("/key")
        service._models.assert_called_once_with("/models?output_modalities=transcription")


if __name__ == "__main__":
    unittest.main()
