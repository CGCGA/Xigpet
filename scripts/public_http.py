"""Public-only HTTP with DNS pinning, TLS hostname checks and checked redirects."""
from __future__ import annotations
import http.client
import http.cookiejar
from email.message import Message
import ipaddress
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit, urljoin, urlunsplit, quote
from urllib.request import Request


class ResourceError(ValueError):
    pass


def request_referer(value, target):
    """Match browser strict-origin-when-cross-origin, including redirects."""
    try:
        source, dest = urlsplit(value), urlsplit(target)
        if source.scheme not in ("http", "https") or not source.hostname or source.username or source.password:
            return None
        if source.scheme == "https" and dest.scheme == "http":
            return None
        host = source.hostname.encode("idna").decode("ascii")
        netloc = "[" + host + "]" if ":" in host else host
        if source.port:
            netloc += ":" + str(source.port)
        same = (source.scheme, host, source.port or (443 if source.scheme == "https" else 80)) == (
            dest.scheme, (dest.hostname or "").encode("idna").decode("ascii"), dest.port or (443 if dest.scheme == "https" else 80))
        return urlunsplit((source.scheme, netloc, quote(source.path or "/", safe="/%:@!$&'()*+,;=-._~") if same else "/",
                          quote(source.query, safe="=&%/:?@!$'()*+,;-._~") if same else "", ""))
    except (ValueError, UnicodeError):
        return None


def resource_failure(exc):
    """Classify failures without leaking URLs, headers or response bodies."""
    if getattr(exc, "category", None) in {"challenge", "no_candidates", "filtered_out", "browser_failed", "download_failed"}:
        return exc.category, str(exc)
    if isinstance(exc, ssl.SSLError):
        return "tls", "网站的HTTPS握手或证书校验失败。"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "network_timeout", "连接或读取网站超时。"
    if isinstance(exc, ResourceError):
        message = str(exc)
        if "连接或读取网站超时" in message:
            return "network_timeout", message
        if "HTTPS" in message:
            return "tls", message
        if "取消" in message:
            return "cancelled", message
        if "超时" in message:
            return "task_timeout", message
        if "HTTP " in message:
            return "http_status", message
        if "公网" in message or "内网" in message or "解析" in message:
            return "address_rejected", message
        return "resource_rejected", message
    if isinstance(exc, OSError):
        return "io_error", "网络连接或本地文件读写失败。"
    return "invalid_response", "资源来源返回格式异常或文件内容校验失败。"


def public_target(url):
    if not isinstance(url, str) or not 1 <= len(url) <= 8192 or any(ord(c) < 33 or c == "\\" for c in url):
        raise ResourceError("地址格式无效。")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").encode("idna").decode("ascii").rstrip(".")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if (parts.scheme not in ("http", "https") or not host or parts.username is not None
                or parts.password is not None or port not in (80, 443) or "%" in host):
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise ResourceError("只允许不含账号密码的公网HTTP(S)地址和80/443端口。") from exc
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ResourceError("不能访问本机或内网。")
    try:
        addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except OSError as exc:
        raise ResourceError("无法解析网页地址。") from exc
    if not addresses:
        raise ResourceError("网页地址没有可用公网IP。")
    for value in addresses:
        address = ipaddress.ip_address(value)
        mapped = getattr(address, "ipv4_mapped", None)
        if not address.is_global or mapped is not None and not mapped.is_global:
            raise ResourceError("不能访问内网、回环、保留或云元数据地址。")
    return parts, host, port, addresses


