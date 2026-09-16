"""全文独立核实审核问题；模型核实不冒充专家真值，不更改原始审核。"""
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from . import schemas
from .provider import APIProvider
from .runner import (READING_RULES, build_material, capture_environment,
                     required_dimensions, validate_dimensions, validate_result_semantics)
from .storage import configuration, digest, now, read_json, write_json


VERIFICATION_SCHEMA = schemas.obj({
    "coverage": schemas.COVERAGE,
    "decisions": schemas.arr(schemas.obj({
        "issue_index": {"type": "integer", "minimum": 0},
        "verdict": {"type": "string", "enum": ["confirmed", "rejected", "uncertain"]},
        "reason": schemas.TEXT,
        "result_pointers": schemas.arr(schemas.TEXT),
        "evidence": schemas.arr(schemas.EVIDENCE),
    })),
})

VERIFICATION_RULES = """
你在一个全新请求中核实既有审核问题，不重新给成果打分，不修改成果或稳定 Skill。
必须独立完整阅读所有原件，以原件为依据逐项判定给定问题是否成立；既有审核意见可能错误。
只返回给定 issue_index，逐项一次，不遗漏、不新增。不能为了改进积累而偏向 confirmed。
confirmed 表示原件直接支持该问题，且业务成果确有对应错误或遗漏；rejected 表示问题被原件与成果直接反驳；其余为 uncertain。
reason 说明原披露口径、成果位置、错误/遗漏关系与必要的独立重算；用条件假设推导出的疑点不能说成发行人确定出错。
evidence 给出可直接在原件核查的连续原文及定位。result_pointers 为指向业务 result 的 JSON Pointer，如 /records/0/value；遗漏指向应包含该项的 /records 或 /analysis。
确认缺失不得填零，未知也不等于确定非零。证据不足、不清晰、无法由给定资料判断，均保留 uncertain。
必须阅读全文；不得只读待核实问题所引用的页面，不输出思维链。只按本次核实 JSON Schema 输出，不返回常规审核 audit 格式。
"""


