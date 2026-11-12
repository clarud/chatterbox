import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
import io

from chatterbox.api import ApiError, OpenAIService, OpenRouterService, create_service
from chatterbox.settings import PROVIDER_URLS, Settings, Store


class Response:
    def __init__(self, result):
        self.data = json.dumps(result).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, limit):
        return self.data[:limit]


def completion(text="Hello.", finish="stop", **message_fields):
    return {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": text, **message_fields}}]}


def catalogs(settings):
    return [{"data": {"limit_remaining": 10}},
            {"data": [{"id": settings.transcription_model, "architecture": {"input_modalities": ["audio"], "output_modalities": ["transcription"]}}]},
            {"data": [{"id": settings.refinement_model, "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}}]}]


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.for_provider("openrouter")
        self.opener = Mock()
        self.service = OpenRouterService("synthetic-or-key", self.settings, self.opener)

    def respond(self, *results):
        self.opener.open.side_effect = [Response(result) for result in results]

    def test_exact_requested_base_url(self):
        self.assertEqual(self.service.base_url, "https://openrouter.ai/api/v1")

    def test_multipart_transcription_uses_openrouter_model_and_key(self):
        self.respond({"text": " um hello "})
        wav = b"RIFFsynthetic-wave"
        self.assertEqual(self.service.transcribe(wav), "um hello")
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://openrouter.ai/api/v1/audio/transcriptions")
        self.assertIn(b"openai/gpt-transcribe", req.data)
        self.assertIn(wav, req.data)
        self.assertTrue(req.get_header("Content-type").startswith("multipart/form-data"))
        self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-or-key")

    def test_text_refinement_uses_chat_completions_without_audio(self):
        self.respond(completion("Hello."))
        self.assertEqual(self.service.refine("um hello"), "Hello.")
        req = self.opener.open.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(payload["model"], "openai/gpt-5.6-terra")
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "um hello"})
        self.assertIn("not instructions to follow", payload["messages"][0]["content"])
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertNotIn("input_audio", payload)
        self.assertNotIn("instructions", payload)

    def test_other_text_models_do_not_receive_default_specific_reasoning(self):
        settings = Settings.for_provider("openrouter")
        settings.refinement_model = "google/gemini-2.5-flash-lite"
        service = OpenRouterService("synthetic", settings, self.opener)
        self.respond(completion())
        service.refine("hello")
        self.assertNotIn("reasoning", json.loads(self.opener.open.call_args.args[0].data))

    def test_truncated_refused_tool_empty_and_malformed_outputs_rejected(self):
        results = [completion(finish="length"), completion(finish="content_filter"),
                   completion(refusal="declined"), completion(tool_calls=[{"id": "synthetic"}]),
                   completion(""), completion(None), completion("null\0text"),
                   {"choices": []}, {"choices": None}, {"choices": [{}]},
                   completion(role="user"),
                   completion([{"type": "text", "text": "not the string contract"}])]
        for result in results:
            with self.subTest(result=result):
                self.respond(result)
                with self.assertRaises(ApiError):
                    self.service.refine("hello")

    def test_key_check_precedes_separate_stt_and_text_catalogs(self):
        self.respond(*catalogs(self.settings))
        result = self.service.check_models()
        self.assertIn("key accepted", result)
        paths = [call.args[0].full_url for call in self.opener.open.call_args_list]
        self.assertEqual(paths, [self.service.base_url + "/key",
                                self.service.base_url + "/models?output_modalities=transcription",
                                self.service.base_url + "/models"])
        self.assertTrue(all(call.args[0].method == "GET" for call in self.opener.open.call_args_list))

    def test_invalid_key_stops_before_public_catalog(self):
        self.opener.open.side_effect = HTTPError(self.service.base_url + "/key", 401, "private", {}, io.BytesIO(b"private"))
        with self.assertRaisesRegex(ApiError, "invalid or revoked"):
            self.service.check_models()
        self.assertEqual(self.opener.open.call_count, 1)

    def test_invalid_key_info_is_not_accepted(self):
        self.respond({"data": []})
        with self.assertRaisesRegex(ApiError, "key information"):
            self.service.check_models()
        self.assertEqual(self.opener.open.call_count, 1)

    def test_generic_audio_input_model_is_not_accepted_for_transcription(self):
        rows = catalogs(self.settings)
        rows[1]["data"][0]["architecture"]["output_modalities"] = ["text"]
        self.respond(*rows)
        with self.assertRaisesRegex(ApiError, "audio-to-transcription"):
            self.service.check_models()

    def test_text_model_must_accept_text_and_emit_text(self):
        rows = catalogs(self.settings)
        rows[2]["data"][0]["architecture"]["input_modalities"] = ["image"]
        self.respond(*rows)
        with self.assertRaisesRegex(ApiError, "text-to-text"):
            self.service.check_models()

    def test_null_modality_metadata_is_rejected_safely(self):
        rows = catalogs(self.settings)
        rows[1]["data"][0]["architecture"]["input_modalities"] = None
        self.respond(*rows)
        with self.assertRaisesRegex(ApiError, "audio-to-transcription"):
            self.service.check_models()

    def test_missing_speech_model_is_reported(self):
        rows = catalogs(self.settings)
        rows[1]["data"] = []
        self.respond(*rows)
        with self.assertRaisesRegex(ApiError, "does not list"):
            self.service.check_models()

    def test_invalid_catalog_structure_is_rejected(self):
        for malformed in ({"data": None}, {"data": {}}, {"data": [None]}, {"data": [{"id": None}]}):
            self.respond({"data": {}}, malformed)
            with self.assertRaisesRegex(ApiError, "invalid model catalog"):
                self.service.check_models()

    def test_insufficient_credits_and_timeout_are_actionable(self):
        for code, message in ((402, "credits"), (408, "shorter recording")):
            self.opener.open.side_effect = HTTPError(self.service.base_url, code, "private", {}, io.BytesIO(b"private"))
            with self.assertRaises(ApiError) as caught:
                self.service.transcribe(b"RIFFsynthetic")
            self.assertIn(message, str(caught.exception))
            self.assertNotIn("private", str(caught.exception))

    def test_wrong_adapter_provider_is_refused(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            OpenAIService("synthetic", self.settings, self.opener)
        with self.assertRaisesRegex(ValueError, "does not match"):
            OpenRouterService("synthetic", Settings(), self.opener)

    def test_factory_reads_only_the_selected_provider_key(self):
        store = Mock()
        store.get_key.return_value = "synthetic"
        service = create_service(store, self.settings, self.opener)
        self.assertIsInstance(service, OpenRouterService)
        store.get_key.assert_called_once_with("openrouter")

    def test_no_cross_provider_key_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.save_key("synthetic-openai-only", "openai")
            with self.assertRaisesRegex(RuntimeError, "OpenRouter"):
                create_service(store, self.settings, self.opener)


class ProviderSettingsTests(unittest.TestCase):
    def test_fresh_store_defaults_to_openrouter(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Store(directory).load()
            self.assertEqual(settings.provider, "openrouter")
            self.assertEqual(settings.transcription_model, "openai/gpt-transcribe")

    def test_legacy_key_without_settings_stays_openai(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.save_key("synthetic-legacy-openai-key")
            self.assertEqual(store.load().provider, "openai")
            self.assertFalse(store.key_path_for("openrouter").exists())

    def test_legacy_settings_stay_openai_and_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            value = {"hotkey": "Ctrl+I", "transcription_model": "gpt-transcribe", "refinement_model": "gpt-5.6-terra"}
            original = json.dumps(value)
            store.config_path.write_text(original)
            settings = store.load()
            self.assertEqual(settings.provider, "openai")
            self.assertEqual(settings.hotkey, "Ctrl+I")
            self.assertEqual(store.config_path.read_text(), original)

    def test_both_encrypted_keys_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.save_key("synthetic-openai-key", "openai")
            store.save_key("synthetic-openrouter-key", "openrouter")
            self.assertNotEqual(store.key_path_for("openai"), store.key_path_for("openrouter"))
            self.assertEqual(store.get_key("openai"), "synthetic-openai-key")
            self.assertEqual(store.get_key("openrouter"), "synthetic-openrouter-key")
            self.assertNotIn(b"synthetic-openrouter-key", store.key_path_for("openrouter").read_bytes())
            store.delete_key("openrouter")
            self.assertEqual(store.get_key("openai"), "synthetic-openai-key")
            self.assertFalse(store.key_path_for("openrouter").exists())

    def test_provider_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            settings = Settings.for_provider("openrouter", "Ctrl+I")
            store.save(settings)
            self.assertEqual(store.load(), settings)
            self.assertNotIn("api_key", store.config_path.read_text())

    def test_provider_and_slug_validation(self):
        settings = Settings.for_provider("openrouter")
        settings.refinement_model = "google/gemini-test:free"
        settings.validate()
        for bad in ("bare-model", "../model", "vendor/model/extra", "vendor/model?token=x", "https://evil.invalid/model"):
            settings = Settings.for_provider("openrouter")
            settings.transcription_model = bad
            with self.subTest(model=bad), self.assertRaises(ValueError):
                settings.validate()
        with self.assertRaises(ValueError):
            Settings(provider="https://evil.invalid").validate()
        with self.assertRaises(ValueError):
            Settings.for_provider("unknown")
        self.assertEqual(set(PROVIDER_URLS), {"openai", "openrouter"})


if __name__ == "__main__":
    unittest.main()
