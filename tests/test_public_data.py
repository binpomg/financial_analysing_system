from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from pydantic import ValidationError

from finagent.catalog import validate_catalog
from finagent.public_data import (
    CninfoCollectionRequest,
    CninfoCollector,
    CninfoIssuer,
    CNINFO_QUERY_URL,
    CNINFO_STATIC_HOST,
    HARD_MAX_BYTES,
    CrawlLimits,
    SecCollectionRequest,
    SecEdgarCollector,
    dry_run_sec_edgar,
    dry_run_cninfo,
)


def _transport(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        response = responses.get(str(request.url))
        if response is None:
            return httpx.Response(404, request=request)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )

    return httpx.MockTransport(handler)


def _submissions() -> bytes:
    return json.dumps(
        {
            "filings": {
                "recent": {
                    "form": ["10-K", "8-K"],
                    "accessionNumber": ["0000123456-25-000001", "0000123456-25-000002"],
                    "primaryDocument": ["annual-report.htm", "current.htm"],
                    "filingDate": ["2025-02-28", "2025-03-01"],
                    "reportDate": ["2024-12-31", ""],
                }
            }
        }
    ).encode("utf-8")


def test_sec_dry_run_never_calls_network_and_has_no_filing_guess(tmp_path: Path) -> None:
    request = SecCollectionRequest(cik="123456", include_companyfacts=True)
    result = dry_run_sec_edgar(request, tmp_path / "public", CrawlLimits())

    assert result["mode"] == "dry_run"
    assert len(result["planned_requests"]) == 2
    assert all("Archives" not in item["url"] for item in result["planned_requests"])
    assert not (tmp_path / "public").exists()


def test_sec_collection_creates_catalog_but_no_financial_facts(tmp_path: Path) -> None:
    output = tmp_path / "public"
    request = SecCollectionRequest(cik="123456", max_filings=1, include_companyfacts=True)
    submissions_url = "https://data.sec.gov/submissions/CIK0000123456.json"
    filing_url = (
        "https://www.sec.gov/Archives/edgar/data/123456/000012345625000001/annual-report.htm"
    )
    facts_url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000123456.json"
    collector = SecEdgarCollector(
        CrawlLimits(quota_bytes=10_000, max_file_bytes=5_000, rate_limit_per_second=10),
        "FinAgent test contact@example.test",
        transport=_transport(
            {
                submissions_url: httpx.Response(200, content=_submissions()),
                filing_url: httpx.Response(200, content=b"<html>annual disclosure</html>"),
                facts_url: httpx.Response(200, content=b'{"facts": {}}'),
            }
        ),
        sleep=lambda _: None,
    )

    result = collector.collect(request, output)

    assert result.issues == []
    assert result.request_count == 3
    assert len(result.files) == 3
    assert result.catalog_path is not None
    catalog = Path(result.catalog_path)
    validation = validate_catalog(catalog)
    assert validation["valid"] is True
    assert validation["source_count"] == 1
    source = validation["sources"][0]
    assert source["document_type"] == "sec_10_k"
    assert source["path_exists"] is True
    catalog_payload = json.loads(catalog.read_text(encoding="utf-8"))
    assert catalog_payload["sources"][0]["source_url"] == filing_url
    assert catalog_payload["sources"][0]["retrieved_at"].endswith("+00:00")
    # The raw HTML and structured JSON remain source material; this collector never creates facts.
    assert not list(output.rglob("*.sqlite3"))


def test_declared_or_streamed_content_cannot_exceed_hard_quota(tmp_path: Path) -> None:
    request = SecCollectionRequest(cik="123456", max_filings=1)
    submissions_url = "https://data.sec.gov/submissions/CIK0000123456.json"
    filing_url = (
        "https://www.sec.gov/Archives/edgar/data/123456/000012345625000001/annual-report.htm"
    )
    collector = SecEdgarCollector(
        CrawlLimits(quota_bytes=500, max_file_bytes=500, rate_limit_per_second=10),
        "FinAgent test contact@example.test",
        transport=_transport(
            {
                submissions_url: httpx.Response(200, content=_submissions()),
                filing_url: httpx.Response(200, headers={"content-length": "501"}, content=b"x" * 501),
            }
        ),
        sleep=lambda _: None,
    )

    result = collector.collect(request, tmp_path / "public")

    assert result.downloaded_bytes == len(_submissions())
    assert any(item["reason"] == "declared content exceeds max_file_bytes" for item in result.skipped)
    assert result.downloaded_bytes <= 500


