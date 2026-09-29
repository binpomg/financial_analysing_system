"""Pilot-data readiness and human-gold-label preparation utilities."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from finagent.catalog import PilotCatalog, load_catalog


CORE_FINANCIAL_FIELDS = (
    "revenue",
    "operating_cost",
    "parent_net_income",
    "operating_cash_flow",
    "assets",
    "liabilities",
    "parent_equity",
    "basic_eps",
    "weighted_roe",
    "cash_and_cash_equivalents",
    "short_term_borrowings",
    "long_term_interest_bearing_debt",
)


def pilot_readiness(catalog_path: Path) -> dict[str, object]:
    """Report company-family coverage without treating parser output as gold."""
    catalog = load_catalog(catalog_path)
    companies: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in catalog.sources:
        companies[source.entity_id].append(
            {
                "source_id": source.source_id,
                "document_type": source.document_type,
                "reporting_period_end": (
                    source.reporting_period_end.isoformat()
                    if source.reporting_period_end
                    else None
                ),
                "dataset_split": source.dataset_split,
            }
        )
    coverage = []
    for entity_id, items in sorted(companies.items()):
        annual_years = sorted(
            {
                item["reporting_period_end"][:4]
                for item in items
                if item["document_type"] == "annual_report" and item["reporting_period_end"]
            }
        )
        coverage.append(
            {
                "entity_id": entity_id,
                "dataset_split": items[0]["dataset_split"],
                "annual_report_years": annual_years,
                "annual_report_count": len(annual_years),
                "meets_three_year_minimum": len(annual_years) >= 3,
                "sources": items,
            }
        )
    return {
        "schema": "finagent.pilot-readiness.v1",
        "pilot_id": catalog.pilot_id,
        "company_count": len(coverage),
        "companies": coverage,
        "required_core_fields": list(CORE_FINANCIAL_FIELDS),
        "ready_for_three_year_financial_analysis": all(
            item["meets_three_year_minimum"] for item in coverage
        ),
        "note": "Readiness is metadata coverage only. Financial facts require page-level human verification.",
    }


def write_label_templates(catalog_path: Path, output_dir: Path) -> list[Path]:
    """Write field-complete, value-empty review packages for annual reports."""
    catalog = load_catalog(catalog_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for source in catalog.sources:
        if source.document_type != "annual_report":
            continue
        destination = output_dir / f"{source.source_id}.gold-review.json"
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite existing gold-review template: {destination}")
        payload = {
            "schema": "finagent.gold-financial-review.v1",
            "status": "pending_human_verification",
            "source": source.model_dump(mode="json"),
            "required_fields": list(CORE_FINANCIAL_FIELDS),
            "observations": [],
            "instructions": (
                "Add only values visually checked against the original document. Each observation must include "
                "field, raw_value, normalized_value, unit, period_end, period_kind, scope, page_number, "
                "table_name, row_name, column_name, locator, excerpt, reviewer, reviewed_at and review_reason."
            ),
        }
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        created.append(destination)
    return created
