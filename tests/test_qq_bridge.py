import base64
import errno
import hashlib
import io
import json
import queue
import socket
import socketserver
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image
from websocket import WebSocketBadStatusException, WebSocketTimeoutException

from xigpet import app
from xigpet import pet_core
from xigpet import qq_bridge
import test_pet_core as core_tests
from scripts.image_inputs import split_images
from scripts.media_tools import attach_media, split_media
from xigpet.qq_bridge import QQBridge, QQSettings, QQError, import_snowluma, parse_message, outgoing_segments


def message(text="你好", **overrides):
    return {"post_type": "message", "message_type": "private", "self_id": 10001,
            "user_id": 20002, "message_id": 101, "time": int(time.time()),
            "message": [{"type": "text", "data": {"text": text}}], **overrides}


def settings(**overrides):
    return QQSettings(target_qq="20002", expected_self_qq="10001", **overrides)


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Timed out waiting for local test bridge")


class LocalOneBot:
    """Small RFC6455/OneBot fixture, bound only to loopback; never uses real QQ."""
    def __init__(self, http_status=101):
        self.calls, self.clients, self.headers = [], [], []
        self.user_id = 10001
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                headers = {}
                self.rfile.readline()
                while True:
                    line = self.rfile.readline().decode("ascii").strip()
                    if not line:
                        break
                    key, _, value = line.partition(":")
                    headers[key.lower()] = value.strip()
                owner.headers.append(headers)
                if http_status != 101:
                    self.wfile.write((f"HTTP/1.1 {http_status} Rejected\r\n"
                                      "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
                    return
                key = headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                accept = base64.b64encode(hashlib.sha1(key.encode()).digest()).decode()
                self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                  f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                self.write_lock = threading.Lock()
                owner.clients.append(self)
                try:
                    while True:
                        header = self.rfile.read(2)
                        if len(header) < 2:
                            return
                        opcode, length = header[0] & 15, header[1] & 127
                        if length == 126:
                            length = struct.unpack("!H", self.rfile.read(2))[0]
                        elif length == 127:
                            length = struct.unpack("!Q", self.rfile.read(8))[0]
                        mask = self.rfile.read(4) if header[1] & 128 else b""
                        payload = self.rfile.read(length)
                        if mask:
                            payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
                        if opcode == 8:
                            return
                        if opcode != 1:
                            continue
                        call = json.loads(payload)
                        owner.calls.append(call)
                        if call["action"] == "get_login_info":
                            data = {"user_id": owner.user_id, "nickname": "fixture"}
                        elif call["action"] == "get_image":
                            data = {"url": "https://example.invalid/test.png"}
                        else:
                            data = {"message_id": 555}
                        self.send({"status": "ok", "retcode": 0, "data": data, "echo": call["echo"]})
                except (OSError, ValueError):
                    pass

            def send(self, frame):
                payload = json.dumps(frame, ensure_ascii=False).encode()
                header = b"\x81" + (bytes([len(payload)]) if len(payload) < 126 else
                                    b"\x7e" + struct.pack("!H", len(payload)))
                with self.write_lock:
                    self.wfile.write(header + payload)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"ws://127.0.0.1:{self.server.server_address[1]}/"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.02), daemon=True)
        self.thread.start()

    def send(self, frame):
        self.clients[-1].send(frame)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_import_uses_ws_endpoint_token_and_separate_bot_identity(self):
        path = self.root / "onebot_10001.json"
        path.write_text(json.dumps({"networks": {"wsServers": [{
            "host": "0.0.0.0", "port": 3001, "path": "/", "accessToken": "fixture-secret",
        }]}}), encoding="utf-8")
        before = path.read_bytes()
        config = import_snowluma(path)
        self.assertEqual(config.ws_url, "ws://127.0.0.1:3001/")
        self.assertEqual(config.expected_self_qq, "10001")
        self.assertEqual(config.target_qq, "")
        self.assertEqual(config.access_token, "fixture-secret")
        self.assertEqual(path.read_bytes(), before)
        config.target_qq = "20002"
        config.save(self.root / "saved.json")
        self.assertEqual(QQSettings.load(self.root / "saved.json"), config)

    def test_validation_rejects_self_target_and_token_in_url(self):
        for config in (QQSettings(target_qq="10001", expected_self_qq="10001"),
                       settings(ws_url="http://127.0.0.1/"), settings(ws_url="ws://host/?access_token=secret"),
                       settings(access_token="x\nAuthorization: bad"), QQSettings(target_qq="")):
            with self.assertRaises(QQError):
                config.validate()

    def test_import_accepts_windows_utf8_bom_without_modifying_config(self):
        path = self.root / "onebot_10001.json"
        path.write_text(json.dumps({"networks": {"wsServers": [{
            "host": "127.0.0.1", "port": 3301, "accessToken": "new-fixture-token",
        }]}}), encoding="utf-8-sig")
        original = path.read_bytes()
        imported = import_snowluma(path)
        self.assertEqual(imported.ws_url, "ws://127.0.0.1:3301/")
        self.assertEqual(imported.access_token, "new-fixture-token")
        self.assertEqual(path.read_bytes(), original)

    def test_connection_errors_are_actionable_without_leaking_server_secrets(self):
        cases = [
            (ConnectionRefusedError(errno.ECONNREFUSED, "secret"), "目标端口拒绝连接"),
            (WebSocketBadStatusException("secret", status_code=401), "认证被拒绝"),
            (WebSocketBadStatusException("secret", status_code=403), "认证被拒绝"),
            (WebSocketBadStatusException("secret", status_code=404), "路径不存在"),
            (WebSocketBadStatusException("secret", status_code=426), "正向 WebSocket"),
            (WebSocketTimeoutException("secret"), "连接超时"),
            (socket.gaierror(-2, "secret"), "无法解析"),
            (PermissionError(errno.EACCES, "secret"), "系统拒绝"),
            (RuntimeError("Authorization: Bearer secret"), "WebSocket 连接失败"),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, expected=expected):
                text = qq_bridge.connection_error_message(error)
                self.assertIn(expected, text)
                self.assertNotIn("secret", text)

    def test_read_loop_reports_refused_port_instead_of_generic_retry(self):
        events = queue.Queue()
        bridge = QQBridge(settings(), events, self.root, self.root)
        bridge._websocket = SimpleNamespace(create_connection=MagicMock(
            side_effect=ConnectionRefusedError(errno.ECONNREFUSED, "fixture")))
        with patch.object(bridge._stop, "wait", return_value=True):
            bridge._read_loop()
        statuses = [payload[1] for kind, payload in list(events.queue) if kind == "qq_status"]
        self.assertIn("目标端口拒绝连接", statuses[-1])
        self.assertFalse(bridge.ready.is_set())

    def test_real_handshake_auth_failure_is_visible(self):
        server = LocalOneBot(http_status=401)
        self.addCleanup(server.close)
        events = queue.Queue()
        bridge = QQBridge(settings(ws_url=server.url, access_token="fixture-secret"), events,
                          self.root, self.root)
        self.addCleanup(bridge.stop)
        bridge.start()
        wait_for(lambda: any(kind == "qq_status" and "HTTP 401" in payload[1]
                            for kind, payload in list(events.queue)))
        self.assertFalse(bridge.ready.is_set())
        self.assertEqual(server.calls, [])
        self.assertFalse(any("fixture-secret" in str(payload) for _, payload in list(events.queue)))

    def test_only_target_private_messages_accepted_and_face_only_is_ignored(self):
        self.assertEqual(parse_message(message(), settings(), "10001").text, "你好")
        for event in (message(user_id=30003), message(message_type="group"),
                      message(post_type="message_sent"), message(user_id=10001),
                      message(self_id=99999), message(message=[{"type": "face", "data": {"id": "14"}}])):
            self.assertIsNone(parse_message(event, settings(), "10001"))
        face_and_text = message(message=[{"type": "face", "data": {"id": "14"}},
                                        {"type": "text", "data": {"text": "收到🙂"}}])
        self.assertEqual(parse_message(face_and_text, settings()).text, "收到🙂")

    def test_cq_string_and_market_stickers_follow_image_path(self):
        event = message(message="你好 &amp; [CQ:face,id=14][CQ:image,file=abc,url=https://example.invalid/a?x=1&#44;y=2]")
        incoming = parse_message(event, settings())
        self.assertEqual(incoming.text, "你好 &")
        self.assertEqual(incoming.images[0]["url"], "https://example.invalid/a?x=1,y=2")
        sticker = {"type": "image", "data": {"emoji_id": "abc", "url": "https://example.invalid/a.gif"}}
        self.assertEqual(len(parse_message(message(message=[sticker]), settings()).images), 1)

    def test_image_import_normalizes_and_never_reads_remote_file_paths(self):
        raw = io.BytesIO()
        Image.new("RGB", (80, 50), "blue").save(raw, "PNG")
        call = MagicMock(return_value={"url": "https://example.invalid/fresh.png"})
        with patch.object(qq_bridge, "_http_image", side_effect=[OSError("expired"), raw.getvalue()]):
            name = qq_bridge.import_image({"url": "https://example.invalid/expired", "file": "image-id"}, self.root, call)
        call.assert_called_once_with("get_image", {"file": "image-id"})
        with Image.open(self.root / name) as image:
            self.assertEqual(image.size, (80, 50))
        with self.assertRaises(QQError):
            qq_bridge._http_image("file:///config/deepseek_key.txt")

    def bridge(self):
        server = LocalOneBot()
        self.addCleanup(server.close)
        events = queue.Queue()
        bridge = QQBridge(settings(ws_url=server.url, access_token="fixture-secret"), events,
                          self.root / "images", self.root)
        bridge.start()
        self.addCleanup(bridge.stop)
        self.assertTrue(bridge.ready.wait(3))
        return bridge, server, events

    def test_real_loopback_websocket_text_image_send_and_receive_with_dedup(self):
        bridge, server, events = self.bridge()
        self.assertEqual(server.headers[0]["authorization"], "Bearer fixture-secret")
        Image.new("RGB", (40, 40), "orange").save(self.root / "drawing.png")
        bridge.send(attach_media("画给你看。", ["drawing.png"]))
        wait_for(lambda: any(call["action"] == "send_private_msg" for call in server.calls))
        outgoing = next(call["params"] for call in server.calls if call["action"] == "send_private_msg")
        self.assertEqual(outgoing["user_id"], 20002)
        self.assertEqual(outgoing["message"][0]["data"]["text"], "画给你看。")
        self.assertTrue(outgoing["message"][1]["data"]["file"].startswith("base64://"))
        raw = io.BytesIO()
        Image.new("RGB", (40, 40), "blue").save(raw, "PNG")
        image = {"type": "image", "data": {"file": "base64://" + base64.b64encode(raw.getvalue()).decode()}}
        event = message(message=[{"type": "text", "data": {"text": "看看这个"}}, image])
        server.send(event)
        server.send(event)
        server.send(message(post_type="message_sent", message_id=102))
        server.send(message(user_id=30003, message_id=103))
        received = []
        def collect():
            while not events.empty():
                kind, payload = events.get_nowait()
                if kind == "qq_message":
                    received.append(payload[1])
            return bool(received)
        wait_for(collect)
        text, names = split_images(received[0])
        self.assertEqual(text, "看看这个")
        self.assertEqual(len(names), 1)
        self.assertTrue((self.root / "images" / names[0]).exists())
        time.sleep(.05)
        collect()
        self.assertEqual(len(received), 1)

    def test_offline_outgoing_waits_for_connection_and_history_replay_is_ignored(self):
        bridge, server, events = self.bridge()
        bridge.ready.clear()
        bridge.send("等待连接后发送")
        time.sleep(.03)
        self.assertFalse(any(call["action"] == "send_private_msg" for call in server.calls))
        bridge.ready.set()
        wait_for(lambda: any(call["action"] == "send_private_msg" for call in server.calls))
        server.send(message(time=int(time.time()) - 600))
        server.send(message(message=[{"type": "face", "data": {"id": "14"}}], message_id=105))
        time.sleep(.05)
        self.assertFalse(any(kind == "qq_message" for kind, _ in list(events.queue)))

    def test_login_identity_mismatch_does_not_send_messages(self):
        server = LocalOneBot()
        server.user_id = 99999
        self.addCleanup(server.close)
        bridge = QQBridge(settings(ws_url=server.url), queue.Queue(), self.root, self.root)
        self.addCleanup(bridge.stop)
        bridge.send("不应发送")
        bridge.start()
        wait_for(bridge._stop.is_set)
        self.assertFalse(bridge.ready.is_set())
        self.assertFalse(any(call["action"] == "send_private_msg" for call in server.calls))

    def test_connection_reconnects_and_does_not_reprocess_duplicate_message(self):
        bridge, server, events = self.bridge()
        server.send(message())
        received = []
        def collect():
            while not events.empty():
                kind, payload = events.get_nowait()
                if kind == "qq_message":
                    received.append(payload[1])
            return bool(received)
        wait_for(collect)
        server.clients[-1].request.shutdown(socket.SHUT_RDWR)
        wait_for(lambda: len(server.clients) >= 2 and bridge.ready.is_set())
        server.send(message())
        bridge.send("重连后正常发送")
        wait_for(lambda: any(call["action"] == "send_private_msg" for call in server.calls))
        time.sleep(.03)
        collect()
        self.assertEqual(received, ["你好"])

    def test_missing_ack_is_reported_without_automatic_resend(self):
        bridge, server, events = self.bridge()
        bridge.call = MagicMock(side_effect=QQError("发送结果未确认"))
        bridge.send("只提交一次")
        wait_for(lambda: any(kind == "qq_send_error" for kind, _ in list(events.queue)))
        self.assertEqual(bridge.call.call_count, 1)


