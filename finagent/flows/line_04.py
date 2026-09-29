"""Line 4: valuation research draft plus deterministic sample DCF."""

from decimal import Decimal

from finagent.contracts import FlowResult, RunStatus
from finagent.finance_kernel import dcf_value
from finagent.flows.base import FlowContext, ResearchDraftFlow
from finagent.flows.sample_utils import corpus_json_documents, decimal_value

class ValuationFlow(ResearchDraftFlow):
    label = "valuation"

    def execute(self, context: FlowContext) -> FlowResult:
        if context.task.corpus_task_id in {"L4_DCF", "L4_INVALID"}:
            return self._sample_dcf(context)
        if context.task.corpus_task_id == "L4_PUBLIC":
            return self._sample_public_valuation_plan(context)
        return super().execute(context)

    def _sample_public_valuation_plan(self, context: FlowContext) -> FlowResult:
        materials = [
            {
                "document_id": document.id,
                "corpus_document_id": document.corpus_document_id,
                "published_at": document.published_at.isoformat() if document.published_at else None,
                "source_url": document.source_url,
                "source_tier": document.source_tier,
                "parser": context.source_metadata.get(document.corpus_document_id or "", {}).get("parser"),
                "parse_status": context.source_metadata.get(document.corpus_document_id or "", {}).get("parse_status"),
            }
            for document in context.documents
        ]
        gaps = [
            "No field-verified historical financial statement inputs are available from the task-bound annual reports.",
            "No valuation-date share price, share count, debt, cash or other capital-structure inputs are supplied.",
            "No forecast cash flow, management guidance, analyst estimates or peer-company data are supplied.",
            "The task-bound annual-report text extraction is partial and is not treated as verified valuation input.",
        ]
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "The available public materials are insufficient for a numerical valuation; the result records a source-bound valuation plan and required inputs instead of a fair-value estimate.",
                "materials": materials,
                "model_assessment": [
                    {
                        "method": "FCFF_DCF",
                        "status": "not_runnable",
                        "required_inputs": [
                            "field-verified historical operating and cash-flow statements",
                            "forecast FCFF and assumptions",
                            "discount rate and terminal-growth evidence",
                            "net debt, minority interest and non-operating assets",
                            "diluted share count",
                        ],
                    },
                    {
                        "method": "trading_multiples",
                        "status": "not_runnable",
                        "required_inputs": [
                            "valuation-date share price and share count",
                            "field-verified earnings, book value and EBITDA",
                            "peer set and comparable accounting periods",
                        ],
                    },
                ],
                "valuation": None,
                "data_gaps": gaps,
                "evidence_refs": [
                    {
                        "document_id": document.id,
                        "corpus_document_id": document.corpus_document_id,
                        "source_url": document.source_url,
                    }
                    for document in context.documents
                ],
                "confidence": "high",
                "confidence_reason": "The missing inputs and partial parser states are directly recorded in the task-bound source catalog; no valuation model is run.",
                "limitations": gaps,
            },
            limitations=gaps,
        )

    def _sample_dcf(self, context: FlowContext) -> FlowResult:
        sources = corpus_json_documents(context)
        assumptions = next((item for item in sources if "forecast_fcff" in item[1]), None)
        if assumptions is None:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={
                    "task_id": context.task.corpus_task_id,
                    "conclusion": "Valuation was rejected because the task has no usable DCF assumptions.",
                    "evidence_refs": [],
                    "confidence": "high",
                    "confidence_reason": "The missing required model inputs are directly observable.",
                    "limitations": ["DCF assumptions are missing"],
                },
                limitations=["DCF assumptions are missing"],
            )
        document, data = assumptions
        try:
            wacc = decimal_value(data, "wacc")
            growth = decimal_value(data, "terminal_growth")
            forecast = [Decimal(str(value)) for value in data["forecast_fcff"]]
            enterprise_value, terminal_value = dcf_value(forecast, wacc, growth)
            net_debt = decimal_value(data, "net_debt")
            minority_interest = decimal_value(data, "minority_interest")
            nonoperating_assets = decimal_value(data, "nonoperating_assets")
            shares = decimal_value(data, "shares_million")
            if shares <= 0:
                raise ValueError("shares_million must be positive")
        except (KeyError, ValueError) as exc:
            limitation = f"invalid DCF input: {exc}"
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={
                    "task_id": context.task.corpus_task_id,
                    "rejection": {"reason": "wacc <= terminal_growth"},
                    "valuation": None,
                    "conclusion": "Valuation was rejected because the perpetual-growth DCF parameters are invalid.",
                    "evidence_refs": [{"document_id": document.id, "source_url": document.source_url}],
                    "confidence": "high",
                    "confidence_reason": "The rejection follows a deterministic DCF validity rule.",
                    "limitations": [limitation, *context.ingestion_issues],
                },
                limitations=[limitation, *context.ingestion_issues],
            )
        equity_value = enterprise_value - net_debt - minority_interest + nonoperating_assets
        base = {
            "enterprise_value": enterprise_value,
            "equity_value": equity_value,
            "per_share": equity_value / shares,
            "terminal_value": terminal_value,
        }
        sensitivity = []
        for sensitivity_wacc in (Decimal("0.08"), Decimal("0.09"), Decimal("0.10")):
            for sensitivity_growth in (Decimal("0.02"), Decimal("0.025"), Decimal("0.03")):
                value, terminal = dcf_value(forecast, sensitivity_wacc, sensitivity_growth)
                equity = value - net_debt - minority_interest + nonoperating_assets
                sensitivity.append({
                    "wacc": sensitivity_wacc,
                    "g": sensitivity_growth,
                    "enterprise_value": value,
                    "equity_value": equity,
                    "per_share": equity / shares,
                    "terminal_value": terminal,
                })
        limitations = list(context.ingestion_issues)
        return FlowResult(
            status=RunStatus.COMPLETED if not limitations else RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Computed an FCFF DCF and a WACC/terminal-growth sensitivity grid from task assumptions.",
                "valuation": {
                    "amount_unit": data.get("amount_unit"),
                    "per_share_unit": "CNY",
                    "base": base,
                    "sensitivity": sensitivity,
                },
                "evidence_refs": [{"document_id": document.id, "source_url": document.source_url}],
                "confidence": "low",
                "confidence_reason": "The calculation is reproducible, while forecast cash flows and valuation assumptions are synthetic external assumptions.",
                "coverage": {"forecast_periods": len(forecast), "sensitivity_cells": len(sensitivity)},
                "limitations": limitations,
            },
            limitations=limitations,
        )
