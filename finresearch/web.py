"""仅在本机提供研究管理界面；后台单任务队列与命令行共享存储。"""
import base64
import json
import mimetypes
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .cli import doctor
from .runner import confirm_issue, run_case, effective_release_status
from .storage import PROJECT, configuration, now, safe_id


CAMPAIGN_ACTIVE = {"queued", "running", "stopping"}


def active_campaigns(store):
    return [record for record in store.list("campaigns") if record.get("status") in CAMPAIGN_ACTIVE]


def campaign_summary(record, store):
    """只读汇总，不将工作进程配置或完整模型上下文送入列表。"""
    fields = ("id", "status", "created_at", "updated_at", "finished_at", "target_documents",
              "current_pipeline_id", "error")
    result = {key: record.get(key) for key in fields}
    progress = record.get("progress", {})
    result["progress"] = {key: progress.get(key, 0) for key in
                          ("processed_documents", "attempted_documents", "audited_runs", "failed_runs", "feedback_count", "candidate_count")}
    identifiers = {item.get("pipeline_id") for item in record.get("pipelines", [])}
    identifiers.add(record.get("current_pipeline_id"))
    documents = set(record.get("document_ids", []))
    batches = []
    for identifier in identifiers - {None, ""}:
        try:
            batch = store.get("pipelines", identifier)
        except FileNotFoundError:
            continue
        batches.append(batch)
        documents.update(batch.get("document_ids", []))
    result["progress"]["attempted_documents"] = max(result["progress"]["attempted_documents"], len(documents))
    if batches:
        # 一份材料七线均尝试完才计入 processed；审核完成也不等于审核通过。
        steps = [step for batch in batches for step in batch.get("steps", [])]
        result["progress"]["audited_runs"] = sum(step.get("run_status") == "audited" for step in steps)
        result["progress"]["failed_runs"] = sum(step.get("run_status") in
                                               {"failed", "interrupted", "reading_incomplete", "blocked"} for step in steps)
    result["stop_requested"] = bool(record.get("stop_requested") or store.path("campaign_stop_requests", record["id"]).exists())
    return result


def pipeline_summary(batch):
    """列表只发送进度与结果引用；完整记录在独立详情入口读取。"""
    fields = ("id", "status", "created_at", "updated_at", "finished_at", "document_ids", "case_ids", "error")
    step_fields = ("line", "status", "run_id", "run_status", "release_status", "feedback_status")
    return {**{key: batch.get(key) for key in fields},
            "steps": [{key: step.get(key) for key in step_fields} for step in batch.get("steps", [])],
            "candidate_attempts": batch.get("candidate_attempts", []),
            "candidate_waiting": batch.get("candidate_waiting", [])}


def run_pipeline_job(store, pipeline_id=None):
    from .pipeline import run_pipeline
    if active_campaigns(store):
        raise ValueError("连续训练任务正在执行，请等待训练结束或当前材料结束后暂停，再启动单份批次")
    return pipeline_summary(run_pipeline(store, pipeline_id=pipeline_id))


def run_feedback_verifications(store, run_id):
    """仅返回本次运行的核实展示字段，不返回配置、请求原文或认证信息。"""
    feedback = {item["id"]: item for item in store.list("feedback") if item.get("run_id") == run_id}
    fields = ("id", "run_id", "line", "status", "created_at", "finished_at", "error", "expert_calibrated",
              "total_issues", "issue_indices", "remaining_issue_indices", "previously_claimed_issue_indices")
    decision_fields = ("issue_index", "verdict", "reason", "model_verdict", "pending_reason", "result_pointers")
    result = []
    for record in store.list("feedback_verifications"):
        if record.get("run_id") != run_id:
            continue
        view = {key: record[key] for key in fields if key in record}
        view["decisions"] = []
        for decision in record.get("decisions", []):
            item = {key: decision[key] for key in decision_fields if key in decision}
            item["evidence"] = [{key: evidence.get(key) for key in ("document_id", "unit_id", "quote")}
                                for evidence in decision.get("evidence", [])]
            # 中断登记中即使留下部分反馈文件，也不能显示为本次核实完成并登记成功。
            item["registered_feedback_ids"] = [identifier for identifier in record.get("feedback_ids", [])
                                               if record.get("status") == "completed" and decision.get("verdict") == "confirmed"
                                               and identifier in feedback
                                               and feedback[identifier].get("issue_index") == decision.get("issue_index")]
            item["feedback_registered"] = bool(item["registered_feedback_ids"])
            view["decisions"].append(item)
        result.append(view)
    return result


class ResearchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        super().__init__(address, Handler)
        self.store = store
        self.queue = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research")
        self.jobs = {job["id"]: job for job in store.list("jobs")}
        self.jobs_lock = threading.Lock()
        self.campaign_controls_lock = threading.Lock()

    def update_job(self, identifier, **changes):
        with self.jobs_lock:
            self.jobs[identifier].update(changes)
            self.store.put("jobs", identifier, self.jobs[identifier], replace=True)

    def job_list(self):
        with self.jobs_lock:
            return [dict(job) for job in self.jobs.values()]

    def submit(self, operation):
        identifier = "job-" + uuid.uuid4().hex[:12]
        with self.jobs_lock:
            self.jobs[identifier] = {"id": identifier, "status": "queued", "created_at": now(), "worker_pid": os.getpid()}
            self.store.put("jobs", identifier, self.jobs[identifier])
        def work():
            self.update_job(identifier, status="running")
            try:
                with self.store.exclusive():
                    result = operation()
                self.update_job(identifier, status="completed", result=result, finished_at=now())
            except (KeyboardInterrupt, SystemExit) as error:
                self.update_job(identifier, status="interrupted", error="任务中断：" + type(error).__name__, finished_at=now())
                raise
            except Exception as error:
                self.update_job(identifier, status="failed", error=str(error), finished_at=now())
        self.queue.submit(work)
        return {"job_id": identifier}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, value, status=200, content_type="application/json; charset=utf-8"):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8") if content_type.startswith("application/json") else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def local_host(self):
        host = self.headers.get("Host", "")
        return host in {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}

    def do_GET(self):
        if not self.local_host():
            self.send({"error": "只允许本机访问"}, 403)
            return
        target = urlsplit(self.path)
        store = self.server.store
        try:
            if target.path == "/api/state":
                feedback = store.list("feedback")
                candidates = store.list("candidates")
                verified_ids = {identifier for record in store.list("feedback_verifications")
                                if record.get("status") == "completed" for identifier in record.get("feedback_ids", [])}
                result = {"environment": doctor(store), "lines": store.lines(), "jobs": self.server.job_list(),
                          "documents": [{"id": d["id"], "name": d["name"], "group_id": d["group_id"], "published_at": d["published_at"], "preparation": d["prepared"]["status"], "units": len(d["prepared"]["units"]), "warnings": d["prepared"]["warnings"]} for d in store.list("documents")],
                          "cases": store.list("cases"), "runs": [{**{k: r.get(k) for k in ("id", "case_id", "line", "status", "created_at", "finished_at", "error", "skill_version")}, "release_status": effective_release_status(r)} for r in store.list("runs")],
                          "pipelines": [pipeline_summary(batch) for batch in store.list("pipelines")],
                          "campaigns": [campaign_summary(record, store) for record in store.list("campaigns")],
                          "training_summary": {"feedback_count": len(feedback),
                                               "model_confirmed_feedback_count": sum(item.get("verification_kind") == "independent_model_check" and item["id"] in verified_ids for item in feedback),
                                               "candidate_count": len(candidates),
                                               "unvalidated_candidate_count": sum(item.get("status") == "candidate_unvalidated" for item in candidates),
                                               "adoption_count": len(store.list("adoptions"))}}
                self.send(result)
            elif target.path == "/api/campaigns":
                self.send([campaign_summary(record, store) for record in store.list("campaigns")])
            elif target.path == "/api/campaign":
                self.send(store.get("campaigns", parse_qs(target.query)["id"][0]))
            elif target.path == "/api/pipeline":
                self.send(store.get("pipelines", parse_qs(target.query)["id"][0]))
            elif target.path == "/api/run":
                run = store.get("runs", parse_qs(target.query)["id"][0])
                self.send({**run, "effective_release_status": effective_release_status(run),
                           "feedback_verifications": run_feedback_verifications(store, run["id"])})
            elif target.path == "/api/original":
                doc = store.get("documents", parse_qs(target.query)["id"][0])
                path = Path(doc["raw_path"])
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                # HTML原件按文本展示，防止原件中的脚本在管理界面来源执行。
                if mime in {"text/html", "application/xhtml+xml", "image/svg+xml"}:
                    mime = "text/plain; charset=utf-8"
                self.send(path.read_bytes(), content_type=mime)
            elif target.path == "/api/export":
                query = parse_qs(target.query)
                run_id = safe_id(query["id"][0])
                name = query.get("file", ["report.md"])[0]
                if name not in {"report.md", "records.csv", "result.json"}:
                    raise ValueError("未知导出格式")
                path = store.root / "outputs" / run_id / name
                self.send(path.read_bytes(), content_type="text/plain; charset=utf-8")
            elif target.path in {"/", "/app.js", "/style.css"}:
                name = "index.html" if target.path == "/" else target.path[1:]
                mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name]
                self.send((PROJECT / "web" / name).read_bytes(), content_type=mime + "; charset=utf-8")
            else:
                self.send({"error": "未找到"}, 404)
        except (ValueError, KeyError, OSError) as error:
            self.send({"error": str(error)}, 400)

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        expected = {"http://127.0.0.1:" + str(self.server.server_port), "http://localhost:" + str(self.server.server_port)}
        if not self.local_host() or origin not in expected or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.send({"error": "请求来源或格式不允许"}, 403)
            return
        store = self.server.store
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 24 * 1024 * 1024:
                raise ValueError("单次界面请求限24MB；大原件请使用命令行导入")
            body = json.loads(self.rfile.read(size).decode("utf-8"))
            if self.path == "/api/import":
                name = body["name"]
                if Path(name).name != name or not name or "/" in name or "\\" in name:
                    raise ValueError("非法文件名")
                raw = base64.b64decode(body["base64"], validate=True)
                def do_import():
                    path = store.root / "uploads" / uuid.uuid4().hex / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
                    return store.import_file(path, body.get("source_url", ""), body.get("group_id") or None, body["published_at"])["id"]
                self.send(self.server.submit(do_import), 202)
            elif self.path == "/api/case":
                self.send(self.server.submit(lambda: store.create_case(body["document_ids"], body["task"], body["cutoff"], body.get("partition", "development"), body.get("expected_attachments", []))["id"]), 202)
            elif self.path == "/api/run":
                lines = body.get("lines") or ["extraction"]
                for line in lines:
                    store.line(line)
                def execute():
                    results = [run_case(store, body["case_id"], line) for line in dict.fromkeys(lines)]
                    return [{"id": run["id"], "status": run["status"], "release_status": effective_release_status(run)} for run in results]
                self.send(self.server.submit(execute), 202)
            elif self.path == "/api/confirm":
                self.send(self.server.submit(lambda: confirm_issue(store, body["run_id"], int(body["issue"]), body["note"])), 202)
            elif self.path == "/api/pipeline":
                if not isinstance(body, dict) or set(body) - {"pipeline_id"}:
                    raise ValueError("自动批次仅接受空对象或 pipeline_id；每批最多1份原件，七线顺序固定")
                if active_campaigns(store):
                    raise ValueError("连续训练任务正在执行，请等待训练结束或当前材料结束后暂停，再启动单份批次")
                pipeline_id = None
                if "pipeline_id" in body:
                    pipeline_id = safe_id(body["pipeline_id"])
                    if store.get("pipelines", pipeline_id).get("status") != "interrupted":
                        raise ValueError("仅已中断批次可显式续批；失败请求不会自动重试")
                self.send(self.server.submit(lambda: run_pipeline_job(store, pipeline_id)), 202)
            elif self.path == "/api/campaign/stop":
                if not isinstance(body, dict) or set(body) != {"campaign_id"}:
                    raise ValueError("暂停请求仅接受 campaign_id")
                identifier = safe_id(body["campaign_id"])
                with self.server.campaign_controls_lock:
                    record = store.get("campaigns", identifier)
                    if record.get("status") not in CAMPAIGN_ACTIVE:
                        raise ValueError("仅进行中的连续训练任务可请求暂停")
                    path = store.path("campaign_stop_requests", identifier)
                    if path.exists():
                        request = store.get("campaign_stop_requests", identifier)
                    else:
                        # 控制文件独立于主记录及写锁，避免覆盖后台进度，也不中断当前付费请求。
                        request = store.put("campaign_stop_requests", identifier,
                                            {"id": identifier, "campaign_id": identifier,
                                             "requested_at": now(), "stop_requested": True})
                self.send(request, 202)
            else:
                self.send({"error": "未找到"}, 404)
        except (ValueError, KeyError, OSError) as error:
            self.send({"error": str(error)}, 400)


def serve(store, port=8765):
    from .lifecycle import recover_interrupted
    try:
        with store.exclusive():
            recovered = recover_interrupted(store)
        if recovered:
            print("已识别并保留中断任务：" + str(len(recovered)), flush=True)
    except RuntimeError:
        print("资料库有任务执行中，本次跳过中断扫描。", flush=True)
    server = ResearchServer(("127.0.0.1", port), store)
    print("金融研究工作台：http://127.0.0.1:" + str(server.server_port), flush=True)
    print("数据目录：" + str(store.root), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        server.queue.shutdown(wait=True)
