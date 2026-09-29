"""Mandatory data preparation for every business line."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from finagent.contracts import (
    DataClass,
    EvidenceSpan,
    Fact,
    FinancialInput,
    SourceDocument,
    VerifiedFinancialInput,
)
from finagent.document_parser import (
    DocumentParseSummary,
    parse_pdf_candidates,
    parse_pdf_summary,
    parse_image_for_ocr,
    parse_xbrl_candidates,
    parse_xlsx_candidates,
)
from finagent.storage import EvidenceStore


class IngestResult:
    def __init__(
        self,
        document: SourceDocument,
        fact_count: int,
        issues: list[str],
        parse_summary: DocumentParseSummary | None = None,
        candidate_count: int = 0,
        ocr_job_ids: list[str] | None = None,
    ) -> None:
        self.document = document
        self.fact_count = fact_count
        self.issues = issues
        self.parse_summary = parse_summary
        self.candidate_count = candidate_count
        self.ocr_job_ids = ocr_job_ids or []


def ingest_path(
    store: EvidenceStore,
    path: Path,
    data_class: DataClass,
    *,
    published_at_override: str | None = None,
    source_url: str | None = None,
    source_tier: str | None = None,
    retrieved_at: str | None = None,
    version: str = "1",
    copy_source: bool = True,
    accept_structured_facts: bool = True,
) -> IngestResult:
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = None
    if path.suffix.lower() == ".json":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    declared_published_at = (
        raw.get("published_at")
        if isinstance(raw, dict) and raw.get("schema") in {
            "finagent.financial.v1",
            "finagent.verified-financial.v1",
        }
        else None
    )
    document = store.put_document(
        path,
        published_at=published_at_override or declared_published_at,
        data_class=data_class,
        source_url=source_url,
        source_tier=source_tier,
        retrieved_at=retrieved_at,
        version=version,
        copy_source=copy_source,
    )
    if path.suffix.lower() == ".pdf":
        parse_summary, candidates = parse_pdf_candidates(path.read_bytes(), document.id)
        store.put_candidates(candidates)
        return IngestResult(
            document,
            0,
            parse_summary.issues,
            parse_summary,
            candidate_count=len(candidates),
        )
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        parse_summary = parse_image_for_ocr(path.read_bytes())
        if parse_summary.status != "ocr_required":
            return IngestResult(document, 0, parse_summary.issues, parse_summary)
        width, height = _dimensions_from_issue(parse_summary.issues)
        job = store.queue_ocr_job(
            document.id,
            "raster_source_requires_ocr",
            width=width,
            height=height,
        )
        return IngestResult(
            document,
            0,
            parse_summary.issues,
            parse_summary,
            ocr_job_ids=[job.id],
        )
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        parse_summary, candidates = parse_xlsx_candidates(path.read_bytes(), document.id)
        store.put_candidates(candidates)
        return IngestResult(
            document,
            0,
            parse_summary.issues,
            parse_summary,
            candidate_count=len(candidates),
        )
    if path.suffix.lower() in {".xml", ".xbrl"}:
        parse_summary, candidates = parse_xbrl_candidates(path.read_bytes(), document.id)
        store.put_candidates(candidates)
        return IngestResult(
            document,
            0,
            parse_summary.issues,
            parse_summary,
            candidate_count=len(candidates),
        )
    if path.suffix.lower() != ".json":
        return IngestResult(
            document, 0, ["source registered; structured parser is not configured for this format"]
        )
    if raw is None:
        return IngestResult(document, 0, ["invalid JSON; source registered without facts"])
    if not isinstance(raw, dict) or raw.get("schema") not in {
        "finagent.financial.v1",
        "finagent.verified-financial.v1",
    }:
        return IngestResult(document, 0, ["unknown JSON schema; source registered without facts"])
    if raw["schema"] == "finagent.verified-financial.v1":
        if not accept_structured_facts:
            return IngestResult(document, 0, ["structured fields await explicit review/import"])
        return _ingest_verified_financial(store, document, raw)
    if not accept_structured_facts:
        return IngestResult(document, 0, ["structured fields await explicit review/import"])
    financial = FinancialInput.model_validate(raw)
    facts: list[Fact] = []
    for index, observation in enumerate(financial.observations):
        locator = f"/observations/{index}"
        raw_observation = raw["observations"][index]
        fact_id = hashlib.sha256(
            f"{document.id}:{locator}".encode("utf-8")
        ).hexdigest()
        facts.append(
            Fact(
                id=fact_id,
                document_id=document.id,
                entity_id=financial.entity_id,
                field=observation.field,
                value=observation.value,
                unit=observation.unit,
                period_end=observation.period_end,
                period_kind=observation.period_kind,
                scope=observation.scope,
                published_at=financial.published_at,
                evidence=EvidenceSpan(
                    document_id=document.id,
                    locator=locator,
                    excerpt=json.dumps(raw_observation, ensure_ascii=False),
                ),
            )
        )
    store.put_facts(facts)
    return IngestResult(document, len(facts), [])


def _dimensions_from_issue(issues: list[str]) -> tuple[int | None, int | None]:
    """Dimensions are informational only; OCR jobs do not depend on this parsing."""
    import re

    match = next((re.search(r"(\d+)x(\d+)", issue) for issue in issues if "x" in issue), None)
    if match is None:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _ingest_verified_financial(
    store: EvidenceStore, document: SourceDocument, raw: dict[str, object]
) -> IngestResult:
    financial = VerifiedFinancialInput.model_validate(raw)
    facts: list[Fact] = []
    for index, observation in enumerate(financial.observations):
        fact_id = hashlib.sha256(
            f"{document.id}:{observation.locator}:{observation.field}:{observation.period_end}".encode("utf-8")
        ).hexdigest()
        facts.append(
            Fact(
                id=fact_id,
                document_id=document.id,
                entity_id=financial.entity_id,
                field=observation.field,
                value=observation.value,
                unit=observation.unit,
                period_end=observation.period_end,
                period_kind=observation.period_kind,
                scope=observation.scope,
                published_at=financial.published_at,
                evidence=EvidenceSpan(
                    document_id=document.id,
                    locator=observation.locator,
                    excerpt=observation.excerpt,
                    page_number=observation.page_number,
                    block_type=observation.block_type,
                    verification_status=observation.verification_status,
                ),
                raw_value=str(raw["observations"][index]),
                confidence=observation.confidence,
                confidence_reason=observation.confidence_reason,
                verification_status=observation.verification_status,
            )
        )
    store.put_facts(facts)
    return IngestResult(document, len(facts), [])
