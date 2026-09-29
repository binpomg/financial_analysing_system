"""Versioned taxonomy-map loading and XBRL candidate enrichment."""

from __future__ import annotations

import hashlib
from pathlib import Path

from finagent.contracts import XbrlTaxonomyMap
from finagent.document_parser import parse_mapped_xbrl_candidates
from finagent.storage import EvidenceStore


def load_xbrl_taxonomy_map(path: Path) -> XbrlTaxonomyMap:
    return XbrlTaxonomyMap.model_validate_json(path.read_text(encoding="utf-8"))


def map_xbrl_document(
    store: EvidenceStore, document_id: str, taxonomy_map_path: Path
) -> dict[str, object]:
    document = store.get_document(document_id)
    if document is None:
        raise KeyError(f"unknown document: {document_id}")
    if document.media_type.lower() not in {".xml", ".xbrl"}:
        raise ValueError(f"document is not an XML/XBRL source: {document_id}")
    taxonomy_map = load_xbrl_taxonomy_map(taxonomy_map_path)
    raw = taxonomy_map_path.read_bytes()
    mapping_sha256 = hashlib.sha256(raw).hexdigest()
    summary, candidates = parse_mapped_xbrl_candidates(
        store.read_document(document_id), document_id, taxonomy_map
    )
    store.put_candidates(candidates)
    mapping_run_id = hashlib.sha256(
        f"xbrl-map-v1:{document_id}:{mapping_sha256}".encode("utf-8")
    ).hexdigest()
    store.put_taxonomy_mapping_run(
        mapping_run_id,
        document_id,
        {
            "mapping_id": taxonomy_map.mapping_id,
            "taxonomy_version": taxonomy_map.taxonomy_version,
            "mapping_sha256": mapping_sha256,
            "mapping": taxonomy_map.model_dump(mode="json", by_alias=True),
            "candidate_ids": [candidate.id for candidate in candidates],
            "issues": summary.issues,
        },
    )
    return {
        "document_id": document_id,
        "mapping_run_id": mapping_run_id,
        "mapping_id": taxonomy_map.mapping_id,
        "taxonomy_version": taxonomy_map.taxonomy_version,
        "mapping_sha256": mapping_sha256,
        "candidate_count": len(candidates),
        "issues": summary.issues,
    }