class PublicHTTP:
    def __init__(self, cancel, deadline):
        self.cancel, self.deadline = cancel, deadline
        self.connections, self.lock = set(), threading.RLock()
        self.cookies = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(
            strict_ns_domain=http.cookiejar.DefaultCookiePolicy.DomainStrictNonDomain))

    def import_browser_cookies(self, rows):
        """Only called with cookies from our ephemeral context, never user profiles."""
        for row in rows[:200]:
            domain, name, value = row.get("domain", ""), row.get("name", ""), row.get("value", "")
            if not domain or not name or any(c in name + value for c in "\r\n\0"):
                continue
            expires = row.get("expires", -1)
            self.cookies.set_cookie(http.cookiejar.Cookie(0, name, value, None, False, domain,
                domain.startswith("."), domain.startswith("."), row.get("path", "/"), True,
                bool(row.get("secure")), int(expires) if expires > 0 else None, expires <= 0,
                None, None, {"HttpOnly": None} if row.get("httpOnly") else {}, False))

    def browser_cookies(self):
        self.cookies.clear_expired_cookies()
        return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/",
                 "secure": c.secure, "httpOnly": c.has_nonstandard_attr("HttpOnly"),
                 **({"expires": c.expires} if c.expires is not None else {})}
                for c in list(self.cookies)[:200]]

    def check(self):
        if self.cancel.is_set():
            raise ResourceError("任务已取消。")
        if time.monotonic() >= self.deadline:
            raise ResourceError("任务已超时。")

    def close(self):
        with self.lock:
            for connection in list(self.connections):
                active_socket = getattr(connection, "_resource_socket", None) or connection.sock
                if active_socket:
                    try:
                        active_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()
            self.connections.clear()
            self.cookies.clear()

    def open(self, url, *, headers=None):
        # Header names are a local allowlist; tools cannot supply arbitrary ones.
        extra = {}
        allowed = {name.lower():name for name in ("User-Agent", "Accept", "Accept-Language", "Referer", "Origin")}
        for key, value in (headers or {}).items():
            if key.lower() not in allowed:
                continue
            if not isinstance(value, str) or len(value) > 8192 or any(c in value for c in "\r\n\0"):
                raise ResourceError("网页请求头无效。")
            extra[allowed[key.lower()]] = value
        first_scheme = urlsplit(url).scheme
        for _ in range(6):
            self.check()
            parts, host, port, addresses = public_target(url)
            connection = http.client.HTTPConnection(host, port, timeout=min(8, max(.1, self.deadline - time.monotonic())))
            with self.lock:
                self.connections.add(connection)
            try:
                # Connect only to the validated numeric address; never resolve again.
                last_error = None
                for address in addresses:
                    self.check()
                    try:
                        connection.sock = socket.create_connection((address, port), timeout=min(3, connection.timeout))
                        break
                    except OSError as exc:
                        last_error = exc
                if connection.sock is None:
                    raise last_error or ResourceError("公网连接失败。")
                connection.sock.settimeout(connection.timeout)
                if parts.scheme == "https":
                    connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=host)
                connection._resource_socket = connection.sock
                self.check()
                path = urlunsplit(("", "", quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"), quote(parts.query, safe="=&%/:?@!$'()*+,;-._~"), ""))
                send_headers = {"User-Agent": "CGCGA/Xigpet (https://github.com/CGCGA/Xigpet)", "Accept": "*/*", **extra}
                if "Referer" in send_headers:
                    referer = request_referer(send_headers.pop("Referer"), url)
                    if referer:
                        send_headers["Referer"] = referer
                if parts.scheme == "http" and first_scheme == "https":
                    send_headers = {k:v for k,v in send_headers.items() if k.lower() not in {"referer", "origin"}}
                request = Request(url)
                self.cookies.add_cookie_header(request)
                cookie = request.get_header("Cookie")
                if cookie and len(cookie) <= 16384 and not any(c in cookie for c in "\r\n\0"):
                    send_headers["Cookie"] = cookie
                send_headers.update({"Host": host, "Accept-Encoding": "identity", "Connection": "close"})
                connection.request("GET", path, headers=send_headers)
                response = connection.getresponse()
                if isinstance(getattr(response, "headers", None), Message):
                    self.cookies.extract_cookies(response, request)
                    for cookie in list(self.cookies):
                        if len(cookie.name) + len(cookie.value or "") > 8192:
                            self.cookies.clear(cookie.domain, cookie.path, cookie.name)
                    if len(list(self.cookies)) > 200:
                        self.cookies.clear()
                if response.status in (301, 302, 303, 307, 308):
                    target = response.getheader("Location")
                    self.release(connection, response)
                    if not target:
                        raise ResourceError("网页重定向缺少目标地址。")
                    url = urljoin(url, target)
                    continue
                if response.status != 200:
                    raise ResourceError(f"网站返回HTTP {response.status}，未下载；不绕过访问限制。")
                encoding = response.getheader("Content-Encoding", "identity").lower()
                if encoding not in ("identity", ""):
                    raise ResourceError("网站未返回可安全限量读取的未压缩响应。")
                return connection, response, url
            except Exception:
                self.release(connection)
                raise
        raise ResourceError("网页重定向次数过多。")

    def release(self, connection, response=None):
        if response is not None:
            response.close()
        connection.close()
        with self.lock:
            self.connections.discard(connection)

    def read(self, url, maximum=4 * 1024 * 1024, *, headers=None):
        connection, response, final = self.open(url, headers=headers) if headers else self.open(url)
        try:
            data = bytearray()
            while True:
                self.check()
                block = response.read1(min(65536, maximum + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                if len(data) > maximum:
                    raise ResourceError("页面资源过大，已停止读取。")
            return bytes(data), dict(response.getheaders()), final
        finally:
            self.release(connection, response)
