import hashlib
import io
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from finresearch import sources


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.closed = False

    def read(self, count):
        return self.body.read(count)

    def getheader(self, key, default=None):
        return self.headers.get(key.lower(), default)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def public_dns(host, port, **kwargs):
    address = "127.0.0.1" if host in ("127.0.0.1", "internal.example") else "93.184.216.34"
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.destination = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def request_patch(self, response):
        return mock.patch.object(sources, "_request", return_value=(
            response, FakeConnection(response), "https://public.example/original.pdf"))

    def test_atomic_fetch_records_exact_original_and_hash(self):
        body = b"%PDF-1.7\noriginal bytes\n"
        response = FakeResponse(body, headers={"Content-Length": str(len(body)),
                                               "Content-Type": "application/pdf"})
        with self.request_patch(response):
            result = sources.fetch_url("https://public.example/source", self.destination)
        self.assertEqual(Path(result["path"]).read_bytes(), body)
        self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())
        self.assertTrue(response.closed)
        self.assertFalse(list(self.destination.glob("*.partial")))

    def test_stream_limit_removes_partial_download(self):
        with self.request_patch(FakeResponse(b"0123456789")):
            with self.assertRaisesRegex(sources.SourceError, "max_bytes"):
                sources.fetch_url("https://public.example", self.destination, max_bytes=5)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_declared_length_mismatch_is_incomplete(self):
        with self.request_patch(FakeResponse(b"abc", headers={"Content-Length": "10"})):
            with self.assertRaisesRegex(sources.SourceError, "下载不完整"):
                sources.fetch_url("https://public.example", self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_declared_oversize_rejected_before_writing(self):
        with self.request_patch(FakeResponse(b"abc", headers={"Content-Length": "999999999"})):
            with self.assertRaises(sources.SourceError):
                sources.fetch_url("https://public.example", self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_empty_and_compressed_responses_are_not_original_documents(self):
        for response in [FakeResponse(b""), FakeResponse(b"fakegzip", headers={"Content-Encoding": "gzip"})]:
            with self.request_patch(response), self.assertRaises(sources.SourceError):
                sources.fetch_url("https://public.example", self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    @mock.patch.object(sources.socket, "getaddrinfo", side_effect=public_dns)
    def test_plain_hostname_defaults_to_https(self, dns):
        result = sources._validate_url("public.example/index")
        self.assertEqual(result[0], "https://public.example/index")

    @mock.patch.object(sources.socket, "getaddrinfo", side_effect=public_dns)
    def test_chinese_paths_are_encoded_for_http_transport(self, dns):
        result = sources._validate_url("https://public.example/公告.pdf?标题=年报")
        self.assertIn("%E5%85%AC%E5%91%8A.pdf", result[0])
        self.assertTrue(result[0].isascii())

    @mock.patch.object(sources.socket, "getaddrinfo", side_effect=public_dns)
    def test_private_target_rejected(self, dns):
        with self.assertRaisesRegex(sources.SourceError, "私网"):
            sources._validate_url("http://127.0.0.1/file")

    def test_mixed_public_private_dns_is_rejected(self):
        addresses = public_dns("public.example", 443) + public_dns("127.0.0.1", 443)
        with mock.patch.object(sources.socket, "getaddrinfo", return_value=addresses):
            with self.assertRaisesRegex(sources.SourceError, "混合 DNS"):
                sources._validate_url("https://public.example/file")

    def test_bad_schemes_credentials_controls_and_allowlist_are_rejected(self):
        for url in ["file:///secret", "https://" + "user:password@" + "public.example/", "https://public.example/\nX: a"]:
            with self.assertRaises(sources.SourceError):
                sources._validate_url(url)
        with self.assertRaisesRegex(sources.SourceError, "白名单"):
            sources._validate_url("https://elsewhere.example/", ["public.example"])

    @mock.patch.object(sources.socket, "getaddrinfo", side_effect=public_dns)
    def test_redirect_to_private_address_rejected_before_second_connection(self, dns):
        response = FakeResponse(status=302, headers={"Location": "http://127.0.0.1/secret"})
        first = FakeConnection(response)
        with mock.patch.object(sources, "_PinnedHTTPSConnection", return_value=first) as connection:
            with self.assertRaisesRegex(sources.SourceError, "私网"):
                sources._request("https://public.example/index")
        self.assertEqual(connection.call_count, 1)
        self.assertTrue(first.closed)

    @mock.patch.object(sources.socket, "getaddrinfo", side_effect=public_dns)
    def test_redirect_to_off_allowlist_rejected(self, dns):
        first = FakeConnection(FakeResponse(status=302, headers={"Location": "https://elsewhere.example/file"}))
        with mock.patch.object(sources, "_PinnedHTTPSConnection", return_value=first):
            with self.assertRaisesRegex(sources.SourceError, "白名单"):
                sources._request("https://public.example/", ["public.example"])

    @mock.patch.object(sources.socket, "getaddrinfo", side_effect=public_dns)
    def test_partial_http_status_is_rejected(self, dns):
        first = FakeConnection(FakeResponse(status=206))
        with mock.patch.object(sources, "_PinnedHTTPSConnection", return_value=first):
            with self.assertRaisesRegex(sources.SourceError, "206"):
                sources._request("https://public.example/")

    @mock.patch.object(sources.socket, "getaddrinfo", side_effect=public_dns)
    @mock.patch.object(sources.time, "sleep")
    def test_transient_errors_have_bounded_retries(self, sleep, dns):
        response = FakeResponse(status=503)
        with mock.patch.object(sources, "_PinnedHTTPSConnection", side_effect=lambda *args:
                               FakeConnection(response)) as connection:
            with self.assertRaisesRegex(sources.SourceError, "503"):
                sources._request("https://public.example/")
        self.assertEqual(connection.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_tls_connect_pins_ip_and_uses_original_hostname_for_certificate(self):
        context = mock.Mock()
        context.wrap_socket.return_value = mock.Mock()
        with mock.patch.object(sources.ssl, "create_default_context", return_value=context), \
                mock.patch.object(sources.socket, "create_connection") as connect:
            connection = sources._PinnedHTTPSConnection("public.example", 443, "93.184.216.34", 10)
            connection.connect()
        connect.assert_called_once_with(("93.184.216.34", 443), 10)
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "public.example")

    def test_discovery_preserves_all_types_without_relevance_filter(self):
        body = ('<html><a href="/report.pdf">年报</a><a href="/unrelated.html">公司活动</a>'
                '<a href="/report.pdf#page=2">重复</a><a href="javascript:alert(1)">bad</a></html>').encode()
        with self.request_patch(FakeResponse(body, headers={"Content-Type": "text/html; charset=utf-8"})):
            links = sources.discover_links("https://public.example/index")
        self.assertEqual([link["title"] for link in links], ["年报", "公司活动"])

    def test_html_base_location_is_applied_to_relative_document_links(self):
        body = (b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN">'
                b'<html><head><base href="/financial/reports/"></head>'
                b'<body><a href="report.pdf">Report</a></body></html>')
        with self.request_patch(FakeResponse(body)):
            links = sources.discover_links("https://public.example/index")
        self.assertEqual(links, [{"url": "https://public.example/financial/reports/report.pdf", "title": "Report"}])

    def test_rss_and_atom_discovery(self):
        feeds = [b'<rss><channel><item><title>Report</title><link>/r.pdf</link>'
                 b'<enclosure url="/a.xlsx"/></item></channel></rss>',
                 b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Report</title>'
                 b'<link href="/r.pdf"/><link rel="enclosure" href="/a.xlsx"/></entry></feed>']
        for body in feeds:
            with self.request_patch(FakeResponse(body)):
                links = sources.discover_links("https://public.example/index")
            self.assertEqual([item["url"] for item in links],
                             ["https://public.example/r.pdf", "https://public.example/a.xlsx"])

    def test_feed_entity_definitions_are_rejected_before_xml_parsing(self):
        body = b'<!DOCTYPE rss [<!ENTITY data "x">]><rss><channel>&data;</channel></rss>'
        with self.request_patch(FakeResponse(body)), self.assertRaisesRegex(sources.SourceError, "实体"):
            sources.discover_links("https://public.example/index")

    def test_windows_filename_is_safe(self):
        self.assertEqual(sources._filename("https://example.org/CON", "application/pdf"), "document_CON.pdf")
        name = sources._filename("https://example.org/%2e%2e%2foutside%3Aname", "application/pdf")
        self.assertNotIn("/", name)
        self.assertNotIn(":", name)
        self.assertNotIn("\\", name)


if __name__ == "__main__":
    unittest.main()