class QQApplicationTests(unittest.TestCase):
    setUp = core_tests.PetCoreTests.setUp

    def window(self):
        self.addCleanup(patch.stopall)
        patch.object(app.PetWindow, "_schedule_tick", return_value=None).start()
        window = app.PetWindow()
        self.addCleanup(window.destroy)
        return window

    def test_imported_credentials_stay_pending_until_explicit_enable(self):
        window = self.window()
        with patch.object(app.QQBridge, "start"):
            window._enable_qq(settings(access_token="old-fixture-token"))
            old = window._qq_bridge
            window._open_qq_settings()
            dialog = window._qq_window
            path = pet_core.KEY_FILE.parent / "onebot_10001.json"
            path.write_text(json.dumps({"networks": {"wsServers": [{
                "port": 3301, "accessToken": "new-fixture-token",
            }]}}), encoding="utf-8")
            with patch("xigpet.qq_settings.filedialog.askopenfilename", return_value=str(path)):
                dialog._import()
            self.assertIs(window._qq_bridge, old)
            self.assertEqual(old.settings.access_token, "old-fixture-token")
            self.assertEqual(dialog.variables["access_token"].get(), "new-fixture-token")
            window._set_qq_status("QQ 未连接：旧连接正在重连。")
            self.assertIn("新配置尚未启用", dialog.status.get())
            self.assertIn("原连接状态", dialog.status.get())
            dialog._enable()
            self.assertTrue(old._stop.is_set())
            self.assertIsNot(window._qq_bridge, old)
            self.assertEqual(window._qq_bridge.settings.access_token, "new-fixture-token")
            self.assertEqual(window._qq_bridge.settings.ws_url, "ws://127.0.0.1:3301/")
            self.assertFalse(dialog._import_pending)
            self.assertNotIn("新配置尚未启用", dialog.status.get())

    def test_prearchived_qq_message_appears_once_in_history_and_model_context(self):
        line = pet_core.append_history("user", "QQ 第一条")
        later = pet_core.append_history("user", "QQ 尚待回复的第二条")
        observed = []
        def api(messages, *_args, **kwargs):
            observed.append(messages[:])
            return core_tests.continuation() if "tools_override" in kwargs else {"content": "收到第一条啦。"}
        with patch.object(pet_core, "call_deepseek", side_effect=api):
            pet_core.reply("QQ 第一条", recorded_line=line)
        users = [row["content"] for row in observed[0] if row["role"] == "user"]
        self.assertEqual(users, ["QQ 第一条"])
        history = pet_core.read_history()
        self.assertEqual([row["content"] for row in history if row["role"] == "user"],
                         ["QQ 第一条", "QQ 尚待回复的第二条"])
        with patch.object(pet_core, "call_deepseek", return_value={"content": "收到第二条啦。"}):
            pet_core.reply("QQ 尚待回复的第二条", recorded_line=later)
        self.assertEqual(len([row for row in pet_core.read_history() if row["role"] == "user"]), 2)

    def test_received_messages_archive_immediately_queue_and_reply_serially(self):
        window = self.window()
        bridge = window._qq_bridge = MagicMock()
        window.busy = True
        window._receive_qq_message("QQ 第一条")
        window._receive_qq_message("QQ 第二条")
        self.assertEqual(len(pet_core.read_history()), 2)
        self.assertEqual(len(window._qq_pending), 2)
        self.assertEqual(window.messages[-1]["content"], "QQ 第二条")
        def immediate_thread(target, args=(), **_kwargs):
            return SimpleNamespace(start=lambda: target(*args))
        response_number = 0
        def api(_messages, *_args, **kwargs):
            nonlocal response_number
            if "tools_override" in kwargs:
                return core_tests.continuation()
            response_number += 1
            return {"content": f"收到第{response_number}条啦。"}
        with patch.object(app.threading, "Thread", side_effect=immediate_thread), \
             patch.object(pet_core, "call_deepseek", side_effect=api):
            window._finish_turn()
            window._drain_events()
        history = pet_core.read_history()
        self.assertEqual(len(history), 4)
        self.assertEqual([row["content"] for row in history if row["role"] == "assistant"],
                         ["收到第1条啦。", "收到第2条啦。"])
        self.assertEqual(bridge.send.call_count, 2)
        self.assertFalse(window.busy)
        self.assertEqual(window._qq_pending, app.deque())

    def test_image_only_qq_message_keeps_pixels_and_is_not_double_archived(self):
        window = self.window()
        window._qq_bridge = MagicMock()
        window.busy = True
        name = core_tests.PetCoreTests._shared_image(self)
        window._receive_qq_message("\n[[user-image:" + name + "]]")
        content, line = window._qq_pending[0]
        with patch.object(pet_core, "call_deepseek", side_effect=[{"content": "看到了蓝色图片。"}, core_tests.continuation()]):
            app.PetWindow._qq_reply_worker(window.events, content, line)
        self.assertEqual(len([row for row in pet_core.read_history() if row["role"] == "user"]), 1)
        self.assertEqual(split_images(pet_core.read_history()[0]["content"])[1], [name])

    def test_outgoing_local_and_proactive_replies_mirror_at_delivery_only(self):
        window = self.window()
        bridge = window._qq_bridge = MagicMock()
        window._begin_turn()
        window.events.put(("reply", "本地回复也发 QQ。"))
        window.events.put(("turn_done", ""))
        window._drain_events()
        bridge.send.assert_called_once_with("本地回复也发 QQ。")
        self.assertEqual(window.messages[-1]["content"], "本地回复也发 QQ。")
        window._begin_turn()
        window._pending_replies.append(("proactive_reply", "到时间吃饭啦。"))
        window._deliver_next_reply()
        self.assertEqual(bridge.send.call_count, 2)
        window._qq_bridge = MagicMock()
        window._next_reply_at = 0
        window._pending_replies.append(("reply", "旧模式开始生成的回复。"))
        window._deliver_next_reply()
        window._qq_bridge.send.assert_not_called()

    def test_right_click_settings_enable_disable_and_ignore_stale_transport_events(self):
        window = self.window()
        window._tray = MagicMock()
        window._minimize_to_tray()
        index = window._desktop._qq_menu_index
        self.assertIn("QQ", window._desktop.menu.entrycget(index, "label"))
        window._desktop.menu.invoke(index)
        self.assertTrue(window._qq_window.winfo_exists())
        with patch.object(app.QQBridge, "start"):
            window._enable_qq(settings())
        self.assertIn("已开启", window._desktop.menu.entrycget(index, "label"))
        old = window._qq_bridge
        window._disable_qq()
        window.events.put(("qq_message", (old, "模式关闭后不应进入聊天")))
        window._drain_events()
        self.assertEqual(pet_core.read_history(), [])
        self.assertIsNone(window._qq_bridge)

    def test_no_key_still_archives_qq_message_without_calling_model(self):
        window = self.window()
        window._qq_bridge = MagicMock()
        pet_core.KEY_FILE.write_text("", encoding="utf-8")
        with patch.object(pet_core, "call_deepseek") as api:
            window._receive_qq_message("先帮我保存")
            api.assert_not_called()
        self.assertEqual(pet_core.read_history()[0]["content"], "先帮我保存")
        self.assertEqual(len(window._qq_pending), 1)
        self.assertIn("Key", window._qq_status)
