"""Durable, dependency-aware execution primitives for financial-agent workflows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from finagent.contracts import WorkflowNodeRecord, WorkflowNodeSpec, WorkflowNodeStatus
from finagent.storage import EvidenceStore


class WorkflowDefinitionError(ValueError):
    pass


@dataclass(frozen=True)
class NodeExecutionContext:
    workflow_id: str
    node: WorkflowNodeRecord
    completed_checkpoints: dict[str, dict[str, object]]


NodeHandler = Callable[[NodeExecutionContext], dict[str, object]]


class DurableWorkflow:
    """A small persisted DAG executor with leases, retries and checkpoints.

    Handlers are deliberately kept outside the persisted plan.  The plan contains
    only stable node IDs, dependency edges and idempotency keys; a restarted process
    can attach the same handler registry and continue from completed checkpoints.
    """

    def __init__(self, store: EvidenceStore, workflow_id: str, specs: list[WorkflowNodeSpec]) -> None:
        self.store = store
        self.workflow_id = workflow_id
        self.specs = specs
        self._validate_specs()

    def plan(self) -> list[WorkflowNodeRecord]:
        return self.store.plan_workflow_nodes(self.workflow_id, self.specs)

    def run(self, handlers: dict[str, NodeHandler]) -> list[WorkflowNodeRecord]:
        missing = {spec.node_id for spec in self.specs} - set(handlers)
        if missing:
            raise WorkflowDefinitionError(f"missing node handlers: {sorted(missing)}")
        self.plan()
        self.store.recover_expired_workflow_nodes(self.workflow_id)
        while True:
            self._block_dependents_of_terminal_failures()
            node = self.store.claim_next_workflow_node(self.workflow_id)
            if node is None:
                return self.store.workflow_nodes_for(self.workflow_id)
            checkpoints = {
                item.node_id: item.checkpoint
                for item in self.store.workflow_nodes_for(self.workflow_id)
                if item.status is WorkflowNodeStatus.COMPLETED
            }
            try:
                checkpoint = handlers[node.node_id](
                    NodeExecutionContext(self.workflow_id, node, checkpoints)
                )
                self.store.complete_workflow_node(self.workflow_id, node.node_id, checkpoint)
            except Exception as exc:
                self.store.retry_or_fail_workflow_node(
                    self.workflow_id,
                    node.node_id,
                    f"{type(exc).__name__}: {exc}",
                )

    def _block_dependents_of_terminal_failures(self) -> None:
        statuses = {item.node_id: item.status for item in self.store.workflow_nodes_for(self.workflow_id)}
        for spec in self.specs:
            if statuses.get(spec.node_id) is not WorkflowNodeStatus.QUEUED:
                continue
            failed_parent = next(
                (
                    parent
                    for parent in spec.depends_on
                    if statuses.get(parent) in {WorkflowNodeStatus.FAILED, WorkflowNodeStatus.BLOCKED}
                ),
                None,
            )
            if failed_parent:
                self.store.block_workflow_node(
                    self.workflow_id, spec.node_id, f"dependency {failed_parent} did not complete"
                )

    def _validate_specs(self) -> None:
        node_ids = [item.node_id for item in self.specs]
        if len(node_ids) != len(set(node_ids)):
            raise WorkflowDefinitionError("workflow node_id values must be unique")
        if len({item.idempotency_key for item in self.specs}) != len(self.specs):
            raise WorkflowDefinitionError("workflow idempotency_key values must be unique")
        known = set(node_ids)
        for spec in self.specs:
            unknown = set(spec.depends_on) - known
            if unknown:
                raise WorkflowDefinitionError(f"{spec.node_id} has unknown dependencies: {sorted(unknown)}")
            if spec.node_id in spec.depends_on:
                raise WorkflowDefinitionError(f"{spec.node_id} cannot depend on itself")
        visiting: set[str] = set()
        visited: set[str] = set()
        dependencies = {item.node_id: item.depends_on for item in self.specs}

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise WorkflowDefinitionError("workflow dependencies contain a cycle")
            visiting.add(node_id)
            for parent in dependencies[node_id]:
                visit(parent)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in node_ids:
            visit(node_id)
