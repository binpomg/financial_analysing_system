from __future__ import annotations

from datetime import date
from pathlib import Path

from finagent.contracts import BusinessLine, DataClass, TaskSpec, WorkflowNodeSpec, WorkflowNodeStatus
from finagent.runtime import Runtime
from finagent.workflow import DurableWorkflow, NodeExecutionContext


def _task() -> TaskSpec:
    return TaskSpec(
        line=BusinessLine.FINANCIAL_REPORT,
        sources=["examples/financial_report.json"],
        entity_id="demo:000001",
        as_of=date(2026, 4, 1),
        source_data_class=DataClass.RESTRICTED,
    )


def test_durable_workflow_retries_and_preserves_completed_checkpoint(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path / "state")
    record = runtime.store.create_run(_task())
    workflow = DurableWorkflow(
        runtime.store,
        record.id,
        [
            WorkflowNodeSpec(node_id="extract", idempotency_key="extract-v1"),
            WorkflowNodeSpec(
                node_id="verify", depends_on=["extract"], max_attempts=2, idempotency_key="verify-v1"
            ),
        ],
    )
    attempts = {"verify": 0}

    def extract(_: NodeExecutionContext) -> dict[str, object]:
        return {"candidate_ids": ["candidate-1"]}

    def verify(context: NodeExecutionContext) -> dict[str, object]:
        attempts["verify"] += 1
        assert context.completed_checkpoints["extract"] == {"candidate_ids": ["candidate-1"]}
        if attempts["verify"] == 1:
            raise RuntimeError("temporary parser service error")
        return {"verified": True}

    nodes = workflow.run({"extract": extract, "verify": verify})

    assert [node.status for node in nodes] == [
        WorkflowNodeStatus.COMPLETED,
        WorkflowNodeStatus.COMPLETED,
    ]
    assert attempts["verify"] == 2
    assert nodes[0].checkpoint == {"candidate_ids": ["candidate-1"]}
    assert nodes[1].attempt_count == 2


def test_durable_workflow_blocks_dependents_after_exhausted_failure(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path / "state")
    record = runtime.store.create_run(_task())
    workflow = DurableWorkflow(
        runtime.store,
        record.id,
        [
            WorkflowNodeSpec(node_id="extract", max_attempts=1, idempotency_key="extract-v1"),
            WorkflowNodeSpec(node_id="review", depends_on=["extract"], idempotency_key="review-v1"),
        ],
    )
    nodes = workflow.run(
        {
            "extract": lambda _: (_ for _ in ()).throw(ValueError("bad source")),
            "review": lambda _: {"should_not": "run"},
        }
    )

    assert [node.status for node in nodes] == [WorkflowNodeStatus.FAILED, WorkflowNodeStatus.BLOCKED]
    assert "dependency extract" in (nodes[1].error or "")


def test_runtime_persists_actor_reviewer_findings_and_resume_is_idempotent(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path / "state")
    record = runtime.run(_task())
    nodes = runtime.store.workflow_nodes_for(record.id)
    findings = runtime.store.review_findings_for(record.id)

    assert [node.node_id for node in nodes] == ["execute_flow", "review_findings"]
    assert all(node.status is WorkflowNodeStatus.COMPLETED for node in nodes)
    assert {item.reviewer for item in findings} == {
        "extractor_actor",
        "evidence_reviewer",
        "financial_reviewer",
        "conflict_reviewer",
    }
    financial = next(item for item in findings if item.reviewer == "financial_reviewer")
    assert financial.suggested_action in {"continue", "escalate"}

    resumed = runtime.resume(record.id)
    assert resumed.id == record.id
    assert resumed.source_ids == record.source_ids
    assert len(runtime.store.workflow_nodes_for(record.id)) == 2
