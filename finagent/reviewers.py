"""Deterministic actor/reviewer findings used by the durable runtime workflow."""

from __future__ import annotations

import hashlib

from finagent.contracts import (
    BusinessLine,
    FindingVerdict,
    FlowResult,
    ReviewFinding,
    TaskSpec,
)
from finagent.storage import EvidenceStore


def review_run(
    store: EvidenceStore,
    workflow_id: str,
    node_id: str,
    task: TaskSpec,
    result: FlowResult,
    source_ids: list[str],
    output_acceptance: dict[str, object],
) -> list[ReviewFinding]:
    """Run the actor/reviewer loop without promoting any unverified candidate.

    These reviewers decide whether a result can continue, should retry, needs a
    human, or is blocked by conflicting verified facts.  They only create findings;
    fact approval remains an explicit human operation.
    """
    findings = [
        _finding(
            workflow_id,
            node_id,
            "extractor_actor",
            "run",
            workflow_id,
            FindingVerdict.PASS if source_ids else FindingVerdict.FAIL,
            "info" if source_ids else "error",
            "source material is available to the flow" if source_ids else "the flow has no source material",
            "continue" if source_ids else "block",
        ),
        _finding(
            workflow_id,
            node_id,
            "evidence_reviewer",
            "flow_result",
            workflow_id,
            FindingVerdict.PASS if output_acceptance.get("passed") else FindingVerdict.FAIL,
            "info" if output_acceptance.get("passed") else "error",
            "output contract is complete"
            if output_acceptance.get("passed")
            else "required output fields are missing: "
            + ", ".join(str(item) for item in output_acceptance.get("missing_fields", [])),
            # 重审同一 checkpoint 不能补齐缺失结果，保留问题交给显式修订。
            "continue" if output_acceptance.get("passed") else "escalate",
        ),
    ]
    if task.line is BusinessLine.FINANCIAL_REPORT:
        facts, unresolved_conflicts, _ = (
            store.effective_facts_for(task.entity_id, task.as_of.isoformat(), source_ids)
            if task.entity_id
            else ([], [], [])
        )
        findings.append(
            _finding(
                workflow_id,
                node_id,
                "financial_reviewer",
                "financial_facts",
                task.entity_id or workflow_id,
                FindingVerdict.PASS if facts else FindingVerdict.NEEDS_HUMAN,
                "info" if facts else "warning",
                "verified financial facts are available"
                if facts
                else "no verified financial facts are available; parser candidates require page-level review",
                "continue" if facts else "escalate",
            )
        )
        findings.append(
            _finding(
                workflow_id,
                node_id,
                "conflict_reviewer",
                "financial_facts",
                task.entity_id or workflow_id,
                FindingVerdict.FAIL if unresolved_conflicts else FindingVerdict.PASS,
                "error" if unresolved_conflicts else "info",
                (
                    f"{len(unresolved_conflicts)} unresolved verified-fact conflict(s)"
                    if unresolved_conflicts
                    else "no unresolved verified-fact conflicts"
                ),
                "block" if unresolved_conflicts else "continue",
            )
        )
    for finding in findings:
        store.put_review_finding(finding)
    return findings


def _finding(
    workflow_id: str,
    node_id: str,
    reviewer: str,
    subject_type: str,
    subject_id: str,
    verdict: FindingVerdict,
    severity: str,
    reason: str,
    suggested_action: str,
) -> ReviewFinding:
    digest = hashlib.sha256(
        f"{workflow_id}:{node_id}:{reviewer}:{subject_type}:{subject_id}".encode("utf-8")
    ).hexdigest()
    return ReviewFinding(
        id=digest,
        workflow_id=workflow_id,
        node_id=node_id,
        reviewer=reviewer,
        subject_type=subject_type,
        subject_id=subject_id,
        verdict=verdict,
        severity=severity,  # type: ignore[arg-type]
        reason=reason,
        suggested_action=suggested_action,  # type: ignore[arg-type]
    )
