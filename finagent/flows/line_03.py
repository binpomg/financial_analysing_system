"""Line 3: industry impact analysis with explicit scenario assumptions."""

from __future__ import annotations

from decimal import Decimal
from html.parser import HTMLParser

from finagent.contracts import FlowResult, RunStatus
from finagent.flows.base import FlowContext, ResearchDraftFlow
from finagent.flows.sample_utils import corpus_json_documents, document_text



class _HtmlTextParser(HTMLParser):
    """Extract text from task-bound HTML without executing its contents."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(part.strip() for part in self.parts if part.strip())


class IndustryFlow(ResearchDraftFlow):
    label = "industry_impact"

    def execute(self, context: FlowContext) -> FlowResult:
        if context.task.corpus_task_id == "L3_COPPER":
            return self._sample_copper_impact(context)
        if context.task.corpus_task_id == "L3_NEWS":
            return self._sample_news_status(context)
        if context.task.corpus_task_id == "L3_POLICY":
            return self._sample_policy_impact(context)
        return super().execute(context)

    @staticmethod
    def _evidence_refs(context: FlowContext) -> list[dict[str, str | None]]:
        return [
            {
                "document_id": document.id,
                "corpus_document_id": document.corpus_document_id,
                "source_url": document.source_url,
            }
            for document in context.documents
        ]

    def _blocked_sample_result(
        self, context: FlowContext, reason: str
    ) -> FlowResult:
        limitations = [reason, *context.ingestion_issues]
        return FlowResult(
            status=RunStatus.BLOCKED,
            payload={
                "task_id": context.task.corpus_task_id,
                "conclusion": "The task-bound evidence is insufficient for the requested industry assessment.",
                "evidence_refs": self._evidence_refs(context),
                "confidence": "low",
                "confidence_reason": reason,
                "limitations": limitations,
            },
            limitations=limitations,
        )

    def _sample_news_status(self, context: FlowContext) -> FlowResult:
        documents = {document.corpus_document_id: document for document in context.documents}
        news = documents.get("SYN_NEWS")
        call = documents.get("SYN_CALL")
        if news is None or call is None:
            return self._blocked_sample_result(
                context, "L3_NEWS requires the supplied media report and earnings-call record."
            )
        news_text = document_text(context, news) or ""
        call_text = document_text(context, call) or ""
        required_news_markers = ("匿名", "未确认", "一手文件")
        required_call_markers = ("意向订单", "交付验收后确认收入")
        if not all(marker in news_text for marker in required_news_markers) or not all(
            marker in call_text for marker in required_call_markers
        ):
            return self._blocked_sample_result(
                context, "The bound texts do not contain the expected evidence needed to classify the reported order."
            )
        limitations = [
            *context.ingestion_issues,
            "The media report is a synthetic, anonymous-source research lead rather than a primary disclosure.",
            "No amount, customer identity, signed contract, delivery record or acceptance record is present in the task-bound sources.",
            "No revenue, profit, backlog or valuation impact is calculated from the reported order.",
        ]
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "The reported large procurement order is unconfirmed and may only be used as a verification lead; it is not established revenue or a company-impact fact.",
                "order_assessment": {
                    "reported_event": "possible_large_procurement_order",
                    "source_classification": "unverified_media_report",
                    "order_status": "unconfirmed",
                    "revenue_recognition_status": "not_established",
                    "recognition_condition_in_bound_call": "delivery_and_acceptance",
                    "management_statement_status": "expectation_not_fact",
                },
                "verification_required": [
                    "primary company disclosure or signed order identifying the customer, amount and binding status",
                    "delivery and customer-acceptance evidence before recognizing revenue",
                    "contract pricing and adjustment clauses before estimating margin or commodity pass-through",
                ],
                "evidence_refs": self._evidence_refs(context),
                "confidence": "high",
                "confidence_reason": "Both the media item and the task-bound management record explicitly describe the order as unconfirmed or indicative and state the delivery-and-acceptance recognition condition.",
                "limitations": limitations,
            },
            limitations=limitations,
        )

    def _sample_policy_impact(self, context: FlowContext) -> FlowResult:
        documents = {document.corpus_document_id: document for document in context.documents}
        policy = documents.get("GOV_EQUIPMENT")
        if policy is None:
            return self._blocked_sample_result(
                context, "L3_POLICY requires the task-bound official policy document."
            )
        raw_policy = document_text(context, policy) or ""
        parser = _HtmlTextParser()
        try:
            parser.feed(raw_policy)
            policy_text = parser.text()
        except Exception:
            return self._blocked_sample_result(
                context, "The task-bound official policy document could not be parsed as HTML text."
            )
        required_markers = ("国务院", "设备更新", "到2027年", "钢铁、有色、石化")
        if not all(marker in policy_text for marker in required_markers):
            return self._blocked_sample_result(
                context, "The task-bound policy text does not contain the official industry and timing statements needed for this assessment."
            )
        limitations = [
            *context.ingestion_issues,
            "The policy establishes an industry-level direction and target, not a company-specific order, revenue, profit or valuation outcome.",
            "The two bound annual reports have partial text parsing status and are not used to infer an issuer's product exposure, market share or financial impact.",
            "Implementation plans, eligibility, budgets, customer capital-expenditure decisions and company execution evidence are absent from the task-bound sources.",
        ]
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "The official action plan supports a conditional industry-level equipment-renewal theme through 2027, while the supplied materials do not establish a company-specific financial impact.",
                "policy_assessment": {
                    "policy_level": "national_action_plan",
                    "issuing_body": "State Council",
                    "document_date": "2024-03-07",
                    "publication_date": policy.published_at.isoformat() if policy.published_at else None,
                    "target_horizon": "2027",
                    "policy_mechanisms": [
                        "equipment renewal and technical transformation",
                        "energy efficiency, emissions, safety, digitalization and intelligent upgrading",
                        "support through fiscal, financial and investment policy tools",
                    ],
                    "candidate_affected_sectors_named_in_policy": [
                        "steel",
                        "nonferrous metals",
                        "petrochemicals",
                        "chemicals",
                        "building materials",
                        "power",
                        "machinery",
                        "aviation",
                        "ships",
                        "light textile",
                        "electronics",
                    ],
                    "company_specific_impact_status": "not_established",
                },
                "evidence_required_for_company_assessment": [
                    "verified company product and segment exposure to the policy-named equipment categories",
                    "customer capital-expenditure plans, tender awards or signed orders linked to the policy",
                    "applicable implementing rules, eligibility criteria, budget and approval evidence",
                    "order execution, delivery, acceptance and accounting recognition evidence",
                ],
                "evidence_refs": self._evidence_refs(context),
                "confidence": "high",
                "confidence_reason": "The industry direction, named sectors and target horizon are directly stated in the official task-bound policy; no company-level causal claim is made.",
                "limitations": limitations,
            },
            limitations=limitations,
        )

    def _sample_copper_impact(self, context: FlowContext) -> FlowResult:
        documents = {document.corpus_document_id: (document, value) for document, value in corpus_json_documents(context)}
        event = documents.get("SYN_EVENT")
        chain = documents.get("SYN_CHAIN")
        if event is None or chain is None:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"task_id": context.task.corpus_task_id, "impact": {}},
                limitations=["L3_COPPER requires the event and industry-chain inputs"],
            )
        event_document, event_data = event
        chain_document, chain_data = chain
        exposure = chain_data.get("exposure", {})
        required = ("annual_copper_linked_cost", "hedge_share", "pass_through_share")
        if event_data.get("change") is None or any(exposure.get(item) is None for item in required):
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"task_id": context.task.corpus_task_id, "impact": {}},
                limitations=["commodity shock or exposure inputs are incomplete"],
            )
        annual_cost = Decimal(str(exposure["annual_copper_linked_cost"]))
        shock = Decimal(str(event_data["change"]))
        hedge_share = Decimal(str(exposure["hedge_share"]))
        pass_through_share = Decimal(str(exposure["pass_through_share"]))
        uncovered_cost = annual_cost * shock * (Decimal(1) - hedge_share) * (Decimal(1) - pass_through_share)
        pathways = [
            {
                "from": edge["source"],
                "to": edge["target"],
                "relationship": edge["relation"],
            }
            for edge in chain_data.get("edges", [])
        ]
        limitations = [
            *context.ingestion_issues,
            "The result is an annualized steady-state cost scenario, not a current-period profit impact.",
            "It excludes tax, inventory timing, foreign exchange, demand changes and undisclosed contract coverage.",
            f"The supplied {exposure.get('lag_months', 'unknown')}-month lag is not converted into a quarterly timing estimate.",
        ]
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "task_id": context.task.corpus_task_id,
                "result_schema_version": "finagent.sample-result.v1",
                "conclusion": "Copper-price exposure is modeled as a conditional upstream cost scenario with explicit hedge and pass-through offsets.",
                "impact": {
                    "entity_id": exposure.get("entity_id"),
                    "annualized_uncovered_cost_increase": str(uncovered_cost),
                    "unit": exposure.get("unit"),
                    "formula": "annual_copper_linked_cost * commodity_change * (1 - hedge_share) * (1 - pass_through_share)",
                    "assumptions": {
                        "commodity_change": str(shock),
                        "hedge_share": str(hedge_share),
                        "pass_through_share": str(pass_through_share),
                    },
                    "timing": {"lag_months": exposure.get("lag_months"), "status": "not_modeled"},
                },
                "industry_pathways": pathways,
                "evidence_refs": [
                    {"document_id": event_document.id, "corpus_document_id": event_document.corpus_document_id, "source_url": event_document.source_url},
                    {"document_id": chain_document.id, "corpus_document_id": chain_document.corpus_document_id, "source_url": chain_document.source_url},
                ],
                "confidence": "medium",
                "confidence_reason": "Arithmetic is deterministic, while the synthetic exposure, hedge and pass-through inputs are scenario assumptions.",
                "limitations": limitations,
            },
            limitations=limitations,
        )
