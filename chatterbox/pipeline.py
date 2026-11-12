"""UI-independent dictation pipeline; emits immutable results and stage events."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import Enum
import time
import uuid


class State(str, Enum):
    IDLE = "Ready"
    RECORDING = "Recording"
    TRANSCRIBING = "Transcribing"
    REFINING = "Refining"
    INSERTING = "Inserting"
    SUCCESS = "Success"
    CANCELLING = "Cancelling"
    ERROR = "Error"
    DOWNLOADING = "Downloading model"


@dataclass(frozen=True)
class DictationResult:
    """Text/provenance result, never containing audio or credentials.

    session_id is the legacy unique dictation ID; a workspace chat session is
    captured separately by the controller, not inferred from UI changes later.
    """
    raw: str
    mode: str = "polished"
    polished: str | None = None
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    refinement_error: str | None = None
    insertion_error: str | None = None
    used_text: str | None = None
    insertion_sent: bool = False
    possibly_sent: bool = False
    warning: str = ""
    transcription_seconds: float | None = None
    refinement_seconds: float | None = None
    insertion_seconds: float | None = None

    def to_dict(self):
        return asdict(self)

    @property
    def can_retry(self):
        return not self.insertion_sent and not self.possibly_sent


class Cancelled(Exception):
    pass


def run_pipeline(recording, service, target, paste, emit, cancel, mode="polished"):
    """Mode is session-fixed. Raw skips LLM editing; failed polish offers raw.

    A failed or uncertain paste preserves text. Only definitely-not-sent results
    may be retried; cleanup failure must not turn a sent Ctrl+V into a duplicate.
    """
    def check_cancelled():
        if cancel.is_set():
            raise Cancelled()

    def timing(stage, start):
        seconds = max(0.0, time.perf_counter() - start)
        emit("timing", {"stage": stage, "seconds": seconds})
        return seconds

    inserting, result = False, None
    insertion_seconds = None
    try:
        if mode not in ("raw", "polished"):
            raise ValueError("Select Raw or Polished output mode.")
        check_cancelled()
        emit("phase", State.TRANSCRIBING)
        start = time.perf_counter()
        try:
            raw = service.transcribe(recording.wav)
        finally:
            transcription_seconds = timing("transcription", start)
        if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
            raise RuntimeError("Transcription returned no usable text.")
        result = DictationResult(raw=raw, mode=mode, transcription_seconds=transcription_seconds)
        emit("result", result)
        emit("preview", raw)
        check_cancelled()
        text = raw
        if mode == "polished":
            emit("phase", State.REFINING)
            start = time.perf_counter()
            try:
                polished = service.refine(raw)
                if not isinstance(polished, str) or not polished.strip() or "\0" in polished:
                    raise RuntimeError("Polishing returned no usable text.")
            except Exception as exc:
                result = replace(result, refinement_error=str(exc), refinement_seconds=timing("refinement", start))
                emit("result", result)
                check_cancelled()
                emit("fallback", result)
                return
            result = replace(result, polished=polished, refinement_seconds=timing("refinement", start))
            emit("result", result)
            emit("preview", polished)
            text = polished
        check_cancelled()
        emit("phase", State.INSERTING)
        inserting = True
        start = time.perf_counter()
        try:
            warning = paste(text, target, cancel)
        finally:
            insertion_seconds = timing("insertion", start)
        emit("result", replace(result, used_text=text, insertion_sent=True, insertion_seconds=insertion_seconds, warning=warning or ""))
        emit("done", warning or "Paste shortcut sent to the original input. Clipboard restored.")
    except Cancelled:
        emit("cancelled", "Cancelled. Nothing was inserted.")
    except Exception as exc:
        if cancel.is_set() and not inserting:
            emit("cancelled", "Cancelled. See the preview if text was already transcribed.")
        else:
            if inserting and result is not None:
                possible = bool(getattr(exc, "possibly_sent", False))
                emit("result", replace(result, insertion_error=str(exc), possibly_sent=possible,
                                       used_text=text if possible else None, insertion_seconds=insertion_seconds))
            emit("error", str(exc))
