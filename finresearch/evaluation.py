"""Evidence-preserving evaluation gates; this module never promotes a Skill."""

import hashlib
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext


_SEVERITIES = {"minor": 1, "major": 2, "critical": 3}
_JUDGMENTS = {"pass", "fail", "uncertain"}
_VERDICTS = {"pass", "revise", "insufficient_evidence"}
_COMPARED_FIELDS = (
    "case_id", "line", "partition", "input_digest", "model",
    "reasoning_effort", "evaluation_version", "auditor_version",
)
_CONFIG_FIELDS = (
    "line", "skill_version", "model", "reasoning_effort",
    "evaluation_version", "auditor_version",
)
_OPTIONAL_CONFIG = ("model_config", "audit_model", "auditor_model", "auditor_reasoning_effort",
                    "auditor_config", "tool_version", "environment_digest")
_MATERIAL_FIELDS = ("material_digest",)


def canonical_digest(value):
    """Hash JSON content independently of dictionary insertion order."""
    def encode(item):
        if isinstance(item, Decimal) and item.is_finite():
            return str(item)
        raise TypeError("Unsupported canonical JSON value: %r" % type(item).__name__)

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False, default=encode)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit_errors(run, label):
    errors = []
    if not isinstance(run, dict):
        return ["%s 必须是运行记录对象。" % label]
    if run.get("evaluation_eligible") is False or run.get("execution_reused"):
        errors.append("%s 为恢复或复用执行成果，不能作为独立晋升试验证据。" % label)
    for field in ("id", "skill_version") + _COMPARED_FIELDS:
        if not isinstance(run.get(field), str) or not run[field].strip():
            errors.append("%s 缺少有效字符串 %s。" % (label, field))
    groups = run.get("group_ids")
    if not isinstance(groups, list) or not groups or any(not isinstance(x, str) or not x for x in groups):
        errors.append("%s 缺少有效的 group_ids。" % label)
    if run.get("status") not in {"completed", "audited"}:
        errors.append("%s 尚未完成执行和审核。" % label)
    if not isinstance(run.get("result"), dict) or not run["result"]:
        errors.append("%s 缺少业务结果。" % label)
    audit = run.get("audit")
    if not isinstance(audit, dict):
        return errors + ["%s 缺少审核记录。" % label]
    if audit.get("verdict") not in _VERDICTS:
        errors.append("%s 审核结论无效。" % label)
    if not isinstance(audit.get("summary"), str) or not audit["summary"].strip():
        errors.append("%s 审核缺少说明。" % label)
    dimensions = audit.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        errors.append("%s 缺少分项评价，不能据总评价判定改善。" % label)
    else:
        names = []
        for dimension in dimensions:
            if not isinstance(dimension, dict):
                errors.append("%s 存在无效评价维度。" % label)
                continue
            name = dimension.get("name")
            if not isinstance(name, str) or not name.strip():
                errors.append("%s 存在未命名评价维度。" % label)
            else:
                names.append(name)
            if dimension.get("judgment") not in _JUDGMENTS or not dimension.get("reason"):
                errors.append("%s 维度 %s 缺少有效判断或理由。" % (label, name))
        if len(names) != len(set(names)):
            errors.append("%s 的评价维度名称重复。" % label)
    issues = audit.get("issues")
    if not isinstance(issues, list):
        errors.append("%s 的问题清单必须是列表。" % label)
    else:
        for index, issue in enumerate(issues):
            if not isinstance(issue, dict):
                errors.append("%s 问题 %s 格式无效。" % (label, index))
                continue
            if issue.get("severity") not in _SEVERITIES:
                errors.append("%s 问题 %s 严重性无效。" % (label, index))
            for field in ("category", "description", "evidence", "correction"):
                if issue.get(field) in (None, "", [], {}):
                    errors.append("%s 问题 %s 缺少 %s。" % (label, index, field))
    return errors


def _issue_key(issue):
    # 无稳定问题标识时按证据和描述保守匹配；文字变化不能冒充已经解决。
    if issue.get("issue_id"):
        return canonical_digest({"issue_id": issue["issue_id"]})
    return canonical_digest({key: issue.get(key) for key in ("category", "description", "evidence")})


