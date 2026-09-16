"""按需小批次：采集、七线全文处理、独立核实与候选积累。"""
import copy
import os
import uuid
from pathlib import Path

from .feedback_review import review_run_feedback
from .ingestion_queue import collect_for_pipeline, mark_assigned
from .lifecycle import ACTIVE, owner_state
from .provider import APIProvider
from .runner import capture_environment, propose_candidate, run_case
from .storage import PROJECT, configuration, digest, now, safe_id


TASK = """完整阅读当前材料包的全部原文、表格、图像、单位、脚注及已提供附件。
按照当前业务线Skill，完成本份材料能够支持的本线研究任务，保留有依据的结构化信息、分析、证据和待补资料。
这是公开原件的开发案例，不是标准答案。各线独立判断适用性，不使用其他业务线的分析结果，也不依据标题或摘要跳过原件。
仅有单份公告时，完成证据支持的局部任务，明确不能完成完整财报、产业研究、估值、备忘录或报告评估的原因及所缺材料；不得编造补齐。
若当前材料本身不是待核查/评价的报告，按本线规范在全文阅读后说明适用性。原件内提到但未提供的必要附件或历史文件须列入待补资料。
计算使用工具；保留主体、期间、原值、单位和金融口径。正文简明，引用取足够支持结论的连续原文，避免重复粘贴整表。
"""


def settings(config):
    value = {"max_documents": 1, "max_feedback_issues_per_run": 2,
             "min_feedback_cases": 3, "max_candidates_per_batch": 1}
    extra = config.get("pipeline", {})
    if set(extra) - set(value):
        raise ValueError("未知自动批次配置项")
    value.update(extra)
    if value["max_documents"] != 1 or isinstance(value["max_documents"], bool):
        raise ValueError("当前按用户约定，每批最多处理1份原件")
    for key in ("max_feedback_issues_per_run", "min_feedback_cases", "max_candidates_per_batch"):
        if not isinstance(value[key], int) or isinstance(value[key], bool) or value[key] < 1:
            raise ValueError("自动批次的反馈与候选限制必须为正整数")
    if value["max_feedback_issues_per_run"] > 10 or value["max_candidates_per_batch"] > 7:
        raise ValueError("自动批次超过反馈或候选调用限制")
    return value


def _freeze(store, config, provider, lines):
    environment, _ = capture_environment(provider, config)
    methods = {}
    for line_id in lines:
        line = store.line(line_id)
        if not line["enabled"]:
            raise ValueError("自动批次中的业务线未启用：" + line_id)
        methods[line_id] = {
            "skill_version": store.snapshot(line_id)["id"],
            "review": digest((PROJECT / "resources" / line["review_path"]).read_text(encoding="utf-8")),
            "criteria": digest((PROJECT / "resources" / line["criteria_path"]).read_text(encoding="utf-8")),
        }
    return {"environment_digest": digest(environment), "models": copy.deepcopy(config["models"]),
            "methods": methods, "settings": settings(config), "task_digest": digest(TASK)}


def _save(store, batch):
    batch["updated_at"] = now()
    return store.put("pipelines", batch["id"], batch, replace=True)


