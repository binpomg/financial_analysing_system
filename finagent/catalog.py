"""Manifest-driven onboarding for a small, auditable production-data pilot."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from finagent.contracts import DataClass, StrictModel
from finagent.ingest import ingest_path
from finagent.storage import EvidenceStore


class CatalogSource(StrictModel):
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    entity_id: str = Field(min_length=1)
    document_type: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    published_at: date
    retrieved_at: datetime
    source_url: str | None = None
    source_tier: str = Field(min_length=1)
    license_label: str = Field(min_length=1)
    data_class: DataClass = DataClass.RESTRICTED
    version: str = Field(default="1", min_length=1)
    reporting_period_end: date | None = None
    dataset_split: Literal["train", "dev", "test"] | None = None


class PilotCatalog(StrictModel):
    schema_id: str = Field(alias="schema", pattern=r"^finagent.pilot-catalog.v1$")
    pilot_id: str = Field(min_length=1)
    created_at: datetime
    source_root: str | None = None
    sources: list[CatalogSource] = Field(min_length=1)

    @model_validator(mode="after")
    def source_ids_are_unique(self) -> "PilotCatalog":
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_id values must be unique")
        return self


def load_catalog(path: Path) -> PilotCatalog:
    return PilotCatalog.model_validate_json(path.read_text(encoding="utf-8"))


def resolve_source_root(catalog_path: Path, catalog: PilotCatalog) -> Path:
    configured = Path(catalog.source_root) if catalog.source_root else catalog_path.parent
    root = configured.resolve() if configured.is_absolute() else (catalog_path.parent / configured).resolve()
    if not root.is_dir():
        raise ValueError(f"catalog source_root is not a directory: {root}")
    return root


def resolve_source_path(catalog_path: Path, source: CatalogSource, root: Path | None = None) -> Path:
    root = (root or catalog_path.parent.resolve()).resolve()
    candidate = (root / source.relative_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"{source.source_id}: relative_path escapes the catalog directory")
    return candidate


def validate_catalog(catalog_path: Path) -> dict[str, object]:
    catalog = load_catalog(catalog_path)
    root = resolve_source_root(catalog_path, catalog)
    entries = []
    errors = []
    entity_splits: dict[str, set[str | None]] = {}
    for source in catalog.sources:
        path = resolve_source_path(catalog_path, source, root)
        exists = path.is_file()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if exists else None
        entries.append(
            {
                "source_id": source.source_id,
                "entity_id": source.entity_id,
                "document_type": source.document_type,
                "published_at": source.published_at.isoformat(),
                "reporting_period_end": (
                    source.reporting_period_end.isoformat()
                    if source.reporting_period_end
                    else None
                ),
                "path_exists": exists,
                "sha256": digest,
                "data_class": source.data_class.value,
                "license_label": source.license_label,
                "dataset_split": source.dataset_split,
            }
        )
        entity_splits.setdefault(source.entity_id, set()).add(source.dataset_split)
        if not exists:
            errors.append(f"{source.source_id}: source file is missing")
    for entity_id, splits in entity_splits.items():
        if len(splits) > 1:
            errors.append(
                f"{entity_id}: all documents in an issuer family must use one dataset_split"
            )
    split_summary = {
        split: sorted(
            entity_id for entity_id, splits in entity_splits.items() if splits == {split}
        )
        for split in ("train", "dev", "test")
    }
    return {
        "catalog_id": catalog.pilot_id,
        "source_count": len(catalog.sources),
        "valid": not errors,
        "errors": errors,
        "sources": entries,
        "source_root": str(root),
        "split_summary": split_summary,
    }


def ingest_catalog(store: EvidenceStore, catalog_path: Path) -> dict[str, object]:
    catalog = load_catalog(catalog_path)
    validation = validate_catalog(catalog_path)
    if not validation["valid"]:
        return validation | {"ingested": []}
    ingested = []
    root = resolve_source_root(catalog_path, catalog)
    for source in catalog.sources:
        result = ingest_path(
            store,
            resolve_source_path(catalog_path, source, root),
            source.data_class,
            published_at_override=source.published_at.isoformat(),
            source_url=source.source_url,
            source_tier=source.source_tier,
            retrieved_at=source.retrieved_at.isoformat(),
            version=source.version,
        )
        registration_id = f"{catalog.pilot_id}:{source.source_id}"
        store.put_source_registration(
            registration_id,
            result.document.id,
            {
                "catalog_id": catalog.pilot_id,
                "source": source.model_dump(mode="json"),
                "content_sha256": result.document.sha256,
            },
        )
        ingested.append(
            {
                "source_id": source.source_id,
                "registration_id": registration_id,
                "document_id": result.document.id,
                "fact_count": result.fact_count,
                "issues": result.issues,
                "parse_summary": (
                    result.parse_summary.model_dump()
                    if result.parse_summary is not None
                    else None
                ),
            }
        )
    return validation | {"ingested": ingested}


def merge_catalogs(catalog_paths: list[Path], output_path: Path, pilot_id: str) -> dict[str, object]:
    """Write one catalog that references sources kept under different approved roots.

    The merged catalog uses the closest common ancestor as ``source_root``.  Source
    bytes are never copied, which keeps a pilot's provenance and quotas intact.
    """
    if len(catalog_paths) < 2:
        raise ValueError("at least two catalogs are required for a merge")
    loaded = [(path, load_catalog(path), None) for path in catalog_paths]
    source_paths: list[tuple[CatalogSource, Path]] = []
    source_ids: set[str] = set()
    for path, catalog, _ in loaded:
        root = resolve_source_root(path, catalog)
        for source in catalog.sources:
            if source.source_id in source_ids:
                raise ValueError(f"duplicate source_id across catalogs: {source.source_id}")
            source_ids.add(source.source_id)
            source_paths.append((source, resolve_source_path(path, source, root)))
    common_root = Path(os.path.commonpath([str(path.parent) for _, path in source_paths])).resolve()
    merged_sources = []
    for source, source_path in source_paths:
        entry = source.model_dump(mode="json")
        entry["relative_path"] = str(source_path.relative_to(common_root))
        merged_sources.append(entry)
    merged = {
        "schema": "finagent.pilot-catalog.v1",
        "pilot_id": pilot_id,
        "created_at": datetime.now().astimezone().isoformat(),
        "source_root": str(common_root),
        "sources": merged_sources,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return validate_catalog(output_path)
