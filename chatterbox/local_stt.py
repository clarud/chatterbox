"""Optional CPU transcription; importing this module never imports model packages.

Only ``download_model`` may access the network. Its caller must obtain the user's
explicit download confirmation first. Model files, but never audio, live on disk.
Availability checks validate files and imports; they do not prove inference works.
"""
from __future__ import annotations

import importlib
import json
import os
import ssl
import threading
from io import BytesIO
from pathlib import Path
from typing import Callable


class LocalTranscriptionError(RuntimeError):
    """A safe, actionable local-model error (without backend bodies or audio)."""


class LocalTranscriptionCancelled(LocalTranscriptionError):
    """An explicitly requested download was cancelled."""


_MODEL_REPOSITORIES = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
}
_REQUIRED_FILES = ("model.bin", "config.json", "tokenizer.json")
_DOWNLOAD_FILES = (
    "model.bin", "config.json", "tokenizer.json", "preprocessor_config.json",
    "vocabulary.*", "vocab.*", "merges.txt", "tokenizer_config.json",
    "special_tokens_map.json", "added_tokens.json",
)
_INCOMPLETE_MARKER = ".voice-input-download-incomplete"
_DOWNLOAD_LOCK = threading.Lock()
Progress = Callable[[str], None]


def _notify(progress: Progress | None, text: str) -> None:
    if progress is not None:
        try:
            progress(text)
        except Exception:
            # A UI callback must never interrupt model/stream/marker cleanup.
            pass


def _model_location(model_name: str, models_dir: Path) -> tuple[Path, Path]:
    if not isinstance(model_name, str) or model_name not in _MODEL_REPOSITORIES:
        raise LocalTranscriptionError("Choose a supported local model: tiny, base, or small.")
    try:
        root = Path(models_dir).expanduser().resolve()
        target = root / model_name
        # Do not follow an existing model-directory symlink/junction elsewhere.
        if target.resolve() != target:
            raise LocalTranscriptionError("The local model directory must not be a symbolic link or junction.")
        return root, target
    except LocalTranscriptionError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise LocalTranscriptionError("The local model storage path is invalid or inaccessible.") from None


def _validate_model(root: Path, target: Path, *, ignore_marker: bool = False) -> None:
    try:
        if target.resolve() != root / target.name:
            raise LocalTranscriptionError("The local model directory changed or points outside its configured location.")
        if not target.is_dir():
            raise LocalTranscriptionError(
                "Local model files are absent. Use the explicit Download model action first; no automatic download was attempted."
            )
        if not ignore_marker and (target / _INCOMPLETE_MARKER).exists():
            raise LocalTranscriptionError("The local model download is incomplete. Retry the explicit Download model action.")
        if any(target.rglob("*.incomplete")):
            raise LocalTranscriptionError("The local model contains unfinished download files. Retry the explicit Download model action.")
        for name in _REQUIRED_FILES:
            file = target / name
            if file.resolve() != file or not file.is_file() or file.stat().st_size == 0:
                raise LocalTranscriptionError(
                    f"The local model is incomplete: {name} is missing, empty, or linked elsewhere. Retry the explicit Download model action."
                )
            if name.endswith(".json"):
                with file.open("r", encoding="utf-8") as stream:
                    if not isinstance(json.load(stream), dict):
                        raise ValueError("Not a JSON object")
    except LocalTranscriptionError:
        raise
    except (OSError, RuntimeError, ValueError, UnicodeError):
        raise LocalTranscriptionError(
            "The local model files are unreadable or incomplete. Check storage permissions or retry the explicit Download model action."
        ) from None


def is_downloaded(model_name: str, models_dir: Path) -> bool:
    """File-presence check only; never imports optional packages."""
    try:
        root, target = _model_location(model_name, models_dir)
        _validate_model(root, target)
        return True
    except LocalTranscriptionError:
        return False


def _whisper_class():
    try:
        model_class = getattr(importlib.import_module("faster_whisper"), "WhisperModel")
        if not callable(model_class):
            raise TypeError("Invalid optional package")
        return model_class
    except Exception:
        raise LocalTranscriptionError(
            "Local transcription is unavailable: faster-whisper and its CTranslate2/PyAV dependencies are missing or incompatible with this Python. "
            "Use cloud transcription, or separately provision a supported standard Windows Python with these optional packages. Nothing was installed."
        ) from None


def _cjk(char: str) -> bool:
    code = ord(char)
    return (0x2E80 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF
            or 0xF900 <= code <= 0xFAFF or 0x20000 <= code <= 0x3134F)


