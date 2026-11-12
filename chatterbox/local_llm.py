"""Optional CPU-only LLM refinement; importing this module never imports model packages.

Mirrors local_stt.py's structure and safety rules: only ``download_model`` may
access the network, and only after explicit user confirmation. Model files,
but never dictation text, live on disk beyond this process's memory.
"""
from __future__ import annotations

import importlib
import ssl
import threading
from pathlib import Path
from typing import Callable

from .api import REFINEMENT_INSTRUCTIONS

_MODEL_REPO = "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
_MODEL_FILE = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
_MIN_FILE_SIZE = 50 * 1024 * 1024  # A complete Q4 quantization is roughly 1 GB; well short of this means truncated.
_INCOMPLETE_MARKER = ".voice-input-download-incomplete"
_DOWNLOAD_LOCK = threading.Lock()
Progress = Callable[[str], None]


class LocalRefineError(RuntimeError):
    """A safe, actionable local-LLM error (without backend bodies or dictation text)."""


class LocalRefineCancelled(LocalRefineError):
    """An explicitly requested download was cancelled."""


def _notify(progress: Progress | None, text: str) -> None:
    if progress is not None:
        try:
            progress(text)
        except Exception:
            pass


def _model_location(models_dir: Path) -> tuple[Path, Path]:
    try:
        root = Path(models_dir).expanduser().resolve()
        target = root / "llm"
        if target.resolve() != target:
            raise LocalRefineError("The local refinement model directory must not be a symbolic link or junction.")
        return root, target
    except LocalRefineError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise LocalRefineError("The local refinement model storage path is invalid or inaccessible.") from None


def _validate_model(root: Path, target: Path, *, ignore_marker: bool = False) -> Path:
    try:
        if target.resolve() != root / target.name:
            raise LocalRefineError("The local refinement model directory changed or points outside its configured location.")
        if not target.is_dir():
            raise LocalRefineError(
                "The local refinement model is absent. Use the explicit Download refinement model action first; no automatic download was attempted."
            )
        if not ignore_marker and (target / _INCOMPLETE_MARKER).exists():
            raise LocalRefineError("The local refinement model download is incomplete. Retry the explicit Download action.")
        file = target / _MODEL_FILE
        if file.resolve() != file or not file.is_file() or file.stat().st_size < _MIN_FILE_SIZE:
            raise LocalRefineError(
                "The local refinement model is missing, incomplete, or linked elsewhere. Retry the explicit Download refinement model action."
            )
        return file
    except LocalRefineError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise LocalRefineError(
            "The local refinement model file is unreadable or incomplete. Check storage permissions or retry the explicit Download action."
        ) from None


def is_downloaded(models_dir: Path) -> bool:
    """File-presence check only; never imports optional packages."""
    try:
        root, target = _model_location(models_dir)
        _validate_model(root, target)
        return True
    except LocalRefineError:
        return False


def _llama_class():
    try:
        model_class = getattr(importlib.import_module("llama_cpp"), "Llama")
        if not callable(model_class):
            raise TypeError("Invalid optional package")
        return model_class
    except Exception:
        raise LocalRefineError(
            "Local LLM refinement is unavailable: llama-cpp-python is missing or incompatible with this Python. "
            "Use Cloud or Local (rule-based) refinement, or separately provision a supported standard Windows Python with this optional package. Nothing was installed."
        ) from None


