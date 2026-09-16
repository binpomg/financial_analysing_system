"""文件存储、不可变输入和跨业务线的数据用途约束。"""
import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", value):
        raise ValueError("非法标识：只能使用字母、数字、下划线、点和连字符")
    return value


def configuration():
    config = read_json(PROJECT / "config.json")
    local = PROJECT / "config.local.json"
    if local.exists():
        def merge(base, extra):
            for key, value in extra.items():
                if isinstance(value, dict) and isinstance(base.get(key), dict):
                    merge(base[key], value)
                else:
                    base[key] = value
        merge(config, read_json(local))
    return config


class Store:
    def __init__(self, root=None):
        self.root = Path(root or PROJECT / "runtime").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def exclusive(self):
        """同一资料库只允许一个写入者；不同角色 API 上下文仍然独立。"""
        path = self.root / ".writer.lock"
        with path.open("a+b") as handle:
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("资料库正在执行其他写入任务，请等待该任务完成") from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def path(self, category, identifier):
        return self.root / safe_id(category) / (safe_id(identifier) + ".json")

    def get(self, category, identifier):
        return read_json(self.path(category, identifier))

    def put(self, category, identifier, value, replace=False):
        path = self.path(category, identifier)
        if path.exists() and not replace:
            raise ValueError("记录已存在，禁止覆盖：" + identifier)
        write_json(path, value)
        return value

    def list(self, category):
        items = [read_json(path) for path in (self.root / safe_id(category)).glob("*.json")]
        return sorted(items, key=lambda item: (item.get("created_at", item.get("imported_at", item.get("confirmed_at", ""))), item.get("id", "")))

    def import_file(self, source, source_url="", group_id=None, published_at=None):
        from .documents import prepare_document
        source = Path(source).resolve()
        if not source.is_file():
            raise ValueError("输入文件不存在")
        checksum = hashlib.sha256(source.read_bytes()).hexdigest()
        identifier = checksum[:24]
        if self.path("documents", identifier).exists():
            existing = self.get("documents", identifier)
            if group_id and existing["group_id"] != group_id:
                raise ValueError("同一原件已有分组，不能用新分组绕过数据隔离")
            return existing
        group = safe_id(group_id or identifier)
        if published_at:
            date = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError("公告发布时间必须包含时区，例如 +08:00")
        original = self.root / "raw" / identifier / source.name
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(original))
        if hashlib.sha256(original.read_bytes()).hexdigest() != checksum:
            raise IOError("原件复制校验失败")
        prepared = prepare_document(original, self.root / "prepared" / identifier, identifier)
        value = {"id": identifier, "document_id": identifier, "name": source.name, "sha256": checksum,
                 "group_id": group, "source_url": source_url, "published_at": published_at,
                 "imported_at": now(), "raw_path": str(original), "prepared": prepared}
        return self.put("documents", identifier, value)

    def create_case(self, document_ids, task, cutoff, partition="development", expected_attachments=None, *, case_id=None, origin=None):
        from .evaluation import validate_partitions
        if case_id is not None:
            safe_id(case_id)
        if origin is not None:
            if (case_id is None or not isinstance(origin, dict)
                    or set(origin) != {"kind", "pipeline_id", "reservation_digest"}
                    or origin["kind"] != "pipeline"
                    or not isinstance(origin["reservation_digest"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", origin["reservation_digest"])):
                raise ValueError("案例来源必须包含显式案例ID、pipeline归属与完整预登记指纹")
            safe_id(origin["pipeline_id"])
        if partition not in {"development", "validation", "regression", "calibration", "holdout"}:
            raise ValueError("未知数据用途")
        if not task.strip() or not document_ids:
            raise ValueError("案例必须包含完整任务和至少一份原件")
        parsed = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("信息截止时间必须包含时区")
        documents = [self.get("documents", identifier) for identifier in dict.fromkeys(document_ids)]
        for doc in documents:
            if not doc.get("published_at"):
                raise ValueError("请先登记每份原件的发布时间，才能建立有时间边界的研究案例")
            if datetime.fromisoformat(doc["published_at"].replace("Z", "+00:00")) > parsed:
                raise ValueError("原件发布晚于案例信息截止时间：" + doc["name"])
        case = {"id": case_id if case_id is not None else "case-" + uuid.uuid4().hex[:16], "created_at": now(), "task": task,
                "cutoff": cutoff, "partition": partition, "document_ids": [d["id"] for d in documents],
                "group_ids": sorted({d["group_id"] for d in documents}),
                "documents": [{"id": d["id"], "group_id": d["group_id"], "published_at": d["published_at"]} for d in documents],
                "expected_attachments": expected_attachments or []}
        if origin is not None:
            case["origin"] = dict(origin)
        case["input_digest"] = digest({k: case[k] for k in ("task", "cutoff", "document_ids", "expected_attachments")})
        conflicts = validate_partitions(self.list("cases") + [case])
        if conflicts:
            raise ValueError("数据用途或时点冲突：" + "; ".join(str(x) for x in conflicts))
        return self.put("cases", case["id"], case)

    def lines(self):
        return read_json(PROJECT / "resources" / "lines.json")

    def line(self, identifier):
        safe_id(identifier)
        for item in self.lines():
            if item["id"] == identifier:
                return item
        raise ValueError("未知业务线：" + identifier)

    def snapshot(self, line_id, candidate_id=None):
        line = self.line(line_id)
        if candidate_id:
            candidate = self.get("candidates", candidate_id)
            if candidate["line"] != line_id:
                raise ValueError("候选版本与业务线不匹配")
            return self.get("versions", candidate["skill_version"])
        stable_path = self.path("stable", line_id)
        if stable_path.exists():
            return self.get("versions", read_json(stable_path)["skill_version"])
        skill_path = PROJECT / "resources" / line["skill_path"]
        files = {"SKILL.md": skill_path.read_text(encoding="utf-8")}
        for path in sorted((skill_path.parent / "references").glob("*.md")):
            files["references/" + path.name] = path.read_text(encoding="utf-8")
        version = {"id": digest({"line": line_id, "files": files})[:24], "line": line_id, "files": files, "created_at": now()}
        if not self.path("versions", version["id"]).exists():
            self.put("versions", version["id"], version)
        self.put("stable", line_id, {"skill_version": version["id"], "changed_at": now()}, replace=True)
        return version
