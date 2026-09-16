"""有限训练的独立后台进程；不依赖聊天/工作台生命周期，不建系统定时任务。"""
import argparse
import os
import sys
from pathlib import Path

from .storage import Store, now, safe_id, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign_id")
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    safe_id(args.campaign_id)
    store = Store(args.data_dir)
    folder = store.root / "campaign_logs" / args.campaign_id
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now().replace(":", "-").replace("+", "_")
    state_path = folder / (stamp + ".worker.json")
    worker = {"campaign_id": args.campaign_id, "worker_pid": os.getpid(), "started_at": now(), "status": "starting"}
    write_json(state_path, worker)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    with (folder / (stamp + ".log")).open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = sys.stderr = log
        try:
            from .campaign import run_campaign
            with store.exclusive():
                result = run_campaign(store, args.campaign_id)
            print(result["id"], result["status"], result["progress"], flush=True)
            worker.update(status="finished", campaign_status=result["status"],
                          exit_code=0 if result["status"] in {"completed", "completed_with_issues"} else 2)
            return worker["exit_code"]
        except BaseException as error:
            import traceback
            traceback.print_exc()
            worker.update(status="failed", error_type=type(error).__name__, exit_code=2)
            return 2
        finally:
            worker["finished_at"] = now()
            write_json(state_path, worker)
            sys.stdout, sys.stderr = original_stdout, original_stderr


if __name__ == "__main__":
    raise SystemExit(main())
