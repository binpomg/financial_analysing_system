"""有限材料任务：逐份完整执行、持久登记进度、停止后不自动重复付费请求。"""
import copy
import os
import uuid
from datetime import datetime, timedelta, timezone

from .collection import safe_collection_error
from .lifecycle import ACTIVE, owner_state, recover_interrupted
from .pipeline import _freeze, run_pipeline
from .provider import APIProvider
from .storage import configuration, digest, now, safe_id


API_FIELDS = {"credential_source", "codex_config_path", "protocol", "base_url_env", "api_key_env",
              "timeout_seconds", "max_output_tokens", "max_response_bytes", "max_tool_rounds", "accepted_reported_models"}
FINISHED = {"completed", "completed_with_issues"}


def campaign_configuration(base, end_date=None):
    """冻结公开窗口与非敏感配置；密钥仍在请求时读取，不进入任务快照。"""
    if set(base["api"]) - API_FIELDS:
        raise ValueError("存在未支持的API配置字段，不能直接写入训练快照")
    config = copy.deepcopy(base)
    end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else datetime.now(timezone(timedelta(hours=8))).date()
    start = end - timedelta(days=90)
    config["sources"] = [
        {"adapter": "cninfo", "query": query, "start_date": str(start), "end_date": str(end),
         "page_size": 30, "max_pages": 10, "extensions": [".pdf"],
         "allowed_hosts": ["www.cninfo.com.cn", "static.cninfo.com.cn"]}
        for query in ("中标", "经营数据")]
    config["pipeline"]["max_documents"] = 1
    if config["promotion_policy"].get("enabled") is not False:
        raise ValueError("本次连续训练只积累候选，要求正式晋升策略保持关闭")
    expected = {"discovery": {"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
                **{role: {"model": "gpt-6-astra", "reasoning_effort": "xhigh"}
                   for role in ("business", "audit", "improvement")}}
    if config["models"] != expected or len(set(config["default_enabled_lines"])) != 7:
        raise ValueError("本次训练必须保留已指定模型、极高推理及完整七线")
    return config


def create_campaign(store, target_documents=100, config=None, provider=None):
    """调用方持有写锁；只建有限任务，不发起模型请求。"""
    if isinstance(target_documents, bool) or not isinstance(target_documents, int) or not 1 <= target_documents <= 100:
        raise ValueError("连续训练材料数量必须在1—100之间")
    if (any(item.get("status") in ACTIVE | {"prepared"} for item in store.list("campaigns"))
            or any(item.get("status") in ACTIVE for item in store.list("pipelines") + store.list("jobs"))):
        raise ValueError("已有活动任务，不能重复建立连续训练")
    base = config or configuration()
    runtime_config = campaign_configuration(base)
    provider = provider or APIProvider(runtime_config)
    record = {"id": "campaign-" + uuid.uuid4().hex[:16], "status": "prepared", "created_at": now(),
              "target_documents": target_documents, "document_ids": [], "pipelines": [], "current_pipeline_id": None,
              "worker_pid": None, "worker_started_at": None, "stop_requested": False,
              "runtime_config": runtime_config, "base_config_digest": digest(base),
              "runtime_config_digest": digest(runtime_config),
              "frozen": _freeze(store, runtime_config, provider, runtime_config["default_enabled_lines"]),
              "max_pipeline_attempts": target_documents * 3, "consecutive_unproductive": 0,
              "automatic_promotion": False, "progress": {}}
    return _save(store, record)


def _progress(store, campaign):
    documents, processed, runs = set(), set(), {}
    pipeline_ids = {entry["pipeline_id"] for entry in campaign["pipelines"]}
    verification_ids, feedback_ids, candidate_ids = set(), set(), set()
    for pipeline_id in pipeline_ids:
        if not store.path("pipelines", pipeline_id).exists():
            continue
        batch = store.get("pipelines", pipeline_id)
        if batch.get("campaign_id") != campaign["id"]:
            raise ValueError("已登记子批次的训练归属不匹配")
        documents.update(batch.get("document_ids", []))
        steps = batch.get("steps", [])
        if (batch["status"] in FINISHED and len(steps) == 7
                and all(step["status"] in {"done", "interrupted"} for step in steps)):
            processed.update(batch.get("document_ids", []))
        for step in steps:
            if step.get("run_id"):
                runs[step["run_id"]] = store.get("runs", step["run_id"])
            verification_ids.update(step.get("verification_ids", []))
        candidate_ids.update(a["candidate_id"] for a in batch.get("candidate_attempts", []) if a.get("candidate_id"))
    for identifier in verification_ids:
        record = store.get("feedback_verifications", identifier)
        if record["status"] == "completed":
            feedback_ids.update(record.get("feedback_ids", []))
    campaign["document_ids"] = sorted(documents)
    campaign["progress"] = {
        "attempted_documents": len(documents), "processed_documents": len(processed),
        "audited_runs": sum(r["status"] == "audited" for r in runs.values()),
        "failed_runs": sum(r["status"] in {"failed", "interrupted", "reading_incomplete"} for r in runs.values()),
        "audit_pass_runs": sum((r.get("audit") or {}).get("verdict") == "pass" for r in runs.values()),
        "requires_review_runs": sum(r.get("release_status") == "requires_review" for r in runs.values()),
        "feedback_count": len(feedback_ids), "candidate_count": len(candidate_ids), "stable_promotions": 0}


def _save(store, campaign):
    _progress(store, campaign)
    campaign["updated_at"] = now()
    return store.put("campaigns", campaign["id"], campaign, replace=True)


def _stop(store, campaign, status, reason):
    campaign.update(status=status, error=reason, finished_at=now())
    return _save(store, campaign)


def run_campaign(store, campaign_id, provider=None, config=None):
    """调用方全程持有写锁。显式续接仅接管本任务预约的子批次，不重试其失败阶段。"""
    safe_id(campaign_id)
    campaign = store.get("campaigns", campaign_id)
    if campaign["status"] in FINISHED:
        return campaign
    if campaign["status"] in ACTIVE and owner_state(campaign.get("worker_pid"), campaign.get("worker_started_at")) != "dead":
        raise ValueError("训练进程仍存在或无法确认，拒绝重复执行")
    base = config or configuration()
    runtime_config = campaign["runtime_config"]
    provider = provider or APIProvider(runtime_config)
    if digest(base) != campaign["base_config_digest"] or digest(runtime_config) != campaign["runtime_config_digest"]:
        raise ValueError("运行配置已变化，不能自动续接该训练")
    if _freeze(store, runtime_config, provider, runtime_config["default_enabled_lines"]) != campaign["frozen"]:
        raise ValueError("代码、模型、Skill或审核标准已变化，不能续接该训练")
    recover_interrupted(store)
    campaign.update(status="running", worker_pid=os.getpid(), worker_started_at=now())
    campaign.pop("finished_at", None)
    campaign.pop("error", None)
    _save(store, campaign)
    try:
        while True:
            _progress(store, campaign)
            if campaign["progress"]["processed_documents"] >= campaign["target_documents"]:
                issues = (campaign["progress"]["failed_runs"] or campaign["progress"]["requires_review_runs"]
                          or any(entry["status"] == "completed_with_issues" for entry in campaign["pipelines"]))
                campaign.update(status="completed_with_issues" if issues else "completed", finished_at=now(), current_pipeline_id=None)
                return _save(store, campaign)
            if store.path("campaign_stop_requests", campaign_id).exists():
                campaign["stop_requested"] = True
                return _stop(store, campaign, "paused", "已按暂停请求停止后续材料；已发起阶段及原结果保留")
            current = next((entry for entry in campaign["pipelines"] if entry["status"] == "running"), None)
            if current is None:
                if len(campaign["document_ids"]) >= campaign["target_documents"]:
                    return _stop(store, campaign, "paused", "原件数量已到上限，但存在未完成七线步骤；保留现场，未补做或超量采集")
                if len(campaign["pipelines"]) >= campaign["max_pipeline_attempts"]:
                    return _stop(store, campaign, "paused", "已到有限采集尝试上限，未继续增加请求")
                if digest(config or configuration()) != campaign["base_config_digest"]:
                    return _stop(store, campaign, "paused", "训练期间配置发生变化，暂停后续材料")
                if _freeze(store, runtime_config, provider, runtime_config["default_enabled_lines"]) != campaign["frozen"]:
                    return _stop(store, campaign, "paused", "训练期间代码或方法发生变化，暂停后续材料")
                current = {"pipeline_id": "pipeline-" + uuid.uuid4().hex[:16], "status": "running",
                           "started_at": now(), "document_ids": []}
                campaign["pipelines"].append(current)
                campaign["current_pipeline_id"] = current["pipeline_id"]
                _save(store, campaign)
            pipeline_id = current["pipeline_id"]
            if store.path("pipelines", pipeline_id).exists():
                batch = store.get("pipelines", pipeline_id)
                if batch.get("campaign_id") != campaign_id:
                    raise ValueError("预约的批次ID已有其他归属，拒绝接管")
                batch = run_pipeline(store, pipeline_id, provider, runtime_config, campaign_id=campaign_id)
            else:
                batch = run_pipeline(store, provider=provider, config=runtime_config,
                                     new_pipeline_id=pipeline_id, campaign_id=campaign_id)
            current.update(status=batch["status"], document_ids=batch.get("document_ids", []),
                           finished_at=batch.get("finished_at"), error=batch.get("error"))
            campaign["current_pipeline_id"] = None
            audited = sum(step.get("run_status") == "audited" for step in batch.get("steps", []))
            campaign["consecutive_unproductive"] = 0 if audited else campaign["consecutive_unproductive"] + 1
            _save(store, campaign)
            if batch["status"] in {"failed", "interrupted"}:
                return _stop(store, campaign, "paused", "子批次未正常完成，保留已发起请求；需检查后显式恢复")
            if not batch.get("document_ids") and not batch.get("intake", {}).get("download_attempts"):
                if batch.get("intake", {}).get("errors"):
                    return _stop(store, campaign, "paused", "公开目录采集失败，保留错误；未将接口异常当成来源穷尽或自动重试")
                return _stop(store, campaign, "source_exhausted", "固定来源本轮无新可用原件或目录失败；未将来源不足记为100份完成")
            if campaign["consecutive_unproductive"] >= 3 and campaign["progress"]["processed_documents"] < campaign["target_documents"]:
                return _stop(store, campaign, "paused", "连续三份/轮次未取得任何完整独立审核，暂停后续费用并保留失败记录")
    except (KeyboardInterrupt, SystemExit):
        _stop(store, campaign, "interrupted", "训练进程中断；显式恢复仅继续尚未发起步骤")
        raise
    except Exception as error:
        return _stop(store, campaign, "failed", safe_collection_error(error, runtime_config))