def _create_or_recover_case(store, batch, document_id):
    """创建前登记归属；仅恢复本批唯一且完整匹配的案例，不凭相似任务收养案例。"""
    reservation = batch.get("case_reservation")
    if reservation is None:
        if any(document_id in case.get("document_ids", []) for case in store.list("cases")):
            raise ValueError("选定原件已有案例但本批未预登记归属，不能自动接管")
        reservation = {"case_id": "case-" + uuid.uuid4().hex[:16], "pipeline_id": batch["id"],
                       "document_ids": [document_id], "task": TASK, "cutoff": now(),
                       "partition": "development", "expected_attachments": []}
        batch["case_reservation"] = reservation
        _save(store, batch)
    fields = {"case_id", "pipeline_id", "document_ids", "task", "cutoff", "partition", "expected_attachments"}
    if (not isinstance(reservation, dict) or set(reservation) != fields
            or reservation["pipeline_id"] != batch["id"] or reservation["document_ids"] != [document_id]
            or reservation["task"] != TASK or reservation["partition"] != "development"
            or reservation["expected_attachments"] != []):
        raise ValueError("案例预登记与本批原件、任务或用途不一致，停止恢复")
    safe_id(reservation["case_id"])
    origin = {"kind": "pipeline", "pipeline_id": batch["id"], "reservation_digest": digest(reservation)}
    related = [case for case in store.list("cases")
               if document_id in case.get("document_ids", []) or case.get("id") == reservation["case_id"]
               or case.get("origin", {}).get("pipeline_id") == batch["id"]]
    if related:
        if len(related) != 1:
            raise ValueError("本批案例归属不唯一，停止恢复；不重建或覆盖案例")
        case = related[0]
        scope = ("task", "cutoff", "partition", "document_ids", "expected_attachments")
        document = store.get("documents", document_id)
        metadata = [{"id": document["id"], "group_id": document["group_id"], "published_at": document["published_at"]}]
        if (case.get("id") != reservation["case_id"] or case.get("origin") != origin
                or any(case.get(key) != reservation[key] for key in scope)
                or case.get("documents") != metadata or case.get("group_ids") != [document["group_id"]]
                or case.get("input_digest") != digest({key: reservation[key]
                    for key in ("task", "cutoff", "document_ids", "expected_attachments")})):
            raise ValueError("既存案例不完整匹配本批预登记归属与输入，不能自动接管")
    else:
        case = store.create_case(reservation["document_ids"], reservation["task"], reservation["cutoff"],
                                 reservation["partition"], reservation["expected_attachments"],
                                 case_id=reservation["case_id"], origin=origin)
    if batch.get("steps") or any(run.get("case_id") == case["id"] for run in store.list("runs")):
        raise ValueError("尚未登记到本批的案例已有执行记录，停止恢复以避免重复请求")
    return case


def _eligible_feedback(store, line_id, version_id):
    used = {fid for candidate in store.list("candidates") for fid in candidate.get("feedback_ids", [])}
    # 即使候选生成未返回结果，该次已经尝试过的反馈也不在后续批次重复收费。
    used.update(fid for batch in store.list("pipelines") for attempt in batch.get("candidate_attempts", [])
                for fid in attempt.get("feedback_ids", []))
    all_feedback = store.list("feedback")
    used_issues = {(item["run_id"], item["issue_index"]) for item in all_feedback if item["id"] in used}
    feedback, components = [], []
    seen = set()
    for item in all_feedback:
        if item["id"] in used or item["line"] != line_id:
            continue
        run = store.get("runs", item["run_id"])
        key = (item["run_id"], item["issue_index"])
        if (key in seen or key in used_issues or run["skill_version"] != version_id or run["partition"] != "development"
                or run.get("status") != "audited" or not run.get("group_ids")):
            continue
        if item.get("verification_kind") == "independent_model_check":
            record = store.get("feedback_verifications", item["verification_id"])
            if record["status"] != "completed" or item["id"] not in record.get("feedback_ids", []):
                continue
        seen.add(key)
        feedback.append(item["id"])
        # 同源原件建多个案例不增加独立样本数，跨组材料包还需合并相交组。
        related = set(run["group_ids"])
        separate = []
        for component in components:
            if related & component:
                related.update(component)
            else:
                separate.append(component)
        components = separate + [related]
    return feedback, components


