"""Small flow contract and shared research draft implementation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from finagent.contracts import FlowResult, RunStatus, SourceDocument, TaskSpec
from finagent.models import ModelGateway, ModelUnavailable
from finagent.prompts import prompt_for
from finagent.storage import EvidenceStore


@dataclass
class FlowContext:
    task: TaskSpec
    run_id: str
    source_ids: list[str]
    documents: list[SourceDocument]
    ingestion_issues: list[str]
    store: EvidenceStore
    gateway: ModelGateway | None
    model_calls: int = 0
    warnings: list[str] = field(default_factory=list)
    source_metadata: dict[str, dict[str, object]] = field(default_factory=dict)
    read_units: list[dict] = field(default_factory=list)

    def ask_model(self, instruction: str, tier: str = "strong"):
        if self.model_calls >= self.task.max_model_calls:
            raise ModelUnavailable("model call budget exhausted")
        if self.gateway is None:
            raise ModelUnavailable("model gateway is not configured")
        if any(document.data_class.value == "restricted" for document in self.documents):
            raise ModelUnavailable("restricted source cannot be sent to the configured gateway")
        from finresearch.documents import prepare_document
        from finresearch.storage import configuration
        limits = configuration()["reading"]
        context_parts, images = [], []
        self.read_units = []
        for document in self.documents:
            if document.published_at and document.published_at > self.task.as_of:
                continue
            if document.published_at is None:
                self.warnings.append(
                    f"{document.id}: disclosure date is unknown; historical availability is unverified"
                )
            raw = self.store.read_document(document.id)
            directory = self.store.root / "prepared" / self.run_id / document.id
            directory.mkdir(parents=True, exist_ok=True)
            source = Path(document.object_path)
            if source.suffix.lower() != document.media_type:
                source = directory / ("source" + document.media_type)
                source.write_bytes(raw)
            prepared = prepare_document(source, directory / "pages", document.id)
            if prepared["status"] != "ready":
                raise ModelUnavailable("source preparation incomplete; no pages omitted: " + document.id)
            self.read_units.extend(prepared["units"])
            for unit in prepared["units"]:
                if unit.get("image_path"):
                    images.append({"label": document.id + "/" + unit["unit_id"], "path": unit["image_path"]})
            content = json.dumps([
                {key: unit[key] for key in ("document_id", "unit_id", "text", "locator")}
                for unit in prepared["units"]
            ], ensure_ascii=False)
            context_parts.append(
                f"document_id={document.id}; name={document.name}; "
                f"published_at={document.published_at}\n{content}"
            )
        if not context_parts:
            raise ModelUnavailable("no readable source text is available")
        material = "\n\n".join(context_parts)
        if (len(material) > limits["max_input_characters"] or len(images) > limits["max_images"] or
                sum(Path(image["path"]).stat().st_size for image in images) > limits["max_image_bytes"]):
            raise ModelUnavailable("complete source exceeds configured limits; input was not truncated")
        if self.task.question:
            instruction = f"{instruction}\nUser question: {self.task.question}"
        self.model_calls += 1
        self.store.add_event(self.run_id, "model_call", {"tier": tier, "number": self.model_calls})
        if hasattr(self.gateway, "research_material"):
            return self.gateway.research_material(
                instruction, material, tier, self.read_units, images,
                self.store.root / "traces" / self.run_id / str(self.model_calls),
            )
        if images:
            raise ModelUnavailable("configured gateway cannot read complete page images")
        return self.gateway.research(instruction, material, tier)

    def valid_locator(self, document_id: str, locator: str) -> bool:
        document = next((item for item in self.documents if item.id == document_id), None)
        if document is None:
            return False
        units = [unit for unit in self.read_units if unit["document_id"] == document_id]
        if locator.startswith("unit:"):
            return any(unit["unit_id"] == locator[5:] for unit in units)
        if locator == "/":
            return len(units) == 1 and document.media_type in {".txt", ".md", ".json", ".csv"}
        if document is None or document.media_type != ".json":
            return False
        try:
            node = json.loads(self.store.read_document(document_id))
            for token in locator.lstrip("/").split("/"):  # JSON Pointer
                token = token.replace("~1", "/").replace("~0", "~")
                node = node[int(token)] if isinstance(node, list) else node[token]
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            return False
        return True


class Flow(Protocol):
    def execute(self, context: FlowContext) -> FlowResult: ...


class ResearchDraftFlow:
    label = "research"

    def execute(self, context: FlowContext) -> FlowResult:
        try:
            answer = context.ask_model(prompt_for(context.task.line), tier="strong")
        except ModelUnavailable as exc:
            return FlowResult(
                status=RunStatus.BLOCKED,
                payload={"line": self.label, "claims": []},
                limitations=[str(exc), *context.ingestion_issues],
            )
        accepted = [
            claim
            for claim in answer.claims
            if claim.document_id in context.source_ids
            and context.valid_locator(claim.document_id, claim.locator)
        ]
        rejected = len(answer.claims) - len(accepted)
        limitations = [
            *context.ingestion_issues,
            *context.warnings,
            *answer.limitations,
            "research draft requires independent evidence and domain review",
        ]
        if rejected:
            limitations.append(f"{rejected} claim(s) rejected for invalid source locator")
        return FlowResult(
            status=RunStatus.PARTIAL,
            payload={
                "line": self.label,
                "summary": answer.summary,
                "claims": [claim.model_dump(mode="json") for claim in accepted],
            },
            limitations=limitations,
        )