class LocalRefiner:
    """One lazily loaded/reused CPU model; loading and inference are serialized."""

    def __init__(self, models_dir: Path, progress: Progress | None = None, cpu_threads: int | None = None):
        self._root, self._model_dir = _model_location(models_dir)
        self.progress = progress
        self._cpu_threads = cpu_threads
        self._model = None
        self._lock = threading.Lock()

    def check_available(self) -> str:
        """Explicitly check the optional import and local file; never loads weights."""
        with self._lock:
            try:
                _llama_class()
                _validate_model(self._root, self._model_dir)
                message = "Local refinement model file and CPU dependencies are available. Inference has not been tested by this check."
                _notify(self.progress, "Ready: " + message)
                return message
            except LocalRefineError as exc:
                _notify(self.progress, "Failed: " + str(exc))
                raise

    def _ensure_model(self):
        """Caller holds self._lock."""
        if self._model is None:
            model_class = _llama_class()
            model_file = _validate_model(self._root, self._model_dir)
            _notify(self.progress, "Loading local refinement model on CPU (first use may take a moment)...")
            try:
                import os
                self._model = model_class(
                    model_path=str(model_file), n_ctx=4096, n_threads=self._cpu_threads or max(1, min(4, os.cpu_count() or 1)),
                    verbose=False,
                )
                if self._model is None:
                    raise TypeError("No model returned")
            except Exception:
                self._model = None
                raise LocalRefineError(
                    "The local CPU refinement model could not be loaded. Check that llama-cpp-python matches this Python/CPU and the downloaded model is complete. No cloud fallback or download was attempted."
                ) from None

    def refine(self, text: str) -> str:
        with self._lock:
            try:
                if not isinstance(text, str) or not text.strip():
                    raise LocalRefineError("Local refinement requires nonempty transcribed text.")
                self._ensure_model()
                _notify(self.progress, "Refining locally on CPU...")
                try:
                    result = self._model.create_chat_completion(
                        messages=[{"role": "system", "content": REFINEMENT_INSTRUCTIONS}, {"role": "user", "content": text}],
                        max_tokens=1024, temperature=0.0,
                    )
                    choices = result.get("choices") if isinstance(result, dict) else None
                    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                        raise LocalRefineError("The local refinement model returned no completion.")
                    message = choices[0].get("message")
                    output = message.get("content") if isinstance(message, dict) else None
                    if (not isinstance(output, str) or not output.strip()
                            or any((ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127 for char in output)):
                        raise LocalRefineError("The local refinement model returned invalid or empty text.")
                except LocalRefineError:
                    raise
                except Exception:
                    raise LocalRefineError(
                        "Local LLM refinement failed. Check the model file and CPU package compatibility. Nothing was sent anywhere."
                    ) from None
                _notify(self.progress, "Ready: local refinement complete.")
                return output.strip()
            except LocalRefineError as exc:
                _notify(self.progress, "Failed: " + str(exc))
                raise

    def release(self) -> None:
        """Drop this model reference after any active inference finishes."""
        with self._lock:
            self._model = None


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise LocalRefineCancelled(
            "Local refinement model download cancelled. Existing files were kept; an in-flight backend download cannot be interrupted per byte."
        )


def _certificate_failure(exc: Exception) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        text = str(exc).lower()
        if "certificate_verify_failed" in text or "certificate verify failed" in text:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _validate_download_paths(target: Path) -> None:
    if target.exists():
        for entry in target.rglob("*"):
            if entry.is_symlink() or entry.resolve() != entry:
                raise LocalRefineError(
                    "The refinement model directory contains a linked file or subdirectory. Check the download location before retrying; no linked files were changed."
                )


def download_model(models_dir: Path, progress: Progress | None = None, cancel=None) -> Path:
    """Explicit confirmed public-weight download, never called automatically.

    Cancellation is checked while waiting and before/after hf_hub_download.
    The backend can finish its current blocking operation before cancellation
    takes effect. All partial/user files are retained for an explicit retry.
    """
    acquired = False
    try:
        root, target = _model_location(models_dir)
        _check_cancel(cancel)
        _notify(progress, "Preparing explicit local refinement model download...")
        while not acquired:
            _check_cancel(cancel)
            acquired = _DOWNLOAD_LOCK.acquire(timeout=0.1)
        _check_cancel(cancel)
        try:
            _validate_model(root, target)
        except LocalRefineError:
            pass
        else:
            _notify(progress, "Ready: the local refinement model is already downloaded.")
            return target / _MODEL_FILE
        try:
            hub = importlib.import_module("huggingface_hub")
            hf_hub_download = getattr(hub, "hf_hub_download")
            if not callable(hf_hub_download):
                raise TypeError("Invalid optional package")
        except Exception:
            raise LocalRefineError(
                "Refinement model download is unavailable: huggingface-hub is missing or incompatible with this Python. Provision it separately in a supported Python environment; nothing was installed."
            ) from None
        try:
            # huggingface_hub's own tqdm progress bars run a background monitor
            # thread guarded by a process-global write lock; a left-over monitor
            # thread from an earlier attempt in this process can deadlock against
            # a new bar's creation on a second download. We already report our
            # own coarse progress via `progress`, so disable theirs entirely.
            hub.utils.disable_progress_bars()
        except Exception:
            pass
        _check_cancel(cancel)
        root, target = _model_location(models_dir)
        _validate_download_paths(target)
        target.mkdir(parents=True, exist_ok=True)
        marker = target / _INCOMPLETE_MARKER
        if marker.is_symlink() or marker.resolve() != marker:
            raise LocalRefineError("The download marker is linked elsewhere. Check the model directory before retrying.")
        marker.touch(exist_ok=True)
        _notify(progress, "Downloading public model weights (roughly 1 GB). Cancellation takes effect after the current backend operation returns; no per-byte progress is available.")
        _check_cancel(cancel)
        hf_hub_download(repo_id=_MODEL_REPO, filename=_MODEL_FILE, local_dir=str(target), token=False)
        _check_cancel(cancel)
        file = _validate_model(root, target, ignore_marker=True)
        marker.unlink()
        _check_cancel(cancel)
        _notify(progress, "Ready: local refinement model downloaded and checked.")
        return file
    except LocalRefineError as exc:
        _notify(progress, "Cancelled: " + str(exc) if isinstance(exc, LocalRefineCancelled) else "Failed: " + str(exc))
        raise
    except Exception as exc:
        if _certificate_failure(exc):
            message = (
                "Refinement model download failed certificate verification. Use a supported Python with a valid trusted CA store, or configure an administrator-approved CA bundle for the download backend. Never disable TLS verification. Existing files were kept."
            )
        else:
            message = "Refinement model download failed. Check internet access, storage permissions, and optional package compatibility, then explicitly retry. Existing files were kept."
        _notify(progress, "Failed: " + message)
        raise LocalRefineError(message) from None
    finally:
        if acquired:
            _DOWNLOAD_LOCK.release()