def _reconcile(store, batch):
    """仅关联已保存成果；请求发出后缺少终态的步骤不会被隐式重试。"""
    for step in batch["steps"]:
        if step["status"] == "running":
            matches = [run for run in store.list("runs")
                       if run["case_id"] == step["case_id"] and run["line"] == step["line"]
                       and run["created_at"] >= step["started_at"]]
            if len(matches) == 1 and matches[0]["status"] not in ACTIVE:
                step.update(run_id=matches[0]["id"], status="done",
                            run_status=matches[0]["status"], release_status=matches[0]["release_status"])
            else:
                step.update(status="interrupted", error="该次执行缺少可唯一关联的终态；保留现场，不重复请求")
        if step.get("feedback_status") == "running":
            found = [record for record in store.list("feedback_verifications") if record["run_id"] == step.get("run_id")]
            if found and all(record["status"] in {"completed", "no_issues", "already_reviewed"} for record in found):
                step.update(feedback_status="recovered", verification_ids=[record["id"] for record in found])
            else:
                step.update(feedback_status="interrupted", feedback_error="核实步骤未取得终态，未自动重试")
    for attempt in batch.get("candidate_attempts", []):
        if attempt["status"] != "running":
            continue
        matches = [candidate for candidate in store.list("candidates")
                   if candidate["line"] == attempt["line"] and set(candidate["feedback_ids"]) == set(attempt["feedback_ids"])
                   and candidate["created_at"] >= attempt["started_at"]]
        if len(matches) == 1:
            attempt.update(status="candidate_unvalidated", candidate_id=matches[0]["id"])
        else:
            attempt.update(status="interrupted", error="候选生成未取得可唯一关联终态，未自动重试")


