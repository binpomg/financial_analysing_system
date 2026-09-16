"""按需有界采集与原件候选队列；技术去重不代替各业务线阅读全文。"""

import os
import uuid
from datetime import datetime, timezone

from .collection import collect_once, safe_collection_error
from .storage import configuration, digest, now


def _usage(store, claimed_document_ids=None):
    document_ids = set(claimed_document_ids or [])
    group_ids = set()
    documents = {item["id"]: item for item in store.list("documents")}
    for category in ("cases", "pipelines"):
        for item in store.list(category):
            document_ids.update(item.get("document_ids", []))
            group_ids.update(item.get("group_ids", []))
    for identifier in document_ids:
        if identifier in documents:
            group_ids.add(documents[identifier].get("group_id"))
    group_ids.discard(None)
    return document_ids, group_ids


def _readiness(document):
    reasons = []
    published = document.get("published_at")
    if not published:
        reasons.append("missing_publication_time")
    else:
        try:
            parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                reasons.append("publication_timezone_missing")
            elif parsed > datetime.now(timezone.utc):
                reasons.append("future_publication_time")
        except (ValueError, TypeError, AttributeError):
            reasons.append("invalid_publication_time")
    prepared = document.get("prepared", {})
    if prepared.get("status") != "ready" or not prepared.get("units"):
        reasons.append("document_preparation_incomplete")
    return reasons


def _sync_queue(store, claimed_document_ids=None):
    used_documents, used_groups = _usage(store, claimed_document_ids)
    membership = {}
    documents_by_url = {document.get("source_url"): document["id"] for document in store.list("documents")
                        if document.get("source_url")}
    for entry in store.list("ingestion_queue"):
        membership[entry["document_id"]] = list(entry.get("collection_ids", []))
    for collection in store.list("collections"):
        collection_documents = list(collection.get("document_ids", []))
        # 崩溃可能发生在原件已入库、collection尚未回写ID之间；仅凭原始URL重建引用。
        for attempt in collection.get("download_records", []):
            if attempt.get("url") in documents_by_url:
                collection_documents.append(documents_by_url[attempt["url"]])
        for identifier in dict.fromkeys(collection_documents):
            membership.setdefault(identifier, [])
            if collection["id"] not in membership[identifier]:
                membership[identifier].append(collection["id"])
    entries = []
    for document_id, collection_ids in membership.items():
        identifier = "ingest-" + document_id
        prior = store.get("ingestion_queue", identifier) if store.path("ingestion_queue", identifier).exists() else {}
        entry = {**prior, "id": identifier, "document_id": document_id,
                 "created_at": prior.get("created_at", now()), "updated_at": now(),
                 "collection_ids": collection_ids, "selection_method": "technical_integrity_and_usage_only",
                 "business_applicability": "requires_independent_full_reading"}
        if not store.path("documents", document_id).exists():
            entry.update(status="blocked", reasons=["document_record_missing"])
        else:
            document = store.get("documents", document_id)
            entry.update(group_id=document.get("group_id"), source_url=document.get("source_url"),
                         published_at=document.get("published_at"))
            if document_id in used_documents:
                entry.update(status="assigned", reasons=["document_already_claimed"])
            elif document.get("group_id") in used_groups:
                entry.update(status="reserved", reasons=["document_group_already_used"])
            else:
                reasons = _readiness(document)
                entry.update(status="blocked" if reasons else "ready", reasons=reasons)
        store.put("ingestion_queue", identifier, entry, replace=True)
        entries.append(entry)
    return sorted(entries, key=lambda item: (item["created_at"], item["id"]))


def mark_assigned(store, document_id, pipeline_id, case_id=None):
    """认领事实先保存在 pipeline；队列只是可重建的索引。"""
    pipeline = store.get("pipelines", pipeline_id)
    if document_id not in pipeline.get("document_ids", []):
        raise ValueError("必须先在自动批次 document_ids 中持久登记原件，再标记队列认领")
    if case_id and document_id not in store.get("cases", case_id).get("document_ids", []):
        raise ValueError("案例没有引用待认领原件")
    _sync_queue(store)
    identifier = "ingest-" + document_id
    entry = store.get("ingestion_queue", identifier)
    entry.update(status="assigned", pipeline_id=pipeline_id, updated_at=now())
    if case_id:
        entry["case_id"] = case_id
    return store.put("ingestion_queue", identifier, entry, replace=True)


