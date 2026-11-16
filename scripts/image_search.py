"""Public image-search pages and user-supplied pages -> verified file download.

HTML/JSON are untrusted data. No search-page scripts are evaluated here.
Dynamic pages may use the existing isolated Edge actor and its fixed actions.
"""
from __future__ import annotations
import html
from html.parser import HTMLParser
import json
from itertools import islice
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit, quote

from .public_http import ResourceError, resource_failure
from .media_tools import media_inventory, media_path

ALIASES = {"葬送的芙莉莲": "Frieren", "芙莉莲": "Frieren", "原神": "Genshin Impact", "表情包": "meme", "开心": "happy"}
ENGINES = {
    "baidu": "https://image.baidu.com/search/index?tn=baiduimage&",
    "bing": "https://www.bing.com/images/search?adlt=strict&",
    "google": "https://www.google.com/search?udm=2&",
    "tieba": "https://tieba.baidu.com/f/search/res?ie=utf-8&",
}
MOODS = ("开心", "难过", "生气", "害羞", "微笑", "可爱", "惊讶", "哭泣")


class ImageSourceError(ResourceError):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


def words(query):
    text = re.sub(r"表情包?|二次元|动漫|下载|图片|照片|一张|帮我", " ", query.casefold())
    text = re.sub("(" + "|".join(MOODS) + ")", r" \1 ", text)
    return [s for s in re.split(r"[\s，,、。]+", text) if s]


def translated(query):
    for before, after in ALIASES.items():
        query = query.replace(before, after)
    return query


def matches(query, label):
    parts = words(query)
    return bool(parts) and all(s in label.casefold() for s in parts)


def blocked(text):
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    title = html.unescape(title.group(1)) if title else ""
    return bool(re.search(r"安全验证|验证码|验证中心|unusual traffic|access denied|just a moment|robot check", title, re.I)
                or re.search(r'"antiFlag"\s*:\s*(?:1|true)', text))