def _pair(baseline, candidate):
    report = {
        "decision": "incomparable", "comparable": False, "promotion_allowed": False,
        "baseline_id": baseline.get("id") if isinstance(baseline, dict) else None,
        "candidate_id": candidate.get("id") if isinstance(candidate, dict) else None,
        "reasons": [], "uncertainties": [], "dimension_changes": [],
        "new_issues": [], "resolved_issues": [], "severity_changes": [],
        "issue_counts": {}, "improvement_observed": False, "regression_detected": False,
    }
    errors = _audit_errors(baseline, "基线") + _audit_errors(candidate, "候选")
    if errors:
        report["reasons"] = errors
        return report
    for field in _COMPARED_FIELDS + _OPTIONAL_CONFIG + _MATERIAL_FIELDS:
        if baseline.get(field) != candidate.get(field):
            errors.append("比较条件不一致：%s。" % field)
    if set(baseline["group_ids"]) != set(candidate["group_ids"]):
        errors.append("比较条件不一致：原始资料组 group_ids。")
    if baseline["id"] == candidate["id"]:
        errors.append("基线和候选不能引用同一次运行。")
    if baseline["skill_version"] == candidate["skill_version"]:
        errors.append("Skill 版本相同，不能据此验证候选修改。")
    base_dimensions = {x["name"]: x for x in baseline["audit"]["dimensions"]}
    new_dimensions = {x["name"]: x for x in candidate["audit"]["dimensions"]}
    if set(base_dimensions) != set(new_dimensions):
        errors.append("评价维度集合不一致；不能删除难项或改变评价范围。")
    if errors:
        report["reasons"] = errors
        return report
    report["comparable"] = True
    for name in sorted(base_dimensions):
        old, new = base_dimensions[name], new_dimensions[name]
        if old["judgment"] == new["judgment"]:
            change = "unchanged"
        elif old["judgment"] == "pass":
            change = "regressed"
            report["regression_detected"] = True
        elif new["judgment"] == "pass":
            change = "improved"
            report["improvement_observed"] = True
        elif old["judgment"] == "uncertain" and new["judgment"] == "fail":
            change = "regressed"
            report["regression_detected"] = True
        else:
            change = "uncertain"
        report["dimension_changes"].append({
            "name": name, "baseline": old["judgment"], "candidate": new["judgment"],
            "change": change, "baseline_reason": old["reason"], "candidate_reason": new["reason"],
        })
        if "uncertain" in (old["judgment"], new["judgment"]):
            report["uncertainties"].append("维度 %s 存在未确定判断。" % name)
    base_issues = baseline["audit"]["issues"]
    new_issues = candidate["audit"]["issues"]
    base_keys, new_keys = Counter(_issue_key(x) for x in base_issues), Counter(_issue_key(x) for x in new_issues)
    pending_new, pending_resolved = new_keys - base_keys, base_keys - new_keys
    for issue in new_issues:
        key = _issue_key(issue)
        if pending_new[key]:
            report["new_issues"].append(issue)
            pending_new[key] -= 1
    for issue in base_issues:
        key = _issue_key(issue)
        if pending_resolved[key]:
            report["resolved_issues"].append(issue)
            pending_resolved[key] -= 1
    for key in base_keys.keys() & new_keys.keys():
        before = sorted(_SEVERITIES[x["severity"]] for x in base_issues if _issue_key(x) == key)
        after = sorted(_SEVERITIES[x["severity"]] for x in new_issues if _issue_key(x) == key)
        if before != after:
            report["severity_changes"].append({"issue_key": key, "baseline": before, "candidate": after})
            if any(new > old for old, new in zip(reversed(before), reversed(after))):
                report["regression_detected"] = True
            else:
                report["uncertainties"].append("问题严重性下降需复核依据，不能仅靠重新分类计为改善。")
    if any(x["severity"] in {"critical", "major"} for x in report["new_issues"]):
        report["regression_detected"] = True
        report["reasons"].append("候选出现新的 critical/major 问题，禁止用其他改善抵消。")
    if report["new_issues"]:
        report["uncertainties"].append("新增问题需逐项核实；没有稳定问题标识时，措辞变化可能影响问题匹配。")
    if report["resolved_issues"]:
        report["improvement_observed"] = True
        report["uncertainties"].append("未再报告的问题仍需回查原文确认已解决，审核意见不是独立真值。")
    for label, issues in (("baseline", base_issues), ("candidate", new_issues)):
        report["issue_counts"][label] = {severity: sum(x["severity"] == severity for x in issues)
                                          for severity in _SEVERITIES}
    if any(x["severity"] in {"critical", "major"} for x in new_issues):
        report["uncertainties"].append("候选仍有 critical/major 问题，不能标记为已具备真实使用质量。")
    for run in (baseline, candidate):
        if run["audit"]["verdict"] == "insufficient_evidence":
            report["uncertainties"].append("运行 %s 的审核证据不足。" % run["id"])
        if run["audit"]["verdict"] == "pass" and (
                any(x["judgment"] != "pass" for x in run["audit"]["dimensions"])
                or any(x["severity"] in {"critical", "major"} for x in run["audit"]["issues"])):
            report["uncertainties"].append("运行 %s 的通过结论与分项证据冲突。" % run["id"])
    if baseline["audit"]["verdict"] == "pass" and candidate["audit"]["verdict"] != "pass":
        report["regression_detected"] = True
        report["reasons"].append("候选从审核通过退为需修订或证据不足。")
    if report["regression_detected"]:
        report["decision"] = "keep_baseline"
        report["reasons"].append("存在分项退化，保留稳定版。")
    elif report["improvement_observed"]:
        report["decision"] = "needs_review"
        report["reasons"].append("观察到改善信号；未预先定义量化晋升门槛，不自动采用。")
    else:
        report["decision"] = "keep_baseline"
        report["reasons"].append("没有足以支持修改的分项改善，保留稳定版。")
    return report


