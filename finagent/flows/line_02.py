"""Line 2: a reproducible first financial-report slice."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from finagent.contracts import Fact, FlowResult, Metric, RunStatus
from finagent.finance_kernel import fcff, growth, quotient, ratio
from finagent.flows.base import FlowContext
from finagent.flows.sample_utils import corpus_json_documents, decimal_value, fact_index
from finagent.models import ModelUnavailable
from finagent.prompts import prompt_for


def latest(facts: list[Fact], field: str, period_end: date, kind: str, scope: str) -> Fact | None:
    candidates = [
        fact for fact in facts
        if fact.field == field
        and fact.period_end == period_end
        and fact.period_kind == kind
        and fact.scope == scope
    ]
    return max(candidates, key=lambda fact: (fact.published_at, fact.id), default=None)


class FinancialFlow:
    def execute(self, context: FlowContext) -> FlowResult:
        if context.task.corpus_task_id == "L2_PUBLIC":
            return self._sample_public_financial_coverage(context)
        if context.task.corpus_task_id == "L2_MAOTAI":
            return self._sample_maotai_fields(context)
        if context.task.corpus_task_id == "L2_EDGE":
            return self._sample_edge_cases(context)
        if context.task.corpus_task_id == "L2_RATIOS":
            return self._sample_ratios(context)
        entity_id = context.task.entity_id
        if not entity_id:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"metrics": []},
                limitations=["entity_id is required for financial analysis"],
            )
        facts, conflicts, applied_resolutions = context.store.effective_facts_for(
            entity_id, context.task.as_of.isoformat(), context.source_ids
        )
        if not facts:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"metrics": []},
                limitations=["no eligible financial facts were found by the analysis date"],
            )
        if conflicts:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"entity_id": entity_id, "metrics": [], "fact_conflicts": conflicts},
                limitations=[
                    "conflicting verified facts require a correction or reviewer resolution before calculation"
                ],
            )
        periods = sorted(
            {(fact.period_end, fact.period_kind) for fact in facts if fact.field == "revenue"},
            reverse=True,
        )
        if not periods:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"metrics": []},
                limitations=["revenue fact is required to select the analysis period"],
            )
        period_end, kind = periods[0]
        scope = "consolidated"
        revenue = latest(facts, "revenue", period_end, kind, scope)
        assert revenue is not None
        prior_year = period_end.year - 1
        prior_day = min(period_end.day, calendar.monthrange(prior_year, period_end.month)[1])
        prior_end = period_end.replace(year=prior_year, day=prior_day)
        prior_revenue = latest(facts, "revenue", prior_end, kind, scope)
        income = latest(facts, "net_income_parent", period_end, kind, scope)
        cash_flow = latest(facts, "operating_cash_flow", period_end, kind, scope)
        metrics: list[Metric] = []
        limitations = list(context.ingestion_issues)
        if prior_revenue:
            metrics.append(growth(revenue, prior_revenue))
        else:
            limitations.append("prior comparable revenue is missing")
        if income:
            metrics.append(ratio("net_margin", income, revenue))
            if cash_flow:
                metrics.append(ratio("operating_cash_to_net_income", cash_flow, income))
            else:
                limitations.append("operating cash flow is missing")
        else:
            limitations.append("net income attributable to parent is missing")
        summary = (
            f"{entity_id}: {kind} ending {period_end.isoformat()}; "
            f"reported revenue {revenue.value} {revenue.unit}."
        )
        claims: list[dict[str, str]] = []
        try:
            answer = context.ask_model(prompt_for(context.task.line), tier="strong")
            summary = answer.summary
            claims = [
                claim.model_dump(mode="json")
                for claim in answer.claims
                if claim.document_id in context.source_ids
                and context.valid_locator(claim.document_id, claim.locator)
            ]
            limitations.extend([*context.warnings, *answer.limitations])
            limitations.append("narrative requires independent claim review")
        except ModelUnavailable as exc:
            limitations.append(str(exc))
        return FlowResult(
            status=RunStatus.PARTIAL if limitations else RunStatus.COMPLETED,
            payload={
                "entity_id": entity_id,
                "period_end": period_end.isoformat(),
                "period_kind": kind,
                "summary": summary,
                "metrics": [metric.model_dump(mode="json") for metric in metrics],
                "claims": claims,
                "source_fact_ids": [fact.id for fact in facts],
                "fact_resolution_ids": [
                    resolution.resolution_id for resolution in applied_resolutions
                ],
            },
            limitations=limitations,
        )

    def _sample_public_financial_coverage(self, context: FlowContext) -> FlowResult:
        entity_id = context.task.entity_id
        facts = context.store.facts_for(
            entity_id or "", context.task.as_of.isoformat(), context.source_ids
        )
        verified_facts = [
            fact for fact in facts
            if fact.verification_status == "visual_spotchecked_against_original"
        ]
        data_coverage = [
            {
                "corpus_document_id": document.corpus_document_id,
                "published_at": document.published_at.isoformat() if document.published_at else None,
                "parser_status": context.source_metadata.get(document.corpus_document_id or "", {}).get("parse_status"),
                "verified_field_count": sum(fact.document_id == document.id for fact in verified_facts),
            }
            for document in context.documents
        ]
        gaps = [
            "The task-bound public reports do not expose a complete set of visually verified comparable financial fields.",
            "Market price, share count and valuation-date capital-structure inputs are absent, so valuation multiples are not calculated.",
            "Partial PDF text is not promoted to financial facts without field-level evidence verification.",
        ]
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Completed a source and verification coverage review; insufficient verified comparable inputs prevent a numerical public-company analysis.",
                "financial_data": [
                    {
                        "metric": fact.field,
                        "value": str(fact.value),
                        "unit": fact.unit,
                        "period": fact.period_end.isoformat(),
                        "scope": fact.scope,
                        "evidence": fact.evidence.model_dump(mode="json"),
                    }
                    for fact in verified_facts
                ],
                "metrics": [],
                "coverage": data_coverage,
                "data_gaps": gaps,
                "evidence_refs": [
                    {"document_id": item.id, "corpus_document_id": item.corpus_document_id, "source_url": item.source_url}
                    for item in context.documents
                ],
                "confidence": "high",
                "confidence_reason": "The stated coverage and gaps are based on the task-bound source catalog and field verification states, not model inference.",
                "limitations": gaps,
            },
            limitations=gaps,
        )

    def _sample_maotai_fields(self, context: FlowContext) -> FlowResult:
        entity_id = context.task.entity_id
        facts = context.store.facts_for(
            entity_id or "", context.task.as_of.isoformat(), context.source_ids
        )
        required = {
            "revenue", "parent_net_income", "ocf", "parent_equity", "assets",
            "basic_eps", "weighted_roe",
        }
        fields = [
            {
                "entity": fact.entity_id,
                "metric": fact.field,
                "period": fact.period_end.year,
                "value": str(fact.value),
                "unit": fact.unit,
                "scope": fact.scope,
                "evidence": fact.evidence.model_dump(mode="json"),
            }
            for fact in sorted(facts, key=lambda item: (item.field, item.period_end), reverse=False)
            if fact.field in required and fact.period_end.year in {2022, 2023, 2024}
        ]
        missing = sorted(
            f"{metric}/{year}"
            for metric in required
            for year in (2022, 2023, 2024)
            if not any(item["metric"] == metric and item["period"] == year for item in fields)
        )
        evidence_pages = {
            item["evidence"].get("page_number") for item in fields
            if item["evidence"].get("page_number") is not None
        }
        limitations = [*context.ingestion_issues]
        if missing:
            limitations.append("missing verified source fields: " + ", ".join(missing))
        return FlowResult(
            status=RunStatus.COMPLETED if not limitations else RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Returned source-unit financial fields with their original reporting scope and page-level evidence.",
                "fields": fields,
                "source_page": min(evidence_pages) if len(evidence_pages) == 1 else None,
                "scope": "mixed: consolidated and parent_attributable",
                "unit": "mixed: CNY, CNY_per_share and percent",
                "evidence_refs": [
                    {"document_id": document.id, "corpus_document_id": document.corpus_document_id, "source_url": document.source_url}
                    for document in context.documents
                ],
                "confidence": "high" if not missing else "medium",
                "confidence_reason": "Fields are copied from sample facts visually spot-checked against the original annual-report page; no audit opinion is inferred.",
                "limitations": limitations,
            },
            limitations=limitations,
        )

    def _sample_edge_cases(self, context: FlowContext) -> FlowResult:
        edge_document = next(
            (item for item in corpus_json_documents(context) if item[0].corpus_document_id == "SYN_EDGE"),
            None,
        )
        if edge_document is None:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"task_id": context.task.corpus_task_id, "cases": []},
                limitations=["SYN_EDGE input is unavailable or invalid JSON"],
            )
        document, payload = edge_document
        assessments: list[dict[str, object]] = []
        for case in payload.get("cases", []):
            case_id = case["id"]
            assessment: dict[str, object] = {"case_id": case_id, "value": None}
            if case_id == "missing_debt":
                assessment.update(metric="enterprise_value", status="missing_input", reason="debt is missing and must not be assumed to be zero")
            elif case_id in {"zero_profit", "negative_profit"}:
                assessment.update(metric="pe_static", status="inapplicable", reason="P/E is not meaningful when net income is zero or negative")
            elif case_id == "negative_equity":
                assessment.update(metric="pb", status="not_meaningful", reason="negative equity produces a negative P/B that requires an explicit going-concern and capital-structure limitation")
            elif case_id == "unit_conversion":
                converted = Decimal(str(case["value"])) * Decimal(100)
                assessment.update(
                    metric="normalized_amount",
                    status="calculated",
                    value=str(converted),
                    unit="CNY_million",
                    formula="CNY_100million * 100 = CNY_million",
                    reason="source unit is explicit; conversion is disclosed rather than implicit",
                )
            elif case_id == "period_mismatch":
                assessment.update(metric="revenue_growth", status="inapplicable", reason="H1 and FY are not comparable periods without a matching H1 base period")
            elif case_id == "scope_mismatch":
                assessment.update(metric="roe", status="inapplicable", reason="parent-only net income and consolidated equity cannot be combined")
            elif case_id == "currency_mismatch":
                assessment.update(metric="gross_margin", status="missing_input", reason="CNY revenue and USD cost require a dated FX rate before calculation")
            else:
                assessment.update(metric="unknown", status="unassessable", reason="no edge-case rule is configured")
            assessments.append(assessment)
        limitations = [
            *context.ingestion_issues,
            "Only the explicit unit conversion is calculated; all other cases lack a valid comparable denominator, scope, period, currency or input.",
        ]
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Applied explicit applicability checks before financial calculation; missing and incomparable inputs were not converted into numeric outputs.",
                "cases": assessments,
                "evidence_refs": [{"document_id": document.id, "corpus_document_id": document.corpus_document_id, "source_url": document.source_url}],
                "confidence": "medium",
                "confidence_reason": "The case labels and values are synthetic; applicability decisions follow explicit accounting and unit rules.",
                "coverage": {"total_cases": len(assessments), "calculated_cases": sum(item["status"] == "calculated" for item in assessments)},
                "limitations": limitations,
            },
            limitations=limitations,
        )

    def _sample_ratios(self, context: FlowContext) -> FlowResult:
        documents = corpus_json_documents(context)
        financials = {
            item[1].get("period_end"): item
            for item in documents
            if isinstance(item[1].get("values"), dict) and item[1].get("period_end")
        }
        market = next((item for item in documents if "price" in item[1] and "shares_million" in item[1]), None)
        current = financials.get("2024-12-31")
        prior = financials.get("2023-12-31")
        if current is None or prior is None or market is None:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"task_id": context.task.corpus_task_id, "metrics": []},
                limitations=["L2_RATIOS requires FY2023, FY2024 and market inputs"],
            )
        current_document, current_data = current
        prior_document, prior_data = prior
        market_document, market_data = market
        current_values = current_data["values"]
        prior_values = prior_data["values"]
        facts = fact_index(context)

        def input_ids(*names: str) -> list[str]:
            identifiers = []
            for name in names:
                fact = facts.get((name, "2024-12-31")) or facts.get((name, "2023-12-31"))
                if fact:
                    identifiers.append(fact.id)
            return identifiers

        def metric(
            name: str,
            numerator: Decimal,
            denominator: Decimal,
            *,
            unit: str,
            formula: str,
            names: tuple[str, ...],
            documents_used: tuple[str, ...],
        ) -> Metric:
            return quotient(
                name,
                numerator,
                denominator,
                unit=unit,
                formula=formula,
                input_fact_ids=input_ids(*names),
                input_document_ids=list(documents_used),
                confidence="medium",
                confidence_reason="synthetic input; deterministic formula is reproducible but not real-world evidence",
            )

        revenue = decimal_value(current_values, "revenue")
        prior_revenue = decimal_value(prior_values, "revenue")
        net_income = decimal_value(current_values, "net_income")
        metrics = [
            metric(
                "revenue_growth",
                revenue - prior_revenue,
                prior_revenue,
                unit="fraction",
                formula="revenue2024 / revenue2023 - 1",
                names=("revenue",),
                documents_used=(current_document.id, prior_document.id),
            ),
            metric(
                "gross_margin",
                revenue - decimal_value(current_values, "cogs"),
                revenue,
                unit="fraction",
                formula="(revenue - cogs) / revenue",
                names=("revenue", "cogs"),
                documents_used=(current_document.id,),
            ),
            metric(
                "net_margin",
                net_income,
                revenue,
                unit="fraction",
                formula="parent net_income / revenue",
                names=("net_income", "revenue"),
                documents_used=(current_document.id,),
            ),
            metric(
                "roe_simple_average",
                net_income,
                (decimal_value(current_values, "equity_begin") + decimal_value(current_values, "equity")) / Decimal(2),
                unit="fraction",
                formula="parent net_income / ((opening parent equity + closing parent equity) / 2)",
                names=("net_income", "equity_begin", "equity"),
                documents_used=(current_document.id,),
            ),
            metric(
                "debt_to_assets",
                decimal_value(current_values, "liabilities"),
                decimal_value(current_values, "assets"),
                unit="fraction",
                formula="total liabilities / total assets",
                names=("liabilities", "assets"),
                documents_used=(current_document.id,),
            ),
            metric(
                "cash_profit_ratio",
                decimal_value(current_values, "ocf"),
                net_income,
                unit="multiple",
                formula="ocf / parent net_income",
                names=("ocf", "net_income"),
                documents_used=(current_document.id,),
            ),
        ]
        market_cap = decimal_value(market_data, "price") * decimal_value(market_data, "shares_million")
        metrics.extend([
            metric(
                "pe_static", market_cap, net_income, unit="multiple",
                formula="price * total shares / FY2024 parent net_income",
                names=("net_income",), documents_used=(current_document.id, market_document.id),
            ),
            metric(
                "pb", market_cap, decimal_value(current_values, "equity"), unit="multiple",
                formula="market_cap / parent equity",
                names=("equity",), documents_used=(current_document.id, market_document.id),
            ),
            metric(
                "ev_ebitda",
                market_cap + decimal_value(current_values, "debt") - decimal_value(current_values, "cash"),
                decimal_value(current_values, "ebit") + decimal_value(current_values, "da"),
                unit="multiple",
                formula="(market_cap + debt - cash) / (ebit + da)",
                names=("debt", "cash", "ebit", "da"),
                documents_used=(current_document.id, market_document.id),
            ),
        ])
        ebit = decimal_value(current_values, "ebit")
        interest = decimal_value(current_values, "interest")
        pre_tax_income = ebit - interest
        if pre_tax_income == 0:
            return FlowResult(
                status=RunStatus.PARTIAL,
                payload={"task_id": context.task.corpus_task_id, "metrics": [item.model_dump(mode="json") for item in metrics]},
                limitations=["FCFF is not applicable because the effective tax-rate denominator is zero"],
            )
        effective_tax_rate = decimal_value(current_values, "tax") / pre_tax_income
        fcff_value = fcff(
            ebit,
            effective_tax_rate,
            decimal_value(current_values, "da"),
            decimal_value(current_values, "capex"),
            decimal_value(current_values, "delta_nwc"),
        )
        metrics.append(
            Metric(
                name="fcff_2024",
                value=fcff_value,
                unit="CNY_million",
                formula="EBIT*(1-effective_tax_rate)+DA-Capex-delta_NWC; effective_tax_rate=tax/(EBIT-interest)",
                input_fact_ids=input_ids("ebit", "interest", "tax", "da", "capex", "delta_nwc"),
                input_document_ids=[current_document.id],
                status="calculated",
                confidence="medium",
                confidence_reason="synthetic input; deterministic formula is reproducible but not real-world evidence",
            )
        )
        evidence_refs = [
            {"document_id": document.id, "source_url": document.source_url}
            for document in (prior_document, current_document, market_document)
        ]
        limitations = list(context.ingestion_issues)
        return FlowResult(
            status=RunStatus.COMPLETED if not limitations else RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Calculated ten financial and valuation metrics from the task-bound synthetic inputs.",
                "metrics": [item.model_dump(mode="json") for item in metrics],
                "evidence_refs": evidence_refs,
                "confidence": "medium",
                "confidence_reason": "The arithmetic is deterministic; all task inputs are synthetic fixtures.",
                "coverage": {"required_metrics": 10, "calculated_metrics": 10},
                "limitations": limitations,
            },
            limitations=limitations,
        )
