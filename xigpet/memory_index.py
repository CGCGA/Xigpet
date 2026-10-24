"""Rebuildable SQLite search index over immutable chat/important-memory sources."""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .memory_search import features, index_tokens, search_features, snippet


CHUNK_SIZE, CHUNK_OVERLAP = 1400, 100


class MemoryError(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def stamp(path):
    stat = path.stat()
    return {"id": stat.st_ino, "size": stat.st_size, "mtime": stat.st_mtime_ns}


class MemoryIndex:
    def __init__(self, catalog, important, path):
        self.catalog, self.important, self.path = catalog, Path(important), Path(path)
        self._lock = catalog._lock
        self.fts = True
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(line INTEGER PRIMARY KEY,offset INTEGER,bytes INTEGER,digest TEXT,
                    role TEXT,time TEXT,topic TEXT,keywords TEXT);
                CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY,kind TEXT,ref TEXT,line INTEGER,
                    chunk INTEGER,role TEXT,time TEXT,body TEXT,terms TEXT,offset INTEGER,bytes INTEGER,digest TEXT);
                CREATE INDEX IF NOT EXISTS docs_line ON docs(kind,line);
                CREATE TABLE IF NOT EXISTS blocks(ref TEXT PRIMARY KEY,start INTEGER,end INTEGER,fingerprint TEXT,
                    title TEXT,summary TEXT,details TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS contexts(ref TEXT PRIMARY KEY,turn_id TEXT,payload TEXT,created REAL);
            """)
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(terms)")
            except sqlite3.OperationalError:
                self.fts = False

    @contextmanager
    def _db(self):
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise MemoryError("记忆索引及目录不能是符号链接。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA cache_size=-2048")
            db.execute("PRAGMA temp_store=FILE")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _meta(db, key, default=None):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set_meta(db, key, value):
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    def _delete_docs(self, db, where, parameters=()):
        if self.fts:
            db.execute("DELETE FROM memory_fts WHERE rowid IN (SELECT id FROM docs WHERE " + where + ")", parameters)
        db.execute("DELETE FROM docs WHERE " + where, parameters)

    def _add_doc(self, db, *, kind, ref, line, body, role="", time="", chunk=0, tags="", offset=0, size=0, source_digest=""):
        tokens = index_tokens(body + " " + tags)
        row = db.execute("INSERT INTO docs(kind,ref,line,chunk,role,time,body,terms,offset,bytes,digest) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (kind, ref, line, chunk, role, time, body, tokens, offset, size, source_digest))
        if self.fts:
            db.execute("INSERT INTO memory_fts(rowid,terms) VALUES(?,?)", (row.lastrowid, tokens))

    def _source_raw(self, path, offset, size, expected):
        if path.is_symlink() or path.parent.is_symlink():
            raise MemoryError("不读取符号链接形式的记忆文件。")
        with path.open("rb") as source:
            source.seek(offset)
            raw = source.read(size)
        if digest(raw) != expected:
            raise MemoryError("记忆原文已变化，正在等待索引重新同步；不能使用旧摘要猜测。")
        return raw

    def _sync_entry(self, db, entry, source):
        old = db.execute("SELECT * FROM messages WHERE line=?", (entry["line"],)).fetchone()
        changed = old is None or old["digest"] != entry["digest"]
        labels_changed = old is not None and (old["topic"] != entry.get("topic", "") or old["keywords"] != entry.get("keywords", ""))
        if changed:
            source.seek(entry["offset"])
            raw = source.read(entry["bytes"])
            if digest(raw) != entry["digest"]:
                raise MemoryError("聊天记录在建立索引时发生变化，请重试。")
            item = json.loads(raw)
            body = item["content"]
            self._delete_docs(db, "kind='message' AND line=?", (entry["line"],))
            for chunk, start in enumerate(range(0, max(1, len(body)), CHUNK_SIZE - CHUNK_OVERLAP)):
                self._add_doc(db, kind="message", ref=f"m{entry['line']}-{entry['digest'][:10]}", line=entry["line"],
                              body=body[start:start + CHUNK_SIZE], role=entry["role"], time=entry.get("time", ""), chunk=start,
                              tags=entry.get("topic", "") + " " + entry.get("keywords", ""))
            db.execute("INSERT OR IGNORE INTO changed VALUES(?)", (entry["line"],))
        elif labels_changed:
            for row in db.execute("SELECT id,body FROM docs WHERE kind='message' AND line=?", (entry["line"],)).fetchall():
                tokens = index_tokens(row["body"] + " " + entry.get("topic", "") + " " + entry.get("keywords", ""))
                db.execute("UPDATE docs SET terms=? WHERE id=?", (tokens, row["id"]))
                if self.fts:
                    db.execute("UPDATE memory_fts SET terms=? WHERE rowid=?", (tokens, row["id"]))
        db.execute("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?)",
                   (entry["line"], entry["offset"], entry["bytes"], entry["digest"], entry["role"],
                    entry.get("time", ""), entry.get("topic", ""), entry.get("keywords", "")))

    def sync(self):
        with self._lock:
            self.catalog.ensure()
            current = stamp(self.catalog.catalog)
            with self._db() as db:
                previous = self._meta(db, "catalog")
                if current != previous:
                    db.execute("CREATE TEMP TABLE changed(line INTEGER PRIMARY KEY)")
                    append = (previous is not None and previous["id"] == current["id"] and current["size"] > previous["size"])
                    if not append:
                        db.execute("CREATE TEMP TABLE seen(line INTEGER PRIMARY KEY)")
                        db.execute("CREATE TEMP TABLE old_lines AS SELECT line FROM messages")
                    with self.catalog.catalog.open("rb") as entries, self.catalog.history.open("rb") as source:
                        if append:
                            entries.seek(previous["size"])
                        for raw in entries:
                            entry = json.loads(raw)
                            if not append:
                                db.execute("INSERT INTO seen VALUES(?)", (entry["line"],))
                            self._sync_entry(db, entry, source)
                    if not append:
                        db.execute("INSERT OR IGNORE INTO changed SELECT line FROM messages WHERE line NOT IN (SELECT line FROM seen)")
                        self._delete_docs(db, "kind='message' AND line NOT IN (SELECT line FROM seen)")
                        db.execute("DELETE FROM messages WHERE line NOT IN (SELECT line FROM seen)")
                        if db.execute("SELECT 1 FROM changed JOIN old_lines USING(line) LIMIT 1").fetchone():
                            db.execute("DELETE FROM contexts")
                    invalid = [row[0] for row in db.execute("SELECT ref FROM blocks WHERE EXISTS(SELECT 1 FROM changed WHERE line BETWEEN blocks.start AND blocks.end)")]
                    for ref in invalid:
                        self._delete_docs(db, "kind='summary' AND ref=?", (ref,))
                        db.execute("DELETE FROM blocks WHERE ref=?", (ref,))
                    self._set_meta(db, "catalog", current)
                important_stamp = stamp(self.important) if self.important.exists() else None
                if self._meta(db, "important", "missing") != important_stamp:
                    self._delete_docs(db, "kind='important'")
                    if important_stamp is not None:
                        if self.important.is_symlink() or self.important.parent.is_symlink():
                            raise MemoryError("重要记忆不能是符号链接。")
                        with self.important.open("rb") as source:
                            for number, raw in enumerate(source, 1):
                                body = raw.decode("utf-8").rstrip("\r\n")
                                for start in range(0, len(body), CHUNK_SIZE - CHUNK_OVERLAP):
                                    self._add_doc(db, kind="important", ref=f"i{number}:{start}-{digest(raw)[:10]}", line=number,
                                                  body=body[start:start + CHUNK_SIZE], chunk=start, role="memory",
                                                  offset=source.tell() - len(raw), size=len(raw), source_digest=digest(raw))
                    self._set_meta(db, "important", important_stamp)

    def _read_message(self, db, line):
        row = db.execute("SELECT * FROM messages WHERE line=?", (line,)).fetchone()
        if row is None:
            raise MemoryError("找不到这条历史原文。")
        raw = self._source_raw(self.catalog.history, row["offset"], row["bytes"], row["digest"])
        item = json.loads(raw)
        return {"ref": f"m{line}-{row['digest'][:10]}", "line": line, "role": item["role"], "time": item.get("time", ""), "content": item["content"]}

    def recent(self, limit=32, *, before_line=None):
        self.sync()
        with self._lock, self._db() as db:
            query, parameters = "SELECT line FROM messages", []
            if before_line is not None:
                query += " WHERE line<?"
                parameters.append(before_line)
            lines = [row[0] for row in db.execute(query + " ORDER BY line DESC LIMIT ?", (*parameters, limit))]
            return [self._read_message(db, line) for line in reversed(lines)]

    def search(self, query, *, limit=8, before_line=None, role=None, date_from=None, date_to=None):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500 or type(limit) is not int or not 1 <= limit <= 20:
            raise MemoryError("查询须为1–500个字，结果数须为1–20。")
        if role not in (None, "user", "assistant"):
            raise MemoryError("来源角色仅支持 user 或 assistant。")
        for value in (date_from, date_to):
            if value is not None:
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except (ValueError, TypeError) as exc:
                    raise MemoryError("日期须为 YYYY-MM-DD。") from exc
        terms, grams = search_features(query)
        if not terms:
            return []
        self.sync()
        with self._lock, self._db() as db:
            filters, params = [], []
            if before_line is not None:
                filters.append("(d.kind='important' OR d.line<?)")
                params.append(before_line)
            if role is not None:
                filters.append("d.role=?")
                params.append(role)
            if date_from:
                filters.append("substr(d.time,1,10)>=?")
                params.append(date_from)
            if date_to:
                filters.append("substr(d.time,1,10)<=?")
                params.append(date_to)
            where = " AND " + " AND ".join(filters) if filters else ""
            if self.fts:
                match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                rows = db.execute("SELECT d.*,bm25(memory_fts) AS rank FROM memory_fts JOIN docs d ON d.id=memory_fts.rowid "
                                  "WHERE memory_fts MATCH ?" + where + " ORDER BY rank LIMIT 120", (match, *params)).fetchall()
            else:
                matches = " OR ".join("d.terms LIKE ?" for _ in terms)
                rows = db.execute("SELECT d.*, -1.0 AS rank FROM docs d WHERE (" + matches + ")" + where +
                                  " ORDER BY d.line DESC LIMIT 120", (*["%" + t + "%" for t in terms], *params)).fetchall()
            peak = max([-row["rank"] for row in rows] or [1]) or 1
            ranked = []
            for row in rows:
                _, body_grams = features(row["body"])
                fuzzy = len(grams & body_grams) / max(1, len(grams | body_grams))
                weight = {"user": 1.5, "assistant": 1.0, "memory": 1.4, "summary": .85}.get(row["role"], 1)
                score = (.7 * (-row["rank"] / peak) + .3 * fuzzy) * weight
                if query.casefold() in row["body"].casefold():
                    score += .2
                ranked.append((score, row["line"], row))
            result, seen = [], set()
            for score, _, row in sorted(ranked, key=lambda item: (-item[0], -item[1])):
                if row["ref"] in seen:
                    continue
                seen.add(row["ref"])
                try:
                    if row["kind"] == "message":
                        original = self._read_message(db, row["line"])
                        text, start = snippet(original["content"][row["chunk"]:row["chunk"] + CHUNK_SIZE], query)
                        offset = row["chunk"] + start
                    elif row["kind"] == "important":
                        raw = self._source_raw(self.important, row["offset"], row["bytes"], row["digest"])
                        text, start = snippet(raw.decode("utf-8").rstrip("\r\n")[row["chunk"]:row["chunk"] + CHUNK_SIZE], query)
                        offset = row["chunk"] + start
                    else:
                        text, offset = snippet(row["body"], query)
                except (MemoryError, ValueError, OSError):
                    continue
                block = db.execute("SELECT ref FROM blocks WHERE start<=? AND end>=? ORDER BY start DESC LIMIT 1", (row["line"], row["line"])).fetchone() if row["kind"] == "message" else None
                result.append({"ref": row["ref"], "kind": row["kind"], "line": row["line"], "role": row["role"],
                               "time": row["time"], "score": round(score, 4), "excerpt": text, "offset": offset,
                               "block_id": block[0] if block else (row["ref"] if row["kind"] == "summary" else None)})
                if len(result) >= limit:
                    break
            return result

    def read(self, ref, *, offset=0, limit=5000):
        if not isinstance(ref, str) or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 6000:
            raise MemoryError("引用、偏移或读取长度无效。")
        self.sync()
        with self._lock, self._db() as db:
            message_ref = re.fullmatch(r"m([1-9]\d*)(?:-([0-9a-f]{10}))?", ref)
            if message_ref:
                result = self._read_message(db, int(message_ref[1]))
                if message_ref[2] and result["ref"] != ref:
                    raise MemoryError("该行原文已变更，请重新检索；不能沿用旧引用。")
                content = result.pop("content")
            elif re.fullmatch(r"i[1-9]\d*:\d+-[0-9a-f]{10}", ref):
                row = db.execute("SELECT * FROM docs WHERE kind='important' AND ref=?", (ref,)).fetchone()
                if row is None:
                    raise MemoryError("找不到该重要记忆引用。")
                content = self._source_raw(self.important, row["offset"], row["bytes"], row["digest"]).decode("utf-8").rstrip("\r\n")
                result = {"ref": ref, "kind": "important", "line": row["line"], "file": "important.md"}
            elif ref.startswith("b"):
                row = db.execute("SELECT * FROM blocks WHERE ref=?", (ref,)).fetchone()
                if row is None:
                    raise MemoryError("找不到此压缩块，原文可能已变更。")
                content = json.dumps({"title": row["title"], "summary": row["summary"], **json.loads(row["details"])}, ensure_ascii=False)
                result = {"ref": ref, "kind": "summary", "start_line": row["start"], "end_line": row["end"]}
            elif re.fullmatch(r"c[0-9a-f]{32}", ref):
                row = db.execute("SELECT payload FROM contexts WHERE ref=?", (ref,)).fetchone()
                if row is None:
                    raise MemoryError("此轮工具缓存已过期；聊天原文仍在历史中。")
                content, result = row[0], {"ref": ref, "kind": "tool_context"}
                if "c" + digest(content.encode())[:32] != ref:
                    raise MemoryError("工具缓存校验失败，不能当作原文使用。")
            else:
                raise MemoryError("未知的记忆引用。")
            result.update(content=content[offset:offset + limit], total_chars=len(content),
                          next_offset=offset + limit if offset + limit < len(content) else None)
            return result

    def decompress(self, block_id, *, offset=0, limit=8):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12:
            raise MemoryError("还原偏移须非负，每次最多12条。")
        self.sync()
        with self._lock, self._db() as db:
            block = db.execute("SELECT * FROM blocks WHERE ref=?", (block_id,)).fetchone()
            if block is None:
                raise MemoryError("此压缩块不存在或原文已变化。")
            lines = [row[0] for row in db.execute("SELECT line FROM messages WHERE line BETWEEN ? AND ? ORDER BY line", (block["start"], block["end"]))]
            rows, length = [], 0
            for line in lines[offset:offset + limit]:
                item = self._read_message(db, line)
                if rows and length + len(item["content"]) > 7000:
                    break
                item["total_chars"] = len(item["content"])
                item["content"] = item["content"][:5000]
                item["truncated"] = item["total_chars"] > 5000
                rows.append(item)
                length += len(item["content"])
            following = offset + len(rows)
            return {"block_id": block_id, "rows": rows, "next_offset": following if following < len(lines) else None,
                    "note": "长消息可用read_memory(ref,offset,limit)继续读；原始文件未改写。"}

    def stats(self):
        with self._lock, self._db() as db:
            count = db.execute("SELECT count(*) FROM messages").fetchone()[0]
            blocks = db.execute("SELECT count(*) FROM blocks").fetchone()[0]
            return {"messages": count, "blocks": blocks, "fts": self.fts,
                    "status": self._meta(db, "compression_status", "尚未生成压缩摘要"),
                    "last_request": self._meta(db, "last_request", {}), "index_bytes": self.path.stat().st_size}

    def archive_context(self, messages, turn_id):
        payload = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode()) > 2 * 1024 * 1024:
            raise MemoryError("单组工具上下文过大，无法安全压缩。")
        ref = "c" + digest(payload.encode())[:32]
        with self._lock, self._db() as db:
            # Reusing the same immutable payload must refresh its live-turn pin,
            # otherwise the cleanup below could evict the ref just returned.
            db.execute("INSERT OR REPLACE INTO contexts VALUES(?,?,?,?)", (ref, turn_id, payload, time.time()))
            db.execute("DELETE FROM contexts WHERE turn_id<>? AND ref NOT IN (SELECT ref FROM contexts ORDER BY created DESC LIMIT 128)", (turn_id,))
            size = db.execute("SELECT coalesce(sum(length(CAST(payload AS BLOB))),0) FROM contexts").fetchone()[0]
            while size > 32 * 1024 * 1024:
                oldest = db.execute("SELECT ref,length(CAST(payload AS BLOB)) FROM contexts WHERE turn_id<>? ORDER BY created LIMIT 1", (turn_id,)).fetchone()
                if oldest is None:
                    raise MemoryError("本轮工具缓存超过32 MiB，无法安全压缩。")
                db.execute("DELETE FROM contexts WHERE ref=?", (oldest[0],))
                size -= oldest[1]
        return ref

    @staticmethod
    def _fingerprint(db, start, end):
        parts = [f"{row['line']}:{row['digest']}" for row in db.execute("SELECT line,digest FROM messages WHERE line BETWEEN ? AND ? ORDER BY line", (start, end))]
        return digest("\n".join(parts).encode())

    def summary_candidate(self, *, recent_keep=16, threshold=3000):
        self.sync()
        with self._lock, self._db() as db:
            boundary = db.execute("SELECT line FROM messages ORDER BY line DESC LIMIT 1 OFFSET ?", (recent_keep - 1,)).fetchone()
            if boundary is None:
                return None
            user = db.execute("SELECT line FROM messages WHERE role='user' AND line<=? ORDER BY line DESC LIMIT 1", (boundary[0],)).fetchone()
            cutoff = user[0] if user else boundary[0]
            recent = db.execute("SELECT line,role FROM messages WHERE line<? ORDER BY line DESC LIMIT 24", (cutoff,)).fetchall()
            selected = []
            for row in recent:
                if db.execute("SELECT 1 FROM blocks WHERE start<=? AND end>=?", (row[0], row[0])).fetchone():
                    break
                selected.append(row)
            selected.reverse()
            while selected and selected[0]["role"] != "user":
                selected.pop(0)
            while selected and selected[-1]["role"] == "user":
                selected.pop()
            if len(selected) < 2:
                return None
            rows = [self._read_message(db, row["line"]) for row in selected]
            tokens = sum(len(row["content"].encode("utf-8")) / 3 for row in rows)
            if len(rows) < 16 and tokens < threshold:
                return None
            start, end = rows[0]["line"], rows[-1]["line"]
            return {"start": start, "end": end, "fingerprint": self._fingerprint(db, start, end), "rows": rows}

    def store_summary(self, candidate, summary):
        self.sync()
        with self._lock, self._db() as db:
            if self._fingerprint(db, candidate["start"], candidate["end"]) != candidate["fingerprint"]:
                return False
            ref = f"b{candidate['start']}-{candidate['end']}-{candidate['fingerprint'][:10]}"
            details = {"facts": summary["facts"], "open_threads": summary["open_threads"], "partial": summary.get("partial", False)}
            db.execute("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?,?,?,?,?)", (ref, candidate["start"], candidate["end"], candidate["fingerprint"],
                       summary["title"], summary["summary"], json.dumps(details, ensure_ascii=False), time.time()))
            self._delete_docs(db, "kind='summary' AND ref=?", (ref,))
            self._add_doc(db, kind="summary", ref=ref, line=candidate["end"], role="summary",
                          body=summary["title"] + "\n" + summary["summary"] + "\n" + json.dumps(details, ensure_ascii=False),
                          time=candidate["rows"][-1].get("time", ""))
            self._set_meta(db, "compression_status", "旧对话已生成带来源的摘要，原文保留")
            return True

    def summaries(self, *, limit=3, before_line=None):
        self.sync()
        with self._lock, self._db() as db:
            where, parameters = (" WHERE end<?", [before_line]) if before_line is not None else ("", [])
            rows = db.execute("SELECT * FROM blocks" + where + " ORDER BY end DESC LIMIT ?", (*parameters, limit)).fetchall()
            return [{"ref": row["ref"], "start_line": row["start"], "end_line": row["end"], "title": row["title"],
                     "summary": row["summary"], **json.loads(row["details"])} for row in reversed(rows)]

    def note(self, key, value):
        with self._lock, self._db() as db:
            self._set_meta(db, key, value)
