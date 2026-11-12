"""No network, real weights, or optional compiled imports; all mocked."""
from __future__ import annotations

from pathlib import Path
import ssl
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from chatterbox import local_llm as llm


def put_model(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / llm._MODEL_FILE).write_bytes(b"0" * (llm._MIN_FILE_SIZE + 1))


class LocalLlmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-llm-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / "llm"
        self.imports = self.enterContext(patch.object(llm.importlib, "import_module"))
        self.imports.side_effect = AssertionError("Unmocked optional module import forbidden")

    def llama(self, content="Cleaned up text."):
        put_model(self.folder)
        model = Mock()
        model.create_chat_completion.return_value = {"choices": [{"message": {"content": content}}]}
        factory = Mock(return_value=model)
        self.imports.side_effect = None
        self.imports.return_value = types.SimpleNamespace(Llama=factory)
        return factory, model

    def hub(self, side_effect=None):
        def successful_download(**kwargs):
            put_model(Path(kwargs["local_dir"]))
            return "ignored backend path"
        download = Mock(side_effect=side_effect or successful_download)
        self.imports.side_effect = None
        self.imports.return_value = types.SimpleNamespace(hf_hub_download=download)
        return download

    def test_constructor_is_lazy_and_does_not_create_directory(self):
        refiner = llm.LocalRefiner(self.root)
        self.assertFalse(self.folder.exists())
        self.imports.assert_not_called()

    def test_check_available_imports_but_does_not_load_model(self):
        factory, _ = self.llama()
        progress = Mock()
        message = llm.LocalRefiner(self.root, progress).check_available()
        self.assertIn("Inference has not been tested", message)
        factory.assert_not_called()
        self.assertTrue(progress.call_args.args[0].startswith("Ready:"))

    def test_unavailable_dependency_is_sanitized(self):
        self.imports.side_effect = ImportError("secret backend detail")
        with self.assertRaisesRegex(llm.LocalRefineError, "llama-cpp-python") as result:
            llm.LocalRefiner(self.root).check_available()
        self.assertNotIn("secret", str(result.exception))

    def test_missing_model_never_loads(self):
        factory, _ = self.llama()
        for file in self.folder.iterdir():
            file.unlink()
        self.folder.rmdir()
        with self.assertRaisesRegex(llm.LocalRefineError, "absent"):
            llm.LocalRefiner(self.root).refine("hello")
        factory.assert_not_called()

    def test_truncated_model_file_is_rejected(self):
        self.llama()
        (self.folder / llm._MODEL_FILE).write_bytes(b"too small")
        with self.assertRaisesRegex(llm.LocalRefineError, "missing, incomplete"):
            llm.LocalRefiner(self.root).check_available()

    def test_refine_loads_model_once_and_reuses_it(self):
        factory, model = self.llama()
        refiner = llm.LocalRefiner(self.root)
        self.assertEqual(refiner.refine("um hello"), "Cleaned up text.")
        self.assertEqual(refiner.refine("um hello again"), "Cleaned up text.")
        factory.assert_called_once()
        self.assertEqual(model.create_chat_completion.call_count, 2)
        messages = model.create_chat_completion.call_args.kwargs["messages"]
        self.assertEqual(messages[1], {"role": "user", "content": "um hello again"})

    def test_refine_rejects_empty_text(self):
        self.llama()
        with self.assertRaisesRegex(llm.LocalRefineError, "nonempty"):
            llm.LocalRefiner(self.root).refine("   ")

    def test_refine_rejects_invalid_completion_shapes(self):
        for result in (None, {}, {"choices": []}, {"choices": [{"message": {"content": ""}}]},
                       {"choices": [{"message": {"content": "bad\x01control"}}]}):
            with self.subTest(result=result):
                _, model = self.llama()
                model.create_chat_completion.return_value = result
                with self.assertRaisesRegex(llm.LocalRefineError, "no completion|invalid"):
                    llm.LocalRefiner(self.root).refine("hello")

    def test_backend_exception_is_sanitized(self):
        _, model = self.llama()
        model.create_chat_completion.side_effect = RuntimeError("secret backend body")
        with self.assertRaisesRegex(llm.LocalRefineError, "Local LLM refinement failed") as result:
            llm.LocalRefiner(self.root).refine("hello")
        self.assertNotIn("secret", str(result.exception))

    def test_release_forces_reload(self):
        factory, _ = self.llama()
        refiner = llm.LocalRefiner(self.root)
        refiner.refine("hello")
        refiner.release()
        refiner.refine("hello")
        self.assertEqual(factory.call_count, 2)

    def test_is_downloaded_never_imports_optional_packages(self):
        self.assertFalse(llm.is_downloaded(self.root))
        put_model(self.folder)
        self.assertTrue(llm.is_downloaded(self.root))
        self.imports.assert_not_called()

    def test_is_downloaded_false_for_incomplete_or_truncated(self):
        put_model(self.folder)
        (self.folder / llm._MODEL_FILE).write_bytes(b"too small")
        self.assertFalse(llm.is_downloaded(self.root))
        put_model(self.folder)
        (self.folder / llm._INCOMPLETE_MARKER).write_text("x", encoding="utf-8")
        self.assertFalse(llm.is_downloaded(self.root))

    def test_explicit_download_has_fixed_repo_and_file(self):
        download = self.hub()
        progress = Mock()
        result = llm.download_model(self.root, progress)
        self.assertEqual(result, self.folder / llm._MODEL_FILE)
        kwargs = download.call_args.kwargs
        self.assertEqual(kwargs["repo_id"], llm._MODEL_REPO)
        self.assertEqual(kwargs["filename"], llm._MODEL_FILE)
        self.assertEqual(kwargs["local_dir"], str(self.folder))
        self.assertIs(kwargs["token"], False)
        self.assertFalse((self.folder / llm._INCOMPLETE_MARKER).exists())
        self.assertTrue(messages_start_with(progress, "Ready:"))

    def test_complete_existing_download_needs_no_network(self):
        put_model(self.folder)
        original = (self.folder / llm._MODEL_FILE).read_bytes()
        self.assertEqual(llm.download_model(self.root), self.folder / llm._MODEL_FILE)
        self.imports.assert_not_called()
        self.assertEqual((self.folder / llm._MODEL_FILE).read_bytes(), original)

    def test_download_missing_hub_is_helpful(self):
        self.imports.side_effect = ImportError("secret")
        with self.assertRaisesRegex(llm.LocalRefineError, "huggingface-hub is missing") as result:
            llm.download_model(self.root)
        self.assertNotIn("secret", str(result.exception))
        self.assertFalse(self.folder.exists())

    def test_download_failure_keeps_user_files_and_retry_marker(self):
        download = self.hub(lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("raw secret url")))
        self.folder.mkdir()
        original = self.folder / "my-notes.txt"
        original.write_text("user file", encoding="utf-8")
        with self.assertRaisesRegex(llm.LocalRefineError, "Existing files were kept") as result:
            llm.download_model(self.root)
        self.assertNotIn("secret", str(result.exception))
        self.assertEqual(original.read_text(encoding="utf-8"), "user file")
        self.assertTrue((self.folder / llm._INCOMPLETE_MARKER).exists())
        download.assert_called_once()

    def test_certificate_errors_are_sanitized(self):
        self.hub(lambda **_kwargs: (_ for _ in ()).throw(ssl.SSLCertVerificationError("secret")))
        with self.assertRaisesRegex(llm.LocalRefineError, "Never disable TLS verification") as result:
            llm.download_model(self.root)
        self.assertNotIn("secret", str(result.exception))

    def test_cancel_before_download_has_no_import_or_files(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(llm.LocalRefineCancelled):
            llm.download_model(self.root, cancel=cancel)
        self.imports.assert_not_called()
        self.assertFalse(self.folder.exists())

    def test_cancel_after_backend_returns_retains_files_and_marker(self):
        cancel = threading.Event()
        def downloaded(**kwargs):
            put_model(Path(kwargs["local_dir"]))
            cancel.set()
        download = self.hub(downloaded)
        with self.assertRaisesRegex(llm.LocalRefineCancelled, "cannot be interrupted per byte"):
            llm.download_model(self.root, cancel=cancel)
        download.assert_called_once()
        self.assertTrue((self.folder / llm._MODEL_FILE).exists())
        self.assertTrue((self.folder / llm._INCOMPLETE_MARKER).exists())


def messages_start_with(mock, prefix):
    return any(call.args[0].startswith(prefix) for call in mock.call_args_list)


if __name__ == "__main__":
    unittest.main()
