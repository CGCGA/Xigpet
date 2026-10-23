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
    """Distribute the complete response instead of throwing away its tail."""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", content) if part.strip()]
    compact = "".join((" " if index and part[:1].isascii() else "") + part for index, part in enumerate(paragraphs))
    # Short answers should not become three bubbles just because the model inserted blank lines.
    if (len(paragraphs) > 1 and len(compact) <= 160 and "```" not in content
            and "http" not in content and "[[" not in content and not limit_sentences(compact)[1]):
        return [compact]
    maximum = 4
    result = []
    for paragraph in paragraphs:
        remaining = paragraph
        while remaining:
            head, remaining = limit_sentences(remaining, maximum)
            if head:
                result.append(head)
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
