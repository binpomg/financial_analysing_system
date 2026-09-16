"""从确证的进程状态恢复中断标记，不自动重发模型请求。"""
import os
from datetime import datetime

from .storage import now


ACTIVE = {"queued", "running", "awaiting_audit", "auditing", "registering_feedback", "collecting"}


def owner_state(pid, created_at):
    """返回 alive/dead/unknown；无法确认时不把仍在执行的任务标为中断。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown"
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        handle = api.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            return "dead" if ctypes.get_last_error() == 87 else "unknown"
        try:
            state = api.WaitForSingleObject(handle, 0)
            if state == 0:
                return "dead"
            if state != 258:
                return "unknown"
            times = [wintypes.FILETIME() for _ in range(4)]
            if created_at and api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                birth = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10000000 - 11644473600
                try:
                    recorded = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    if recorded.tzinfo and birth > recorded.timestamp() + 1:
                        return "dead"  # 同一PID已被晚于任务创建的新进程复用。
                except (ValueError, TypeError):
                    return "unknown"
            return "alive"
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    return "alive"


def recover_interrupted(store):
    """调用方持有资料库写锁；原成果和trace保留，恢复不产生API费用。"""
    from .runner import export_run
    recovered = []
    for category in ("runs", "jobs", "experiments", "pipelines", "feedback_verifications", "ingestion_batches", "collections", "campaigns"):
        for record in store.list(category):
            if record.get("status") not in ACTIVE:
                continue
            if owner_state(record.get("worker_pid"), record.get("worker_started_at", record.get("created_at"))) != "dead":
                continue
            record["interrupted_stage"] = record["status"]
            record.update(status="interrupted", finished_at=now(),
                          error="执行进程已结束；保留原件、已完成阶段和失败现场。未自动重新调用模型。")
            if category == "runs":
                record["release_status"] = "blocked"
            store.put(category, record["id"], record, replace=True)
            if category == "runs":
                export_run(store, record)
            recovered.append(record["id"])
    return recovered
