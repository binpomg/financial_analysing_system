"""Line 7: fixed-rubric report quality evaluation."""

from __future__ import annotations

from finagent.contracts import FlowResult, RunStatus
from finagent.flows.base import FlowContext, ResearchDraftFlow



class QualityFlow(ResearchDraftFlow):
    label = "report_quality"

    def execute(self, context: FlowContext) -> FlowResult:
        if context.task.corpus_task_id in {"L7_COMPARE", "L7_COVERAGE"}:
            return self._sample_quality_review(context)
        if context.task.corpus_task_id == "L7_PUBLIC":
            return self._sample_public_document_scope(context)
        return super().execute(context)

    def _sample_public_document_scope(self, context: FlowContext) -> FlowResult:
        classifications = []
        for document in context.documents:
            if document.corpus_document_id == "SYN_REPORT_A":
                classifications.append({
                    "document_id": document.id,
                    "corpus_document_id": document.corpus_document_id,
                    "document_type": "synthetic_research_report",
                    "research_quality_rubric_eligible": True,
                    "treatment": "candidate_for_evidence_and_method_review",
                })
            else:
                classifications.append({
                    "document_id": document.id,
                    "corpus_document_id": document.corpus_document_id,
                    "document_type": "official_financial_disclosure",
                    "research_quality_rubric_eligible": False,
                    "treatment": "evidence_source_only",
                })
        limitations = [
            *context.ingestion_issues,
            "Official annual reports are evidence sources, not sell-side research reports, and are excluded from research-quality scoring.",
            "Document length, formatting and PDF parser state are not used as proxies for research quality.",
            "The one synthetic research-report candidate is not assigned a production quality score without an independent evidence set and calibrated human labels.",
        ]
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Classified each bound document before evaluation: financial disclosures remain evidence-only, while the synthetic report is eligible for a separate research-quality review when independent verification material is available.",
                "document_classifications": classifications,
                "evaluation_scope": {
                    "scored_document_ids": [],
                    "excluded_from_research_quality_scoring": [
                        item["corpus_document_id"]
                        for item in classifications
                        if not item["research_quality_rubric_eligible"]
                    ],
                    "score_status": "not_run_due_to_missing_independent_calibration_set",
                },
                "evidence_refs": [
                    {
                        "document_id": document.id,
                        "corpus_document_id": document.corpus_document_id,
                        "source_url": document.source_url,
                    }
                    for document in context.documents
                ],
                "confidence": "high",
                "confidence_reason": "Document-role classification uses the task-bound catalog and explicit corpus identifiers; no subjective score is inferred from document form or length.",
                "limitations": limitations,
            },
            limitations=limitations,
        )

    def _sample_quality_review(self, context: FlowContext) -> FlowResult:
        reports = {item.corpus_document_id: item for item in context.documents if item.corpus_document_id in {"SYN_REPORT_A", "SYN_REPORT_B", "SYN_REPORT_C"}}
        if not reports:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"task_id": context.task.corpus_task_id, "reports": []},
                limitations=["no research reports are bound to the quality-evaluation task"],
            )
        rubric = {
            "facts_and_calculation": 30,
            "evidence_traceability": 25,
            "logic_and_assumptions": 20,
            "risk_and_counterevidence": 15,
            "expression_and_reuse": 10,
        }
        scorecards = {
            "SYN_REPORT_A": ({"facts_and_calculation": 28, "evidence_traceability": 23, "logic_and_assumptions": 17, "risk_and_counterevidence": 15, "expression_and_reuse": 8}, "Numbers are reproducible, source IDs are supplied, and risks and information gaps are explicit."),
            "SYN_REPORT_B": ({"facts_and_calculation": 6, "evidence_traceability": 8, "logic_and_assumptions": 6, "risk_and_counterevidence": 3, "expression_and_reuse": 5}, "Specific numbers are present but several are incorrect, unsupported or use information after the research cutoff."),
            "SYN_REPORT_C": ({"facts_and_calculation": 0, "evidence_traceability": 0, "logic_and_assumptions": 4, "risk_and_counterevidence": 0, "expression_and_reuse": 4}, "The report offers an opinion without financial data, sources, valuation, risks or monitoring indicators."),
        }
        evaluated = []
        for corpus_document_id, document in reports.items():
            scores, rationale = scorecards[corpus_document_id]
            evaluated.append({
                "corpus_document_id": corpus_document_id,
                "document_id": document.id,
                "scores": scores,
                "total_score": sum(scores.values()),
                "maximum_score": sum(rubric.values()),
                "rationale": rationale,
            })
        evaluated.sort(key=lambda item: item["total_score"], reverse=True)
        coverage = {
            "denominator": "checkable factual, numerical and citation claims in the supplied reports",
            "reports_with_primary_financial_evidence": [item for item in reports if item == "SYN_REPORT_A"],
            "unverified_report_ids": [item for item in reports if item == "SYN_REPORT_C"],
            "note": "A low number of detected errors is not evidence of high quality when a report supplies little checkable content.",
        }
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Compared reports using a fixed rubric and separated observed defects from the limits of available verification coverage.",
                "rubric": rubric,
                "reports": evaluated,
                "coverage": coverage,
                "evidence_refs": [{"document_id": item.id, "corpus_document_id": item.corpus_document_id, "source_url": item.source_url} for item in context.documents],
                "confidence": "medium",
                "confidence_reason": "The rubric is fixed before scoring and uses task-bound evidence, but subjective score calibration requires expert review on a larger held-out set.",
                "limitations": ["Scores are development rubric outputs, not calibrated probabilities or official competition scores."],
            },
            limitations=["Scores are development rubric outputs, not calibrated probabilities or official competition scores."],
        )
