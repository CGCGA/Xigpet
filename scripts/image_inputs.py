"""Local user attachments and DeepSeek-compatible vision content blocks."""

from __future__ import annotations

import base64
import hashlib
import io
import re
from pathlib import Path

from PIL import Image, ImageOps


MAX_IMAGES = 4
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 36_000_000
MAX_EDGE = 4096
FORMATS = {"JPEG", "PNG", "GIF", "WEBP", "BMP"}
NAME = re.compile(r"[0-9a-f]{64}\.(?:png|jpg)")
MARKER = re.compile(r"(?m)^\[\[user-image:([0-9a-f]{64}\.(?:png|jpg))\]\]$")


def image_path(folder: Path, filename: str) -> Path:
    if not isinstance(filename, str) or not NAME.fullmatch(filename):
        raise ValueError("图片附件名称无效。")
    path = folder / filename
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("图片附件不存在，请重新选择图片。")
    if path.resolve().parent != folder.resolve() or path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("图片附件无效或过大，请重新选择图片。")
    return path


def save_image(source: str | Path | Image.Image, folder: Path) -> str:
    """Normalize rotation, strip metadata, bound payload size, and keep a local copy."""
    try:
        if isinstance(source, Image.Image):
            image = source
            opened_here = False
        else:
            path = Path(source)
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError("单张原始图片不能超过 20 MB。")
            image = Image.open(path)
            opened_here = True
        try:
            if image.format and image.format not in FORMATS:
                raise ValueError("请选择 PNG、JPEG、WebP、GIF 或 BMP 图片。")
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("图片分辨率过大，请先裁剪或缩小图片。")
            image.seek(0)
            normalized = ImageOps.exif_transpose(image).convert("RGBA")
            normalized.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            normalized.info.clear()
        finally:
            if opened_here:
                image.close()
        output = io.BytesIO()
        normalized.save(output, format="PNG")
        data, suffix = output.getvalue(), "png"
        if len(data) > MAX_IMAGE_BYTES:
            rgb = Image.new("RGB", normalized.size, "white")
            rgb.paste(normalized, mask=normalized.getchannel("A"))
            for _ in range(5):
                output = io.BytesIO()
                rgb.save(output, format="JPEG", quality=88, optimize=True)
                data, suffix = output.getvalue(), "jpg"
                if len(data) <= MAX_IMAGE_BYTES:
                    break
                rgb.thumbnail((max(1, round(rgb.width * .75)), max(1, round(rgb.height * .75))), Image.Resampling.LANCZOS)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("图片压缩后仍过大，请先缩小图片。")
        if folder.is_symlink():
            raise ValueError("图片目录不能是符号链接。")
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{hashlib.sha256(data).hexdigest()}.{suffix}"
        destination = folder / name
        if destination.is_symlink():
            raise ValueError("图片附件不能是符号链接。")
        if not destination.exists():
            with destination.open("xb") as target:
                target.write(data)
        return name
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("无法读取这张图片，请检查文件是否损坏或过大。") from exc


def attach_images(text: str, filenames: list[str]) -> str:
    return text.strip() + "".join(f"\n[[user-image:{name}]]" for name in filenames)


def split_images(content: str) -> tuple[str, list[str]]:
    return MARKER.sub("", content).strip(), MARKER.findall(content)


def vision_messages(messages: list[dict], folder: Path) -> list[dict]:
    """Attach at most four recent images; keep history on disk as short references."""
    selected = set()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            for name in reversed(split_images(message["content"])[1]):
                if len(selected) < MAX_IMAGES:
                    selected.add((index, name))
    result = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, str):
            result.append(dict(message))
            continue
        text, names = split_images(content)
        if not names:
            result.append(dict(message))
            continue
        parts = [{"type": "text", "text": text or "看看我发的图片。"}]
        for name in dict.fromkeys(names):
            note = f"图片附件：{name}"
            if (index, name) not in selected:
                parts.append({"type": "text", "text": note + "（本次未附图，需要时可用 view_shared_image 重新查看。）"})
                continue
            try:
                data = image_path(folder, name).read_bytes()
            except (OSError, ValueError):
                parts.append({"type": "text", "text": note + "（本地文件已缺失，不能查看，请勿猜测图片内容。）"})
                continue
            mime = "image/png" if name.endswith(".png") else "image/jpeg"
            parts.extend([
                {"type": "text", "text": note},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", "detail": "high",
                }},
            ])
        result.append({**message, "content": parts})
    return result
