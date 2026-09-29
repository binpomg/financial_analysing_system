"""Compile the authoritative Markdown business sections into packaged JSON.

Run from the repository root after changing the source specifications. The
generated snapshot is reviewed and committed alongside the source documents.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "金融投研智能体体系构建思路.md"
TARGET = ROOT / "finagent" / "data" / "business_specifications.v1.json"
POLICY_TARGET = ROOT / "finagent" / "data" / "platform_policy.v1.json"
VERSION = "business-spec.v1"


def section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    end = text.index(next_heading, start + len(heading))
    return text[start:end]


def table_rows(text: str, header: str) -> list[dict[str, str]]:
    match = re.search(rf"^\| {re.escape(header)} \|.*$", text, re.MULTILINE)
    if match is None:
        raise ValueError(f"table not found: {header}")
    rows = []
    for line in text[match.end():].splitlines():
        if not line.strip() or re.match(r"^\|[\s:|-]+\|$", line):
            continue
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2:
            rows.append({"name": cells[0], "detail": cells[1]})
    return rows


def stage_rows(text: str, line_number: int) -> list[dict[str, str]]:
    if line_number != 1:
        return table_rows(text, "步骤")
    return [{"name": step, "detail": ""} for step in workflow_sequence(text)]


def workflow_sequence(text: str) -> list[str]:
    workflow = re.search(r"^> (.+)$", text, re.MULTILINE)
    if workflow is None:
        raise ValueError("workflow not found")
    return [step.strip().rstrip("。") for step in workflow.group(1).split("→")]


def output_rows(text: str) -> list[dict[str, str]]:
    rows = []
    for match in re.finditer(r"^\d+\. \*\*(.+?)\*\*[：:]?(.+)$", text, re.MULTILINE):
        rows.append({"name": match.group(1).strip(), "detail": match.group(2).strip()})
    if not rows:
        raise ValueError("deliverables not found")
    return rows


def supplemental_profile(filename: str) -> dict[str, object]:
    path = ROOT / filename
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    workflow = section(text, "## 3．", "## 4．")
    outputs = section(text, "## 5．", "## 6．")
    evaluation = section(text, "## 6．", "## 7．")
    return {
        "source_file": filename,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "workflow_section": "3",
        "workflow_guidance": workflow.strip(),
        "output_section": "5",
        "output_guidance": outputs.strip(),
        "evaluation_section": "6",
        "evaluation_guidance": evaluation.strip(),
        "workflow_sequence": workflow_sequence(workflow),
        "stages": [{"name": step, "detail": ""} for step in workflow_sequence(workflow)],
        "deliverables": output_rows(outputs),
        "criteria": table_rows(evaluation, "评价维度"),
        "domain_guidance": section(text, "## 4．", "## 5．").strip(),
    }


def main() -> None:
    raw = SOURCE.read_bytes()
    text = raw.decode("utf-8")
    entries = []
    for number in range(1, 8):
        prefix = f"6.{number}."
        start = re.search(rf"^### 6\.{number}．", text, re.MULTILINE)
        end_pattern = rf"^### 6\.{number + 1}．" if number < 7 else r"^## 7．"
        end = re.search(end_pattern, text, re.MULTILINE)
        if start is None or end is None:
            raise ValueError(f"business line heading missing: {number}")
        content = text[start.start():end.start()]
        title = content.splitlines()[0].split("．", 1)[1].strip()
        headings = list(re.finditer(r"^#### (6\.\d+\.\d+)．(.+)$", content, re.MULTILINE))
        parts = {
            match.group(1): content[match.end():headings[index + 1].start() if index + 1 < len(headings) else len(content)]
            for index, match in enumerate(headings)
        }
        workflow_id = f"{prefix}3"
        output_id = f"{prefix}5"
        evaluation_id = f"{prefix}6"
        profiles = (
            [supplemental_profile("通用金融事件文档理解与结构化提取构建思路.md")]
            if number == 1 else
            [supplemental_profile("上市公司财务报告分析构建思路.md")]
            if number == 2 else []
        )
        version_material = raw + b"".join(
            bytes.fromhex(str(profile["source_sha256"])) for profile in profiles
        )
        entries.append({
            "line": number,
            "title": title,
            "version": f"{VERSION}+{hashlib.sha256(version_material).hexdigest()[:12]}",
            "source_file": SOURCE.name,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "workflow_section": workflow_id,
            "workflow_guidance": parts[workflow_id].strip(),
            "output_section": output_id,
            "output_guidance": parts[output_id].strip(),
            "evaluation_section": evaluation_id,
            "evaluation_guidance": parts[evaluation_id].strip(),
            "domain_section": f"{prefix}4",
            "domain_guidance": parts[f"{prefix}4"].strip(),
            "workflow_sequence": workflow_sequence(parts[workflow_id]),
            "stages": stage_rows(parts[workflow_id], number),
            "deliverables": output_rows(parts[output_id]),
            "criteria": table_rows(parts[evaluation_id], "评价维度"),
            "scoring_policy": "proposed_unweighted",
            "supplemental_profiles": profiles,
        })
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    policy = {
        "version": f"{VERSION}+{hashlib.sha256(raw).hexdigest()[:12]}",
        "source_file": SOURCE.name,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "sections": {
            "2": section(text, "## 2．", "## 3．").strip(),
            "5": section(text, "## 5．", "## 6．").strip(),
            "7": section(text, "## 7．", "## 8．").strip(),
            "9": section(text, "## 9．", "## 10．").strip(),
        },
    }
    POLICY_TARGET.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {TARGET}: {len(entries)} business lines")
    print(f"Wrote {POLICY_TARGET}: {len(policy['sections'])} shared sections")


if __name__ == "__main__":
    main()
