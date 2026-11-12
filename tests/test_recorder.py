from array import array
import ctypes as C
from dataclasses import FrozenInstanceError
import io
import threading
import time
import unittest
from unittest.mock import Mock, patch
import wave
import weakref
import gc

from chatterbox import audio
from chatterbox.audio import Recorder


def pcm(value, samples):
    return array("h", [value] * samples).tobytes()


class FakeWinMM:
    """Synthetic native ownership/auto-reset event; never opens any real device."""

    def __init__(self):
        self.lock = threading.RLock()
        self.condition = threading.Condition()
        self.signaled = False
        self.pending = []
        self.prepared = set()
        self.failures = {}
        self.open_results = []
        self.open_rates = []
        self.add_sizes = []
        self.operation_threads = []
        self.initial_value = 1000
        self.final_pcm = []
        self.opened = False
        self.event_closed = False
        self.reset_returns = True

    def header(self, pointer):
        return C.cast(pointer, C.POINTER(audio.WAVEHDR)).contents

    def failure(self, name):
        self.operation_threads.append((name, threading.current_thread().name))
        return self.failures.get(name, 0)

    def CreateEvent(self, *args):
        self.event_closed = False
        return 456

    def SetEvent(self, handle):
        with self.condition:
            self.signaled = True
            self.condition.notify_all()
        return 1

    def WaitForSingleObject(self, handle, milliseconds):
        with self.condition:
            if not self.condition.wait_for(lambda: self.signaled, milliseconds / 1000):
                return audio.WAIT_TIMEOUT
            self.signaled = False
            return audio.WAIT_OBJECT_0

    def CloseHandle(self, handle):
        self.assert_unowned()
        if self.failure("CloseHandle"):
            return 0
        self.event_closed = True
        return 1

    def waveInGetErrorText(self, code, text, size):
        text.value = "synthetic native failure"
        return 0

    def waveInGetNumDevs(self):
        return 1

    def waveInOpen(self, pointer, device, format_pointer, callback, instance, flags):
        rate = C.cast(format_pointer, C.POINTER(audio.WAVEFORMATEX)).contents.nSamplesPerSec
        self.open_rates.append(rate)
        if pointer is None:
            assert flags == 1 and callback == 0
            return 0
        assert flags == audio.CALLBACK_EVENT and callback == 456 and instance == 0
        result = self.open_results.pop(0) if self.open_results else self.failure("waveInOpen")
        if not result:
            C.cast(pointer, C.POINTER(C.c_void_p)).contents.value = 123
            self.opened = True
        return result

    def waveInPrepareHeader(self, handle, pointer, size):
        result = self.failure("waveInPrepareHeader")
        if not result:
            header = self.header(pointer)
            self.prepared.add(C.addressof(header))
            header.dwFlags |= 2
        return result

    def waveInAddBuffer(self, handle, pointer, size):
        with self.lock:
            result = self.failure("waveInAddBuffer")
            if not result:
                header = self.header(pointer)
                assert C.addressof(header) in self.prepared
                assert not header.dwFlags & audio.WHDR_INQUEUE
                header.dwFlags = (header.dwFlags | audio.WHDR_INQUEUE) & ~audio.WHDR_DONE
                header.dwBytesRecorded = 0
                self.pending.append(header)
                self.add_sizes.append(header.dwBufferLength)
            return result

    def waveInStart(self, handle):
        result = self.failure("waveInStart")
        if not result and self.initial_value is not None:
            with self.lock:
                length = self.pending[0].dwBufferLength // 2
            self.complete(pcm(self.initial_value, length))
        return result

    def complete(self, data, index=0, *, notify=True):
        with self.lock:
            header = self.pending.pop(index)
            assert len(data) <= header.dwBufferLength
            C.memmove(header.lpData, data, len(data))
            header.dwBytesRecorded = len(data)
            header.dwFlags = (header.dwFlags | audio.WHDR_DONE) & ~audio.WHDR_INQUEUE
        if notify:
            self.SetEvent(456)
        return header

    def waveInReset(self, handle):
        with self.lock:
            result = self.failure("waveInReset")
            if not result and self.reset_returns:
                for header in self.pending:
                    data = self.final_pcm.pop(0) if self.final_pcm else b""
                    assert len(data) <= header.dwBufferLength
                    C.memmove(header.lpData, data, len(data))
                    header.dwBytesRecorded = len(data)
                    header.dwFlags = (header.dwFlags | audio.WHDR_DONE) & ~audio.WHDR_INQUEUE
                self.pending.clear()
                self.SetEvent(456)
            return result

    def waveInUnprepareHeader(self, handle, pointer, size):
        header = self.header(pointer)
        assert not header.dwFlags & audio.WHDR_INQUEUE, "Unprepare attempted on driver-owned buffer"
        result = self.failure("waveInUnprepareHeader")
        if not result:
            self.prepared.remove(C.addressof(header))
            header.dwFlags &= ~2
        return result

    def assert_unowned(self):
        assert not self.pending and not self.prepared, "Closed while driver owns buffers"

    def waveInClose(self, handle):
        self.assert_unowned()
        result = self.failure("waveInClose")
        if not result:
            self.opened = False
        return result


class RecorderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.driver = FakeWinMM()
        self.recorders = []
        self.calls = {}
        self.patchers = []
        names = ("waveInOpen", "waveInPrepareHeader", "waveInAddBuffer", "waveInStart",
                 "waveInReset", "waveInUnprepareHeader", "waveInClose", "waveInGetErrorText",
                 "waveInGetNumDevs", "CreateEvent", "SetEvent", "WaitForSingleObject", "CloseHandle")
        for name in names:
            fn = Mock(side_effect=getattr(self.driver, name))
            patcher = patch.object(audio, name, fn)
            patcher.start()
            self.patchers.append(patcher)
            self.calls[name] = fn

    def tearDown(self):
        self.driver.failures.clear()
        self.driver.reset_returns = True
        self.driver.final_pcm.clear()
        try:
            for recorder in self.recorders:
                recorder.cancel()
                if recorder._thread is not None:
                    self.assertFalse(recorder._thread.is_alive())
        finally:
            for patcher in reversed(self.patchers):
                patcher.stop()

    def recorder(self):
        recorder = Recorder()
        self.recorders.append(recorder)
        return recorder

    def wait_for(self, predicate, message="collector did not make progress"):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(message)
            time.sleep(0.001)

    def send(self, recorder, data):
        target = recorder.snapshot().seconds + len(data) / (recorder.rate * 2)
        self.driver.complete(data)
        self.wait_for(lambda: recorder.snapshot().seconds >= target - 1e-9)

    def wave_pcm(self, recording):
        with wave.open(io.BytesIO(recording.wav), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            return wav.getframerate(), wav.readframes(wav.getnframes())

    def test_construct_and_idle_snapshot_do_not_open_device(self):
        recorder = self.recorder()
        self.assertEqual(recorder.snapshot(), audio.CaptureStatus(False, 0, 0, 0))
        self.calls["waveInOpen"].assert_not_called()
        recorder.cancel()
        self.calls["waveInReset"].assert_not_called()

    def test_start_confirms_silence_and_snapshot_is_frozen(self):
        self.driver.initial_value = 0
        recorder = self.recorder()
        recorder.start()
        status = recorder.snapshot()
        self.assertTrue(status.active)
        self.assertAlmostEqual(status.seconds, 0.04)
        self.assertEqual(status.rms, 0)
        self.assertGreater(status.last_data_at, 0)
        self.assertEqual(status.error, "")
        with self.assertRaises(FrozenInstanceError):
            status.active = False
        self.assertEqual(len(recorder._buffers), 8)
        self.assertEqual({len(buffer.data) for buffer in recorder._buffers}, {1280})
        self.assertEqual(self.calls["waveInPrepareHeader"].call_count, 8)

    def test_start_stop_preserves_all_pcm_and_releases_resources(self):
        recorder = self.recorder()
        recorder.start()
        expected = pcm(1000, 640)
        for value in range(1100, 1800, 100):
            block = pcm(value, 640)
            self.send(recorder, block)
            expected += block
        final = pcm(-2000, 173)
        self.driver.final_pcm = [final]
        result = recorder.stop()
        expected += final
        rate, captured = self.wave_pcm(result)
        self.assertEqual(rate, 16000)
        self.assertEqual(captured, expected)
        self.assertAlmostEqual(result.seconds, len(expected) / 32000)
        self.assertGreater(result.rms, 1000)  # Recording RMS retains its legacy PCM units.
        self.assertFalse(recorder.handle.value)
        self.assertEqual(recorder._buffers, [])
        self.assertFalse(recorder.snapshot().active)
        self.calls["waveInStart"].assert_called_once()
        self.calls["waveInClose"].assert_called_once()
        self.assertEqual(self.calls["waveInUnprepareHeader"].call_count, 8)
        self.assertTrue(self.driver.event_closed)
        self.assertNotIn(recorder, audio._retained)
        self.assertTrue(all(name == "wavein-collector" for _, name in self.driver.operation_threads))

    def test_only_done_headers_are_read_and_out_of_order_completion_does_not_reorder(self):
        recorder = self.recorder()
        recorder.start()
        expected = pcm(1000, 640)
        before = recorder.snapshot().seconds
        with self.driver.lock:
            head = self.driver.pending[0]
            C.memmove(head.lpData, pcm(2222, 640), 1280)
            head.dwBytesRecorded = 1280  # Still owned; this is not valid live progress.
        self.driver.SetEvent(456)
        time.sleep(0.02)
        self.assertEqual(recorder.snapshot().seconds, before)
        later = pcm(3333, 640)
        self.driver.complete(later, index=1)
        time.sleep(0.02)
        self.assertEqual(recorder.snapshot().seconds, before)
        first = pcm(2222, 640)
        self.driver.complete(first)
        self.wait_for(lambda: recorder.snapshot().seconds >= 0.12)
        expected += first + later
        for _ in range(5):
            block = pcm(-1000, 640)
            self.send(recorder, block)
            expected += block
        self.assertEqual(self.wave_pcm(recorder.stop())[1], expected)

    def test_partial_buffers_returned_by_reset_remain_in_queue_order(self):
        recorder = self.recorder()
        recorder.start()
        expected = pcm(1000, 640)
        for _ in range(6):
            block = pcm(1500, 640)
            self.send(recorder, block)
            expected += block
        self.driver.final_pcm = [pcm(2100, 200), pcm(-2200, 99)]
        expected += b"".join(self.driver.final_pcm)
        self.assertEqual(self.wave_pcm(recorder.stop())[1], expected)

    def test_rms_uses_latest_returned_buffer_and_silence_is_zero(self):
        recorder = self.recorder()
        recorder.start()
        self.send(recorder, pcm(-32768, 640))
        self.assertEqual(recorder.snapshot().rms, 1)
        self.send(recorder, pcm(0, 640))
        self.assertEqual(recorder.snapshot().rms, 0)
        self.assertTrue(recorder.snapshot().active)

    def test_snapshot_reports_stale_audio_without_changing_captured_time(self):
        recorder = self.recorder()
        recorder.start()
        status = recorder.snapshot()
        with patch.object(audio.time, "monotonic", return_value=status.last_data_at + audio.STALE_SECONDS + 0.1):
            stale = recorder.snapshot()
        self.assertFalse(stale.active)
        self.assertEqual(stale.seconds, status.seconds)
        self.assertEqual(stale.rms, 0)
        self.assertIn("stale", stale.error)
        self.send(recorder, pcm(1000, 640))
        self.assertEqual(recorder.snapshot().error, "")
        self.assertTrue(recorder.snapshot().active)

    def test_start_does_not_return_before_first_nonempty_buffer(self):
        self.driver.initial_value = None
        recorder = self.recorder()
        failures = []
        completed = threading.Event()

        def start():
            try:
                recorder.start()
            except Exception as exc:
                failures.append(exc)
            finally:
                completed.set()

        caller = threading.Thread(target=start, name="test-start")
        caller.start()
        try:
            self.wait_for(lambda: self.calls["waveInStart"].call_count == 1)
            self.assertFalse(recorder.snapshot().active)
            self.assertFalse(completed.is_set())
            self.driver.complete(b"")
            time.sleep(0.02)
            self.assertFalse(completed.is_set())
            self.driver.complete(pcm(0, 640))
            self.assertTrue(completed.wait(1))
            self.assertFalse(failures)
            self.assertTrue(recorder.snapshot().active)
        finally:
            caller.join(2)

    def test_start_timeout_cleans_up_and_never_claims_active(self):
        self.driver.initial_value = None
        recorder = self.recorder()
        with patch.object(audio, "START_TIMEOUT", 0.05):
            with self.assertRaisesRegex(RuntimeError, "startup timeout"):
                recorder.start()
        self.assertFalse(recorder.snapshot().active)
        self.assertIn("timeout", recorder.snapshot().error)
        self.assertFalse(recorder.handle.value)
        self.assertEqual(recorder._buffers, [])
        self.calls["waveInClose"].assert_called_once()

    def test_cancel_discards_without_encoding_and_is_idempotent(self):
        recorder = self.recorder()
        recorder.start()
        with patch.object(audio, "encode_recording") as encode:
            recorder.cancel()
            recorder.cancel()
            encode.assert_not_called()
        self.assertEqual(recorder._pcm, b"")
        self.assertEqual(recorder.snapshot().seconds, 0)
        self.calls["waveInClose"].assert_called_once()

    def test_duplicate_start_and_stop_without_session_are_rejected(self):
        recorder = self.recorder()
        with self.assertRaisesRegex(RuntimeError, "Not recording"):
            recorder.stop()
        recorder.start()
        with self.assertRaisesRegex(RuntimeError, "Already recording"):
            recorder.start()
        self.calls["waveInOpen"].assert_called_once()

    def test_restart_uses_new_session_and_resets_status(self):
        recorder = self.recorder()
        recorder.start()
        recorder.cancel()
        recorder.start()
        self.assertAlmostEqual(recorder.snapshot().seconds, 0.04)
        self.assertEqual(recorder.snapshot().error, "")
        self.assertEqual(self.calls["waveInOpen"].call_count, 2)

    def test_format_fallback_is_16k_then_48k_then_44100(self):
        self.driver.open_results = [32, 32, 0]
        recorder = self.recorder()
        recorder.start()
        self.assertEqual(recorder.rate, 44100)
        self.assertEqual(self.driver.open_rates, [16000, 48000, 44100])
        self.assertAlmostEqual(recorder.snapshot().seconds, 0.04)
        self.assertEqual({len(buffer.data) for buffer in recorder._buffers}, {3528})

    def test_failed_formats_do_not_start_capture(self):
        self.driver.open_results = [32, 32, 32]
        recorder = self.recorder()
        with self.assertRaisesRegex(RuntimeError, "Microphone error"):
            recorder.start()
        self.calls["waveInStart"].assert_not_called()
        self.calls["waveInClose"].assert_not_called()
        self.assertTrue(self.driver.event_closed)

    def test_prepare_add_and_start_failures_clean_up(self):
        for name in ("waveInPrepareHeader", "waveInAddBuffer", "waveInStart"):
            with self.subTest(name=name):
                self.driver.failures[name] = 1
                recorder = self.recorder()
                with self.assertRaisesRegex(RuntimeError, "Microphone error"):
                    recorder.start()
                self.assertFalse(recorder.handle.value)
                self.assertEqual(recorder._buffers, [])
                self.assertFalse(recorder.snapshot().active)
                self.driver.failures.clear()

    def test_reset_failure_retains_driver_memory_and_cancel_can_retry(self):
        recorder = self.recorder()
        recorder.start()
        retained_buffers = list(recorder._buffers)
        self.driver.failures["waveInReset"] = 1
        with self.assertRaisesRegex(RuntimeError, "Microphone error"):
            recorder.stop()
        self.assertTrue(recorder.handle.value)
        self.assertEqual(recorder._buffers, retained_buffers)
        self.assertFalse(self.driver.event_closed)
        self.calls["waveInUnprepareHeader"].assert_not_called()
        self.calls["waveInClose"].assert_not_called()
        self.assertIn(recorder, audio._retained)
        self.assertIn("Microphone error", recorder.snapshot().error)
        with self.assertRaisesRegex(RuntimeError, "cleanup"):
            recorder.start()
        self.driver.failures.clear()
        recorder.cancel()
        self.assertFalse(recorder.handle.value)
        self.assertNotIn(recorder, audio._retained)
        self.assertTrue(all(bytes(buffer.data) == bytes(len(buffer.data)) for buffer in retained_buffers))

    def test_gc_does_not_release_driver_owned_buffers_after_cleanup_failure(self):
        recorder = self.recorder()
        recorder.start()
        self.driver.failures["waveInReset"] = 1
        with self.assertRaises(RuntimeError):
            recorder.stop()
        reference = weakref.ref(recorder)
        self.recorders.remove(recorder)
        del recorder
        gc.collect()
        self.assertIsNotNone(reference())
        self.recorders.append(reference())

    def test_cancel_reports_repeated_cleanup_failure(self):
        recorder = self.recorder()
        recorder.start()
        self.driver.failures["waveInReset"] = 1
        with self.assertRaisesRegex(RuntimeError, "Microphone error"):
            recorder.cancel()
        self.assertTrue(recorder.handle.value)
        self.assertEqual(recorder._pcm, pcm(1000, 640))

    def test_successful_reset_without_returned_headers_is_not_assumed_safe(self):
        recorder = self.recorder()
        recorder.start()
        self.driver.reset_returns = False
        with self.assertRaisesRegex(RuntimeError, "did not return every queued buffer"):
            recorder.stop()
        self.assertTrue(recorder.handle.value)
        self.calls["waveInUnprepareHeader"].assert_not_called()
        self.driver.reset_returns = True
        recorder.cancel()

    def test_unprepare_failure_is_reported_and_only_failed_headers_are_retried(self):
        recorder = self.recorder()
        recorder.start()
        original = self.calls["waveInUnprepareHeader"].side_effect
        failed_once = False

        def unprepare(*args):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                return 1
            return original(*args)

        self.calls["waveInUnprepareHeader"].side_effect = unprepare
        with self.assertRaisesRegex(RuntimeError, "header cleanup failed"):
            recorder.stop()
        self.assertEqual(sum(buffer.prepared for buffer in recorder._buffers), 1)
        self.calls["waveInClose"].assert_not_called()
        recorder.cancel()
        self.assertEqual(self.calls["waveInUnprepareHeader"].call_count, 9)

    def test_close_failure_retains_callback_event_until_retry(self):
        recorder = self.recorder()
        recorder.start()
        self.driver.failures["waveInClose"] = 1
        with self.assertRaisesRegex(RuntimeError, "Microphone error"):
            recorder.stop()
        self.assertTrue(recorder.handle.value)
        self.assertFalse(self.driver.event_closed)
        self.assertTrue(recorder._buffers)
        self.driver.failures.clear()
        recorder.cancel()
        self.assertTrue(self.driver.event_closed)

    def test_invalid_returned_size_is_error_not_out_of_bounds_read(self):
        recorder = self.recorder()
        recorder.start()
        header = self.driver.complete(b"", notify=False)
        header.dwBytesRecorded = header.dwBufferLength + 2
        self.driver.SetEvent(456)
        self.wait_for(lambda: recorder._done.is_set())
        self.assertIn("invalid PCM", recorder.snapshot().error)
        with self.assertRaisesRegex(RuntimeError, "invalid PCM"):
            recorder.stop()
        self.assertFalse(recorder.handle.value)

    def test_sample_budget_bounds_memory_without_truncating_the_last_buffer(self):
        recorder = self.recorder()
        # Exercise the exact production budgeting code with a non-40ms-aligned cap.
        with patch.object(audio, "MAX_SECONDS", 0.333):
            recorder.start()
            expected = pcm(1000, 640)
            while not recorder._done.is_set():
                with self.driver.lock:
                    pending = bool(self.driver.pending)
                    samples = self.driver.pending[0].dwBufferLength // 2 if pending else 0
                if pending:
                    block = pcm(2000, samples)
                    self.driver.complete(block)
                    expected += block
                else:
                    time.sleep(0.001)
            self.assertTrue(recorder.snapshot().limit_reached)
            self.assertFalse(recorder.snapshot().active)
            self.assertEqual(len(recorder._pcm), int(16000 * 0.333) * 2)
            self.assertEqual(sum(self.driver.add_sizes), int(16000 * 0.333) * 2)
            self.assertEqual(self.driver.add_sizes[-1], 416)
            self.assertEqual(self.wave_pcm(recorder.stop())[1], expected)
        self.assertEqual(audio.MAX_SECONDS, 120)

    def test_full_120_second_capture_budget_and_pcm_sequence(self):
        recorder = self.recorder()
        recorder.start()
        expected = bytearray(pcm(1000, 640))
        deadline = time.monotonic() + 10
        value = -3000
        while not recorder._done.is_set():
            self.assertLess(time.monotonic(), deadline)
            with self.driver.lock:
                samples = self.driver.pending[0].dwBufferLength // 2 if self.driver.pending else 0
            if samples:
                block = pcm(value, samples)
                self.driver.complete(block)
                expected.extend(block)
                value = 3000 if value < 0 else -3000
            else:
                time.sleep(0.001)
        status = recorder.snapshot()
        self.assertTrue(status.limit_reached)
        self.assertFalse(status.active)
        self.assertEqual(status.seconds, 120)
        self.assertEqual(len(recorder._pcm), 16000 * 120 * 2)
        self.assertEqual(sum(self.driver.add_sizes), 16000 * 120 * 2)
        self.assertEqual(self.wave_pcm(recorder.stop())[1], expected)
        self.calls["waveInOpen"].assert_called_once()

    def test_event_creation_failure_does_not_open_device(self):
        self.calls["CreateEvent"].side_effect = None
        self.calls["CreateEvent"].return_value = 0
        recorder = self.recorder()
        with self.assertRaises(RuntimeError):
            recorder.start()
        self.assertFalse(recorder.snapshot().active)
        self.calls["waveInOpen"].assert_not_called()
        self.assertNotIn(recorder, audio._retained)

    def test_event_wait_failure_is_reported_and_cleans_up(self):
        self.calls["WaitForSingleObject"].side_effect = None
        self.calls["WaitForSingleObject"].return_value = 0xFFFFFFFF
        recorder = self.recorder()
        with self.assertRaises(RuntimeError):
            recorder.start()
        self.assertTrue(recorder.snapshot().error)
        self.assertFalse(recorder.snapshot().active)
        self.assertFalse(recorder.handle.value)
        self.assertTrue(self.driver.event_closed)

    def test_event_close_failure_is_retained_and_cancel_reports_then_retries(self):
        recorder = self.recorder()
        recorder.start()
        self.driver.failures["CloseHandle"] = 1
        with self.assertRaises(RuntimeError):
            recorder.cancel()
        self.assertFalse(recorder.handle.value)
        self.assertEqual(recorder._event, 456)
        self.assertIn(recorder, audio._retained)
        self.assertTrue(recorder.snapshot().error)
        self.driver.failures.clear()
        recorder.cancel()
        self.assertIsNone(recorder._event)
        self.assertNotIn(recorder, audio._retained)

    def test_shutdown_signal_failure_is_not_swallowed(self):
        recorder = self.recorder()
        recorder.start()
        self.calls["SetEvent"].side_effect = None
        self.calls["SetEvent"].return_value = 0
        with self.assertRaises(RuntimeError):
            recorder.cancel()
        self.assertFalse(recorder.handle.value)
        self.assertTrue(recorder.snapshot().error)
        recorder.cancel()

    def test_cleanup_timeout_keeps_memory_until_native_call_finishes(self):
        recorder = self.recorder()
        recorder.start()
        entered = threading.Event()
        release = threading.Event()
        original = self.calls["waveInReset"].side_effect

        def reset(*args):
            entered.set()
            release.wait(2)
            return original(*args)

        self.calls["waveInReset"].side_effect = reset
        buffers = list(recorder._buffers)
        try:
            with patch.object(audio, "FINISH_TIMEOUT", 0.05):
                with self.assertRaisesRegex(RuntimeError, "cleanup timed out"):
                    recorder.cancel()
            self.assertTrue(entered.is_set())
            self.assertTrue(recorder.handle.value)
            self.assertFalse(recorder.snapshot().active)
            self.assertEqual(recorder._buffers, buffers)
            self.assertIn(recorder, audio._retained)
            self.calls["waveInUnprepareHeader"].assert_not_called()
        finally:
            release.set()
            self.assertTrue(recorder._done.wait(2))
        recorder.cancel()
        self.assertFalse(recorder.handle.value)
        self.assertNotIn(recorder, audio._retained)

    def test_silence_still_fails_the_existing_usable_audio_guard_on_stop(self):
        self.driver.initial_value = 0
        recorder = self.recorder()
        recorder.start()
        for _ in range(7):
            self.send(recorder, pcm(0, 640))
        with self.assertRaisesRegex(RuntimeError, "No usable audio"):
            recorder.stop()
        self.assertFalse(recorder.handle.value)

    def test_too_short_recording_is_rejected_only_after_cleanup(self):
        recorder = self.recorder()
        recorder.start()
        with self.assertRaisesRegex(RuntimeError, "too short"):
            recorder.stop()
        self.assertFalse(recorder.handle.value)
        self.assertEqual(recorder._buffers, [])

    def test_diagnostics_remain_query_only(self):
        result = audio.diagnostics()
        self.assertEqual(result, {"input_device_count": 1, "supported_mono_pcm_rates": [16000, 48000, 44100]})
        self.assertFalse(self.driver.opened)
        self.calls["waveInStart"].assert_not_called()
        self.calls["CreateEvent"].assert_not_called()

    def test_concurrent_stop_and_cancel_serialize_and_leave_no_worker(self):
        recorder = self.recorder()
        recorder.start()
        for _ in range(7):
            self.send(recorder, pcm(1000, 640))
        errors = []

        def stop():
            try:
                recorder.stop()
            except RuntimeError as exc:
                errors.append(str(exc))

        stopper = threading.Thread(target=stop)
        canceller = threading.Thread(target=recorder.cancel)
        stopper.start()
        canceller.start()
        stopper.join(2)
        canceller.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(canceller.is_alive())
        self.assertTrue(not errors or errors == ["Not recording."])
        self.assertFalse(recorder.handle.value)
        self.calls["waveInClose"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
