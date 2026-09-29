"""End-to-end boundaries between the workbench and imported FinAgent."""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from finagent.contracts import BusinessLine, DataClass, ResearchAnswer, TaskSpec
from finagent.finance_kernel import growth, dcf_value
from finagent.models import GatewayConfig, ModelUnavailable, OpenAICompatibleGateway
from finagent.runtime import Runtime
from finagent.workbench import LINES, evidence_store, workflow_view
from finresearch.cli import dispatch, parser
from finresearch.runner import run_case, resume_audit
from finresearch.storage import Store, configuration
from test.test_pipeline import PipelineProvider


def workbench(tmp_path):
    store = Store(tmp_path / "state")
    source = tmp_path / "source.txt"
    source.write_text("营业收入100万元。" + "完整正文。" * 3000 + "末页信息", encoding="utf-8")
    document = store.import_file(source, published_at="2026-01-01T09:00:00+08:00")
    case = store.create_case([document["id"]], "研究原件", "2026-01-02T09:00:00+08:00")
    return store, document, case


def test_all_seven_workbench_lines_share_originals_and_keep_independent_audits(tmp_path):
    store, document, case = workbench(tmp_path)
    provider = PipelineProvider()
    runs = [run_case(store, case["id"], line, provider=provider) for line in LINES]
    assert len(provider.calls) == 14
    assert all("末页信息" in prompt for _, prompt in provider.calls)
    agent = evidence_store(store)
    registered = agent.get_document(document["sha256"])
    assert registered.object_path == document["raw_path"]
    assert not list(agent.objects.iterdir())
    for result in runs:
        assert result["release_status"] == "audit_passed"
        view = workflow_view(store, result["agent_workflow_id"])
        assert view["source_ids"] == [document["sha256"]]
        assert [node["status"] for node in view["nodes"]] == ["completed", "completed"]
        manifest = next(a["payload"] for a in agent.artifacts_for(view["id"]) if a["kind"] == "workbench_case")
        assert manifest["cutoff"] == case["cutoff"]
        assert manifest["partition"] == case["partition"]
        assert manifest["group_ids"] == case["group_ids"]
        assert dispatch(store, parser().parse_args(["workflow", result["id"]])) == view


def test_workbench_does_not_retry_failed_model_or_convert_failure_to_pass(tmp_path):
    store, _, case = workbench(tmp_path)
    provider = PipelineProvider(fail_business=1)
    run = run_case(store, case["id"], provider=provider)
    assert provider.business_count == 1
    assert run["status"] == "failed"
    assert run["agent_workflow"]["status"] == "failed"
    with pytest.raises(ValueError, match="workbench workflows"):
        Runtime(store.root / "agent").resume(run["agent_workflow_id"])
    assert provider.business_count == 1


def test_workbench_json_is_not_automatically_promoted_to_fact(tmp_path):
    store = Store(tmp_path / "state")
    source = Path(__file__).parents[1] / "examples" / "financial_report.json"
    document = store.import_file(source, published_at="2026-03-31T09:00:00+08:00")
    case = store.create_case([document["id"]], "查看输入", "2026-04-01T09:00:00+08:00")
    # 数据登记独立于模型质量。该测试只验证上传 JSON 不等于人工核验。
    from finagent.workbench import index_case
    agent = evidence_store(store)
    index_case(store, case, agent)
    assert agent.facts_for("demo:000001", "2026-04-01", [document["sha256"]]) == []


@pytest.mark.parametrize("abrupt", [False, True])
def test_interruption_and_recovery_close_workflow_without_repeating_requests(tmp_path, abrupt):
    from finresearch.lifecycle import recover_interrupted

    class ProcessDeath(BaseException):
        """模拟进程直接退出，跳过正常中断处理。"""

    store, _, case = workbench(tmp_path)
    provider = PipelineProvider()
    with patch.object(provider, "call", side_effect=ProcessDeath() if abrupt else KeyboardInterrupt()) as call:
        with pytest.raises(ProcessDeath if abrupt else KeyboardInterrupt):
            run_case(store, case["id"], provider=provider)
        original = store.list("runs")[0]
        if abrupt:
            assert original["status"] == "running"
            with store.exclusive(), patch("finresearch.lifecycle.owner_state", return_value="dead"):
                assert recover_interrupted(store) == [original["id"]]
                assert recover_interrupted(store) == []
        result = store.get("runs", original["id"])
        assert result["status"] == "interrupted"
        view = workflow_view(store, result["agent_workflow_id"])
        assert view["status"] == "failed"
        assert [node["status"] for node in view["nodes"]] == ["completed", "failed"]
        assert call.call_count == 1


