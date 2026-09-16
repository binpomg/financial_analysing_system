"""配对版本试验与显式、可回退的稳定入口变更。"""
import copy
import os
import uuid

from .evaluation import compare_batch
from .runner import run_case
from .storage import configuration, digest, now


def _policy(config):
    policy = copy.deepcopy(config.get("promotion_policy", {"enabled": False}))
    if not isinstance(policy, dict) or not isinstance(policy.get("enabled"), bool):
        raise ValueError("promotion_policy 必须显式包含布尔 enabled。")
    unknown = set(policy) - {"enabled", "min_validation_cases", "min_regression_cases", "min_improved_cases"}
    if unknown:
        raise ValueError("尚未实现的晋升政策字段不能静默忽略：" + ", ".join(sorted(unknown)))
    if policy["enabled"]:
        for field in ("min_validation_cases", "min_regression_cases", "min_improved_cases"):
            value = policy.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("启用晋升政策前必须显式设置正整数 " + field + "；系统不补默认阈值。")
    return policy


def _exposed_groups(store):
    """汇总全库已被优化消费或预占用于择优的资料组，不限当前业务线。"""
    groups = set()
    for candidate in store.list("candidates"):
        groups.update(candidate.get("development_group_ids", []))
    for feedback in store.list("feedback"):
        groups.update(store.get("runs", feedback["run_id"])["group_ids"])
    for experiment in store.list("experiments"):
        groups.update(experiment.get("validation_group_ids", []))
    return groups


def run_experiment(store, candidate_id, case_ids, regression_case_ids, provider=None, config=None):
    config = copy.deepcopy(config if config is not None else configuration())
    policy = _policy(config)
    candidate = store.get("candidates", candidate_id)
    stable = store.snapshot(candidate["line"])
    if stable["id"] != candidate["parent_version"]:
        raise ValueError("稳定版已变化，候选需要重新建立")
    if not case_ids or not regression_case_ids:
        raise ValueError("试验需要新批次与独立历史回归案例")
    if len(set(case_ids)) != len(case_ids) or len(set(regression_case_ids)) != len(regression_case_ids):
        raise ValueError("预登记案例不能重复；不能将同一次证据重复计数")
    if set(case_ids) & set(regression_case_ids):
        raise ValueError("新批次与回归案例不能重合")
    exposed = _exposed_groups(store)
    validation_groups, regression_groups = set(), set()
    for identifier in case_ids:
        case = store.get("cases", identifier)
        if case["partition"] != "validation" or set(case["group_ids"]) & exposed:
            raise ValueError("新批次必须为全库尚未用于任何业务线优化或版本选择的 validation 文档组")
        validation_groups.update(case["group_ids"])
    for identifier in regression_case_ids:
        case = store.get("cases", identifier)
        if case["partition"] != "regression":
            raise ValueError("历史回归案例用途必须是 regression")
        regression_groups.update(case["group_ids"])
    if validation_groups & regression_groups:
        raise ValueError("新批次与历史回归共享原始资料组，不能算作独立证据")
    identifier = "experiment-" + uuid.uuid4().hex[:16]
    experiment = {"id": identifier, "candidate_id": candidate_id, "line": candidate["line"], "created_at": now(),
                  "worker_pid": os.getpid(),
                  "baseline_version": stable["id"], "candidate_version": candidate["skill_version"],
                  "validation_case_ids": list(case_ids), "regression_case_ids": list(regression_case_ids),
                  "validation_group_ids": sorted(validation_groups), "regression_group_ids": sorted(regression_groups),
                  "promotion_policy": policy, "promotion_policy_digest": digest(policy),
                  "execution_config_digest": digest(config),
                  "pairs": [], "regression_pairs": [], "status": "running"}
    store.put("experiments", identifier, experiment)
    try:
        for group, cases in [("pairs", case_ids), ("regression_pairs", regression_case_ids)]:
            for cid in cases:
                old = run_case(store, cid, candidate["line"], provider=provider, config=config)
                new = run_case(store, cid, candidate["line"], candidate_id=candidate_id, provider=provider, config=config)
                experiment[group].append({"baseline": old, "candidate": new})
                store.put("experiments", identifier, experiment, replace=True)
        experiment["comparison"] = compare_batch(experiment["pairs"], experiment["regression_pairs"])
        experiment["status"] = "evaluated"
    except (KeyboardInterrupt, SystemExit) as error:
        experiment["status"] = "interrupted"
        experiment["error"] = "实验中断：" + type(error).__name__
        experiment["finished_at"] = now()
        store.put("experiments", identifier, experiment, replace=True)
        raise
    except Exception as error:
        experiment["status"] = "failed"
        experiment["error"] = type(error).__name__ + ": " + str(error)
        experiment["finished_at"] = now()
        store.put("experiments", identifier, experiment, replace=True)
        raise
    experiment["finished_at"] = now()
    store.put("experiments", identifier, experiment, replace=True)
    return experiment