def _regressions(pairs, baseline, candidate, excluded_case_ids, excluded_group_ids):
    result = {"provided": bool(pairs), "complete": False, "passed": False, "pairs": [], "reasons": []}
    if not isinstance(pairs, list) or not pairs:
        result["reasons"].append("缺少非空、配对的历史回归运行证据。")
        return result
    seen = set(excluded_case_ids)
    valid = True
    for pair in pairs:
        if not isinstance(pair, dict) or not isinstance(pair.get("baseline"), dict) or not isinstance(pair.get("candidate"), dict):
            result["reasons"].append("回归项必须包含 baseline 和 candidate 完整运行记录。")
            valid = False
            continue
        old, new = pair["baseline"], pair["candidate"]
        item = _pair(old, new)
        result["pairs"].append(item)
        if not isinstance(old.get("case_id"), str):
            result["reasons"].append("回归缺少有效案例标识。")
            valid = False
            continue
        if old.get("case_id") in seen:
            result["reasons"].append("回归案例重复或复用了本次新数据案例：%s。" % old.get("case_id"))
            valid = False
        seen.add(old.get("case_id"))
        if old.get("partition") != "regression" or new.get("partition") != "regression":
            result["reasons"].append("历史回归证据必须来自 regression 分区。")
            valid = False
        groups = old.get("group_ids", [])
        if isinstance(groups, list) and all(isinstance(group, str) for group in groups) and set(groups) & excluded_group_ids:
            result["reasons"].append("历史回归与新批次共享原始资料组，不能伪装为独立回归证据。")
            valid = False
        for current, reference in ((old, baseline), (new, candidate)):
            for field in _CONFIG_FIELDS + _OPTIONAL_CONFIG:
                if current.get(field) != reference.get(field):
                    result["reasons"].append("回归与主比较的 %s 不一致。" % field)
                    valid = False
        if not item["comparable"]:
            valid = False
    result["complete"] = valid and len(result["pairs"]) == len(pairs)
    result["passed"] = result["complete"] and all(
        not x["regression_detected"] and not x["new_issues"]
        and not x["uncertainties"] for x in result["pairs"])
    return result


def compare_runs(baseline, candidate, regressions=None):
    """Compare one matched case; return evidence, never an automatic promotion."""
    report = _pair(baseline, candidate)
    if not report["comparable"]:
        report["regressions"] = {"provided": bool(regressions), "complete": False, "passed": False, "pairs": [], "reasons": []}
        return report
    regression = _regressions(regressions, baseline, candidate, {baseline["case_id"]}, set(baseline["group_ids"]))
    report["regressions"] = regression
    if any(x["regression_detected"] for x in regression["pairs"]):
        report["decision"] = "keep_baseline"
        report["regression_detected"] = True
        report["reasons"].append("历史回归出现退化，保留稳定版。")
    if not regression["passed"]:
        report["uncertainties"].append("配对回归未完整通过，不能标为可晋升。")
    report["uncertainties"].extend(regression["reasons"])
    return report


