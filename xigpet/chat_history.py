"""Local history browsing: bounded tail reads and compact, incremental date index."""
from __future__ import annotations

import json
import threading
from collections import deque
from datetime import date

from .memory_catalog import MemoryCatalog

LIVE_MESSAGE_LIMIT = 50
HISTORY_PAGE_SIZE = 50
UNKNOWN_DATE = "日期未知"


class HistoryArchive:
    def __init__(self, catalog: MemoryCatalog):
        self.catalog = catalog
        self._days: dict[str, list[tuple[int, int]]] = {}
        self._stamp = None
        self._history_stamp = None
        self._position = 0
        self._lock = threading.RLock()

    def recent(self, limit=LIVE_MESSAGE_LIMIT) -> list[dict]:
        """Seek backwards rather than scanning the full file at every startup."""
        if limit <= 0 or not self.catalog.history.exists():
            return []
        rows = deque()
        with self.catalog._lock, self.catalog.history.open("rb") as stream:
            position = stream.seek(0, 2)
            remainder = b""
            while position and len(rows) < limit:
                size = min(position, 16384)
                position -= size
                stream.seek(position)
                parts = (stream.read(size) + remainder).split(b"\n")
                remainder = parts.pop(0)
                for raw in reversed(parts):
                    row = self._decode(raw)
                    if row is not None:
                        rows.appendleft(row)
                        if len(rows) == limit:
                            break
            if not position and len(rows) < limit:
                row = self._decode(remainder)
                if row is not None:
                    rows.appendleft(row)
        return list(rows)

    @staticmethod
    def _decode(raw):
        try:
            row = json.loads(raw)
            if (isinstance(row, dict) and row.get("role") in ("user", "assistant")
                    and isinstance(row.get("content"), str)):
                return row
        except (ValueError, UnicodeDecodeError):
            pass
        return None

    @staticmethod
    def _day(value):
        try:
            return date.fromisoformat(str(value)[:10]).isoformat()
        except ValueError:
            return UNKNOWN_DATE

    def _refresh(self):
        self.catalog.ensure()
        stat = self.catalog.catalog.stat()
        history_stat = self.catalog.history.stat()
        stamp = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        history_stamp = (history_stat.st_ino, history_stat.st_size, history_stat.st_mtime_ns)
        if stamp == self._stamp and history_stamp == self._history_stamp:
            return
        # append() preserves file identity; repairs/label rewrites replace the index.
        append_only = (self._stamp is not None and self._history_stamp is not None
                       and stamp[0] == self._stamp[0] and stamp[1] > self._stamp[1]
                       and history_stamp[0] == self._history_stamp[0]
                       and history_stamp[1] > self._history_stamp[1])
        if not append_only:
            self._days.clear()
            self._position = 0
        with self.catalog.catalog.open("rb") as stream:
            stream.seek(self._position)
            for raw in stream:
                try:
                    item = json.loads(raw)
                    offset, size = int(item["offset"]), int(item["bytes"])
                    if offset < 0 or size <= 0:
                        continue
                    self._days.setdefault(self._day(item.get("time")), []).append((offset, size))
                except (ValueError, UnicodeDecodeError, KeyError, TypeError):
                    continue
            self._position = stream.tell()
        self._stamp, self._history_stamp = stamp, history_stamp

    def dates(self) -> list[tuple[str, int]]:
        with self._lock, self.catalog._lock:
            self._refresh()
            days = sorted((day for day in self._days if day != UNKNOWN_DATE), reverse=True)
            if UNKNOWN_DATE in self._days:
                days.append(UNKNOWN_DATE)
            return [(day, len(self._days[day])) for day in days]

    def page(self, day, page=0) -> tuple[list[dict], int, int]:
        """Return only one day's page, its total count, and the clamped page index."""
        with self._lock, self.catalog._lock:
            self._refresh()
            entries = self._days.get(day, [])
            total = len(entries)
            page = max(0, min(int(page), max(0, (total - 1) // HISTORY_PAGE_SIZE)))
            selected = entries[page * HISTORY_PAGE_SIZE:(page + 1) * HISTORY_PAGE_SIZE]
            rows = []
            with self.catalog.history.open("rb") as stream:
                for offset, size in selected:
                    stream.seek(offset)
                    row = self._decode(stream.read(size))
                    if row is not None and self._day(row.get("time")) == day:
                        rows.append(row)
            return rows, total, page

    def clear_index(self):
        with self._lock:
            self._days.clear()
            self._stamp = self._history_stamp = None
            self._position = 0
