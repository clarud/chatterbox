"""Bounded, explicit-action-only PCM recording through winmm.waveIn."""
from __future__ import annotations

from array import array
from collections import deque
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import io
import math
import threading
import time
import wave

from .windows import ULONG_PTR, bind, kernel32

MAX_SECONDS = 120
BUFFER_COUNT = 8
BUFFER_MILLISECONDS = 40
START_TIMEOUT = 1.5
FINISH_TIMEOUT = 3.0
STALE_SECONDS = 0.75
CALLBACK_EVENT = 0x00050000
WHDR_DONE = 0x00000001
WHDR_INQUEUE = 0x00000010
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
winmm = C.WinDLL("winmm", use_last_error=True)
# A failed cleanup must not let Python GC free memory that Windows still owns.
_retained: set[Recorder] = set()
_retention_lock = threading.Lock()


class WAVEFORMATEX(C.Structure):
    _pack_ = 2
    _fields_ = [("wFormatTag", W.WORD), ("nChannels", W.WORD), ("nSamplesPerSec", W.DWORD),
                ("nAvgBytesPerSec", W.DWORD), ("nBlockAlign", W.WORD),
                ("wBitsPerSample", W.WORD), ("cbSize", W.WORD)]


class WAVEHDR(C.Structure):
    _fields_ = [("lpData", C.c_void_p), ("dwBufferLength", W.DWORD),
                ("dwBytesRecorded", W.DWORD), ("dwUser", ULONG_PTR), ("dwFlags", W.DWORD),
                ("dwLoops", W.DWORD), ("lpNext", C.c_void_p), ("reserved", ULONG_PTR)]


waveInGetNumDevs = bind(winmm, "waveInGetNumDevs", W.UINT)
waveInOpen = bind(winmm, "waveInOpen", W.UINT, C.POINTER(C.c_void_p), W.UINT,
                  C.POINTER(WAVEFORMATEX), ULONG_PTR, ULONG_PTR, W.DWORD)
waveInGetErrorText = bind(winmm, "waveInGetErrorTextW", W.UINT, W.UINT, W.LPWSTR, W.UINT)
waveInStart = bind(winmm, "waveInStart", W.UINT, C.c_void_p)
waveInReset = bind(winmm, "waveInReset", W.UINT, C.c_void_p)
waveInClose = bind(winmm, "waveInClose", W.UINT, C.c_void_p)
waveInPrepareHeader = bind(winmm, "waveInPrepareHeader", W.UINT, C.c_void_p, C.POINTER(WAVEHDR), W.UINT)
waveInUnprepareHeader = bind(winmm, "waveInUnprepareHeader", W.UINT, C.c_void_p, C.POINTER(WAVEHDR), W.UINT)
waveInAddBuffer = bind(winmm, "waveInAddBuffer", W.UINT, C.c_void_p, C.POINTER(WAVEHDR), W.UINT)
CreateEvent = bind(kernel32, "CreateEventW", W.HANDLE, C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR)
SetEvent = bind(kernel32, "SetEvent", W.BOOL, W.HANDLE)
WaitForSingleObject = bind(kernel32, "WaitForSingleObject", W.DWORD, W.HANDLE, W.DWORD)
CloseHandle = bind(kernel32, "CloseHandle", W.BOOL, W.HANDLE)


def _check(code: int) -> None:
    if code:
        text = C.create_unicode_buffer(256)
        waveInGetErrorText(code, text, len(text))
        raise RuntimeError(f"Microphone error: {text.value or code}. Check Windows microphone privacy and input-device settings.")


def _kernel_check(result: object) -> None:
    if not result:
        raise C.WinError(C.get_last_error())


def pcm_format(rate: int) -> WAVEFORMATEX:
    return WAVEFORMATEX(1, 1, rate, rate * 2, 2, 16, 0)


def diagnostics() -> dict:
    """Query capabilities only. This does not open a device or record audio."""
    rates = [rate for rate in (16000, 48000, 44100)
             if waveInOpen(None, 0xFFFFFFFF, C.byref(pcm_format(rate)), 0, 0, 1) == 0]
    return {"input_device_count": waveInGetNumDevs(), "supported_mono_pcm_rates": rates}


@dataclass(frozen=True)
class Recording:
    wav: bytes
    seconds: float
    rms: float


@dataclass(frozen=True)
class CaptureStatus:
    active: bool
    seconds: float
    rms: float
    last_data_at: float
    error: str = ""
    limit_reached: bool = False


def encode_recording(pcm: bytes | bytearray, rate: int) -> Recording:
    pcm = pcm[:len(pcm) - len(pcm) % 2]
    seconds = len(pcm) / (rate * 2)
    if seconds < 0.25:
        raise RuntimeError("Recording was too short. Record at least a quarter second.")
    samples = array("h", pcm)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    if rms < 20:
        raise RuntimeError("No usable audio detected. Check your microphone and speak closer to it.")
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return Recording(stream.getvalue(), seconds, rms)


