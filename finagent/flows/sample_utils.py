"""Helpers shared by deterministic sample-corpus flows."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from finagent.contracts import Fact, SourceDocument
from finagent.flows.base import FlowContext


def corpus_json_documents(context: FlowContext) -> list[tuple[SourceDocument, dict[str, Any]]]:
    values: list[tuple[SourceDocument, dict[str, Any]]] = []
    for document in context.documents:
        if document.media_type != ".json":
            continue
        try:
            value = json.loads(context.store.read_document(document.id).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            values.append((document, value))
    return values


def document_text(context: FlowContext, document: SourceDocument) -> str | None:
    """Return readable source text for a bound document without treating it as instructions."""
    raw = context.store.read_document(document.id)
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def decimal_value(payload: dict[str, Any], name: str) -> Decimal:
    value = payload.get(name)
    if value is None:
        raise ValueError(f"missing required numeric input: {name}")
    return Decimal(str(value))


def fact_index(context: FlowContext) -> dict[tuple[str, str], Fact]:
    if not context.task.entity_id:
        return {}
    facts = context.store.facts_for(
        context.task.entity_id,
        context.task.as_of.isoformat(),
        context.source_ids,
    )
    return {(fact.field, fact.period_end.isoformat()): fact for fact in facts}