def promote(store, experiment_id, reason):
    experiment = store.get("experiments", experiment_id)
    policy = _policy({"promotion_policy": experiment.get("promotion_policy", {"enabled": False})})
    if not policy["enabled"]:
        raise ValueError("实验开始前没有启用并冻结正式晋升政策；当前仅能比较和记录，不能用采用理由绕过。")
    if digest(policy) != experiment.get("promotion_policy_digest"):
        raise ValueError("实验晋升政策快照与指纹不一致，禁止事后修改门槛。")
    if experiment.get("status") != "evaluated" or not isinstance(reason, str) or not reason.strip():
        raise ValueError("仅完整评测结束的实验可按冻结政策申请采用，并需明确人工核验依据。")
    candidate = store.get("candidates", experiment["candidate_id"])
    old = store.snapshot(candidate["line"])
    if old["id"] != candidate["parent_version"] or old["id"] != experiment.get("baseline_version"):
        raise ValueError("稳定版与试验基线不同，禁止覆盖")
    if candidate["skill_version"] != experiment.get("candidate_version") or candidate["line"] != experiment.get("line"):
        raise ValueError("候选版本或业务线与实验预登记不一致。")
    for group, field, partition in (("pairs", "validation_case_ids", "validation"),
                                    ("regression_pairs", "regression_case_ids", "regression")):
        pairs, declared = experiment.get(group), experiment.get(field)
        if not isinstance(pairs, list) or not isinstance(declared, list) or not declared:
            raise ValueError("缺少预登记案例或完整配对：" + group)
        actual = [pair.get("baseline", {}).get("case_id") for pair in pairs]
        if actual != declared or len(set(declared)) != len(declared):
            raise ValueError("实际配对不覆盖预登记案例；禁止删除失败案例或重复计数。")
        for pair in pairs:
            for side, version in (("baseline", old["id"]), ("candidate", candidate["skill_version"])):
                run = pair.get(side, {})
                if run.get("skill_version") != version or run.get("partition") != partition:
                    raise ValueError("配对版本或数据分区与预登记不一致。")
                if digest(store.get("runs", run["id"])) != digest(run):
                    raise ValueError("实验配对与原始运行记录不一致，禁止修改评分或结果后晋升。")
    # 重新计算，不信任外层手写的 decision 或曾经保存的“已通过”布尔值。
    comparison = compare_batch(experiment["pairs"], experiment["regression_pairs"])
    if digest(comparison) != digest(experiment.get("comparison", {})):
        raise ValueError("实验比较结果与原始配对证据不一致。")
    regression = comparison.get("regressions", {})
    if (comparison.get("decision") != "needs_review" or comparison.get("uncertainties")
            or not regression.get("complete") or not regression.get("passed")):
        raise ValueError("未通过完整回归和不确定性检查；采用理由不能替代证据。")
    reports = comparison.get("pairs", []) + regression.get("pairs", [])
    if any(not report.get("comparable") or report.get("regression_detected") or report.get("uncertainties") for report in reports):
        raise ValueError("存在不可比、退化或尚未核实的分项证据。")
    all_pairs = experiment["pairs"] + experiment["regression_pairs"]
    for pair in all_pairs:
        audit = pair["candidate"]["audit"]
        if audit["verdict"] != "pass" or any(issue["severity"] in {"critical", "major"} for issue in audit["issues"]):
            raise ValueError("候选仍未通过审核或残留重大错误，不能晋升。")
    counts = {"min_validation_cases": len(experiment["pairs"]),
              "min_regression_cases": len(experiment["regression_pairs"]),
              "min_improved_cases": comparison["improved_cases"]}
    if any(counts[field] < policy[field] for field in counts):
        raise ValueError("实际验证／回归／改善案例数量未达到实验前冻结的下限。")
    action = {"id": "adoption-" + uuid.uuid4().hex[:16], "line": candidate["line"], "from_version": old["id"],
              "to_version": candidate["skill_version"], "experiment_id": experiment_id, "reason": reason,
              "decision_type": "pre_registered_policy_and_human_review",
              "promotion_policy_digest": experiment["promotion_policy_digest"], "created_at": now()}
    store.put("adoptions", action["id"], action)
    store.put("stable", candidate["line"], {"skill_version": candidate["skill_version"], "changed_at": now(), "adoption_id": action["id"]}, replace=True)
    return action


def rollback(store, line_id, adoption_id, reason):
    action = store.get("adoptions", adoption_id)
    if action["line"] != line_id or not reason.strip():
        raise ValueError("需要匹配业务线的采用记录与回退依据")
    current = store.snapshot(line_id)
    if current["id"] != action["to_version"]:
        raise ValueError("当前版本不是该采用记录的结果，不能回退未知状态")
    reversal = {"id": "adoption-" + uuid.uuid4().hex[:16], "line": line_id, "from_version": current["id"],
                "to_version": action["from_version"], "reason": reason, "created_at": now(), "reverts": adoption_id}
    store.put("adoptions", reversal["id"], reversal)
    store.put("stable", line_id, {"skill_version": action["from_version"], "changed_at": now(), "adoption_id": reversal["id"]}, replace=True)
    return reversal