def compare_batch(pairs, regression_pairs=None):
    """Aggregate every supplied pair without averaging away serious regressions."""
    report = {"decision": "incomparable", "promotion_allowed": False, "pairs": [],
              "reasons": [], "uncertainties": [], "case_count": 0,
              "improved_cases": 0, "regressed_cases": 0}
    if not isinstance(pairs, list) or not pairs:
        report["reasons"].append("比较批次不能为空。")
        return report
    first = pairs[0]
    if not isinstance(first, dict) or not isinstance(first.get("baseline"), dict) or not isinstance(first.get("candidate"), dict):
        report["reasons"].append("每个配对必须包含 baseline 和 candidate 运行记录。")
        return report
    seen, valid, batch_groups = set(), True, set()
    for pair in pairs:
        if not isinstance(pair, dict) or not isinstance(pair.get("baseline"), dict) or not isinstance(pair.get("candidate"), dict):
            report["reasons"].append("存在无效配对，不能删除失败案例后计算改善。")
            valid = False
            continue
        item = _pair(pair["baseline"], pair["candidate"])
        report["pairs"].append(item)
        case_id = pair["baseline"].get("case_id")
        if not isinstance(case_id, str):
            valid = False
            continue
        if case_id in seen:
            report["reasons"].append("批次案例重复：%s。" % case_id)
            valid = False
        seen.add(case_id)
        groups = pair["baseline"].get("group_ids", [])
        if isinstance(groups, list) and all(isinstance(group, str) for group in groups):
            batch_groups.update(groups)
        for side in ("baseline", "candidate"):
            for field in _CONFIG_FIELDS + _OPTIONAL_CONFIG + ("partition",):
                if pair[side].get(field) != first[side].get(field):
                    report["reasons"].append("批次内 %s 的 %s 不一致。" % (side, field))
                    valid = False
        if not item["comparable"]:
            valid = False
    report["case_count"] = len(pairs)
    report["improved_cases"] = sum(x["improvement_observed"] for x in report["pairs"])
    report["regressed_cases"] = sum(x["regression_detected"] for x in report["pairs"])
    if not valid:
        report["reasons"].append("存在不可比配对，整批不可用于晋升判断。")
        return report
    regression = _regressions(regression_pairs, first["baseline"], first["candidate"], seen, batch_groups)
    report["regressions"] = regression
    for item in report["pairs"]:
        report["uncertainties"].extend(item["uncertainties"])
    if report["regressed_cases"] or any(x["regression_detected"] for x in regression["pairs"]):
        report["decision"] = "keep_baseline"
        report["reasons"].append("新批次或历史回归存在退化，禁止以其他案例改善抵消。")
    elif report["improved_cases"]:
        report["decision"] = "needs_review"
        report["reasons"].append("存在改善信号；尚无冻结晋升阈值，需核实证据并继续验证。")
    else:
        report["decision"] = "keep_baseline"
        report["reasons"].append("未观察到分项改善。")
    if not regression["passed"]:
        report["uncertainties"].append("配对回归未完整通过。")
    report["uncertainties"].extend(regression["reasons"])
    return report


def _aware_time(value, label, errors):
    if not isinstance(value, str):
        errors.append("%s 必须为带时区的 ISO 8601 字符串。" % label)
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        errors.append("%s 时间格式无效。" % label)
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append("%s 缺少显式时区，不能判断信息可得时间。" % label)
        return None
    return parsed


