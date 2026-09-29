"""FinAgent orchestration for the existing full-document research workbench.

Keep the stable Skill executor and independent audit as one explicitly invoked
operation. A failed/ambiguous paid operation is never retried automatically.
Parser candidates live under the same workspace and reference its originals.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from finagent.contracts import (
    BusinessLine, DataClass, RunStatus, TaskSpec, WorkflowNodeSpec, WorkflowNodeStatus, utc_now,
)
from finagent.ingest import ingest_path
from finagent.storage import EvidenceStore
from finagent.workflow import DurableWorkflow


LINES = dict(zip((
    "extraction", "financial-report", "industry-chain", "valuation",
    "report-factcheck", "investment-memo", "research-quality",
), BusinessLine))


def evidence_store(store):
    return EvidenceStore(store.root / "agent")


def index_case(store, case, agent):
    """Index an existing case without altering its IDs, time or partition."""
    sources = []
    for identifier in case["document_ids"]:
        source = store.get("documents", identifier)
        path = Path(source["raw_path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError("原件指纹变化，必须重新导入")
        if (not source.get("published_at") or
                datetime.fromisoformat(source["published_at"]) > datetime.fromisoformat(case["cutoff"])):
            raise ValueError("原件缺少发布时间或晚于案例截止时间")
        registration_id = "workbench:" + identifier
        previous = agent.source_registration(registration_id)
        if previous is not None:
            agent.read_document(source["sha256"])
            sources.append(previous["data"])
            continue
        ingestion = ingest_path(
            agent, path, DataClass.RESTRICTED,
            published_at_override=datetime.fromisoformat(source["published_at"]).date().isoformat(),
            source_url=source.get("source_url") or None,
            copy_source=False, accept_structured_facts=False,
        )
        entry = {
            "workbench_document_id": identifier,
            "agent_document_id": ingestion.document.id,
            "published_at": source["published_at"],
            "group_id": source["group_id"],
            "candidate_count": ingestion.candidate_count,
            "ocr_job_ids": ingestion.ocr_job_ids,
            "issues": ingestion.issues,
            "fact_policy": "no_automatic_promotion",
        }
        agent.put_source_registration(registration_id, ingestion.document.id, entry)
        sources.append(entry)
    return sources


def run_case(store, case_id, line_id, candidate_id, provider, config):
    from finresearch.runner import _run_skill_case, effective_release_status, export_run

    case = store.get("cases", case_id)
    store.line(line_id)
    agent = evidence_store(store)
    task = TaskSpec(
        line=LINES[line_id], sources=case["document_ids"],
        as_of=datetime.fromisoformat(case["cutoff"]).date(), question=case["task"],
        max_model_calls=0,  # 模型预算与工具轮次由原执行器配置约束。
    )
    record = agent.create_run(task)
    agent.put_artifact(record.id, "workbench_case", {
        "case_id": case_id, "cutoff": case["cutoff"],
        "partition": case["partition"], "group_ids": case["group_ids"],
        "input_digest": case["input_digest"], "line": line_id,
        "method": "stable_skill" if candidate_id is None else "candidate_skill",
        "candidate_id": candidate_id,
        "execution_policy": "explicit_call_only; no_automatic_paid_retry",
    })
    specs = [
        WorkflowNodeSpec(node_id="index_sources", max_attempts=1,
                         idempotency_key=record.id + ":sources"),
        WorkflowNodeSpec(node_id="research_and_audit", depends_on=["index_sources"],
                         max_attempts=1, timeout_seconds=3600,
                         idempotency_key=record.id + ":research"),
    ]
    outcomes, errors = [], []

    def index(_):
        try:
            sources = index_case(store, case, agent)
            record.source_ids = [source["agent_document_id"] for source in sources]
            agent.update_run(record)
            return {"sources": sources}
        except Exception as exc:
            # 预检错误交由原执行器落盘，保持 CLI/Web 的失败交付接口。
            return {"error": str(exc)}

    def research(context):
        try:
            result = _run_skill_case(
                store, case_id, line_id, candidate_id, provider, config,
                agent_workflow_id=record.id,
                preflight_error=context.completed_checkpoints["index_sources"].get("error"),
            )
            outcomes.append(result)
            return {"workbench_run_id": result["id"], "status": result["status"],
                    "release_status": effective_release_status(result)}
        except Exception as exc:
            errors.append(exc)
            raise

    try:
        DurableWorkflow(agent, record.id, specs).run({
            "index_sources": index, "research_and_audit": research,
        })
    except (KeyboardInterrupt, SystemExit):
        mark_interrupted_workflow(store, record.id)
        raise
    if errors:
        record.status = RunStatus.FAILED
        record.finished_at = utc_now()
        agent.update_run(record)
        raise errors[0]
    if not outcomes:
        raise RuntimeError("FinAgent 工作流没有产生研究运行")
    result = outcomes[0]
    release = effective_release_status(result)
    record.status = (RunStatus.COMPLETED if release == "audit_passed" else
                     RunStatus.FAILED if result["status"] in {"failed", "interrupted"} else
                     RunStatus.BLOCKED if release == "blocked" else RunStatus.PARTIAL)
    record.finished_at = utc_now()
    agent.update_run(record)
    agent.put_artifact(record.id, "workbench_result", {
        "run_id": result["id"], "status": result["status"], "release_status": release,
        "skill_version": result["skill_version"], "material_digest": result.get("material_digest"),
        "environment_digest": result["environment_digest"],
    })
    result["agent_workflow"] = workflow_view(store, record.id)
    store.put("runs", result["id"], result, replace=True)
    export_run(store, result)
    return result


def mark_interrupted_workflow(store, workflow_id):
    """调用方已捕获中断或确证进程结束；只同步状态，不重发请求。"""
    agent = evidence_store(store)
    record = agent.get_run(workflow_id)
    if record is None:
        raise ValueError("未知 FinAgent 工作流")
    reason = "工作台执行已中断；保留完成节点，使用工作台显式恢复入口。"
    for node in agent.workflow_nodes_for(workflow_id):
        if node.status is WorkflowNodeStatus.RUNNING:
            agent.retry_or_fail_workflow_node(workflow_id, node.node_id, reason)
        # 即使以后调整节点重试上限，中断恢复也不得重新排队付费请求。
        agent.block_workflow_node(workflow_id, node.node_id, reason)
    if record.status is not RunStatus.FAILED:
        agent.add_event(workflow_id, "interrupted", {"action": "inspect_workbench_run_before_recovery"})
        record.status = RunStatus.FAILED
        record.finished_at = utc_now()
        agent.update_run(record)


def workflow_view(store, workflow_id):
    """Safe display fields only: no credentials, raw prompts or endpoint config."""
    agent = evidence_store(store)
    record = agent.get_run(workflow_id)
    if record is None:
        raise ValueError("未知 FinAgent 工作流")
    return {
        "id": record.id, "status": record.status.value,
        "source_ids": record.source_ids,
        "nodes": [{"id": node.node_id, "status": node.status.value,
                   "attempts": node.attempt_count, "error": node.error}
                  for node in agent.workflow_nodes_for(record.id)],
        "documents": [source for node in agent.workflow_nodes_for(record.id)
                      if node.node_id == "index_sources" for source in node.checkpoint.get("sources", [])],
        "results": [artifact["payload"] for artifact in agent.artifacts_for(record.id)
                    if artifact["kind"] == "workbench_result"],
    }
