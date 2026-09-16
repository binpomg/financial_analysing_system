"""Bounded public-source downloads with redirect and DNS-rebinding protection."""

import hashlib
import http.client
import ipaddress
import os
import re
import socket
import ssl
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit


class SourceError(ValueError):
    """A source could not be safely or completely obtained."""


def _validate_url(url, allowed_hosts=None):
    if not isinstance(url, str) or not url.strip():
        raise SourceError("来源 URL 不能为空")
    url = url.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise SourceError("URL 含控制字符或反斜杠")
    if "://" not in url:
        url = "https://" + url
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("https", "http") or not parsed.hostname:
            raise SourceError("仅允许 HTTP/HTTPS 公开来源")
        if parsed.username is not None or parsed.password is not None:
            raise SourceError("来源 URL 不得包含登录凭据")
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        if allowed_hosts is not None:
            allowed = {str(value).rstrip(".").encode("idna").decode("ascii").lower()
                       for value in allowed_hosts}
            if host not in allowed:
                raise SourceError("来源或重定向主机不在白名单：%s" % host)
        addresses = list(dict.fromkeys(item[4][0] for item in
                                      socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(address.split("%")[0]).is_global
                                for address in addresses):
            raise SourceError("禁止私网、回环、保留地址或混合 DNS 解析：%s" % host)
        netloc = ("[%s]" % host if ":" in host else host)
        if port != (443 if parsed.scheme.lower() == "https" else 80):
            netloc += ":%d" % port
        path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parsed.query, safe="=&?/:;+,%@!$'()*[]-._~")
        canonical = urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))
        return canonical, host, port, addresses
    except (UnicodeError, OSError, ValueError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError("来源地址校验失败：%s" % exc) from exc


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # 连接已验证 IP，同时按原始主机名完成 SNI 与证书校验，避免 DNS 重绑定。
        connection = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except Exception:
            connection.close()
            raise


def _request(url, allowed_hosts=None, timeout=30, max_redirects=5):
    current, visited = url, set()
    for hop in range(max_redirects + 1):
        canonical, host, port, addresses = _validate_url(current, allowed_hosts)
        if canonical in visited:
            raise SourceError("来源重定向循环")
        visited.add(canonical)
        parsed = urlsplit(canonical)
        connection_type = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
        response = connection = None
        for attempt in range(3):
            connection = connection_type(host, port, addresses[attempt % len(addresses)], timeout)
            try:
                connection.request("GET", urlunsplit(("", "", parsed.path, parsed.query, "")),
                                   headers={"User-Agent": "FinancialResearchSystem/0.1 (public-source research)",
                                            "Accept-Encoding": "identity", "Accept": "*/*"})
                response = connection.getresponse()
                if response.status in (429, 500, 502, 503, 504) and attempt < 2:
                    response.close()
                    connection.close()
                    time.sleep(0.25 * (attempt + 1))
                    continue
                break
            except (OSError, http.client.HTTPException) as exc:
                connection.close()
                if attempt == 2:
                    raise SourceError("来源连接失败，已有限重试：%s" % exc) from exc
                time.sleep(0.25 * (attempt + 1))
        if response.status in (301, 302, 303, 307, 308):
            location = response.getheader("Location")
            response.close()
            connection.close()
            if not location or hop == max_redirects:
                raise SourceError("来源重定向缺少位置或超过 %d 跳" % max_redirects)
            current = urljoin(canonical, location)
            # 下一跳重新校验主机和所有解析地址；不复用上一跳的授权边界。
            continue
        if not 200 <= response.status < 300 or response.status == 206:
            status = response.status
            response.close()
            connection.close()
            raise SourceError("来源返回 HTTP %d，未保存为完整原件" % status)
        return response, connection, canonical
    raise SourceError("来源重定向超过上限")


def _stream(response, max_bytes):
    length = response.getheader("Content-Length")
    if response.getheader("Content-Encoding", "identity").lower() not in ("", "identity"):
        raise SourceError("服务端返回压缩传输内容，未将压缩字节冒充原件")
    try:
        expected = int(length) if length is not None else None
    except ValueError as exc:
        raise SourceError("来源 Content-Length 非法") from exc
    if expected is not None and (expected < 0 or expected > max_bytes):
        raise SourceError("来源大小超过限制或为非法值")
    total = 0
    try:
        while True:
            block = response.read(min(65536, max_bytes - total + 1))
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise SourceError("来源流超过 max_bytes=%d，未保留不完整下载" % max_bytes)
            yield block
    except (OSError, http.client.HTTPException) as exc:
        raise SourceError("下载流中断，未保存为完整原件：%s" % exc) from exc
    if expected is not None and total != expected:
        raise SourceError("下载不完整：声明 %d 字节，实际 %d 字节" % (expected, total))
    if total == 0:
        raise SourceError("来源返回空文件")


def _filename(url, content_type):
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .")[:150]
    if not name or name in (".", ".."):
        name = "document"
    if re.match(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        name = "document_" + name
    if not Path(name).suffix:
        suffixes = {"application/pdf": ".pdf", "text/html": ".html", "text/plain": ".txt",
                    "text/csv": ".csv", "application/json": ".json", "application/xml": ".xml",
                    "application/rss+xml": ".xml", "application/atom+xml": ".xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
                    "image/png": ".png", "image/jpeg": ".jpg"}
        name += suffixes.get(content_type.split(";", 1)[0].lower(), ".bin")
    return name


def fetch_url(url, destination_dir, allowed_hosts=None, max_bytes=50 * 1024 * 1024, timeout=30):
    """Download a complete public file atomically, returning source provenance."""
    if max_bytes <= 0 or timeout <= 0:
        raise SourceError("max_bytes 与 timeout 必须为正数")
    destination = Path(destination_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    response, connection, final_url = _request(url, allowed_hosts, timeout)
    temporary = None
    try:
        content_type = response.getheader("Content-Type", "application/octet-stream")
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(prefix=".download-", suffix=".partial", dir=str(destination),
                                         delete=False) as output:
            temporary = Path(output.name)
            for block in _stream(response, max_bytes):
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        fingerprint = digest.hexdigest()
        path = destination / (fingerprint[:20] + "-" + _filename(final_url, content_type))
        os.replace(str(temporary), str(path))
        temporary = None
        return {"path": str(path), "url": url, "final_url": final_url, "sha256": fingerprint,
                "downloaded_at": datetime.now(timezone.utc).isoformat(), "content_type": content_type}
    finally:
        response.close()
        connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.current, self.title = [], None, []
        self.base = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "base" and self.base is None and attributes.get("href"):
            self.base = attributes["href"]
        if tag == "a" and attributes.get("href"):
            self.current, self.title = attributes["href"], []

    def handle_data(self, data):
        if self.current is not None:
            self.title.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.links.append((self.current, " ".join("".join(self.title).split())))
            self.current, self.title = None, []


def discover_links(url, allowed_hosts=None, max_links=100):
    """Read an RSS/Atom/HTML index. Returns discovery links, never relevance decisions.

    max_links bounds discovery only. Every fetched business document still requires
    full reading by each enabled line. External targets are checked again on fetch.
    """
    if max_links <= 0:
        raise SourceError("max_links 必须为正数")
    response, connection, final_url = _request(url, allowed_hosts, 30)
    try:
        content_type = response.getheader("Content-Type", "")
        raw = b"".join(_stream(response, 10 * 1024 * 1024))
    finally:
        response.close()
        connection.close()
    charset = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", content_type, re.I)
    try:
        text = raw.decode(charset.group(1) if charset else "utf-8-sig")
    except (UnicodeError, LookupError) as exc:
        raise SourceError("索引编码无法无损解码，请确认来源编码：%s" % exc) from exc
    links, link_base = [], final_url
    if re.search(r"<!ENTITY", text, re.I) or (re.search(r"<!DOCTYPE", text, re.I)
                                             and not re.search(r"<!DOCTYPE\s+html\b[^>]*>", text, re.I)):
        raise SourceError("不解析带实体定义的 XML 索引")
    try:
        tree = ET.fromstring(text)
    except ET.ParseError:
        parser = _Links()
        parser.feed(text)
        links = parser.links
        link_base = urljoin(final_url, parser.base or final_url)
    else:
        for item in tree.iter():
            if item.tag.split("}")[-1] not in ("item", "entry"):
                continue
            title = next(("".join(child.itertext()).strip() for child in item
                          if child.tag.split("}")[-1] == "title"), "")
            for child in item:
                if child.tag.split("}")[-1] == "link":
                    target = child.get("href") or (child.text or "").strip()
                    if target and child.get("rel", "alternate") in ("alternate", "enclosure"):
                        links.append((target, title))
                elif child.tag.split("}")[-1] == "enclosure" and child.get("url"):
                    links.append((child.get("url"), title))
        if not links and tree.tag.split("}")[-1].lower() == "html":
            parser = _Links()
            parser.feed(text)
            links = parser.links
            link_base = urljoin(final_url, parser.base or final_url)
    found, seen = [], set()
    allowed = None if allowed_hosts is None else {str(host).rstrip(".").lower() for host in allowed_hosts}
    for target, title in links:
        full = urljoin(link_base, target)
        parsed = urlsplit(full)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
            continue
        if allowed is not None and parsed.hostname.rstrip(".").lower() not in allowed:
            continue
        full = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if full in seen:
            continue
        seen.add(full)
        found.append({"url": full, "title": title or full})
        if len(found) >= max_links:
            break
    return found
