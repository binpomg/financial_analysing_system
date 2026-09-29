"""Line 6: evidence-bound investment memo outputs."""

from __future__ import annotations

from finagent.contracts import FlowResult, RunStatus
from finagent.flows.base import FlowContext, ResearchDraftFlow
from finagent.flows.sample_utils import corpus_json_documents



class MemoFlow(ResearchDraftFlow):
    label = "investment_memo"

    def execute(self, context: FlowContext) -> FlowResult:
        if context.task.corpus_task_id == "L6_MEMO":
            return self._sample_memo(context)
        if context.task.corpus_task_id == "L6_MISSING":
            return self._sample_missing_materials(context)
        if context.task.corpus_task_id == "L6_PUBLIC":
            return self._sample_public_memo_plan(context)
        return super().execute(context)

    def _sample_public_memo_plan(self, context: FlowContext) -> FlowResult:
        materials = [
            {
                "document_id": document.id,
                "corpus_document_id": document.corpus_document_id,
                "published_at": document.published_at.isoformat() if document.published_at else None,
                "source_url": document.source_url,
                "source_tier": document.source_tier,
                "parse_status": context.source_metadata.get(document.corpus_document_id or "", {}).get("parse_status"),
            }
            for document in context.documents
        ]
        limitations = [
            *context.ingestion_issues,
            "The supplied annual-report text is partial and has no field-level verification for a company comparison or investment thesis.",
            "No valuation-date market data, capital structure, earnings forecast, peer research or verified company-specific catalyst is available.",
            "Any macro source without a disclosed publication date is excluded from historical evidence use by the corpus adapter.",
        ]
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "The public materials support only a constrained research plan; no investment recommendation, valuation, relative ranking or historical backtest conclusion is issued.",
                "memo": {
                    "recommendation": "no_recommendation",
                    "evidence_boundary": "source_catalog_only",
                    "eligible_materials": materials,
                    "facts": [],
                    "assumptions": [],
                    "valuation": None,
                    "research_plan": [
                        "field-verify comparable annual-report statements with page and table locators",
                        "obtain dated market price, diluted shares and capital-structure inputs",
                        "build a peer set and record each comparable company's accounting period and business differences",
                        "collect dated forecast, policy, order and operating evidence before forming a catalyst or risk thesis",
                    ],
                    "monitoring_prerequisites": [
                        "report-version updates",
                        "field-verification status",
                        "valuation-date market-data snapshot",
                        "source publication date and retrieval record",
                    ],
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
                "confidence_reason": "The recommendation boundary follows directly from the task-bound source catalog, parser status and missing valuation inputs.",
                "limitations": limitations,
            },
            limitations=limitations,
        )

    def _sample_memo(self, context: FlowContext) -> FlowResult:
        documents = {item.corpus_document_id: item for item in context.documents}
        required = {"SYN_EMAIL", "SYN_FIN_2023", "SYN_FIN_2024", "SYN_DCF", "SYN_CHAIN", "SYN_EVENT", "SYN_CALL", "SYN_NEWS"}
        absent = sorted(required - set(documents))
        if absent:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"task_id": context.task.corpus_task_id, "memo": {}},
                limitations=["required memo inputs are missing: " + ", ".join(absent)],
            )
        json_documents = {document.corpus_document_id: value for document, value in corpus_json_documents(context)}
        current = json_documents["SYN_FIN_2024"]["values"]
        prior = json_documents["SYN_FIN_2023"]["values"]
        dcf = json_documents["SYN_DCF"]
        memo = {
            "recommendation": "observe_only",
            "conclusion_with_evidence": [
                {
                    "claim": "FY2024 revenue and parent net income increased versus FY2023 in the supplied synthetic statements.",
                    "evidence_corpus_document_ids": ["SYN_FIN_2024", "SYN_FIN_2023"],
                },
                {
                    "claim": "Copper-price exposure is conditional on hedging, pass-through and procurement timing.",
                    "evidence_corpus_document_ids": ["SYN_EVENT", "SYN_CHAIN", "SYN_CALL"],
                },
            ],
            "facts_and_assumptions": {
                "facts": {
                    "revenue_2024": current["revenue"],
                    "net_income_2024": current["net_income"],
                    "revenue_2023": prior["revenue"],
                    "all_inputs_synthetic": True,
                },
                "valuation_assumptions": {
                    "forecast_fcff": dcf["forecast_fcff"],
                    "wacc": dcf["wacc"],
                    "terminal_growth": dcf["terminal_growth"],
                    "source_corpus_document_id": "SYN_DCF",
                },
            },
            "valuation_traceability": {
                "historical_financial_sources": ["SYN_FIN_2023", "SYN_FIN_2024"],
                "valuation_assumption_source": "SYN_DCF",
                "status": "scenario_only",
            },
            "risks_and_invalidation": [
                "Indicative orders may be cancelled and should not be treated as recognized revenue.",
                "Material cash-collection deterioration invalidates the provisional operating thesis.",
                "Copper-price pressure may differ from the scenario if hedge, pass-through or inventory conditions change.",
            ],
            "monitoring": ["operating_cash_flow", "receivables_aging", "order_execution_rate", "copper_price", "pass_through_terms"],
            "data_gaps": ["order_execution_rate", "customer_concentration", "historical_valuation_range", "verified_contract_terms"],
        }
        limitations = [
            "All company, event and valuation materials in this task are synthetic and cannot support a real investment recommendation.",
            "The supplied news item is an unverified report and is treated only as a research lead.",
        ]
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Produced a provisional evidence-bound memo with an observation-only conclusion and explicit invalidation conditions.",
                "memo": memo,
                "evidence_refs": [
                    {"document_id": item.id, "corpus_document_id": item.corpus_document_id, "source_url": item.source_url}
                    for item in context.documents
                ],
                "confidence": "low",
                "confidence_reason": "The structure and arithmetic dependencies are reproducible, but all company materials are synthetic and several decision-critical inputs are missing.",
                "limitations": limitations,
            },
            limitations=limitations,
        )

    def _sample_missing_materials(self, context: FlowContext) -> FlowResult:
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "conclusion": "The available request and unverified news are insufficient for a numerical valuation or investment conclusion.",
                "research_plan": ["obtain audited financial statements", "obtain share price and capital structure", "verify orders with primary documents", "establish peer set and valuation assumptions"],
                "evidence_refs": [{"document_id": item.id, "corpus_document_id": item.corpus_document_id, "source_url": item.source_url} for item in context.documents],
                "confidence": "high",
                "confidence_reason": "The material gaps are directly observable from the task-bound sources.",
                "limitations": ["No numerical valuation or investment recommendation is generated."],
            },
            limitations=["No numerical valuation or investment recommendation is generated."],
        )
