"""Small, policy-bound collection from the SEC's public EDGAR endpoints.

The collector deliberately uses structured SEC endpoints and archive document URLs,
rather than scraping HTML search pages.  It only creates raw source files plus a
pilot catalog; callers must still use catalog ingestion and candidate review before
anything can enter the financial-fact layer.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field, field_validator, model_validator

from finagent.contracts import DataClass, StrictModel


GIB = 1024**3
HARD_MAX_BYTES = 2 * GIB
SEC_DATA_HOST = "https://data.sec.gov"
SEC_ARCHIVES_HOST = "https://www.sec.gov"
CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_HOST = "https://static.cninfo.com.cn"
DEFAULT_FORMS = ("10-K",)


class CrawlLimits(StrictModel):
    """Hard limits for one local collection destination.

    ``quota_bytes`` is intentionally bounded by ``HARD_MAX_BYTES`` even when a
    caller constructs this object directly instead of using the CLI.
    """

    quota_bytes: int = Field(default=HARD_MAX_BYTES, ge=1, le=HARD_MAX_BYTES)
    max_file_bytes: int = Field(default=100 * 1024**2, ge=1, le=HARD_MAX_BYTES)
    max_requests: int = Field(default=50, ge=1, le=500)
    rate_limit_per_second: float = Field(default=5.0, gt=0, le=10.0)
    timeout_seconds: float = Field(default=30.0, gt=0, le=120.0)
    retries: int = Field(default=2, ge=0, le=3)

    @field_validator("max_file_bytes")
    @classmethod
    def file_must_fit_in_quota(cls, value: int, info: Any) -> int:
        quota = info.data.get("quota_bytes")
        if quota is not None and value > quota:
            raise ValueError("max_file_bytes cannot exceed quota_bytes")
        return value


class SecCollectionRequest(StrictModel):
    cik: str = Field(pattern=r"^\d{1,10}$")
    forms: tuple[str, ...] = DEFAULT_FORMS
    max_filings: int = Field(default=3, ge=1, le=20)
    include_companyfacts: bool = False

    @field_validator("forms")
    @classmethod
    def only_supported_forms(cls, forms: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {"10-K", "10-Q", "20-F", "40-F"}
        normalized = tuple(form.upper().strip() for form in forms if form.strip())
        if not normalized:
            raise ValueError("at least one SEC form is required")
        unsupported = set(normalized) - allowed
        if unsupported:
            raise ValueError(f"unsupported SEC forms: {sorted(unsupported)}")
        return normalized

    @property
    def padded_cik(self) -> str:
        return self.cik.zfill(10)

    @property
    def entity_id(self) -> str:
        return f"sec:cik:{self.padded_cik}"


class CninfoIssuer(StrictModel):
    """One listed Chinese issuer as identified by CNINFO's public query API."""

    code: str = Field(pattern=r"^\d{6}$")
    org_id: str = Field(pattern=r"^(?:gss[hz]\d{7}|\d{10})$")
    market: str = Field(pattern=r"^(sse|szse)$")
    entity_id: str = Field(min_length=1)
    dataset_split: str = Field(pattern=r"^(train|dev|test)$")

    @property
    def column(self) -> str:
        return self.market


class CninfoCollectionRequest(StrictModel):
    """A small, named annual-report collection for the public-data pilot."""

    issuers: tuple[CninfoIssuer, ...] = Field(min_length=1, max_length=20)
    fiscal_years: tuple[int, ...] = Field(min_length=1, max_length=5)

    @field_validator("fiscal_years")
    @classmethod
    def supported_years(cls, years: tuple[int, ...]) -> tuple[int, ...]:
        normalized = tuple(sorted(set(years)))
        if any(year < 1990 or year > date.today().year for year in normalized):
            raise ValueError("fiscal_years must be plausible calendar years")
        return normalized

    @model_validator(mode="after")
    def issuers_are_unique(self) -> "CninfoCollectionRequest":
        codes = [issuer.code for issuer in self.issuers]
        entities = [issuer.entity_id for issuer in self.issuers]
        if len(codes) != len(set(codes)) or len(entities) != len(set(entities)):
            raise ValueError("CNINFO issuers must have unique code and entity_id values")
        return self


