from contextlib import nullcontext
import threading
import unittest
from unittest.mock import Mock, patch

from chatterbox.clipboard import paste_text


class ClipboardTransactionTests(unittest.TestCase):
    def setUp(self):
        self.target = Mock()
        self.snapshot = Mock()
        self.cancel = threading.Event()
        self.patches = []
        for name, value in (("opened", Mock(side_effect=lambda owner: nullcontext())),
                            ("Snapshot", Mock()), ("_install_text", Mock()),
                            ("GetClipboardSequenceNumber", Mock(return_value=42)),
                            ("owns_transaction", Mock(return_value=True)),
                            ("wait_for_modifiers", Mock()), ("send_paste", Mock())):
            patcher = patch("chatterbox.clipboard." + name, value)
            patcher.start()
            self.patches.append(patcher)
            setattr(self, name, value)
        self.Snapshot.capture.return_value = self.snapshot

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def paste(self):
        return paste_text("Hello.", self.target, 123, self.cancel, restore_delay=0)

    def test_preserve_and_restore_transaction(self):
        self.assertEqual(self.paste(), "")
        self.target.restore.assert_called_once()
        self._install_text.assert_called_once()
        self.assertEqual(self._install_text.call_args.args[0], "Hello.")
        self.assertEqual(len(self._install_text.call_args.args[1]), 16)
        self.send_paste.assert_called_once()
        self.snapshot.restore_locked.assert_called_once()
        self.snapshot.close.assert_called_once()

    def test_focus_refused_before_clipboard_change(self):
        self.target.restore.side_effect = RuntimeError("focus refused")
        with self.assertRaisesRegex(RuntimeError, "focus refused"):
            self.paste()
        self.Snapshot.capture.assert_not_called()
        self.send_paste.assert_not_called()

    def test_focus_changes_after_clipboard_replacement(self):
        self.target.ensure_focused.side_effect = [None, RuntimeError("focus changed")]
        with self.assertRaisesRegex(RuntimeError, "focus changed"):
            self.paste()
        self.send_paste.assert_not_called()
        self.snapshot.restore_locked.assert_called_once()

    def test_new_clipboard_is_not_overwritten(self):
        self.GetClipboardSequenceNumber.side_effect = [42, 42, 43]
        self.assertIn("newer clipboard was kept", self.paste())
        self.snapshot.restore_locked.assert_not_called()
        self.snapshot.close.assert_called_once()

    def test_clipboard_changed_before_paste_is_not_inserted(self):
        self.GetClipboardSequenceNumber.side_effect = [42, 43, 43]
        with self.assertRaisesRegex(RuntimeError, "Clipboard changed"):
            self.paste()
        self.send_paste.assert_not_called()
        self.snapshot.restore_locked.assert_not_called()
        self.snapshot.close.assert_called_once()

    def test_install_failure_rolls_back(self):
        self._install_text.side_effect = RuntimeError("set failed")
        with self.assertRaisesRegex(RuntimeError, "set failed"):
            self.paste()
        self.snapshot.restore_locked.assert_called_once()
        self.snapshot.close.assert_called_once()
        self.send_paste.assert_not_called()

    def test_cancelled_before_mutation(self):
        self.cancel.set()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.paste()
        self.Snapshot.capture.assert_not_called()

    def test_unsupported_snapshot_aborts_without_emptying(self):
        self.Snapshot.capture.side_effect = RuntimeError("unsupported clipboard")
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            self.paste()
        self._install_text.assert_not_called()
        self.send_paste.assert_not_called()

    def test_change_before_sequence_baseline_is_detected_by_token(self):
        self.owns_transaction.return_value = False
        with self.assertRaisesRegex(RuntimeError, "Clipboard changed"):
            self.paste()
        self.send_paste.assert_not_called()
        self.snapshot.restore_locked.assert_not_called()

    def test_cleanup_failure_after_send_is_marked_possibly_sent(self):
        from chatterbox.clipboard import InsertionError
        self.snapshot.restore_locked.side_effect = RuntimeError("restore failed")
        with self.assertRaises(InsertionError) as caught:
            self.paste()
        self.assertTrue(caught.exception.possibly_sent)
        self.assertIn("Do not retry", str(caught.exception))

    def test_partial_keyboard_send_preserves_uncertainty(self):
        from chatterbox.windows import KeyboardInjectionError
        from chatterbox.clipboard import InsertionError
        self.send_paste.side_effect = KeyboardInjectionError("partial batch", possibly_sent=True)
        with self.assertRaises(InsertionError) as caught:
            self.paste()
        self.assertTrue(caught.exception.possibly_sent)
        self.snapshot.restore_locked.assert_called_once()

    def test_definite_no_send_failure_is_retryable(self):
        from chatterbox.windows import KeyboardInjectionError
        from chatterbox.clipboard import InsertionError
        self.send_paste.side_effect = KeyboardInjectionError("no batch accepted", possibly_sent=False)
        with self.assertRaises(InsertionError) as caught:
            self.paste()
        self.assertFalse(caught.exception.possibly_sent)


class SnapshotSafetyTests(unittest.TestCase):
    def test_owner_bound_ole_data_is_refused_before_read_or_mutation(self):
        from chatterbox.clipboard import Snapshot
        with patch("chatterbox.clipboard.EnumClipboardFormats", return_value=0xC001), patch("chatterbox.clipboard.format_name", return_value="ole private data"), patch("chatterbox.clipboard.GetClipboardData") as read, patch("chatterbox.clipboard.EmptyClipboard") as empty:
            with self.assertRaisesRegex(RuntimeError, "unsupported owner/private"):
                Snapshot.capture()
            read.assert_not_called()
            empty.assert_not_called()


if __name__ == "__main__":
    unittest.main()