def validate_partitions(cases):
    """Check global holdout overlap and point-in-time metadata across all lines.

    Top-level and metadata keys are both read. Group identities must include all
    known revisions, reposts and derivatives; undisclosed relationships cannot
    be inferred from a title or from this validator.
    """
    if not isinstance(cases, list):
        return ["cases 必须是案例列表。"]
    errors, identities, seen_ids = [], {}, set()
    allowed = {"development", "train", "validation", "calibration", "regression", "holdout", "test"}
    reserved = {"holdout", "test"}
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append("案例 %s 必须是对象。" % index)
            continue
        metadata = case.get("metadata", {})
        if not isinstance(metadata, dict):
            errors.append("案例 %s 的 metadata 必须是对象。" % index)
            metadata = {}
        case_id = case.get("id", case.get("case_id", "case[%s]" % index))
        if not isinstance(case_id, str) or not case_id:
            errors.append("案例 %s 标识必须是非空字符串。" % index)
            case_id = "invalid-case[%s]" % index
        if case_id in seen_ids:
            errors.append("案例标识重复：%s。" % case_id)
        seen_ids.add(case_id)
        partition = case.get("partition", metadata.get("partition"))
        if not isinstance(partition, str) or partition not in allowed:
            errors.append("案例 %s 分区无效或缺失：%s。" % (case_id, partition))
            partition = "invalid"
        tokens = set()
        documents = []
        for container in (metadata, case):
            for field, namespace in (("group_ids", "group"), ("document_ids", "doc"),
                                     ("doc_ids", "doc"), ("source_groups", "source"),
                                     ("source_group_ids", "source")):
                values = container.get(field, [])
                if not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values):
                    errors.append("案例 %s 的 %s 必须是非空字符串列表。" % (case_id, field))
                    continue
                tokens.update((namespace, x) for x in values)
            for field, namespace in (("input_digest", "input"), ("group_id", "group"),
                                     ("source_group", "source"), ("source_group_id", "source")):
                if container.get(field):
                    tokens.add((namespace, str(container[field])))
            entries = container.get("documents", [])
            if not isinstance(entries, list):
                errors.append("案例 %s 的 documents 必须是列表。" % case_id)
            else:
                documents.extend(entries)
        cutoff_value = case.get("cutoff", case.get("information_cutoff", metadata.get("cutoff", metadata.get("information_cutoff"))))
        cutoff = _aware_time(cutoff_value, "案例 %s cutoff" % case_id, errors) if cutoff_value else None
        if not cutoff_value:
            errors.append("案例 %s 缺少 information_cutoff/cutoff。" % case_id)
        published_values = []
        for container in (metadata, case):
            if container.get("published_at"):
                published_values.append(("案例 %s" % case_id, container["published_at"]))
        for document in documents:
            if isinstance(document, str) and document:
                tokens.add(("doc", document))
                continue
            if not isinstance(document, dict):
                errors.append("案例 %s 包含无效文档记录。" % case_id)
                continue
            document_id = document.get("id", document.get("document_id"))
            if document_id:
                tokens.add(("doc", str(document_id)))
            for field, namespace in (("group_id", "group"), ("source_group", "source"),
                                     ("source_group_id", "source"), ("sha256", "digest"),
                                     ("digest", "digest")):
                if document.get(field):
                    tokens.add((namespace, str(document[field])))
            for field, namespace in (("group_ids", "group"), ("source_groups", "source")):
                values = document.get(field, [])
                if isinstance(values, list) and all(isinstance(x, str) and x for x in values):
                    tokens.update((namespace, x) for x in values)
                else:
                    errors.append("文档 %s 的 %s 格式无效。" % (document_id, field))
            if document.get("published_at"):
                published_values.append(("案例 %s 文档 %s" % (case_id, document_id), document["published_at"]))
            else:
                errors.append("案例 %s 文档 %s 缺少 published_at，不能用案例时间代替附件披露时间。" % (case_id, document_id))
        if not tokens:
            errors.append("案例 %s 缺少文档或关联组标识，无法核验跨线泄漏。" % case_id)
        if not published_values:
            errors.append("案例 %s 缺少 published_at，不能确认截止时点可得性。" % case_id)
        for label, value in published_values:
            published = _aware_time(value, label + " published_at", errors)
            if published is not None and cutoff is not None and published > cutoff:
                errors.append("%s 披露时间晚于信息截止时间。" % label)
        for token in sorted(tokens):
            for prior_id, prior_partition in identities.get(token, []):
                if (partition in reserved) != (prior_partition in reserved):
                    errors.append("盲测泄漏：%s 与 %s 共享 %s:%s（%s/%s）。" % (
                        prior_id, case_id, token[0], token[1], prior_partition, partition))
            identities.setdefault(token, []).append((case_id, partition))
    return list(dict.fromkeys(errors))