def _join_fragments(fragments: list[str]) -> str:
    """Preserve model wording/whitespace; supply only absent phrase boundaries."""
    result = ""
    for fragment in fragments:
        if not fragment:
            continue
        if (result and not result[-1].isspace() and not fragment[0].isspace()
                and fragment[0] not in ".,!?;:%)]}»”’'，。！？；：、）】」』…"
                and result[-1] not in "([{«“‘\"（【「『"
                and not (_cjk(result[-1]) and _cjk(fragment[0]))):
            result += " "
        result += fragment
    return result.strip()


class LocalTranscriber:
    """One lazily loaded/reused CPU model; loading and inference are serialized."""

    def __init__(self, model_name: str, models_dir: Path, progress: Progress | None = None):
        self._root, self._model_path = _model_location(model_name, models_dir)
        self.model_name = model_name
        self.progress = progress
        self._model = None
        self._lock = threading.Lock()

    @property
    def model_path(self) -> Path:
        return self._model_path

    def check_available(self) -> str:
        """Explicitly check optional imports and local files; never load weights."""
        with self._lock:
            try:
                _whisper_class()
                _validate_model(self._root, self._model_path)
                message = f"Local {self.model_name} model files and CPU dependencies are available. Inference has not been tested by this check."
                _notify(self.progress, "Ready: " + message)
                return message
            except LocalTranscriptionError as exc:
                _notify(self.progress, "Failed: " + str(exc))
                raise

    def _ensure_model(self):
        """Caller holds self._lock."""
        if self._model is None:
            model_class = _whisper_class()
            _validate_model(self._root, self._model_path)
            _notify(self.progress, f"Loading local {self.model_name} model on CPU (first use may take a moment)...")
            try:
                self._model = model_class(
                    str(self._model_path), device="cpu", compute_type="int8",
                    local_files_only=True, cpu_threads=max(1, min(4, os.cpu_count() or 1)),
                    num_workers=1,
                )
                if self._model is None:
                    raise TypeError("No model returned")
            except Exception:
                self._model = None
                raise LocalTranscriptionError(
                    "The local CPU model could not be loaded. Check that the optional packages match this Python/CPU and the downloaded model is complete. No cloud fallback or download was attempted."
                ) from None

    def transcribe(self, wav: bytes) -> str:
        with self._lock:
            try:
                if (not isinstance(wav, bytes) or len(wav) < 12
                        or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE"):
                    raise LocalTranscriptionError("Local transcription requires a non-empty WAV recording as bytes.")
                self._ensure_model()
                _notify(self.progress, "Transcribing locally on CPU...")
                segments = None
                try:
                    with BytesIO(wav) as audio:
                        segments, _info = self._model.transcribe(audio, beam_size=5, language="en")
                        fragments = []
                        try:
                            # faster-whisper performs inference while this generator runs.
                            for segment in segments:
                                text = getattr(segment, "text", None)
                                if (not isinstance(text, str)
                                        or any((ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127 for char in text)):
                                    raise LocalTranscriptionError("The local model returned invalid transcription text. Nothing was inserted.")
                                fragments.append(text)
                            text = _join_fragments(fragments)
                            if not text:
                                raise LocalTranscriptionError("No speech was transcribed locally. Check the recording and try again.")
                        finally:
                            close = getattr(segments, "close", None)
                            if callable(close):
                                try:
                                    close()
                                except Exception:
                                    pass
                except LocalTranscriptionError:
                    raise
                except Exception:
                    raise LocalTranscriptionError(
                        "Local transcription failed. Check the WAV recording, model files, and CPU package compatibility. No audio was saved or sent to a cloud service."
                    ) from None
                _notify(self.progress, "Ready: local transcription complete.")
                return text
            except LocalTranscriptionError as exc:
                _notify(self.progress, "Failed: " + str(exc))
                raise

    def transcribe_file(self, path, cancel=None) -> list[dict]:
        """Transcribe an existing local audio/video file with per-segment timestamps.

        Decoding (any format faster-whisper/PyAV supports, including mp3/mp4) happens
        entirely on this machine; the file is never uploaded or sent anywhere.
        """
        with self._lock:
            try:
                file_path = Path(path)
                if not file_path.is_file():
                    raise LocalTranscriptionError("Select an existing audio or video file.")
                self._ensure_model()
                _notify(self.progress, "Transcribing file locally on CPU...")
                results = []
                try:
                    segments, _info = self._model.transcribe(str(file_path), beam_size=5, language="en")
                    try:
                        for segment in segments:
                            if cancel is not None and cancel.is_set():
                                raise LocalTranscriptionCancelled("File transcription cancelled. Partial results were discarded.")
                            text = getattr(segment, "text", None)
                            if (not isinstance(text, str)
                                    or any((ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127 for char in text)):
                                raise LocalTranscriptionError("The local model returned invalid transcription text.")
                            text = text.strip()
                            if text:
                                results.append({"start": float(segment.start), "end": float(segment.end), "text": text})
                    finally:
                        close = getattr(segments, "close", None)
                        if callable(close):
                            try:
                                close()
                            except Exception:
                                pass
                except LocalTranscriptionError:
                    raise
                except Exception:
                    raise LocalTranscriptionError(
                        "Local file transcription failed. Check the file format, model files, and CPU package compatibility. The file was not uploaded anywhere."
                    ) from None
                if not results:
                    raise LocalTranscriptionError("No speech was transcribed from the file.")
                _notify(self.progress, "Ready: file transcription complete.")
                return results
            except LocalTranscriptionError as exc:
                _notify(self.progress, ("Cancelled: " if isinstance(exc, LocalTranscriptionCancelled) else "Failed: ") + str(exc))
                raise

    def release(self) -> None:
        """Drop this model reference after any active inference finishes."""
        with self._lock:
            self._model = None


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise LocalTranscriptionCancelled(
            "Local model download cancelled. Existing files were kept; an in-flight backend download cannot be interrupted per byte."
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
    """Do not let a preexisting weight/cache link redirect the backend's writes."""
    if target.exists():
        for entry in target.rglob("*"):
            if entry.is_symlink() or entry.resolve() != entry:
                raise LocalTranscriptionError(
                    "The model directory contains a linked file or subdirectory. Check the download location before retrying; no linked files were changed."
                )


def download_model(model_name: str, models_dir: Path, progress: Progress | None = None, cancel=None) -> Path:
    """Explicit confirmed public-weight download, never called by the provider.

    Cancellation is checked while waiting and before/after snapshot_download.
    The backend can finish its current blocking operation before cancellation
    takes effect. All partial/user files are retained for an explicit retry.
    """
    acquired = False
    try:
        root, target = _model_location(model_name, models_dir)
        _check_cancel(cancel)
        _notify(progress, f"Preparing explicit local {model_name} model download...")
        while not acquired:
            _check_cancel(cancel)
            acquired = _DOWNLOAD_LOCK.acquire(timeout=0.1)
        _check_cancel(cancel)
        # A complete existing model needs no network request or overwriting.
        try:
            _validate_model(root, target)
        except LocalTranscriptionError:
            pass
        else:
            _notify(progress, "Ready: the local model is already downloaded.")
            return target
        try:
            hub = importlib.import_module("huggingface_hub")
            snapshot_download = getattr(hub, "snapshot_download")
            if not callable(snapshot_download):
                raise TypeError("Invalid optional package")
        except Exception:
            raise LocalTranscriptionError(
                "Model download is unavailable: huggingface-hub is missing or incompatible with this Python. Provision it separately in a supported Python environment; nothing was installed."
            ) from None
        try:
            # See local_llm.py: huggingface_hub's own tqdm progress bars run a
            # background monitor thread guarded by a process-global write lock;
            # a left-over monitor thread from an earlier attempt in this process
            # can deadlock against a new bar's creation on a second download.
            hub.utils.disable_progress_bars()
        except Exception:
            pass
        _check_cancel(cancel)
        # Revalidate containment before creating/writing anything.
        root, target = _model_location(model_name, models_dir)
        _validate_download_paths(target)
        target.mkdir(parents=True, exist_ok=True)
        marker = target / _INCOMPLETE_MARKER
        if marker.is_symlink() or marker.resolve() != marker:
            raise LocalTranscriptionError("The download marker is linked elsewhere. Check the model directory before retrying.")
        marker.touch(exist_ok=True)
        _notify(progress, "Downloading public model weights. Cancellation takes effect after the current backend operation returns; no per-byte progress is available.")
        _check_cancel(cancel)
        snapshot_download(
            repo_id=_MODEL_REPOSITORIES[model_name], local_dir=str(target),
            allow_patterns=list(_DOWNLOAD_FILES), token=False, max_workers=2,
        )
        _check_cancel(cancel)
        _validate_model(root, target, ignore_marker=True)
        marker.unlink()
        _check_cancel(cancel)
        _notify(progress, "Ready: local model downloaded and required files checked.")
        return target
    except LocalTranscriptionError as exc:
        _notify(progress, "Cancelled: " + str(exc) if isinstance(exc, LocalTranscriptionCancelled) else "Failed: " + str(exc))
        raise
    except Exception as exc:
        if _certificate_failure(exc):
            message = (
                "Model download failed certificate verification. Use a supported Python with a valid trusted CA store, or configure an administrator-approved CA bundle for the download backend. Never disable TLS verification. Existing files were kept."
            )
        else:
            message = "Model download failed. Check internet access, storage permissions, and optional package compatibility, then explicitly retry. Existing files were kept."
        _notify(progress, "Failed: " + message)
        raise LocalTranscriptionError(message) from None
    finally:
        if acquired:
            _DOWNLOAD_LOCK.release()
