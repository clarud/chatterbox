"""No real DLLs, windows, tray icons, audio, clipboard or network are used."""
import ctypes as C
from ctypes import wintypes as W
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from chatterbox import tray as T


class FakeNative:
    """A native API fake with an actual Python-only thread message queue."""
    def __init__(self):
        self.messages = queue.Queue()
        self.notifications = []
        self.operations = []
        self.callback = None
        names = ("GetModuleHandleW", "GetCurrentThreadId", "RegisterClassW", "UnregisterClassW",
                 "CreateWindowExW", "DefWindowProcW", "DestroyWindow", "RegisterWindowMessageW",
                 "LoadIconW", "LoadImageW", "DestroyIcon", "GetMessageW", "PeekMessageW", "TranslateMessage", "DispatchMessageW",
                 "PostMessageW", "PostThreadMessageW", "PostQuitMessage", "EndMenu", "CreatePopupMenu",
                 "AppendMenuW", "CheckMenuRadioItem", "DestroyMenu", "GetCursorPos",
                 "SetForegroundWindow", "TrackPopupMenuEx", "Shell_NotifyIconW")
        for name in names:
            setattr(self, name, Mock(return_value=1))
        self.GetModuleHandleW.return_value = 10
        self.GetCurrentThreadId.return_value = 11
        self.CreateWindowExW.return_value = 20
        self.LoadIconW.return_value = 30
        self.CreatePopupMenu.return_value = 40
        self.RegisterWindowMessageW.return_value = 0xC123
        self.DefWindowProcW.return_value = 99
        self.TrackPopupMenuEx.return_value = 0
        self.RegisterClassW.side_effect = self.register_class
        self.Shell_NotifyIconW.side_effect = self.notify
        self.GetMessageW.side_effect = self.get_message
        self.DispatchMessageW.side_effect = self.dispatch
        self.PostMessageW.side_effect = self.post
        self.PostThreadMessageW.side_effect = lambda tid, msg, wp, lp: self.post(None, msg, wp, lp)
        self.PostQuitMessage.side_effect = lambda code: self.messages.put((None, T.WM_QUIT, 0, 0))
        self.GetCursorPos.side_effect = self.cursor
        for name in ("CreateWindowExW", "LoadIconW", "DestroyWindow", "UnregisterClassW"):
            result = getattr(self, name).return_value
            getattr(self, name).side_effect = self.record(name, result)

    def record(self, name, result):
        def call(*args):
            self.operations.append((name, threading.get_ident()))
            return result
        return call

    def register_class(self, pointer):
        self.callback = C.cast(pointer, C.POINTER(T._WNDCLASSW)).contents.lpfnWndProc
        self.operations.append(("RegisterClassW", threading.get_ident()))
        return 1

    def notify(self, action, pointer):
        data = T._NOTIFYICONDATAW.from_buffer_copy(C.string_at(pointer, C.sizeof(T._NOTIFYICONDATAW)))
        self.notifications.append((action, data))
        self.operations.append(("notify:" + str(action), threading.get_ident()))
        return 1

    def post(self, hwnd, message, wparam, lparam):
        self.messages.put((hwnd, message, wparam, lparam))
        return 1

    def get_message(self, pointer, hwnd, low, high):
        item = self.messages.get(timeout=2)
        if item == -1:
            return -1
        hwnd, message, wparam, lparam = item
        if message == T.WM_QUIT:
            return 0
        msg = C.cast(pointer, C.POINTER(W.MSG)).contents
        msg.hWnd, msg.message, msg.wParam, msg.lParam = hwnd, message, wparam, lparam
        return 1

    def dispatch(self, pointer):
        msg = C.cast(pointer, C.POINTER(W.MSG)).contents
        return self.callback(msg.hWnd, msg.message, msg.wParam, msg.lParam)

    def cursor(self, pointer):
        point = C.cast(pointer, C.POINTER(W.POINT)).contents
        point.x, point.y = 120, 240
        return 1