_UNITS = {
    "元": ("CNY", "1"), "万元": ("CNY", "10000"), "亿元": ("CNY", "100000000"),
    "CNY": ("CNY", "1"), "股": ("shares", "1"), "万股": ("shares", "10000"),
    "亿股": ("shares", "100000000"), "shares": ("shares", "1"),
    "%": ("ratio", "0.01"), "百分比": ("ratio", "0.01"), "ratio": ("ratio", "1"),
    "百分点": ("rate_change", "0.01"), "percentage_points": ("rate_change", "0.01"),
    "bp": ("rate_change", "0.0001"), "基点": ("rate_change", "0.0001"),
    "倍": ("multiple", "1"), "multiple": ("multiple", "1"),
}


def _decimal(value):
    if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("金融数值须使用十进制字符串、整数或 Decimal，不能输入二进制浮点数。")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("无效十进制数值：%r。" % value) from exc
    if not number.is_finite():
        raise ValueError("金融数值必须有限。")
    return number


def _precision(numbers):
    return max(50, max(x.adjusted() for x in numbers) - min(x.as_tuple().exponent for x in numbers) + 20)


def normalize_number(value, unit):
    """Normalize known units without inferring currency, period or meaning."""
    if unit not in _UNITS:
        raise ValueError("未知单位：%s；请显式补充单位规范。" % unit)
    number = _decimal(value)
    dimension, factor = _UNITS[unit]
    scale = Decimal(factor)
    with localcontext() as context:
        context.prec = _precision([number, scale])
        normalized = number * scale
    return {"value": str(normalized), "dimension": dimension}


def check_numeric(actual, expected, actual_unit, expected_unit, tolerance="0"):
    """Check exact decimal equality unless an explicit absolute tolerance is given.

    Tolerance uses the normalized base unit. A percentage value and a percentage
    point change are intentionally different dimensions.
    """
    left, right = normalize_number(actual, actual_unit), normalize_number(expected, expected_unit)
    allowed_error = _decimal(tolerance)
    if allowed_error < 0:
        raise ValueError("数值容差不能为负。")
    if left["dimension"] != right["dimension"]:
        return {"matches": False, "actual": left, "expected": right, "absolute_error": None,
                "reason": "单位含义不一致，不能将百分比、百分点、金额或股数直接互换。"}
    values = [Decimal(left["value"]), Decimal(right["value"]), allowed_error]
    with localcontext() as context:
        context.prec = _precision(values)
        difference = abs(values[0] - values[1])
    matches = difference <= allowed_error
    return {"matches": matches, "actual": left, "expected": right, "absolute_error": str(difference),
            "reason": "在显式容差内一致。" if matches else "数值不一致。"}


def check_sum(items, declared_total, tolerance="0"):
    """Independently verify a same-dimension table total with decimal arithmetic."""
    if not isinstance(items, list) or not items:
        raise ValueError("合计检查至少需要一个明细项。")
    normalized = [normalize_number(item["value"], item["unit"]) for item in items]
    total = normalize_number(declared_total["value"], declared_total["unit"])
    if any(item["dimension"] != total["dimension"] for item in normalized):
        return {"matches": False, "reason": "明细与合计的单位含义不一致。", "absolute_error": None}
    values = [Decimal(item["value"]) for item in normalized]
    with localcontext() as context:
        context.prec = _precision(values + [Decimal(total["value"])]) + len(str(len(values)))
        computed = sum(values, Decimal(0))
    canonical_unit = {"CNY": "元", "shares": "股", "ratio": "ratio", "multiple": "倍"}.get(total["dimension"])
    if canonical_unit is None:
        # 百分点变化的标准单位是比例变化；转为基点后再调用同一检查。
        with localcontext() as context:
            context.prec = _precision([computed, Decimal(total["value"])])
            computed *= Decimal(10000)
            expected = Decimal(total["value"]) * Decimal(10000)
        return check_numeric(computed, expected, "bp", "bp", tolerance)
    return check_numeric(computed, total["value"], canonical_unit, canonical_unit, tolerance)
