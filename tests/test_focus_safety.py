import unittest
from unittest.mock import patch
from chatterbox.windows import WindowTarget, GUIThreadInfo, KeyboardInjectionError, send_paste


class FocusSafetyTests(unittest.TestCase):
    def setUp(self):
        self.target = WindowTarget(100, 1, 2, 123, caret_hwnd=456, caret_rect=(1, 2, 3, 4), guard_gui=True)
        self.info = GUIThreadInfo(hwndFocus=123, hwndCaret=456)
        self.info.rcCaret.left, self.info.rcCaret.top, self.info.rcCaret.right, self.info.rcCaret.bottom = 1, 2, 3, 4

    def check(self):
        with patch('chatterbox.windows.WindowTarget.validate'), patch('chatterbox.windows.GetForegroundWindow', return_value=100), patch('chatterbox.windows.focus_info', return_value=self.info):
            self.target.ensure_focused()

    def test_unchanged_focus_and_caret_are_accepted(self):
        self.check()

    def test_blink_bit_is_not_treated_as_menu(self):
        self.info.flags = 1
        self.check()

    def test_menu_blocks_paste(self):
        self.info.flags = 4
        with self.assertRaisesRegex(RuntimeError, 'menu'):
            self.check()

    def test_changed_caret_position_within_same_control_is_accepted(self):
        # Typing/scrolling in the still-focused field while dictation processes
        # moves the caret; that's expected, not a target change.
        self.info.rcCaret.top = 55
        self.check()

    def test_changed_caret_owner_blocks_paste(self):
        self.info.hwndCaret = 789
        with self.assertRaisesRegex(RuntimeError, 'caret'):
            self.check()

    def test_changed_native_focus_blocks_paste(self):
        self.info.hwndFocus = 999
        with self.assertRaisesRegex(RuntimeError, 'focus changed'):
            self.check()

    def test_partial_send_is_uncertain_and_releases_keys(self):
        with patch('chatterbox.windows.SendInput', side_effect=[2, 2]) as send:
            with self.assertRaises(KeyboardInjectionError) as caught:
                send_paste()
        self.assertTrue(caught.exception.possibly_sent)
        self.assertEqual(send.call_count, 2)

    def test_no_keys_accepted_is_definitely_not_sent(self):
        with patch('chatterbox.windows.SendInput', side_effect=[0, 2]):
            with self.assertRaises(KeyboardInjectionError) as caught:
                send_paste()
        self.assertFalse(caught.exception.possibly_sent)


if __name__ == '__main__':
    unittest.main()
