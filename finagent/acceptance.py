"""Machine-checkable node acceptance for typed task outputs."""

from __future__ import annotations

from finagent.contracts import FlowResult, TaskSpec


def output_contract_result(task: TaskSpec, result: FlowResult) -> dict[str, object]:
    required = task.expected_output_fields
    provided = sorted(result.payload)
    missing = sorted(set(required) - set(provided))
    return {
        "task_id": task.corpus_task_id,
        "required_fields": required,
        "provided_fields": provided,
        "missing_fields": missing,
        "passed": not missing,
    }
