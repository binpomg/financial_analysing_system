from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from finagent.contracts import BusinessLine, TaskSpec
from finagent.prompts import prompt_for
from finagent.runtime import Runtime
from finagent.specifications import all_specifications, platform_policy


ROOT = Path(__file__).parents[1]


def test_all_seven_business_inventories_match_source_documents() -> None:
    specifications = all_specifications()
    policy = platform_policy()
    assert set(specifications) == set(BusinessLine)
    assert policy.source_sha256 == hashlib.sha256((ROOT / policy.source_file).read_bytes()).hexdigest()
    assert set(policy.sections) == {"2", "5", "7", "9"}
    expected_counts = {
        1: (10, 4, 11),
        2: (7, 5, 9),
        3: (9, 6, 10),
        4: (9, 8, 10),
        5: (9, 6, 10),
        6: (10, 7, 11),
        7: (10, 8, 10),
    }
    for line, spec in specifications.items():
        assert (len(spec.stages), len(spec.deliverables), len(spec.criteria)) == expected_counts[line]
        assert spec.title.startswith(f"第{'一二三四五六七'[line.value - 1]}条业务线")
        assert spec.source_sha256 == hashlib.sha256((ROOT / spec.source_file).read_bytes()).hexdigest()
        assert spec.scoring_policy == "proposed_unweighted"
        assert spec.workflow_sequence
        assert spec.domain_guidance
        assert "→" in spec.workflow_guidance
        assert "评价维度" in spec.evaluation_guidance
        assert all(item.name != "---" for item in (*spec.stages, *spec.deliverables, *spec.criteria))
        instruction = prompt_for(line)
        assert spec.stages[0].name in instruction
        assert spec.criteria[-1].name in instruction
        for profile in spec.supplemental_profiles:
            assert profile.source_sha256 == hashlib.sha256(
                (ROOT / profile.source_file).read_bytes()
            ).hexdigest()
            assert profile.criteria[-1].name in instruction
            assert "评价维度" in profile.evaluation_guidance
    assert len(specifications[BusinessLine.DOCUMENTS].supplemental_profiles) == 1
    assert len(specifications[BusinessLine.FINANCIAL_REPORT].supplemental_profiles) == 1
    assert all(not specifications[line].supplemental_profiles for line in list(BusinessLine)[2:])


def test_blocked_run_still_records_unassessed_specification(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path / "state")
    run = runtime.run(
        TaskSpec(
            line=BusinessLine.REPORT_QUALITY,
            sources=[str(ROOT / "examples" / "financial_report.json")],
            as_of=date(2026, 4, 1),
        )
    )
    artifacts = {item["kind"]: item["payload"] for item in runtime.store.artifacts_for(run.id)}
    spec = all_specifications()[BusinessLine.REPORT_QUALITY]
    manifest = artifacts["run_manifest"]["business_specification"]
    assessment = artifacts["spec_assessment"]
    assert manifest["version"] == spec.version
    assert manifest["source_sha256"] == spec.source_sha256
    assert artifacts["run_manifest"]["platform_policy"]["version"] == platform_policy().version
    assert len(assessment["stage_status"]) == len(spec.stages)
    assert len(assessment["workflow_status"]) == len(spec.workflow_sequence)
    assert len(assessment["deliverable_status"]) == len(spec.deliverables)
    assert len(assessment["criterion_status"]) == len(spec.criteria)
    assert all(item["status"] == "not_assessed" for item in assessment["criterion_status"])
    assert "score" not in assessment