class ImagePage(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.rows, self.posts = base, [], []

    def add(self, url, label="", preview=False, search_result=False):
        if not isinstance(url, str) or not url or len(url) > 8192 or len(self.rows) >= 160:
            return
        url = urljoin(self.base, html.unescape(url))
        try:
            parts = urlsplit(url)
        except ValueError:
            return
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return
        if re.search(r"favicon|spacer|loading\.(?:gif|png)|/logo[/.]", parts.path, re.I):
            return
        for row in self.rows:
            if row["url"] == url:
                if search_result:
                    row.update(search_result=True, label=str(label)[:240])
                return
        self.rows.append({"url": url, "label": str(label)[:240], "preview": preview,
                          "search_result": search_result, "source_page": self.base})

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        label = a.get("alt") or a.get("title") or ""
        if a.get("m"):
            try:
                data = json.loads(a["m"])
                self.add(data.get("murl"), data.get("t", label), search_result=True)
                self.add(data.get("turl"), data.get("t", label), True, True)
                if isinstance(data.get("purl"), str):
                    for row in self.rows:
                        if row["url"] == data.get("murl"):
                            row["source_page"] = urljoin(self.base, data["purl"])
            except (ValueError, AttributeError):
                pass
        if tag in ("img", "source"):
            if any(str(a.get(k, "")).isdigit() and int(a[k]) < 32 for k in ("width", "height")):
                return
            for key in ("data-original", "data-original-tb-img", "data-src", "data-url", "data-lazy-src"):
                self.add(a.get(key), label)
            for key in ("data-srcset", "srcset"):
                choices = []
                srcset = a.get(key) or ""
                for item in ([] if "data:" in srcset else srcset.split(",")[:8]):
                    parts = item.strip().split()
                    if parts:
                        size = re.search(r"[\d.]+", parts[1]) if len(parts) > 1 else None
                        choices.append((float(size[0]) if size else 0, parts[0]))
                for _, candidate in sorted(choices, reverse=True):
                    self.add(candidate, label)
            self.add(a.get("src"), label, True)
        elif tag == "meta" and a.get("property") == "og:image":
            self.add(a.get("content"), label)
        elif tag == "a" and a.get("href"):
            target = urljoin(self.base, a["href"])
            try:
                parts = urlsplit(target)
                params = parse_qs(parts.query)
            except ValueError:
                return
            for key in ("imgurl", "mediaurl", "objurl"):
                for value in params.get(key, [])[:1]:
                    self.add(value, label)
            if re.search(r"\.(?:png|jpe?g|gif|webp)$", parts.path, re.I):
                self.add(target, label)
            if parts.hostname == "tieba.baidu.com" and re.fullmatch(r"/p/\d+", parts.path) and target not in self.posts and len(self.posts) < 20:
                self.posts.append(target)


def parse_images(text, url):
    page = ImagePage(url)
    page.feed(text)
    # Search data embedded in a page, including Baidu's original/preview URLs.
    pattern = r'"(?:objURL|middleURL|thumbURL|murl|ou)"\s*:\s*("(?:\\.|[^"\\])*")'
    for m in islice(re.finditer(pattern, text), 100):
        try:
            page.add(json.loads(m.group(1)))
        except ValueError:
            pass
    # Google image payloads contain URL/width/height tuples. Decode JSON only.
    for m in islice(re.finditer(r'\[("https?(?:\\.|[^"\\]){1,8192}"),\s*(\d+),\s*(\d+)\]', text), 80):
        if min(int(m[2]), int(m[3])) >= 80:
            try:
                page.add(json.loads(m[1]))
            except ValueError:
                pass
    return page


def read_page(task, url):
    task.start_network()
    old = task.http.deadline
    task.http.deadline = min(task.deadline, time.monotonic() + 12)
    try:
        raw, headers, final = task.http.read(url, 4 * 1024 * 1024)
        if any(k.lower() == "content-type" and str(v).lower().startswith("image/") for k,v in headers.items()):
            page = ImagePage(final)
            page.add(final)
            return page
        text = raw.decode("utf-8", errors="replace")
        if blocked(text):
            raise ImageSourceError("challenge", "网站需要安全验证或拒绝自动访问，未继续请求。")
        return parse_images(text, final)
    except ResourceError as exc:
        if "超时" in str(exc) and not task.cancel.is_set() and time.monotonic() < task.deadline:
            raise TimeoutError("图片来源读取超时") from exc
        raise
    finally:
        task.http.deadline = old


def select_images(rows, keyword, source):
    if source not in ("bing", "google", "baidu"):
        return rows
    keys = words(keyword)
    anchors = [key for key in keys if key not in MOODS]
    ranked = []
    for row in rows:
        label = row.get("label", "").casefold()
        score = sum(key in label for key in keys)
        if label and anchors and not any(key in label for key in anchors):
            continue
        if not label and not row.get("search_result") and re.search(r"logo|icon|avatar|favicon", row["url"], re.I):
            continue
        ranked.append((score + bool(row.get("search_result")), row))
    return [row for _,row in sorted(ranked, key=lambda pair: pair[0], reverse=True)]


def browser_images(task, url, keyword="", source="page"):
    task.run("browser_open", {"images": True})
    try:
        task.run("browser_navigate", {"url": url})
        rows = {}
        for index in range(3):
            task.check()
            data = task.run("browser_images", {"wait_ms": 400 + index * 250, "scroll": index > 0})
            if blocked("<title>" + html.escape(data["title"]) + "</title>"):
                raise ImageSourceError("challenge", "网站需要验证，未继续操作。")
            for row in data["images"]:
                # Several inline/blob previews have no public download URL.
                # Keep each candidate so relevance filtering can choose its image.
                key = row["url"] or row.get("capture_element_id")
                if key:
                    rows.setdefault(key, row)
            if select_images(list(rows.values()), keyword, source):
                break
        return list(rows.values())
    except (OSError, ValueError) as exc:
        reason, _ = resource_failure(exc)
        if reason in ("cancelled", "task_timeout", "challenge", "address_rejected"):
            raise
        raise ImageSourceError("browser_failed", "浏览器未能读取此页面，页面加载失败或受到站点限制。") from exc
    finally:
        task.close_browser()


def capture_fallback(task, url, keyword, source, preferred_urls=()):
    """Capture only a relevant, actually rendered img; never a whole page."""
    task.run("browser_open", {"images":True})
    try:
        task.run("browser_navigate", {"url":url})
        for index in range(3):
            task.check()
            data=task.run("browser_images", {"wait_ms":500+index*250,"scroll":index>0})
            if blocked("<title>"+html.escape(data["title"])+"</title>"):
                raise ImageSourceError("challenge","验证页面不能作为表情截图。")
            rows=select_images(data["images"],keyword,source)
            rows=sorted((r for r in rows if r.get("capture_element_id")),key=lambda r:r["url"] not in preferred_urls)
            if rows:
                return task.run("browser_capture_image",{"element_id":rows[0]["capture_element_id"],"label":keyword[:60]})
        raise ImageSourceError("no_candidates","页面中没有相关且已加载的图片可供截图。")
    finally:
        task.close_browser()


def diagnostic(task, source, stage, parsed=0, selected=0, reason=None):
    row = {"source": source, "stage": stage, "parsed": parsed, "selected": selected, "filtered": max(0, parsed-selected)}
    if reason:
        row["reason"] = reason
    task.image_diagnostics.append(row)
    task._log("image_stage", "download_image", **row)


def download_headers(task, row):
    # Referer comes only from the page actually read. Never forward userinfo or fragments.
    headers = {}
    parts = urlsplit(row.get("source_page", ""))
    if parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password:
        target = urlsplit(row["url"])
        same_origin = (parts.scheme, parts.netloc.lower()) == (target.scheme, target.netloc.lower())
        headers["Referer"] = urlunsplit((parts.scheme, parts.netloc,
            quote(parts.path, safe="/%:@!$&'()*+,;=-._~") if same_origin else "/",
            quote(parts.query, safe="=&%/:?@!$'()*+,;-._~") if same_origin else "", ""))
    if getattr(task, "_browser_ua", ""):
        headers["User-Agent"] = task._browser_ua
    return headers or None


def download_candidate(task, row):
    # A slow original must leave time for previews and a rendered-image fallback.
    old = task.http.deadline
    candidate_deadline = min(old, task.deadline, time.monotonic() + 12)
    task.http.deadline = candidate_deadline
    try:
        return task.store.download(task.http, row["url"], "image", task.progress,
                                   headers=download_headers(task, row))
    except ResourceError as exc:
        if ("超时" in str(exc) and not task.cancel.is_set()
                and candidate_deadline < task.deadline):
            raise TimeoutError("图片候选读取超时") from exc
        raise
    finally:
        task.http.deadline = old


def settings(task):
    path = task.store.root.parent.parent / "config/images.json"
    value = {"enabled": True}
    if path.exists():
        if path.is_symlink() or path.stat().st_size > 8192:
            raise ResourceError("图片下载配置无效。")
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict) or type(raw.get("enabled", True)) is not bool:
                raise ValueError()
            value.update(enabled=raw.get("enabled", True))
        except ValueError as exc:
            raise ResourceError("请检查config/images.json中的enabled设置。") from exc
    return value


