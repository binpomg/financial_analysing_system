import copy
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from finresearch.calculator import calculate
from finresearch.provider import APIProvider, ProviderError, read_event_stream
from finresearch.runner import run_case, required_dimensions, validate_dimensions
from finresearch.schemas import obj, TEXT, validate_evidence
from finresearch.storage import PROJECT, Store, configuration, digest, read_json
from finresearch.web import ResearchServer


class FakeProvider:
    """只用于程序边界测试，不进入业务质量评测或生产默认路径。"""
    def __init__(self, change=None):
        self.calls = []
        self.change = change

    def call(self, role, instructions, prompt, schema, images=None, trace_dir=None):
        self.calls.append({"role": role, "instructions": instructions, "prompt": prompt, "images": images})
        material = json.loads(prompt.split("\n待审核成果")[0])
        units = material["source_units"]
        evidence = {"document_id": units[0]["document_id"], "unit_id": units[0]["unit_id"], "quote": "营业收入100万元"}
        coverage = [{"document_id": u["document_id"], "unit_id": u["unit_id"], "status": "read", "note": "已读原文"} for u in units]
        if role == "business":
            value = {"coverage": coverage, "result": {"status": "completed", "summary": "演练提取", "records": [{"record_type": "observed", "entity": "演练公司", "period": "2025", "field": "营业收入", "raw_value": "100", "value": "100", "unit": "万元", "currency": "CNY", "evidence": [evidence]}], "analysis": [], "missing_data": [], "warnings": []}}
        else:
            criteria = (PROJECT / "resources/reviews/extraction/criteria.md").read_text(encoding="utf-8")
            value = {"coverage": coverage, "audit": {"verdict": "pass", "issues": [], "dimensions": [{"name": name, "judgment": "pass", "reason": "已逐项回查本演练原文"} for name in required_dimensions(criteria)], "summary": "测试夹具通过"}}
        if self.change:
            self.change(role, value)
        return value


class SystemTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.source = self.root / "example.md"
        self.source.write_text("演练公司2025年营业收入100万元。注：仅为程序测试。", encoding="utf-8")
        self.doc = self.store.import_file(self.source, "https://example.org/test", "example", "2026-01-01T00:00:00+08:00")
        self.case = self.store.create_case([self.doc["id"]], "提取所有披露数值", "2026-01-02T00:00:00+08:00")

    def tearDown(self):
        self.temp.cleanup()

    def test_independent_roles_and_exports(self):
        provider = FakeProvider()
        run = run_case(self.store, self.case["id"], provider=provider)
        self.assertEqual(run["release_status"], "audit_passed")
        self.assertEqual([c["role"] for c in provider.calls], ["business", "audit"])
        self.assertNotIn(run["skill_version"], provider.calls[1]["prompt"])
        self.assertNotIn("稳定版", provider.calls[1]["prompt"])
        self.assertIn("record_type", provider.calls[0]["instructions"])
        for name in ["result.json", "report.md", "records.csv"]:
            self.assertTrue((self.store.root / "outputs" / run["id"] / name).is_file())

    def test_missing_coverage_cannot_pass(self):
        def change(role, result):
            if role == "business": result["coverage"] = []
        provider = FakeProvider(change)
        run = run_case(self.store, self.case["id"], provider=provider)
        self.assertEqual(run["status"], "reading_incomplete")
        self.assertEqual(len(provider.calls), 1)

    def test_uncited_observed_value_is_blocked(self):
        def change(role, result):
            if role == "business": result["result"]["records"][0]["evidence"] = []
        run = run_case(self.store, self.case["id"], provider=FakeProvider(change))
        self.assertEqual(run["status"], "failed")
        self.assertIn("缺少", run["error"])

    def test_invented_dimension_is_blocked(self):
        def change(role, result):
            if role == "audit": result["audit"]["dimensions"] = [{"name": "随便通过", "judgment": "pass", "reason": ""}]
        run = run_case(self.store, self.case["id"], provider=FakeProvider(change))
        self.assertEqual(run["release_status"], "blocked")

    def test_issue_and_pass_contradiction(self):
        def change(role, result):
            if role == "audit": result["audit"]["dimensions"][0]["judgment"] = "fail"
        run = run_case(self.store, self.case["id"], provider=FakeProvider(change))
        self.assertEqual(run["status"], "failed")

    def test_changed_original_is_rejected(self):
        Path(self.doc["raw_path"]).write_text("收入变成200万元", encoding="utf-8")
        p = FakeProvider()
        run = run_case(self.store, self.case["id"], provider=p)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(p.calls, [])

    def test_prepared_text_cannot_replace_original(self):
        doc = self.store.get("documents", self.doc["id"])
        doc["prepared"]["units"][0]["text"] = "被篡改"
        self.store.put("documents", doc["id"], doc, replace=True)
        p = FakeProvider()
        result = run_case(self.store, self.case["id"], provider=p)
        self.assertEqual(result["status"], "audited")
        self.assertNotIn("被篡改", p.calls[0]["prompt"])

    def test_full_input_limits_fail_without_request(self):
        config = configuration()
        config["reading"]["max_input_characters"] = 1
        p = FakeProvider()
        result = run_case(self.store, self.case["id"], provider=p, config=config)
        self.assertEqual(result["status"], "reading_incomplete")
        self.assertEqual(p.calls, [])

    def test_missing_attachment_blocks_request(self):
        case = self.store.create_case([self.doc["id"]], "提取", "2026-01-02T00:00:00+08:00", expected_attachments=["附件.pdf"])
        p = FakeProvider()
        result = run_case(self.store, case["id"], provider=p)
        self.assertEqual(result["status"], "reading_incomplete")
        self.assertEqual(p.calls, [])

    def test_holdout_cannot_reuse_development_original(self):
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.store.create_case([self.doc["id"]], "另一条线", "2026-01-02T00:00:00+08:00", "holdout")

    def test_no_future_data(self):
        with self.assertRaisesRegex(ValueError, "晚于"):
            self.store.create_case([self.doc["id"]], "历史研究", "2025-01-02T00:00:00+08:00")

    def test_duplicate_cannot_change_group(self):
        with self.assertRaisesRegex(ValueError, "分组"):
            self.store.import_file(self.source, group_id="different")

    def test_stable_snapshot_is_not_mutable(self):
        stable = self.store.snapshot("extraction")
        stable["files"]["SKILL.md"] += "\n篡改"
        self.store.put("versions", stable["id"], stable, replace=True)
        with self.assertRaisesRegex(ValueError, "指纹"):
            run_case(self.store, self.case["id"], provider=FakeProvider())

    def test_api_failure_retains_original_record(self):
        p = FakeProvider()
        with patch.object(p, "call", side_effect=ProviderError("模拟超时")):
            run = run_case(self.store, self.case["id"], provider=p)
        self.assertEqual(self.store.get("runs", run["id"])["error"], "模拟超时")

    def test_material_fingerprint_repeatable(self):
        a = run_case(self.store, self.case["id"], provider=FakeProvider())
        b = run_case(self.store, self.case["id"], provider=FakeProvider())
        self.assertEqual(a["material_digest"], b["material_digest"])


class CalculationTest(unittest.TestCase):
    def test_units_and_growth(self):
        self.assertEqual(calculate("12950*10000")["value"], "129500000")
        self.assertEqual(calculate("(120-100)/100*100")["value"], "20.0")
        self.assertEqual(calculate("0.1+0.2")["value"], "0.3")

    def test_cashflow_and_sensitivity(self):
        value = calculate("(12000/1.1+13500/1.1**2+15000/1.1**3+(15000*1.02/(0.1-0.02))/1.1**3+5000-8000)/10000")["value"]
        self.assertTrue(float(value) > 17)

    def test_no_code_execution(self):
        for expr in ["__import__('os').system('echo x')", "open('x')", "2**10000", "[x for x in range(5)]", "(1).__class__"]:
            with self.subTest(expr=expr), self.assertRaises((ValueError, SyntaxError)):
                calculate(expr)

    def test_invalid_math(self):
        with self.assertRaises(Exception): calculate("1/0")