class TrayTests(unittest.TestCase):
    def setUp(self):
        self.native = FakeNative()
        self.factory_patch = patch.object(T, "_Win32", return_value=self.native)
        self.factory = self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)
        self.emit = Mock()
        self.tray = T.SystemTray(self.emit)
        self.addCleanup(self.tray.close)

    def wait_until(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(condition(), "Mocked tray operation did not complete")

    def bare(self, version4=True):
        self.tray._native = self.native
        self.tray._hwnd = 20
        self.tray._icon = 30
        self.tray._taskbar_created = 0xC123
        self.tray._version4 = version4
        return self.tray

    def actions(self):
        return [action for action, data in self.native.notifications]

    def test_constructor_is_lazy_and_close_before_start_is_terminal(self):
        self.factory.assert_not_called()
        self.assertFalse(self.tray.available)
        self.assertEqual(self.tray.error, "")
        self.tray.close()
        self.tray.close()
        self.assertFalse(self.tray.start())
        self.factory.assert_not_called()
        self.emit.assert_not_called()

    def test_native_lifecycle_is_owned_hidden_and_idempotent(self):
        main_id = threading.get_ident()
        self.assertTrue(self.tray.start())
        self.assertTrue(self.tray.available)
        self.assertEqual(self.tray.error, "")
        self.assertTrue(self.tray.start())
        self.factory.assert_called_once()
        self.native.RegisterWindowMessageW.assert_called_once_with("TaskbarCreated")
        args = self.native.CreateWindowExW.call_args.args
        self.assertEqual(args[0], T.WS_EX_TOOLWINDOW)
        self.assertEqual(args[3], 0)  # Not WS_VISIBLE.
        self.assertIsNone(args[8])  # Top-level, not HWND_MESSAGE.
        self.assertEqual(self.actions(), [T.NIM_ADD, T.NIM_SETVERSION])
        add, version = [data for _, data in self.native.notifications]
        self.assertEqual(add.cbSize, C.sizeof(T._NOTIFYICONDATAW))
        self.assertEqual(add.hWnd, 20)
        self.assertEqual(add.uID, T.ICON_ID)
        self.assertEqual(add.uCallbackMessage, T.WM_TRAY)
        self.assertEqual(add.uFlags, T.NIF_MESSAGE | T.NIF_ICON | T.NIF_TIP | T.NIF_SHOWTIP)
        self.assertEqual(add.szInfo, "")
        self.assertEqual(add.szInfoTitle, "")
        self.assertEqual(add.dwInfoFlags, 0)
        self.assertFalse(add.hBalloonIcon)
        self.assertEqual(version.uVersion, T.NOTIFYICON_VERSION_4)
        self.tray.close()
        self.tray.close()
        self.assertFalse(self.tray.available)
        self.assertFalse(self.tray._thread.is_alive())
        self.assertEqual(self.actions(), [T.NIM_ADD, T.NIM_SETVERSION, T.NIM_DELETE])
        self.native.DestroyWindow.assert_called_once_with(20)
        self.native.UnregisterClassW.assert_called_once_with(self.tray._class_name, 10)
        self.assertIsNone(self.tray._hwnd)
        self.assertIsNone(self.tray._icon)
        self.assertFalse(self.tray._registered)
        self.assertIsNotNone(self.tray._wndproc)  # Callback kept safe through native teardown.
        ids = {tid for name, tid in self.native.operations}
        self.assertEqual(len(ids), 1)
        self.assertNotIn(main_id, ids)
        self.emit.assert_not_called()

    def test_updates_before_start_and_async_tooltip_only_modification(self):
        self.tray.update("raw", "Recording", True)
        self.assertTrue(self.tray.start())
        self.assertEqual(self.native.notifications[0][1].szTip, "Chatterbox | Raw | Recording (busy)")
        self.tray.update("polished", "Refining")
        self.wait_until(lambda: T.NIM_MODIFY in self.actions())
        action, data = self.native.notifications[-1]
        self.assertEqual(action, T.NIM_MODIFY)
        self.assertEqual(data.szTip, "Chatterbox | Polished | Refining")
        self.assertEqual(data.uFlags, T.NIF_TIP | T.NIF_SHOWTIP)
        self.assertEqual(data.szInfo, "")
        self.tray.close()
        count = len(self.native.notifications)
        self.tray.update("raw", "Recording", True)
        self.assertEqual(len(self.native.notifications), count)

    def test_tooltip_never_stores_arbitrary_transcript_or_key(self):
        secret = "sk-secret-key transcript\n" + "x" * 500
        self.tray.update(secret, secret, True)
        self.assertEqual(self.tray._mode, "polished")
        self.assertEqual(self.tray._state, "Processing")
        self.assertEqual(self.tray._tooltip(), "Chatterbox | Polished | Processing (busy)")
        self.tray.update("raw", secret)
        self.assertEqual(self.tray._tooltip(), "Chatterbox | Raw | Ready")
        self.tray.update("RAW", "idle")
        self.assertEqual(self.tray._tooltip(), "Chatterbox | Raw | Ready")
        self.tray.update(None, None)
        self.assertLessEqual(len(self.tray._tooltip()), 127)

    def test_version4_mouse_and_keyboard_show_emit_on_message_thread(self):
        thread_ids = []
        self.emit.side_effect = lambda kind, value: thread_ids.append(threading.get_ident())
        self.assertTrue(self.tray.start())
        for event in (T.NIN_SELECT, T.NIN_KEYSELECT):
            self.native.post(20, T.WM_TRAY, 0, (T.ICON_ID << 16) | event)
        self.wait_until(lambda: self.emit.call_count == 2)
        self.assertEqual(self.emit.call_args_list, [unittest.mock.call("tray_show", None)] * 2)
        self.assertEqual(set(thread_ids), {self.tray._thread.ident})
        # v4 does not also react to legacy UP, or callbacks for another icon.
        self.tray._window_proc(20, T.WM_TRAY, 0, (T.ICON_ID << 16) | T.WM_LBUTTONUP)
        self.tray._window_proc(20, T.WM_TRAY, 0, (2 << 16) | T.NIN_SELECT)
        self.assertEqual(self.emit.call_count, 2)

    def test_version4_context_coordinates_are_signed_and_keyboard_falls_back(self):
        tray = self.bare()
        with patch.object(tray, "_show_menu") as menu:
            xy = ((-30 & 0xFFFF) << 16) | (-120 & 0xFFFF)
            tray._window_proc(20, T.WM_TRAY, xy, (T.ICON_ID << 16) | T.WM_CONTEXTMENU)
            menu.assert_called_once_with(-120, -30)
        tray._show_menu(-1, -1)
        self.native.GetCursorPos.assert_called_once()
        self.assertEqual(self.native.TrackPopupMenuEx.call_args.args[2:4], (120, 240))

    def test_legacy_callback_protocol_if_version4_is_rejected(self):
        original = self.native.notify
        self.native.Shell_NotifyIconW.side_effect = lambda action, ptr: (
            original(action, ptr) and (action != T.NIM_SETVERSION))
        self.assertTrue(self.tray.start())
        self.assertFalse(self.tray._version4)
        self.native.post(20, T.WM_TRAY, T.ICON_ID, T.WM_LBUTTONUP)
        self.wait_until(lambda: self.emit.called)
        self.emit.assert_called_once_with("tray_show", None)
        with patch.object(self.tray, "_show_menu") as menu:
            self.tray._window_proc(20, T.WM_TRAY, T.ICON_ID, T.WM_RBUTTONUP)
            menu.assert_called_once_with()
            self.tray._window_proc(20, T.WM_TRAY, 2, T.WM_RBUTTONUP)
            self.assertEqual(menu.call_count, 1)

    def test_menu_order_radio_selection_controller_events_and_cleanup(self):
        tray = self.bare()
        self.native.TrackPopupMenuEx.return_value = T.CMD_RAW
        tray._show_menu(7, 8)
        items = [(call.args[2], call.args[3]) for call in self.native.AppendMenuW.call_args_list]
        self.assertEqual(items, [(T.CMD_SHOW, "Show"), (T.CMD_SETTINGS, "Settings"), (0, None),
                                 (T.CMD_RAW, "Raw"), (T.CMD_POLISHED, "Polished"), (0, None),
                                 (T.CMD_EXIT, "Exit")])
        self.native.CheckMenuRadioItem.assert_called_once_with(40, T.CMD_RAW, T.CMD_POLISHED,
                                                               T.CMD_POLISHED, 0)
        self.native.TrackPopupMenuEx.assert_called_once_with(
            40, T.TPM_RETURNCMD | T.TPM_NONOTIFY | T.TPM_RIGHTBUTTON, 7, 8, 20, None)
        self.native.SetForegroundWindow.assert_called_once_with(20)
        self.native.DestroyMenu.assert_called_once_with(40)
        self.native.PostMessageW.assert_called_with(20, T.WM_NULL, 0, 0)
        self.emit.assert_called_once_with("tray_mode", "raw")
        self.assertEqual(tray._mode, "polished")  # Controller confirms/persists mode.
        for command, event, value in ((T.CMD_SHOW, "tray_show", None),
                                     (T.CMD_SETTINGS, "tray_settings", None),
                                     (T.CMD_POLISHED, "tray_mode", "polished"),
                                     (T.CMD_EXIT, "tray_exit", None)):
            self.emit.reset_mock()
            tray._select(command)
            self.emit.assert_called_once_with(event, value)
        self.emit.reset_mock()
        tray._select(0)
        tray._select(999)
        self.emit.assert_not_called()

    def test_busy_modes_disabled_and_selection_race_rechecked(self):
        tray = self.bare()
        tray.update("raw", "Transcribing", True)
        self.native.TrackPopupMenuEx.return_value = T.CMD_POLISHED
        tray._show_menu()
        calls = self.native.AppendMenuW.call_args_list
        self.assertEqual(calls[3].args[1], T.MF_GRAYED)
        self.assertEqual(calls[4].args[1], T.MF_GRAYED)
        self.assertEqual(calls[0].args[1], T.MF_STRING)
        self.native.CheckMenuRadioItem.assert_called_with(40, T.CMD_RAW, T.CMD_POLISHED, T.CMD_RAW, 0)
        self.emit.assert_not_called()
        tray.update("polished", "Ready")
        def select_when_busy(*args):
            tray.update("polished", "Recording", True)
            return T.CMD_RAW
        self.native.TrackPopupMenuEx.side_effect = select_when_busy
        tray._show_menu()
        self.emit.assert_not_called()
        tray._select(T.CMD_SHOW)
        self.emit.assert_called_once_with("tray_show", None)

    def test_taskbar_created_readds_and_resets_version4(self):
        self.assertTrue(self.tray.start())
        self.tray.update("raw", "Recording", True)
        self.wait_until(lambda: T.NIM_MODIFY in self.actions())
        self.native.post(20, 0xC123, 0, 0)
        self.wait_until(lambda: self.actions().count(T.NIM_SETVERSION) == 2)
        self.assertEqual(self.actions().count(T.NIM_ADD), 2)
        self.assertTrue(self.tray.available)
        self.assertTrue(self.tray._version4)
        adds = [data for action, data in self.native.notifications if action == T.NIM_ADD]
        self.assertEqual(adds[-1].szTip, "Chatterbox | Raw | Recording (busy)")
        self.tray.close()
        self.assertEqual(self.actions().count(T.NIM_DELETE), 1)
        self.native.RegisterClassW.assert_called_once()

    def test_initialization_failures_are_safe_and_release_acquired_resources(self):
        cases = (("GetModuleHandleW", False, False),
                 ("RegisterWindowMessageW", False, False),
                 ("RegisterClassW", False, False),
                 ("CreateWindowExW", False, True),
                 ("LoadIconW", True, True))
        for name, window, registered in cases:
            with self.subTest(name=name):
                native = FakeNative()
                getattr(native, name).side_effect = None
                getattr(native, name).return_value = 0
                if name == "LoadIconW":
                    # Both the custom-icon path and its stock-icon fallback must fail
                    # for this case to actually exercise "no icon could be loaded".
                    native.LoadImageW.side_effect = None
                    native.LoadImageW.return_value = 0
                emit = Mock()
                tray = T.SystemTray(emit)
                with patch.object(T, "_Win32", return_value=native):
                    self.assertFalse(tray.start())
                    tray.close()
                    tray.close()
                self.assertFalse(tray.available)
                self.assertEqual(tray.error, T._UNAVAILABLE)
                emit.assert_called_once_with("tray_error", T._UNAVAILABLE)
                self.assertEqual(native.DestroyWindow.call_count, int(window))
                self.assertEqual(native.UnregisterClassW.call_count, int(registered))
                self.assertEqual(native.Shell_NotifyIconW.call_count, 0)

    def test_icon_add_failure_does_not_delete_an_unregistered_icon(self):
        self.native.Shell_NotifyIconW.side_effect = None
        self.native.Shell_NotifyIconW.return_value = 0
        self.assertFalse(self.tray.start())
        self.assertEqual(self.tray.error, T._UNAVAILABLE)
        self.native.DestroyWindow.assert_called_once()
        self.native.UnregisterClassW.assert_called_once()
        self.assertEqual(self.native.Shell_NotifyIconW.call_count, 1)

    def test_native_exception_text_is_not_exposed(self):
        self.factory.side_effect = OSError("private transcript sk-secret")
        self.assertFalse(self.tray.start())
        self.assertEqual(self.tray.error, T._UNAVAILABLE)
        self.emit.assert_called_once_with("tray_error", T._UNAVAILABLE)
        self.assertNotIn("secret", self.tray.error)

    def test_message_loop_failure_cleans_up(self):
        self.assertTrue(self.tray.start())
        self.native.messages.put(-1)
        self.wait_until(lambda: not self.tray._thread.is_alive())
        self.assertFalse(self.tray.available)
        self.assertEqual(self.tray.error, T._RUNTIME_ERROR)
        self.assertEqual(self.actions()[-1], T.NIM_DELETE)
        self.native.DestroyWindow.assert_called_once()
        self.native.UnregisterClassW.assert_called_once()

    def test_tooltip_and_explorer_restart_failures_clean_up(self):
        for message in (T.WM_UPDATE, 0xC123):
            with self.subTest(message=message):
                native, emit = FakeNative(), Mock()
                tray = T.SystemTray(emit)
                with patch.object(T, "_Win32", return_value=native):
                    self.assertTrue(tray.start())
                    original = native.notify
                    native.Shell_NotifyIconW.side_effect = lambda action, ptr: (
                        original(action, ptr) and action not in (T.NIM_MODIFY, T.NIM_ADD))
                    native.post(20, message, 0, 0)
                    self.wait_until(lambda: not tray._thread.is_alive())
                    tray.close()
                self.assertFalse(tray.available)
                self.assertEqual(tray.error, T._RUNTIME_ERROR)
                native.DestroyWindow.assert_called_once()
                native.UnregisterClassW.assert_called_once()
                emit.assert_called_once_with("tray_error", T._RUNTIME_ERROR)

    def test_menu_exception_is_caught_and_owned_menu_is_destroyed(self):
        tray = self.bare()
        self.native.AppendMenuW.side_effect = OSError("sk-secret transcript")
        result = tray._window_proc(20, T.WM_TRAY, 0, (T.ICON_ID << 16) | T.WM_CONTEXTMENU)
        self.assertEqual(result, 0)
        self.assertFalse(tray.available)
        self.assertEqual(tray.error, T._RUNTIME_ERROR)
        self.native.DestroyMenu.assert_called_once_with(40)
        self.native.EndMenu.assert_called_once()
        self.native.PostQuitMessage.assert_called_once_with(0)
        self.emit.assert_called_once_with("tray_error", T._RUNTIME_ERROR)

    def test_menu_creation_failure_and_cancel_are_non_ui_actions(self):
        tray = self.bare()
        tray._show_menu()
        self.emit.assert_not_called()
        self.native.CreatePopupMenu.return_value = 0
        tray._window_proc(20, T.WM_TRAY, 0, (T.ICON_ID << 16) | T.WM_CONTEXTMENU)
        self.emit.assert_called_once_with("tray_error", T._RUNTIME_ERROR)
        self.assertEqual(self.native.DestroyMenu.call_count, 1)

    def test_cleanup_attempts_all_resources_even_when_delete_raises(self):
        self.assertTrue(self.tray.start())
        original = self.native.notify
        def notify(action, pointer):
            if action == T.NIM_DELETE:
                raise OSError("private native error")
            return original(action, pointer)
        self.native.Shell_NotifyIconW.side_effect = notify
        self.tray.close()
        self.assertEqual(self.tray.error, T._CLEANUP_ERROR)
        self.native.DestroyWindow.assert_called_once()
        self.native.UnregisterClassW.assert_called_once()
        self.emit.assert_called_once_with("tray_error", T._CLEANUP_ERROR)

    def test_close_unblocks_menu_and_unknown_message_delegates(self):
        tray = self.bare()
        self.assertEqual(tray._window_proc(20, 987, 2, 3), 99)
        self.native.DefWindowProcW.assert_called_with(20, 987, 2, 3)
        self.assertEqual(tray._window_proc(20, T.WM_STOP, 0, 0), 0)
        self.native.EndMenu.assert_called_once()
        self.native.PostQuitMessage.assert_called_once_with(0)
        tray._select(T.CMD_EXIT)
        self.emit.assert_not_called()

    def test_post_failure_stops_via_thread_queue(self):
        self.assertTrue(self.tray.start())
        self.native.PostMessageW.side_effect = None
        self.native.PostMessageW.return_value = 0
        self.tray.update("raw", "Ready")
        self.wait_until(lambda: not self.tray._thread.is_alive())
        self.assertFalse(self.tray.available)
        self.assertEqual(self.tray.error, T._RUNTIME_ERROR)
        self.native.PostThreadMessageW.assert_called_with(11, T.WM_QUIT, 0, 0)
        self.native.DestroyWindow.assert_called_once()

    def test_controller_callback_exception_never_escapes_ctypes_callback(self):
        tray = self.bare()
        self.emit.side_effect = RuntimeError("controller closing")
        self.assertEqual(tray._window_proc(20, T.WM_TRAY, 0, (T.ICON_ID << 16) | T.NIN_SELECT), 0)
        self.assertEqual(tray.error, "")

    def test_thread_start_failure_is_nonfatal_and_close_is_safe(self):
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("private error")):
            self.assertFalse(self.tray.start())
        self.tray.close()
        self.tray.close()
        self.assertEqual(self.tray.error, T._UNAVAILABLE)
        self.emit.assert_called_once_with("tray_error", T._UNAVAILABLE)
        self.factory.assert_not_called()

    def test_startup_timeout_cleans_partial_resources_without_late_icon(self):
        entered, release = threading.Event(), threading.Event()
        original = self.native.register_class
        def register(pointer):
            entered.set()
            release.wait(2)
            return original(pointer)
        self.native.RegisterClassW.side_effect = register
        def timeout(seconds):
            self.assertTrue(entered.wait(1))
            return False
        try:
            with patch.object(self.tray._ready, "wait", side_effect=timeout):
                self.assertFalse(self.tray.start())
        finally:
            release.set()
        self.tray.close()
        self.assertFalse(self.tray.available)
        self.assertIn("startup timed out", self.tray.error)
        self.assertFalse(self.tray._thread.is_alive())
        self.assertEqual(self.actions(), [])
        self.native.DestroyWindow.assert_called_once()
        self.native.UnregisterClassW.assert_called_once()
        self.assertEqual(self.emit.call_count, 1)

    def test_update_racing_initial_icon_registration_is_not_lost(self):
        original = self.native.notify
        def notify(action, pointer):
            result = original(action, pointer)
            if action == T.NIM_ADD:
                self.tray.update("raw", "Recording", True)
            return result
        self.native.Shell_NotifyIconW.side_effect = notify
        self.assertTrue(self.tray.start())
        self.assertEqual(self.actions(), [T.NIM_ADD, T.NIM_SETVERSION, T.NIM_MODIFY])
        self.assertEqual(self.native.notifications[-1][1].szTip,
                         "Chatterbox | Raw | Recording (busy)")
        self.emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