DEFAULT_CNINFO_ISSUERS = (
    CninfoIssuer(code="600519", org_id="gssh0600519", market="sse", entity_id="cn:600519", dataset_split="train"),
    CninfoIssuer(code="000333", org_id="9900005965", market="szse", entity_id="cn:000333", dataset_split="train"),
    CninfoIssuer(code="600309", org_id="gssh0600309", market="sse", entity_id="cn:600309", dataset_split="train"),
    CninfoIssuer(code="601012", org_id="9900022338", market="sse", entity_id="cn:601012", dataset_split="dev"),
    CninfoIssuer(code="600900", org_id="gssh0600900", market="sse", entity_id="cn:600900", dataset_split="test"),
)


class CollectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlannedDownload:
    kind: str
    url: str
    relative_path: str
    source_id: str | None = None
    document_type: str | None = None
    published_at: date | None = None
    reporting_period_end: date | None = None
    version: str = "1"
    source_tier: str = "official_regulatory_disclosure"
    license_label: str = "SEC public EDGAR data; review SEC terms before reuse"
    data_class: DataClass = DataClass.PUBLIC


@dataclass
class CollectionResult:
    mode: str
    output_dir: str
    entity_id: str
    quota_bytes: int
    existing_bytes: int
    downloaded_bytes: int = 0
    request_count: int = 0
    files: list[dict[str, object]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    catalog_path: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "output_dir": self.output_dir,
            "entity_id": self.entity_id,
            "quota_bytes": self.quota_bytes,
            "existing_bytes": self.existing_bytes,
            "downloaded_bytes": self.downloaded_bytes,
            "request_count": self.request_count,
            "files": self.files,
            "skipped": self.skipped,
            "issues": self.issues,
            "catalog_path": self.catalog_path,
        }


def sec_dry_run_plan(request: SecCollectionRequest, output_dir: Path) -> list[PlannedDownload]:
    """Return known endpoint URLs without contacting a network service."""
    root = _entity_relative_root(request)
    plan = [
        PlannedDownload(
            kind="submissions",
            url=f"{SEC_DATA_HOST}/submissions/CIK{request.padded_cik}.json",
            relative_path=str(root / "metadata" / "submissions.json"),
        )
    ]
    if request.include_companyfacts:
        plan.append(
            PlannedDownload(
                kind="companyfacts",
                url=f"{SEC_DATA_HOST}/api/xbrl/companyfacts/CIK{request.padded_cik}.json",
            relative_path=str(root / "metadata" / "companyfacts.json"),
            )
        )
    return plan


