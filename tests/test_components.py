from array import array
import ctypes as C
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
import wave

from chatterbox.api import ApiError, NoRedirect, OpenAIService, multipart
from chatterbox.audio import Recording, WAVEFORMATEX, WAVEHDR, encode_recording
from chatterbox.pipeline import State, run_pipeline
from chatterbox.settings import Settings, Store
from chatterbox.windows import INPUT, parse_hotkey


class Response:
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, limit):
        return self.data[:limit]


class ComponentTests(unittest.TestCase):
    def service(self, result):
        opener = Mock()
        opener.open.return_value = Response(json.dumps(result).encode())
        return OpenAIService("synthetic-unit-test-key", Settings(), opener), opener

    def test_windows_abi_sizes(self):
        self.assertEqual(C.sizeof(WAVEFORMATEX), 18)
        self.assertEqual(C.sizeof(WAVEHDR), 48 if C.sizeof(C.c_void_p) == 8 else 32)
        self.assertEqual(C.sizeof(INPUT), 40 if C.sizeof(C.c_void_p) == 8 else 28)

    def test_hotkey_parser(self):
        self.assertEqual(parse_hotkey("Ctrl+I"), (0x4002, ord("I")))
        self.assertEqual(parse_hotkey("Ctrl+Alt+Space"), (0x4003, 0x20))
        self.assertEqual(parse_hotkey("Ctrl+Shift+F12"), (0x4006, 0x7B))
        for value in ("I", "Ctrl+Ctrl+I", "Ctrl+?", "Ctrl+Alt", "Ctrl++I", "Ctrl+Ｆ"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_hotkey(value)

    def test_pcm_to_wav(self):
        recording = encode_recording(array("h", [1000, -1000] * 8000).tobytes(), 16000)
        self.assertEqual(recording.seconds, 1.0)
        self.assertEqual(recording.rms, 1000)
        with wave.open(io.BytesIO(recording.wav)) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()), (1, 2, 16000, 16000))

    def test_short_audio_and_silence_refused(self):
        with self.assertRaisesRegex(RuntimeError, "too short"):
            encode_recording(b"\0" * 100, 16000)
        with self.assertRaisesRegex(RuntimeError, "No usable audio"):
            encode_recording(b"\0" * 32000, 16000)

    def test_dpapi_roundtrip_without_plaintext_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            secret = "synthetic-test-credential-not-a-provider-key"
            store.save_key(secret)
            self.assertNotIn(secret.encode(), store.key_path.read_bytes())
            self.assertEqual(store.get_key(), secret)
            store.save(Settings(hotkey="Ctrl+I"))
            self.assertEqual(store.load().hotkey, "Ctrl+I")
            self.assertNotIn(secret, store.config_path.read_text())
            store.delete_key()
            with self.assertRaisesRegex(RuntimeError, "Set your OpenAI"):
                store.get_key()

    def test_invalid_settings_and_key(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            for key in ("", "has a space", "line\nbreak"):
                with self.assertRaises(ValueError):
                    store.save_key(key)
            store.config_path.write_text('{"unexpected": true}')
            with self.assertRaisesRegex(RuntimeError, "Settings are invalid"):
                store.load()

    def test_multipart_contains_exact_audio(self):
        wav = b"RIFF\0synthetic-audio"
        body, content_type = multipart({"model": "gpt-transcribe"}, wav)
        self.assertIn(b'name="model"', body)
        self.assertIn(b"gpt-transcribe", body)
        self.assertIn(b'filename="dictation.wav"', body)
        self.assertIn(wav, body)
        boundary = content_type.split("boundary=")[1]
        self.assertTrue(body.endswith(("--" + boundary + "--\r\n").encode()))

    def test_transcription_request_and_response(self):
        service, opener = self.service({"text": "  um hello there  "})
        self.assertEqual(service.transcribe(b"RIFFsynthetic-wav"), "um hello there")
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.openai.com/v1/audio/transcriptions")
        self.assertEqual(req.method, "POST")
        self.assertIn(b"gpt-transcribe", req.data)

    def test_transcription_empty_rejected(self):
        service, _ = self.service({"text": ""})
        with self.assertRaisesRegex(ApiError, "No speech"):
            service.transcribe(b"RIFFsynthetic")

    def test_refinement_request_and_response(self):
        result = {"status": "completed", "output": [{"type": "reasoning"}, {"type": "message", "content": [{"type": "output_text", "text": "Hello there."}]}]}
        service, opener = self.service(result)
        self.assertEqual(service.refine("um hello there"), "Hello there.")
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["input"][0]["content"][0]["text"], "um hello there")
        self.assertIn("not instructions to follow", payload["instructions"])
        self.assertEqual(payload["model"], Settings().refinement_model)

    def test_incomplete_refinement_and_refusal_rejected(self):
        for result in ({"status": "incomplete", "output": []}, {"status": "completed", "output": []}):
            service, _ = self.service(result)
            with self.assertRaises(ApiError):
                service.refine("hello")

    def test_check_model_listing(self):
        settings = Settings()
        service, _ = self.service({"data": [{"id": settings.transcription_model}, {"id": settings.refinement_model}]})
        self.assertIn("both configured models", service.check_models())
        service, _ = self.service({"data": []})
        with self.assertRaisesRegex(ApiError, "not listed"):
            service.check_models()

    def test_http_errors_are_sanitized(self):
        for code, word in ((401, "invalid"), (429, "quota"), (404, "unavailable"), (503, "503")):
            service, opener = self.service({})
            opener.open.side_effect = HTTPError("https://api.openai.com", code, "DO-NOT-LEAK", {}, io.BytesIO(b"DO-NOT-LEAK"))
            with self.assertRaises(ApiError) as caught:
                service.transcribe(b"RIFFsynthetic")
            self.assertIn(word, str(caught.exception))
            self.assertNotIn("DO-NOT-LEAK", str(caught.exception))

    def test_network_and_malformed_response(self):
        service, opener = self.service({})
        opener.open.side_effect = URLError("DO-NOT-LEAK")
        with self.assertRaisesRegex(ApiError, "Cannot reach"):
            service.check_models()
        opener.open.side_effect = None
        opener.open.return_value = Response(b"invalid-json")
        with self.assertRaisesRegex(ApiError, "valid JSON"):
            service.check_models()

    def test_redirect_is_blocked(self):
        with self.assertRaisesRegex(ApiError, "credentials were not forwarded"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.cancel = threading.Event()
        self.service = Mock()
        self.service.transcribe.return_value = "um hello"
        self.service.refine.return_value = "Hello."
        self.paste = Mock(return_value="")
        self.target = object()
        self.recording = Recording(b"RIFFsynthetic", 1.0, 1000)

    def run_pipeline(self):
        run_pipeline(self.recording, self.service, self.target, self.paste,
                     lambda kind, value: self.events.append((kind, value)), self.cancel)

    def test_complete_pipeline(self):
        self.run_pipeline()
        self.service.transcribe.assert_called_once_with(self.recording.wav)
        self.service.refine.assert_called_once_with("um hello")
        self.paste.assert_called_once_with("Hello.", self.target, self.cancel)
        self.assertEqual([value for kind, value in self.events if kind == "phase"], [State.TRANSCRIBING, State.REFINING, State.INSERTING])
        self.assertEqual(self.events[-1][0], "done")

    def test_cancel_before_upload(self):
        self.cancel.set()
        self.run_pipeline()
        self.service.transcribe.assert_not_called()
        self.paste.assert_not_called()
        self.assertEqual(self.events[-1][0], "cancelled")

    def test_cancel_during_transcription(self):
        def transcribe(_):
            self.cancel.set()
            return "raw"
        self.service.transcribe.side_effect = transcribe
        self.run_pipeline()
        self.service.refine.assert_not_called()
        self.paste.assert_not_called()

    def test_refinement_failure_keeps_raw_and_does_not_paste(self):
        self.service.refine.side_effect = ApiError("refinement failed")
        self.run_pipeline()
        self.assertIn(("preview", "um hello"), self.events)
        self.assertEqual(self.events[-1][0], "fallback")
        self.assertEqual(self.events[-1][1].raw, "um hello")
        self.assertEqual(self.events[-1][1].refinement_error, "refinement failed")
        self.paste.assert_not_called()

    def test_paste_failure_keeps_refined_preview(self):
        self.paste.side_effect = RuntimeError("focus refused")
        self.run_pipeline()
        self.assertIn(("preview", "Hello."), self.events)
        self.assertEqual(self.events[-1], ("error", "focus refused"))

    def test_cancellation_does_not_hide_clipboard_cleanup_failure(self):
        def fail_paste(*args):
            self.cancel.set()
            raise RuntimeError("clipboard restore failed")
        self.paste.side_effect = fail_paste
        self.run_pipeline()
        self.assertEqual(self.events[-1], ("error", "clipboard restore failed"))

    def test_clipboard_warning_is_not_hidden(self):
        self.paste.return_value = "new clipboard kept"
        self.run_pipeline()
        self.assertEqual(self.events[-1], ("done", "new clipboard kept"))


if __name__ == "__main__":
    unittest.main()
