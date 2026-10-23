"""SnowLuma OneBot v11 private-chat bridge. No Tk or model calls in transport threads."""
from __future__ import annotations

import base64
import errno
import io
import json
import queue
import re
import socket
import ssl
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from PIL import Image

from scripts.image_inputs import MAX_IMAGES, MAX_SOURCE_BYTES, attach_images, save_image
from scripts.media_tools import media_path, split_media


class QQError(ValueError):
    pass


def connection_error_message(exc: Exception) -> str:
    """Explain connection failures without exposing response bodies or credentials."""
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    name = type(exc).__name__
    if status in (401, 403):
        detail = f"认证被拒绝（HTTP {status}），请重新读取当前 OneBot 服务的 Access Token。"
    elif status == 404:
        detail = "WebSocket 路径不存在（HTTP 404），请检查配置中的 path。"
    elif status in (400, 405, 426):
        detail = f"WebSocket 握手失败（HTTP {status}），请确认连接的是正向 WebSocket 服务，而非 WebUI 或普通 HTTP 端口。"
    elif isinstance(status, int):
        detail = f"WebSocket 服务拒绝连接（HTTP {status}），请检查 SnowLuma 的服务设置。"
    elif isinstance(exc, ConnectionRefusedError) or code in (errno.ECONNREFUSED, 10061):
        detail = "目标端口拒绝连接，请在 SnowLuma 中启用正向 WebSocket 服务，并核对地址、端口；QQ 已登录不代表该服务已开启。"
    elif isinstance(exc, socket.gaierror):
        detail = "无法解析服务器地址，请检查 WebSocket 主机名。"
    elif isinstance(exc, (TimeoutError, socket.timeout)) or code in (errno.ETIMEDOUT, 10060) or name == "WebSocketTimeoutException":
        detail = "连接超时，请检查地址、端口以及防火墙或网络设置。"
    elif isinstance(exc, PermissionError) or code in (errno.EACCES, 10013):
        detail = "连接被系统拒绝，请检查防火墙或安全软件是否阻止了程序。"
    elif isinstance(exc, ssl.SSLError):
        detail = "TLS 连接失败，请检查 wss 地址与服务证书。"
    elif name == "WebSocketProxyException":
        detail = "代理连接失败，请检查代理设置；本机 SnowLuma 应使用本机 WebSocket 地址。"
    elif isinstance(exc, QQError) and str(exc) in ("登录信息读取超时。", "SnowLuma 已关闭 WebSocket 连接。"):
        detail = str(exc)
    elif name == "WebSocketConnectionClosedException" or code in (errno.ECONNRESET, errno.ECONNABORTED, 10054, 10053):
        detail = "SnowLuma 连接已断开，请检查服务是否仍在运行。"
    else:
        detail = "WebSocket 连接失败，请核对正向服务的地址、端口、路径和 Token。"
    return f"QQ 未连接：{detail} 正在重连。"


