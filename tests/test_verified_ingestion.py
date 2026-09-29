from __future__ import annotations

import json
import base64
from datetime import date
from pathlib import Path

from finagent.catalog import ingest_catalog, validate_catalog
from finagent.contracts import BusinessLine, OcrTextSpan, TaskSpec
from finagent.document_parser import (
    PdfExtractedTable,
    parse_pdf_summary,
    reconstruct_pdf_table_candidates,
)
from finagent.ingest import ingest_path
from finagent.contracts import DataClass
from finagent.review import apply_review_bundle
from finagent.resolution import apply_fact_resolution
from finagent.ocr import OcrEngineOutput, OcrEngineUnavailable, process_ocr_job
from finagent.taxonomy import map_xbrl_document
from finagent.runtime import Runtime


def test_verified_financial_input_keeps_page_evidence_and_publication_date(
    tmp_path: Path,
) -> None:
    source = tmp_path / "verified_financial.json"
    source.write_text(
        json.dumps(
            {
                "schema": "finagent.verified-financial.v1",
                "entity_id": "demo:verified",
                "published_at": "2025-03-28",
                "observations": [
                    {
                        "field": "revenue",
                        "period_end": "2024-12-31",
                        "period_kind": "FY",
                        "value": "125.40",
                        "unit": "CNY_million",
                        "scope": "consolidated",
                        "locator": "page:5/table:main/row:revenue",
                        "excerpt": "Revenue 125.40",
                        "page_number": 5,
                        "block_type": "table_cell",
                        "verification_status": "human_verified",
                        "confidence": "high",
                        "confidence_reason": "reviewer matched the original annual-report table cell",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime = Runtime(tmp_path / "state")
    run = runtime.run(
        TaskSpec(
            line=BusinessLine.FINANCIAL_REPORT,
            sources=[str(source)],
            entity_id="demo:verified",
            as_of=date(2025, 3, 31),
        )
    )

    document = runtime.store.get_document(run.source_ids[0])
    assert document is not None
    assert document.published_at == date(2025, 3, 28)
    fact = runtime.store.facts_for("demo:verified", "2025-03-31", run.source_ids)[0]
    assert fact.evidence.page_number == 5
    assert fact.evidence.locator == "page:5/table:main/row:revenue"
    assert fact.verification_status == "human_verified"
    assert fact.confidence_reason.startswith("reviewer matched")


def test_non_pdf_payload_is_rejected_before_text_extraction() -> None:
    summary = parse_pdf_summary(b"not a PDF")
    assert summary.status == "invalid_document"
    assert summary.text_characters == 0


def test_pdf_table_reconstruction_inherits_prior_page_header_unit_and_scope() -> None:
    candidates, issues = reconstruct_pdf_table_candidates(
        "document-pdf",
        [
            PdfExtractedTable(
                page_number=5,
                table_index=1,
                rows=[
                    ["\u5408\u5e76\u5229\u6da6\u8868 (\u5355\u4f4d:\u4e07\u5143)", None, None],
                    ["\u9879\u76ee", "2024\u5e74\u5ea6", "2023\u5e74\u5ea6"],
                    ["\u8425\u4e1a\u6536\u5165", "1,250.50", "1,100.00"],
                ],
            ),
            PdfExtractedTable(
                page_number=6,
                table_index=1,
                rows=[
                    ["\u7eed\u8868", None, None],
                    ["\u8425\u4e1a\u6210\u672c", "700.00", "650.00"],
                ],
            ),
        ],
    )
    assert len(candidates) == 4
    continued = next(
        item
        for item in candidates
        if item.label == "\u8425\u4e1a\u6210\u672c | 2024\u5e74\u5ea6"
    )
    assert continued.raw_value == "700.00"
    assert continued.unit_hint == "\u4e07\u5143"
    assert continued.scope_hint == "consolidated"
    assert continued.period_hint == "2024\u5e74\u5ea6"
    assert continued.evidence.locator == (
        "page:6/table:1/row:2/column:2/header:page:5/table:1/row:2"
    )
    assert continued.evidence.block_type == "pdf_reconstructed_table_cell"
    assert "inherited headers" in " ".join(issues)


def test_pdf_table_without_header_or_continuation_produces_no_candidates() -> None:
    candidates, issues = reconstruct_pdf_table_candidates(
        "document-pdf",
        [
            PdfExtractedTable(
                page_number=5,
                table_index=1,
                rows=[
                    ["\u8425\u4e1a\u6536\u5165", "1,250.50"],
                    ["\u8425\u4e1a\u6210\u672c", "700.00"],
                ],
            )
        ],
    )
    assert candidates == []
    assert "no usable header" in " ".join(issues)


def test_xml_values_are_saved_as_candidates_not_financial_facts(tmp_path: Path) -> None:
    source = tmp_path / "report.xml"
    source.write_text(
        '<financial entity="demo"><Revenue context="consolidated">1200</Revenue>'
        '<NetIncome context="parent">120</NetIncome><Note>text</Note></financial>',
        encoding="utf-8",
    )
    runtime = Runtime(tmp_path / "state")
    result = ingest_path(
        runtime.store,
        source,
        DataClass.RESTRICTED,
        published_at_override="2025-03-28",
    )

    assert result.fact_count == 0
    assert result.candidate_count == 2
    candidates = runtime.store.candidates_for_document(result.document.id)
    assert {candidate.label for candidate in candidates} == {"Revenue", "NetIncome"}
    assert all(candidate.status == "candidate" for candidate in candidates)

    revenue = next(candidate for candidate in candidates if candidate.label == "Revenue")
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "schema": "finagent.candidate-review.v1",
                "review_id": "review-001",
                "approvals": [
                    {
                        "candidate_id": revenue.id,
                        "entity_id": "demo:reviewed",
                        "field": "revenue",
                        "normalized_value": "1200",
                        "unit": "CNY_million",
                        "period_end": "2024-12-31",
                        "period_kind": "FY",
                        "scope": "consolidated",
                        "reviewer": "test-reviewer",
                        "reviewed_at": "2025-03-29T00:00:00Z",
                        "review_reason": "matched the source XML node to the review taxonomy",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    applied = apply_review_bundle(runtime.store, review)
    assert applied["approved_count"] == 1
    fact = runtime.store.facts_for("demo:reviewed", "2025-03-30", [result.document.id])[0]
    assert fact.value == 1200
    assert fact.verification_status == "human_verified"
    assert fact.evidence.locator == revenue.evidence.locator


def test_xbrl_taxonomy_map_enriches_only_context_and_unit_valid_candidates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.xbrl"
    source.write_text(
        """<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:gaap="https://example.test/taxonomy/cn-gaap"
            xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
          <xbrli:context id="FY2024">
            <xbrli:entity><xbrli:identifier scheme="issuer">demo:xbrl</xbrli:identifier></xbrli:entity>
            <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period>
          </xbrli:context>
          <xbrli:unit id="CNY"><xbrli:measure>iso4217:CNY</xbrli:measure></xbrli:unit>
          <gaap:Revenue contextRef="FY2024" unitRef="CNY">1200</gaap:Revenue>
        </xbrli:xbrl>""",
        encoding="utf-8",
    )
    mapping = tmp_path / "taxonomy-map.json"
    mapping.write_text(
        json.dumps(
            {
                "schema": "finagent.xbrl-taxonomy-map.v1",
                "mapping_id": "cn-gaap-core",
                "taxonomy_version": "2026.1",
                "entries": [
                    {
                        "namespace_uri": "https://example.test/taxonomy/cn-gaap",
                        "local_name": "Revenue",
                        "field": "revenue",
                        "unit": "iso4217:CNY",
                        "scope": "consolidated",
                        "period_type": "duration",
                        "period_kind_hint": "FY",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime = Runtime(tmp_path / "state")
    ingested = ingest_path(
        runtime.store, source, DataClass.RESTRICTED, published_at_override="2025-03-28"
    )
    mapped = map_xbrl_document(runtime.store, ingested.document.id, mapping)
    assert mapped["candidate_count"] == 1
    mapping_run = runtime.store.taxonomy_mapping_runs_for(ingested.document.id)[0]
    assert mapping_run["id"] == mapped["mapping_run_id"]
    assert mapping_run["data"]["mapping"]["schema"] == "finagent.xbrl-taxonomy-map.v1"
    candidates = runtime.store.candidates_for_document(ingested.document.id)
    candidate = next(item for item in candidates if item.field_hint == "revenue")
    assert candidate.unit_hint == "iso4217:CNY"
    assert candidate.period_hint == "2024-01-01/2024-12-31"
    assert candidate.period_kind_hint == "FY"
    assert candidate.scope_hint == "consolidated"
    assert candidate.evidence.locator.startswith("xbrl-map:cn-gaap-core:2026.1/")
    assert runtime.store.facts_for("demo:xbrl", "2025-03-31", [ingested.document.id]) == []

    rejected_mapping = tmp_path / "taxonomy-map-invalid-unit.json"
    rejected_mapping.write_text(
        mapping.read_text(encoding="utf-8").replace("iso4217:CNY", "iso4217:USD"),
        encoding="utf-8",
    )
    rejected = map_xbrl_document(runtime.store, ingested.document.id, rejected_mapping)
    assert rejected["candidate_count"] == 0
    assert "failed context, period-type or unit validation" in rejected["issues"][-1]


def test_raster_source_is_queued_for_ocr_without_transcription(tmp_path: Path) -> None:
    source = tmp_path / "scan.png"
    source.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
            "qSfh0QAAAABJRU5ErkJggg=="
        )
    )
    runtime = Runtime(tmp_path / "state")
    result = ingest_path(runtime.store, source, DataClass.RESTRICTED)

    assert result.fact_count == 0
    assert result.candidate_count == 0
    assert len(result.ocr_job_ids) == 1
    job = runtime.store.ocr_jobs()[0]
    assert job.status == "queued"
    assert job.document_id == result.document.id


def test_ocr_execution_creates_review_candidates_but_never_facts(tmp_path: Path) -> None:
    source = tmp_path / "scan.png"
    source.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
            "qSfh0QAAAABJRU5ErkJggg=="
        )
    )
    runtime = Runtime(tmp_path / "state")
    ingested = ingest_path(runtime.store, source, DataClass.RESTRICTED)

    class StubOcrEngine:
        def transcribe(self, raw: bytes, language: str) -> OcrEngineOutput:
            assert raw
            assert language == "chi_sim+eng"
            return OcrEngineOutput(
                engine="test-ocr",
                engine_version="1.0",
                spans=[
                    OcrTextSpan(
                        text="营业收入 1,250.50",
                        confidence=87.5,
                        bounding_box=(10, 20, 120, 15),
                    )
                ],
            )

    processed = process_ocr_job(
        runtime.store, ingested.ocr_job_ids[0], engine=StubOcrEngine()
    )
    assert processed["status"] == "completed"
    assert processed["candidate_count"] == 1
    transcript = runtime.store.ocr_transcripts_for_job(ingested.ocr_job_ids[0])[0]
    assert transcript.engine == "test-ocr"
    assert transcript.text == "营业收入 1,250.50"
    candidate = runtime.store.candidates_for_document(ingested.document.id)[0]
    assert candidate.raw_value == "1,250.50"
    assert candidate.evidence.locator.startswith(f"ocr:{transcript.id}/")
    assert candidate.status == "candidate"
    assert runtime.store.facts_for("demo:ocr", "9999-12-31", [ingested.document.id]) == []


def test_ocr_failure_is_recorded_and_can_be_requeued(tmp_path: Path) -> None:
    source = tmp_path / "failed-scan.png"
    source.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
            "qSfh0QAAAABJRU5ErkJggg=="
        )
    )
    runtime = Runtime(tmp_path / "state")
    ingested = ingest_path(runtime.store, source, DataClass.RESTRICTED)

    class FailingOcrEngine:
        def transcribe(self, raw: bytes, language: str) -> OcrEngineOutput:
            raise OcrEngineUnavailable("test OCR engine unavailable")

    result = process_ocr_job(
        runtime.store, ingested.ocr_job_ids[0], engine=FailingOcrEngine()
    )
    assert result["status"] == "failed"
    failed = runtime.store.ocr_job_by_id(ingested.ocr_job_ids[0])
    assert failed is not None
    assert failed.error == "OcrEngineUnavailable: test OCR engine unavailable"
    assert failed.attempt_count == 1
    assert runtime.store.requeue_ocr_job(failed.id).status == "queued"


def test_conflicting_verified_facts_block_generic_financial_calculation(
    tmp_path: Path,
) -> None:
    sources = []
    for number, value in enumerate(("100", "120"), start=1):
        source = tmp_path / f"report-{number}.json"
        source.write_text(
            json.dumps(
                {
                    "schema": "finagent.verified-financial.v1",
                    "entity_id": "demo:conflict",
                    "published_at": f"2025-03-{27 + number}",
                    "observations": [
                        {
                            "field": "revenue",
                            "period_end": "2024-12-31",
                            "period_kind": "FY",
                            "value": value,
                            "unit": "CNY_million",
                            "scope": "consolidated",
                            "locator": f"page:5/cell:{number}",
                            "excerpt": f"Revenue {value}",
                            "page_number": 5,
                            "verification_status": "human_verified",
                            "confidence_reason": "reviewed source value",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        sources.append(str(source))
    runtime = Runtime(tmp_path / "state")
    run = runtime.run(
        TaskSpec(
            line=BusinessLine.FINANCIAL_REPORT,
            sources=sources,
            entity_id="demo:conflict",
            as_of=date(2025, 3, 31),
        )
    )
    result = next(
        item["payload"] for item in runtime.store.artifacts_for(run.id) if item["kind"] == "result"
    )
    assert run.status.value == "blocked"
    assert result["payload"]["fact_conflicts"][0]["field"] == "revenue"


def test_fact_resolution_unblocks_calculation_with_the_selected_fact(
    tmp_path: Path,
) -> None:
    sources = []
    for number, value in enumerate(("100", "120"), start=1):
        source = tmp_path / f"restated-report-{number}.json"
        source.write_text(
            json.dumps(
                {
                    "schema": "finagent.verified-financial.v1",
                    "entity_id": "demo:restatement",
                    "published_at": f"2025-03-{27 + number}",
                    "observations": [
                        {
                            "field": "revenue",
                            "period_end": "2024-12-31",
                            "period_kind": "FY",
                            "value": value,
                            "unit": "CNY_million",
                            "scope": "consolidated",
                            "locator": f"page:5/cell:{number}",
                            "excerpt": f"Revenue {value}",
                            "page_number": 5,
                            "verification_status": "human_verified",
                            "confidence_reason": "reviewed source value",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        sources.append(str(source))

    runtime = Runtime(tmp_path / "state")
    blocked = runtime.run(
        TaskSpec(
            line=BusinessLine.FINANCIAL_REPORT,
            sources=sources,
            entity_id="demo:restatement",
            as_of=date(2025, 3, 31),
        )
    )
    assert blocked.status.value == "blocked"
    facts = runtime.store.facts_for(
        "demo:restatement", "2025-03-31", blocked.source_ids
    )
    preferred = next(fact for fact in facts if fact.value == 100)
    resolution = tmp_path / "resolution.json"
    resolution.write_text(
        json.dumps(
            {
                "schema": "finagent.fact-resolution.v1",
                "resolution_id": "resolution-001",
                "entity_id": "demo:restatement",
                "field": "revenue",
                "period_end": "2024-12-31",
                "period_kind": "FY",
                "scope": "consolidated",
                "unit": "CNY_million",
                "preferred_fact_id": preferred.id,
                "reviewer": "test-reviewer",
                "resolved_at": "2025-03-31T00:00:00Z",
                "reason": "The first filing is the applicable version for this analysis.",
            }
        ),
        encoding="utf-8",
    )
    applied = apply_fact_resolution(runtime.store, resolution)
    assert applied["preferred_fact_id"] == preferred.id

    resolved = runtime.run(
        TaskSpec(
            line=BusinessLine.FINANCIAL_REPORT,
            sources=sources,
            entity_id="demo:restatement",
            as_of=date(2025, 3, 31),
        )
    )
    result = next(
        item["payload"] for item in runtime.store.artifacts_for(resolved.id) if item["kind"] == "result"
    )
    assert resolved.status.value != "blocked"
    assert result["payload"]["summary"].endswith("reported revenue 100 CNY_million.")
    assert result["payload"]["source_fact_ids"] == [preferred.id]
    assert result["payload"]["fact_resolution_ids"] == ["resolution-001"]
    assert len(runtime.store.fact_resolutions_for("demo:restatement")) == 1


def test_future_fact_resolution_does_not_change_historical_run(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path / "state")
    source_ids = []
    for number, value in enumerate(("100", "120"), start=1):
        source = tmp_path / f"future-resolution-{number}.json"
        source.write_text(
            json.dumps(
                {
                    "schema": "finagent.verified-financial.v1",
                    "entity_id": "demo:future-resolution",
                    "published_at": "2025-03-28",
                    "observations": [{
                        "field": "revenue", "period_end": "2024-12-31", "period_kind": "FY",
                        "value": value, "unit": "CNY_million", "scope": "consolidated",
                        "locator": f"page:5/cell:{number}", "excerpt": f"Revenue {value}",
                        "page_number": 5, "verification_status": "human_verified",
                        "confidence_reason": "reviewed source value",
                    }],
                }
            ),
            encoding="utf-8",
        )
        source_ids.append(str(source))
    initial = runtime.run(
        TaskSpec(
            line=BusinessLine.FINANCIAL_REPORT, sources=source_ids,
            entity_id="demo:future-resolution", as_of=date(2025, 3, 31),
        )
    )
    preferred = runtime.store.facts_for(
        "demo:future-resolution", "2025-03-31", initial.source_ids
    )[0]
    resolution = tmp_path / "future-resolution.json"
    resolution.write_text(json.dumps({
        "schema": "finagent.fact-resolution.v1", "resolution_id": "resolution-future",
        "entity_id": "demo:future-resolution", "field": "revenue",
        "period_end": "2024-12-31", "period_kind": "FY", "scope": "consolidated",
        "unit": "CNY_million", "preferred_fact_id": preferred.id,
        "reviewer": "test-reviewer", "resolved_at": "2025-04-01T00:00:00Z",
        "reason": "This was decided after the historical analysis date.",
    }), encoding="utf-8")
    apply_fact_resolution(runtime.store, resolution)
    historical = runtime.run(
        TaskSpec(
            line=BusinessLine.FINANCIAL_REPORT, sources=source_ids,
            entity_id="demo:future-resolution", as_of=date(2025, 3, 31),
        )
    )
    assert historical.status.value == "blocked"


def test_pilot_catalog_preserves_source_metadata_and_ingests_verified_facts(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "report.json").write_text(
        json.dumps(
            {
                "schema": "finagent.verified-financial.v1",
                "entity_id": "demo:catalog",
                "published_at": "2025-03-28",
                "observations": [
                    {
                        "field": "revenue",
                        "period_end": "2024-12-31",
                        "period_kind": "FY",
                        "value": "10",
                        "unit": "CNY_million",
                        "scope": "consolidated",
                        "locator": "page:5/cell:revenue",
                        "excerpt": "Revenue 10",
                        "page_number": 5,
                        "verification_status": "human_verified",
                        "confidence_reason": "reviewed against original",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema": "finagent.pilot-catalog.v1",
                "pilot_id": "pilot-001",
                "created_at": "2026-09-26T00:00:00Z",
                "sources": [
                    {
                        "source_id": "demo-2024-report",
                        "entity_id": "demo:catalog",
                        "document_type": "annual_report",
                        "relative_path": "data/report.json",
                        "published_at": "2025-03-28",
                        "retrieved_at": "2025-03-29T00:00:00Z",
                        "source_url": "https://example.test/report",
                        "source_tier": "official_disclosure_platform",
                        "license_label": "internal_test",
                        "data_class": "restricted",
                        "reporting_period_end": "2024-12-31",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    validation = validate_catalog(catalog_path)
    assert validation["valid"] is True
    assert validation["sources"][0]["sha256"]
    runtime = Runtime(tmp_path / "state")
    ingested = ingest_catalog(runtime.store, catalog_path)
    assert ingested["ingested"][0]["fact_count"] == 1
    registration = runtime.store.source_registration(
        ingested["ingested"][0]["registration_id"]
    )
    assert registration is not None
    assert registration["data"]["source"]["license_label"] == "internal_test"
    document = runtime.store.get_document(ingested["ingested"][0]["document_id"])
    assert document is not None
    assert document.source_url == "https://example.test/report"
    assert document.source_tier == "official_disclosure_platform"
