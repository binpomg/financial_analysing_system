"""Line 5: report error review draft."""

import re
from decimal import Decimal

from finagent.contracts import FlowResult, RunStatus
from finagent.flows.base import FlowContext, ResearchDraftFlow
from finagent.flows.sample_utils import corpus_json_documents, decimal_value, document_text

class ReportReviewFlow(ResearchDraftFlow):
    label = "report_review"

    def execute(self, context: FlowContext) -> FlowResult:
        if context.task.corpus_task_id == "L5_ERRORS":
            return self._sample_error_review(context)
        if context.task.corpus_task_id == "L5_CONTROL":
            return self._sample_control_review(context)
        if context.task.corpus_task_id == "L5_VERSION":
            return self._sample_version_review(context)
        return super().execute(context)

    def _sample_control_review(self, context: FlowContext) -> FlowResult:
        documents = corpus_json_documents(context)
        financials = {
            item[1].get("period_end"): item
            for item in documents
            if isinstance(item[1].get("values"), dict) and item[1].get("period_end")
        }
        current = financials.get("2024-12-31")
        prior = financials.get("2023-12-31")
        market = next((item for item in documents if "price" in item[1] and "shares_million" in item[1]), None)
        report = next((item for item in context.documents if item.corpus_document_id == "SYN_REPORT_A"), None)
        if current is None or prior is None or market is None or report is None:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"task_id": context.task.corpus_task_id, "findings": []},
                limitations=["control review requires report A, FY2023/FY2024 and valuation-date inputs"],
            )
        current_document, current_data = current
        prior_document, prior_data = prior
        market_document, market_data = market
        values = current_data["values"]
        revenue = decimal_value(values, "revenue")
        net_income = decimal_value(values, "net_income")
        growth = revenue / decimal_value(prior_data["values"], "revenue") - Decimal(1)
        market_cap = decimal_value(market_data, "price") * decimal_value(market_data, "shares_million")
        verified = {
            "revenue_growth": str(growth),
            "net_margin": str(net_income / revenue),
            "pe_static": str(market_cap / net_income),
        }
        return FlowResult(
            status=RunStatus.COMPLETED,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Verified report A's numeric claims and retained its conditional statements as opinions rather than errors.",
                "findings": [],
                "control_result": {
                    "numeric_claims_verified": verified,
                    "conditional_opinions_preserved": True,
                    "unsupported_findings_added": 0,
                },
                "evidence_refs": [
                    {"document_id": item.id, "corpus_document_id": item.corpus_document_id, "source_url": item.source_url}
                    for item in (report, prior_document, current_document, market_document)
                ],
                "confidence": "high",
                "confidence_reason": "The three numeric claims are recomputed from task-bound synthetic inputs; conditional language is not classified as a factual error.",
                "limitations": ["The review checks supplied claims only and does not establish complete coverage of every narrative statement."],
            },
            limitations=["The review checks supplied claims only and does not establish complete coverage of every narrative statement."],
        )

    def _sample_version_review(self, context: FlowContext) -> FlowResult:
        candidates = [
            (document, value)
            for document, value in corpus_json_documents(context)
            if {"entity_id", "period", "metric", "value", "unit"}.issubset(value)
        ]
        corrected = next((item for item in candidates if item[0].corpus_document_id == "SYN_CORRECTION_V2"), None)
        original = next((item for item in candidates if item[0].corpus_document_id == "SYN_CORRECTION_V1"), None)
        third_party = next((item for item in candidates if item[0].corpus_document_id == "SYN_CONFLICT"), None)
        if corrected is None or original is None or third_party is None:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"task_id": context.task.corpus_task_id, "findings": []},
                limitations=["the correction chain and third-party conflict are required"],
            )
        corrected_document, corrected_value = corrected
        original_document, original_value = original
        third_party_document, third_party_value = third_party
        return FlowResult(
            status=RunStatus.COMPLETED,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "The explicit correction supersedes the original disclosed value; the unsupported third-party value remains an unresolved conflict.",
                "findings": [{
                    "metric": corrected_value["metric"],
                    "period": corrected_value["period"],
                    "current_corpus_document_id": corrected_document.corpus_document_id,
                    "superseded_corpus_document_id": original_document.corpus_document_id,
                    "unresolved_third_party_corpus_document_id": third_party_document.corpus_document_id,
                    "current_value": corrected_value["value"],
                    "unit": corrected_value["unit"],
                    "recalculation_required": ["revenue_growth", "margin", "valuation_multiples"],
                }],
                "evidence_refs": [
                    {"document_id": item.id, "corpus_document_id": item.corpus_document_id, "source_url": item.source_url}
                    for item in (original_document, corrected_document, third_party_document)
                ],
                "confidence": "medium",
                "confidence_reason": "The selection follows explicit correction metadata; the conflicting third-party source has no supporting original evidence.",
                "limitations": ["Historical versions remain available and any analysis using the superseded revenue requires recalculation."],
            },
            limitations=["Historical versions remain available and any analysis using the superseded revenue requires recalculation."],
        )

    def _sample_error_review(self, context: FlowContext) -> FlowResult:
        report = next((document for document in context.documents if document.name.endswith("研究稿B.md")), None)
        if report is None:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={
                    "task_id": context.task.corpus_task_id,
                    "conclusion": "The task-bound report to review is missing.",
                    "evidence_refs": [],
                    "confidence": "high",
                    "confidence_reason": "The report binding is required for claim review.",
                    "limitations": ["research report B is missing"],
                },
                limitations=["research report B is missing"],
            )
        text = document_text(context, report)
        if text is None:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={
                    "task_id": context.task.corpus_task_id,
                    "conclusion": "The task-bound report cannot be decoded.",
                    "evidence_refs": [{"document_id": report.id, "source_url": report.source_url}],
                    "confidence": "high",
                    "confidence_reason": "The report bytes are unreadable by the configured decoder.",
                    "limitations": ["research report is unreadable"],
                },
                limitations=["research report is unreadable"],
            )
        claims = {
            match.group(1): match.group(2)
            for match in re.finditer(r"^\[(C\d{2})\]\s+(.+)$", text, re.MULTILINE)
        }
        documents = corpus_json_documents(context)
        financials = {
            item[1].get("period_end"): item
            for item in documents
            if isinstance(item[1].get("values"), dict) and item[1].get("period_end")
        }
        current = financials.get("2024-12-31")
        prior = financials.get("2023-12-31")
        market = next((item for item in documents if "price" in item[1] and "shares_million" in item[1]), None)
        if current is None or prior is None or market is None or len(claims) != 10:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={
                    "task_id": context.task.corpus_task_id,
                    "conclusion": "The report or its required financial comparison inputs are incomplete.",
                    "evidence_refs": [{"document_id": report.id, "source_url": report.source_url}],
                    "confidence": "high",
                    "confidence_reason": "Claim review needs the report, FY2023/FY2024 data and valuation-date market inputs.",
                    "limitations": ["required review inputs are incomplete"],
                },
                limitations=["required review inputs are incomplete"],
            )
        current_document, current_data = current
        prior_document, prior_data = prior
        market_document, market_data = market
        values = current_data["values"]
        prior_values = prior_data["values"]
        revenue = decimal_value(values, "revenue")
        net_income = decimal_value(values, "net_income")
        market_cap = decimal_value(market_data, "price") * decimal_value(market_data, "shares_million")
        average_equity = (decimal_value(values, "equity_begin") + decimal_value(values, "equity")) / Decimal(2)
        future_material_excluded = any("future_information" in issue for issue in context.ingestion_issues)

        def finding(claim_id: str, label: str, correction: str, evidence_documents: list[str], severity: str = "high") -> dict[str, object]:
            return {
                "claim_id": claim_id,
                "label": label,
                "report_locator": f"line:{claim_id}",
                "claim": claims[claim_id],
                "correction": correction,
                "evidence_document_ids": evidence_documents,
                "severity": severity,
                "confidence": "high" if label in {"arithmetic_error", "unit_and_growth_error", "lookahead_information"} else "medium",
            }

        findings = [
            finding("C01", "unit_and_growth_error", f"Revenue is {revenue} CNY_million and growth is {(revenue / decimal_value(prior_values, 'revenue') - Decimal(1)) * Decimal(100)}%; the report changes both unit and growth rate.", [current_document.id, prior_document.id]),
            finding("C02", "metric_confusion", f"Operating cash flow is {decimal_value(values, 'ocf')} CNY_million; {net_income} is net income.", [current_document.id]),
            finding("C03", "arithmetic_error", f"Net margin is {net_income / revenue * Decimal(100)}%.", [current_document.id]),
            finding("C04", "arithmetic_error", f"Static P/E is {market_cap / net_income}x.", [current_document.id, market_document.id]),
            finding("C05", "incomparable_definition", f"Ending-equity ROE is {net_income / decimal_value(values, 'equity') * Decimal(100)}%; average-equity ROE is {net_income / average_equity * Decimal(100)}%, and the definitions are not directly comparable.", [current_document.id]),
            finding("C06", "unsupported_causal_quantification", "A raw-material price change cannot be converted directly into an identical total-profit change without hedging, pass-through and procurement-cycle evidence.", [current_document.id], "medium"),
            finding("C07", "forecast_as_fact", "An indicative order requires conversion conditions and cannot be presented as certain current-period revenue.", [report.id], "medium"),
            finding("C08", "lookahead_information", "The task cutoff is 2025-04-30; the referenced June default announcement is excluded as future information.", [report.id], "high"),
            finding("C09", "unsupported_forecast", "A policy document alone does not establish a company-specific mandatory 50% profit increase.", [report.id], "medium"),
            finding("C10", "invalid_citation", "The cited page is not available in the task-bound evidence and cannot support the absolute risk claim.", [report.id], "medium"),
        ]
        limitations = [*context.ingestion_issues]
        if not future_material_excluded:
            limitations.append("future-information exclusion was not observed in task inputs; C08 requires manual review")
        return FlowResult(
            status=RunStatus.COMPLETED if future_material_excluded else RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Reviewed ten checkable report claims and identified numeric, definition, evidence and time-cutoff problems.",
                "findings": findings,
                "evidence_refs": [
                    {"document_id": item.id, "source_url": item.source_url}
                    for item in (report, prior_document, current_document, market_document)
                ],
                "confidence": "medium",
                "confidence_reason": "Numeric and time-cutoff findings are deterministic; causal and forecast findings depend on the supplied evidence scope.",
                "coverage": {"claims_identified": len(claims), "claims_reviewed": len(findings)},
                "limitations": limitations,
            },
            limitations=limitations,
        )
