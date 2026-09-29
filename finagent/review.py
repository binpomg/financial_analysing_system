"""Apply explicit human approvals to parser candidates."""

from __future__ import annotations

import hashlib
from pathlib import Path

from finagent.contracts import CandidateReviewBundle, EvidenceSpan, Fact
from finagent.storage import EvidenceStore


def load_review_bundle(path: Path) -> CandidateReviewBundle:
    return CandidateReviewBundle.model_validate_json(path.read_text(encoding="utf-8"))


def apply_review_bundle(store: EvidenceStore, path: Path) -> dict[str, object]:
    bundle = load_review_bundle(path)
    facts: list[Fact] = []
    decisions = []
    for approval in bundle.approvals:
        candidate = store.candidate_by_id(approval.candidate_id)
        if candidate is None:
            raise KeyError(f"unknown candidate: {approval.candidate_id}")
        document = store.get_document(candidate.document_id)
        if document is None:
            raise KeyError(f"candidate source is unavailable: {approval.candidate_id}")
        if document.published_at is None:
            raise ValueError(
                f"candidate source has no published_at: {approval.candidate_id}"
            )
        fact_id = hashlib.sha256(
            f"{bundle.review_id}:{approval.candidate_id}:{approval.field}:"
            f"{approval.period_end}:{approval.scope}".encode("utf-8")
        ).hexdigest()
        facts.append(
            Fact(
                id=fact_id,
                document_id=document.id,
                entity_id=approval.entity_id,
                field=approval.field,
                value=approval.normalized_value,
                unit=approval.unit,
                period_end=approval.period_end,
                period_kind=approval.period_kind,
                scope=approval.scope,
                published_at=document.published_at,
                evidence=candidate.evidence.model_copy(
                    update={"verification_status": "human_verified"}
                ),
                raw_value=candidate.raw_value,
                confidence="high",
                confidence_reason=approval.review_reason,
                verification_status="human_verified",
            )
        )
        decisions.append(
            {
                "candidate_id": approval.candidate_id,
                "fact_id": fact_id,
                "reviewer": approval.reviewer,
                "reviewed_at": approval.reviewed_at.isoformat(),
            }
        )
    store.put_facts(facts)
    entity_ids = sorted({fact.entity_id for fact in facts})
    detected_conflicts = []
    for entity_id in entity_ids:
        detected_conflicts.extend(
            store.fact_conflicts(entity_id, "9999-12-31")
        )
    store.put_candidate_review(
        bundle.review_id,
        {"approvals": decisions, "fact_conflicts": detected_conflicts},
    )
    return {
        "review_id": bundle.review_id,
        "approved_count": len(facts),
        "fact_ids": [fact.id for fact in facts],
        "fact_conflicts": detected_conflicts,
    }