def run_pipeline(store, pipeline_id=None, provider=None, config=None, *, new_pipeline_id=None, campaign_id=None):
    """调用方持有Store写锁。新批次最多1份；显式续批只执行尚未发起的步骤。"""
    config = config or configuration()
    provider = provider or APIProvider(config)
    policy = settings(config)
    if new_pipeline_id is not None:
        safe_id(new_pipeline_id)
        if pipeline_id is not None:
            raise ValueError("新建批次ID与续批ID不能同时提供")
    if campaign_id is not None:
        safe_id(campaign_id)
    if pipeline_id:
        batch = store.get("pipelines", pipeline_id)
        if campaign_id is not None and batch.get("campaign_id") != campaign_id:
            raise ValueError("批次不属于本次连续训练任务，不能自动接管")
        if batch["status"] in {"completed", "completed_with_issues", "no_documents", "blocked", "failed"}:
            return batch
        if batch["status"] in ACTIVE and owner_state(batch.get("worker_pid"), batch.get("worker_started_at", batch["created_at"])) != "dead":
            raise ValueError("原批次进程仍存活或无法确认，不启动重复批次")
        if batch["frozen"] != _freeze(store, config, provider, batch["lines"]):
            raise ValueError("代码、模型、方法或评价环境已变化，不能续接该自动批次")
        if batch["collection_status"] != "completed":
            batch.update(status="blocked", error="原采集步骤未完成；保留队列与现场，请明确检查后新建批次", finished_at=now())
            return _save(store, batch)
        _reconcile(store, batch)
        batch.update(status="running", worker_pid=os.getpid(), worker_started_at=now())
    else:
        lines = list(dict.fromkeys(config["default_enabled_lines"]))
        if not lines:
            raise ValueError("没有已配置业务线")
        batch = {"id": new_pipeline_id or "pipeline-" + uuid.uuid4().hex[:16], "created_at": now(), "status": "running",
                 "worker_pid": os.getpid(), "worker_started_at": now(), "lines": lines,
                 "frozen": _freeze(store, config, provider, lines), "document_ids": [], "case_ids": [],
                 "collection_ids": [], "collection_status": "pending", "steps": [], "candidate_attempts": [],
                 "candidate_waiting": [], "automatic_promotion": False}
        if campaign_id is not None:
            batch["campaign_id"] = campaign_id
        store.put("pipelines", batch["id"], batch)
    try:
        _save(store, batch)
        if batch["collection_status"] == "pending":
            batch["collection_status"] = "running"
            _save(store, batch)
            intake = collect_for_pipeline(store, max_documents=1, provider=provider, config=config)
            batch.update(collection_status="completed", intake=intake, document_ids=intake["document_ids"],
                         collection_ids=intake.get("collection_ids", []))
            if len(batch["document_ids"]) > 1:
                raise ValueError("采集超过本批1份原件限制")
            _save(store, batch)
        if not batch["document_ids"]:
            intake = batch.get("intake", {})
            batch.update(status="blocked" if intake.get("errors") or intake.get("blocked") else "no_documents", finished_at=now())
            return _save(store, batch)
        for doc_id in batch["document_ids"]:
            if not batch["case_ids"]:
                # 自动材料仅进入开发用途，不把同一原件改标签充当新验证/保留集。
                mark_assigned(store, doc_id, batch["id"])
                case = _create_or_recover_case(store, batch, doc_id)
                batch["case_ids"].append(case["id"])
                batch["steps"] = [{"line": line_id, "case_id": case["id"], "status": "pending", "feedback_status": "pending"}
                                  for line_id in batch["lines"]]
                _save(store, batch)
                mark_assigned(store, doc_id, batch["id"], case["id"])
        for step in batch["steps"]:
            if step["status"] == "pending":
                if batch["frozen"] != _freeze(store, config, provider, batch["lines"]):
                    raise ValueError("批次执行期间方法或运行环境发生变化，已暂停后续步骤")
                step.update(status="running", started_at=now())
                _save(store, batch)
                run = run_case(store, step["case_id"], step["line"], provider=provider, config=config)
                step.update(status="done", run_id=run["id"], run_status=run["status"], release_status=run["release_status"])
                _save(store, batch)
            if step.get("run_status") != "audited" or step["feedback_status"] != "pending":
                continue
            step["feedback_status"] = "running"
            _save(store, batch)
            try:
                verification = review_run_feedback(store, step["run_id"], policy["max_feedback_issues_per_run"], provider, config)
                step.update(feedback_status=verification["status"], verification_ids=[verification["id"]],
                            feedback_ids=verification.get("feedback_ids", []),
                            remaining_issue_indices=verification.get("remaining_issue_indices", []))
            except Exception as error:
                step.update(feedback_status="failed", feedback_error=str(error))
            _save(store, batch)
        for line_id in batch["lines"]:
            if len(batch["candidate_attempts"]) >= policy["max_candidates_per_batch"]:
                break
            if batch["frozen"] != _freeze(store, config, provider, batch["lines"]):
                raise ValueError("候选生成前方法或运行环境已变化，已停止本批次")
            version_id = batch["frozen"]["methods"][line_id]["skill_version"]
            if any(c["line"] == line_id and c["parent_version"] == version_id and c["status"] == "candidate_unvalidated"
                   for c in store.list("candidates")):
                batch["candidate_waiting"].append({"line": line_id, "reason": "已有同稳定版本候选等待新批次与回归验证"})
                continue
            feedback_ids, cases = _eligible_feedback(store, line_id, version_id)
            if len(cases) < policy["min_feedback_cases"]:
                batch["candidate_waiting"].append({"line": line_id, "confirmed_case_count": len(cases),
                                                    "reason": "继续积累独立开发案例反馈"})
                continue
            attempt = {"line": line_id, "feedback_ids": feedback_ids, "status": "running", "started_at": now()}
            batch["candidate_attempts"].append(attempt)
            _save(store, batch)
            try:
                candidate = propose_candidate(store, line_id, feedback_ids, provider, config)
                attempt.update(status="candidate_unvalidated", candidate_id=candidate["id"],
                               next_step="预登记新的validation材料与独立regression案例后运行experiment；不会自动晋升")
            except Exception as error:
                attempt.update(status="failed", error=str(error))
            _save(store, batch)
        unresolved = (any(step.get("run_status") != "audited" or step.get("release_status") == "requires_review"
                          or step.get("feedback_status") in {"failed", "interrupted"} for step in batch["steps"])
                      or bool(batch.get("intake", {}).get("errors"))
                      or any(a["status"] in {"failed", "interrupted"} for a in batch["candidate_attempts"]))
        batch.update(status="completed_with_issues" if unresolved else "completed", finished_at=now())
    except (KeyboardInterrupt, SystemExit):
        batch.update(status="interrupted", error="批次中断；显式续批只执行尚未发起的步骤", finished_at=now())
        _save(store, batch)
        raise
    except Exception as error:
        batch.update(status="failed", error=str(error), finished_at=now())
    return _save(store, batch)
