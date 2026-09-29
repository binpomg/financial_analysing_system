"""Apply auditable reviewer decisions to verified-fact conflicts."""

from __future__ import annotations

from pathlib import Path

from finagent.contracts import FactResolution
from finagent.storage import EvidenceStore


def load_fact_resolution(path: Path) -> FactResolution:
    return FactResolution.model_validate_json(path.read_text(encoding="utf-8"))


def apply_fact_resolution(store: EvidenceStore, path: Path) -> dict[str, object]:
    resolution = load_fact_resolution(path)
    store.put_fact_resolution(resolution)
    return {
        "resolution_id": resolution.resolution_id,
        "entity_id": resolution.entity_id,
        "preferred_fact_id": resolution.preferred_fact_id,
        "resolved_at": resolution.resolved_at.isoformat(),
        "reason": resolution.reason,
    }
