"""No network, real models, optional compiled imports, microphone, or credentials.

Fixtures are deliberately tiny fake model files, not inference-capable weights.
All optional imports are mocked, including tests of unavailable dependencies.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import ssl
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch
import wave

from chatterbox import local_stt as stt


def wav_bytes() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 160)
    return stream.getvalue()


def put_model(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.bin").write_bytes(b"fake weight fixture, not a model")
    (directory / "config.json").write_text(json.dumps({"model_type": "Whisper"}), encoding="utf-8")
    (directory / "tokenizer.json").write_text(json.dumps({"version": "1.0"}), encoding="utf-8")


class LocalSttTests(unittest.TestCase):
    def setUp(self):
        # Transient synthetic fixtures remain inside the authorized workspace.
        self.temp = tempfile.TemporaryDirectory(prefix="local-stt-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / "base"
        self.imports = self.enterContext(patch.object(stt.importlib, "import_module"))
        self.imports.side_effect = AssertionError("Unmocked optional module import forbidden")
        self.audio = wav_bytes()

    def whisper(self, texts=(" Hello", " world.")):
        put_model(self.folder)
        model = Mock()
        model.transcribe.side_effect = lambda *_args, **_kwargs: (
            iter(types.SimpleNamespace(text=text) for text in texts), object()
        )
        factory = Mock(return_value=model)
        self.imports.side_effect = None
        self.imports.return_value = types.SimpleNamespace(WhisperModel=factory)
        return factory, model

    def hub(self, side_effect=None):
        def successful_download(**kwargs):
            put_model(Path(kwargs["local_dir"]))
            (Path(kwargs["local_dir"]) / "preprocessor_config.json").write_text("{}", encoding="utf-8")
            (Path(kwargs["local_dir"]) / "vocabulary.json").write_text("{}", encoding="utf-8")
            return "ignored backend path; use the checked local_dir"
        snapshot = Mock(side_effect=side_effect or successful_download)
        self.imports.side_effect = None
        self.imports.return_value = types.SimpleNamespace(snapshot_download=snapshot)
        return snapshot

    def test_module_import_does_not_import_optional_dependencies(self):
        self.imports.side_effect = AssertionError("Startup must not import optional packages")
        spec = importlib.util.spec_from_file_location("isolated_local_stt", stt.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.imports.assert_not_called()
        self.assertTrue(callable(module.LocalTranscriber))

    def test_constructor_is_lazy_and_does_not_create_directory(self):
        provider = stt.LocalTranscriber("base", self.root)
        self.assertEqual(provider.model_path, self.folder)
        self.assertFalse(self.folder.exists())
        self.imports.assert_not_called()

    def test_allowlisted_model_paths(self):
        for name in ("tiny", "base", "small"):
            with self.subTest(name=name):
                self.assertEqual(stt.LocalTranscriber(name, self.root).model_path, self.root / name)
        self.imports.assert_not_called()

    def test_model_names_reject_traversal_absolute_paths_and_repo_ids(self):
        for name in ("../base", "..", "base/../../other", "base\\other", "/base", "C:\\base", "BASE", "large", "Systran/faster-whisper-base", "", None, [], 5):
            with self.subTest(name=name):
                with self.assertRaisesRegex(stt.LocalTranscriptionError, "tiny, base, or small"):
                    stt.LocalTranscriber(name, self.root)
        self.imports.assert_not_called()

    def test_invalid_models_directory(self):
        for directory in (None, 42, "invalid\0path"):
            with self.subTest(directory=directory):
                with self.assertRaisesRegex(stt.LocalTranscriptionError, "path"):
                    stt.LocalTranscriber("base", directory)

    def test_directory_redirect_rejected_without_reading_external_files(self):
        original = Path.resolve
        redirected = self.root / "elsewhere"
        def resolve(path, *args, **kwargs):
            return redirected if path == self.folder else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            with self.assertRaisesRegex(stt.LocalTranscriptionError, "symbolic link or junction"):
                stt.LocalTranscriber("base", self.root)

    def test_check_available_imports_but_does_not_load_model(self):
        factory, _ = self.whisper()
        progress = Mock()
        message = stt.LocalTranscriber("base", self.root, progress).check_available()
        self.assertIn("Inference has not been tested", message)
        self.imports.assert_called_once_with("faster_whisper")
        factory.assert_not_called()
        self.assertTrue(progress.call_args.args[0].startswith("Ready:"))

    def test_unavailable_or_incompatible_dependencies_have_helpful_errors(self):
        for error in (ImportError("secret"), OSError("secret native library body"), RuntimeError("secret")):
            with self.subTest(error=type(error)):
                self.imports.side_effect = error
                progress = Mock()
                with self.assertRaisesRegex(stt.LocalTranscriptionError, "CTranslate2/PyAV") as result:
                    stt.LocalTranscriber("base", self.root, progress).check_available()
                self.assertNotIn("secret", str(result.exception))
                self.assertIn("Nothing was installed", str(result.exception))
                self.assertTrue(progress.call_args.args[0].startswith("Failed:"))

    def test_invalid_optional_package_api(self):
        self.imports.side_effect = None
        for module in (object(), types.SimpleNamespace(WhisperModel=None)):
            self.imports.return_value = module
            with self.assertRaisesRegex(stt.LocalTranscriptionError, "missing or incompatible"):
                stt.LocalTranscriber("base", self.root).check_available()

    def test_missing_model_never_loads_or_downloads(self):
        factory, _ = self.whisper()
        for file in self.folder.iterdir():
            file.unlink()
        self.folder.rmdir()
        provider = stt.LocalTranscriber("base", self.root)
        with self.assertRaisesRegex(stt.LocalTranscriptionError, "files are absent"):
            provider.transcribe(self.audio)
        factory.assert_not_called()
        self.assertFalse(self.folder.exists())
        self.assertEqual([call.args[0] for call in self.imports.call_args_list], ["faster_whisper"])

    def test_all_required_files_must_exist_and_be_nonempty(self):
        factory, _ = self.whisper()
        for name in ("model.bin", "config.json", "tokenizer.json"):
            for state in ("missing", "empty"):
                with self.subTest(name=name, state=state):
                    put_model(self.folder)
                    file = self.folder / name
                    file.unlink() if state == "missing" else file.write_bytes(b"")
                    with self.assertRaisesRegex(stt.LocalTranscriptionError, name):
                        stt.LocalTranscriber("base", self.root).check_available()
                    self.assertEqual(file.exists(), state == "empty")
        factory.assert_not_called()

    def test_invalid_json_and_nonobject_json_are_incomplete(self):
        self.whisper()
        for name in ("config.json", "tokenizer.json"):
            for payload in (b"{not json", b"null", b"[]", b"\xff"):
                with self.subTest(name=name, payload=payload):
                    put_model(self.folder)
                    (self.folder / name).write_bytes(payload)
                    with self.assertRaisesRegex(stt.LocalTranscriptionError, "unreadable or incomplete"):
                        stt.LocalTranscriber("base", self.root).check_available()
                    self.assertEqual((self.folder / name).read_bytes(), payload)

    def test_incomplete_markers_are_reported_and_never_deleted_by_check(self):
        self.whisper()
        for name in (stt._INCOMPLETE_MARKER, "model.bin.incomplete"):
            with self.subTest(name=name):
                marker = self.folder / name
                marker.write_text("keep", encoding="utf-8")
                with self.assertRaisesRegex(stt.LocalTranscriptionError, "incomplete|unfinished"):
                    stt.LocalTranscriber("base", self.root).check_available()
                self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
                marker.unlink()

    def test_required_file_redirect_is_rejected(self):
        self.whisper()
        original = Path.resolve
        file = self.folder / "model.bin"
        def resolve(path, *args, **kwargs):
            return self.root / "elsewhere.bin" if path == file else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            with self.assertRaisesRegex(stt.LocalTranscriptionError, "linked elsewhere"):
                stt.LocalTranscriber("base", self.root).check_available()

    def test_directory_redirect_after_construction_is_rejected(self):
        factory, _ = self.whisper()
        provider = stt.LocalTranscriber("base", self.root)
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            return self.root / "elsewhere" if path == self.folder else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            with self.assertRaisesRegex(stt.LocalTranscriptionError, "changed or points outside"):
                provider.transcribe(self.audio)
        factory.assert_not_called()

    def test_local_only_cpu_int8_load_and_in_memory_closed_audio(self):
        factory, model = self.whisper()
        progress = Mock()
        with patch.object(stt.os, "cpu_count", return_value=64):
            text = stt.LocalTranscriber("base", self.root, progress).transcribe(self.audio)
        self.assertEqual(text, "Hello world.")
        factory.assert_called_once_with(str(self.folder), device="cpu", compute_type="int8", local_files_only=True, cpu_threads=4, num_workers=1)
        model.transcribe.assert_called_once()
        stream = model.transcribe.call_args.args[0]
        self.assertIsInstance(stream, io.BytesIO)
        self.assertTrue(stream.closed)
        messages = [call.args[0] for call in progress.call_args_list]
        self.assertTrue(messages[0].startswith("Loading"))
        self.assertTrue(messages[1].startswith("Transcribing"))
        self.assertTrue(messages[-1].startswith("Ready:"))
        self.assertEqual([call.args[0] for call in self.imports.call_args_list], ["faster_whisper"])

    def test_cpu_thread_count_is_modest_even_when_unknown(self):
        factory, _ = self.whisper()
        for count, expected in ((None, 1), (0, 1), (1, 1), (2, 2), (200, 4)):
            with self.subTest(count=count), patch.object(stt.os, "cpu_count", return_value=count):
                stt.LocalTranscriber("base", self.root).transcribe(self.audio)
                self.assertEqual(factory.call_args.kwargs["cpu_threads"], expected)

    def test_invalid_wav_never_imports_or_loads(self):
        for value in (None, "RIFF", b"", b"not wav at all", b"RIFF0000NOTWAVE", bytearray(self.audio)):
            with self.subTest(value=type(value)):
                with self.assertRaisesRegex(stt.LocalTranscriptionError, "WAV recording as bytes"):
                    stt.LocalTranscriber("base", self.root).transcribe(value)
        self.imports.assert_not_called()

    def test_segments_are_fully_consumed_before_audio_closes(self):
        _, model = self.whisper()
        consumed = []
        def transcribe(audio, **_kwargs):
            self.assertEqual(audio.read(), self.audio)
            def segments():
                for text in (" Keep", " my wording", ", not a rewrite."):
                    self.assertFalse(audio.closed)
                    consumed.append(text)
                    yield types.SimpleNamespace(text=text)
            return segments(), object()
        model.transcribe.side_effect = transcribe
        self.assertEqual(stt.LocalTranscriber("base", self.root).transcribe(self.audio), "Keep my wording, not a rewrite.")
        self.assertEqual(len(consumed), 3)

    def test_wording_and_natural_fragment_boundaries(self):
        cases = [
            (("  I do NOT", " want 2.5", " milligrams.  "), "I do NOT want 2.5 milligrams."),
            (("Hello", "world", ",", "again", "."), "Hello world, again."),
            (("你好", "世界", "。"), "你好世界。"),
            (("(Hello", ")", "world"), "(Hello) world"),
            (("Keep\n", "the   spaces", "!"), "Keep\nthe   spaces!"),
        ]
        for fragments, expected in cases:
            with self.subTest(fragments=fragments):
                self.whisper(fragments)
                self.assertEqual(stt.LocalTranscriber("base", self.root).transcribe(self.audio), expected)

    def test_no_speech_errors(self):
        for texts in ((), ("",), (" ", "\n\t")):
            with self.subTest(texts=texts):
                self.whisper(texts)
                with self.assertRaisesRegex(stt.LocalTranscriptionError, "No speech was transcribed"):
                    stt.LocalTranscriber("base", self.root).transcribe(self.audio)

    def test_invalid_segment_outputs(self):
        for text in (None, 123, {}, "null\0character", "unsafe\x01control", "escape\x1b", "delete\x7f"):
            with self.subTest(text=text):
                self.whisper((text,))
                with self.assertRaisesRegex(stt.LocalTranscriptionError, "invalid transcription text"):
                    stt.LocalTranscriber("base", self.root).transcribe(self.audio)

    def test_invalid_transcribe_result_is_safe(self):
        for result in (None, (), (None, object()), (42, object())):
            with self.subTest(result=result):
                _, model = self.whisper()
                model.transcribe.side_effect = None
                model.transcribe.return_value = result
                with self.assertRaisesRegex(stt.LocalTranscriptionError, "Local transcription failed"):
                    stt.LocalTranscriber("base", self.root).transcribe(self.audio)
                self.assertTrue(model.transcribe.call_args.args[0].closed)

    def test_model_reuse_then_release_and_lazy_reload(self):
        factory, model = self.whisper()
        provider = stt.LocalTranscriber("base", self.root)
        provider.release()
        factory.assert_not_called()
        self.assertEqual(provider.transcribe(self.audio), "Hello world.")
        self.assertEqual(provider.transcribe(self.audio), "Hello world.")
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(model.transcribe.call_count, 2)
        provider.release()
        self.assertEqual(factory.call_count, 1)
        provider.transcribe(self.audio)
        self.assertEqual(factory.call_count, 2)

    def test_loading_error_is_sanitized_and_can_be_retried(self):
        factory, model = self.whisper()
        factory.side_effect = [OSError("raw secret backend details"), model]
        progress = Mock()
        provider = stt.LocalTranscriber("base", self.root, progress)
        with self.assertRaisesRegex(stt.LocalTranscriptionError, "could not be loaded") as result:
            provider.transcribe(self.audio)
        self.assertNotIn("secret", str(result.exception))
        self.assertTrue(progress.call_args.args[0].startswith("Failed:"))
        self.assertEqual(provider.transcribe(self.audio), "Hello world.")
        self.assertEqual(factory.call_count, 2)

    def test_none_loaded_model_is_rejected(self):
        factory, _ = self.whisper()
        factory.return_value = None
        with self.assertRaisesRegex(stt.LocalTranscriptionError, "could not be loaded"):
            stt.LocalTranscriber("base", self.root).transcribe(self.audio)

    def test_inference_error_closes_generator_stream_and_keeps_reusable_model(self):
        factory, model = self.whisper()
        closed = []
        def fail(audio, **_kwargs):
            def segments():
                try:
                    yield types.SimpleNamespace(text="first")
                    raise RuntimeError("secret transcript or backend body")
                finally:
                    closed.append(not audio.closed)
            return segments(), object()
        model.transcribe.side_effect = fail
        provider = stt.LocalTranscriber("base", self.root)
        with self.assertRaisesRegex(stt.LocalTranscriptionError, "Local transcription failed") as result:
            provider.transcribe(self.audio)
        self.assertNotIn("secret", str(result.exception))
        self.assertEqual(closed, [True])
        self.assertTrue(model.transcribe.call_args.args[0].closed)
        model.transcribe.side_effect = lambda *_args, **_kwargs: (iter([types.SimpleNamespace(text="retry")]), None)
        self.assertEqual(provider.transcribe(self.audio), "retry")
        factory.assert_called_once()

    def test_invalid_segment_aborts_and_closes_generator(self):
        _, model = self.whisper()
        closed = []
        def segments():
            try:
                yield types.SimpleNamespace(text=None)
                self.fail("Must not continue after an invalid segment")
            finally:
                closed.append(True)
        model.transcribe.side_effect = None
        model.transcribe.return_value = segments(), None
        with self.assertRaises(stt.LocalTranscriptionError):
            stt.LocalTranscriber("base", self.root).transcribe(self.audio)
        self.assertEqual(closed, [True])
        self.assertTrue(model.transcribe.call_args.args[0].closed)

    def test_progress_callback_exceptions_do_not_break_transcription_or_release(self):
        factory, _ = self.whisper()
        callback = Mock(side_effect=RuntimeError("broken UI"))
        provider = stt.LocalTranscriber("base", self.root, callback)
        provider.check_available()
        self.assertEqual(provider.transcribe(self.audio), "Hello world.")
        provider.release()
        self.assertGreaterEqual(callback.call_count, 4)
        self.assertEqual(factory.call_count, 1)

    def test_concurrent_loading_and_inference_are_serialized(self):
        factory, model = self.whisper()
        first_inference = threading.Event()
        allow_first = threading.Event()
        second_started = threading.Event()
        active_lock = threading.Lock()
        active = [0]
        maximum = [0]
        calls = [0]
        errors = []
        results = []
        def transcribe(audio, **_kwargs):
            def segments():
                with active_lock:
                    active[0] += 1
                    maximum[0] = max(maximum[0], active[0])
                    calls[0] += 1
                    number = calls[0]
                try:
                    if number == 1:
                        first_inference.set()
                        if not allow_first.wait(3):
                            raise RuntimeError("test gate timed out")
                    yield types.SimpleNamespace(text="words")
                finally:
                    with active_lock:
                        active[0] -= 1
            return segments(), None
        model.transcribe.side_effect = transcribe
        provider = stt.LocalTranscriber("base", self.root)
        def run(second=False):
            try:
                if second:
                    second_started.set()
                results.append(provider.transcribe(self.audio))
            except Exception as exc:
                errors.append(exc)
        first = threading.Thread(target=run)
        second = threading.Thread(target=run, args=(True,))
        try:
            first.start()
            self.assertTrue(first_inference.wait(3))
            second.start()
            self.assertTrue(second_started.wait(3))
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(model.transcribe.call_count, 1)
        finally:
            allow_first.set()
            first.join(3)
            if second.ident is not None:
                second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, ["words", "words"])
        self.assertEqual(maximum[0], 1)
        self.assertEqual(factory.call_count, 1)

    def test_transcribe_file_returns_timestamped_segments(self):
        factory, model = self.whisper(texts=())
        segments = [types.SimpleNamespace(text=" Hello", start=0.0, end=1.2),
                    types.SimpleNamespace(text=" world.", start=1.2, end=2.5)]
        model.transcribe.side_effect = lambda *_a, **_k: (iter(segments), object())
        target = self.root / "clip.mp3"
        target.write_bytes(b"not real audio, just needs to exist")
        transcriber = stt.LocalTranscriber("base", self.root)
        result = transcriber.transcribe_file(str(target))
        self.assertEqual(result, [{"start": 0.0, "end": 1.2, "text": "Hello"},
                                   {"start": 1.2, "end": 2.5, "text": "world."}])
        model.transcribe.assert_called_once_with(str(target), beam_size=5, language="en")

    def test_transcribe_file_rejects_missing_file(self):
        self.whisper(texts=())
        transcriber = stt.LocalTranscriber("base", self.root)
        with self.assertRaisesRegex(stt.LocalTranscriptionError, "existing"):
            transcriber.transcribe_file(str(self.root / "missing.mp4"))

    def test_transcribe_file_honors_cancellation_mid_stream(self):
        factory, model = self.whisper(texts=())
        segments = [types.SimpleNamespace(text=" one", start=0.0, end=1.0),
                    types.SimpleNamespace(text=" two", start=1.0, end=2.0)]
        model.transcribe.side_effect = lambda *_a, **_k: (iter(segments), object())
        target = self.root / "clip.mp4"
        target.write_bytes(b"fixture")
        cancel = threading.Event()
        cancel.set()
        transcriber = stt.LocalTranscriber("base", self.root)
        with self.assertRaises(stt.LocalTranscriptionCancelled):
            transcriber.transcribe_file(str(target), cancel=cancel)

    def test_transcribe_file_reuses_already_loaded_model(self):
        factory, model = self.whisper(texts=())
        model.transcribe.side_effect = lambda *_a, **_k: (iter([types.SimpleNamespace(text="hi", start=0.0, end=0.5)]), object())
        target = self.root / "clip.wav"
        target.write_bytes(b"fixture")
        transcriber = stt.LocalTranscriber("base", self.root)
        transcriber.transcribe_file(str(target))
        transcriber.transcribe_file(str(target))
        factory.assert_called_once()

    def test_release_waits_for_active_inference(self):
        _, model = self.whisper()
        entered = threading.Event()
        finish = threading.Event()
        released = threading.Event()
        errors = []
        def transcribe(_audio, **_kwargs):
            def segments():
                entered.set()
                if not finish.wait(3):
                    raise RuntimeError("test gate timeout")
                yield types.SimpleNamespace(text="done")
            return segments(), None
        model.transcribe.side_effect = transcribe
        provider = stt.LocalTranscriber("base", self.root)
        def run():
            try:
                provider.transcribe(self.audio)
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        releaser = threading.Thread(target=lambda: (provider.release(), released.set()))
        try:
            worker.start()
            self.assertTrue(entered.wait(3))
            releaser.start()
            self.assertFalse(released.wait(0.05))
        finally:
            finish.set()
            worker.join(3)
            if releaser.ident is not None:
                releaser.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(released.is_set())
        self.assertIsNone(provider._model)

    def test_is_downloaded_never_imports_optional_packages(self):
        self.assertFalse(stt.is_downloaded("base", self.root))
        put_model(self.folder)
        self.assertTrue(stt.is_downloaded("base", self.root))
        self.imports.assert_not_called()

    def test_is_downloaded_false_for_incomplete_or_marked_download(self):
        put_model(self.folder)
        (self.folder / "model.bin").unlink()
        self.assertFalse(stt.is_downloaded("base", self.root))
        put_model(self.folder)
        (self.folder / stt._INCOMPLETE_MARKER).write_text("x", encoding="utf-8")
        self.assertFalse(stt.is_downloaded("base", self.root))

    def test_explicit_download_has_fixed_public_repo_local_dir_and_needed_files(self):
        snapshot = self.hub()
        progress = Mock()
        for size in ("tiny", "base", "small"):
            with self.subTest(size=size):
                result = stt.download_model(size, self.root, progress)
                self.assertEqual(result, self.root / size)
                kwargs = snapshot.call_args.kwargs
                self.assertEqual(kwargs["repo_id"], f"Systran/faster-whisper-{size}")
                self.assertEqual(kwargs["local_dir"], str(result))
                self.assertIs(kwargs["token"], False)
                self.assertEqual(kwargs["max_workers"], 2)
                for filename in ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json", "vocabulary.*"):
                    self.assertIn(filename, kwargs["allow_patterns"])
                self.assertFalse((result / stt._INCOMPLETE_MARKER).exists())
        self.assertEqual(snapshot.call_count, 3)
        messages = [call.args[0] for call in progress.call_args_list]
        self.assertTrue(any("no per-byte progress" in text for text in messages))
        self.assertTrue(messages[-1].startswith("Ready:"))
        self.assertEqual([call.args[0] for call in self.imports.call_args_list], ["huggingface_hub"] * 3)

    def test_download_rejects_unallowlisted_repo_before_import_or_files(self):
        for size in ("large-v3", "attacker/repo", "../base", "base\\other", None):
            with self.subTest(size=size):
                with self.assertRaisesRegex(stt.LocalTranscriptionError, "supported local model"):
                    stt.download_model(size, self.root)
        self.imports.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_complete_existing_download_needs_no_network_or_optional_import(self):
        put_model(self.folder)
        original = (self.folder / "model.bin").read_bytes()
        self.assertEqual(stt.download_model("base", self.root), self.folder)
        self.imports.assert_not_called()
        self.assertEqual((self.folder / "model.bin").read_bytes(), original)

    def test_download_missing_hub_is_helpful_and_creates_no_directory(self):
        self.imports.side_effect = ImportError("secret")
        progress = Mock()
        with self.assertRaisesRegex(stt.LocalTranscriptionError, "huggingface-hub is missing") as result:
            stt.download_model("base", self.root, progress)
        self.assertNotIn("secret", str(result.exception))
        self.assertFalse(self.folder.exists())
        self.assertTrue(progress.call_args.args[0].startswith("Failed:"))

    def test_download_invalid_hub_api_is_helpful(self):
        self.imports.side_effect = None
        self.imports.return_value = types.SimpleNamespace(snapshot_download=None)
        with self.assertRaisesRegex(stt.LocalTranscriptionError, "huggingface-hub is missing"):
            stt.download_model("base", self.root)
        self.assertFalse(self.folder.exists())

    def test_download_failure_keeps_user_files_and_retry_marker(self):
        snapshot = self.hub(lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("raw secret url or token")))
        self.folder.mkdir()
        original = self.folder / "my-notes.txt"
        original.write_text("user file", encoding="utf-8")
        with self.assertRaisesRegex(stt.LocalTranscriptionError, "Existing files were kept") as result:
            stt.download_model("base", self.root)
        self.assertNotIn("secret", str(result.exception))
        self.assertEqual(original.read_text(encoding="utf-8"), "user file")
        self.assertTrue((self.folder / stt._INCOMPLETE_MARKER).exists())
        snapshot.assert_called_once()

    def test_download_does_not_accept_missing_or_corrupt_required_files(self):
        for kind in ("missing", "corrupt"):
            with self.subTest(kind=kind):
                target = self.root / ("tiny" if kind == "missing" else "small")
                def bad_download(**kwargs):
                    directory = Path(kwargs["local_dir"])
                    if kind == "corrupt":
                        put_model(directory)
                        (directory / "tokenizer.json").write_text("not-json", encoding="utf-8")
                self.hub(bad_download)
                with self.assertRaisesRegex(stt.LocalTranscriptionError, "incomplete"):
                    stt.download_model(target.name, self.root)
                self.assertTrue((target / stt._INCOMPLETE_MARKER).exists())

    def test_explicit_retry_completes_marked_partial_download_without_deleting_user_files(self):
        self.hub()
        self.folder.mkdir()
        (self.folder / stt._INCOMPLETE_MARKER).write_text("partial", encoding="utf-8")
        notes = self.folder / "notes.txt"
        notes.write_text("keep", encoding="utf-8")
        self.assertEqual(stt.download_model("base", self.root), self.folder)
        self.assertFalse((self.folder / stt._INCOMPLETE_MARKER).exists())
        self.assertEqual(notes.read_text(encoding="utf-8"), "keep")

    def test_certificate_errors_keep_verification_and_are_sanitized(self):
        nested = RuntimeError("download wrapper with sensitive body")
        nested.__cause__ = ssl.SSLCertVerificationError("secret certificate details")
        for error in (ssl.SSLCertVerificationError("secret"), RuntimeError("[SSL: CERTIFICATE_VERIFY_FAILED] secret"), nested):
            with self.subTest(error=type(error)):
                snapshot = self.hub(lambda **_kwargs: (_ for _ in ()).throw(error))
                with self.assertRaisesRegex(stt.LocalTranscriptionError, "Never disable TLS verification") as result:
                    stt.download_model("base", self.root)
                self.assertNotIn("secret", str(result.exception))
                self.assertIs(snapshot.call_args.kwargs["token"], False)
                self.assertNotIn("verify", snapshot.call_args.kwargs)

    def test_progress_callback_exceptions_do_not_break_download(self):
        snapshot = self.hub()
        callback = Mock(side_effect=ValueError("broken UI"))
        self.assertEqual(stt.download_model("base", self.root, callback), self.folder)
        snapshot.assert_called_once()
        self.assertFalse((self.folder / stt._INCOMPLETE_MARKER).exists())

    def test_cancel_before_download_has_no_import_or_files(self):
        cancel = threading.Event()
        cancel.set()
        progress = Mock()
        with self.assertRaises(stt.LocalTranscriptionCancelled):
            stt.download_model("base", self.root, progress, cancel)
        self.imports.assert_not_called()
        self.assertFalse(self.folder.exists())
        self.assertTrue(progress.call_args.args[0].startswith("Cancelled:"))

    def test_cancel_from_preparing_callback_prevents_import_and_download(self):
        cancel = threading.Event()
        def progress(text):
            if text.startswith("Preparing"):
                cancel.set()
        with self.assertRaises(stt.LocalTranscriptionCancelled):
            stt.download_model("base", self.root, progress, cancel)
        self.imports.assert_not_called()
        self.assertFalse(self.folder.exists())

    def test_cancel_after_backend_returns_retains_files_and_retry_marker(self):
        cancel = threading.Event()
        def downloaded(**kwargs):
            put_model(Path(kwargs["local_dir"]))
            cancel.set()
        snapshot = self.hub(downloaded)
        with self.assertRaisesRegex(stt.LocalTranscriptionCancelled, "cannot be interrupted per byte"):
            stt.download_model("base", self.root, cancel=cancel)
        snapshot.assert_called_once()
        self.assertTrue((self.folder / "model.bin").exists())
        self.assertTrue((self.folder / stt._INCOMPLETE_MARKER).exists())

    def test_cancel_before_backend_from_progress_keeps_marker_but_does_not_download(self):
        snapshot = self.hub()
        cancel = threading.Event()
        def progress(text):
            if text.startswith("Downloading"):
                cancel.set()
        with self.assertRaises(stt.LocalTranscriptionCancelled):
            stt.download_model("base", self.root, progress, cancel)
        snapshot.assert_not_called()
        self.assertTrue((self.folder / stt._INCOMPLETE_MARKER).exists())

    def test_cancel_while_waiting_for_another_download_releases_no_other_lock(self):
        cancel = threading.Event()
        errors = []
        preparing = threading.Event()
        def progress(text):
            if text.startswith("Preparing"):
                preparing.set()
        def run():
            try:
                stt.download_model("base", self.root, progress, cancel)
            except Exception as exc:
                errors.append(exc)
        stt._DOWNLOAD_LOCK.acquire()
        worker = threading.Thread(target=run)
        try:
            worker.start()
            self.assertTrue(preparing.wait(3))
            cancel.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertTrue(stt._DOWNLOAD_LOCK.locked())
        finally:
            stt._DOWNLOAD_LOCK.release()
            worker.join(3)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], stt.LocalTranscriptionCancelled)
        self.imports.assert_not_called()

    def test_download_rejects_preexisting_linked_weight_without_backend_writes(self):
        snapshot = self.hub()
        self.folder.mkdir()
        file = self.folder / "model.bin"
        file.write_bytes(b"keep")
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            return self.root / "elsewhere.bin" if path == file else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            with self.assertRaisesRegex(stt.LocalTranscriptionError, "linked file or subdirectory"):
                stt.download_model("base", self.root)
        snapshot.assert_not_called()
        self.assertEqual(file.read_bytes(), b"keep")

    def test_storage_permission_failure_is_safe_and_releases_download_lock(self):
        snapshot = self.hub()
        with patch.object(Path, "mkdir", side_effect=PermissionError("sensitive user path")):
            with self.assertRaisesRegex(stt.LocalTranscriptionError, "storage permissions") as result:
                stt.download_model("base", self.root)
        self.assertNotIn("sensitive", str(result.exception))
        snapshot.assert_not_called()
        self.assertFalse(stt._DOWNLOAD_LOCK.locked())
        self.assertFalse(self.folder.exists())

    def test_broken_failure_progress_does_not_replace_provider_error(self):
        callback = Mock(side_effect=RuntimeError("broken callback"))
        self.imports.side_effect = ImportError("missing optional package")
        with self.assertRaisesRegex(stt.LocalTranscriptionError, "Local transcription is unavailable"):
            stt.LocalTranscriber("base", self.root, callback).check_available()
        self.hub(lambda **_kwargs: (_ for _ in ()).throw(OSError("backend failure")))
        with self.assertRaisesRegex(stt.LocalTranscriptionError, "Model download failed"):
            stt.download_model("base", self.root, callback)
        self.assertFalse(stt._DOWNLOAD_LOCK.locked())
        self.assertTrue((self.folder / stt._INCOMPLETE_MARKER).exists())

    def test_download_locks_released_after_failure_and_cancellation(self):
        self.hub(lambda **_kwargs: (_ for _ in ()).throw(OSError("failed")))
        with self.assertRaises(stt.LocalTranscriptionError):
            stt.download_model("base", self.root)
        self.assertFalse(stt._DOWNLOAD_LOCK.locked())
        self.hub()
        self.assertEqual(stt.download_model("base", self.root), self.folder)
        self.assertFalse(stt._DOWNLOAD_LOCK.locked())


if __name__ == "__main__":
    unittest.main()