def collect_for_pipeline(store, max_documents=1, provider=None, config=None, claimed_document_ids=None):
    """先复用已采集待处理原件，再按来源尝试；不建案例、不调用业务模型。

    调用者持有资料库写入锁，并在返回后、执行模型前将 document_ids 写入
    pipelines。每批全部来源合计最多 max_documents 次原件下载尝试；目录错误
    不消耗下载额度。缺时点/准备不完整的已下载材料留在 blocked，不偷换成不适用。
    """
    if isinstance(max_documents, bool) or not isinstance(max_documents, int) or not 1 <= max_documents <= 1000:
        raise ValueError("每批原件上限必须在 1—1000 之间")
    config = config or configuration()
    sources = config.get("sources", [])
    identifier = "ingestion-" + uuid.uuid4().hex[:16]
    batch = {"id": identifier, "created_at": now(), "status": "running", "stage": "collecting", "max_documents": max_documents,
             "worker_pid": os.getpid(), "worker_started_at": now(),
             "document_ids": [], "queue_ids": [], "collection_ids": [], "errors": [], "blocked": [],
             "source_scope": [], "has_more": False, "coverage_complete": False, "download_attempts": 0,
             "sources_digest": digest(sources), "source_start_index": None,
             "source_order": list(range(len(sources))), "source_rotation_advanced": False,
             "selection_method": "technical_integrity_and_usage_only",
             "business_applicability": "requires_each_line_full_reading",
             "deduplication_note": "原件内容指纹、已登记URL、case/pipeline及其已知文档组技术去重；未自动识别未登记的转载/同事件关系"}
    store.put("ingestion_batches", identifier, batch)

    def refresh_selection():
        entries = _sync_queue(store, claimed_document_ids)
        ready = [entry for entry in entries if entry["status"] == "ready"]
        chosen = ready[:max_documents]
        batch["document_ids"] = [entry["document_id"] for entry in chosen]
        batch["queue_ids"] = [entry["id"] for entry in chosen]
        batch["blocked"] = [{"document_id": entry["document_id"], "queue_id": entry["id"],
                             "reasons": entry["reasons"], "status": entry["status"]}
                            for entry in entries if entry["status"] in {"blocked", "reserved"}]
        for entry in chosen:
            for collection_id in entry["collection_ids"]:
                if collection_id not in batch["collection_ids"]:
                    batch["collection_ids"].append(collection_id)
        batch["queued_ready_remaining"] = max(0, len(ready) - len(chosen))
        batch["collection_scope"] = [{"collection_id": collection_id,
                                      "source_scope": store.get("collections", collection_id).get("source_scope", {}),
                                      "has_more": store.get("collections", collection_id).get("has_more")}
                                     for collection_id in batch["collection_ids"]
                                     if store.path("collections", collection_id).exists()]
        return chosen

    def save():
        batch["updated_at"] = now()
        store.put("ingestion_batches", identifier, batch, replace=True)

    refresh_selection()
    try:
        enabled_indices = [index for index, source in enumerate(sources) if source.get("enabled") is not False]
        if len(batch["document_ids"]) < max_documents and enabled_indices:
            # 只在实际需要新采集时轮换；排首来源持续有资料也不能占满以后每批额度。
            start = enabled_indices[0]
            for prior in reversed(store.list("ingestion_batches")):
                if (prior.get("sources_digest") == batch["sources_digest"] and prior.get("source_rotation_advanced")
                        and prior.get("source_start_index") in enabled_indices):
                    start = enabled_indices[(enabled_indices.index(prior["source_start_index"]) + 1) % len(enabled_indices)]
                    break
            batch.update(source_start_index=start, source_order=list(range(start, len(sources))) + list(range(start)),
                         source_rotation_advanced=True)
            save()
        for index in batch["source_order"]:
            source = sources[index]
            scope = {"source_index": index, "source_key": digest(source)[:24], "configured_source": source}
            if source.get("enabled") is False:
                scope["status"] = "disabled"
                batch["source_scope"].append(scope)
                continue
            remaining = min(max_documents - len(batch["document_ids"]), max_documents - batch["download_attempts"])
            if remaining <= 0:
                scope.update(status="not_attempted", reason="batch_limit_or_existing_queue", has_more=None)
                batch["source_scope"].append(scope)
                continue
            try:
                collected = collect_once(store, source, limit=remaining, provider=provider, config=config,
                                         reuse_pending_listing=True)
            except Exception as error:
                failure = {"source_index": index, "source_key": scope["source_key"],
                           "stage": "collection", "error_type": type(error).__name__, "error": safe_collection_error(error, config)}
                batch["errors"].append(failure)
                scope.update(status="failed", error=failure, has_more=None)
                batch["source_scope"].append(scope)
                save()
                continue
            if collected.get("id"):
                batch["collection_ids"].append(collected["id"])
            attempts = collected.get("download_attempts", len(collected.get("document_ids", [])) + len(collected.get("errors", [])))
            batch["download_attempts"] += attempts
            for failure in collected.get("errors", []):
                batch["errors"].append({**failure, "source_index": index, "collection_id": collected.get("id"), "stage": "download"})
            scope.update(status=collected.get("status", "collected"), collection_id=collected.get("id"),
                         discovery_id=collected.get("discovery_id"), scope=collected.get("source_scope", {}),
                         has_more=collected.get("has_more"), directory_has_more=collected.get("directory_has_more"),
                         pending_listing_count=collected.get("pending_listing_count"),
                         download_attempts=attempts)
            batch["source_scope"].append(scope)
            refresh_selection()
            save()
        batch["collection_ids"] = list(dict.fromkeys(batch["collection_ids"]))
        batch["has_more"] = bool(batch["queued_ready_remaining"] or batch["blocked"] or
                                 any(scope.get("has_more") is not False for scope in batch["source_scope"] if scope["status"] != "disabled") or
                                 any(scope.get("has_more") is not False for scope in batch.get("collection_scope", [])))
        batch["status"] = ("ready" if batch["document_ids"] else "needs_material_review" if batch["blocked"]
                           else "collection_failed" if batch["errors"] else "no_documents")
        batch["finished_at"] = now()
        save()
        return batch
    except BaseException as error:
        batch.update(status="interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed",
                     failure_type=type(error).__name__, finished_at=now())
        save()
        raise
