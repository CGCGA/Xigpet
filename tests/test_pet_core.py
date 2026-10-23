import copy
import base64
import io
import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from xigpet import pet_core
from xigpet import app
from scripts import web_tools
from scripts import image_inputs
from scripts import drawing_tools
from xigpet.chat_text import limit_sentences, naturalize_text, split_bubbles
from xigpet.daily_plan import DailyPlan


def continuation(keep_going=False, focus="", reaction=""):
    return {"tool_calls": [{"id": "decision", "type": "function", "function": {
        "name": "decide_next_reply", "arguments": json.dumps({"continue_reply": keep_going, "focus": focus, "reaction": reaction,
        "response_scope": "extended" if keep_going else "chat", "request_complete": not keep_going,
        "remaining_request": focus if keep_going else ""}, ensure_ascii=False),
    }}]}


class PetCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=pet_core.ROOT)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.patches = [
            patch.object(pet_core, "SETTING", root / "config" / "setting.md"),
            patch.object(pet_core, "KEY_FILE", root / "config" / "deepseek_key.txt"),
            patch.object(pet_core, "IMPORTANT", root / "memory" / "important.md"),
            patch.object(pet_core, "HISTORY", root / "memory" / "chat_history.jsonl"),
            patch.object(pet_core, "CATALOG", root / "memory" / "catalog.jsonl"),
            patch.object(pet_core, "CATALOG_STATE", root / "memory" / ".catalog_state.json"),
            patch.object(pet_core, "PLAN_DIR", root / "plans"),
            patch.object(pet_core, "NOTES_DIR", root / "memory" / "notes"),
            patch.object(pet_core, "MEDIA_DIR", root / "media"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        pet_core.ensure_files()
        pet_core.SETTING.write_text("你叫测试宠物，使用中文。", encoding="utf-8")
        pet_core.KEY_FILE.write_text("test-key", encoding="utf-8")

    def _shared_image(self, color="red"):
        return image_inputs.save_image(app.Image.new("RGB", (160, 96), color), pet_core.HISTORY.parent / "images")

    def _drawing_call(self, code="background('#eaf5ff')\ndraw.ellipse((60,60,300,300),fill='#f9cc82')"):
        return {"tool_calls": [{"id": "draw", "type": "function", "function": {
            "name": "draw_picture", "arguments": json.dumps({"title": "一朵小花", "code": code, "width": 384, "height": 384}),
        }}]}

    def test_model_drawing_tool_sends_caption_then_actual_image(self):
        responses = [self._drawing_call(), {"content": "我画了一朵小花。"}, continuation()]
        sent = []
        with patch.object(pet_core, "call_deepseek", side_effect=lambda *_args, **_kwargs: responses.pop(0)):
            pet_core.reply("画点东西送给我吧", on_reply=sent.append)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], "我画了一朵小花。")
        text, names = app.split_media(sent[1])
        self.assertEqual(text, "")
        self.assertEqual(len(names), 1)
        with app.Image.open(pet_core.MEDIA_DIR / names[0]) as image:
            self.assertEqual(image.size, (384, 384))
        self.assertEqual([row["role"] for row in pet_core.read_history()], ["user", "assistant", "assistant"])

    def test_idle_drawing_requires_tool_and_creates_no_fake_user_turn(self):
        responses = [self._drawing_call(), {"content": "送给你呀。"}]
        calls, sent = [], []
        def api(messages, key, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)
        with patch.object(pet_core, "call_deepseek", side_effect=api):
            pet_core.create_idle_drawing(on_reply=sent.append)
        self.assertEqual(calls[0]["tool_choice"]["function"]["name"], "draw_picture")
        self.assertTrue(sent[0].startswith("我画了"))
        self.assertTrue(app.split_media(sent[1])[1])
        self.assertEqual(pet_core.read_history(), [])
        self.assertEqual(len(calls), 2)

    def test_failed_drawings_stop_at_tool_loop_limit_and_can_retry_more_than_twice(self):
        sent = []
        with patch.object(pet_core, "call_deepseek", side_effect=[self._drawing_call("import os") for _ in range(pet_core.MAX_TOOL_ROUNDS)]) as api:
            with self.assertRaises(pet_core.PetError):
                pet_core.create_idle_drawing(on_reply=sent.append)
        self.assertEqual(api.call_count, pet_core.MAX_TOOL_ROUNDS)
        self.assertEqual(sent, [])
        self.assertEqual(pet_core.read_history(), [])
        self.assertEqual(list(pet_core.MEDIA_DIR.glob("drawings/drawing_*.png")), [])
        with patch.object(pet_core, "call_deepseek", side_effect=[self._drawing_call("import os"), self._drawing_call("open('not-allowed')"), self._drawing_call(), {"content": "我画了一朵小花。"}]):
            pet_core.create_idle_drawing(on_reply=sent.append)
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(list(pet_core.MEDIA_DIR.glob("drawings/drawing_*.png"))), 1)

    def test_multiple_drawings_in_one_reply_have_no_one_or_three_picture_quota(self):
        calls = []
        for i in range(4):
            call = self._drawing_call()["tool_calls"][0]
            call["id"] = f"drawing-{i}"
            arguments = json.loads(call["function"]["arguments"])
            arguments["title"] = f"小花{i}"
            call["function"]["arguments"] = json.dumps(arguments)
            calls.append(call)
        sent = []
        with patch.object(pet_core, "call_deepseek", side_effect=[
            {"tool_calls": calls}, {"content": "四幅小花都画好啦。"}, continuation(),
        ]):
            pet_core.reply("请画四幅不同的小花。", on_reply=sent.append)
        pictures = [name for content in sent for name in app.split_media(content)[1]]
        self.assertEqual(len(pictures), 4)
        self.assertEqual(len(set(pictures)), 4)
        self.assertEqual(len(list(pet_core.MEDIA_DIR.glob("drawings/drawing_*.png"))), 4)
        self.assertEqual(sent[0], "四幅小花都画好啦。")
        self.assertTrue(all((pet_core.MEDIA_DIR / name).is_file() for name in pictures))

    def test_three_existing_images_do_not_block_drawing(self):
        calls = []
        for i in range(3):
            filename = f"sample_{i}.png"
            app.Image.new("RGB", (32, 32), "blue").save(pet_core.MEDIA_DIR / filename)
            calls.append({"id": f"media-{i}", "type": "function", "function": {
                "name": "send_media", "arguments": json.dumps({"filename": filename, "purpose": "requested"}),
            }})
        calls += self._drawing_call()["tool_calls"]
        sent, results = [], []
        responses = [{"tool_calls": calls}, {"content": "这些图片和新画都送给你。"}, continuation()]
        def api(messages, *_args, **_kwargs):
            results.extend(row["content"] for row in messages if row.get("role") == "tool")
            return responses.pop(0)
        with patch.object(pet_core, "call_deepseek", side_effect=api):
            pet_core.reply("分享三张图片，再画一幅画。", on_reply=sent.append)
        pictures = [name for content in sent for name in app.split_media(content)[1]]
        self.assertEqual(len(pictures), 4)
        self.assertEqual(sum(name.startswith("drawings/drawing_") for name in pictures), 1)
        self.assertFalse(any("最多" in result for result in results))

    def test_continuation_has_fresh_tool_loop_and_can_draw_after_previous_drawing(self):
        time_calls = [{"tool_calls": [{"id": f"clock-{i}", "type": "function", "function": {
            "name": "get_current_time", "arguments": "{}",
        }}]} for i in range(pet_core.MAX_TOOL_ROUNDS - 1)]
        responses = [*time_calls, self._drawing_call(), {"content": "先画一朵小花。"},
                     continuation(True, "按用户要求再画一幅不同颜色的花"), self._drawing_call(),
                     {"content": "第二幅也送给你。"}, continuation()]
        allowed, sent = [], []
        def api(_messages, _key, **kwargs):
            if "tools_override" not in kwargs:
                allowed.append(kwargs["allow_tools"])
            return responses.pop(0)
        with patch.object(pet_core, "call_deepseek", side_effect=api):
            pet_core.reply("画两幅小花给我。", on_reply=sent.append)
        self.assertEqual(allowed[pet_core.MAX_TOOL_ROUNDS], False)
        self.assertEqual(allowed[pet_core.MAX_TOOL_ROUNDS + 1], True)
        self.assertEqual(sum(len(app.split_media(content)[1]) for content in sent), 2)
        self.assertEqual(len(list(pet_core.MEDIA_DIR.glob("drawings/drawing_*.png"))), 2)
        self.assertTrue(any("第二幅" in content for content in sent))

    def test_new_user_message_can_draw_after_previous_drawing(self):
        responses = [self._drawing_call(), {"content": "小花画好了。"}, continuation(),
                     self._drawing_call(), {"content": "新的小花也画好了。"}, continuation()]
        with patch.object(pet_core, "call_deepseek", side_effect=responses):
            first = pet_core.reply("画一幅小花。")
            second = pet_core.reply("再画一幅。")
        self.assertIn("drawing_", first)
        self.assertIn("drawing_", second)
        self.assertEqual(len(list(pet_core.MEDIA_DIR.glob("drawings/drawing_*.png"))), 2)

    def test_drawing_fallback_only_describes_pictures_from_current_generation(self):
        state = pet_core.TurnState()
        with patch.object(pet_core, "call_deepseek", side_effect=[self._drawing_call(), {"content": "画好了。"}]):
            pet_core._generate_reply([], "test-key", state)
        app.Image.new("RGB", (32, 32), "blue").save(pet_core.MEDIA_DIR / "sample.png")
        send = {"tool_calls": [{"id": "photo", "type": "function", "function": {
            "name": "send_media", "arguments": '{"filename":"sample.png"}',
        }}]}
        with patch.object(pet_core, "call_deepseek", side_effect=[send, pet_core.PetError("network failed")]):
            with self.assertRaises(pet_core.PetError):
                pet_core._generate_reply([], "test-key", state)

    def test_completed_drawing_is_delivered_if_final_model_request_fails(self):
        sent = []
        with patch.object(pet_core, "call_deepseek", side_effect=[self._drawing_call(), pet_core.PetError("network failed")]):
            pet_core.create_idle_drawing(on_reply=sent.append)
        self.assertEqual(sent[0], "我画了一朵小花。")
        self.assertTrue(app.split_media(sent[1])[1])

    def test_moegirl_search_returns_public_results_excerpt_and_source(self):
        data = ["初音", ["初音未来"], [""], ["https://zh.moegirl.org.cn/example"]]
        with patch.object(web_tools, "_get_json", return_value=data) as fetch, \
             patch.object(web_tools, "_get_moegirl_excerpt", return_value="初音未来是虚拟歌姬。"):
            result = json.loads(pet_core.execute_tool("search_moegirl", {"query": "初音"}))
        self.assertEqual(result[0]["source"], "萌娘百科")
        self.assertEqual(result[0]["read_status"], "article_excerpt")
        self.assertIn("虚拟歌姬", result[0]["excerpt"])
        self.assertTrue(result[0]["url"].startswith("https://zh.moegirl.org.cn/"))
        self.assertIn("action=opensearch", fetch.call_args.args[0])
        with patch.object(web_tools, "_get_json", side_effect=RuntimeError("blocked")):
            error = json.loads(pet_core.execute_tool("search_moegirl", {"query": "初音"}))
        self.assertIn("error", error)
        self.assertIn("search_url", error)
        parser = web_tools._ArticleText()
        parser.feed('<nav><p>菜单</p></nav><div class="mw-parser-output"><table><tr><td><p>导航</p></td></tr></table><p>条目<span>正文</span><sup>1</sup></p></div>')
        self.assertEqual("".join(parser.parts).strip(), "条目正文")

    def test_idle_drawing_only_shows_bubble_without_history_chat_or_qq(self):
        window = app.PetWindow()
        window._tray = MagicMock()
        window._qq_bridge = MagicMock()
        def immediate_thread(target, args=(), **_kwargs):
            return SimpleNamespace(start=lambda: target(*args))
        before = list(window.messages)
        try:
            window._minimize_to_tray()
            window._next_idle_at = 0
            with patch.object(app.random, "choice", return_value=app.IDLE_DRAW_TEXT), \
                 patch.object(app.threading, "Thread", side_effect=immediate_thread), \
                 patch.object(pet_core, "call_deepseek", side_effect=[self._drawing_call(), {"content": "我画了一朵小花。"}]) as model:
                window._desktop_tick()
                self.assertTrue(window.busy)
                self.assertIn("想画", window._desktop.bubble_text)
                window._drain_events()
            self.assertEqual(model.call_count, 2)
            self.assertIn("我画了", window._desktop.bubble_text)
            self.assertNotIn("[[pet-media:", window._desktop.bubble_text)
            self.assertEqual(len(list(pet_core.MEDIA_DIR.glob("drawings/drawing_*.png"))), 1)
            self.assertEqual(window.messages, before)
            self.assertEqual(pet_core.read_history(), [])
            window._qq_bridge.send.assert_not_called()
            window._tray.notify.assert_not_called()
            self.assertIsNone(window._delivery_job)
            self.assertFalse(window.busy)
        finally:
            window.destroy()

    def test_plain_idle_bubbles_never_enter_chat_or_qq(self):
        window = app.PetWindow()
        self.addCleanup(window.destroy)
        window._tray = MagicMock()
        window._minimize_to_tray()
        before = list(window.messages)
        ordinary = [text for text in app.IDLE_REMINDER_TEXTS
                    if text not in (app.IDLE_DRAW_TEXT, app.SCREEN_REQUEST_TEXT)]
        for qq_enabled in (False, True):
            bridge = MagicMock()
            window._qq_bridge = bridge if qq_enabled else None
            for text in ordinary:
                with self.subTest(qq_enabled=qq_enabled, text=text):
                    window._desktop._hide_bubble()
                    window._next_idle_at = 0
                    with patch.object(app.random, "choice", return_value=text):
                        window._desktop_tick()
                    self.assertEqual(window._desktop.bubble_text, text)
                    self.assertEqual(window.messages, before)
                    self.assertEqual(pet_core.read_history(), [])
                    bridge.send.assert_not_called()
        window._tray.notify.assert_not_called()

    def test_idle_drawing_failure_is_only_a_bubble(self):
        window = app.PetWindow()
        self.addCleanup(window.destroy)
        window._tray = MagicMock()
        window._qq_bridge = MagicMock()
        window._minimize_to_tray()
        before = list(window.messages)
        def immediate_thread(target, args=(), **_kwargs):
            return SimpleNamespace(start=lambda: target(*args))
        window._next_idle_at = 0
        with patch.object(app.random, "choice", return_value=app.IDLE_DRAW_TEXT), \
             patch.object(app.threading, "Thread", side_effect=immediate_thread), \
             patch.object(pet_core, "call_deepseek", side_effect=pet_core.PetError("fixture failure")):
            window._desktop_tick()
            window._drain_events()
        self.assertIn("没画好", window._desktop.bubble_text)
        self.assertEqual(window.messages, before)
        self.assertEqual(pet_core.read_history(), [])
        window._qq_bridge.send.assert_not_called()
        self.assertFalse(window.busy)

    def test_consented_screen_capture_enters_normal_chat_and_history(self):
        window = app.PetWindow()
        self.addCleanup(window.destroy)
        window._tray = MagicMock()
        window._minimize_to_tray()
        def immediate_thread(target, args=(), **_kwargs):
            return SimpleNamespace(start=lambda: target(*args))
        bbox = (0, 0, 320, 240)
        with patch.object(app.ImageGrab, "grab", return_value=app.Image.new("RGB", (320, 240), "blue")), \
             patch.object(app, "work_area", return_value=bbox), \
             patch.object(app.threading, "Thread", side_effect=immediate_thread), \
             patch.object(pet_core, "call_deepseek", side_effect=[{"content": "我看到蓝色的画面啦。"}, continuation()]):
            self._show_screen_question(window)
            self.assertEqual(pet_core.read_history(), [])
            window._desktop._answer_bubble(True)
            window.after_cancel(window._capture_job)
            window._launch_screen_capture(bbox)
            window._drain_events()
        history = pet_core.read_history()
        self.assertEqual([item["role"] for item in history], ["user", "assistant"])
        self.assertTrue(image_inputs.split_images(history[0]["content"])[1])
        self.assertIn("同意分享这一次截图", history[0]["content"])
        self.assertEqual(window.messages[-2]["content"], history[0]["content"])
        self.assertEqual(window.messages[-1]["content"], history[1]["content"])

    def _show_screen_question(self, window):
        window._next_idle_at = 0
        window._last_idle_reminder = ""
        with patch.object(app.random, "choice", return_value=app.SCREEN_REQUEST_TEXT):
            window._desktop_tick()
        self.assertEqual(window._desktop.bubble_text, app.SCREEN_REQUEST_TEXT)
        self.assertIsNotNone(window._desktop._bubble_choice)
        self.assertTrue(window._desktop.bubble_canvas.find_withtag("accept"))
        self.assertTrue(window._desktop.bubble_canvas.find_withtag("decline"))
        return window._active_screen_request

    def test_screen_request_decline_expiry_and_stale_answer_never_capture(self):
        window = app.PetWindow()
        window._tray = MagicMock()
        try:
            window._minimize_to_tray()
            with patch.object(app.ImageGrab, "grab") as grab, patch.object(pet_core, "reply") as model:
                token = self._show_screen_question(window)
                grab.assert_not_called()
                window._desktop._answer_bubble(False)
                window._on_screen_answer(token, True)
                self.assertFalse(window._capturing_screen)
                token = self._show_screen_question(window)
                with patch.object(app.time, "monotonic", return_value=window._screen_request_until + 1):
                    window._desktop._answer_bubble(True)
                    window._on_screen_answer(token, True)
                token = self._show_screen_question(window)
                window._restore_from_tray()
                window._on_screen_answer(token, True)
                window._desktop._answer_bubble(True)
                grab.assert_not_called()
                model.assert_not_called()
                self.assertFalse((pet_core.HISTORY.parent / "images").exists())
        finally:
            window.destroy()

    def test_screen_capture_requires_one_yes_and_preserves_composer_draft(self):
        window = app.PetWindow()
        window._tray = MagicMock()
        def immediate_thread(target, args=(), **_kwargs):
            return SimpleNamespace(start=lambda: target(*args))
        try:
            window._minimize_to_tray()
            draft = self._shared_image("blue")
            window._pending_images.append(draft)
            window._remove_placeholder()
            window.input.insert("1.0", "尚未发送的草稿")
            bbox = (0, 0, 640, 480)
            with patch.object(app.ImageGrab, "grab", side_effect=lambda **_kwargs: app.Image.new("RGB", (640, 480), "orange")) as grab, \
                 patch.object(window, "_worker") as worker, \
                 patch.object(app.threading, "Thread", side_effect=immediate_thread), \
                 patch.object(app, "work_area", return_value=bbox):
                token = self._show_screen_question(window)
                window._desktop._answer_bubble(True)
                self.assertTrue(window._capturing_screen)
                self.assertEqual(window._desktop.state(), "withdrawn")
                grab.assert_not_called()
                window.after_cancel(window._capture_job)
                window._launch_screen_capture(bbox)
                window._drain_events()
                grab.assert_called_once_with(bbox=bbox, all_screens=True)
                worker.assert_called_once()
                content = worker.call_args.args[0]
                text, images = image_inputs.split_images(content)
                self.assertIn("同意分享这一次截图", text)
                self.assertEqual(len(images), 1)
                self.assertTrue((pet_core.HISTORY.parent / "images" / images[0]).is_file())
                self.assertFalse(window._capturing_screen)
                self.assertEqual(window._desktop.state(), "normal")
                self.assertEqual(window.input.get("1.0", "end-1c"), "尚未发送的草稿")
                self.assertEqual(window._pending_images, [draft])
                window._on_screen_answer(token, True)
                window._launch_screen_capture(bbox)
                self.assertEqual(grab.call_count, 1)
        finally:
            window.destroy()

    def test_screen_capture_failure_recovers_without_sending(self):
        window = app.PetWindow()
        window._tray = MagicMock()
        try:
            window._minimize_to_tray()
            self._show_screen_question(window)
            with patch.object(app.ImageGrab, "grab", side_effect=OSError("capture failed")), \
                 patch.object(window, "_worker") as worker:
                window._desktop._answer_bubble(True)
                window.after_cancel(window._capture_job)
                window._screen_capture_worker((0, 0, 640, 480))
                window._drain_events()
                worker.assert_not_called()
            self.assertFalse(window.busy)
            self.assertFalse(window._capturing_screen)
            self.assertEqual(window._desktop.state(), "normal")
            self.assertEqual(pet_core.read_history(), [])
        finally:
            window.destroy()

    def test_image_reply_sends_real_pixels_and_keeps_local_history_references(self):
        name = self._shared_image()
        requests = []
        responses = [{"content": "这张红色卡片挺醒目的。"}, continuation(), {"content": "对，就是刚才那张红色的。"}, continuation()]
        def fake_urlopen(request, **_kwargs):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"choices": [{"message": responses.pop(0)}]}).encode())
        with patch.object(pet_core.urllib.request, "urlopen", side_effect=fake_urlopen):
            pet_core.reply(image_inputs.attach_images("你看这张图片是什么颜色？", [name]))
            pet_core.reply("刚才那张是什么颜色？")
        for request in requests:
            parts = [part for message in request["messages"] if message["role"] == "user" and isinstance(message["content"], list) for part in message["content"]]
            images = [part for part in parts if part["type"] == "image_url"]
            self.assertEqual(len(images), 1)
            data_url = images[0]["image_url"]["url"]
            self.assertEqual(base64.b64decode(data_url.split(",", 1)[1]), (pet_core.HISTORY.parent / "images" / name).read_bytes())
        stored = pet_core.HISTORY.read_text(encoding="utf-8")
        self.assertIn(name, stored)
        self.assertNotIn("data:image", stored)
        self.assertEqual(len(pet_core.read_history()), 4)
        self.assertTrue(any("你看这张图片是什么颜色" in row["topic"] for row in pet_core.memory_catalog().search("什么颜色")))

    def test_agent_can_reopen_an_old_shared_image_without_fake_history_turn(self):
        name = self._shared_image("blue")
        responses = [
            {"tool_calls": [{"id": "view", "type": "function", "function": {"name": "view_shared_image", "arguments": json.dumps({"filename": name})}}]},
            {"content": "重新看到了，是之前那张蓝色图片。"}, continuation(),
        ]
        requests = []
        def fake_urlopen(request, **_kwargs):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"choices": [{"message": responses.pop(0)}]}).encode())
        with patch.object(pet_core.urllib.request, "urlopen", side_effect=fake_urlopen):
            pet_core.reply("重新看看之前的图片。")
        self.assertTrue(any(part["type"] == "image_url" for message in requests[1]["messages"] if message["role"] == "user" and isinstance(message["content"], list) for part in message["content"]))
        self.assertEqual([row["role"] for row in pet_core.read_history()], ["user", "assistant"])
        self.assertNotIn(name, pet_core.HISTORY.read_text(encoding="utf-8"))
        self.assertNotIn("viewed", pet_core.execute_tool("view_shared_image", {"filename": "../config/deepseek_key.txt"}))

    def test_vision_context_is_bounded_and_missing_images_are_explicit(self):
        names = [self._shared_image(color) for color in ("red", "green", "blue", "white", "black", "yellow")]
        messages = [{"role": "user", "content": image_inputs.attach_images("分享图片", [name])} for name in names]
        original = copy.deepcopy(messages)
        converted = image_inputs.vision_messages(messages, pet_core.HISTORY.parent / "images")
        count = lambda values: sum(part["type"] == "image_url" for message in values if isinstance(message["content"], list) for part in message["content"])
        self.assertEqual(count(converted), 4)
        self.assertNotIn("image_url", json.dumps(converted[:2]))
        self.assertEqual(messages, original)
        (pet_core.HISTORY.parent / "images" / names[-1]).unlink()
        converted = image_inputs.vision_messages(messages, pet_core.HISTORY.parent / "images")
        self.assertEqual(count(converted), 3)
        self.assertIn("本地文件已缺失", json.dumps(converted[-1], ensure_ascii=False))
        with self.assertRaises(pet_core.PetError):
            pet_core.reply(image_inputs.attach_images("这张图", [names[-1]]))
        self.assertEqual(pet_core.read_history(), [])

    def test_image_import_normalizes_orientation_and_rejects_bad_files(self):
        source = Path(self.temp.name) / "rotated.jpg"
        image = app.Image.new("RGB", (64, 32), "red")
        exif = app.Image.Exif()
        exif[274] = 6
        exif[315] = "Test image author"
        image.save(source, exif=exif)
        folder = pet_core.HISTORY.parent / "images"
        name = image_inputs.save_image(source, folder)
        with app.Image.open(folder / name) as saved:
            self.assertEqual(saved.size, (32, 64))
            self.assertFalse(saved.getexif())
        source.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            image_inputs.save_image(source, folder)
        with patch.object(image_inputs, "MAX_PIXELS", 100):
            with self.assertRaises(ValueError):
                image_inputs.save_image(image, folder)
        with self.assertRaises(ValueError):
            image_inputs.image_path(folder, "../deepseek_key.txt")

    def test_choose_preview_remove_send_image_only_and_restore_chat(self):
        source = Path(self.temp.name) / "picture.png"
        app.Image.new("RGB", (160, 96), "orange").save(source)
        window = app.PetWindow()
        def immediate_thread(target, args=(), **_kwargs):
            return SimpleNamespace(start=lambda: target(*args))
        try:
            with patch.object(app.filedialog, "askopenfilenames", return_value=(str(source),)), \
                 patch.object(app.threading, "Thread", side_effect=immediate_thread):
                window._choose_images()
                self.assertTrue(window._attachments_loading)
                self.assertEqual(window.send_button.cget("state"), "disabled")
                window._drain_events()
            self.assertEqual(len(window._pending_images), 1)
            self.assertEqual(len(window._attachment_thumbnails), 1)
            name = window._pending_images[0]
            window._remove_image(name)
            self.assertEqual(window._pending_images, [])
            window._attachments_complete([name], [])
            with patch.object(window, "_worker") as worker, \
                 patch.object(app.threading, "Thread", side_effect=immediate_thread):
                window._send()
            content = worker.call_args.args[0]
            self.assertEqual(image_inputs.split_images(content), ("", [name]))
            self.assertEqual(window._pending_images, [])
            window.update_idletasks()
            self.assertEqual(len(window.chat_canvas.find_withtag("chat_media")), 1)
            self.assertFalse(any("user-image" in window.chat_canvas.itemcget(item, "text") for item in window.chat_canvas.find_withtag("message_text")))
            pet_core.append_history("user", content)
        finally:
            window.destroy()
        restored = app.PetWindow()
        try:
            restored.update_idletasks()
            self.assertEqual(len(restored.chat_canvas.find_withtag("chat_media")), 1)
        finally:
            restored.destroy()

    def test_clipboard_image_paste_preserves_normal_text_and_limits_attachments(self):
        window = app.PetWindow()
        try:
            window._remove_placeholder()
            window.input.insert("1.0", "看看这个")
            with patch.object(app.ImageGrab, "grabclipboard", return_value=app.Image.new("RGB", (32, 32), "pink")), \
                 patch.object(app.threading, "Thread"):
                self.assertEqual(window._paste_image(), "break")
            self.assertEqual(window.input.get("1.0", "end-1c"), "看看这个")
            window._image_worker([app.Image.new("RGB", (32, 32), "pink")])
            window._drain_events()
            self.assertEqual(len(window._pending_images), 1)
            with patch.object(app.ImageGrab, "grabclipboard", return_value=None):
                self.assertIsNone(window._paste_image())
            with patch.object(app.messagebox, "showinfo") as warning, patch.object(app.threading, "Thread") as worker:
                window._queue_images(["one.png"] * 4)
            warning.assert_called_once()
            worker.assert_not_called()
            pet_core.KEY_FILE.write_text("", encoding="utf-8")
            with patch.object(app.messagebox, "showwarning") as warning:
                window._send()
            warning.assert_called_once()
            self.assertEqual(len(window._pending_images), 1)
            self.assertEqual(pet_core.read_history(), [])
        finally:
            window.destroy()

    def test_agent_reads_and_writes_memory_before_reply(self):
        pet_core.append_history("user", "我喜欢草莓")
        responses = [
            {"content": None, "tool_calls": [{"id": "a", "type": "function", "function": {"name": "search_chat_history", "arguments": '{"keyword":"草莓"}'}}]},
            {"content": None, "tool_calls": [{"id": "b", "type": "function", "function": {"name": "remember_important", "arguments": '{"fact":"用户喜欢草莓"}'}}]},
            {"content": "我记住啦。"},
            continuation(),
        ]
        seen = []
        def fake_api(messages, key, **kwargs):
            seen.append(copy.deepcopy(messages))
            return responses.pop(0)
        with patch.object(pet_core, "call_deepseek", side_effect=fake_api):
            answer = pet_core.reply("还记得我喜欢什么吗？")
        self.assertEqual(answer, "我记住啦。")
        self.assertIn("- 用户喜欢草莓", pet_core.IMPORTANT.read_text(encoding="utf-8"))
        self.assertIn("我喜欢草莓", seen[1][-1]["content"])
        self.assertEqual([x["role"] for x in pet_core.read_history()][-2:], ["user", "assistant"])

    def test_key_is_required_before_writing_chat(self):
        pet_core.KEY_FILE.write_text("", encoding="utf-8")
        with self.assertRaises(pet_core.PetError):
            pet_core.reply("你好")
        self.assertEqual(pet_core.read_history(), [])

    def test_duplicate_memory_is_not_appended(self):
        pet_core.execute_tool("remember_important", {"fact": "  用户喜欢草莓  "})
        pet_core.execute_tool("remember_important", {"fact": "用户喜欢草莓"})
        self.assertEqual(pet_core.IMPORTANT.read_text(encoding="utf-8").count("用户喜欢草莓"), 1)

    def test_catalog_indexes_lines_and_reads_original(self):
        first = pet_core.append_history("user", "去年秋天去了杭州看桂花")
        second = pet_core.append_history("assistant", "听起来很美。")
        self.assertEqual((first, second), (1, 2))
        matches = pet_core.memory_catalog().search("杭州")
        self.assertEqual(matches[0]["line"], 1)
        original = pet_core.memory_catalog().read_lines(1, 2)
        self.assertEqual([item["content"] for item in original], ["去年秋天去了杭州看桂花", "听起来很美。"])
        self.assertEqual([item["line"] for item in original], [1, 2])
        self.assertEqual(len(pet_core.recent_history()), 2)

    def test_catalog_backfills_and_repairs_edited_history(self):
        old = {"time": "2025-01-02T03:04:05+08:00", "role": "user", "content": "小时候养过猫"}
        pet_core.HISTORY.write_text(json.dumps(old, ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(pet_core.memory_catalog().search("养过猫")[0]["line"], 1)
        old["content"] = "小时候养过狗"
        pet_core.HISTORY.write_text(json.dumps(old, ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(pet_core.memory_catalog().search("养过狗")[0]["line"], 1)
        self.assertEqual(pet_core.memory_catalog().search("养过猫"), [])

    def test_model_label_improves_catalog_search(self):
        line = pet_core.append_history("user", "那天真开心")
        result = pet_core.execute_tool("label_current_topic", {"topic": "杭州旅行", "keywords": "西湖 桂花 2025"}, current_line=line)
        self.assertEqual(result, "目录标题已更新。")
        self.assertEqual(pet_core.memory_catalog().search("西湖")[0]["line"], line)
        with pet_core.HISTORY.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": "2025-01-01", "role": "assistant", "content": "好的"}, ensure_ascii=False) + "\n")
        self.assertEqual(pet_core.memory_catalog().search("西湖")[0]["line"], line)

    def test_daily_plan_due_and_sent_only_once(self):
        plan = DailyPlan(pet_core.PLAN_DIR)
        now = datetime(2026, 10, 19, 16, 0, tzinfo=timezone(timedelta(hours=8)))
        plan.create(now, [{"time": "17:20", "topic": "提醒吃饭"}])
        self.assertIsNone(plan.due(now))
        due = plan.due(now.replace(hour=17, minute=20))
        self.assertEqual(due["topic"], "提醒吃饭")
        self.assertTrue(plan.mark_sent(now.date().isoformat(), "17:20", "提醒吃饭"))
        self.assertIsNone(plan.due(now.replace(hour=17, minute=21)))

    def test_shared_plan_rewrites_for_new_day(self):
        plan = DailyPlan(pet_core.PLAN_DIR)
        first = datetime(2026, 10, 19, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        second = first + timedelta(days=1)
        first_path = plan.create(first, [{"time": "17:20", "topic": "提醒吃饭"}])
        self.assertEqual(first_path.name, "plan.md")
        self.assertTrue(plan.has_day(first.date().isoformat()))
        self.assertFalse(plan.has_day(second.date().isoformat()))
        self.assertEqual(plan.read(second.date().isoformat()), [])
        second_path = plan.create(second, [])
        self.assertEqual(second_path, first_path)
        self.assertTrue(plan.has_day(second.date().isoformat()))
        self.assertFalse(plan.has_day(first.date().isoformat()))
        self.assertNotIn("提醒吃饭", second_path.read_text(encoding="utf-8"))

    def test_legacy_daily_plans_are_migrated_without_losing_history(self):
        today = datetime.now().astimezone().date().isoformat()
        old = "2025-01-01"
        (pet_core.PLAN_DIR / f"{today}.md").write_text(f"# {today} 主动聊天计划\n\n- [ ] 23:50 | 聊天\n", encoding="utf-8")
        (pet_core.PLAN_DIR / f"{old}.md").write_text(f"# {old} 主动聊天计划\n", encoding="utf-8")
        plan = DailyPlan(pet_core.PLAN_DIR)
        self.assertTrue(plan.has_day(today))
        self.assertEqual(plan.read(today)[0]["topic"], "聊天")
        self.assertFalse((pet_core.PLAN_DIR / f"{today}.md").exists())
        self.assertTrue((pet_core.PLAN_DIR / "archive" / f"{old}.md").exists())

    def test_expired_plan_is_not_sent_late(self):
        plan = DailyPlan(pet_core.PLAN_DIR)
        now = datetime(2026, 10, 19, 16, 0, tzinfo=timezone(timedelta(hours=8)))
        plan.create(now, [{"time": "17:20", "topic": "提醒吃饭"}])
        self.assertIsNone(plan.due(now.replace(hour=18, minute=0)))
        self.assertEqual(plan.read(now.date().isoformat())[0]["status"], "~")

    def test_planner_writes_once_per_day(self):
        now = datetime(2026, 10, 21, 12, 0).astimezone()
        future = (now + timedelta(minutes=20)).strftime("%H:%M")
        response = {"tool_calls": [{"function": {"name": "write_daily_plan", "arguments": json.dumps({"events": [{"time": future, "topic": "提醒吃饭"}]}, ensure_ascii=False)}}]}
        with patch.object(pet_core, "call_deepseek", return_value=response) as api, \
             patch.object(pet_core, "datetime") as clock:
            clock.now.return_value = now
            self.assertTrue(pet_core.plan_today())
            self.assertFalse(pet_core.plan_today())
        self.assertEqual(api.call_count, 1)
        self.assertIn("提醒吃饭", pet_core.daily_plan().path(now.date().isoformat()).read_text(encoding="utf-8"))

    def test_notes_are_scoped_to_notes_folder(self):
        self.assertIn("已保存", pet_core.execute_tool("write_note", {"filename": "todo.md", "content": "# 事项\n- 吃饭"}))
        self.assertIn("吃饭", pet_core.execute_tool("read_note", {"filename": "todo.md"}))
        self.assertIn("笔记文件名", pet_core.execute_tool("write_note", {"filename": "../pet/setting.md", "content": "坏内容"}))
        self.assertEqual(pet_core.SETTING.read_text(encoding="utf-8"), "你叫测试宠物，使用中文。")

    def test_proactive_message_does_not_add_fake_user_turn(self):
        with patch.object(pet_core, "call_deepseek", return_value={"content": "该吃饭啦！"}):
            self.assertEqual(pet_core.proactive_reply("提醒吃饭"), "该吃饭啦！")
        self.assertEqual([item["role"] for item in pet_core.read_history()], ["assistant"])

    def test_pet_reply_removes_styling_markers(self):
        self.assertEqual(naturalize_text("**你真棒**，<b>继续加油</b>！"), "你真棒，继续加油！")
        self.assertEqual(naturalize_text("[来源](https://example.com/a)"), "来源：https://example.com/a")
        self.assertEqual(naturalize_text("```python\nx ** 2\n```"), "```python\nx ** 2\n```")
        with patch.object(pet_core, "call_deepseek", return_value={"content": "**你好**，<b>今天过得怎么样</b>？"}):
            answer = pet_core.reply("你好")
        self.assertEqual(answer, "你好，今天过得怎么样？")
        self.assertEqual(pet_core.read_history()[-1]["content"], answer)

    def test_time_and_web_lookup_tools(self):
        current = json.loads(pet_core.execute_tool("get_current_time", {}))
        self.assertIn("local_time", current)
        self.assertIn("weekday", current)
        with patch.object(web_tools, "_get_json", return_value={"query": {"search": [{"title": "月球", "snippet": "<b>月球</b>是卫星"}]}}) as request:
            result = json.loads(pet_core.execute_tool("search_knowledge", {"query": "月球"}))
        self.assertEqual(result[0]["title"], "月球")
        self.assertIn("zh.wikipedia.org/wiki/", result[0]["url"])
        self.assertIn("srsearch=%E6%9C%88%E7%90%83", request.call_args.args[0])
        with patch.object(web_tools, "_get_rss", return_value=[{"title": "News", "url": "https://example.com/news", "source": "example.com", "published": "2026-10-20"}]):
            news = json.loads(pet_core.execute_tool("search_news", {"query": "technology"}))
        self.assertEqual(news[0]["url"], "https://example.com/news")
        with patch.object(web_tools, "_get_rss", side_effect=[RuntimeError("主源不可用"), [{"title": "Fallback", "url": "https://example.com/fallback"}]]):
            fallback = json.loads(pet_core.execute_tool("search_news", {"query": "technology"}))
        self.assertEqual(fallback[0]["title"], "Fallback")
        with patch.object(web_tools, "_get_json", return_value={"query": {"pages": {"1": {"title": "水母", "extract": "水母是海洋动物。"}}}}):
            fact = json.loads(pet_core.execute_tool("find_fun_fact", {}))
        self.assertEqual(fact["title"], "水母")

    def test_media_search_send_and_persist_in_assistant_message(self):
        source = Path(__file__).resolve().parents[1] / "media" / "happy.png"
        shutil.copy2(source, pet_core.MEDIA_DIR / "happy.png")
        (pet_core.MEDIA_DIR / "index.json").write_text(json.dumps({"happy.png": {"mood": "开心", "keywords": "笑"}}, ensure_ascii=False), encoding="utf-8")
        found = json.loads(pet_core.execute_tool("search_media", {"query": "开心"}))
        self.assertEqual(found[0]["file"], "happy.png")
        natural = json.loads(pet_core.execute_tool("search_media", {"query": "发个开心的表情包"}))
        self.assertEqual(natural[0]["file"], "happy.png")
        self.assertIn("请提供媒体文件名", pet_core.execute_tool("send_media", {"filename": "../happy.png"}))
        response = {"content": None, "tool_calls": [{"id": "media", "type": "function", "function": {"name": "send_media", "arguments": '{"filename":"happy.png"}'}}]}
        with patch.object(pet_core, "call_deepseek", side_effect=[response, {"content": "给你一个开心表情。"}, continuation()]):
            answer = pet_core.reply("发个开心表情")
        self.assertIn("[[pet-media:happy.png]]", answer)
        self.assertIn("[[pet-media:happy.png]]", pet_core.read_history()[-1]["content"])
        self.assertEqual(pet_core.split_media(answer), ("给你一个开心表情。", ["happy.png"]))
        self.assertIn("找不到", pet_core.execute_tool("send_media", {"filename": "missing.gif"}))

    def test_image_and_gif_render_in_normal_chat_bubble(self):
        root_media = Path(__file__).resolve().parents[1] / "media"
        for name in ("happy.png", "wink.gif"):
            shutil.copy2(root_media / name, pet_core.MEDIA_DIR / name)
        window = app.PetWindow()
        try:
            window._add_message("assistant", "给你看\n[[pet-media:happy.png]]\n[[pet-media:wink.gif]]")
            window.update()
            window._animate_media()
            self.assertEqual(len(window.chat_canvas.find_withtag("chat_media")), 2)
            self.assertGreater(len(window.animated_items[0]["frames"]), 1)
            cached = window._load_media("wink.gif")
            window._redraw_messages()
            window.update_idletasks()
            self.assertIs(window._load_media("wink.gif"), cached)
        finally:
            window.destroy()

    def test_window_refreshes_name_and_key_state(self):
        pet_core.SETTING.write_text("- # 小希的设定\n- 你的名字是**小希**，是一只水母。", encoding="utf-8")
        window = app.PetWindow()
        def visible_text():
            window.update_idletasks()
            return [window.chat_canvas.itemcget(item, "text") for item in window.chat_canvas.find_all() if window.chat_canvas.type(item) == "text"]
        try:
            self.assertIn("小希", window.title())
            self.assertIn("API Key 已设置", window.key_var.get())
            self.assertIn("今天有什么想分享的吗？", visible_text())
            pet_core.SETTING.write_text("你的名字是**小海**。", encoding="utf-8")
            pet_core.KEY_FILE.write_text("", encoding="utf-8")
            window._refresh_identity()
            self.assertIn("小海", window.title())
            self.assertIn("API Key 未设置", window.key_var.get())
            self.assertIn("点右上角设置 API Key，就可以开始聊天了。", visible_text())
            pet_core.KEY_FILE.write_text("test-key", encoding="utf-8")
            window._refresh_identity()
            self.assertIn("API Key 已设置", window.key_var.get())
            self.assertNotIn("点右上角设置 API Key，就可以开始聊天了。", visible_text())
        finally:
            window.destroy()

    def test_window_only_exposes_chat_history_and_key_settings(self):
        pet_core.SETTING.write_text("你的名字是**小希**。", encoding="utf-8")
        window = app.PetWindow()
        def widgets(widget):
            yield widget
            for child in widget.winfo_children():
                yield from widgets(child)
        try:
            self.assertEqual(window.name_var.get(), "小希")
            self.assertFalse(hasattr(window, "sidebar_name_var"))
            controls = list(widgets(window))
            visible_text = [item.cget("text") for item in controls if isinstance(item, (app.tk.Label, app.tk.Button))]
            for hidden in ("XIGPET", "今日计划", "重要记忆", "记忆目录", "宠物设定", "主动联系"):
                self.assertFalse(any(hidden in text for text in visible_text))
            self.assertIn("聊天记录", visible_text)
            key_buttons = [item for item in controls if isinstance(item, app.tk.Button) and "API Key" in item.cget("text")]
            self.assertEqual(len(key_buttons), 1)
            with patch.object(app.os, "startfile") as open_file:
                key_buttons[0].invoke()
            open_file.assert_called_once_with(pet_core.KEY_FILE)
            window.events.put(("proactive_reply", "记得吃饭呀"))
            window._drain_events()
            self.assertEqual(window.messages[-1], {"role": "assistant", "content": "记得吃饭呀"})
            window.update_idletasks()
            self.assertFalse(any("主动联系" in window.chat_canvas.itemcget(item, "text") for item in window.chat_canvas.find_withtag("message_text")))
        finally:
            window.destroy()

    def test_minimize_to_tray_restore_and_exit(self):
        window = app.PetWindow()
        tray = MagicMock()
        tray.ready = threading.Event()
        tray.ready.set()
        window._tray = tray
        try:
            window.update_idletasks()
            window.iconify()
            window.update()
            self.assertTrue(window._background)
            self.assertEqual(window.state(), "withdrawn")
            self.assertEqual(window._desktop.state(), "normal")
            self.assertTrue(window._desktop.attributes("-topmost"))
            window.events.put(("tray_restore", ""))
            window._drain_events()
            self.assertFalse(window._background)
            self.assertEqual(window.state(), "normal")
            self.assertEqual(window._desktop.state(), "withdrawn")
            window._on_close()
            self.assertEqual(window.state(), "withdrawn")
            window.events.put(("tray_exit", ""))
            window._drain_events()
            self.assertTrue(window._closing)
            tray.stop.assert_called_once()
        finally:
            window.destroy()

    def test_background_proactive_schedule_delivers_without_opening_window(self):
        window = app.PetWindow()
        tray = MagicMock()
        tray.ready = threading.Event()
        tray.ready.set()
        window._tray = tray
        plan = MagicMock()
        plan.has_day.return_value = True
        plan.due.return_value = {"time": "17:20", "topic": "吃饭"}
        def immediate_thread(target, args=(), **_kwargs):
            return SimpleNamespace(start=lambda: target(*args))
        def proactive(_topic, on_reply):
            on_reply("吃饭了吗？")
            on_reply("我也准备去吃点东西。")
        try:
            window._minimize_to_tray()
            with patch.object(pet_core, "daily_plan", return_value=plan), \
                 patch.object(pet_core, "proactive_reply", side_effect=proactive) as reply, \
                 patch.object(app.threading, "Thread", side_effect=immediate_thread):
                window._schedule_tick()
                window._drain_events()
            reply.assert_called_once()
            plan.mark_sent.assert_called_once()
            self.assertTrue(window.busy)
            window.after_cancel(window._delivery_job)
            window._delivery_job = None
            window._next_reply_at = 0
            window._deliver_next_reply()
            self.assertFalse(window.busy)
            self.assertEqual(window.state(), "withdrawn")
            self.assertEqual(window._unread_count, 2)
            tray.notify.assert_called_once_with(window.pet_name, "吃饭了吗？")
            self.assertEqual(window.messages[-1]["content"], "我也准备去吃点东西。")
            self.assertEqual(window._desktop.bubble_text, "我也准备去吃点东西。")
            window._restore_from_tray()
            self.assertEqual(window._unread_count, 0)
            tray.clear_notification.assert_called_once()
        finally:
            window.destroy()

    def test_desktop_pet_remains_available_while_tray_starts_or_fails(self):
        window = app.PetWindow()
        tray = MagicMock()
        tray.ready = threading.Event()
        try:
            with patch.object(app, "SystemTray", return_value=tray):
                self.assertTrue(window._minimize_to_tray())
            self.assertEqual(window.state(), "withdrawn")
            self.assertEqual(window._desktop.state(), "normal")
            tray.ready.set()
            window.events.put(("tray_ready", ""))
            window._drain_events()
            self.assertEqual(window.state(), "withdrawn")
            window.events.put(("tray_error", "test failure"))
            with patch.object(app.logging, "warning") as warning:
                window._drain_events()
            warning.assert_called_once()
            self.assertEqual(window.state(), "withdrawn")
            self.assertEqual(window._desktop.state(), "normal")
            self.assertIsNone(window._tray)
            tray.stop.assert_called_once()
        finally:
            window.destroy()

    def test_desktop_bubble_previews_twenty_characters_and_double_click_restores(self):
        window = app.PetWindow()
        window._tray = MagicMock()
        try:
            window._minimize_to_tray()
            content = "一二三四五六七八九十一二三四五六七八九十还有后续文字"
            window.events.put(("reply", content))
            window._drain_events()
            pet = window._desktop
            self.assertEqual(pet.bubble_text, content[:20] + "…")
            self.assertEqual(window.messages[-1]["content"], content)
            self.assertEqual(pet.bubble.state(), "normal")
            pet._double_click()
            self.assertEqual(window.state(), "normal")
            self.assertEqual(pet.state(), "withdrawn")
            self.assertEqual(pet.bubble.state(), "withdrawn")
        finally:
            window.destroy()

    def test_idle_bubble_uses_random_deadline_and_resets_after_user_message(self):
        window = app.PetWindow()
        window._tray = MagicMock()
        try:
            window._minimize_to_tray()
            self.assertGreaterEqual(window._next_idle_at - window._last_user_at, app.IDLE_MIN_SECONDS)
            self.assertLessEqual(window._next_idle_at - window._last_user_at, app.IDLE_MAX_SECONDS)
            window._next_idle_at = 500
            with patch.object(app.time, "monotonic", return_value=499):
                window._desktop_tick()
                self.assertFalse(window._desktop.has_bubble)
            with patch.object(app.time, "monotonic", return_value=500), \
                 patch.object(app.random, "uniform", return_value=600), \
                 patch.object(app.random, "choice", return_value=app.IDLE_REMINDER_TEXTS[0]):
                window._desktop_tick()
                self.assertIn(window._desktop.bubble_text, app.IDLE_REMINDER_TEXTS)
                self.assertEqual(window._next_idle_at, 1100)
                window._desktop._hide_bubble()
                window._desktop_tick()
                self.assertFalse(window._desktop.has_bubble)
                self.assertEqual(pet_core.read_history(), [])
                window._restore_from_tray()
                window._remove_placeholder()
                window.input.insert("1.0", "没有忘记你呀")
                with patch.object(app.threading, "Thread"):
                    window._send()
                self.assertEqual(window._next_idle_at, 1100)
                self.assertEqual(window._last_user_at, 500)
        finally:
            window.destroy()

    def test_pet_drag_resize_and_preferences_persist(self):
        window = app.PetWindow()
        window._tray = MagicMock()
        try:
            window._minimize_to_tray()
            pet = window._desktop
            with patch("xigpet.desktop_pet.work_area", return_value=(0, 0, 1600, 1000)):
                pet._x, pet._y = 300, 300
                pet._drag_start(SimpleNamespace(x_root=320, y_root=320))
                pet._drag_move(SimpleNamespace(x_root=390, y_root=360))
                pet._drag_end()
                self.assertEqual(pet._anchor, (370, 340))
                size_index = next(index for index in range(pet.menu.index("end") + 1)
                                  if pet.menu.type(index) == "cascade"
                                  and pet.menu.entrycget(index, "label") == "宠物大小")
                size_menu = pet.nametowidget(pet.menu.entrycget(size_index, "menu"))
                size_menu.invoke(0)
                self.assertEqual(pet.size, 80)
                pet.set_size(64)
                self.assertEqual(pet._image.width(), 64)
                size_menu.invoke(3)
                self.assertEqual(pet.size, 260)
                with patch("xigpet.desktop_pet.simpledialog.askinteger", return_value=220):
                    pet._custom_size()
                pet.floating.set(False)
                pet._save_settings()
                saved = json.loads((pet_core.KEY_FILE.parent / "desktop_pet.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["size"], 220)
                self.assertFalse(saved["floating"])
                pet.destroy()
                window._desktop = app.DesktopPet(window, [pet_core.ROOT / "Xig.png"],
                    pet_core.KEY_FILE.parent / "desktop_pet.json", window._restore_from_tray, window.destroy)
                self.assertEqual(window._desktop.size, 220)
                self.assertFalse(window._desktop.floating.get())
                self.assertEqual(window._desktop._anchor, (saved["x"], saved["y"]))
        finally:
            window.destroy()

    def test_pet_float_is_slow_bounded_and_pauses_for_interaction(self):
        window = app.PetWindow()
        window._tray = MagicMock()
        try:
            window._minimize_to_tray()
            pet = window._desktop
            pet.after_cancel(pet._motion_job)
            pet._motion_job = None
            pet._x = pet._y = 300
            pet._anchor = (300, 300)
            pet.behavior.state = "float"
            pet.behavior.target = (18, 12)
            pet.behavior.steps_remaining = 2
            pet._resume_motion_at = 100
            pet._last_frame = 100
            positions = []
            with patch("xigpet.desktop_pet.work_area", return_value=(0, 0, 1600, 1000)), \
                 patch.object(pet, "_pointer_over_pet", return_value=False) as pointer, \
                 patch.object(pet, "_place"):
                for index in range(1, 26):
                    with patch.object(app.time, "monotonic", return_value=100 + index * .04):
                        pet._float_tick()
                    pet.after_cancel(pet._motion_job)
                    positions.append((pet._x, pet._y))
                    self.assertLessEqual(abs(pet._x - 300), 18.001)
                    self.assertLessEqual(abs(pet._y - 300), 12.001)
                    if index > 1:
                        self.assertLess(((pet._x - positions[-2][0]) ** 2 + (pet._y - positions[-2][1]) ** 2) ** .5, 1)
                self.assertGreater(((positions[24][0] - 300) ** 2 + (positions[24][1] - 300) ** 2) ** .5, 8)
                self.assertEqual(pet._anchor, (300, 300))
                before = (pet._x, pet._y)
                target = pet.behavior.target
                pointer.return_value = True
                with patch.object(app.time, "monotonic", return_value=101.04):
                    pet._float_tick()
                self.assertEqual((pet._x, pet._y), before)
                self.assertEqual(pet.behavior.target, target)
                pet.after_cancel(pet._motion_job)
                pointer.return_value = False
                # A stale Enter flag must not leave the pet frozen after moving away.
                pet._hovering = True
                with patch.object(app.time, "monotonic", return_value=101.08):
                    pet._float_tick()
                self.assertNotEqual((pet._x, pet._y), before)
                self.assertFalse(pet._hovering)
                pet.after_cancel(pet._motion_job)
                before = (pet._x, pet._y)
                with patch.object(pet.menu, "winfo_ismapped", return_value=True) as menu_open:
                    with patch.object(app.time, "monotonic", return_value=101.12):
                        pet._float_tick()
                    self.assertEqual((pet._x, pet._y), before)
                    pet.after_cancel(pet._motion_job)
                    menu_open.return_value = False
                    with patch.object(app.time, "monotonic", return_value=101.16):
                        pet._float_tick()
                    self.assertNotEqual((pet._x, pet._y), before)
                    pet.after_cancel(pet._motion_job)
                before = (pet._x, pet._y)
                pet.floating.set(False)
                with patch.object(app.time, "monotonic", return_value=101.20):
                    pet._float_tick()
                self.assertEqual((pet._x, pet._y), before)
        finally:
            window.destroy()

    def test_desktop_gifs_load_and_advance_for_each_state(self):
        window = app.PetWindow()
        window._tray = MagicMock()
        try:
            window._minimize_to_tray()
            pet = window._desktop
            for state in ("relax", "float", "play"):
                pet._switch_animation(state)
                self.assertEqual(pet._animation_state, state)
                self.assertGreater(len(pet._frames), 1)
                first = pet._image
                pet._animate_sprite(pet._next_sprite_at + .01)
                self.assertIsNot(pet._image, first)
            pet.set_size(80)
            self.assertEqual(pet._animation_state, "relax")
            self.assertEqual(pet._image.width(), 80)
        finally:
            window.destroy()

    def test_idle_reminders_randomize_without_consecutive_repeats(self):
        window = app.PetWindow()
        window._tray = MagicMock()
        try:
            window._minimize_to_tray()
            with patch.object(app.random, "choice", side_effect=lambda options: options[0]) as choose, \
                 patch.object(app.time, "monotonic", return_value=1000):
                window._next_idle_at = 1000
                window._desktop_tick()
                first = window._desktop.bubble_text
                self.assertEqual(len(choose.call_args.args[0]), len(app.IDLE_REMINDER_TEXTS))
                window._desktop._hide_bubble()
                window._desktop_tick()
                self.assertEqual(choose.call_count, 1)
                window._next_idle_at = 1000
                window._desktop_tick()
                second = window._desktop.bubble_text
                self.assertNotEqual(first, second)
                self.assertNotIn(first, choose.call_args.args[0])
                self.assertIn(second, app.IDLE_REMINDER_TEXTS)
                self.assertEqual(pet_core.read_history(), [])
        finally:
            window.destroy()

    def test_short_reply_is_delivered_before_continuation_decision(self):
        sent = []
        responses = [
            {"content": "第一句。第二句！第三句？第四句。第五句。"},
            continuation(),
        ]
        def fake_api(messages, key, **kwargs):
            if kwargs.get("tools_override") == [pet_core.CONTINUATION_TOOL]:
                self.assertEqual(len(sent), 2)
                self.assertEqual(pet_core.read_history()[-1]["content"], sent[-1])
            return responses.pop(0)
        with patch.object(pet_core, "call_deepseek", side_effect=fake_api) as api:
            pet_core.reply("陪我聊聊", on_reply=sent.append)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(sent, ["第一句。第二句！第三句？第四句。", "第五句。"])
        self.assertEqual([item["role"] for item in pet_core.read_history()], ["user", "assistant", "assistant"])
        self.assertTrue(all(not limit_sentences(text)[1] for text in sent))

    def test_continuation_stops_at_limit_and_on_duplicate_or_failure(self):
        responses = [{"content": "第一条。"}, continuation(True, "新内容"), {"content": "第二条。"}, continuation(True, "最后补充"), {"content": "第三条。"}]
        sent = []
        with patch.object(pet_core, "call_deepseek", side_effect=responses) as api:
            pet_core.reply("聊聊", on_reply=sent.append)
        self.assertEqual(len(sent), 3)
        self.assertEqual(api.call_count, 5)
        with patch.object(pet_core, "call_deepseek", side_effect=[{"content": "你好呀。"}, continuation(True, "补充"), {"content": "你好呀。"}]):
            self.assertEqual(pet_core.reply("你好"), "你好呀。")
        with patch.object(pet_core, "call_deepseek", side_effect=[{"content": "晚安。"}, pet_core.PetError("暂时断网")]):
            self.assertEqual(pet_core.reply("晚安"), "晚安。")

    def test_retrieval_answer_is_exempt_from_short_limit(self):
        long_answer = "第一项。第二项。第三项。第四项。第五项。来源：https://example.com"
        search = {"tool_calls": [{"id": "search", "type": "function", "function": {"name": "search_knowledge", "arguments": '{"query":"月球"}'}}]}
        with patch.object(web_tools, "search_knowledge", return_value="百科摘要"), patch.object(pet_core, "call_deepseek", side_effect=[search, {"content": long_answer}, continuation()]):
            self.assertEqual(pet_core.reply("帮我查月球的资料"), long_answer)

    def test_sentence_limit_preserves_urls_numbers_and_code(self):
        text = "值是3.14。你好！真的吗？好呀。第五句。"
        self.assertEqual(limit_sentences(text), ("值是3.14。你好！真的吗？好呀。", "第五句。"))
        self.assertEqual(limit_sentences("Hello. Hi! Yes? Sure. More."), ("Hello. Hi! Yes? Sure.", "More."))
        self.assertEqual(limit_sentences('"Hello." Hi! Yes? Sure. More.'), ('"Hello." Hi! Yes? Sure.', 'More.'))
        self.assertEqual(limit_sentences("```python\nx=1.2\nprint(x)\n```"), ("```python\nx=1.2\nprint(x)\n```", ""))
        self.assertEqual(limit_sentences("看看 https://example.com/a.b\n就知道了。"), ("看看 https://example.com/a.b\n就知道了。", ""))

    def test_ui_keeps_turn_busy_between_bubbles_and_uses_xig_avatar(self):
        window = app.PetWindow()
        try:
            with patch.object(app.time, "monotonic", return_value=100.0) as clock, patch.object(app.random, "uniform", return_value=2.0):
                window._begin_turn()
                window.events.put(("reply", "第一条。"))
                window._drain_events()
                self.assertTrue(window.busy)
                self.assertEqual(window.messages[-1]["content"], "第一条。")
                window.events.put(("reply", "第二条。"))
                window.events.put(("turn_done", ""))
                window._drain_events()
                self.assertTrue(window.busy)
                window._drain_events()
                self.assertEqual(window.messages[-1]["content"], "第一条。")
                clock.return_value = 102.0
                window.after_cancel(window._delivery_job)
                window._deliver_next_reply()
                self.assertFalse(window.busy)
            self.assertEqual([item["content"] for item in window.messages[-2:]], ["第一条。", "第二条。"])
            window.update_idletasks()
            self.assertIn(42, window.avatar_images)
            self.assertGreater(window.avatar_images[42].width(), 0)
        finally:
            window.destroy()

    def test_reaction_is_sent_proactively_without_user_request(self):
        source = Path(__file__).resolve().parents[1] / "media" / "happy.png"
        shutil.copy2(source, pet_core.MEDIA_DIR / "happy.png")
        (pet_core.MEDIA_DIR / "index.json").write_text('{"happy.png":{"title":"开心","mood":"开心"}}', encoding="utf-8")
        sent = []
        with patch.object(pet_core, "call_deepseek", side_effect=[{"content": "今天真替你高兴！"}, continuation(reaction="happy.png")]):
            pet_core.reply("我今天考试通过了", on_reply=sent.append)
        self.assertEqual(sent, ["今天真替你高兴！", "\n[[pet-media:happy.png]]"])
        self.assertEqual(len(pet_core.read_history()), 3)

    def test_complete_response_is_preserved_when_judge_fails(self):
        full = "我在这里陪你。\n\n今天辛苦了，先放松一下。\n\n等你想说了，我再听你慢慢讲。"
        sent = []
        with patch.object(pet_core, "call_deepseek", side_effect=[{"content": full}, pet_core.PetError("断网"), continuation(), continuation()]):
            pet_core.reply("今天心情不好", on_reply=sent.append)
        self.assertEqual(sent, [full.replace("\n\n", "")])
        self.assertEqual(split_bubbles("一。二。三。四。五。"), ["一。二。三。四。", "五。"])

    def test_simple_exchange_stays_short_without_a_bubble_quota(self):
        sent = []
        with patch.object(pet_core, "call_deepseek", side_effect=[{"content": "回来啦，坐下来歇会儿吧。"}, continuation()]):
            pet_core.reply("我回来了", on_reply=sent.append)
        self.assertEqual(sent, ["回来啦，坐下来歇会儿吧。"])
        self.assertEqual(split_bubbles("好呀。那就这么说定了。"), ["好呀。那就这么说定了。"])
        self.assertEqual(split_bubbles("第一点。第二点。\n\n另外还要说明这件事。"), ["第一点。第二点。另外还要说明这件事。"])

    def test_buffered_replies_wait_random_one_to_three_seconds(self):
        window = app.PetWindow()
        try:
            window.update_idletasks()
            with patch.object(app.time, "monotonic", return_value=100.0) as clock, patch.object(app.random, "uniform", side_effect=[1.25, 2.75, 1.5]) as gap:
                window._begin_turn()
                for content in ("第一条。", "第二条。", "第三条。"):
                    window.events.put(("reply", content))
                window.events.put(("turn_done", ""))
                window._drain_events()
                self.assertEqual(window.messages[-1]["content"], "第一条。")
                self.assertEqual(len(window._pending_replies), 2)
                self.assertTrue(window.busy)
                def fire_at(moment):
                    clock.return_value = moment
                    if window._delivery_job is not None:
                        window.after_cancel(window._delivery_job)
                    window._deliver_next_reply()
                fire_at(101.24)
                self.assertEqual(window.messages[-1]["content"], "第一条。")
                fire_at(101.25)
                self.assertEqual(window.messages[-1]["content"], "第二条。")
                self.assertTrue(window.busy)
                fire_at(103.99)
                self.assertEqual(window.messages[-1]["content"], "第二条。")
                fire_at(104.0)
                self.assertEqual(window.messages[-1]["content"], "第三条。")
                self.assertFalse(window.busy)
                self.assertEqual([item["content"] for item in window.messages[-3:]], ["第一条。", "第二条。", "第三条。"])
                self.assertTrue(all(call.args == (1.0, 3.0) for call in gap.call_args_list))
        finally:
            window.destroy()

    def test_tool_stage_text_is_not_treated_as_an_already_sent_greeting(self):
        responses = [
            {"content": "工具阶段未发送的开场", "tool_calls": [{"id": "clock", "type": "function", "function": {"name": "get_current_time", "arguments": "{}"}}]},
            {"content": "下午好呀。"}, continuation(),
        ]
        observed = []
        def fake_api(messages, key, **kwargs):
            observed.append(copy.deepcopy(messages))
            return responses.pop(0)
        with patch.object(pet_core, "call_deepseek", side_effect=fake_api):
            self.assertEqual(pet_core.reply("下午好"), "下午好呀。")
        tool_stage = [item for item in observed[1] if item.get("tool_calls")]
        self.assertIsNone(tool_stage[0]["content"])

    def test_autoscroll_and_high_resolution_wheel_on_canvas(self):
        window = app.PetWindow()
        try:
            for index in range(45):
                window._add_message("user" if index % 2 else "assistant", f"第{index}条。" + "测试换行与滚动。" * 4)
            window.update_idletasks()
            self.assertGreater(window.chat_canvas.yview()[0], 0)
            self.assertAlmostEqual(window.chat_canvas.yview()[1], 1.0, places=2)
            before = window.chat_canvas.canvasy(0)
            window._scroll_chat(SimpleNamespace(delta=30))
            window.after_cancel(window._scroll_job)
            window._scroll_job = None
            window._animate_scroll()
            self.assertLess(window.chat_canvas.canvasy(0), before)
            window._add_message("user", "新发送的消息。")
            window.update_idletasks()
            self.assertAlmostEqual(window.chat_canvas.yview()[1], 1.0, places=2)
            window.chat_canvas.yview_moveto(0)
            window._add_message("assistant", "这是最新回复。")
            window.update_idletasks()
            self.assertAlmostEqual(window.chat_canvas.yview()[1], 1.0, places=2)
            self.assertEqual(window.chat_canvas.winfo_children(), [])
        finally:
            window.destroy()


if __name__ == "__main__":
    unittest.main()