@dataclass
class _Buffer:
    data: C.Array
    header: WAVEHDR
    prepared: bool = False


class Recorder:
    """One native stream; only its collector reads returned headers or recycles them.

    Lifecycle calls serialize separately from snapshots. Failed native cleanup retains
    every allocation and the callback event; cancel() can retry after the worker exits.
    No Python function is registered as a native callback.
    """

    def __init__(self) -> None:
        self.handle = C.c_void_p()
        self.rate = 16000
        self._lifecycle = threading.Lock()
        self._lock = threading.RLock()
        self._buffers: list[_Buffer] = []
        self._queue: deque[_Buffer] = deque()
        self._event: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._done = threading.Event()
        self._stop_requested = threading.Event()
        self._session = False
        self._running = False
        self._pcm = bytearray()
        self._samples = 0
        self._submitted = 0
        self._rms = 0.0
        self._last_data_at = 0.0
        self._error = ""
        self._cleanup_error = ""
        self._limit_reached = False

    def snapshot(self) -> CaptureStatus:
        """RMS is the latest returned block, normalized to full-scale signed PCM.

        Time is actual received samples, not wall time. Stale data is an error even
        when the native device remains open; polling never starts or stops capture.
        """
        with self._lock:
            error = self._error
            running = self._running and not self._stop_requested.is_set()
            if running and self._samples and time.monotonic() - self._last_data_at > STALE_SECONDS:
                error = error or "Microphone audio is stale; no new captured samples were returned."
            active = running and self._samples > 0 and not error and not self._limit_reached
            return CaptureStatus(active, self._samples / self.rate,
                                 self._rms if active else 0.0, self._last_data_at,
                                 error, self._limit_reached)

    def _record_error(self, exc: Exception) -> None:
        with self._lock:
            text = str(exc) or type(exc).__name__
            self._error = f"{self._error}; {text}" if self._error else text

    def start(self) -> None:
        with self._lifecycle:
            if self._session or self.handle.value or self._event is not None:
                raise RuntimeError("Already recording or microphone cleanup is pending.")
            with self._lock:
                self._pcm = bytearray()
                self._samples = self._submitted = 0
                self._rms = self._last_data_at = 0.0
                self._error = self._cleanup_error = ""
                self._limit_reached = False
                self._session = True
            self._ready.clear()
            self._done.clear()
            self._stop_requested.clear()
            self._thread = threading.Thread(target=self._collect, name="wavein-collector", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._session = False
                self._thread = None
                raise
            ready = self._ready.wait(START_TIMEOUT)
            with self._lock:
                confirmed = ready and self._samples > 0 and self._running and not self._error
            if not confirmed:
                if not ready:
                    self._record_error(RuntimeError("Microphone returned no audio within the startup timeout."))
                self._finish()
                if not self.handle.value and self._event is None:
                    self._session = False
                    self._pcm.clear()
                raise RuntimeError(self._error or "Microphone stopped before returning audio.")

    def _open(self) -> None:
        event = CreateEvent(None, False, False, None)  # Auto-reset; no ResetEvent race.
        _kernel_check(event)
        with self._lock:
            self._event = event
        with _retention_lock:
            _retained.add(self)
        last_error = 0
        for rate in (16000, 48000, 44100):
            candidate = C.c_void_p()
            last_error = waveInOpen(C.byref(candidate), 0xFFFFFFFF,
                                    C.byref(pcm_format(rate)), event, 0, CALLBACK_EVENT)
            if last_error == 0:
                self.handle = candidate
                self.rate = rate
                break
        _check(last_error)
        size = self.rate * BUFFER_MILLISECONDS // 1000 * 2
        for _ in range(BUFFER_COUNT):
            data = C.create_string_buffer(size)
            buffer = _Buffer(data, WAVEHDR(lpData=C.cast(data, C.c_void_p), dwBufferLength=size))
            self._buffers.append(buffer)  # Retain even if preparation/queueing fails.
            _check(waveInPrepareHeader(self.handle, C.byref(buffer.header), C.sizeof(WAVEHDR)))
            buffer.prepared = True
            self._enqueue(buffer)
        _check(waveInStart(self.handle))
        with self._lock:
            self._running = True

    def _enqueue(self, buffer: _Buffer) -> None:
        remaining = int(self.rate * MAX_SECONDS) - self._submitted
        if remaining <= 0:
            return
        samples = min(len(buffer.data) // 2, remaining)
        buffer.header.dwBufferLength = samples * 2
        buffer.header.dwBytesRecorded = 0
        _check(waveInAddBuffer(self.handle, C.byref(buffer.header), C.sizeof(WAVEHDR)))
        self._submitted += samples
        self._queue.append(buffer)

    def _drain(self, *, requeue: bool, keep: bool = True) -> None:
        # A later DONE header must never leapfrog the head of the submission queue.
        while self._queue and self._queue[0].header.dwFlags & WHDR_DONE:
            buffer = self._queue[0]
            header = buffer.header
            if header.dwFlags & WHDR_INQUEUE:
                raise RuntimeError("Microphone returned a buffer still owned by the driver.")
            self._queue.popleft()
            count = int(header.dwBytesRecorded)
            if keep and (count > header.dwBufferLength or count % 2):
                raise RuntimeError("Microphone returned an invalid PCM buffer length.")
            if keep and count:
                pcm = C.string_at(header.lpData, count)
                samples = array("h", pcm)
                rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768.0
                with self._lock:
                    self._pcm.extend(pcm)
                    self._samples += count // 2
                    self._rms = min(1.0, rms)
                    self._last_data_at = time.monotonic()
                    self._limit_reached = self._samples >= int(self.rate * MAX_SECONDS)
                self._ready.set()  # Silence is data; amplitude is not startup evidence.
            if requeue and not self._stop_requested.is_set() and not self._limit_reached:
                self._enqueue(buffer)

    def _collect(self) -> None:
        try:
            self._open()
            while not self._stop_requested.is_set() and not self._limit_reached:
                result = WaitForSingleObject(self._event, 250)
                if result == WAIT_OBJECT_0:
                    self._drain(requeue=not self._stop_requested.is_set())
                elif result != WAIT_TIMEOUT:
                    raise C.WinError(C.get_last_error())
        except Exception as exc:
            self._record_error(exc)
        finally:
            self._complete_cleanup(keep=True)

    def _cleanup(self, *, keep: bool) -> None:
        if self.handle.value:
            # Reset synchronously returns partial final buffers. Never infer their
            # contents before DONE or unprepare/wipe any driver-owned allocation.
            _check(waveInReset(self.handle))
            self._drain(requeue=False, keep=keep)
            if self._queue or any(buffer.header.dwFlags & WHDR_INQUEUE for buffer in self._buffers):
                raise RuntimeError("Microphone reset did not return every queued buffer; resources retained.")
            errors = []
            for buffer in self._buffers:
                if buffer.prepared:
                    try:
                        _check(waveInUnprepareHeader(self.handle, C.byref(buffer.header), C.sizeof(WAVEHDR)))
                        buffer.prepared = False
                    except Exception as exc:
                        errors.append(str(exc))
            if errors:
                raise RuntimeError("Microphone header cleanup failed: " + "; ".join(errors))
            _check(waveInClose(self.handle))
            self.handle = C.c_void_p()
        with self._lock:
            if self._event is not None:
                _kernel_check(CloseHandle(self._event))
                self._event = None
        for buffer in self._buffers:
            C.memset(buffer.data, 0, len(buffer.data))
        self._buffers.clear()
        with _retention_lock:
            _retained.discard(self)

    def _complete_cleanup(self, *, keep: bool) -> None:
        try:
            self._cleanup(keep=keep)
        except Exception as exc:
            self._cleanup_error = str(exc) or type(exc).__name__
            self._record_error(exc)
        finally:
            with self._lock:
                self._running = False
                self._rms = 0.0
            self._ready.set()
            self._done.set()

    def _finish(self) -> None:
        self._stop_requested.set()
        wake_error = None
        with self._lock:
            if self._event is not None and not self._done.is_set():
                try:
                    _kernel_check(SetEvent(self._event))
                except Exception as exc:
                    wake_error = exc
                    self._record_error(exc)  # The bounded wait also observes stop.
        if not self._done.wait(FINISH_TIMEOUT):
            self._record_error(RuntimeError("Microphone cleanup timed out; resources retained."))
            raise RuntimeError(self._error)
        if self._thread is not None:
            self._thread.join()
        if wake_error is not None:
            raise RuntimeError(self._error) from wake_error

    def stop(self) -> Recording:
        with self._lifecycle:
            if not self._session:
                raise RuntimeError("Not recording.")
            self._finish()
            if self._error:
                if not self.handle.value and self._event is None:
                    self._session = False
                    self._pcm.clear()
                raise RuntimeError(self._error)
            pcm, self._pcm = self._pcm, bytearray()
            self._session = False
            return encode_recording(pcm, self.rate)

    def cancel(self) -> None:
        with self._lifecycle:
            if self._thread is not None and self._thread.is_alive():
                self._finish()
            # A reset/unprepare/close failure can be retried without opening a device.
            if self.handle.value or self._event is not None:
                self._cleanup_error = ""
                self._done.clear()
                self._thread = threading.Thread(target=lambda: self._complete_cleanup(keep=False),
                                                name="wavein-cleanup", daemon=True)
                self._thread.start()
                self._finish()
            if self._cleanup_error:
                raise RuntimeError(self._cleanup_error)
            with self._lock:
                self._session = False
                self._pcm.clear()
                self._samples = 0
                self._rms = self._last_data_at = 0.0
                self._limit_reached = False
