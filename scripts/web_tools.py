"""Small, bounded public web lookups for the pet's tool loop."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser


USER_AGENT = "Xigpet/1.0 (desktop chat pet; encyclopedia and news lookup)"
MAX_RESPONSE = 1_000_000


def _get_json(url: str, *, allow_list=False):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("搜索结果过大")
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict) and not (allow_list and isinstance(result, list)):
            raise ValueError("搜索结果格式不正确")
        return result
    except (urllib.error.URLError, TimeoutError, ValueError, UnicodeError) as exc:
        raise RuntimeError(f"联网查询失败：{exc}") from exc


def _get_rss(url: str) -> list[dict]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("新闻结果过大")
        root = ET.fromstring(raw)
        rows = []
        for item in root.findall(".//item")[:5]:
            title = _plain(item.findtext("title") or "", 250)
            url_value = item.findtext("link") or ""
            if title and url_value.startswith(("https://", "http://")):
                rows.append({
                    "title": title,
                    "source": _plain(item.findtext("source") or "", 100),
                    "published": item.findtext("pubDate") or "",
                    "url": url_value,
                })
        return rows
    except (urllib.error.URLError, TimeoutError, ValueError, ET.ParseError) as exc:
        raise RuntimeError(f"新闻查询失败：{exc}") from exc


def _plain(value: str, limit: int = 500) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(value).split())[:limit]


def _query(value: str, max_len: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ValueError(f"查询词须为 1 至 {max_len} 个字符")
    return value.strip()


class _ArticleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts = [], []
        self.length = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"img", "br", "hr", "input", "meta", "link", "source", "wbr", "area", "embed", "param"}:
            return
        attrs = dict(attrs)
        _, article, skip, paragraph = self.stack[-1] if self.stack else ("", False, False, False)
        article = article or "mw-parser-output" in (attrs.get("class") or "").split()
        skip = skip or tag in {"script", "style", "table", "nav", "aside", "sup"}
        paragraph = paragraph or tag == "p"
        self.stack.append((tag, article, skip, paragraph))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag == "p":
            self.parts.append(" ")

    def handle_data(self, data):
        if self.stack and self.stack[-1][1] and not self.stack[-1][2] and self.stack[-1][3] and self.length < 4000:
            self.parts.append(data[:4000 - self.length])
            self.length += len(self.parts[-1])


def _get_moegirl_excerpt(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("条目页面过大")
        parser = _ArticleText()
        parser.feed(raw.decode("utf-8", errors="replace"))
        return " ".join("".join(parser.parts).split())[:1200]
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"萌娘条目读取失败：{exc}") from exc


def search_knowledge(query: str) -> str:
    query = _query(query)
    args = urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": 5, "format": "json", "utf8": 1,
    })
    data = _get_json("https://zh.wikipedia.org/w/api.php?" + args)
    rows = []
    for item in data.get("query", {}).get("search", [])[:5]:
        title = item.get("title", "")
        if not isinstance(title, str) or not title:
            continue
        rows.append({
            "title": title,
            "snippet": _plain(item.get("snippet", "")),
            "url": "https://zh.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
        })
    return json.dumps(rows, ensure_ascii=False) if rows else "百科没有找到结果，可换关键词。"


def search_moegirl(query: str) -> str:
    query = _query(query)
    args = urllib.parse.urlencode({
        "action": "opensearch", "search": query, "limit": 5, "namespace": 0, "format": "json",
    })
    try:
        data = _get_json("https://zh.moegirl.org.cn/api.php?" + args, allow_list=True)
        if not isinstance(data, list) or len(data) < 4 or not all(isinstance(item, list) for item in data[1:4]):
            raise RuntimeError("萌娘百科暂未返回可用的公开搜索结果。")
        rows = []
        for index, title in enumerate(data[1][:5]):
            if not isinstance(title, str) or not title:
                continue
            snippet = data[2][index] if index < len(data[2]) and isinstance(data[2][index], str) else ""
            rows.append({"title": title, "snippet": _plain(snippet), "source": "萌娘百科", "read_status": "search_only",
                         "url": "https://zh.moegirl.org.cn/" + urllib.parse.quote(title.replace(" ", "_"), safe="")})
        if rows:
            try:
                excerpt = _get_moegirl_excerpt(rows[0]["url"])
                if excerpt:
                    rows[0].update({"excerpt": excerpt, "read_status": "article_excerpt"})
            except RuntimeError:
                pass
        return json.dumps(rows, ensure_ascii=False) if rows else "萌娘百科没有找到结果，可换关键词。"
    except RuntimeError as exc:
        search_url = "https://zh.moegirl.org.cn/index.php?" + urllib.parse.urlencode({"title": "Special:搜索", "search": query})
        return json.dumps({"error": "萌娘百科查询暂时不可用，可能受到站点访问限制。不能声称已读到条目。",
                           "detail": str(exc)[:250], "search_url": search_url}, ensure_ascii=False)


def search_news(query: str) -> str:
    query = _query(query)
    encoded = urllib.parse.quote(query)
    feeds = [
        f"https://news.google.com/rss/search?q={encoded}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
        f"https://www.bing.com/news/search?q={encoded}&format=rss",
    ]
    errors = []
    for feed in feeds:
        try:
            rows = _get_rss(feed)
            if rows:
                return json.dumps(rows, ensure_ascii=False)
        except RuntimeError as exc:
            errors.append(str(exc))
    return "没有找到相关新闻。" if not errors else "新闻搜索暂时不可用：" + "; ".join(errors)


def find_fun_fact() -> str:
    args = urllib.parse.urlencode({
        "action": "query", "generator": "random", "grnnamespace": 0,
        "grnlimit": 1, "prop": "extracts", "exintro": 1,
        "explaintext": 1, "exchars": 600, "format": "json",
    })
    data = _get_json("https://zh.wikipedia.org/w/api.php?" + args)
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        title = page.get("title", "")
        extract = _plain(page.get("extract", ""), 600)
        if title and extract:
            return json.dumps({
                "title": title, "fact": extract,
                "url": "https://zh.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
            }, ensure_ascii=False)
    return "这次没找到有内容的趣闻，请再试一次。"