def test_stream_without_content_length_stops_before_reading_remaining_chunks(tmp_path: Path) -> None:
    class RecordingStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.yielded = 0
            self.closed = False

        def __iter__(self) -> Iterator[bytes]:
            for chunk in (b"x" * 300, b"y" * 300, b"z" * 300):
                self.yielded += 1
                yield chunk

        def close(self) -> None:
            self.closed = True

    request = SecCollectionRequest(cik="123456", max_filings=1)
    submissions_url = "https://data.sec.gov/submissions/CIK0000123456.json"
    filing_url = (
        "https://www.sec.gov/Archives/edgar/data/123456/000012345625000001/annual-report.htm"
    )
    stream = RecordingStream()

    def handler(http_request: httpx.Request) -> httpx.Response:
        if str(http_request.url) == submissions_url:
            return httpx.Response(200, content=_submissions(), request=http_request)
        if str(http_request.url) == filing_url:
            return httpx.Response(200, stream=stream, request=http_request)
        return httpx.Response(404, request=http_request)

    result = SecEdgarCollector(
        CrawlLimits(quota_bytes=2_000, max_file_bytes=500, rate_limit_per_second=10),
        "FinAgent test contact@example.test",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    ).collect(request, tmp_path / "public")

    assert any(item["reason"] == "response exceeds max_file_bytes" for item in result.skipped)
    assert stream.yielded == 2
    assert stream.closed is True
    assert result.downloaded_bytes == len(_submissions())


def test_collection_limits_reject_more_than_two_gib_and_execute_needs_contact() -> None:
    with pytest.raises(ValidationError):
        CrawlLimits(quota_bytes=HARD_MAX_BYTES + 1)
    with pytest.raises(ValueError, match="contact User-Agent"):
        SecEdgarCollector(CrawlLimits(), "anonymous-client")


def test_cninfo_collector_selects_only_primary_annual_reports_and_writes_catalog(tmp_path: Path) -> None:
    issuer = CninfoIssuer(
        code="600519", org_id="gssh0600519", market="sse", entity_id="cn:600519", dataset_split="train"
    )
    request = CninfoCollectionRequest(issuers=(issuer,), fiscal_years=(2022, 2023))
    listing = {
        "announcements": [
            {
                "secCode": "600519",
                "announcementId": "a2022",
                "announcementTitle": "贵州茅台2022年年度报告",
                "announcementTime": 1680192000000,
                "adjunctUrl": "finalpage/2023-03-31/1216281757.PDF",
                "adjunctType": "PDF",
            },
            {
                "secCode": "600519",
                "announcementId": "summary",
                "announcementTitle": "贵州茅台2022年年度报告摘要",
                "announcementTime": 1680192000000,
                "adjunctUrl": "finalpage/2023-03-31/1216281752.PDF",
                "adjunctType": "PDF",
            },
            {
                "secCode": "600519",
                "announcementId": "a2023",
                "announcementTitle": "贵州茅台2023年年度报告",
                "announcementTime": 1712073600000,
                "adjunctUrl": "finalpage/2024-04-03/1219506510.PDF",
                "adjunctType": "PDF",
            },
        ]
    }
    urls = {
        f"{CNINFO_STATIC_HOST}/finalpage/2023-03-31/1216281757.PDF": b"%PDF-1.7 2022",
        f"{CNINFO_STATIC_HOST}/finalpage/2024-04-03/1219506510.PDF": b"%PDF-1.7 2023",
    }

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.method == "POST" and str(http_request.url) == CNINFO_QUERY_URL:
            return httpx.Response(200, json=listing, request=http_request)
        return httpx.Response(200, content=urls.get(str(http_request.url), b"not found"), request=http_request)

    result = CninfoCollector(
        CrawlLimits(quota_bytes=10_000, max_file_bytes=5_000, rate_limit_per_second=10),
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    ).collect(request, tmp_path / "cninfo")

    assert result.issues == []
    assert result.request_count == 3
    assert [item["source_id"] for item in result.files] == ["cninfo-600519-a2022", "cninfo-600519-a2023"]
    validation = validate_catalog(Path(result.catalog_path or ""))
    assert validation["valid"] is True
    assert validation["sources"][0]["data_class"] == "public"
    assert validation["sources"][0]["dataset_split"] == "train"


def test_cninfo_dry_run_has_no_network_or_output(tmp_path: Path) -> None:
    issuer = CninfoIssuer(
        code="600519", org_id="gssh0600519", market="sse", entity_id="cn:600519", dataset_split="train"
    )
    result = dry_run_cninfo(
        CninfoCollectionRequest(issuers=(issuer,), fiscal_years=(2022,)),
        tmp_path / "cninfo",
        CrawlLimits(),
    )
    assert result["mode"] == "dry_run"
    assert len(result["planned_queries"]) == 1
    assert not (tmp_path / "cninfo").exists()
