"""Keep pet messages conversational while preserving requested code blocks."""

from __future__ import annotations

import re
from html import unescape


SENTENCE_TOKEN = re.compile(
    r"```[\s\S]*?```|https?://[^\s<>。！？“”「」]+|"
    r"[。！？!?]+[\"'”’」』]*|…+[\"'”’」』]*|\.[\"'”’」』]*(?=\s|$|[\u4e00-\u9fff])|\n+"
)


def limit_sentences(content: str, maximum: int = 4) -> tuple[str, str]:
    """Keep complete sentences; return the unsent draft for the next decision."""
    boundaries = []
    start = 0
    for match in SENTENCE_TOKEN.finditer(content):
        if match.group().startswith(("```", "http://", "https://")):
            continue
        if content[start:match.end()].strip():
            boundaries.append(match.end())
        start = match.end()
    if content[start:].strip():
        boundaries.append(len(content))
    if len(boundaries) <= maximum:
        return content.strip(), ""
    cut = boundaries[maximum - 1]
    return content[:cut].strip(), content[cut:].strip()


def split_bubbles(content: str) -> list[str]:
    """One conversational sentence per bubble; preserve code, links and quotes.

    A model's newline is also an intentional send boundary. All text is retained,
    including tails beyond the continuation budget; no model call per sentence.
    """
    protected = re.compile(
        r"```[\s\S]*?(?:```|$)|`[^`\n]+`|https?://[^\s<>。！？…，；“”‘’「」『』]+|"
        r"(?m:^[ \t]*\|[^\n]*(?:\n[ \t]*\|[^\n]*)+)|"
        r"(?m:^[ \t]*(?:[-*+]|\d+[.)]) [^\n]*(?:\n[ \t]*(?:[-*+]|\d+[.)]) [^\n]*)+)"
    )
    result, buffer, quotes = [], [], []
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』"}

    def flush():
        text = "".join(buffer).strip()
        if text:
            result.append(text)
        buffer.clear()

    def plain(text):
        index = 0
        while index < len(text):
            char = text[index]
            if char in pairs:
                quotes.append(pairs[char])
            elif quotes and char == quotes[-1]:
                quotes.pop()
            if char in "\r\n" and not quotes:
                flush()
                index += 1
                continue
            buffer.append(char)
            ending = char in "。！？!?；;…"
            if char == ".":
                following = text[index + 1:index + 2]
                ending = not following or following.isspace() or "\u4e00" <= following <= "\u9fff"
                # Keep common English abbreviations and version/decimal tokens.
                prefix = "".join(buffer)
                if re.search(r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|e\.g|i\.e)\.$|\b(?:[A-Za-z]\.){2,}$", prefix, re.I):
                    ending = False
            if ending and not quotes:
                # Repeated punctuation/ellipsis and closing quotation marks stay
                # attached, rather than becoming punctuation-only messages.
                while index + 1 < len(text) and text[index + 1] in "。！？!?；;…\"'”’」』":
                    index += 1
                    buffer.append(text[index])
                flush()
            index += 1

    position = 0
    for match in protected.finditer(content):
        plain(content[position:match.start()])
        token = match.group()
        if token.startswith("```") or "\n" in token and not token.startswith(("http://", "https://")):
            flush()
            result.append(token.strip())
        elif token.startswith(("http://", "https://")):
            # English sentence punctuation following a URL is not part of it.
            link = token.rstrip(".,!?;")
            buffer.append(link)
            plain(token[len(link):])
        else:
            buffer.append(token)
        position = match.end()
    plain(content[position:])
    flush()
    return result


def naturalize_text(content: str) -> str:
    parts = re.split(r"(```[\s\S]*?```)", content)
    for index in range(0, len(parts), 2):
        text = parts[index]
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</?(?:b|strong|i|em|p|div|span|h[1-6])\b[^>]*>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", r"\1：\2", text)
        text = re.sub(r"(?<!\*)\*\*(\S(?:.*?\S)?)\*\*(?!\*)", r"\1", text)
        text = re.sub(r"(?<!_)__(\S(?:.*?\S)?)__(?!_)", r"\1", text)
        text = re.sub(r"(?<!\*)\*(\S(?:[^*\n]*?\S)?)\*(?!\*)", r"\1", text)
        text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
        parts[index] = unescape(text)
    return "".join(parts).strip()
