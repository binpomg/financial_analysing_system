"""后台服务入口：独立于启动终端，日志留在项目运行目录。"""
import argparse
import sys
from pathlib import Path

from .storage import PROJECT, Store, now, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    folder = PROJECT / "runtime" / "server"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now().replace(":", "-").replace("+", "_")
    with (folder / (stamp + ".stdout.log")).open("a", encoding="utf-8", buffering=1) as output, \
            (folder / (stamp + ".stderr.log")).open("a", encoding="utf-8", buffering=1) as errors:
        original_stdout, original_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = output, errors
        try:
            import os
            write_json(folder / "service.json", {"worker_pid": os.getpid(), "port": args.port,
                                                 "project": str(PROJECT), "started_at": now()})
            from .web import serve
            serve(Store(), args.port)
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr


if __name__ == "__main__":
    main()
