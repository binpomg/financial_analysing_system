"""命令入口。命令行和本地界面共用相同业务函数。"""
import argparse
import json
import sys
import time
from pathlib import Path

from .collection import collect_once, discover, fetch_document
from .experiments import promote, rollback, run_experiment
from .provider import APIProvider
from .runner import confirm_issue, propose_candidate, run_case, resume_audit, effective_release_status
from .storage import PROJECT, Store, configuration, now


def doctor(store, live=False):
    import importlib.util
    config = configuration()
    provider = APIProvider(config)
    from urllib.parse import urlsplit
    base, key, protocol = provider.credentials()
    result = {"python": sys.version.split()[0], "project": str(PROJECT), "data_directory": str(store.root),
              "dependencies": {name: importlib.util.find_spec(name) is not None for name in ["jsonschema", "pypdf", "fitz", "openpyxl", "PIL"]},
              "credential_available": bool(base and key), "api_host": urlsplit(base).hostname, "protocol": protocol,
              "models": config["models"], "business_quality": "尚须依据各线真实校准与保留样本评价"}
    if live:
        from .schemas import obj, TEXT
        result["live"] = {}
        for role in ["discovery", "business"]:
            try:
                result["live"][role] = provider.call(role, "你是接口检查助手。", '只输出 {"status":"ok"}。', obj({"status": TEXT}), trace_dir=store.root / "verification" / ("doctor-" + role))
            except Exception as error:
                result["live"][role] = {"error": str(error)}
    return result


def parser():
    p = argparse.ArgumentParser(description="金融研究系统：完整材料 → 独立业务 → 独立审核 → 有证据的版本改进")
    p.add_argument("--data-dir", help="资料库目录，默认项目 runtime")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("doctor", help="检查运行环境与模型配置")
    q.add_argument("--live", action="store_true", help="实际调用两种模型验证连接")
    sub.add_parser("status", help="列出业务线、资料与案例状态")
    sub.add_parser("recover", help="将确证执行进程已结束的任务标为中断，保留成果，不重发模型请求")
    q = sub.add_parser("import", help="导入原件并准备所有页面")
    q.add_argument("files", nargs="+")
    q.add_argument("--published-at", required=True)
    q.add_argument("--source-url", default="")
    q.add_argument("--group")
    q = sub.add_parser("fetch", help="下载公开原件并导入")
    q.add_argument("url")
    q.add_argument("--published-at", required=True)
    q.add_argument("--group")
    q.add_argument("--allowed-host", action="append")
    q = sub.add_parser("discover", help="用 Luna 整理公开目录，保留所有发现链接")
    q.add_argument("url")
    q.add_argument("--allowed-host", action="append")
    q.add_argument("--limit", type=int, default=100)
    q = sub.add_parser("collect", help="执行配置的有限采集轮次；不会自动启动业务研究")
    q.add_argument("--rounds", type=int, default=1)
    q.add_argument("--interval", type=int, default=3600)
    q.add_argument("--limit", type=int, default=5)
    q = sub.add_parser("pipeline", help="按需自动小批次：最多1份新材料 → 七线全文处理 → 核实反馈 → 候选积累")
    q.add_argument("--resume", metavar="PIPELINE_ID", help="显式继续中断批次中尚未发起的步骤，不重复失败请求")
    q = sub.add_parser("campaign-create", help="建立有限连续训练任务；不调用模型")
    q.add_argument("--documents", type=int, default=100)
    q = sub.add_parser("campaign-run", help="执行/显式恢复已登记连续训练；每份原件完整七线，不重试失败阶段")
    q.add_argument("campaign_id")
    q = sub.add_parser("campaign-status", help="只读查看连续训练记录")
    q.add_argument("campaign_id")
    q = sub.add_parser("verify-feedback", help="全文独立核实尚未处理的审核问题，不自动重试已失败的核实")
    q.add_argument("run_id")
    q.add_argument("--limit", type=int, default=2)
    q = sub.add_parser("date", help="为尚未建立案例的待定日期原件登记发布时间")
    q.add_argument("document_id")
    q.add_argument("--published-at", required=True)
    q = sub.add_parser("case", help="定义完整材料包、任务和信息时点")
    q.add_argument("--document", action="append", required=True)
    q.add_argument("--task", required=True)
    q.add_argument("--cutoff", required=True)
    q.add_argument("--partition", choices=["development", "validation", "regression", "calibration", "holdout"], default="development")
    q.add_argument("--attachment", action="append", default=[])
    q = sub.add_parser("run", help="完整运行一条线或全部已启用线，每线均独立读完整材料")
    q.add_argument("case_id")
    group = q.add_mutually_exclusive_group()
    group.add_argument("--line", default="extraction")
    group.add_argument("--all-lines", action="store_true")
    q.add_argument("--candidate")
    q = sub.add_parser("resume-audit", help="保留原运行，使用已完成业务成果继续独立审核")
    q.add_argument("run_id")
    q = sub.add_parser("confirm", help="记录审核问题已回查原件的核实依据")
    q.add_argument("run_id")
    q.add_argument("--issue", type=int, required=True)
    q.add_argument("--note", required=True)
    q = sub.add_parser("propose", help="根据本线已核实反馈提出候选 Skill")
    q.add_argument("line")
    q.add_argument("--feedback", action="append", required=True)
    q = sub.add_parser("experiment", help="下一批新旧对照及历史回归")
    q.add_argument("candidate_id")
    q.add_argument("--case", action="append", required=True)
    q.add_argument("--regression-case", action="append", required=True)
    q = sub.add_parser("promote", help="有对照证据后，记录明确人工采用决定")
    q.add_argument("experiment_id")
    q.add_argument("--reason", required=True)
    q = sub.add_parser("rollback", help="根据采用记录恢复上一个稳定版本")
    q.add_argument("line")
    q.add_argument("adoption_id")
    q.add_argument("--reason", required=True)
    q = sub.add_parser("serve", help="启动仅本机访问的管理界面")
    q.add_argument("--port", type=int, default=8765)
    return p