class SecEdgarCollector:
    """Conservative SEC fetcher with hard byte and request accounting."""

    def __init__(
        self,
        limits: CrawlLimits,
        user_agent: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        if not user_agent.strip() or "@" not in user_agent:
            raise ValueError("SEC execution requires a contact User-Agent containing an email address")
        self.limits = limits
        self.user_agent = user_agent.strip()
        self.transport = transport
        self.sleep = sleep
        self._last_request_at: float | None = None

    def collect(self, request: SecCollectionRequest, output_dir: Path) -> CollectionResult:
        root = output_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        existing_bytes = _directory_bytes(root)
        result = CollectionResult(
            mode="execute",
            output_dir=str(root),
            entity_id=request.entity_id,
            quota_bytes=self.limits.quota_bytes,
            existing_bytes=existing_bytes,
        )
        if existing_bytes >= self.limits.quota_bytes:
            result.issues.append("output directory already meets or exceeds the collection quota")
            return result

        with httpx.Client(
            timeout=self.limits.timeout_seconds,
            follow_redirects=False,
            transport=self.transport,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"},
        ) as client:
            submissions_plan = sec_dry_run_plan(request, root)[0]
            raw = self._download(client, submissions_plan, root, result)
            if raw is None:
                return result
            try:
                submissions = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                result.issues.append(f"SEC submissions response was not valid JSON: {exc}")
                return result

            filing_plans = _filing_plans(request, submissions)
            for plan in filing_plans:
                self._download(client, plan, root, result)
                if _quota_exhausted(result):
                    result.issues.append("collection stopped because the byte quota was reached")
                    break

            if request.include_companyfacts and not _quota_exhausted(result):
                facts_plan = sec_dry_run_plan(request, root)[1]
                self._download(client, facts_plan, root, result)

        self._write_metadata(request, root, result)
        return result

    def _download(
        self,
        client: httpx.Client,
        plan: PlannedDownload,
        root: Path,
        result: CollectionResult,
        *,
        expect_pdf: bool = False,
    ) -> bytes | None:
        if result.request_count >= self.limits.max_requests:
            result.issues.append("collection stopped at max_requests")
            return None
        destination = _safe_destination(root, plan.relative_path)
        if destination.exists():
            result.skipped.append({"url": plan.url, "reason": "destination already exists"})
            return destination.read_bytes()
        try:
            response = self._request(client, plan.url, result)
        except CollectionError as exc:
            result.issues.append(str(exc))
            return None
        try:
            content_length = response.headers.get("content-length")
            if content_length and content_length.isdigit():
                declared = int(content_length)
                if declared > self.limits.max_file_bytes:
                    result.skipped.append({"url": plan.url, "reason": "declared content exceeds max_file_bytes"})
                    return None
                if result.existing_bytes + result.downloaded_bytes + declared > self.limits.quota_bytes:
                    result.skipped.append({"url": plan.url, "reason": "declared content would exceed quota"})
                    return None

            payload = bytearray()
            for chunk in response.iter_bytes():
                projected_file = len(payload) + len(chunk)
                projected_total = result.existing_bytes + result.downloaded_bytes + projected_file
                if projected_file > self.limits.max_file_bytes:
                    result.skipped.append({"url": plan.url, "reason": "response exceeds max_file_bytes"})
                    return None
                if projected_total > self.limits.quota_bytes:
                    result.skipped.append({"url": plan.url, "reason": "response would exceed quota"})
                    return None
                payload.extend(chunk)
        finally:
            response.close()
        if expect_pdf and not payload.startswith(b"%PDF-"):
            result.skipped.append({"url": plan.url, "reason": "response is not a PDF"})
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        result.downloaded_bytes += len(payload)
        result.files.append(
            {
                "kind": plan.kind,
                "source_id": plan.source_id,
                "path": str(destination.relative_to(root)),
                "url": plan.url,
                "bytes": len(payload),
                "sha256": digest,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "source_tier": plan.source_tier,
                "license_label": plan.license_label,
                "data_class": plan.data_class.value,
                "document_type": plan.document_type,
                "published_at": plan.published_at.isoformat() if plan.published_at else None,
                "reporting_period_end": (
                    plan.reporting_period_end.isoformat() if plan.reporting_period_end else None
                ),
                "version": plan.version,
            }
        )
        return bytes(payload)

    def _request(self, client: httpx.Client, url: str, result: CollectionResult) -> httpx.Response:
        for attempt in range(self.limits.retries + 1):
            if result.request_count >= self.limits.max_requests:
                raise CollectionError("collection stopped at max_requests")
            self._wait_for_rate_limit()
            result.request_count += 1
            try:
                response = client.send(client.build_request("GET", url), stream=True)
            except httpx.HTTPError as exc:
                if attempt == self.limits.retries:
                    raise CollectionError(f"request failed for {url}: {type(exc).__name__}: {exc}") from exc
                self.sleep(min(2**attempt, 8))
                continue
            self._last_request_at = time.monotonic()
            if response.status_code == 200:
                return response
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.limits.retries:
                retry_after = response.headers.get("retry-after")
                response.close()
                delay = min(float(retry_after), 60.0) if retry_after and retry_after.isdigit() else 2**attempt
                self.sleep(delay)
                continue
            response.close()
            raise CollectionError(f"SEC returned HTTP {response.status_code} for {url}")
        raise AssertionError("unreachable")

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        minimum_gap = 1 / self.limits.rate_limit_per_second
        remaining = minimum_gap - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            self.sleep(remaining)

    def _write_metadata(self, request: SecCollectionRequest, root: Path, result: CollectionResult) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        catalog_sources = []
        for item in result.files:
            if item["source_id"] is None:
                continue
            catalog_sources.append(
                {
                    "source_id": item["source_id"],
                    "entity_id": request.entity_id,
                    "document_type": item["document_type"],
                    "relative_path": item["path"],
                    "published_at": item["published_at"],
                    "retrieved_at": item["retrieved_at"],
                    "source_url": item["url"],
                    "source_tier": item["source_tier"],
                    "license_label": item["license_label"],
                    "data_class": item["data_class"],
                    "version": item["version"],
                    "reporting_period_end": item["reporting_period_end"],
                }
            )
        catalog = {
            "schema": "finagent.pilot-catalog.v1",
            "pilot_id": f"sec-edgar-{request.padded_cik}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            "created_at": created_at,
            "sources": catalog_sources,
        }
        # An empty catalog cannot pass the current catalog contract, so write a manifest only.
        if catalog_sources:
            catalog_path = root / "pilot_catalog.sec_edgar.json"
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
            result.catalog_path = str(catalog_path)
        manifest_path = root / "crawl_manifest.sec_edgar.json"
        manifest_path.write_text(json.dumps(result.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def dry_run_sec_edgar(request: SecCollectionRequest, output_dir: Path, limits: CrawlLimits) -> dict[str, object]:
    """Return a network-free plan; filing URLs require submissions metadata and are not guessed."""
    root = output_dir.resolve()
    existing = _directory_bytes(root) if root.exists() else 0
    return {
        "mode": "dry_run",
        "entity_id": request.entity_id,
        "output_dir": str(root),
        "quota_bytes": limits.quota_bytes,
        "existing_bytes": existing,
        "planned_requests": [plan.__dict__ for plan in sec_dry_run_plan(request, root)],
        "note": "No network request is made. Filing document URLs are selected only after --execute retrieves SEC submissions metadata.",
    }


class CninfoCollector:
    """Conservative collector for named annual reports on CNINFO's public API.

    The API response is used only to locate the issuer's disclosure PDF.  It does
    not scrape search pages, paginate an exchange-wide corpus, or create facts.
    """

    def __init__(
        self,
        limits: CrawlLimits,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.limits = limits
        self.transport = transport
        self.sleep = sleep
        self._last_request_at: float | None = None

    def collect(self, request: CninfoCollectionRequest, output_dir: Path) -> CollectionResult:
        root = output_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        existing_bytes = _directory_bytes(root)
        result = CollectionResult(
            mode="execute",
            output_dir=str(root),
            entity_id="cninfo:annual-report-pilot",
            quota_bytes=self.limits.quota_bytes,
            existing_bytes=existing_bytes,
        )
        if existing_bytes >= self.limits.quota_bytes:
            result.issues.append("output directory already meets or exceeds the collection quota")
            return result

        headers = {
            "User-Agent": "FinAgentPlatform/0.1 public-disclosure-pilot",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        }
        with httpx.Client(
            timeout=self.limits.timeout_seconds,
            follow_redirects=False,
            transport=self.transport,
            headers=headers,
        ) as client:
            for issuer in request.issuers:
                announcements = self._query_announcements(client, issuer, request.fiscal_years, result)
                for plan in _cninfo_annual_report_plans(issuer, request.fiscal_years, announcements):
                    # Reuse the byte-accounted streaming writer shared with SEC.
                    SecEdgarCollector._download(self, client, plan, root, result, expect_pdf=True)
                    if _quota_exhausted(result):
                        result.issues.append("collection stopped because the byte quota was reached")
                        break
                if _quota_exhausted(result) or result.request_count >= self.limits.max_requests:
                    break

        self._write_metadata(request, root, result)
        return result

    def _query_announcements(
        self,
        client: httpx.Client,
        issuer: CninfoIssuer,
        fiscal_years: tuple[int, ...],
        result: CollectionResult,
    ) -> list[dict[str, Any]]:
        start_year = min(fiscal_years) + 1
        end_year = max(fiscal_years) + 1
        form = {
            "pageNum": "1",
            "pageSize": "30",
            "column": issuer.column,
            "tabName": "fulltext",
            "plate": "",
            "stock": f"{issuer.code},{issuer.org_id}",
            "searchkey": "",
            "secid": "",
            "category": "category_ndbg_szsh",
            "trade": "",
            "seDate": f"{start_year}-01-01~{end_year}-12-31",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        try:
            payload = self._request_json(client, CNINFO_QUERY_URL, form, result)
        except CollectionError as exc:
            result.issues.append(str(exc))
            return []
        announcements = payload.get("announcements", [])
        if not isinstance(announcements, list):
            result.issues.append(f"CNINFO returned invalid announcement list for {issuer.code}")
            return []
        return [item for item in announcements if isinstance(item, dict)]

    def _request_json(
        self,
        client: httpx.Client,
        url: str,
        form: dict[str, str],
        result: CollectionResult,
    ) -> dict[str, Any]:
        for attempt in range(self.limits.retries + 1):
            if result.request_count >= self.limits.max_requests:
                raise CollectionError("collection stopped at max_requests")
            self._wait_for_rate_limit()
            result.request_count += 1
            try:
                response = client.post(url, data=form)
            except httpx.HTTPError as exc:
                if attempt == self.limits.retries:
                    raise CollectionError(f"request failed for {url}: {type(exc).__name__}: {exc}") from exc
                self.sleep(min(2**attempt, 8))
                continue
            self._last_request_at = time.monotonic()
            if response.status_code == 200:
                try:
                    payload = response.json()
                except json.JSONDecodeError as exc:
                    raise CollectionError(f"CNINFO response was not valid JSON: {exc}") from exc
                if not isinstance(payload, dict):
                    raise CollectionError("CNINFO response was not a JSON object")
                return payload
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.limits.retries:
                retry_after = response.headers.get("retry-after")
                delay = min(float(retry_after), 60.0) if retry_after and retry_after.isdigit() else 2**attempt
                self.sleep(delay)
                continue
            raise CollectionError(f"CNINFO returned HTTP {response.status_code} for {url}")
        raise AssertionError("unreachable")

    def _request(self, client: httpx.Client, url: str, result: CollectionResult) -> httpx.Response:
        # This method is consumed by SecEdgarCollector._download above.
        for attempt in range(self.limits.retries + 1):
            if result.request_count >= self.limits.max_requests:
                raise CollectionError("collection stopped at max_requests")
            self._wait_for_rate_limit()
            result.request_count += 1
            try:
                response = client.send(client.build_request("GET", url), stream=True)
            except httpx.HTTPError as exc:
                if attempt == self.limits.retries:
                    raise CollectionError(f"request failed for {url}: {type(exc).__name__}: {exc}") from exc
                self.sleep(min(2**attempt, 8))
                continue
            self._last_request_at = time.monotonic()
            if response.status_code == 200:
                return response
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.limits.retries:
                response.close()
                self.sleep(2**attempt)
                continue
            response.close()
            raise CollectionError(f"CNINFO returned HTTP {response.status_code} for {url}")
        raise AssertionError("unreachable")

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        remaining = 1 / self.limits.rate_limit_per_second - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            self.sleep(remaining)

    def _write_metadata(
        self, request: CninfoCollectionRequest, root: Path, result: CollectionResult
    ) -> None:
        catalog_sources: list[dict[str, object]] = []
        existing_catalog_path = root / "pilot_catalog.cninfo.json"
        if existing_catalog_path.is_file():
            try:
                existing_catalog = json.loads(existing_catalog_path.read_text(encoding="utf-8"))
                existing_sources = existing_catalog.get("sources", [])
                if isinstance(existing_sources, list):
                    catalog_sources.extend(item for item in existing_sources if isinstance(item, dict))
            except (OSError, json.JSONDecodeError):
                result.issues.append("existing CNINFO pilot catalog could not be read; it was not reused")
        splits = {issuer.entity_id: issuer.dataset_split for issuer in request.issuers}
        source_ids = {str(item.get("source_id")) for item in catalog_sources}
        for item in result.files:
            if item["source_id"] is None:
                continue
            if str(item["source_id"]) in source_ids:
                continue
            entity_id = _cninfo_entity_for_source_id(str(item["source_id"]), request)
            catalog_sources.append(
                {
                    "source_id": item["source_id"],
                    "entity_id": entity_id,
                    "document_type": item["document_type"],
                    "relative_path": item["path"],
                    "published_at": item["published_at"],
                    "retrieved_at": item["retrieved_at"],
                    "source_url": item["url"],
                    "source_tier": item["source_tier"],
                    "license_label": item["license_label"],
                    "data_class": item["data_class"],
                    "version": item["version"],
                    "reporting_period_end": item["reporting_period_end"],
                    "dataset_split": splits[entity_id],
                }
            )
            source_ids.add(str(item["source_id"]))
        if catalog_sources:
            catalog = {
                "schema": "finagent.pilot-catalog.v1",
                "pilot_id": f"cninfo-annual-reports-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_root": str(root),
                "sources": catalog_sources,
            }
            path = existing_catalog_path
            path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
            result.catalog_path = str(path)
        (root / "crawl_manifest.cninfo.json").write_text(
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


def dry_run_cninfo(
    request: CninfoCollectionRequest, output_dir: Path, limits: CrawlLimits
) -> dict[str, object]:
    root = output_dir.resolve()
    return {
        "mode": "dry_run",
        "output_dir": str(root),
        "quota_bytes": limits.quota_bytes,
        "existing_bytes": _directory_bytes(root) if root.exists() else 0,
        "planned_queries": [
            {
                "issuer": issuer.entity_id,
                "code": issuer.code,
                "org_id": issuer.org_id,
                "fiscal_years": list(request.fiscal_years),
                "url": CNINFO_QUERY_URL,
            }
            for issuer in request.issuers
        ],
        "note": "No network request is made. PDFs are selected only after the official CNINFO query response.",
    }


def _cninfo_annual_report_plans(
    issuer: CninfoIssuer, fiscal_years: tuple[int, ...], announcements: list[dict[str, Any]]
) -> list[PlannedDownload]:
    plans: list[PlannedDownload] = []
    for year in fiscal_years:
        matches = [item for item in announcements if _is_primary_cninfo_annual_report(item, issuer, year)]
        if not matches:
            continue
        # Prefer an unqualified original title; API ordering then supplies the newest duplicate.
        item = sorted(matches, key=lambda value: len(str(value.get("announcementTitle", ""))))[0]
        annex = str(item.get("adjunctUrl") or "")
        announcement_id = str(item.get("announcementId") or "")
        published_at = _cninfo_announcement_date(item.get("announcementTime"))
        if not announcement_id or not _safe_cninfo_annex(annex) or published_at is None:
            continue
        plans.append(
            PlannedDownload(
                kind="filing",
                url=f"{CNINFO_STATIC_HOST}/{annex}",
                relative_path=str(Path("cninfo") / issuer.code / str(year) / f"{announcement_id}.pdf"),
                source_id=f"cninfo-{issuer.code}-{announcement_id}",
                document_type="annual_report",
                published_at=published_at,
                reporting_period_end=date(year, 12, 31),
                version=announcement_id,
                source_tier="official_regulatory_disclosure",
                license_label="CNINFO public listed-company disclosure; review CNINFO terms before reuse",
                data_class=DataClass.PUBLIC,
            )
        )
    return plans


def _is_primary_cninfo_annual_report(item: dict[str, Any], issuer: CninfoIssuer, year: int) -> bool:
    title = str(item.get("announcementTitle") or "")
    excluded = ("摘要", "英文", "更正", "修订", "取消")
    return (
        str(item.get("secCode") or "") == issuer.code
        and f"{year}年年度报告" in title
        and not any(word in title for word in excluded)
        and str(item.get("adjunctType") or "").upper() == "PDF"
    )


def _safe_cninfo_annex(value: str) -> bool:
    return bool(re.fullmatch(r"finalpage/\d{4}-\d{2}-\d{2}/\d+\.PDF", value))


def _cninfo_announcement_date(value: Any) -> date | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None


def _cninfo_entity_for_source_id(source_id: str, request: CninfoCollectionRequest) -> str:
    code = source_id.split("-", 2)[1]
    return next(issuer.entity_id for issuer in request.issuers if issuer.code == code)


def _filing_plans(request: SecCollectionRequest, submissions: dict[str, Any]) -> list[PlannedDownload]:
    recent = submissions.get("filings", {}).get("recent", {})
    if not isinstance(recent, dict):
        return []
    plans: list[PlannedDownload] = []
    seen: set[str] = set()
    count = len(recent.get("accessionNumber", []))
    for index in range(count):
        form = str(_at(recent, "form", index) or "").upper()
        accession = str(_at(recent, "accessionNumber", index) or "")
        primary_document = str(_at(recent, "primaryDocument", index) or "")
        filing_date = _date_or_none(_at(recent, "filingDate", index))
        report_date = _date_or_none(_at(recent, "reportDate", index))
        if form not in request.forms or not accession or not primary_document or filing_date is None:
            continue
        if accession in seen or not _safe_sec_filename(primary_document):
            continue
        seen.add(accession)
        accession_flat = accession.replace("-", "")
        path = _entity_relative_root(request) / "filings" / accession / primary_document
        plans.append(
            PlannedDownload(
                kind="filing",
                url=(
                    f"{SEC_ARCHIVES_HOST}/Archives/edgar/data/{int(request.cik)}/"
                    f"{accession_flat}/{primary_document}"
                ),
                relative_path=str(path),
                source_id=f"sec-{request.padded_cik}-{accession}",
                document_type=f"sec_{form.lower().replace('-', '_')}",
                published_at=filing_date,
                reporting_period_end=report_date,
                version=accession,
            )
        )
        if len(plans) >= request.max_filings:
            break
    return plans


def _at(values: dict[str, Any], key: str, index: int) -> Any:
    column = values.get(key, [])
    return column[index] if isinstance(column, list) and index < len(column) else None


def _entity_relative_root(request: SecCollectionRequest) -> Path:
    return Path(f"sec_cik_{request.padded_cik}")


def _safe_destination(root: Path, relative_path: str) -> Path:
    destination = (root / relative_path).resolve()
    if not destination.is_relative_to(root):
        raise CollectionError("refusing a destination path outside the output directory")
    return destination


def _safe_sec_filename(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value))


def _date_or_none(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _quota_exhausted(result: CollectionResult) -> bool:
    return result.existing_bytes + result.downloaded_bytes >= result.quota_bytes
