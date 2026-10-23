import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from xigpet import app, pet_core
from scripts.image_inputs import split_images, vision_messages
from scripts.screen_capture import request_screen_capture, screen_request_handler
import test_pet_core as core_tests


def capture_call():
    return {"tool_calls": [{"id": "screen", "type": "function", "function": {
        "name": "request_screen_capture", "arguments": json.dumps({"reason": "看看当前程序的界面"}),
    }}]}


def immediate_thread(target, args=(), **_kwargs):
    return SimpleNamespace(start=lambda: target(*args))


class ScreenToolTests(unittest.TestCase):
    setUp = core_tests.PetCoreTests.setUp

    def window(self):
        with patch.object(app.PetWindow, "_schedule_tick"):
            window = app.PetWindow()
        self.addCleanup(window.destroy)
        window._tray = MagicMock()
        window._tray.ready = threading.Event()
        window._tray.ready.set()
        window.update_idletasks()
        return window

    def ask(self, window):
        window._begin_turn()
        with patch.object(pet_core, "call_deepseek", side_effect=[capture_call(), {"content": "请确认一下，我再看看。"}]):
            window._worker("截图看看当前屏幕")
            window._drain_events()
        self.assertIsNotNone(window._active_screen_request)

    def test_tool_requests_only_once_per_reply_and_never_captures_by_itself(self):
        requested = []
        with patch.object(app.ImageGrab, "grab") as grab:
            self.assertEqual(request_screen_capture("看看屏幕")["status"], "unavailable")
            with screen_request_handler(requested.append):
                self.assertIn("error", request_screen_capture(None))
                self.assertEqual(request_screen_capture("看看程序")["status"], "awaiting_user_confirmation")
                request_screen_capture("不要重复弹窗")
            with screen_request_handler(requested.append):
                request_screen_capture("下一轮可以再次请求")
            grab.assert_not_called()
        self.assertEqual(requested, ["看看程序", "下一轮可以再次请求"])

    def test_agent_waits_for_confirmation_without_claiming_an_image_or_react_loop(self):
        requested, calls = [], []
        responses = [capture_call(), {"content": "我想看看，请在电脑上确认。"}]
        def api(messages, *_args, **kwargs):
            calls.append(copy.deepcopy(messages))
            return responses.pop(0)
        with screen_request_handler(requested.append), patch.object(pet_core, "call_deepseek", side_effect=api):
            pet_core.reply("帮我截图")
        self.assertEqual(len(calls), 2)
        self.assertEqual(requested, ["看看当前程序的界面"])
        tool_result = next(item for item in calls[1] if item["role"] == "tool")
        self.assertEqual(json.loads(tool_result["content"])["status"], "awaiting_user_confirmation")
        self.assertEqual([row["role"] for row in pet_core.read_history()], ["user", "assistant"])
        self.assertFalse((pet_core.HISTORY.parent / "images").exists())

    def test_foreground_decline_timeout_and_stale_confirmation_do_not_capture(self):
        window = self.window()
        with patch.object(app.ImageGrab, "grab") as grab:
            self.ask(window)
            token = window._active_screen_request
            window._tool_screen_dialog._answer(False)
            window._on_screen_answer(token, True)
            self.assertIsNone(window._tool_screen_dialog)
            self.ask(window)
            with patch.object(app.time, "monotonic", return_value=window._screen_request_until + 1):
                window._on_screen_answer(window._active_screen_request, True)
            grab.assert_not_called()
        self.assertFalse(window._capturing_screen)
        self.assertFalse((pet_core.HISTORY.parent / "images").exists())

    def test_foreground_approval_captures_once_restores_chat_and_feeds_vision(self):
        window = self.window()
        window._remove_placeholder()
        window.input.insert("1.0", "未发送草稿")
        bbox = (0, 0, 320, 240)
        observed = []
        def api(messages, *_args, **kwargs):
            observed.append(vision_messages(copy.deepcopy(messages), pet_core.HISTORY.parent / "images"))
            return core_tests.continuation() if "tools_override" in kwargs else {"content": "看到了蓝色的程序界面。"}
        with patch.object(app.ImageGrab, "grab", return_value=app.Image.new("RGB", (320, 240), "blue")) as grab:
            self.ask(window)
            grab.assert_not_called()
            token = window._active_screen_request
            with patch.object(app, "work_area", return_value=bbox):
                window._tool_screen_dialog._answer(True)
            self.assertEqual(window.state(), "withdrawn")
            window.after_cancel(window._capture_job)
            with patch.object(app.threading, "Thread", side_effect=immediate_thread), \
                 patch.object(pet_core, "call_deepseek", side_effect=api):
                window._launch_screen_capture(bbox)
                window._drain_events()
            grab.assert_called_once_with(bbox=bbox, all_screens=True)
            window._on_screen_answer(token, True)
            self.assertEqual(grab.call_count, 1)
        self.assertEqual(window.state(), "normal")
        self.assertEqual(window.input.get("1.0", "end-1c"), "未发送草稿")
        history = pet_core.read_history()
        self.assertEqual([row["role"] for row in history], ["user", "assistant", "user", "assistant"])
        self.assertTrue(split_images(history[2]["content"])[1])
        self.assertTrue(any(part.get("type") == "image_url" for row in observed[0]
                            if isinstance(row.get("content"), list) for part in row["content"]))

    def test_desktop_tool_request_uses_consent_bubble_and_can_be_cancelled(self):
        window = self.window()
        window._minimize_to_tray()
        with patch.object(app.ImageGrab, "grab") as grab:
            self.ask(window)
            self.assertIsNone(window._tool_screen_dialog)
            self.assertIn(app.SCREEN_REQUEST_TEXT, window._desktop.bubble_text)
            token = window._active_screen_request
            window._desktop._answer_bubble(False)
            window._on_screen_answer(token, True)
            grab.assert_not_called()

    def test_qq_request_only_opens_local_confirmation(self):
        window = self.window()
        line = pet_core.append_history("user", "帮我看看电脑屏幕")
        window._begin_turn()
        with patch.object(pet_core, "call_deepseek", side_effect=[capture_call(), {"content": "请在电脑上确认。"}]), \
             patch.object(app.ImageGrab, "grab") as grab:
            app.PetWindow._qq_reply_worker(window.events, "帮我看看电脑屏幕", line, window._queue_screen_request)
            window._drain_events()
            self.assertIsNotNone(window._tool_screen_dialog)
            grab.assert_not_called()
        window._tool_screen_dialog._answer(False)

    def test_capture_failure_restores_foreground_without_image_record(self):
        window = self.window()
        self.ask(window)
        window._tool_screen_dialog._answer(True)
        window.after_cancel(window._capture_job)
        with patch.object(app.ImageGrab, "grab", side_effect=OSError("fixture capture error")):
            window._screen_capture_worker((0, 0, 320, 240))
            window._drain_events()
        self.assertEqual(window.state(), "normal")
        self.assertFalse(window.busy)
        self.assertEqual([row["role"] for row in pet_core.read_history()], ["user", "assistant"])


if __name__ == "__main__":
    unittest.main()
