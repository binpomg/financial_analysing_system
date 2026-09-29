"""Versioned prompt text. The digest travels with every run manifest."""

from __future__ import annotations

import hashlib

from finagent.contracts import BusinessLine
from finagent.specifications import platform_policy, specification_for


PROMPTS: dict[BusinessLine, str] = {
    BusinessLine.DOCUMENTS: (
        "Extract candidate financial events or claims from the supplied document. "
        "State only what the source says, preserve its event status and report missing fields. "
        "This is a candidate extraction, not verified structured data."
    ),
    BusinessLine.FINANCIAL_REPORT: (
        "Explain disclosed performance and cash-flow quality. Distinguish facts, "
        "management explanations and your inferences. Do not recalculate metrics."
    ),
    BusinessLine.INDUSTRY_IMPACT: (
        "Identify the event or scenario, candidate affected industries, transmission "
        "mechanism, company exposure, countervailing factors and missing evidence. "
        "Do not infer a stock-price move from an industry effect."
    ),
    BusinessLine.VALUATION: (
        "Identify applicable valuation methods, disclosed inputs, assumptions and gaps. "
        "Do not output a fair-value number unless a deterministic valuation model has run."
    ),
    BusinessLine.REPORT_REVIEW: (
        "Extract checkable report claims and potential factual, numeric, unit, period or "
        "citation problems. Distinguish confirmed errors from unverified claims."
    ),
    BusinessLine.INVESTMENT_MEMO: (
        "Draft a buy-side investment thesis with supporting and contrary evidence, "
        "key assumptions, invalidation conditions and monitoring indicators. "
        "Do not turn unsupported opinions into disclosed facts."
    ),
    BusinessLine.REPORT_QUALITY: (
        "Evaluate a report's evidence quality, method, logic, coverage and usability. "
        "Separate the quality assessment from the fraction independently verified. "
        "Do not invent a numerical score without a calibrated rubric."
    ),
}


def prompt_for(line: BusinessLine) -> str:
    spec = specification_for(line)
    policy = platform_policy()
    stages = "; ".join(item.name for item in spec.stages)
    deliverables = "; ".join(item.name for item in spec.deliverables)
    criteria = "; ".join(f"{item.name}: {item.detail}" for item in spec.criteria)
    supplemental = "\n".join(
        f"Supplemental source {profile.source_file}: "
        f"stages {', '.join(item.name for item in profile.stages)}; "
        f"deliverables {', '.join(item.name for item in profile.deliverables)}; "
        f"evaluation checks {'; '.join(f'{item.name}: {item.detail}' for item in profile.criteria)}; "
        f"domain guidance {profile.domain_guidance}."
        for profile in spec.supplemental_profiles
    )
    return (
        f"{PROMPTS[line]}\n"
        f"Authoritative business specification: {spec.source_file}, version {spec.version}, "
        f"sections {spec.workflow_section}/{spec.output_section}/{spec.evaluation_section}.\n"
        f"Required workflow stages: {stages}.\n"
        f"Complete workflow order: {' -> '.join(spec.workflow_sequence)}.\n"
        f"Expected deliverables: {deliverables}.\n"
        f"Evaluation checks for platform validation: {criteria}.\n"
        f"Domain definitions and method constraints ({spec.domain_section}): "
        f"{spec.domain_guidance}\n"
        f"Shared evidence and confidence rules (source {policy.source_file}, section 7): "
        f"{policy.sections['7']}\n"
        f"{supplemental}\n"
        "These are proposed, unweighted criteria. Do not claim a stage was performed or a "
        "criterion passed without independent evidence. Preserve source, period, scope, "
        "publication time, assumptions, limitations, and confidence rationale."
    )


def prompt_digest(line: BusinessLine) -> str:
    return hashlib.sha256(prompt_for(line).encode("utf-8")).hexdigest()
