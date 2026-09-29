"""Bundled, versioned inventories compiled from the project business specifications."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files

from pydantic import Field

from finagent.contracts import BusinessLine, StrictModel


class SpecItem(StrictModel):
    name: str = Field(min_length=1)
    detail: str = ""


class SupplementalProfile(StrictModel):
    source_file: str
    source_sha256: str
    workflow_section: str
    workflow_guidance: str
    output_section: str
    output_guidance: str
    evaluation_section: str
    evaluation_guidance: str
    domain_guidance: str
    workflow_sequence: list[str] = Field(min_length=1)
    stages: list[SpecItem] = Field(min_length=1)
    deliverables: list[SpecItem] = Field(min_length=1)
    criteria: list[SpecItem] = Field(min_length=1)


class BusinessSpecification(StrictModel):
    line: BusinessLine
    title: str
    version: str
    source_file: str
    source_sha256: str
    workflow_section: str
    workflow_guidance: str
    output_section: str
    output_guidance: str
    evaluation_section: str
    evaluation_guidance: str
    domain_section: str
    domain_guidance: str
    workflow_sequence: list[str] = Field(min_length=1)
    stages: list[SpecItem] = Field(min_length=1)
    deliverables: list[SpecItem] = Field(min_length=1)
    criteria: list[SpecItem] = Field(min_length=1)
    scoring_policy: str = "proposed_unweighted"
    supplemental_profiles: list[SupplementalProfile] = Field(default_factory=list)


class PlatformPolicy(StrictModel):
    version: str
    source_file: str
    source_sha256: str
    sections: dict[str, str]


@lru_cache(maxsize=1)
def platform_policy() -> PlatformPolicy:
    resource = files("finagent").joinpath("data/platform_policy.v1.json")
    policy = PlatformPolicy.model_validate_json(resource.read_text(encoding="utf-8"))
    if set(policy.sections) != {"2", "5", "7", "9"}:
        raise ValueError("shared policy sections are incomplete")
    return policy


@lru_cache(maxsize=1)
def all_specifications() -> dict[BusinessLine, BusinessSpecification]:
    resource = files("finagent").joinpath("data/business_specifications.v1.json")
    entries = json.loads(resource.read_text(encoding="utf-8"))
    specifications = {
        BusinessLine(entry["line"]): BusinessSpecification.model_validate(entry)
        for entry in entries
    }
    if set(specifications) != set(BusinessLine) or len(entries) != len(BusinessLine):
        raise ValueError("business specification inventory must contain each line exactly once")
    return specifications


def specification_for(line: BusinessLine) -> BusinessSpecification:
    return all_specifications()[line]


def assessment_inventory(spec: BusinessSpecification) -> dict[str, object]:
    def profile_inventory(profile: SupplementalProfile) -> dict[str, object]:
        return {
            "source_file": profile.source_file,
            "source_sha256": profile.source_sha256,
            "workflow_status": [
                {"name": name, "status": "not_assessed", "source_section": profile.workflow_section}
                for name in profile.workflow_sequence
            ],
            "stage_status": [
                {"name": item.name, "detail": item.detail, "status": "not_assessed", "source_section": profile.workflow_section}
                for item in profile.stages
            ],
            "deliverable_status": [
                {"name": item.name, "detail": item.detail, "status": "not_assessed", "source_section": profile.output_section}
                for item in profile.deliverables
            ],
            "criterion_status": [
                {"name": item.name, "detail": item.detail, "status": "not_assessed", "source_section": profile.evaluation_section}
                for item in profile.criteria
            ],
        }

    return {
        "specification_version": spec.version,
        "source_file": spec.source_file,
        "source_sha256": spec.source_sha256,
        "scoring_policy": spec.scoring_policy,
        "workflow_status": [
            {"name": name, "status": "not_assessed", "source_section": spec.workflow_section}
            for name in spec.workflow_sequence
        ],
        "stage_status": [
            {"name": item.name, "detail": item.detail, "status": "not_assessed", "source_section": spec.workflow_section}
            for item in spec.stages
        ],
        "deliverable_status": [
            {"name": item.name, "detail": item.detail, "status": "not_assessed", "source_section": spec.output_section}
            for item in spec.deliverables
        ],
        "criterion_status": [
            {"name": item.name, "detail": item.detail, "status": "not_assessed", "source_section": spec.evaluation_section}
            for item in spec.criteria
        ],
        "supplemental_profiles": [profile_inventory(p) for p in spec.supplemental_profiles],
    }
