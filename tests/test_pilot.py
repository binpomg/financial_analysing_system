from __future__ import annotations

import json
from pathlib import Path

from finagent.catalog import load_catalog, merge_catalogs, validate_catalog
from finagent.contracts import DataClass
from finagent.document_parser import PdfExtractedTable, reconstruct_pdf_table_candidates
from finagent.pilot import pilot_readiness, write_label_templates
from finagent.storage import EvidenceStore


def _catalog(path: Path, source_root: Path, sources: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "finagent.pilot-catalog.v1",
                "pilot_id": "pilot-test",
                "created_at": "2026-09-26T00:00:00Z",
                "source_root": str(source_root),
                "sources": sources,
            }
        ),
        encoding="utf-8",
    )


def _source(source_id: str, entity_id: str, year: int, split: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "entity_id": entity_id,
        "document_type": "annual_report",
        "relative_path": f"{source_id}.pdf",
        "published_at": f"{year + 1}-03-31",
        "retrieved_at": f"{year + 1}-04-01T00:00:00Z",
        "source_tier": "test",
        "license_label": "test",
        "data_class": "restricted",
        "reporting_period_end": f"{year}-12-31",
        "dataset_split": split,
    }


def test_pilot_uses_read_only_external_root_and_generates_empty_review_templates(tmp_path: Path) -> None:
    source_root = tmp_path / "approved-read-only-root"
    source_root.mkdir()
    for year in (2022, 2023, 2024):
        (source_root / f"issuer-{year}.pdf").write_bytes(b"%PDF-test")
    catalog = tmp_path / "catalog.json"
    _catalog(catalog, source_root, [_source(f"issuer-{year}", "issuer:1", year, "train") for year in (2022, 2023, 2024)])

    validation = validate_catalog(catalog)
    assert validation["valid"]
    assert validation["split_summary"] == {"train": ["issuer:1"], "dev": [], "test": []}
    readiness = pilot_readiness(catalog)
    assert readiness["ready_for_three_year_financial_analysis"] is True

    created = write_label_templates(catalog, tmp_path / "labels")
    payload = json.loads(created[0].read_text(encoding="utf-8"))
    assert payload["status"] == "pending_human_verification"
    assert payload["observations"] == []
    assert "revenue" in payload["required_fields"]


def test_pilot_rejects_company_family_split_leakage(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    for source_id in ("issuer-2023", "issuer-2024"):
        (source_root / f"{source_id}.pdf").write_bytes(b"%PDF-test")
    catalog = tmp_path / "catalog.json"
    _catalog(
        catalog,
        source_root,
        [
            _source("issuer-2023", "issuer:1", 2023, "train"),
            _source("issuer-2024", "issuer:1", 2024, "test"),
        ],
    )
    validation = validate_catalog(catalog)
    assert not validation["valid"]
    assert "issuer family" in validation["errors"][0]


def test_pdf_reconstruction_switches_to_embedded_period_end_header() -> None:
    candidates, _ = reconstruct_pdf_table_candidates(
        "document-pdf",
        [
            PdfExtractedTable(
                page_number=5,
                table_index=1,
                rows=[
                    ["主要会计数据", "2024年度", "2023年度"],
                    ["营业收入", "170", "147"],
                    [None, "2024年末", "2023年末"],
                    ["资产总额", "298", "272"],
                ],
            )
        ],
    )
    asset = next(item for item in candidates if item.label == "资产总额 | 2024年末")
    assert asset.raw_value == "298"
    assert asset.evidence.locator == "page:5/table:1/row:4/column:2"


def test_content_deduplication_keeps_the_more_restrictive_data_class(tmp_path: Path) -> None:
    source = tmp_path / "shared.pdf"
    source.write_bytes(b"%PDF-shared")
    store = EvidenceStore(tmp_path / "state")
    first = store.put_document(source, data_class=DataClass.RESTRICTED)
    second = store.put_document(source, data_class=DataClass.PUBLIC)
    assert first.id == second.id
    assert second.data_class is DataClass.RESTRICTED


def test_catalog_merge_references_multiple_approved_roots_without_copying(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "issuer-a.pdf").write_bytes(b"%PDF-a")
    (second_root / "issuer-b.pdf").write_bytes(b"%PDF-b")
    first_catalog = tmp_path / "first-catalog.json"
    second_catalog = tmp_path / "second-catalog.json"
    _catalog(first_catalog, first_root, [_source("issuer-a", "issuer:a", 2022, "train")])
    _catalog(second_catalog, second_root, [_source("issuer-b", "issuer:b", 2022, "test")])

    result = merge_catalogs(
        [first_catalog, second_catalog], tmp_path / "merged.json", "merged-pilot"
    )

    assert result["valid"] is True
    assert result["source_count"] == 2
    merged = load_catalog(tmp_path / "merged.json")
    assert Path(merged.source_root or "").resolve() == tmp_path.resolve()
    assert {Path(item.relative_path) for item in merged.sources} == {
        Path("first") / "issuer-a.pdf",
        Path("second") / "issuer-b.pdf",
    }