class StreamingTest(unittest.TestCase):
    def test_responses_terminal_event(self):
        event = {"type": "response.completed", "response": {"status": "completed", "model": "m", "output": []}}
        stream = io.BytesIO((": ping\n\ndata: " + json.dumps(event) + "\n\n").encode())
        result = read_event_stream(stream, "responses", 5000, time.monotonic() + 30)
        self.assertEqual(result["status"], "completed")

    def test_truncated_stream_rejected(self):
        with self.assertRaisesRegex(ProviderError, "中断"):
            read_event_stream(io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'), "responses", 5000, time.monotonic()+30)

    def test_chat_tool_arguments_reassembled(self):
        events = [
            {"model": "m", "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "calculate", "arguments": '{"expression":"1'}}]}, "finish_reason": None}]},
            {"model": "m", "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '+2"}'}}]}, "finish_reason": "tool_calls"}]}
        ]
        stream = io.BytesIO(("".join("data: "+json.dumps(e)+"\n\n" for e in events)+"data: [DONE]\n\n").encode())
        result = read_event_stream(stream, "chat_completions", 5000, time.monotonic()+30)
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], '{"expression":"1+2"}')


class WebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = ResearchServer(("127.0.0.1", 0), Store(self.temp.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:"+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.server.queue.shutdown(wait=True)
        self.temp.cleanup()

    def test_dashboard_and_state(self):
        with urllib.request.urlopen(self.base) as response:
            self.assertIn("金融研究工作台", response.read().decode())
        with urllib.request.urlopen(self.base+"/api/state") as response:
            value = json.load(response)
        self.assertEqual(len(value["lines"]), 7)
        self.assertNotIn("experimental_bearer_token", json.dumps(value))

    def test_cross_origin_mutation_refused(self):
        request = urllib.request.Request(self.base+"/api/case", b'{}', {"Content-Type":"application/json", "Origin":"https://other.example"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 403)

    def test_historical_missing_data_does_not_hide_failed_audit(self):
        record = {"id": "legacy-run", "status": "audited", "release_status": "needs_data",
                  "result": {"status": "needs_data"}, "audit": {"verdict": "revise"}}
        self.server.store.put("runs", record["id"], record)
        with urllib.request.urlopen(self.base + "/api/state") as response:
            state = json.load(response)
        self.assertEqual(state["runs"][0]["release_status"], "requires_review")
        with urllib.request.urlopen(self.base + "/api/run?id=legacy-run") as response:
            detail = json.load(response)
        self.assertEqual(detail["effective_release_status"], "requires_review")
        self.assertEqual(detail["release_status"], "needs_data")
        self.assertEqual(self.server.store.get("runs", record["id"]), record)

    def test_path_traversal_refused(self):
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base+"/api/run?id=../../config")

    def test_upload_case_roundtrip(self):
        import base64
        def post(path, body):
            request = urllib.request.Request(self.base+path, json.dumps(body).encode(), {"Content-Type":"application/json", "Origin":self.base})
            with urllib.request.urlopen(request) as response: return json.load(response)
        job=post('/api/import',{'name':'test.txt','base64':base64.b64encode('演练文本'.encode()).decode(),'published_at':'2026-01-01T00:00:00+08:00'})
        for _ in range(100):
            if self.server.jobs[job['job_id']]['status'] not in {'queued','running'}: break
            time.sleep(.01)
        self.assertEqual(self.server.jobs[job['job_id']]['status'],'completed')
        document=self.server.jobs[job['job_id']]['result']
        casejob=post('/api/case',{'document_ids':[document],'task':'提取所有文字','cutoff':'2026-01-02T00:00:00+08:00'})
        for _ in range(100):
            if self.server.jobs[casejob['job_id']]['status'] not in {'queued','running'}: break
            time.sleep(.01)
        self.assertEqual(self.server.jobs[casejob['job_id']]['status'],'completed')


if __name__ == "__main__":
    unittest.main()