def _pointer_exists(value, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False
    for segment in pointer[1:].split("/"):
        # JSON Pointer 仅允许 ~0 和 ~1；避免宽松解码接受不可定位的字段。
        if re.search(r"~(?![01])", segment):
            return False
        segment = segment.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and segment in value:
            value = value[segment]
        elif isinstance(value, list) and segment.isdigit() and str(int(segment)) == segment and int(segment) < len(value):
            value = value[int(segment)]
        else:
            return False
    return True


def _safe_error(provider, error):
    message = str(error)
    try:
        key = provider.credentials()[1] if hasattr(provider, "credentials") else ""
        if key:
            message = message.replace(key, "[REDACTED]")
    except Exception:
        return type(error).__name__ + "：核实失败，认证信息无法安全核对；请查已脱敏接口日志"
    return message


def _reserved_indices(store, run_id):
    reserved = {item["issue_index"] for item in store.list("feedback") if item.get("run_id") == run_id}
    for item in store.list("feedback_verifications"):
        if item.get("run_id") == run_id:
            reserved.update(item.get("issue_indices", []))
    claim_root = store.root / "feedback_review_claims" / run_id
    for path in claim_root.glob("issue-*"):
        try:
            reserved.add(int(path.name[6:]))
        except ValueError:
            continue
    return reserved


def review_run_feedback(store, run_id, max_issues=2, provider=None, config=None):
    """核实最多 max_issues 项；既有/失败/中断核实不自动重试。调用者可持有 Store 写入锁。"""
    if isinstance(max_issues, bool) or not isinstance(max_issues, int) or not 1 <= max_issues <= 100:
        raise ValueError("max_issues 必须是 1 至 100 的整数")
    run = store.get("runs", run_id)
    case = store.get("cases", run["case_id"])
    groups = set(run.get("group_ids", [])) | set(case.get("group_ids", []))
    if run.get("partition") in {"holdout", "calibration"} or case.get("partition") in {"holdout", "calibration"}:
        raise ValueError("保留评测/审核校准材料不能进入反馈改进")
    if any(item.get("partition") in {"holdout", "calibration"} and groups & set(item.get("group_ids", [])) for item in store.list("cases")):
        raise ValueError("同源资料组属于保留评测/审核校准，不能进入反馈改进")
    if run.get("status") != "audited" or not run.get("result") or not run.get("audit"):
        raise ValueError("只有已完整执行且完成独立审核的运行可以核实反馈")
    issues = run["audit"]["issues"]
    identifier = "verification-" + uuid.uuid4().hex[:16]
    record = {"id": identifier, "run_id": run_id, "line": run["line"], "created_at": now(),
              "worker_pid": os.getpid(), "status": "running", "verification_kind": "independent_model_check",
              "issue_indices": [], "decisions": [], "feedback_ids": [],
              "total_issues": len(issues), "remaining_issue_indices": [],
              "expert_calibrated": False, "promotion_allowed": False}
    reserved = _reserved_indices(store, run_id)
    record["previously_claimed_issue_indices"] = sorted(reserved)
    claim_root = store.root / "feedback_review_claims" / run_id
    claim_root.mkdir(parents=True, exist_ok=True)
    priorities = {"critical": 0, "major": 1, "minor": 2}
    for index in sorted(range(len(issues)), key=lambda i: (priorities.get(issues[i].get("severity"), 3), i)):
        if index in reserved:
            continue
        claim = claim_root / ("issue-%05d" % index)
        try:
            claim.mkdir()
        except FileExistsError:
            continue
        # 即使进程在登记记录前终止，此永久认领仍阻止自动重复付费请求。
        write_json(claim / "owner.json", {"verification_id": identifier, "created_at": now()})
        record["issue_indices"].append(index)
        if len(record["issue_indices"]) >= max_issues:
            break
    record["remaining_issue_indices"] = sorted(set(range(len(issues))) - _reserved_indices(store, run_id))
    if not record["issue_indices"]:
        record.update(status="no_issues" if not issues else "already_reviewed", finished_at=now())
        return store.put("feedback_verifications", identifier, record)
    store.put("feedback_verifications", identifier, record)
    trace = store.root / "traces" / identifier
    try:
        config = config or configuration()
        provider = provider or APIProvider(config)
        old_trace = store.root / "traces" / run_id
        frozen = read_json(old_trace / "frozen-context.json")
        frozen_case = frozen["case"]
        if digest(case) != digest(frozen_case):
            raise ValueError("案例已变化，不能将当前资料作为原运行核实依据")
        expected_input = digest({key: frozen_case[key] for key in ("task", "cutoff", "document_ids", "expected_attachments")})
        if run["input_digest"] != expected_input or frozen_case["input_digest"] != expected_input:
            raise ValueError("案例任务及输入指纹不一致")
        if run["partition"] != frozen_case["partition"] or set(run["group_ids"]) != set(frozen_case["group_ids"]):
            raise ValueError("原运行与案例用途或同源组不一致")
        if digest(frozen["criteria"]) != run["evaluation_version"] or digest(frozen["review"]) != run["auditor_version"]:
            raise ValueError("冻结评价规范指纹不一致")
        version = frozen["skill"]
        if version["line"] != run["line"] or version["id"] != run["skill_version"] or digest({"line": version["line"], "files": version["files"]})[:24] != version["id"]:
            raise ValueError("原运行的业务线或 Skill 版本与冻结快照不一致")
        actual_documents = [store.get("documents", doc_id) for doc_id in frozen_case["document_ids"]]
        actual_metadata = [{"id": doc["id"], "group_id": doc["group_id"], "published_at": doc["published_at"]} for doc in actual_documents]
        if actual_metadata != frozen_case["documents"]:
            raise ValueError("原件的时间或同源分组已变化，不能沿用原运行核实")
        original_execution = read_json(old_trace / "original-execution.json")
        original_audit = read_json(old_trace / "original-audit.json")
        if original_execution["result"] != run["result"] or original_audit["audit"] != run["audit"]:
            raise ValueError("成果或审核与原始记录不一致，不能核实被修改的结果")
        units, material, images, missing = build_material(store, frozen_case, config["reading"], trace / "prepared")
        if missing:
            raise ValueError("核实材料不完整：" + "；".join(missing))
        fingerprints = [{"document_id": u["document_id"], "unit_id": u["unit_id"], "text_hash": digest(u["text"]),
                         "image_hash": hashlib.sha256(Path(u["image_path"]).read_bytes()).hexdigest() if u.get("image_path") else None} for u in units]
        material_digest = digest({"material": material, "fingerprints": fingerprints})
        manifest = read_json(old_trace / "material-manifest.json")
        if material_digest != manifest["material_digest"] or material_digest != run["material_digest"]:
            raise ValueError("核实材料与原运行材料指纹不一致")
        verification_schema = schemas.bind_material_ids(VERIFICATION_SCHEMA, units)
        for payload, template in ((original_execution, schemas.EXECUTION), (original_audit, schemas.AUDIT)):
            schemas.validate(payload, schemas.bind_material_ids(template, units))
            schemas.validate_coverage(payload["coverage"], units)
            schemas.validate_evidence(payload, units)
        validate_result_semantics(run["result"])
        validate_dimensions(run["audit"], required_dimensions(frozen["criteria"]))
        if run["result"]["status"] == "reading_incomplete":
            raise ValueError("原业务未完成全文阅读，不能积累为有效反馈")
        record["material_digest"] = material_digest
        record["reviewed_run_digest"] = digest(run)
        record["audit_model"] = config["models"]["audit"]
        environment, record["model_config"] = capture_environment(provider, config)
        record["environment_digest"] = digest({"environment": environment, "schema": verification_schema, "rules": VERIFICATION_RULES})
        write_json(trace / "environment.json", environment)
        write_json(trace / "material-manifest.json", {"fingerprints": fingerprints, "images": images, "material_digest": material_digest})
        selected = [{"issue_index": index, "issue": issues[index]} for index in record["issue_indices"]]
        instructions = READING_RULES + "\n" + frozen["review"] + "\n" + frozen["criteria"] + VERIFICATION_RULES
        prompt = material + "\n待核实业务成果与问题：\n" + json.dumps({"result": run["result"], "issues": selected}, ensure_ascii=False)
        if len(instructions) + len(prompt) > config["reading"]["max_input_characters"]:
            raise ValueError("完整原件、成果及核实规范超过输入限制；未截断任何内容")
        store.put("feedback_verifications", identifier, record, replace=True)
        response = provider.call("audit", instructions, prompt, verification_schema, images, trace / "verification")
        write_json(trace / "original-verification.json", response)
        schemas.validate(response, verification_schema)
        schemas.validate_coverage(response["coverage"], units)
        indices = [item["issue_index"] for item in response["decisions"]]
        if len(indices) != len(set(indices)) or set(indices) != set(record["issue_indices"]):
            raise ValueError("核实问题索引必须逐项完整、唯一且不得新增")
        # 整批先核查后登记，避免后项格式错误时先项已成为反馈。
        decisions = []
        for item in response["decisions"]:
            if not item["reason"].strip():
                raise ValueError("核实判断缺少依据")
            if any(not _pointer_exists(run["result"], pointer) for pointer in item["result_pointers"]):
                raise ValueError("核实指出的成果位置不存在")
            notes = schemas.validate_evidence(item, units)
            decision = {**item, "model_verdict": item["verdict"], "evidence_checks": notes}
            if item["verdict"] == "confirmed" and (not item["evidence"] or not item["result_pointers"] or notes):
                decision.update(verdict="uncertain", pending_reason="缺少可直接核对的文本证据/成果位置，或引文仍需视觉核验；不自动登记为已确认反馈")
            decisions.append(decision)
        record.update(decisions=decisions, coverage=response["coverage"], status="registering_feedback")
        if digest(store.get("runs", run_id)) != record["reviewed_run_digest"] or digest(store.get("cases", run["case_id"])) != digest(frozen_case):
            raise ValueError("核实请求期间原运行或案例发生变化，停止自动登记")
        store.put("feedback_verifications", identifier, record, replace=True)
        for decision in decisions:
            if decision["verdict"] != "confirmed":
                continue
            index = decision["issue_index"]
            existing = [item for item in store.list("feedback") if item.get("run_id") == run_id and item.get("issue_index") == index]
            if existing:
                record["feedback_ids"].extend(item["id"] for item in existing)
                continue
            note = ("独立模型全文核实（未经专家校准），核实记录 " + identifier + "。\n" + decision["reason"]
                    + "\n成果定位：" + json.dumps(decision["result_pointers"], ensure_ascii=False)
                    + "\n原件证据：" + json.dumps(decision["evidence"], ensure_ascii=False))
            # 一次原子写入全部来源标记；强杀不能留下被误认为人工核实的半成品反馈。
            feedback = {"id": "feedback-" + uuid.uuid4().hex[:16], "run_id": run_id, "line": run["line"],
                        "issue_index": index, "issue": issues[index], "confirmation": note, "confirmed_at": now(),
                        "verification_id": identifier, "verification_kind": "independent_model_check", "expert_calibrated": False,
                        "verification_evidence": decision["evidence"], "result_pointers": decision["result_pointers"]}
            store.put("feedback", feedback["id"], feedback)
            record["feedback_ids"].append(feedback["id"])
            store.put("feedback_verifications", identifier, record, replace=True)
        record.update(status="completed", finished_at=now())
    except (KeyboardInterrupt, SystemExit) as error:
        record.update(status="interrupted", error="核实中断：" + type(error).__name__, finished_at=now())
        store.put("feedback_verifications", identifier, record, replace=True)
        raise
    except Exception as error:
        record.update(status="failed", error=_safe_error(provider, error), finished_at=now())
    return store.put("feedback_verifications", identifier, record, replace=True)