def download_image(task, query, engine="auto", page_url=None, fresh=False, fallback=True):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 80 or any(ord(c) < 32 for c in query):
        raise ResourceError("图片关键词须为1～80字，不含私人聊天。")
    if engine not in ("auto", *ENGINES) or type(fresh) is not bool or type(fallback) is not bool:
        raise ResourceError("图片来源或参数无效。")
    if page_url is not None and (not isinstance(page_url, str) or not 1 <= len(page_url) <= 8192):
        raise ResourceError("网页地址无效。")
    if getattr(task, "image_attempted", False):
        raise ResourceError("本轮已执行图片获取，请根据结果回复，不重复查找。")
    task.image_attempted = True
    task.image_diagnostics = []
    query = query.strip()
    if not fresh and page_url is None:
        folder = task.store.root.parent.parent / "media"
        for row in media_inventory(folder):
            label = " ".join(str(row.get(k, "")) for k in ("title", "mood", "keywords"))
            if row["kind"] == "media" and matches(query, label):
                media_path(folder, row["file"])
                return {"sent": row["file"], "source": "local_library", "status": "ready"}
        for meta in sorted(task.store.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:1000]:
            try:
                data, _ = task.store.get(meta.stem)
                if data["kind"] == "image" and matches(query, data["name"]):
                    result = task.run("send_download", {"file_id": data["id"]})
                    result["source"] = "image_cache"
                    return result
            except (OSError, ValueError, KeyError):
                continue
    if not settings(task)["enabled"]:
        raise ResourceError("本地没有匹配图片，联网图片获取已关闭。")
    if task.autonomous and (task.files or task.auto_downloads):
        raise ResourceError("本次自主聊天最多获取一张图片。")
    task.start_network()
    plan = [(engine, query)] if engine != "auto" else [(name, query) for name in ("bing", "baidu", "google")]
    if fallback and engine != "auto":
        plan.extend((name, query) for name in ("bing", "baidu", "google") if name != engine)
    if translated(query) != query and engine in ("auto", "bing"):
        plan.insert(1, ("bing", translated(query)))
    if page_url is not None:
        plan = [("page", page_url)]
    attempts, errors, seen = 0, [], set()
    browser_attempts = 0
    max_attempts = 3 if task.autonomous else 6
    for source, keyword in plan:
        task.check()
        url = keyword if source == "page" else ENGINES[source] + urlencode({"word" if source == "baidu" else "qw" if source == "tieba" else "q": keyword})
        render_url = url
        source_had_candidates=False
        source_blocked=False
        failed_urls=[]
        source_attempts=0
        source_limit=(1 if task.autonomous else 2) if page_url is None and fallback else (2 if task.autonomous else 4)
        for stage in ("static", "rendered"):
            task.check()
            if stage == "rendered":
                if browser_attempts >= 2 or task.deadline - time.monotonic() < 15:
                    break
                browser_attempts += 1
            try:
                if stage == "rendered":
                    rows = browser_images(task, render_url, keyword, source)
                else:
                    if source == "page" and re.search(r"\.(?:png|jpe?g|gif|webp)$", urlsplit(url).path, re.I):
                        page = ImagePage(url)
                        page.add(url, query)
                    else:
                        page = read_page(task, url)
                    rows = page.rows
                    if source == "tieba":
                        rows = []
                        for post in page.posts[:2]:
                            rows.extend(read_page(task, post).rows)
                        if page.posts:
                            render_url = page.posts[0]
                parsed = len(rows)
                rows = select_images(rows, keyword, source)
                source_had_candidates = source_had_candidates or bool(rows)
                reason = "no_candidates" if not parsed else "filtered_out" if not rows else None
                diagnostic(task, source, stage, parsed, len(rows), reason)
                if not rows:
                    errors.append(f"{source}（{'浏览器读取' if stage == 'rendered' else '网页解析'}）：解析{parsed}张，相关性筛选后0张")
                    continue
                for row in rows:
                    if not row["url"]:
                        continue
                    if row["url"] in seen:
                        continue
                    seen.add(row["url"])
                    if attempts >= max_attempts or source_attempts >= source_limit:
                        break
                    attempts += 1
                    source_attempts += 1
                    if task.autonomous:
                        task.auto_downloads += 1
                    try:
                        result = download_candidate(task, row)
                        data, _ = task.store.get(result["file_id"])
                        data["name"] = re.sub(r"[^\w\u4e00-\u9fff .-]", "_", query)[:60]
                        task.store._write(data)
                        task.files.append(result["file_id"])
                        task._log("downloaded", "download_image", source=source, stage="download", attempts=attempts)
                        result.update(source=source, status="ready", resolution="preview" if row["preview"] else "original",
                                      diagnostics=task.image_diagnostics)
                        return result
                    except (OSError, ValueError) as exc:
                        reason, message = resource_failure(exc)
                        task._log("candidate_failed", "download_image", reason, source=source, stage="download", attempts=attempts)
                        errors.append(source + "：图片下载失败：" + message)
                        failed_urls.append(row["url"])
                        if task.cancel.is_set() or reason in ("cancelled", "task_timeout"):
                            raise
                if attempts >= max_attempts or source_attempts >= source_limit:
                    break
            except (OSError, ValueError) as exc:
                reason, message = resource_failure(exc)
                diagnostic(task, source, stage, reason=reason)
                if task.cancel.is_set() or reason in ("cancelled", "task_timeout") or (source == "page" and reason == "address_rejected"):
                    raise
                errors.append(source + "：" + message)
                # Verification and explicit access denials are not render failures.
                if reason in ("challenge", "http_status", "address_rejected"):
                    source_blocked=True
                    break
        if source_had_candidates and not source_blocked and not task.capture_attempts and task.deadline-time.monotonic()>=12:
            try:
                result=capture_fallback(task,render_url,keyword,source,failed_urls)
                diagnostic(task,source,"capture",1,1)
                result.update(source=source,status="ready",diagnostics=task.image_diagnostics)
                return result
            except (OSError,ValueError) as exc:
                reason,message=resource_failure(exc)
                diagnostic(task,source,"capture",reason=reason)
                if task.cancel.is_set() or reason in ("cancelled","task_timeout"):
                    raise
                errors.append(source+"：图片区域截图未成功："+message)
        if attempts >= max_attempts:
            break
    categories = {row.get("reason") for row in task.image_diagnostics}
    category = "download_failed" if attempts else "filtered_out" if "filtered_out" in categories else "browser_failed" if "browser_failed" in categories else "challenge" if categories == {"challenge"} else "no_candidates"
    raise ImageSourceError(category, "没有取得可下载图片。" + "；".join(dict.fromkeys(errors))[:600])
