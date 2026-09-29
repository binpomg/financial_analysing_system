"""Local evidence store. The interfaces can later use managed SQL and object storage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from finagent.contracts import (
    DataClass,
    ExtractionCandidate,
    Fact,
    FactResolution,
    OcrJob,
    OcrTranscript,
    ReviewFinding,
    RunRecord,
    RunStatus,
    SourceDocument,
    TaskSpec,
    WorkflowNodeRecord,
    WorkflowNodeSpec,
    WorkflowNodeStatus,
    utc_now,
)


class EvidenceStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "metadata.sqlite3"
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL, published_at TEXT NOT NULL,
                    data TEXT NOT NULL,
                    FOREIGN KEY (document_id) REFERENCES documents(id)
                );
                CREATE INDEX IF NOT EXISTS facts_lookup
                    ON facts(entity_id, published_at);
                CREATE TABLE IF NOT EXISTS fact_resolutions (
                    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, field TEXT NOT NULL,
                    period_end TEXT NOT NULL, period_kind TEXT NOT NULL, scope TEXT NOT NULL,
                    unit TEXT NOT NULL, resolved_at TEXT NOT NULL, data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS fact_resolutions_lookup
                    ON fact_resolutions(entity_id, field, period_end, period_kind, scope, unit, resolved_at);
                CREATE TABLE IF NOT EXISTS extraction_candidates (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    data TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (document_id) REFERENCES documents(id)
                );
                CREATE INDEX IF NOT EXISTS extraction_candidates_document
                    ON extraction_candidates(document_id);
                CREATE TABLE IF NOT EXISTS candidate_reviews (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ocr_jobs (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    status TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (document_id) REFERENCES documents(id)
                );
                CREATE INDEX IF NOT EXISTS ocr_jobs_status ON ocr_jobs(status);
                CREATE TABLE IF NOT EXISTS ocr_transcripts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, document_id TEXT NOT NULL,
                    data TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES ocr_jobs(id),
                    FOREIGN KEY (document_id) REFERENCES documents(id)
                );
                CREATE INDEX IF NOT EXISTS ocr_transcripts_job ON ocr_transcripts(job_id);
                CREATE TABLE IF NOT EXISTS taxonomy_mapping_runs (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (document_id) REFERENCES documents(id)
                );
                CREATE INDEX IF NOT EXISTS taxonomy_mapping_runs_document
                    ON taxonomy_mapping_runs(document_id);
                CREATE TABLE IF NOT EXISTS source_registrations (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    data TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (document_id) REFERENCES documents(id)
                );
                CREATE INDEX IF NOT EXISTS source_registrations_document
                    ON source_registrations(document_id);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS workflow_nodes (
                    workflow_id TEXT NOT NULL, node_id TEXT NOT NULL, status TEXT NOT NULL,
                    data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(workflow_id, node_id),
                    FOREIGN KEY (workflow_id) REFERENCES runs(id)
                );
                CREATE INDEX IF NOT EXISTS workflow_nodes_claimable
                    ON workflow_nodes(workflow_id, status, created_at);
                CREATE TABLE IF NOT EXISTS review_findings (
                    id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, node_id TEXT NOT NULL,
                    verdict TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (workflow_id) REFERENCES runs(id)
                );
                CREATE INDEX IF NOT EXISTS review_findings_workflow
                    ON review_findings(workflow_id, node_id, created_at);
                """
            )

    def put_document(
        self,
        path: Path,
        *,
        published_at: str | None = None,
        data_class: DataClass = DataClass.RESTRICTED,
        source_url: str | None = None,
        source_tier: str | None = None,
        retrieved_at: str | None = None,
        version: str = "1",
        is_synthetic: bool = False,
        corpus_document_id: str | None = None,
        copy_source: bool = True,
    ) -> SourceDocument:
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        existing = self.get_document(digest)
        if existing is not None:
            if existing.data_class != data_class:
                # Content deduplication must never elevate a restricted registration to public.
                # Provenance records retain each source context separately.
                stricter_class = (
                    DataClass.RESTRICTED
                    if DataClass.RESTRICTED in {existing.data_class, data_class}
                    else DataClass.PUBLIC
                )
                existing = existing.model_copy(update={"data_class": stricter_class})
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE documents SET data = ? WHERE id = ?",
                        (existing.model_dump_json(), existing.id),
                    )
            if corpus_document_id and existing.corpus_document_id != corpus_document_id:
                existing = existing.model_copy(
                    update={"corpus_document_id": corpus_document_id}
                )
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE documents SET data = ? WHERE id = ?",
                        (existing.model_dump_json(), existing.id),
                    )
            return existing
        # 工作台已保存不可变原件时只登记引用，避免第二份原件及两套资料库。
        object_path = self.objects / digest if copy_source else path.resolve()
        if copy_source:
            object_path.write_bytes(content)
        document = SourceDocument(
            id=digest,
            name=path.name,
            sha256=digest,
            media_type=path.suffix.lower() or "application/octet-stream",
            published_at=published_at,
            ingested_at=utc_now(),
            data_class=data_class,
            object_path=str(object_path),
            source_url=source_url,
            source_tier=source_tier,
            retrieved_at=retrieved_at,
            version=version,
            is_synthetic=is_synthetic,
            corpus_document_id=corpus_document_id,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO documents(id, data) VALUES (?, ?)",
                (document.id, document.model_dump_json()),
            )
        return document

    def get_document(self, document_id: str) -> SourceDocument | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
        return SourceDocument.model_validate_json(row["data"]) if row else None

    def read_document(self, document_id: str) -> bytes:
        document = self.get_document(document_id)
        if document is None:
            raise KeyError(f"unknown document: {document_id}")
        content = Path(document.object_path).read_bytes()
        if hashlib.sha256(content).hexdigest() != document.sha256:
            raise ValueError("source content differs from its registered fingerprint")
        return content

    def put_facts(self, facts: list[Fact]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO facts(id, document_id, entity_id, published_at, data) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        fact.id,
                        fact.document_id,
                        fact.entity_id,
                        fact.published_at.isoformat(),
                        fact.model_dump_json(),
                    )
                    for fact in facts
                ],
            )

    def fact_by_id(self, fact_id: str) -> Fact | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
        return Fact.model_validate_json(row["data"]) if row else None

    def put_fact_resolution(self, resolution: FactResolution) -> None:
        preferred = self.fact_by_id(resolution.preferred_fact_id)
        if preferred is None:
            raise KeyError(f"unknown preferred fact: {resolution.preferred_fact_id}")
        preferred_key = self._fact_key(preferred)
        resolution_key = self._resolution_key(resolution)
        if preferred.entity_id != resolution.entity_id or preferred_key != resolution_key:
            raise ValueError(
                "preferred_fact_id does not match the entity, field, period, scope and unit of this resolution"
            )
        conflicts = self.fact_conflicts(resolution.entity_id, "9999-12-31")
        if not any(
            self._conflict_key(conflict) == resolution_key
            and any(item["fact_id"] == preferred.id for item in conflict["facts"])
            for conflict in conflicts
        ):
            raise ValueError("a resolution requires an existing conflict for the selected fact key")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO fact_resolutions("
                "id, entity_id, field, period_end, period_kind, scope, unit, resolved_at, data, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resolution.resolution_id,
                    resolution.entity_id,
                    resolution.field,
                    resolution.period_end.isoformat(),
                    resolution.period_kind,
                    resolution.scope,
                    resolution.unit,
                    resolution.resolved_at.isoformat(),
                    resolution.model_dump_json(by_alias=True),
                    utc_now().isoformat(),
                ),
            )

    def fact_resolutions_for(
        self, entity_id: str, as_of: str | None = None
    ) -> list[FactResolution]:
        query = "SELECT data FROM fact_resolutions WHERE entity_id = ?"
        params: list[str] = [entity_id]
        if as_of is not None:
            query += " AND date(resolved_at) <= date(?)"
            params.append(as_of)
        query += " ORDER BY resolved_at, id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [FactResolution.model_validate_json(row["data"]) for row in rows]

    def put_candidates(self, candidates: list[ExtractionCandidate]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO extraction_candidates(id, document_id, data, created_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        candidate.id,
                        candidate.document_id,
                        candidate.model_dump_json(),
                        utc_now().isoformat(),
                    )
                    for candidate in candidates
                ],
            )

    def candidates_for_document(self, document_id: str) -> list[ExtractionCandidate]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM extraction_candidates WHERE document_id = ? ORDER BY id",
                (document_id,),
            ).fetchall()
        return [ExtractionCandidate.model_validate_json(row["data"]) for row in rows]

    def candidate_by_id(self, candidate_id: str) -> ExtractionCandidate | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM extraction_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return ExtractionCandidate.model_validate_json(row["data"]) if row else None

    def put_candidate_review(self, review_id: str, payload: dict[str, object]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO candidate_reviews(id, data, created_at) VALUES (?, ?, ?)",
                (review_id, json.dumps(payload, ensure_ascii=False), utc_now().isoformat()),
            )

    def queue_ocr_job(
        self,
        document_id: str,
        reason: str,
        *,
        page_number: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> OcrJob:
        if self.get_document(document_id) is None:
            raise KeyError(f"unknown document: {document_id}")
        job_id = hashlib.sha256(
            f"ocr-v1:{document_id}:{page_number or 'document'}".encode("utf-8")
        ).hexdigest()
        with self._connect() as connection:
            row = connection.execute("SELECT data FROM ocr_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is not None:
                return OcrJob.model_validate_json(row["data"])
            job = OcrJob(
                id=job_id,
                document_id=document_id,
                page_number=page_number,
                reason=reason,
                width=width,
                height=height,
                created_at=utc_now(),
            )
            connection.execute(
                "INSERT INTO ocr_jobs(id, document_id, status, data, created_at) VALUES (?, ?, ?, ?, ?)",
                (job.id, job.document_id, job.status, job.model_dump_json(), job.created_at.isoformat()),
            )
        return job

    def ocr_jobs(self, status: str | None = None) -> list[OcrJob]:
        query = "SELECT data FROM ocr_jobs"
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at, id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [OcrJob.model_validate_json(row["data"]) for row in rows]

    def ocr_job_by_id(self, job_id: str) -> OcrJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM ocr_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return OcrJob.model_validate_json(row["data"]) if row else None

    def claim_ocr_job(self, job_id: str) -> OcrJob:
        """Atomically move a queued job to processing and record its attempt."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM ocr_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown OCR job: {job_id}")
            job = OcrJob.model_validate_json(row["data"])
            if job.status != "queued":
                raise ValueError(f"OCR job is not queued: {job_id} ({job.status})")
            claimed = job.model_copy(
                update={
                    "status": "processing",
                    "started_at": utc_now(),
                    "finished_at": None,
                    "attempt_count": job.attempt_count + 1,
                    "error": None,
                }
            )
            updated = connection.execute(
                "UPDATE ocr_jobs SET status = ?, data = ? WHERE id = ? AND status = 'queued'",
                (claimed.status, claimed.model_dump_json(), job_id),
            )
            if updated.rowcount != 1:
                raise ValueError(f"OCR job was claimed by another worker: {job_id}")
        return claimed

    def complete_ocr_job(self, job_id: str) -> OcrJob:
        return self._finish_ocr_job(job_id, "completed")

    def fail_ocr_job(self, job_id: str, error: str) -> OcrJob:
        return self._finish_ocr_job(job_id, "failed", error=error)

    def _finish_ocr_job(
        self, job_id: str, status: Literal["completed", "failed"], *, error: str | None = None
    ) -> OcrJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM ocr_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown OCR job: {job_id}")
            job = OcrJob.model_validate_json(row["data"])
            if job.status != "processing":
                raise ValueError(f"OCR job is not processing: {job_id} ({job.status})")
            finished = job.model_copy(
                update={"status": status, "finished_at": utc_now(), "error": error}
            )
            connection.execute(
                "UPDATE ocr_jobs SET status = ?, data = ? WHERE id = ?",
                (finished.status, finished.model_dump_json(), job_id),
            )
        return finished

    def requeue_ocr_job(self, job_id: str) -> OcrJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM ocr_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown OCR job: {job_id}")
            job = OcrJob.model_validate_json(row["data"])
            if job.status != "failed":
                raise ValueError(f"only failed OCR jobs can be requeued: {job_id} ({job.status})")
            requeued = job.model_copy(
                update={"status": "queued", "started_at": None, "finished_at": None, "error": None}
            )
            connection.execute(
                "UPDATE ocr_jobs SET status = ?, data = ? WHERE id = ?",
                (requeued.status, requeued.model_dump_json(), job_id),
            )
        return requeued

    def put_ocr_transcript(self, transcript: OcrTranscript) -> None:
        job = self.ocr_job_by_id(transcript.job_id)
        if job is None:
            raise KeyError(f"unknown OCR job: {transcript.job_id}")
        if job.document_id != transcript.document_id:
            raise ValueError("OCR transcript document_id does not match its job")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO ocr_transcripts(id, job_id, document_id, data, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    transcript.id,
                    transcript.job_id,
                    transcript.document_id,
                    transcript.model_dump_json(by_alias=True),
                    utc_now().isoformat(),
                ),
            )

    def ocr_transcripts_for_job(self, job_id: str) -> list[OcrTranscript]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM ocr_transcripts WHERE job_id = ? ORDER BY created_at, id",
                (job_id,),
            ).fetchall()
        return [OcrTranscript.model_validate_json(row["data"]) for row in rows]

    def put_taxonomy_mapping_run(
        self, run_id: str, document_id: str, payload: dict[str, object]
    ) -> None:
        if self.get_document(document_id) is None:
            raise KeyError(f"unknown document: {document_id}")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO taxonomy_mapping_runs(id, document_id, data, created_at) "
                "VALUES (?, ?, ?, ?)",
                (run_id, document_id, json.dumps(payload, ensure_ascii=False), utc_now().isoformat()),
            )

    def taxonomy_mapping_runs_for(self, document_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, data, created_at FROM taxonomy_mapping_runs "
                "WHERE document_id = ? ORDER BY created_at, id",
                (document_id,),
            ).fetchall()
        return [
            {"id": row["id"], "data": json.loads(row["data"]), "created_at": row["created_at"]}
            for row in rows
        ]

    def facts_for(self, entity_id: str, as_of: str, source_ids: list[str]) -> list[Fact]:
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        query = (
            "SELECT data FROM facts WHERE entity_id = ? AND published_at <= ? "
            f"AND document_id IN ({placeholders}) ORDER BY published_at DESC, id"
        )
        with self._connect() as connection:
            rows = connection.execute(query, (entity_id, as_of, *source_ids)).fetchall()
        return [Fact.model_validate_json(row["data"]) for row in rows]

    @staticmethod
    def _fact_key(fact: Fact) -> tuple[str, str, str, str, str]:
        return (
            fact.field,
            fact.period_end.isoformat(),
            fact.period_kind,
            fact.scope,
            fact.unit,
        )

    @staticmethod
    def _resolution_key(resolution: FactResolution) -> tuple[str, str, str, str, str]:
        return (
            resolution.field,
            resolution.period_end.isoformat(),
            resolution.period_kind,
            resolution.scope,
            resolution.unit,
        )

    @staticmethod
    def _conflict_key(conflict: dict[str, object]) -> tuple[str, str, str, str, str]:
        return (
            str(conflict["field"]),
            str(conflict["period_end"]),
            str(conflict["period_kind"]),
            str(conflict["scope"]),
            str(conflict["unit"]),
        )

    def effective_facts_for(
        self, entity_id: str, as_of: str, source_ids: list[str]
    ) -> tuple[list[Fact], list[dict[str, object]], list[FactResolution]]:
        """Return task-eligible facts after applying timely scoped reviewer choices."""

        facts = self.facts_for(entity_id, as_of, source_ids)
        conflicts = self.fact_conflicts(entity_id, as_of, source_ids)
        resolutions = self.fact_resolutions_for(entity_id, as_of)
        latest_by_key: dict[tuple[str, str, str, str, str], FactResolution] = {}
        for resolution in resolutions:
            latest_by_key[self._resolution_key(resolution)] = resolution

        excluded_fact_ids: set[str] = set()
        unresolved: list[dict[str, object]] = []
        applied: list[FactResolution] = []
        for conflict in conflicts:
            key = self._conflict_key(conflict)
            resolution = latest_by_key.get(key)
            conflict_fact_ids = {str(item["fact_id"]) for item in conflict["facts"]}
            if resolution is None or resolution.preferred_fact_id not in conflict_fact_ids:
                unresolved.append(conflict)
                continue
            excluded_fact_ids.update(conflict_fact_ids - {resolution.preferred_fact_id})
            applied.append(resolution)
        effective_facts = [fact for fact in facts if fact.id not in excluded_fact_ids]
        return effective_facts, unresolved, applied

    def fact_conflicts(
        self, entity_id: str, as_of: str, source_ids: list[str] | None = None
    ) -> list[dict[str, object]]:
        query = "SELECT data FROM facts WHERE entity_id = ? AND published_at <= ?"
        params: list[str] = [entity_id, as_of]
        if source_ids is not None:
            if not source_ids:
                return []
            placeholders = ",".join("?" for _ in source_ids)
            query += f" AND document_id IN ({placeholders})"
            params.extend(source_ids)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        groups: dict[tuple[str, str, str, str, str], list[Fact]] = {}
        for row in rows:
            fact = Fact.model_validate_json(row["data"])
            key = self._fact_key(fact)
            groups.setdefault(key, []).append(fact)
        conflicts = []
        for key, facts in groups.items():
            if len({fact.value for fact in facts}) < 2:
                continue
            conflicts.append(
                {
                    "entity_id": entity_id,
                    "field": key[0],
                    "period_end": key[1],
                    "period_kind": key[2],
                    "scope": key[3],
                    "unit": key[4],
                    "facts": [
                        {
                            "fact_id": fact.id,
                            "document_id": fact.document_id,
                            "value": str(fact.value),
                            "published_at": fact.published_at.isoformat(),
                            "verification_status": fact.verification_status,
                        }
                        for fact in sorted(facts, key=lambda item: (item.published_at, item.id))
                    ],
                }
            )
        return conflicts

    def put_source_registration(
        self, registration_id: str, document_id: str, payload: dict[str, object]
    ) -> None:
        if self.get_document(document_id) is None:
            raise KeyError(f"unknown document: {document_id}")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO source_registrations(id, document_id, data, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    registration_id,
                    document_id,
                    json.dumps(payload, ensure_ascii=False),
                    utc_now().isoformat(),
                ),
            )

    def source_registration(self, registration_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document_id, data, created_at FROM source_registrations WHERE id = ?",
                (registration_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": registration_id,
            "document_id": row["document_id"],
            "data": json.loads(row["data"]),
            "created_at": row["created_at"],
        }

    def create_run(self, task: TaskSpec) -> RunRecord:
        record = RunRecord(
            id=uuid4().hex,
            task=task,
            status=RunStatus.RUNNING,
            source_ids=[],
            created_at=utc_now(),
        )
        self._save_run(record)
        return record

    def _save_run(self, record: RunRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runs(id, status, data) VALUES (?, ?, ?)",
                (record.id, record.status.value, record.model_dump_json()),
            )

    def update_run(self, record: RunRecord) -> None:
        self._save_run(record)

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        return RunRecord.model_validate_json(row["data"]) if row else None

    def add_event(self, run_id: str, kind: str, payload: dict[str, object]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO events(run_id, at, kind, payload) VALUES (?, ?, ?, ?)",
                (run_id, utc_now().isoformat(), kind, json.dumps(payload, ensure_ascii=False)),
            )

    def events_for(self, run_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT at, kind, payload FROM events WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [
            {"at": row["at"], "kind": row["kind"], "payload": json.loads(row["payload"])}
            for row in rows
        ]

    def put_artifact(self, run_id: str, kind: str, payload: dict[str, object]) -> str:
        artifact_id = uuid4().hex
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO artifacts(id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    run_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    utc_now().isoformat(),
                ),
            )
        return artifact_id

    def artifacts_for(self, run_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, kind, payload, created_at FROM artifacts "
                "WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def plan_workflow_nodes(self, workflow_id: str, specs: list[WorkflowNodeSpec]) -> list[WorkflowNodeRecord]:
        """Persist a DAG plan once; repeated planning is idempotent by node key."""

        if self.get_run(workflow_id) is None:
            raise KeyError(f"unknown workflow/run: {workflow_id}")
        now = utc_now()
        with self._connect() as connection:
            for spec in specs:
                record = WorkflowNodeRecord(
                    workflow_id=workflow_id,
                    node_id=spec.node_id,
                    status=WorkflowNodeStatus.QUEUED,
                    spec=spec,
                )
                connection.execute(
                    "INSERT OR IGNORE INTO workflow_nodes(workflow_id, node_id, status, data, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        workflow_id,
                        spec.node_id,
                        record.status.value,
                        record.model_dump_json(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
        return self.workflow_nodes_for(workflow_id)

    def workflow_nodes_for(self, workflow_id: str) -> list[WorkflowNodeRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM workflow_nodes WHERE workflow_id = ? ORDER BY created_at, node_id",
                (workflow_id,),
            ).fetchall()
        return [WorkflowNodeRecord.model_validate_json(row["data"]) for row in rows]

    def workflow_node(self, workflow_id: str, node_id: str) -> WorkflowNodeRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM workflow_nodes WHERE workflow_id = ? AND node_id = ?",
                (workflow_id, node_id),
            ).fetchone()
        return WorkflowNodeRecord.model_validate_json(row["data"]) if row else None

    def claim_next_workflow_node(self, workflow_id: str) -> WorkflowNodeRecord | None:
        """Atomically claim one dependency-ready node for a worker."""

        now = utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT node_id, data FROM workflow_nodes WHERE workflow_id = ? AND status = ? "
                "ORDER BY created_at, node_id",
                (workflow_id, WorkflowNodeStatus.QUEUED.value),
            ).fetchall()
            statuses = {
                row["node_id"]: row["status"]
                for row in connection.execute(
                    "SELECT node_id, status FROM workflow_nodes WHERE workflow_id = ?", (workflow_id,)
                ).fetchall()
            }
            for row in rows:
                node = WorkflowNodeRecord.model_validate_json(row["data"])
                if any(statuses.get(parent) != WorkflowNodeStatus.COMPLETED.value for parent in node.spec.depends_on):
                    continue
                claimed = node.model_copy(
                    update={
                        "status": WorkflowNodeStatus.RUNNING,
                        "attempt_count": node.attempt_count + 1,
                        "started_at": now,
                        "finished_at": None,
                        "error": None,
                    }
                )
                updated = connection.execute(
                    "UPDATE workflow_nodes SET status = ?, data = ?, updated_at = ? "
                    "WHERE workflow_id = ? AND node_id = ? AND status = ?",
                    (
                        claimed.status.value,
                        claimed.model_dump_json(),
                        now.isoformat(),
                        workflow_id,
                        claimed.node_id,
                        WorkflowNodeStatus.QUEUED.value,
                    ),
                )
                if updated.rowcount == 1:
                    return claimed
        return None

    def complete_workflow_node(
        self, workflow_id: str, node_id: str, checkpoint: dict[str, object]
    ) -> WorkflowNodeRecord:
        return self._transition_workflow_node(
            workflow_id, node_id, WorkflowNodeStatus.COMPLETED, checkpoint=checkpoint
        )

    def retry_or_fail_workflow_node(
        self, workflow_id: str, node_id: str, error: str, checkpoint: dict[str, object] | None = None
    ) -> WorkflowNodeRecord:
        node = self.workflow_node(workflow_id, node_id)
        if node is None:
            raise KeyError(f"unknown workflow node: {workflow_id}/{node_id}")
        status = (
            WorkflowNodeStatus.QUEUED
            if node.attempt_count < node.spec.max_attempts
            else WorkflowNodeStatus.FAILED
        )
        return self._transition_workflow_node(
            workflow_id, node_id, status, checkpoint=checkpoint or node.checkpoint, error=error
        )

    def block_workflow_node(self, workflow_id: str, node_id: str, reason: str) -> WorkflowNodeRecord:
        node = self.workflow_node(workflow_id, node_id)
        if node is None:
            raise KeyError(f"unknown workflow node: {workflow_id}/{node_id}")
        if node.status is not WorkflowNodeStatus.QUEUED:
            return node
        return self._transition_workflow_node(
            workflow_id,
            node_id,
            WorkflowNodeStatus.BLOCKED,
            checkpoint=node.checkpoint,
            error=reason,
            expected_status=WorkflowNodeStatus.QUEUED,
        )

    def recover_expired_workflow_nodes(self, workflow_id: str) -> list[WorkflowNodeRecord]:
        """Release expired worker leases; exhausted nodes become terminal failures."""

        now = utc_now()
        recovered = []
        for node in self.workflow_nodes_for(workflow_id):
            if node.status is not WorkflowNodeStatus.RUNNING or node.started_at is None:
                continue
            elapsed = (now - node.started_at).total_seconds()
            if elapsed < node.spec.timeout_seconds:
                continue
            status = (
                WorkflowNodeStatus.QUEUED
                if node.attempt_count < node.spec.max_attempts
                else WorkflowNodeStatus.FAILED
            )
            recovered.append(
                self._transition_workflow_node(
                    workflow_id,
                    node.node_id,
                    status,
                    checkpoint=node.checkpoint,
                    error=f"worker lease expired after {node.spec.timeout_seconds} seconds",
                    expected_status=WorkflowNodeStatus.RUNNING,
                )
            )
        return recovered

    def _transition_workflow_node(
        self,
        workflow_id: str,
        node_id: str,
        status: WorkflowNodeStatus,
        *,
        checkpoint: dict[str, object],
        error: str | None = None,
        expected_status: WorkflowNodeStatus = WorkflowNodeStatus.RUNNING,
    ) -> WorkflowNodeRecord:
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM workflow_nodes WHERE workflow_id = ? AND node_id = ?",
                (workflow_id, node_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown workflow node: {workflow_id}/{node_id}")
            node = WorkflowNodeRecord.model_validate_json(row["data"])
            if node.status is not expected_status:
                raise ValueError(
                    f"workflow node is not {expected_status.value}: {workflow_id}/{node_id} ({node.status.value})"
                )
            updated_node = node.model_copy(
                update={
                    "status": status,
                    "checkpoint": checkpoint,
                    "error": error,
                    "finished_at": now if status in {WorkflowNodeStatus.COMPLETED, WorkflowNodeStatus.FAILED, WorkflowNodeStatus.BLOCKED} else None,
                    "started_at": None if status is WorkflowNodeStatus.QUEUED else node.started_at,
                }
            )
            connection.execute(
                "UPDATE workflow_nodes SET status = ?, data = ?, updated_at = ? WHERE workflow_id = ? AND node_id = ?",
                (updated_node.status.value, updated_node.model_dump_json(), now.isoformat(), workflow_id, node_id),
            )
        return updated_node

    def put_review_finding(self, finding: ReviewFinding) -> None:
        if self.workflow_node(finding.workflow_id, finding.node_id) is None:
            raise KeyError(f"finding references unknown workflow node: {finding.workflow_id}/{finding.node_id}")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO review_findings(id, workflow_id, node_id, verdict, data, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    finding.id,
                    finding.workflow_id,
                    finding.node_id,
                    finding.verdict.value,
                    finding.model_dump_json(),
                    utc_now().isoformat(),
                ),
            )

    def review_findings_for(self, workflow_id: str, node_id: str | None = None) -> list[ReviewFinding]:
        query = "SELECT data FROM review_findings WHERE workflow_id = ?"
        params: list[str] = [workflow_id]
        if node_id is not None:
            query += " AND node_id = ?"
            params.append(node_id)
        query += " ORDER BY created_at, id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [ReviewFinding.model_validate_json(row["data"]) for row in rows]
