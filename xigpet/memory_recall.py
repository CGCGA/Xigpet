"""Bounded local evidence retrieval; never writes memories or synthesizes facts."""
from __future__ import annotations

import heapq
import json
import re
from dataclasses import dataclass


ALIASES = (
    ("生日", "生辰", "出生", "诞生", "birthday", "born"),
    ("喜欢", "最爱", "爱好", "偏好", "favorite", "favourite"),
    ("家乡", "老家", "故乡", "hometown"),
    ("约定", "答应", "承诺", "promise"),
    ("名字", "姓名", "昵称", "称呼", "name"),
)
STOP_WORDS = set("你是 是什 什么 多少 几月 哪天 怎么 知道 记得 我的 你的 我说 告诉 想要 可以 是不 能不 有没有 是多 的是 之前 以前 时候 还记 得我 得你 请问 一下 呢呀 我们 你们 那个 这个 吗呢".split())
RECALL_CUES = ("以前", "之前", "上次", "那次", "说过", "记得", "忘了", "忘记", "约定", "生日", "生辰", "出生", "喜欢", "最爱", "家乡", "老家", "名字", "birthday", "remember", "favorite")
UNKNOWN_CUES = ("不记得", "记不清", "忘记了", "忘了", "不知道", "不清楚", "没印象", "没有印象", "没告诉过", "没有告诉", "没有记录", "没记录过", "don't remember", "do not remember", "don't know")


def recall_requested(query):
    return any(word in query.casefold() for word in RECALL_CUES)


def uncertain_memory_answer(query, answer):
    return recall_requested(query) and any(word in answer.casefold() for word in UNKNOWN_CUES)


def search_terms(query):
    query = query.casefold()[:500]
    terms = {}
    for group in ALIASES:
        if any(word in query for word in group):
            terms.update((word, 6) for word in group)
    for word in re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]+", query):
        parts = (word,) if word.isascii() else (word[i:i + 2] for i in range(len(word) - 1))
        for part in parts:
            if part not in STOP_WORDS:
                terms[part] = max(terms.get(part, 0), 1)
    return terms


def _score(text, terms):
    folded = text.casefold()
    return sum(weight for term, weight in terms.items() if term in folded)


def important_excerpt(path, query="", limit=6000):
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "", False
    if not text:
        return "", False
    terms = search_terms(query)
    threshold = 6 if any(weight == 6 for weight in terms.values()) else 1
    if len(text) <= limit:
        return text, bool(terms and _score(text, terms) >= threshold)
    if not terms:
        half = (limit - 50) // 2
        return "[重要记忆较长，以下为开头与末尾，具体事实可按主题检索]\n" + text[:half] + "\n……\n" + text[-half:], False
    # Split long paragraphs as well as Markdown bullets: older facts are not lost behind a tail cutoff.
    fragments, heading = [], ""
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            heading = line
        for start in range(0, len(line), 650):
            fragment = line[start:start + 700]
            fragments.append(f"{heading}\n{fragment}" if heading and line != heading else fragment)
    ranked = sorted(((_score(fragment, terms), index, fragment) for index, fragment in enumerate(fragments)),
                    key=lambda item: (-item[0], item[1]))
    selected, length = [], 0
    for score, _, fragment in ranked:
        if score < threshold or length + len(fragment) + 1 > limit - 800:
            continue
        selected.append(fragment)
        length += len(fragment) + 1
    evidence = bool(selected)
    pieces = ["[按当前话题选取的重要记忆原文片段]", *selected,
              "[文件开头与末尾摘要，其他内容可用 search_important_memory 检索]", text[:350], text[-350:]]
    return "\n".join(dict.fromkeys(pieces))[:limit], evidence


def history_excerpt(catalog, query, *, before_line=None, limit=5000):
    terms = search_terms(query)
    if not terms:
        return []
    threshold = 6 if any(weight == 6 for weight in terms.values()) else 1
    with catalog._lock:
        def candidates():
            for entry in catalog.entries():
                if before_line is not None and entry["line"] >= before_line:
                    continue
                score = _score(f"{entry.get('topic', '')} {entry.get('keywords', '')}", terms)
                if score >= threshold:
                    yield score, entry["line"], entry
        best = heapq.nlargest(6, candidates(), key=lambda item: (item[0], item[1]))
        rows, length = [], 0
        with catalog.history.open("rb") as stream:
            for _, _, entry in best:
                stream.seek(entry["offset"])
                try:
                    row = json.loads(stream.read(entry["bytes"]))
                except (ValueError, UnicodeDecodeError):
                    continue
                content = row.get("content", "")
                if not isinstance(content, str):
                    continue
                # Catalog digests cover only previews. Keep full matching excerpts from their actual source.
                size = min(1300, max(0, limit - length))
                positions = [content.casefold().find(term) for term in terms if term in content.casefold()]
                start = max(0, min(positions) - 250) if positions and len(content) > size else 0
                clipped = content[start:start + size]
                if not clipped:
                    break
                rows.append({"line": entry["line"], "time": row.get("time", ""),
                             "role": row.get("role"), "content": clipped})
                length += len(clipped)
        return rows


@dataclass
class RecallContext:
    important: str
    history: list
    relevant: bool

    def content(self):
        return json.dumps({"important.md原文": self.important, "历史原文片段": self.history}, ensure_ascii=False)


def prepare_recall(important, catalog, query, *, before_line=None, force_history=False):
    excerpt, relevant = important_excerpt(important, query)
    rows = history_excerpt(catalog, query, before_line=before_line) if force_history or (recall_requested(query) and not relevant) else []
    return RecallContext(excerpt, rows, relevant or bool(rows))
