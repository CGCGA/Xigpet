"""Migrate user files from older layouts without rewriting chat history."""

import json
import re
from datetime import datetime
from pathlib import Path

from scripts.media_tools import register_drawing, split_media


def _move_if_free(source: Path, target: Path):
    if source.is_file() and not source.is_symlink() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)


def _remove_empty(folder: Path):
    if folder.is_dir() and not folder.is_symlink() and not any(folder.iterdir()):
        folder.rmdir()


def migrate_layout(setting: Path, notes: Path, media: Path, history: Path):
    root = setting.parent.parent
    legacy_setting = root / "pet" / "setting.md"
    if legacy_setting != setting:
        _move_if_free(legacy_setting, setting)
        _remove_empty(legacy_setting.parent)
    legacy_notes = root / "notes"
    if legacy_notes != notes and legacy_notes.is_dir() and not legacy_notes.is_symlink():
        for source in legacy_notes.glob("*.md"):
            _move_if_free(source, notes / source.name)
        _remove_empty(legacy_notes)
    for source, title, stamp in legacy_drawing_info(media, history):
        target = media / "drawings" / source.name
        _move_if_free(source, target)
        if not source.exists():
            register_drawing(media, "drawings/" + target.name, title=title, keywords=title, created_at=stamp)


def legacy_drawing_info(media: Path, history: Path):
    legacy_drawings = [path for path in media.glob("drawing_*.png")
                       if path.is_file() and not path.is_symlink()]
    if (media / "drawings").is_symlink():
        raise ValueError("绘画目录不能是符号链接")
    # Recover names from old drawing captions; never rewrite the JSONL or its offsets.
    titles = {}
    previous = ""
    if legacy_drawings and history.is_file() and not history.is_symlink():
        wanted = {path.name for path in legacy_drawings}
        with history.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("role") != "assistant":
                    previous = ""
                    continue
                content = row.get("content", "")
                if not isinstance(content, str):
                    continue
                caption, names = split_media(content)
                for name in names:
                    if name in wanted:
                        match = re.search(r"我画了\s*(.{1,80}?)(?:[。！!\n]|$)", caption or previous)
                        if match:
                            titles[name] = match[1].strip("‘’“”\" ")
                if caption:
                    previous = caption[-200:]
    return [(source, titles.get(source.name) or "旧作 " + source.stem.removeprefix("drawing_")[:8],
             datetime.fromtimestamp(source.stat().st_mtime).astimezone().isoformat(timespec="seconds"))
            for source in legacy_drawings]
