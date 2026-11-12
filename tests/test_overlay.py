from contextlib import nullcontext
import ctypes as C
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from chatterbox.audio import CaptureStatus
from chatterbox.overlay import EX_STYLE, FRAME_MS, FloatingOverlay, Waveform, overlay_bounds, monitor_dpi_context
from chatterbox.pipeline import State
from chatterbox.settings import Settings


class WaveformTests(unittest.TestCase):
    def test_silence_is_flat_with_no_fake_animation(self):
        wave = Waveform()
        for _ in range(30):
            wave.update(0)
        self.assertEqual(tuple(wave.values), (0.0,) * 13)

    def test_more_rms_produces_taller_bars(self):
        quiet, loud = Waveform(), Waveform()
        for _ in range(30):
            quiet.update(0.01)
            loud.update(0.05)
        self.assertGreater(loud.level, quiet.level)
        self.assertAlmostEqual(loud.level / quiet.level, (0.05 - 0.0015) / (0.01 - 0.0015), places=4)

    def test_attack_is_smoothed_and_release_returns_to_flat(self):
        wave = Waveform()
        wave.update(0.06)
        self.assertGreater(wave.level, 0)
        self.assertLess(wave.level, (0.06 - 0.0015) * 14)
        previous = wave.level
        wave.update(0)
        self.assertGreater(wave.level, 0)
        self.assertLess(wave.level, previous)
        for _ in range(25):
            wave.update(0)
        self.assertEqual(max(wave.values), 0)

    def test_noise_floor_negative_values_and_full_scale_are_bounded(self):
        wave = Waveform()
        wave.update(-1)
        wave.update(0.001)
        self.assertEqual(wave.level, 0)
        for _ in range(30):
            wave.update(1)
        self.assertTrue(all(0 <= value <= 1 for value in wave.values))

    def test_frame_target_is_25hz(self):
        self.assertEqual(1000 / FRAME_MS, 25)


class GeometryTests(unittest.TestCase):
    def test_bottom_center_uses_work_area_above_taskbar(self):
        self.assertEqual(overlay_bounds((0, 0, 1920, 1040)), (770, 958, 380, 58))

    def test_negative_monitor_origin(self):
        self.assertEqual(overlay_bounds((-1920, 0, 0, 1040)), (-1150, 958, 380, 58))
        self.assertEqual(overlay_bounds((-1920, 0, 0, 1040), position="bottom-right")[0], -404)

    def test_scaling_and_positions(self):
        self.assertEqual(overlay_bounds((0, 0, 1920, 1040), 144), (675, 917, 570, 87))
        self.assertEqual(overlay_bounds((0, 0, 1920, 1040), 144, "top-center")[1], 36)
        self.assertEqual(overlay_bounds((0, 0, 1920, 1040), 120, "bottom-left")[0], 30)

    def test_small_work_area_is_clamped(self):
        x, y, width, height = overlay_bounds((-200, -100, 100, 200), 288)
        self.assertGreaterEqual(x, -200)
        self.assertGreaterEqual(y, -100)
        self.assertLessEqual(x + width, 100)
        self.assertLessEqual(y + height, 200)

    def test_overlay_styles_prevent_activation_and_input_capture(self):
        for flag in (0x08000000, 0x80, 0x80000, 0x20, 0x08):
            self.assertEqual(EX_STYLE & flag, flag)

    def test_scoped_dpi_restores_previous_context_on_error(self):
        with patch("chatterbox.overlay.SetThreadDpiAwarenessContext", return_value=99) as setter:
            with self.assertRaises(RuntimeError), monitor_dpi_context():
                raise RuntimeError("test")
            self.assertEqual(setter.call_args_list[-1].args, (99,))


class OverlayLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root, self.recorder, self.error = Mock(), Mock(), Mock()
        self.root.after.return_value = "timer"
        self.recorder.snapshot.return_value = CaptureStatus(True, 1.2, 0.03, 100)
        self.patchers = []
        self.native = {}
        for name in ("RegisterClass", "UnregisterClass", "CreateWindow", "GetModuleHandle", "SetLayeredWindowAttributes",
                     "DestroyWindow", "ShowWindow", "InvalidateRect"):
            patcher = patch("chatterbox.overlay." + name, return_value=101 if name == "CreateWindow" else 1)
            self.native[name] = patcher.start()
            self.patchers.append(patcher)
        context = patch("chatterbox.overlay.monitor_dpi_context", return_value=nullcontext())
        context.start()
        self.patchers.append(context)
        self.overlay = FloatingOverlay(self.root, self.recorder, self.error)
        self.overlay._position = Mock()
        self.target = SimpleNamespace(hwnd=123)

    def tearDown(self):
        self.overlay.close()
        for patcher in reversed(self.patchers):
            patcher.stop()

    def test_initially_hidden_and_no_microphone_queries(self):
        self.assertFalse(self.overlay.visible)
        self.native["ShowWindow"].assert_not_called()
        self.recorder.snapshot.assert_not_called()
        self.overlay.phase(State.TRANSCRIBING)
        self.native["ShowWindow"].assert_not_called()

    def test_recording_only_shows_without_activation_and_reads_existing_stream(self):
        self.overlay.begin(self.target, "raw", Settings())
        self.native["ShowWindow"].assert_called_once_with(101, 4)
        self.overlay._tick()
        self.recorder.snapshot.assert_called_once()
        self.assertEqual(self.overlay.elapsed, 1.2)
        self.assertEqual(self.overlay.mode, "raw")
        self.recorder.start.assert_not_called()

    def test_processing_and_success_do_not_read_microphone(self):
        self.overlay.begin(self.target, "polished", Settings())
        self.overlay.phase(State.TRANSCRIBING)
        self.overlay._tick()
        self.overlay.phase(State.REFINING)
        self.overlay._tick()
        self.overlay.success()
        self.assertEqual(self.overlay.state, State.SUCCESS)
        self.assertGreater(self.overlay.deadline, 0)
        self.recorder.snapshot.assert_not_called()

    def test_idle_and_success_deadline_hide(self):
        self.overlay.begin(self.target, "polished", Settings())
        self.overlay.phase(State.IDLE)
        self.assertFalse(self.overlay.visible)
        self.overlay.success()
        self.overlay.deadline = 1
        self.overlay._tick()
        self.assertFalse(self.overlay.visible)
        self.assertFalse(self.overlay.active)

    def test_error_is_visible_then_times_out(self):
        self.overlay.begin(self.target, "polished", Settings())
        self.overlay.phase(State.ERROR)
        self.assertEqual(self.overlay.state, State.ERROR)
        self.assertGreater(self.overlay.deadline, 0)
        self.overlay.deadline = 1
        self.overlay._tick()
        self.assertFalse(self.overlay.visible)

    def test_stale_capture_reports_once(self):
        self.overlay.begin(self.target, "polished", Settings())
        self.recorder.snapshot.return_value = CaptureStatus(False, 1, 0, 1, "stale")
        self.overlay._tick()
        self.overlay._tick()
        self.error.assert_called_once_with("stale")

    def test_completed_capture_cap_is_not_reported_as_failure(self):
        self.overlay.begin(self.target, "raw", Settings())
        self.recorder.snapshot.return_value = CaptureStatus(False, 120, 0, 100, limit_reached=True)
        self.overlay._tick()
        self.error.assert_not_called()

    def test_raw_recovery_is_inserting_not_recording(self):
        self.overlay.resume_insertion(self.target, Settings())
        self.assertEqual(self.overlay.state, State.INSERTING)
        self.assertEqual(self.overlay.mode, "raw")
        self.overlay._tick()
        self.recorder.snapshot.assert_not_called()

    def test_mouse_hit_test_and_activation_are_refused(self):
        self.assertEqual(self.overlay._window_proc(101, 0x84, 0, 0), -1)
        self.assertEqual(self.overlay._window_proc(101, 0x21, 0, 0), 3)

    def test_close_cancels_timer_and_cleans_window_once(self):
        self.overlay.close()
        self.overlay.close()
        self.root.after_cancel.assert_called_once_with("timer")
        self.native["DestroyWindow"].assert_called_once_with(101)
        self.native["UnregisterClass"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
