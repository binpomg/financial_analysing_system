"""Read-only adapter for the delivered financial research sample corpus.

The corpus remains the source of truth for its raw documents, task bindings and
reference data. Runtime copies only task-selected source bytes into its content
addressed evidence store, then records the source corpus IDs in the run manifest.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from finagent.contracts import (
    BusinessLine,
    DataClass,
    EvidenceSpan,
    Fact,
    FactNature,
    SourceDocument,
    TaskSpec,
)
from finagent.storage import EvidenceStore


AVAILABLE = "available_but_requires_task_specific_verification"


@dataclass(frozen=True)
class CorpusTask:
    task_id: str
    line: BusinessLine
    title: str
    as_of: date
    prompt: str
    expected_output_fields: list[str]
    network_policy: str
    is_synthetic: bool
    entity_id: str | None


@dataclass(frozen=True)
class PreparedCorpusTask:
    task: CorpusTask
    documents: list[SourceDocument]
    issues: list[str]
    availability: list[dict[str, str | None]]


class SampleCorpus:
    """Access the delivery package without modifying its SQLite databases."""

    def __init__(self, root: Path) -> None:
        requested = root.resolve()
        candidate = requested / "金融投研样例.sqlite"
        if not candidate.is_file():
            candidate = requested / "样例数据库" / "金融投研样例.sqlite"
        if not candidate.is_file():
            raise FileNotFoundError(
                "sample corpus must contain 金融投研样例.sqlite directly or under 样例数据库"
            )
        self.root = candidate.parent
        self.database = candidate
        self.reference_database = self.root / "evaluation" / "评测参考.sqlite"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.database.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def task(self, task_id: str) -> CorpusTask:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT task_id, line_id, title, as_of_date, prompt, "
                "expected_output_schema, network_policy, is_synthetic "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown sample task: {task_id}")
            entity = connection.execute(
                "SELECT f.entity_id FROM facts f JOIN task_inputs i ON i.doc_id = f.doc_id "
                "WHERE i.task_id = ? ORDER BY f.entity_id LIMIT 1",
                (task_id,),
            ).fetchone()
        schema = json.loads(row["expected_output_schema"])
        return CorpusTask(
            task_id=row["task_id"],
            line=BusinessLine(row["line_id"]),
            title=row["title"],
            as_of=date.fromisoformat(row["as_of_date"]),
            prompt=row["prompt"],
            expected_output_fields=list(schema.get("required", [])),
            network_policy=row["network_policy"],
            is_synthetic=bool(row["is_synthetic"]),
            entity_id=entity["entity_id"] if entity else None,
        )

    def tasks(self) -> list[CorpusTask]:
        with self._connect() as connection:
            identifiers = [row["task_id"] for row in connection.execute(
                "SELECT task_id FROM tasks ORDER BY line_id, task_id"
            )]
        return [self.task(identifier) for identifier in identifiers]

    def task_spec(self, task_id: str) -> TaskSpec:
        task = self.task(task_id)
        return TaskSpec(
            line=task.line,
            sources=[f"corpus:{task.task_id}"],
            entity_id=task.entity_id,
            as_of=task.as_of,
            question=task.prompt,
            max_model_calls=0,
            source_data_class=DataClass.PUBLIC,
            corpus_task_id=task.task_id,
            expected_output_fields=task.expected_output_fields,
            network_policy=task.network_policy,
        )

    def prepare(self, store: EvidenceStore, task_id: str) -> PreparedCorpusTask:
        task = self.task(task_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT v.doc_id, v.availability, d.relative_path, d.published_at, "
                "d.source_url, d.source_tier, d.retrieved_at, d.version, d.is_synthetic, "
                "s.parser, s.parse_status, s.downstream_eligible "
                "FROM v_available_task_inputs v JOIN documents d ON d.doc_id = v.doc_id "
                "JOIN structured_documents s ON s.doc_id = d.doc_id "
                "WHERE v.task_id = ? ORDER BY v.doc_id",
                (task_id,),
            ).fetchall()
        documents: list[SourceDocument] = []
        issues: list[str] = []
        availability: list[dict[str, str | None]] = []
        source_doc_ids: list[str] = []
        for row in rows:
            status = row["availability"]
            availability.append({
                "doc_id": row["doc_id"],
                "availability": status,
                "parser": row["parser"],
                "parse_status": row["parse_status"],
                "downstream_eligible": str(row["downstream_eligible"]),
            })
            inspect_for_document_flow = task.line is BusinessLine.DOCUMENTS
            if status != AVAILABLE and not inspect_for_document_flow:
                issues.append(f"{row['doc_id']} excluded by corpus availability: {status}")
                continue
            if status != AVAILABLE:
                issues.append(
                    f"{row['doc_id']} retained for document processing only; "
                    f"downstream availability is {status}"
                )
            path = (self.root / row["relative_path"]).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                issues.append(f"{row['doc_id']} raw source is unavailable at declared relative path")
                continue
            document = store.put_document(
                path,
                published_at=row["published_at"],
                data_class=DataClass.PUBLIC,
                source_url=row["source_url"],
                source_tier=row["source_tier"],
                retrieved_at=row["retrieved_at"],
                version=row["version"],
                is_synthetic=bool(row["is_synthetic"]),
                corpus_document_id=row["doc_id"],
            )
            documents.append(document)
            source_doc_ids.append(row["doc_id"])
        self._copy_facts(store, source_doc_ids, {item.id: item for item in documents})
        return PreparedCorpusTask(task, documents, issues, availability)

    def _copy_facts(
        self,
        store: EvidenceStore,
        source_doc_ids: list[str],
        platform_documents: dict[str, SourceDocument],
    ) -> None:
        if not source_doc_ids:
            return
        placeholders = ",".join("?" for _ in source_doc_ids)
        query = (
            "SELECT f.*, e.locator, e.text_content, e.page_number, e.block_type, "
            "e.verification_status AS evidence_verification_status "
            "FROM facts f JOIN evidence_blocks e ON e.evidence_id = f.evidence_id "
            f"WHERE f.doc_id IN ({placeholders})"
        )
        with self._connect() as connection:
            rows = connection.execute(query, source_doc_ids).fetchall()
            document_rows = connection.execute(
                f"SELECT doc_id, sha256 FROM documents WHERE doc_id IN ({placeholders})",
                source_doc_ids,
            ).fetchall()
        by_source_id = {row["doc_id"]: row["sha256"] for row in document_rows}
        facts: list[Fact] = []
        for row in rows:
            document_id = by_source_id.get(row["doc_id"])
            if document_id not in platform_documents or row["value"] is None:
                continue
            period_end = self._period_end(row["period"])
            if period_end is None:
                continue
            nature = {
                "assumption": FactNature.ASSUMPTION,
                "opinion": FactNature.OPINION,
            }.get(row["information_type"], FactNature.DISCLOSED)
            locator = f"sample:{row['evidence_id']}:{row['locator']}"
            fact_id = hashlib.sha256(
                f"{document_id}:{row['fact_id']}".encode("utf-8")
            ).hexdigest()
            facts.append(
                Fact(
                    id=fact_id,
                    document_id=document_id,
                    entity_id=row["entity_id"],
                    field=row["metric"],
                    value=Decimal(str(row["value"])),
                    unit=row["unit"] or "unknown",
                    period_end=period_end,
                    period_kind="FY",
                    scope=row["scope"] or "unknown",
                    published_at=platform_documents[document_id].published_at or period_end,
                    nature=nature,
                    evidence=EvidenceSpan(
                        document_id=document_id,
                        locator=locator,
                        excerpt=row["text_content"],
                        external_id=row["evidence_id"],
                        page_number=row["page_number"],
                        block_type=row["block_type"],
                        verification_status=row["evidence_verification_status"],
                    ),
                    raw_value=row["value_text"],
                    confidence=row["confidence"],
                    confidence_reason=row["confidence_reason"],
                    verification_status=row["verification_status"],
                    source_information_type=row["information_type"],
                )
            )
        store.put_facts(facts)

    @staticmethod
    def _period_end(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            if len(value) == 4 and value.isdigit():
                return date(int(value), 12, 31)
        return None
