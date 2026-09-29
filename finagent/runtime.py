"""Task execution, mandatory ingestion and durable run records."""

from __future__ import annotations

import re
from pathlib import Path

from finagent import __version__
from finagent.acceptance import output_contract_result
from finagent.contracts import (
    FlowResult,
    RunRecord,
    RunStatus,
    TaskSpec,
    WorkflowNodeSpec,
    WorkflowNodeStatus,
    utc_now,
)
from finagent.corpus import SampleCorpus
from finagent.evaluation import evaluate_sample_run
from finagent.flows import REGISTRY
from finagent.flows.base import FlowContext
from finagent.ingest import ingest_path
from finagent.models import GatewayConfig, ModelGateway, OpenAICompatibleGateway
from finagent.prompts import prompt_digest
from finagent.specifications import assessment_inventory, platform_policy, specification_for
from finagent.storage import EvidenceStore
from finagent.workflow import DurableWorkflow
from finagent.reviewers import review_run


DOCUMENT_ID = re.compile(r"^[0-9a-f]{64}$")


class Runtime:
    def __init__(self, state_dir: Path, gateway: ModelGateway | None = None) -> None:
        self.store = EvidenceStore(state_dir)
        config = GatewayConfig.from_environment()
        self.gateway = gateway or (OpenAICompatibleGateway(config) if config else None)

    def run(self, task: TaskSpec) -> RunRecord:
        return self._run(task, corpus=None)

    def run_sample_task(self, corpus_root: Path, task_id: str) -> RunRecord:
        corpus = SampleCorpus(corpus_root)
        return self._run(corpus.task_spec(task_id), corpus=corpus)

    def resume(self, run_id: str, corpus_root: Path | None = None) -> RunRecord:
        """Continue a durable run after a worker or process interruption."""
        record = self.store.get_run(run_id)
        if record is None:
            raise KeyError(f"unknown run ID: {run_id}")
        nodes = self.store.workflow_nodes_for(run_id)
        if nodes and {node.node_id for node in nodes} != {"execute_flow", "review_findings"}:
            raise ValueError("workbench workflows use finresearch pipeline --resume or resume-audit; paid calls are not replayed")
        if nodes and all(node.status is WorkflowNodeStatus.COMPLETED for node in nodes):
            return record
        corpus = SampleCorpus(corpus_root) if corpus_root is not None else None
        if record.task.corpus_task_id and corpus is None:
            raise ValueError("resuming a sample task requires corpus_root")
        return self._execute_durable(record, corpus)

    def evaluate_sample_run(self, corpus_root: Path, run_id: str) -> dict[str, object]:
        evaluation = evaluate_sample_run(self.store, corpus_root, run_id)
        self.store.put_artifact(run_id, "offline_evaluation", evaluation)
        self.store.add_event(
            run_id,
            "offline_evaluation_finished",
            {
                "status": evaluation["status"],
                "assessed_count": evaluation["assessed_count"],
                "passed_count": evaluation["passed_count"],
            },
        )
        return evaluation

    def _run(self, task: TaskSpec, corpus: SampleCorpus | None) -> RunRecord:
        spec = specification_for(task.line)
        record = self.store.create_run(task)
        self.store.add_event(
            record.id, "task_started",
            {"line": task.line.value, "specification_version": spec.version},
        )
        self.store.add_event(
            record.id,
            "task_planned",
            {
                "line": task.line.value,
                "corpus_task_id": task.corpus_task_id,
                "network_policy": task.network_policy,
                "max_model_calls": task.max_model_calls,
            },
        )
        try:
            return self._execute_durable(record, corpus)
        except Exception as exc:
            self.store.add_event(
                record.id,
                "task_failed",
                {"error_type": type(exc).__name__, "reason": str(exc)},
            )
            result = FlowResult(
                status=RunStatus.FAILED,
                payload={},
                limitations=[f"{type(exc).__name__}: {exc}"],
            )
        self._finish_run(record, result, corpus)
        return record

    def _execute_durable(self, record: RunRecord, corpus: SampleCorpus | None) -> RunRecord:
        """Execute flow and reviewer stages as a persisted, restartable DAG."""
        task = record.task
        specs = [
            WorkflowNodeSpec(
                node_id="execute_flow",
                max_attempts=1,
                timeout_seconds=900,
                idempotency_key=f"{record.id}:flow:v1",
            ),
            WorkflowNodeSpec(
                node_id="review_findings",
                depends_on=["execute_flow"],
                max_attempts=2,
                timeout_seconds=120,
                idempotency_key=f"{record.id}:review:v1",
            ),
        ]
        workflow = DurableWorkflow(self.store, record.id, specs)

        def execute_flow(_: object) -> dict[str, object]:
            result = self._execute(record, corpus)
            return {"result": result.model_dump(mode="json"), "source_ids": record.source_ids}

        def review_findings(context: object) -> dict[str, object]:
            # ``context`` is a NodeExecutionContext; handlers receive only completed checkpoints.
            checkpoints = getattr(context, "completed_checkpoints")
            execution = checkpoints["execute_flow"]
            result = FlowResult.model_validate(execution["result"])
            acceptance = output_contract_result(task, result)
            self.store.put_artifact(record.id, "output_contract_acceptance", acceptance)
            findings = review_run(
                self.store,
                record.id,
                "review_findings",
                task,
                result,
                list(execution["source_ids"]),
                acceptance,
            )
            self.store.put_artifact(
                record.id,
                "review_findings",
                {"findings": [item.model_dump(mode="json") for item in findings]},
            )
            self.store.add_event(
                record.id,
                "review_finished",
                {
                    "reviewers": [item.reviewer for item in findings],
                    "finding_count": len(findings),
                },
            )
            retry_findings = [item for item in findings if item.suggested_action == "retry"]
            if retry_findings:
                raise ReviewerRetryRequested(
                    "; ".join(item.reason for item in retry_findings)
                )
            return {
                "result": result.model_dump(mode="json"),
                "output_contract": acceptance,
                "finding_ids": [item.id for item in findings],
            }

        nodes = workflow.run({"execute_flow": execute_flow, "review_findings": review_findings})
        terminal = {node.node_id: node for node in nodes}
        review = terminal.get("review_findings")
        execution = terminal.get("execute_flow")
        if execution is None or execution.status is not WorkflowNodeStatus.COMPLETED:
            result = FlowResult(
                status=RunStatus.FAILED,
                payload={},
                limitations=["workflow execution node did not complete"],
            )
        elif review is None or review.status is not WorkflowNodeStatus.COMPLETED:
            result = FlowResult.model_validate(execution.checkpoint["result"])
            if result.status is RunStatus.COMPLETED:
                result.status = RunStatus.PARTIAL
            result.limitations.append("workflow review node did not complete")
        else:
            result = FlowResult.model_validate(review.checkpoint["result"])
            acceptance = review.checkpoint["output_contract"]
            if not acceptance["passed"] and result.status is RunStatus.COMPLETED:
                result.status = RunStatus.PARTIAL
                result.limitations.append(
                    "output contract incomplete: " + ", ".join(acceptance["missing_fields"])
                )
            findings = self.store.review_findings_for(record.id, "review_findings")
            blocking = [item for item in findings if item.suggested_action == "block"]
            if blocking and result.status is not RunStatus.FAILED:
                result.status = RunStatus.BLOCKED
                result.limitations.append(
                    "reviewer blocked execution: " + "; ".join(item.reason for item in blocking)
                )
        self._finish_run(record, result, corpus)
        return record

    def _finish_run(self, record: RunRecord, result: FlowResult, corpus: SampleCorpus | None) -> None:
        task = record.task
        spec = specification_for(task.line)
        policy = platform_policy()
        self.store.put_artifact(record.id, "result", result.model_dump(mode="json"))
        self.store.put_artifact(record.id, "spec_assessment", assessment_inventory(spec))
        self.store.put_artifact(
            record.id,
            "run_manifest",
            {
                "platform_version": __version__,
                "task": task.model_dump(mode="json"),
                "source_ids": record.source_ids,
                "gateway": type(self.gateway).__name__ if self.gateway else None,
                "prompt_sha256": prompt_digest(task.line),
                "business_specification": {
                    "version": spec.version, "source_file": spec.source_file,
                    "source_sha256": spec.source_sha256, "workflow_section": spec.workflow_section,
                    "output_section": spec.output_section, "evaluation_section": spec.evaluation_section,
                    "domain_section": spec.domain_section,
                },
                "platform_policy": {
                    "version": policy.version, "source_file": policy.source_file,
                    "source_sha256": policy.source_sha256, "sections": sorted(policy.sections),
                },
                "corpus": ({"task_id": task.corpus_task_id, "root": str(corpus.root)} if corpus else None),
            },
        )
        record.status = result.status
        record.finished_at = utc_now()
        self.store.update_run(record)
        self.store.add_event(record.id, "task_finished", {"status": record.status.value})

    def _execute(self, record: RunRecord, corpus: SampleCorpus | None = None) -> FlowResult:
        task = record.task
        documents = []
        ingestion_issues = []
        source_metadata: dict[str, dict[str, object]] = {}
        if corpus is not None:
            assert task.corpus_task_id is not None
            prepared = corpus.prepare(self.store, task.corpus_task_id)
            documents = prepared.documents
            ingestion_issues = prepared.issues
            source_metadata = {
                item["doc_id"]: item for item in prepared.availability if item.get("doc_id")
            }
            for document in documents:
                self._append_source_id(record, document.id)
                self.store.add_event(
                    record.id,
                    "source_reused",
                    {"document_id": document.id, "corpus_task_id": task.corpus_task_id},
                )
            self.store.update_run(record)
            self.store.put_artifact(
                record.id,
                "corpus_resolution",
                {
                    "task_id": prepared.task.task_id,
                    "title": prepared.task.title,
                    "availability": prepared.availability,
                    "expected_output_fields": prepared.task.expected_output_fields,
                    "network_policy": prepared.task.network_policy,
                },
            )
            self.store.add_event(
                record.id,
                "task_data_ready",
                {
                    "source_count": len(documents),
                    "excluded_count": len(ingestion_issues),
                    "corpus_task_id": task.corpus_task_id,
                },
            )
            if not documents:
                return FlowResult(
                    status=RunStatus.BLOCKED,
                    payload={"source_ids": record.source_ids},
                    limitations=["no corpus documents are eligible for this task", *ingestion_issues],
                )
        else:
            for source in task.sources:
                path = Path(source)
                if path.is_file():
                    ingestion = ingest_path(self.store, path, task.source_data_class)
                    document = ingestion.document
                    ingestion_issues.extend(f"{document.id}: {issue}" for issue in ingestion.issues)
                    source_event = {
                        "document_id": document.id,
                        "fact_count": ingestion.fact_count,
                        "candidate_count": ingestion.candidate_count,
                        "ocr_job_ids": ingestion.ocr_job_ids,
                    }
                    if ingestion.parse_summary is not None:
                        source_event["parse_summary"] = ingestion.parse_summary.model_dump()
                    self.store.add_event(
                        record.id,
                        "source_ingested",
                        source_event,
                    )
                elif DOCUMENT_ID.fullmatch(source):
                    document = self.store.get_document(source)
                    if document is None:
                        raise ValueError(f"unknown document ID: {source}")
                    self.store.add_event(
                        record.id, "source_reused", {"document_id": document.id}
                    )
                else:
                    raise FileNotFoundError(source)
                documents.append(document)
                self._append_source_id(record, document.id)
                self.store.update_run(record)
                if document.published_at and document.published_at > task.as_of:
                    return FlowResult(
                        status=RunStatus.BLOCKED,
                        payload={"source_ids": record.source_ids},
                        limitations=[
                            f"{document.id} was published after the analysis date {task.as_of}"
                        ],
                    )
            self.store.add_event(
                record.id,
                "task_data_ready",
                {"source_count": len(documents), "excluded_count": len(ingestion_issues)},
            )
        context = FlowContext(
            task=task,
            run_id=record.id,
            source_ids=record.source_ids,
            documents=documents,
            ingestion_issues=ingestion_issues,
            store=self.store,
            gateway=self.gateway,
            source_metadata=source_metadata,
        )
        self.store.add_event(record.id, "flow_started", {"line": task.line.value})
        result = REGISTRY[task.line].execute(context)
        self.store.add_event(
            record.id,
            "flow_finished",
            {"line": task.line.value, "status": result.status.value, "model_calls": context.model_calls},
        )
        return result

    @staticmethod
    def _append_source_id(record: RunRecord, document_id: str) -> None:
        if document_id not in record.source_ids:
            record.source_ids.append(document_id)


class ReviewerRetryRequested(RuntimeError):
    """A reviewer can return the current work item to the durable retry queue."""