def dispatch(store, args):
    command = args.command
    if command == "recover":
        from .lifecycle import recover_interrupted
        return {"interrupted": recover_interrupted(store)}
    if command == "doctor":
        return doctor(store, args.live)
    if command == "pipeline":
        from .pipeline import run_pipeline
        return run_pipeline(store, args.resume)
    if command == "campaign-create":
        from .campaign import create_campaign
        return create_campaign(store, args.documents)
    if command == "campaign-run":
        from .campaign import run_campaign
        return run_campaign(store, args.campaign_id)
    if command == "campaign-status":
        return store.get("campaigns", args.campaign_id)
    if command == "verify-feedback":
        from .feedback_review import review_run_feedback
        return review_run_feedback(store, args.run_id, args.limit)
    if command == "status":
        return {"lines": store.lines(), "documents": [{k: d[k] for k in ("id", "name", "published_at", "group_id")} for d in store.list("documents")],
                "cases": store.list("cases"), "runs": [{**{k: r.get(k) for k in ("id", "case_id", "line", "status", "error")}, "release_status": effective_release_status(r)} for r in store.list("runs")]}
    if command == "import":
        return [store.import_file(path, args.source_url, args.group, args.published_at) for path in args.files]
    if command == "fetch":
        return fetch_document(store, args.url, args.published_at, args.group, args.allowed_host)
    if command == "discover":
        return discover(store, args.url, args.allowed_host, args.limit)
    if command == "collect":
        if not 1 <= args.rounds <= 1000 or args.interval < 1 or not 1 <= args.limit <= 100:
            raise ValueError("采集轮次/间隔/批次限制不合法")
        sources = configuration().get("sources", [])
        if not sources:
            raise ValueError("请先在 config.local.json sources 中登记公开目录 URL 和 allowed_hosts")
        results = []
        for index in range(args.rounds):
            with store.exclusive():
                results.extend(collect_once(store, source, args.limit) for source in sources)
            if index + 1 < args.rounds:
                print(json.dumps({"event": "collection_round_done", "round": index + 1, "next_in_seconds": args.interval}, ensure_ascii=False), flush=True)
                time.sleep(args.interval)
        return results
    if command == "date":
        from datetime import datetime
        timestamp = datetime.fromisoformat(args.published_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("发布时间必须有时区")
        doc = store.get("documents", args.document_id)
        if any(args.document_id in case["document_ids"] for case in store.list("cases")):
            raise ValueError("原件已有案例引用，不能修改其时点元数据")
        doc["published_at"] = args.published_at
        return store.put("documents", args.document_id, doc, replace=True)
    if command == "case":
        return store.create_case(args.document, args.task, args.cutoff, args.partition, args.attachment)
    if command == "run":
        if args.all_lines and args.candidate:
            raise ValueError("候选版本只属于单一业务线")
        selected = configuration()["default_enabled_lines"] if args.all_lines else [args.line]
        return [run_case(store, args.case_id, line, args.candidate) for line in selected]
    if command == "confirm":
        return confirm_issue(store, args.run_id, args.issue, args.note)
    if command == "resume-audit":
        return resume_audit(store, args.run_id)
    if command == "propose":
        return propose_candidate(store, args.line, args.feedback)
    if command == "experiment":
        return run_experiment(store, args.candidate_id, args.case, args.regression_case)
    if command == "promote":
        return promote(store, args.experiment_id, args.reason)
    if command == "rollback":
        return rollback(store, args.line, args.adoption_id, args.reason)
    raise ValueError("未知命令")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    store = Store(args.data_dir)
    try:
        if args.command == "serve":
            from .web import serve
            serve(store, args.port)
            return
        if args.command in {"status", "doctor", "collect", "campaign-status"}:
            result = dispatch(store, args)
        else:
            with store.exclusive():
                result = dispatch(store, args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "run" and any(r["status"] in {"failed", "reading_incomplete", "interrupted"} for r in result):
            raise SystemExit(2)
        if args.command == "resume-audit" and result["status"] in {"failed", "reading_incomplete", "interrupted"}:
            raise SystemExit(2)
        if args.command == "pipeline" and result["status"] in {"failed", "blocked", "interrupted"}:
            raise SystemExit(2)
        if args.command == "verify-feedback" and result["status"] in {"failed", "interrupted"}:
            raise SystemExit(2)
        if args.command == "campaign-run" and result["status"] not in {"completed", "completed_with_issues"}:
            raise SystemExit(2)
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("任务已中断；已保存的原始结果仍可复核。", file=sys.stderr)
        raise SystemExit(130)