def test_agent_reference_detects_later_original_tampering(tmp_path):
    store, document, case = workbench(tmp_path)
    run_case(store, case["id"], provider=PipelineProvider())
    Path(document["raw_path"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        evidence_store(store).read_document(document["sha256"])


def test_native_long_document_is_not_truncated(tmp_path):
    source = tmp_path / "long.txt"
    source.write_text("材料。" * 6000 + "last-page-sentinel", encoding="utf-8")

    class Gateway:
        def research(self, instruction, context, tier):
            assert "last-page-sentinel" in context
            return ResearchAnswer(summary="draft", claims=[])

    runtime = Runtime(tmp_path / "agent", Gateway())
    run = runtime.run(TaskSpec(line=BusinessLine.INDUSTRY_IMPACT, sources=[str(source)],
                              as_of=date(2026, 4, 1), source_data_class=DataClass.PUBLIC))
    assert run.status.value == "partial"
    assert any(e["kind"] == "model_call" for e in runtime.store.events_for(run.id))


def test_native_pdf_sends_every_page_and_requires_page_locator(tmp_path):
    import fitz
    from finagent.flows.base import FlowContext
    from finagent.ingest import ingest_path
    source = tmp_path / "pages.pdf"
    pdf = fitz.open()
    for i in range(3):
        pdf.new_page().insert_text((72, 72), f"page {i + 1}")
    pdf.save(source)
    pdf.close()

    class Gateway:
        def research_material(self, instruction, context, tier, units, images, trace_dir):
            assert len(units) == len(images) == 3
            assert "page 3" in context
            return ResearchAnswer(summary="draft", claims=[])

    runtime = Runtime(tmp_path / "agent", Gateway())
    document = ingest_path(runtime.store, source, DataClass.PUBLIC).document
    task = TaskSpec(line=BusinessLine.INDUSTRY_IMPACT, sources=[document.id], as_of=date(2026, 4, 1))
    record = runtime.store.create_run(task)
    context = FlowContext(task, record.id, [document.id], [document], [], runtime.store, Gateway())
    context.ask_model("read")
    assert context.valid_locator(document.id, "unit:p0003")
    assert not context.valid_locator(document.id, "/")
    assert not context.valid_locator(document.id, "unit:p0004")


def test_shared_gateway_uses_existing_models_and_no_credentials_in_repr(monkeypatch):
    monkeypatch.setenv("FIN_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("FIN_API_KEY", "test-only-credential")
    config = GatewayConfig.from_environment()
    assert config.strong_model == configuration()["models"]["business"]["model"]
    assert "test-only-credential" not in repr(config)
    gateway = OpenAICompatibleGateway(config)
    with patch.object(gateway.provider, "call", return_value={"summary": "draft", "claims": [], "limitations": []}) as call:
        gateway.research("read", "material", "strong")
    assert call.call_args.args[0] == "business"
    assert gateway.settings["models"]["business"]["reasoning_effort"] == "xhigh"


def test_shared_gateway_rejects_missing_coverage():
    config = GatewayConfig("https://example.invalid/v1", "test-only-credential", "small", "strong")
    gateway = OpenAICompatibleGateway(config)
    units = [{"document_id": "doc", "unit_id": "p1"}]
    value = {"coverage": [], "answer": {"summary": "draft", "claims": [], "limitations": []}}
    with patch.object(gateway.provider, "call", return_value=value):
        with pytest.raises(ModelUnavailable, match="complete-source"):
            gateway.research_material("read", "material", "strong", units, [], None)


def test_growth_rejects_multi_year_comparison():
    from tests.test_finance_kernel import fact
    assert growth(fact("current", "20"), fact("prior", "10", period_end=date(2023, 12, 31))).value is None


@pytest.mark.parametrize("wacc,g", [("-1", "-2"), ("NaN", "0.02"), ("0.1", "-1")])
def test_dcf_rejects_invalid_domain(wacc, g):
    with pytest.raises(ValueError):
        dcf_value([Decimal("10")], Decimal(wacc), Decimal(g))


def test_native_missing_contract_is_escalated_once(tmp_path):
    runtime = Runtime(tmp_path / "agent")
    source = Path(__file__).parents[1] / "examples" / "financial_report.json"
    run = runtime.run(TaskSpec(line=BusinessLine.DOCUMENTS, sources=[str(source)],
                              entity_id="demo:000001", as_of=date(2026, 4, 1),
                              expected_output_fields=["missing_deliverable"]))
    assert run.status.value == "partial"
    node = next(n for n in runtime.store.workflow_nodes_for(run.id) if n.node_id == "review_findings")
    assert node.attempt_count == 1
    finding = next(f for f in runtime.store.review_findings_for(run.id) if f.reviewer == "evidence_reviewer")
    assert finding.suggested_action == "escalate"