@dataclass
class QQSettings:
    ws_url: str = "ws://127.0.0.1:3001/"
    access_token: str = ""
    target_qq: str = ""
    expected_self_qq: str = ""
    snowluma_config: str = ""

    def validate(self):
        try:
            url = urlsplit(self.ws_url)
            port = url.port
        except ValueError as exc:
            raise QQError("WebSocket 地址无效。") from exc
        if (url.scheme not in ("ws", "wss") or not url.hostname or url.username or url.password
                or url.query or url.fragment or port == 0):
            raise QQError("请填写 ws:// 或 wss:// 地址；Token 单独填写，不要放在地址中。")
        if not re.fullmatch(r"[1-9]\d{4,11}", self.target_qq):
            raise QQError("请填写接收宠物消息的用户 QQ 号。")
        if self.expected_self_qq and not re.fullmatch(r"[1-9]\d{4,11}", self.expected_self_qq):
            raise QQError("SnowLuma 登录 QQ 号格式无效。")
        if self.target_qq == self.expected_self_qq:
            raise QQError("用户 QQ 不能与 SnowLuma 登录的宠物 QQ 相同。")
        if "\r" in self.access_token or "\n" in self.access_token:
            raise QQError("Access Token 不能包含换行。")
        return self

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(**{key: value for key, value in data.items()
                          if key in cls.__dataclass_fields__ and isinstance(value, str)})
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

    def save(self, path):
        self.validate()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def import_snowluma(path: Path) -> QQSettings:
    """Read only network credentials, never modify SnowLuma or load account databases."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        networks = data.get("networks", data)
        candidates = [item for item in networks.get("wsServers", [])
                      if isinstance(item, dict) and item.get("enabled") is not False
                      and item.get("role", "Universal") in ("Universal", "universal", "auto")]
        if not candidates:
            candidates = [item for item in networks.get("httpServers", [])
                          if isinstance(item, dict) and item.get("enabled") is not False
                          and item.get("enableWebSocket") is True]
        if not candidates:
            raise QQError("此配置没有启用可双向收发的 WebSocket 服务，请先在 SnowLuma 中开启。")
        item = candidates[0]
        host = str(item.get("host") or "127.0.0.1")
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        port = int(item.get("port", 3001))
        suffix = "/" + str(item.get("path") or "/").lstrip("/")
        match = re.fullmatch(r"onebot_(\d+)\.json", path.name)
        return QQSettings(f"ws://{host}:{port}{suffix}", str(item.get("accessToken") or ""),
                          expected_self_qq=match[1] if match else "", snowluma_config=str(path.resolve()))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        if isinstance(exc, QQError):
            raise
        raise QQError("无法读取 SnowLuma 的 OneBot 配置文件。") from exc


def _unescape(value):
    return str(value).replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def cq_segments(message):
    if isinstance(message, list):
        return [item for item in message if isinstance(item, dict)]
    if not isinstance(message, str):
        return []
    segments, previous = [], 0
    for match in re.finditer(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]", message):
        if match.start() > previous:
            segments.append({"type": "text", "data": {"text": _unescape(message[previous:match.start()])}})
        data = {}
        for pair in match[2].lstrip(",").split(","):
            key, separator, value = pair.partition("=")
            if separator:
                data[key] = _unescape(value)
        segments.append({"type": match[1], "data": data})
        previous = match.end()
    if previous < len(message):
        segments.append({"type": "text", "data": {"text": _unescape(message[previous:])}})
    return segments


@dataclass
class QQIncoming:
    message_id: str
    text: str
    images: list[dict]


def parse_message(event, settings: QQSettings, self_qq="") -> QQIncoming | None:
    if not isinstance(event, dict) or event.get("post_type") != "message" or event.get("message_type") != "private":
        return None
    sender = str(event.get("user_id", ""))
    account = str(event.get("self_id", ""))
    if sender != settings.target_qq or sender == account or sender == self_qq:
        return None
    if settings.expected_self_qq and account != settings.expected_self_qq:
        return None
    if self_qq and account != self_qq:
        return None
    text, images = [], []
    for segment in cq_segments(event.get("message", event.get("raw_message", ""))):
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        if segment.get("type") == "text" and isinstance(data.get("text"), str):
            text.append(data["text"])
        elif segment.get("type") == "image":
            images.append(data)
        # face is only a numeric ID; record/voice/video/forward are not chat text.
    content = "".join(text).strip()
    if not content and not images:
        return None
    return QQIncoming(str(event.get("message_id", "")), content, images)


def _http_image(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise QQError("图片地址无效。")
    # Never send the OneBot token to an image host, and never open a remote file:// path.
    request = Request(url, headers={"User-Agent": "Xigpet/QQ-Bridge"})
    with urlopen(request, timeout=15) as response:
        data = response.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise QQError("QQ 图片超过 20 MB。")
    return data


def import_image(data: dict, folder: Path, call) -> str:
    file = str(data.get("file", ""))
    url = str(data.get("url", ""))
    raw = None
    if file.startswith("base64://"):
        if len(file) > MAX_SOURCE_BYTES * 4 // 3 + 32:
            raise QQError("QQ 图片过大。")
        raw = base64.b64decode(file[9:], validate=True)
    else:
        if not url and file.startswith(("https://", "http://")):
            url = file
        if url:
            try:
                raw = _http_image(url)
            except (OSError, ValueError):
                pass  # get_image may refresh an expired QQ CDN URL.
        if raw is None and file:
            fresh = call("get_image", {"file": file})
            url = str(fresh.get("url") or fresh.get("file") or "")
            raw = _http_image(url)
    if raw is None or len(raw) > MAX_SOURCE_BYTES:
        raise QQError("没有可读取的 QQ 图片。")
    with Image.open(io.BytesIO(raw)) as image:
        return save_image(image, folder)


def outgoing_segments(content: str, media_folder: Path) -> list[dict]:
    text, pictures = split_media(content)
    segments = [{"type": "text", "data": {"text": text}}] if text else []
    for name in pictures:
        data = media_path(media_folder, name).read_bytes()
        segments.append({"type": "image", "data": {"file": "base64://" + base64.b64encode(data).decode("ascii")}})
    return segments


class QQBridge:
    def __init__(self, settings: QQSettings, events, image_folder: Path, media_folder: Path):
        self.settings = settings.validate()
        self.events, self.image_folder, self.media_folder = events, image_folder, media_folder
        self._stop = threading.Event()
        self.ready = threading.Event()
        self._lock = threading.RLock()
        self._socket = None
        self._pending = {}
        self._outgoing = queue.Queue()
        self._incoming = queue.Queue()
        self._seen = OrderedDict()
        self._self_qq = ""
        self._login_echo = ""
        self._started_at = time.time()

    def _emit(self, kind, value):
        if not self._stop.is_set():
            self.events.put((kind, (self, value)))

    def start(self):
        try:
            import websocket
        except ImportError as exc:
            raise QQError("缺少 websocket-client，请先安装 requirements.txt 中的依赖。") from exc
        self._websocket = websocket
        for operation in (self._read_loop, self._send_loop, self._receive_loop):
            threading.Thread(target=operation, daemon=True, name=f"XigpetQQ-{operation.__name__}").start()

    def stop(self):
        self._stop.set()
        self.ready.clear()
        self._outgoing.put(None)
        self._incoming.put(None)
        with self._lock:
            socket, self._socket = self._socket, None
        if socket is not None:
            try:
                socket.shutdown()
            except Exception:
                pass
        self._fail_pending()

    def send(self, content):
        if not self._stop.is_set():
            self._outgoing.put(content)

    def _fail_pending(self):
        with self._lock:
            for signal, result in self._pending.values():
                result.update(status="failed", retcode=-1)
                signal.set()
            self._pending.clear()

    def call(self, action, params, timeout=20):
        echo = uuid.uuid4().hex
        signal, response = threading.Event(), {}
        with self._lock:
            socket = self._socket
            if socket is None or not self.ready.is_set() or self._stop.is_set():
                raise QQError("QQ 尚未连接。")
            self._pending[echo] = signal, response
        try:
            socket.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            if not signal.wait(timeout):
                raise QQError("QQ 接口超时；发送结果未确认，请先检查 QQ，避免重复发送。")
            if response.get("status") != "ok" or response.get("retcode") != 0:
                raise QQError("SnowLuma 操作失败或连接断开；发送结果未确认，请检查 QQ。")
            return response.get("data") or {}
        except OSError as exc:
            raise QQError("QQ 连接中断，发送结果未确认。") from exc
        finally:
            with self._lock:
                self._pending.pop(echo, None)

    def _read_loop(self):
        delay = 1
        while not self._stop.is_set():
            socket = None
            try:
                self._emit("qq_status", "正在连接 SnowLuma…")
                headers = {"Authorization": f"Bearer {self.settings.access_token}"} if self.settings.access_token else {}
                socket = self._websocket.create_connection(self.settings.ws_url, timeout=5, header=headers,
                                                           enable_multithread=True, suppress_origin=True,
                                                           http_no_proxy=["127.0.0.1", "localhost", "::1"])
                socket.settimeout(1)
                with self._lock:
                    if self._stop.is_set():
                        socket.close()
                        return
                    self._socket = socket
                    self._login_echo = uuid.uuid4().hex
                socket.send(json.dumps({"action": "get_login_info", "params": {}, "echo": self._login_echo}))
                login_deadline = time.monotonic() + 10
                while not self._stop.is_set():
                    try:
                        raw = socket.recv()
                    except self._websocket.WebSocketTimeoutException:
                        if not self.ready.is_set() and time.monotonic() > login_deadline:
                            raise QQError("登录信息读取超时。")
                        continue
                    if not raw:
                        raise QQError("SnowLuma 已关闭 WebSocket 连接。")
                    self._route_frame(raw)
                    if self.ready.is_set():
                        delay = 1
            except Exception as exc:
                self._emit("qq_status", connection_error_message(exc))
            finally:
                self.ready.clear()
                with self._lock:
                    if self._socket is socket:
                        self._socket = None
                self._fail_pending()
                if socket is not None:
                    try:
                        socket.close(timeout=0)
                    except Exception:
                        pass
            if self._stop.wait(delay):
                break
            delay = min(30, delay * 2)

    def _route_frame(self, raw):
        try:
            frame = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(frame, dict):
            return
        echo = frame.get("echo")
        if echo == self._login_echo:
            data = frame.get("data") or {}
            account = str(data.get("user_id", "")) if isinstance(data, dict) else ""
            if (frame.get("retcode") != 0 or not account.isdecimal()
                    or account == self.settings.target_qq
                    or (self.settings.expected_self_qq and account != self.settings.expected_self_qq)):
                self._emit("qq_status", "登录 QQ 与设置不符，或用户 QQ 填成了宠物 QQ；请修改设置后重新启用。")
                self.stop()
                return
            self._self_qq = account
            self.ready.set()
            self._emit("qq_status", "QQ 已连接 · 文字与图片双向同步")
        elif isinstance(echo, str):
            with self._lock:
                pending = self._pending.get(echo)
                if pending:
                    pending[1].update(frame)
                    pending[0].set()
        elif frame.get("post_type") == "message":
            # SnowLuma history sync must not make old messages trigger fresh replies.
            stamp = frame.get("time")
            if isinstance(stamp, (int, float)) and stamp < self._started_at - 120:
                return
            self._incoming.put(frame)

    def _send_loop(self):
        while not self._stop.is_set():
            content = self._outgoing.get()
            if content is None:
                return
            while not self._stop.is_set() and not self.ready.wait(1):
                pass
            if self._stop.is_set():
                return
            try:
                segments = outgoing_segments(content, self.media_folder)
                if segments:
                    self.call("send_private_msg", {"user_id": int(self.settings.target_qq), "message": segments})
            except Exception as exc:
                detail = str(exc) if isinstance(exc, QQError) else "图片无法发送或 QQ 连接中断。"
                self._emit("qq_send_error", detail)

    def _receive_loop(self):
        while not self._stop.is_set():
            event = self._incoming.get()
            if event is None:
                return
            while not self._stop.is_set() and not self.ready.wait(1):
                pass
            if self._stop.is_set():
                return
            incoming = parse_message(event, self.settings, self._self_qq)
            if incoming is None:
                continue
            key = f"{event.get('self_id')}:{incoming.message_id}"
            if incoming.message_id and key in self._seen:
                continue
            images, notes = [], []
            for image in incoming.images[:MAX_IMAGES]:
                try:
                    images.append(import_image(image, self.image_folder, self.call))
                except Exception:
                    notes.append("（这张 QQ 图片读取失败，未附图，不能查看。）")
            if len(incoming.images) > MAX_IMAGES:
                notes.append(f"（本条 QQ 消息只接收前 {MAX_IMAGES} 张图片。）")
            if self._stop.is_set():
                return
            if incoming.message_id:
                self._seen[key] = None
                while len(self._seen) > 4096:
                    self._seen.popitem(last=False)
            text = incoming.text + ("\n" + "\n".join(notes) if notes else "")
            self._emit("qq_message", attach_images(text, images))
