from __future__ import annotations

from datetime import date
from pathlib import Path

from finagent.contracts import (
    BusinessLine,
    Claim,
    DataClass,
    FactNature,
    ResearchAnswer,
    RunStatus,
    TaskSpec,
)
from finagent.runtime import Runtime


SAMPLE = Path(__file__).parents[1] / "examples" / "financial_report.json"


def task(line: BusinessLine, as_of: date = date(2026, 4, 1), **kwargs) -> TaskSpec:
    return TaskSpec(
        line=line,
        sources=[str(SAMPLE)],
        entity_id="demo:000001",
        as_of=as_of,
        **kwargs,
    )


def result(runtime: Runtime, run_id: str) -> dict:
    return next(
        artifact["payload"]
        for artifact in runtime.store.artifacts_for(run_id)
        if artifact["kind"] == "result"
    )


def test_document_ingest_and_reuse_keep_field_evidence(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)
    first = runtime.run(task(BusinessLine.DOCUMENTS))
    second = runtime.run(task(BusinessLine.DOCUMENTS))

    assert first.status is RunStatus.COMPLETED
    assert second.status is RunStatus.COMPLETED
    assert first.source_ids == second.source_ids
    facts = result(runtime, second.id)["payload"]["facts"]
    assert len(facts) == 5
    assert facts[0]["evidence"]["locator"].startswith("/observations/")
    assert any(event["kind"] == "source_ingested" for event in runtime.store.events_for(second.id))


def test_financial_metrics_use_decimal_and_report_source_ids(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)
    run = runtime.run(task(BusinessLine.FINANCIAL_REPORT))
    payload = result(runtime, run.id)
    metrics = {metric["name"]: metric for metric in payload["payload"]["metrics"]}

    assert run.status is RunStatus.PARTIAL  # Narrative model is deliberately unconfigured.
    assert metrics["revenue_yoy"]["value"] == "25.00"
    assert metrics["net_margin"]["value"] == "10.0"
    assert metrics["operating_cash_to_net_income"]["value"] == "120.0"
    assert all(metric["input_fact_ids"] for metric in metrics.values())
    assert "model gateway is not configured" in payload["limitations"]


def test_future_disclosure_is_blocked(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)
    run = runtime.run(task(BusinessLine.FINANCIAL_REPORT, as_of=date(2026, 3, 30)))

    assert run.status is RunStatus.BLOCKED
    assert "published after" in result(runtime, run.id)["limitations"][0]


class FakeGateway:
    calls = 0

    def research(self, instruction: str, context: str, tier: str) -> ResearchAnswer:
        self.calls += 1
        document_id = context.split("document_id=", 1)[1].split(";", 1)[0]
        return ResearchAnswer(
            summary="Draft based on supplied material.",
            claims=[
                Claim(
                    text="Revenue is disclosed.",
                    document_id=document_id,
                    locator="/observations/0",
                    nature=FactNature.DISCLOSED,
                ),
                Claim(
                    text="Unsupported locator.",
                    document_id=document_id,
                    locator="/does-not-exist",
                    nature=FactNature.OPINION,
                ),
            ],
        )


def test_research_draft_rejects_invalid_locator(tmp_path: Path) -> None:
    gateway = FakeGateway()
    runtime = Runtime(tmp_path, gateway=gateway)
    run = runtime.run(
        task(BusinessLine.INDUSTRY_IMPACT, source_data_class=DataClass.PUBLIC)
    )
    payload = result(runtime, run.id)

    assert run.status is RunStatus.PARTIAL
    assert gateway.calls == 1
    assert len(payload["payload"]["claims"]) == 1
    assert any("rejected" in item for item in payload["limitations"])
    assert any(event["kind"] == "model_call" for event in runtime.store.events_for(run.id))


def test_restricted_source_never_reaches_external_gateway(tmp_path: Path) -> None:
    gateway = FakeGateway()
    runtime = Runtime(tmp_path, gateway=gateway)
    run = runtime.run(task(BusinessLine.INDUSTRY_IMPACT))

    assert run.status is RunStatus.BLOCKED
    assert gateway.calls == 0
    assert "restricted source" in result(runtime, run.id)["limitations"][0]


def test_text_candidate_extraction_uses_small_model_and_is_not_verified(
    tmp_path: Path,
) -> None:
    source = tmp_path / "announcement.txt"
    source.write_text("Company announces a proposed acquisition.", encoding="utf-8")

    class SmallGateway:
        tiers: list[str] = []

        def research(self, instruction: str, context: str, tier: str) -> ResearchAnswer:
            self.tiers.append(tier)
            document_id = context.split("document_id=", 1)[1].split(";", 1)[0]
            return ResearchAnswer(
                summary="Proposed acquisition.",
                claims=[
                    Claim(
                        text="The acquisition is proposed.",
                        document_id=document_id,
                        locator="/",
                        nature=FactNature.DISCLOSED,
                    )
                ],
            )

    gateway = SmallGateway()
    runtime = Runtime(tmp_path / "state", gateway=gateway)
    run = runtime.run(
        TaskSpec(
            line=BusinessLine.DOCUMENTS,
            sources=[str(source)],
            as_of=date(2026, 4, 1),
            source_data_class=DataClass.PUBLIC,
        )
    )
    payload = result(runtime, run.id)

    assert run.status is RunStatus.PARTIAL
    assert gateway.tiers == ["small"]
    assert len(payload["payload"]["candidate_claims"]) == 1
    assert any("not field-verified" in item for item in payload["limitations"])
    manifest = next(
        artifact["payload"]
        for artifact in runtime.store.artifacts_for(run.id)
        if artifact["kind"] == "run_manifest"
    )
    assert len(manifest["prompt_sha256"]) == 64


def test_every_business_line_has_independent_entry(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path)
    statuses = {
        line: runtime.run(task(line)).status
        for line in BusinessLine
    }
    assert statuses[BusinessLine.DOCUMENTS] is RunStatus.COMPLETED
    assert statuses[BusinessLine.FINANCIAL_REPORT] is RunStatus.PARTIAL
    assert all(statuses[line] is RunStatus.BLOCKED for line in list(BusinessLine)[2:])
