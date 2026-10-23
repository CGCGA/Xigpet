"""Line-addressed, automatically repairable directory for chat_history.jsonl."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
import weakref
from functools import wraps
from pathlib import Path


_LOCKS = weakref.WeakValueDictionary()


def _synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


def _atomic_write(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class MemoryCatalog:
    def __init__(self, history: Path, catalog: Path, state: Path):
        self.history = history
        self.catalog = catalog
        self.state = state
        self._lock = _LOCKS.setdefault(str(history.resolve()), threading.RLock())

    @staticmethod
    def _stamp(path: Path) -> dict:
        stat = path.stat()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    @staticmethod
    def _entry(raw: bytes, line: int, offset: int) -> dict | None:
        try:
            item = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant") or not isinstance(item.get("content"), str):
            return None
        preview = " ".join(item["content"].split())[:180]
        return {
            "line": line, "offset": offset, "bytes": len(raw),
            "time": item.get("time", ""), "role": item["role"],
            "topic": preview, "keywords": "", "digest": hashlib.sha256(raw).hexdigest(),
        }

    def _save_state(self, lines: int) -> None:
        _atomic_write(self.state, json.dumps({
            "lines": lines, "history": self._stamp(self.history),
            "catalog": self._stamp(self.catalog),
        }, ensure_ascii=False))

    @_synchronized
    def ensure(self) -> None:
        self.history.parent.mkdir(parents=True, exist_ok=True)
        if not self.history.exists():
            self.history.touch()
        try:
            saved = json.loads(self.state.read_text(encoding="utf-8"))
            if saved["history"] == self._stamp(self.history) and saved["catalog"] == self._stamp(self.catalog):
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        # Rebuild for older projects, interrupted writes, or externally edited history.
        labels = {}
        try:
            with self.catalog.open("r", encoding="utf-8") as source:
                for raw in source:
                    item = json.loads(raw)
                    labels[(item["line"], item.get("digest"))] = (item.get("topic", ""), item.get("keywords", ""))
        except (OSError, ValueError, KeyError, TypeError):
            labels = {}
        fd, name = tempfile.mkstemp(prefix="catalog-", suffix=".tmp", dir=self.history.parent)
        lines = 0
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target, self.history.open("rb") as source:
                while raw := source.readline():
                    lines += 1
                    entry = self._entry(raw, lines, source.tell() - len(raw))
                    if entry:
                        previous = labels.get((entry["line"], entry["digest"]))
                        if previous:
                            entry["topic"], entry["keywords"] = previous
                        target.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(name, self.catalog)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self._save_state(lines)

    @_synchronized
    def append(self, role: str, content: str, time: str) -> int:
        self.ensure()
        try:
            previous = json.loads(self.state.read_text(encoding="utf-8"))
            line = int(previous["lines"]) + 1
        except (OSError, ValueError, KeyError, TypeError):
            self.ensure()
            previous = json.loads(self.state.read_text(encoding="utf-8"))
            line = int(previous["lines"]) + 1
        raw = (json.dumps({"time": time, "role": role, "content": content}, ensure_ascii=False) + "\n").encode("utf-8")
        offset = self.history.stat().st_size
        with self.history.open("ab") as stream:
            stream.write(raw)
        entry = self._entry(raw, line, offset)
        if entry:
            with self.catalog.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._save_state(line)
        return line

    def entries(self):
        with self._lock:
            self.ensure()
            yield from self._entries()

    def _entries(self):
        with self.catalog.open("r", encoding="utf-8") as stream:
            for raw in stream:
                try:
                    yield json.loads(raw)
                except ValueError:
                    continue

    def search(self, query: str, limit: int = 12) -> list[dict]:
        query = query.strip().casefold()
        if not query or len(query) > 100:
            return []
        terms = [term for term in query.split() if term]
        ranked = []
        for entry in self.entries():
            searchable = f"{entry.get('time', '')} {entry.get('topic', '')} {entry.get('keywords', '')}".casefold()
            score = 5 if query in searchable else sum(1 for term in terms if term in searchable)
            if score:
                ranked.append((score, entry["line"], entry))
        ranked.sort(key=lambda value: (value[0], value[1]), reverse=True)
        return [entry for _, _, entry in ranked[:limit]]

    @_synchronized
    def read_lines(self, start: int, end: int) -> list[dict]:
        self.ensure()
        if start < 1 or end < start or end - start > 19:
            return []
        wanted = [entry for entry in self.entries() if start <= entry["line"] <= end]
        result = []
        with self.history.open("rb") as stream:
            for entry in wanted:
                stream.seek(entry["offset"])
                raw = stream.read(entry["bytes"])
                try:
                    item = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                result.append({"line": entry["line"], **item})
        return result

    @_synchronized
    def label(self, line: int, topic: str, keywords: str) -> bool:
        self.ensure()
        if line < 1 or not topic or len(topic) > 120 or len(keywords) > 160:
            return False
        entries = list(self.entries())
        found = False
        for entry in entries:
            if entry["line"] == line and entry["role"] == "user":
                entry["topic"] = " ".join(topic.split())
                entry["keywords"] = " ".join(keywords.split())
                found = True
                break
        if found:
            _atomic_write(self.catalog, "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries))
            self._save_state(json.loads(self.state.read_text(encoding="utf-8"))["lines"])
        return found
