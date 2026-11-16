"""Bounded streamed downloads; untrusted files never execute or auto-open."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid
from urllib.parse import urlsplit, unquote
from PIL import Image

from .public_http import ResourceError

MB = 1024 * 1024
LIMITS = {"image": 20 * MB, "audio": 80 * MB, "video": 250 * MB, "document": 30 * MB}
EXTENSIONS = {"image": {"jpg", "jpeg", "png", "webp", "gif"}, "audio": {"mp3", "wav", "flac", "m4a", "ogg"},
              "video": {"mp4", "webm", "mov"}, "document": {"pdf", "txt", "md"}}
FILE_MARKER = re.compile(r"(?m)^\[\[pet-file:([a-f0-9]{32})\]\]$")
ATTACHMENT_MARKER = re.compile(r"(?m)^\[\[pet-(file|voice):([a-f0-9]{32})\]\]$")
_DOWNLOAD_LOCK = threading.Lock()
_STORE_LOCK = threading.RLock()


class _MetadataReader:
    """Bound third-party tag parsing, including oversized album-art metadata."""
    def __init__(self, source):
        self.source, self.remaining = source, 4 * MB

    def read(self, size=-1):
        value = self.source.read(min(size, self.remaining + 1) if size >= 0 else self.remaining + 1)
        self.remaining -= len(value)
        if self.remaining < 0:
            raise ResourceError("音频元数据读取超过内存预算。")
        return value

    def __getattr__(self, name):
        return getattr(self.source, name)


def split_files(content):
    text, attachments = split_attachments(content)
    return text, [identifier for identifier, _ in attachments]


def split_attachments(content):
    return ATTACHMENT_MARKER.sub("", content).strip(), [(m[1], m[0]) for m in ATTACHMENT_MARKER.findall(content)]


def detect_type(path, declared, mime):
    with path.open("rb") as stream:
        head = stream.read(8192)
    if head.startswith((b"MZ", b"PK\x03\x04", b"#!", b"\x7fELF")):
        raise ResourceError("不接受程序、脚本或压缩包。")
    import filetype
    guessed = filetype.guess(head)
    if guessed:
        extension = guessed.extension
        if extension in ("mp4", "m4a", "mov"):
            tracks = _mp4_tracks(path)
            if b"vide" in tracks:
                extension = "mov" if extension == "mov" else "mp4"
            elif b"soun" in tracks:
                extension = "m4a"
            else:
                raise ResourceError("MP4/M4A/MOV中未找到有效音视频轨道。")
        if extension == "jpeg":
            extension = "jpg"
        kind = next((name for name, values in EXTENSIONS.items() if extension in values), None)
        if kind is None or kind != declared:
            raise ResourceError("文件真实类型与请求类型不符或不在支持列表。")
        if kind == "image":
            with Image.open(path) as image:
                if image.width * image.height > 25_000_000:
                    raise ResourceError("图片像素过大。")
                image.verify()
        if kind == "audio":
            import mutagen
            if head.startswith(b"ID3") and len(head) >= 10:
                tag_size = sum((head[6 + i] & 127) << (21 - 7 * i) for i in range(4))
                if tag_size > 2 * MB:
                    raise ResourceError("音频内嵌元数据过大。")
            with path.open("rb") as source:
                if mutagen.File(_MetadataReader(source)) is None:
                    raise ResourceError("音频结构无法识别。")
        # EBML also carries other data: require a WebM doctype.
        if extension == "webm" and b"webm" not in head[:256].lower():
            raise ResourceError("不是可识别的WebM文件。")
        return kind, extension
    if declared != "document" or mime.split(";")[0].lower() not in ("text/plain", "text/markdown", "text/x-markdown"):
        raise ResourceError("无法确认文件真实类型。")
    # Validate all text incrementally and reject active formats disguised as text.
    import codecs
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    with path.open("rb") as stream:
        while block := stream.read(65536):
            text = decoder.decode(block)
            if any(ord(c) < 32 and c not in "\t\r\n" for c in text):
                raise ResourceError("文档包含二进制数据。")
        decoder.decode(b"", final=True)
    beginning = head.decode("utf-8-sig", errors="ignore").lstrip().lower()
    if re.match(r"(?:<|#!|@echo|import\s|from\s+\w+\s+import|powershell|function\s|javascript:|\$[a-z_])", beginning):
        raise ResourceError("不接受HTML或脚本伪装的文档。")
    return "document", "md" if "markdown" in mime else "txt"


def _mp4_tracks(path):
    """Read only bounded atom headers/handler records; never load mdat into RAM."""
    tracks, boxes = set(), 0
    with path.open("rb") as source:
        pending = [(0, path.stat().st_size, 0)]
        while pending:
            start, end, depth = pending.pop()
            if depth > 5:
                raise ResourceError("媒体容器嵌套过深。")
            while start + 8 <= end:
                boxes += 1
                if boxes > 10000:
                    raise ResourceError("媒体容器结构过于复杂。")
                source.seek(start)
                header = source.read(8)
                size, kind = int.from_bytes(header[:4], "big"), header[4:]
                width = 8
                if size == 1:
                    size, width = int.from_bytes(source.read(8), "big"), 16
                if size == 0:
                    size = end - start
                if size < width or start + size > end:
                    raise ResourceError("媒体容器长度无效或不完整。")
                if kind in (b"moov", b"trak", b"mdia"):
                    pending.append((start + width, start + size, depth + 1))
                elif kind == b"hdlr":
                    value = source.read(min(24, size - width))
                    if len(value) >= 12:
                        tracks.add(value[8:12])
                start += size
    return tracks


class DownloadStore:
    def __init__(self, root):
        raw = Path(root).absolute()
        if raw.resolve() != raw or any(parent.is_symlink() for parent in (raw, *raw.parents)):
            raise ResourceError("下载目录及上级目录不能重定向到其他位置。")
        self.root = raw.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ResourceError("下载目录不能是符号链接。")

    def _meta(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ResourceError("附件ID无效。")
        return self.root / (identifier + ".json")

    def get(self, identifier, *, allow_expired=False):
        meta = self._meta(identifier)
        if meta.is_symlink():
            raise ResourceError("附件记录无效。")
        data = json.loads(meta.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("id") != identifier
                or data.get("kind") not in LIMITS or data.get("extension") not in EXTENSIONS[data["kind"]]
                or data.get("file") != identifier + "." + data["extension"]):
            raise ResourceError("附件记录不符合已验证文件格式。")
        expiry = data.get("sent_at", data["created_at"]) + (3 * 86400 if data.get("sent_at") else 86400)
        if not allow_expired and time.time() >= expiry:
            raise ResourceError("附件已过期，请重新下载。")
        path = self.root / data["file"]
        if path.parent != self.root or path.is_symlink() or not path.is_file():
            raise ResourceError("附件已清理或文件无效。")
        return data, path

    def voice(self, identifier):
        """Only verified cached MP3s may enter the QQ record transport."""
        data, path = self.get(identifier)
        if data["kind"] != "audio" or data["extension"] != "mp3":
            raise ResourceError("语音发送目前只支持已缓存的MP3音频。")
        if not 0 < path.stat().st_size <= LIMITS["audio"]:
            raise ResourceError("语音文件为空或超过音频大小上限。")
        try:
            if detect_type(path, "audio", "audio/mpeg") != ("audio", "mp3"):
                raise ResourceError("语音文件不是有效的MP3。")
        except ResourceError:
            raise
        except Exception as exc:
            raise ResourceError("语音MP3内容损坏或无法识别。") from exc
        return data, path

    def _write(self, data):
        target = self._meta(data["id"])
        temporary = target.with_suffix(".json.tmp")
        if target.is_symlink() or temporary.is_symlink():
            raise ResourceError("附件记录路径无效。")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)

    def cleanup(self, reserve=0):
        with _STORE_LOCK:
            now = time.time()
            records = []
            for meta in self.root.glob("*.json"):
                try:
                    data, path = self.get(meta.stem, allow_expired=True)
                    expiry = data.get("sent_at", data["created_at"]) + (3 * 86400 if data.get("sent_at") else 86400)
                    if now >= expiry:
                        path.unlink()
                        meta.unlink()
                    else:
                        records.append((data, path, meta))
                except (OSError, ValueError, KeyError):
                    continue
            total = sum(path.stat().st_size + meta.stat().st_size for _, path, meta in records)
            known = {path.name for _, path, _ in records} | {meta.name for _, _, meta in records}
            for path in self.root.iterdir():
                if path.is_symlink():
                    raise ResourceError("缓存目录出现不允许的符号链接。")
                if path.is_file() and path.name not in known:
                    if path.suffix == ".jsonl":
                        total += path.stat().st_size
                        continue
                    if now - path.stat().st_mtime >= 86400:
                        path.unlink()
                    else:
                        total += path.stat().st_size
            for data, path, meta in sorted(records, key=lambda row: row[0]["created_at"]):
                if total + reserve <= 1024 * MB and len(records) < (1000 if reserve else 1001):
                    break
                # Sent attachments keep their three-day retention window.
                if not data.get("sent_at"):
                    size = path.stat().st_size + meta.stat().st_size
                    path.unlink()
                    meta.unlink()
                    total -= size
                    records.remove((data, path, meta))
            for partial in self.root.glob("*.part"):
                if not partial.is_symlink() and now - partial.stat().st_mtime > 86400:
                    partial.unlink()
            if total + reserve > 1024 * MB or len(records) >= (1000 if reserve else 1001):
                raise ResourceError("缓存空间不足，尚在保留期的已发送附件不会提前删除。")
            if shutil.disk_usage(self.root).free < reserve + 128 * MB:
                raise ResourceError("磁盘空间不足，已停止下载。")

    def mark_sent(self, identifier):
        with _STORE_LOCK:
            data, _ = self.get(identifier)
            data["sent_at"] = time.time()
            self._write(data)

    def save(self, identifier, destination):
        # Only the local Save As dialog may supply destination; no model tool.
        data, path = self.get(identifier)
        destination = Path(destination)
        if destination.resolve().is_relative_to(self.root):
            raise ResourceError("请在下载缓存目录之外选择保存位置，保存的副本才不会被自动清理。")
        if destination.suffix.lower() != path.suffix.lower():
            raise ResourceError("保存时请保留已验证的文件扩展名。")
        with path.open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target, 65536)

    def download(self, http, url, kind, progress=None, *, headers=None):
        if not isinstance(kind, str) or kind not in LIMITS:
            raise ResourceError("下载类型无效。")
        if not _DOWNLOAD_LOCK.acquire(blocking=False):
            raise ResourceError("已有文件正在下载，请稍后再试。")
        identifier = uuid.uuid4().hex
        partial = self.root / (identifier + ".part")
        connection = response = None
        try:
            maximum = LIMITS[kind]
            self.cleanup(maximum + 65536 + 4096)
            connection, response, final_url = http.open(url, headers=headers) if headers else http.open(url)
            mime = response.getheader("Content-Type", "application/octet-stream")
            length = response.getheader("Content-Length")
            expected = int(length) if length and length.isdigit() else None
            if expected is not None and expected > maximum:
                raise ResourceError("文件超过该类型的单文件大小上限。")
            filename = unquote(urlsplit(final_url).path.rsplit("/", 1)[-1])
            from email.message import Message
            disposition = Message()
            disposition["Content-Disposition"] = response.getheader("Content-Disposition", "")
            suggested = disposition.get_filename()
            if isinstance(suggested, str) and suggested:
                filename = suggested.replace("\\", "/").rsplit("/", 1)[-1]
            suffix = Path(filename).suffix.lower().lstrip(".")
            if suffix in {"exe", "msi", "dll", "zip", "7z", "rar", "tar", "gz", "bat", "cmd", "ps1", "py", "js", "vbs", "sh", "com", "scr", "m3u8", "mpd"}:
                raise ResourceError("不下载程序、压缩包、脚本或分片播放列表。")
            size, last = 0, 0
            with partial.open("xb") as target:
                while True:
                    http.check()
                    block = response.read1(65536)
                    if not block:
                        break
                    size += len(block)
                    if size > maximum:
                        raise ResourceError("实际文件超过大小上限，已停止并清理。")
                    target.write(block)
                    if progress and time.monotonic() - last >= .5:
                        progress(size, expected)
                        last = time.monotonic()
            if not size or expected is not None and size != expected:
                raise ResourceError("文件为空或下载不完整。")
            try:
                actual_kind, extension = detect_type(partial, kind, mime)
            except Exception as exc:
                if isinstance(exc, ResourceError):
                    raise
                raise ResourceError("文件内容或结构校验失败。") from exc
            if actual_kind == "document" and extension == "txt" and suffix == "md":
                extension = "md"
            http.check()
            final = partial.with_suffix("." + extension)
            os.replace(partial, final)
            data = {"id": identifier, "file": final.name, "name": re.sub(r"[^\w\u4e00-\u9fff .-]", "_", Path(filename).stem)[:60] or "下载附件",
                    "kind": actual_kind, "extension": extension, "size": size, "created_at": time.time(),
                    "source_origin": f"{urlsplit(final_url).scheme}://{urlsplit(final_url).netloc}"}
            try:
                self._write(data)
            except Exception:
                final.unlink(missing_ok=True)
                raise
            return {"file_id": identifier, "kind": actual_kind, "name": data["name"] + "." + extension, "size": size}
        finally:
            partial.unlink(missing_ok=True)
            if connection is not None:
                http.release(connection, response)
            _DOWNLOAD_LOCK.release()

    def capture_image(self, capture, source_url, label, check):
        """Publish a bounded PNG from our browser compositor into the same cache."""
        if not _DOWNLOAD_LOCK.acquire(blocking=False):
            raise ResourceError("已有文件正在下载，请稍后再试。")
        identifier = uuid.uuid4().hex
        partial = self.root / (identifier + ".part")
        try:
            check()
            self.cleanup(LIMITS["image"] + 65536 + 4096)
            payload = capture()
            check()
            if not isinstance(payload,bytes) or not 0 < len(payload) <= LIMITS["image"]:
                raise ResourceError("浏览器图片截图为空或超过大小上限。")
            with partial.open("xb") as stream:
                view = memoryview(payload)
                for start in range(0,len(payload),65536):
                    check()
                    stream.write(view[start:start+65536])
            kind,extension=detect_type(partial,"image","image/png")
            if extension != "png":
                raise ResourceError("浏览器截图不是有效PNG图片。")
            with Image.open(partial) as image:
                if image.width*image.height>4_000_000:
                    raise ResourceError("浏览器图片截图尺寸过大。")
            check()
            target=partial.with_suffix(".png")
            os.replace(partial,target)
            data={"id":identifier,"file":target.name,"name":re.sub(r"[^\w\u4e00-\u9fff .-]","_",str(label or "网页图片截图"))[:60],
                  "kind":"image","extension":"png","size":len(payload),"created_at":time.time(),
                  "source_origin":f"{urlsplit(source_url).scheme}://{urlsplit(source_url).netloc}","capture_kind":"browser_image"}
            try:
                self._write(data)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return {"file_id":identifier,"kind":kind,"name":data["name"]+".png","size":len(payload),"resolution":"screenshot"}
        finally:
            partial.unlink(missing_ok=True)
            _DOWNLOAD_LOCK.release()


def cleanup_cache(root):
    if Path(root).exists() and _DOWNLOAD_LOCK.acquire(blocking=False):
        try:
            DownloadStore(root).cleanup()
        except (OSError, ValueError):
            pass
        finally:
            _DOWNLOAD_LOCK.release()
