"""Small mixed Chinese/English lexical features; no downloaded models."""

from collections import Counter
import re


ALIASES = (
    ("生日", "生辰", "出生", "诞生", "birthday", "born"),
    ("喜欢", "最爱", "爱好", "偏好", "爱喝", "favorite", "favourite"),
    ("家乡", "老家", "故乡", "hometown"),
    ("约定", "答应", "承诺", "promise"),
    ("名字", "姓名", "昵称", "称呼", "name"),
)
STOP = set("你是 是什 什么 多少 几月 哪天 怎么 知道 记得 我的 你的 我说 告诉 想要 可以 是不 能不 有没有 是多 的是 之前 以前 时候 还记 得我 得你 请问 一下 呢呀 我们 你们 那个 这个 吗呢 说过 上次 一些 一个 你好 哈哈 谢谢 好的 然后 现在 今天 the and that this with have were your you are was for from what when".split())


def _stem(word):
    for ending in ("ing", "ed", "es", "s"):
        if len(word) > len(ending) + 4 and word.endswith(ending):
            return word[:-len(ending)]
    return word


def features(text):
    words, grams = [], set()
    for word in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.casefold()):
        if word.isascii():
            if len(word) >= 2 and word not in STOP:
                words.append("w" + _stem(word))
            if len(word) >= 4 and word not in STOP:
                grams.update("g" + word[i:i + 3] for i in range(len(word) - 2))
        else:
            pieces = [word] if len(word) == 1 else [word[i:i + 2] for i in range(len(word) - 1)]
            words.extend(("u" if len(piece) == 1 else "c") + piece for piece in pieces if piece not in STOP)
            grams.update("c" + piece for piece in pieces if len(piece) >= 2 and piece not in STOP)
    return Counter(words), grams


def search_features(query):
    query = query.casefold().strip()[:500]
    terms, grams = features(query)
    for group in ALIASES:
        if any(word in query for word in group):
            for alias in group:
                expanded, _ = features(alias)
                terms.update(expanded)
    # Prefer aliases and query words; admit typo-tolerant Latin trigrams too.
    tokens = [term for term, _ in terms.most_common(28)]
    tokens += [term for term in sorted(grams) if term not in tokens][:12]
    return tokens[:40], grams


def index_tokens(text):
    words, grams = features(text)
    # Single-character names/topics are searchable without adding noisy
    # single-character terms to every multi-character query.
    singles = {"u" + char for char in re.findall(r"[\u3400-\u9fff]", text)}
    return " ".join([word for word, count in words.items() for _ in range(min(8, count))] + sorted(grams | singles))


def snippet(text, query, limit=650):
    _, grams = search_features(query)
    positions = [text.casefold().find(query.casefold())] if query else []
    for gram in grams:
        positions.append(text.casefold().find(gram[1:]))
    positions = [position for position in positions if position >= 0]
    start = max(0, min(positions) - limit // 4) if positions else 0
    if start + limit > len(text):
        start = max(0, len(text) - limit)
    return text[start:start + limit], start
