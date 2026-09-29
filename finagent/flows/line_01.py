"""Line 1: registered sources and structured extraction results."""

from __future__ import annotations

from collections import defaultdict
import json
import re
from typing import Any

from finagent.contracts import FlowResult, RunStatus
from finagent.flows.base import FlowContext
from finagent.flows.sample_utils import corpus_json_documents
from finagent.models import ModelUnavailable
from finagent.prompts import prompt_for


class DocumentFlow:
    def execute(self, context: FlowContext) -> FlowResult:
        if context.task.corpus_task_id == "L1_PUBLIC":
            return self._sample_public_annual_reports(context)
        if context.task.corpus_task_id == "L1_FORMATS":
            return self._sample_formats(context)
        if context.task.corpus_task_id == "L1_VERSION":
            return self._sample_version_review(context)
        facts = []
        if context.task.entity_id:
            facts = context.store.facts_for(
                context.task.entity_id, context.task.as_of.isoformat(), context.source_ids
            )
        payload = {
                "documents": [doc.model_dump(mode="json") for doc in context.documents],
                "facts": [fact.model_dump(mode="json") for fact in facts],
            }
        limitations = list(context.ingestion_issues)
        if context.ingestion_issues:
            try:
                answer = context.ask_model(prompt_for(context.task.line), tier="small")
                payload["candidate_summary"] = answer.summary
                payload["candidate_claims"] = [
                    claim.model_dump(mode="json")
                    for claim in answer.claims
                    if claim.document_id in context.source_ids
                    and context.valid_locator(claim.document_id, claim.locator)
                ]
                limitations.extend([*context.warnings, *answer.limitations])
                limitations.append("candidate extraction is not field-verified")
            except ModelUnavailable as exc:
                limitations.append(str(exc))
        return FlowResult(
            status=RunStatus.PARTIAL if limitations else RunStatus.COMPLETED,
            payload=payload,
            limitations=limitations,
        )

    def _sample_public_annual_reports(self, context: FlowContext) -> FlowResult:
        facts = []
        if context.task.entity_id:
            facts = context.store.facts_for(
                context.task.entity_id, context.task.as_of.isoformat(), context.source_ids
            )
        materials = []
        for document in context.documents:
            metadata = context.source_metadata.get(document.corpus_document_id or "", {})
            materials.append({
                "document_id": document.id,
                "corpus_document_id": document.corpus_document_id,
                "published_at": document.published_at.isoformat() if document.published_at else None,
                "source_url": document.source_url,
                "parser": metadata.get("parser"),
                "parse_status": metadata.get("parse_status"),
                "downstream_eligible": metadata.get("downstream_eligible"),
            })
        verified_fields = [
            {
                "entity": fact.entity_id,
                "metric": fact.field,
                "value": str(fact.value),
                "unit": fact.unit,
                "period": fact.period_end.isoformat(),
                "scope": fact.scope,
                "evidence": fact.evidence.model_dump(mode="json"),
                "verification_status": fact.verification_status,
            }
            for fact in facts
            if fact.verification_status == "visual_spotchecked_against_original"
        ]
        limitations = [
            *context.ingestion_issues,
            "The sample corpus marks the supplied annual-report text extraction as partial; only fields with an explicit visual spot-check are exposed as verified fields.",
            "No table reconstruction, entity resolution or unverified PDF-text extraction is presented as a financial fact.",
        ]
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Registered the public annual reports and returned only source fields carrying explicit sample verification status.",
                "materials": materials,
                "verified_fields": verified_fields,
                "evidence_refs": [
                    {"document_id": item.id, "corpus_document_id": item.corpus_document_id, "source_url": item.source_url}
                    for item in context.documents
                ],
                "confidence": "medium",
                "confidence_reason": "Document provenance and parser state come from the read-only sample catalog; verified fields retain their page evidence and source verification status.",
                "limitations": limitations,
            },
            limitations=limitations,
        )

    def _sample_formats(self, context: FlowContext) -> FlowResult:
        """Classify each raw input without treating source text as executable instructions."""
        parse_results: list[dict[str, Any]] = []
        limitations = list(context.ingestion_issues)
        injection_found = False
        for document in context.documents:
            raw = context.store.read_document(document.id)
            result = self._inspect_document(document, raw)
            parse_results.append(result)
            if result.get("untrusted_instruction_detected"):
                injection_found = True

        gbk_result = next(
            (item for item in parse_results if item.get("corpus_document_id") == "SYN_GBK"),
            None,
        )
        extractions = {
            "gbk_announcement": gbk_result.get("extraction", {}) if gbk_result else {},
        }
        injection_handling = {
            "source_preserved": injection_found,
            "instructions_executed": False,
            "provenance_retained": injection_found,
            "confidence_override_applied": False,
            "external_transfer_performed": False,
        }
        limitations.extend([
            "OCR is intentionally pending for raster-only inputs; no OCR text was fabricated.",
            "Format classification records parser readiness, not field-level financial verification.",
        ])
        evidence_refs = [
            {
                "document_id": document.id,
                "corpus_document_id": document.corpus_document_id,
                "source_url": document.source_url,
            }
            for document in context.documents
        ]
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Registered every task-bound source, classified bytes and encoding, and preserved unsafe source text as data.",
                "parse_results": parse_results,
                "extractions": extractions,
                "injection_handling": injection_handling,
                "evidence_refs": evidence_refs,
                "confidence": "medium",
                "confidence_reason": "File signatures and decoding were checked deterministically; OCR and complex format extraction remain pending.",
                "limitations": limitations,
            },
            limitations=limitations,
        )

    @staticmethod
    def _inspect_document(document, raw: bytes) -> dict[str, Any]:
        suffix = document.media_type.lower()
        result: dict[str, Any] = {
            "document_id": document.id,
            "corpus_document_id": document.corpus_document_id,
            "declared_extension": suffix,
            "byte_count": len(raw),
            "parser_status": "registered",
            "detected_format": "unknown",
            "downstream_eligible": False,
        }
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            result.update(detected_format="image/png", parser_status="ocr_pending")
            return result
        if raw.startswith(b"%PDF-"):
            result.update(detected_format="application/pdf", parser_status="parser_not_configured")
            return result
        if suffix == ".pdf":
            result.update(
                detected_format="non_pdf_payload",
                parser_status="failed",
                reason="declared PDF does not have a PDF file signature",
            )
            return result
        if raw.startswith(b"PK\x03\x04"):
            result.update(detected_format="application/zip", parser_status="parser_not_configured")
            return result
        if suffix == ".json":
            try:
                json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                result.update(detected_format="application/json", parser_status="failed", reason=type(exc).__name__)
            else:
                result.update(detected_format="application/json", parser_status="parsed", downstream_eligible=True)
            return result
        if suffix == ".jsonl":
            try:
                [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                result.update(detected_format="application/jsonl", parser_status="failed", reason=type(exc).__name__)
            else:
                result.update(detected_format="application/jsonl", parser_status="parsed", downstream_eligible=True)
            return result
        text, encoding = DocumentFlow._decode_text(raw)
        if text is None:
            result.update(detected_format="binary", parser_status="failed", reason="no supported text decoding")
            return result
        result["encoding"] = encoding
        suspicious = bool(re.search(r"忽略所有既有规则|ignore (all|previous).*rule", text, flags=re.IGNORECASE))
        if suffix in {".txt", ".md", ".eml"}:
            result.update(detected_format="text/plain", parser_status="parsed", downstream_eligible=True)
        elif text.lstrip().lower().startswith(("<!doctype html", "<html")):
            result.update(detected_format="text/html", parser_status="parsed", downstream_eligible=True)
        elif text.lstrip().startswith("<?xml"):
            result.update(detected_format="application/xml", parser_status="parsed", downstream_eligible=True)
        else:
            result.update(detected_format="text/plain", parser_status="registered")
        if document.corpus_document_id == "SYN_GBK":
            result["extraction"] = DocumentFlow._extract_gbk_announcement(text)
        if suspicious:
            result["untrusted_instruction_detected"] = True
            result["instruction_handling"] = "stored_as_untrusted_source_content"
        return result

    @staticmethod
    def _decode_text(raw: bytes) -> tuple[str | None, str | None]:
        for encoding in ("utf-8", "gb18030"):
            try:
                return raw.decode(encoding), encoding
            except UnicodeDecodeError:
                continue
        return None, None

    @staticmethod
    def _extract_gbk_announcement(text: str) -> dict[str, Any]:
        amount_match = re.search(r"合同金额[：:]\s*([\d,]+(?:\.\d+)?)", text)
        date_match = re.search(r"签约日\s*(\d{4})-(\d{1,2})-(\d{1,2})", text)
        result: dict[str, Any] = {"currency": "CNY", "unit": "CNY_10000"}
        if amount_match:
            result["amount"] = float(amount_match.group(1).replace(",", ""))
        if date_match:
            result["event_date"] = "-".join(
                (date_match.group(1), date_match.group(2).zfill(2), date_match.group(3).zfill(2))
            )
        return result

    def _sample_version_review(self, context: FlowContext) -> FlowResult:
        records = []
        by_fact = defaultdict(list)
        for document, value in corpus_json_documents(context):
            if not {"entity_id", "period", "metric", "value", "unit"}.issubset(value):
                continue
            record = {
                "document_id": document.id,
                "corpus_document_id": document.corpus_document_id,
                "entity_id": value["entity_id"],
                "period": value["period"],
                "metric": value["metric"],
                "value": value["value"],
                "unit": value["unit"],
                "version": value.get("version", "unversioned"),
                "supersedes": value.get("supersedes"),
                "published_at": value.get("published_at"),
                "claimed_source": value.get("claimed_source"),
            }
            records.append(record)
            by_fact[(record["entity_id"], record["period"], record["metric"], record["unit"])].append(record)
        if not records:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={
                    "task_id": context.task.corpus_task_id,
                    "conclusion": "Version review could not identify comparable structured values.",
                    "evidence_refs": [],
                    "confidence": "unassessable",
                    "confidence_reason": "No task-bound source exposed the required entity, period, metric and value fields.",
                    "limitations": ["no comparable structured values"],
                },
                limitations=["no comparable structured values"],
            )
        conflicts = []
        selected = []
        for key, candidates in by_fact.items():
            superseding = [item for item in candidates if item["supersedes"]]
            chosen = superseding[-1] if superseding else max(candidates, key=lambda item: item["published_at"] or "")
            selected.append({
                "fact_key": key,
                "selected_document_id": chosen["document_id"],
                "selected_corpus_document_id": chosen["corpus_document_id"],
                "reason": "explicit_supersedes" if superseding else "latest_available_version",
            })
            values = {str(item["value"]) for item in candidates}
            if len(values) > 1:
                conflicts.append({
                    "fact_key": key,
                    "candidate_document_ids": [item["document_id"] for item in candidates],
                    "candidate_corpus_document_ids": [item["corpus_document_id"] for item in candidates],
                    "selected_document_id": chosen["document_id"],
                    "selected_corpus_document_id": chosen["corpus_document_id"],
                    "reason": "explicit correction takes precedence; third-party values remain recorded as conflicts",
                })
        limitations = list(context.ingestion_issues)
        if conflicts:
            limitations.append("conflicting values remain visible; selection does not erase historical or third-party records")
        evidence_refs = [{"document_id": item.id, "source_url": item.source_url} for item in context.documents]
        return FlowResult(
            status=RunStatus.COMPLETED,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Reviewed correction and conflict relationships while retaining all source versions.",
                "records": records,
                "selected_versions": selected,
                "conflicts": conflicts,
                "evidence_refs": evidence_refs,
                "confidence": "medium",
                "confidence_reason": "Selection follows explicit supersession metadata; all sources are synthetic fixtures.",
                "limitations": limitations,
            },
            limitations=limitations,
        )
