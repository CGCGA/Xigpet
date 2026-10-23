"""Local image/GIF library and safe chat media markers."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path


EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_FILE_BYTES = 20 * 1024 * 1024
MARKER = re.compile(r"(?m)^\[\[pet-media:([^\]\n]+)\]\]$")
_INDEX_LOCK = threading.RLock()


def is_drawing(filename: str) -> bool:
    return filename.startswith(("drawings/", "drawing_"))


def _read_index(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def media_path(folder: Path, filename: str) -> Path:
    if not isinstance(filename, str) or not re.fullmatch(r"(?:drawings/)?[\w\u4e00-\u9fff .-]{1,100}", filename):
        raise ValueError("请提供媒体文件名")
    if Path(filename).name in {".", ".."} or "\\" in filename:
        raise ValueError("只能发送 media 文件夹中的文件")
    path = folder / filename
    if folder.is_symlink() or (filename.startswith("drawings/") and (folder / "drawings").is_symlink()):
        raise ValueError("不允许使用媒体目录中的符号链接")
    # Old chat markers keep working after their images have moved.
    if not path.exists() and filename.startswith("drawing_"):
        path = folder / "drawings" / filename
    if path.parent.is_symlink():
        raise ValueError("不允许使用媒体目录中的符号链接")
    if path.suffix.lower() not in EXTENSIONS or path.is_symlink() or not path.is_file():
        raise ValueError("找不到支持的图片或 GIF 文件")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("媒体文件不能超过 20 MB")
    return path


def register_drawing(folder: Path, filename: str, *, title: str, keywords="", mood="", created_at=None):
    path = media_path(folder, filename)
    if path.parent != folder / "drawings":
        raise ValueError("绘画必须保存在 media/drawings 中")
    index_path = folder / "drawings" / "index.json"
    if index_path.is_symlink():
        raise ValueError("绘画索引不能是符号链接")
    with _INDEX_LOCK:
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8"))
            if not isinstance(index, dict):
                raise ValueError("绘画索引格式无效")
        else:
            index = {}
        index[path.name] = {"title": title[:100], "keywords": str(keywords or title)[:300],
                            "mood": str(mood)[:80], "kind": "drawing",
                            "created_at": created_at or datetime.now().astimezone().isoformat(timespec="seconds")}
        descriptor, temporary = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=index_path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(index, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, index_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _library(folder: Path) -> list[tuple[str, dict]]:
    index = _read_index(folder / "index.json")
    drawings = _read_index(folder / "drawings" / "index.json")
    paths = list(folder.iterdir()) if folder.exists() else []
    drawing_dir = folder / "drawings"
    if drawing_dir.is_dir() and not drawing_dir.is_symlink():
        paths.extend(drawing_dir.iterdir())
    result = []
    for path in paths:
        name = path.relative_to(folder).as_posix()
        try:
            media_path(folder, name)
        except (OSError, ValueError):
            continue
        info = drawings.get(path.name, {}) if name.startswith("drawings/") else index.get(name, {})
        info = dict(info) if isinstance(info, dict) else {}
        info.setdefault("title", path.stem)
        info.setdefault("kind", "drawing" if is_drawing(name) else "media")
        result.append((name, info))
    return result


def search_media(folder: Path, query: str) -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > 80:
        return "情绪或关键词须为 1 至 80 个字符。"
    terms = [part.casefold() for part in query.split() if part]
    if not terms:
        terms = [query.casefold()]
    results = []
    for name, info in sorted(_library(folder)):
        mood = str(info.get("mood", ""))
        keywords = str(info.get("keywords", ""))
        title = str(info["title"])
        haystack = f"{name} {title} {mood} {keywords} {info.get('created_at', '')}".casefold()
        tags = [part.casefold() for part in f"{title} {mood} {keywords}".split() if len(part) >= 2]
        score = sum(1 for term in terms if term in haystack) + sum(1 for tag in tags if tag in query.casefold())
        if info["kind"] == "drawing" and any(word in query for word in ("作品", "画过", "旧画", "以前画", "画作")):
            score += 1
        if score:
            results.append((score, {"file": name, "title": title, "mood": mood, "keywords": keywords,
                                    "kind": info["kind"], "created_at": info.get("created_at", "")}))
    results.sort(key=lambda item: item[0], reverse=True)
    return json.dumps([item for _, item in results[:8]], ensure_ascii=False) if results else "没有找到匹配的表情包。"


def attach_media(content: str, filenames: list[str]) -> str:
    return content + "".join(f"\n[[pet-media:{name}]]" for name in filenames)


def split_media(content: str) -> tuple[str, list[str]]:
    filenames = MARKER.findall(content)
    return MARKER.sub("", content).strip(), filenames


def media_inventory(folder: Path) -> list[dict]:
    """A compact menu lets the model use expressions without an extra search."""
    entries = _library(folder)
    stock = sorted((item for item in entries if item[1]["kind"] != "drawing"),
                   key=lambda item: (not item[0].startswith("xig_"), item[0]))
    drawings = sorted((item for item in entries if item[1]["kind"] == "drawing"),
                      key=lambda item: item[1].get("created_at", ""), reverse=True)
    selected = stock[:18] + drawings[:6]
    remaining = stock[18:] + drawings[6:]
    selected.extend(remaining[:24 - len(selected)])
    return [{"file": name, "title": str(info["title"])[:100], "mood": str(info.get("mood", ""))[:70],
             "keywords": str(info.get("keywords", ""))[:100], "kind": info["kind"]} for name, info in selected]
