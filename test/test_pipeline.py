import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finresearch.cli import parser
from finresearch.pipeline import run_pipeline, settings
from finresearch.runner import confirm_issue
from finresearch.storage import Store, configuration, now


class PipelineProvider:
    """程序测试夹具；不用于业务效果评价。"""
    def __init__(self, fail_business=None, interrupt_business=None):
        self.calls = []
        self.business_count = 0
        self.fail_business = fail_business
        self.interrupt_business = interrupt_business

    def call(self, role, instructions, prompt, schema, images=None, trace_dir=None):
        self.calls.append((role, prompt))
        material = json.loads(prompt.split("\n待审核成果")[0])
        units = material["source_units"]
        evidence = {"document_id": units[0]["document_id"], "unit_id": units[0]["unit_id"], "quote": "营业收入100万元"}
        coverage = [{"document_id": unit["document_id"], "unit_id": unit["unit_id"], "status": "read", "note": "程序测试已读"} for unit in units]
        if role == "business":
            self.business_count += 1
            if self.business_count == self.interrupt_business:
                raise KeyboardInterrupt()
            if self.business_count == self.fail_business:
                raise RuntimeError("模拟接口断流")
            return {"coverage": coverage, "result": {"status": "completed", "summary": "程序演练",
                    "records": [{"record_type": "observed", "entity": "测试主体", "period": "2025", "field": "营业收入",
                                 "raw_value": "100", "value": "100", "unit": "万元", "currency": "CNY", "evidence": [evidence]}],
                    "analysis": [], "missing_data": [], "warnings": []}}
        dimensions = json.loads(instructions.splitlines()[-1].split("：", 1)[1])
        return {"coverage": coverage, "audit": {"verdict": "pass", "issues": [], "summary": "仅程序测试",
                "dimensions": [{"name": name, "judgment": "pass", "reason": "测试逐项验证"} for name in dimensions]}}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.config = configuration()
        self.config["sources"] = []
        self.provider = PipelineProvider()
        self.doc = self.add_document("incoming", "new-source")

    def tearDown(self):
        self.temp.cleanup()

    def add_document(self, name, group):
        path = self.root / (name + ".md")
        path.write_text(name + "：营业收入100万元。程序测试夹具，不作投资证据。", encoding="utf-8")
        document = self.store.import_file(path, "https://example.org/" + name, group, "2026-01-01T00:00:00+08:00")
        self.store.put("collections", "collection-" + name, {"id": "collection-" + name, "document_ids": [document["id"]], "created_at": now()})
        return document

    def run_batch(self, pipeline_id=None):
        with self.store.exclusive():
            return run_pipeline(self.store, pipeline_id, self.provider, self.config)

    def test_one_original_reaches_all_seven_independent_lines(self):
        batch = self.run_batch()
        self.assertEqual(batch["status"], "completed")
        self.assertEqual(batch["document_ids"], [self.doc["id"]])
        self.assertEqual(len(batch["steps"]), 7)
        self.assertEqual(self.provider.business_count, 7)
        self.assertEqual(len(self.provider.calls), 14)
        self.assertEqual({step["feedback_status"] for step in batch["steps"]}, {"no_issues"})
        self.assertTrue(all(self.doc["id"] in prompt for _, prompt in self.provider.calls))
        self.assertFalse(batch["automatic_promotion"])
        self.assertEqual(self.store.get("cases", batch["case_ids"][0])["partition"], "development")

    def test_completed_batch_and_consumed_original_are_not_reprocessed(self):
        first = self.run_batch()
        self.run_batch(first["id"])
        second = self.run_batch()
        self.assertEqual(self.provider.business_count, 7)
        self.assertFalse(second["document_ids"])
        self.assertEqual(len(self.store.list("cases")), 1)

    def test_failed_line_is_retained_and_other_lines_continue(self):
        self.provider.fail_business = 2
        batch = self.run_batch()
        self.assertEqual(batch["status"], "completed_with_issues")
        self.assertEqual(len(batch["steps"]), 7)
        self.assertEqual(batch["steps"][1]["run_status"], "failed")
        self.assertEqual(sum(step["run_status"] == "audited" for step in batch["steps"]), 6)
        self.assertEqual(self.provider.business_count, 7)

    def test_resume_only_starts_steps_never_attempted(self):
        self.provider.interrupt_business = 2
        with self.assertRaises(KeyboardInterrupt):
            self.run_batch()
        batch = self.store.list("pipelines")[0]
        self.assertEqual(batch["status"], "interrupted")
        self.assertEqual(self.provider.business_count, 2)
        final = self.run_batch(batch["id"])
        self.assertEqual(final["status"], "completed_with_issues")
        self.assertEqual(self.provider.business_count, 7)
        self.assertEqual(len(self.store.list("runs")), 7)
        self.assertEqual(final["steps"][1]["run_status"], "interrupted")

    def test_resume_rejects_changed_model_configuration(self):
        self.provider.interrupt_business = 1
        with self.assertRaises(KeyboardInterrupt):
            self.run_batch()
        batch = self.store.list("pipelines")[0]
        self.config["models"]["business"]["reasoning_effort"] = "high"
        with self.assertRaisesRegex(ValueError, "环境已变化"):
            self.run_batch(batch["id"])
        self.assertEqual(self.provider.business_count, 1)

    def test_live_owner_blocks_duplicate_resume(self):
        self.provider.interrupt_business = 1
        with self.assertRaises(KeyboardInterrupt):
            self.run_batch()
        batch = self.store.list("pipelines")[0]
        batch["status"] = "running"
        self.store.put("pipelines", batch["id"], batch, replace=True)
        with self.assertRaisesRegex(ValueError, "仍存活"):
            self.run_batch(batch["id"])

    def test_model_change_during_batch_does_not_mutate_frozen_settings(self):
        original_call = self.provider.call
        def mutate_config(*args, **kwargs):
            result = original_call(*args, **kwargs)
            if args[0] == "business":
                self.config["models"]["business"]["reasoning_effort"] = "high"
            return result
        with patch.object(self.provider, "call", side_effect=mutate_config):
            batch = self.run_batch()
        self.assertEqual(batch["status"], "failed")
        self.assertEqual(batch["frozen"]["models"]["business"]["reasoning_effort"], "xhigh")
        self.assertEqual(self.provider.business_count, 1)

    def test_missing_time_never_becomes_an_automatic_case(self):
        document = self.store.get("documents", self.doc["id"])
        document["published_at"] = None
        self.store.put("documents", self.doc["id"], document, replace=True)
        batch = self.run_batch()
        self.assertEqual(batch["status"], "blocked")
        self.assertFalse(self.store.list("cases"))
        self.assertFalse(self.provider.calls)

    def test_feedback_exception_does_not_hide_other_line_results(self):
        with patch("finresearch.pipeline.review_run_feedback", side_effect=RuntimeError("模拟核实异常")):
            batch = self.run_batch()
        self.assertEqual(batch["status"], "completed_with_issues")
        self.assertEqual(sum(step["run_status"] == "audited" for step in batch["steps"]), 7)
        self.assertTrue(all(step["feedback_status"] == "failed" for step in batch["steps"]))

    def add_confirmed(self, name, group):
        document = self.add_document(name, group)
        case = self.store.create_case([document["id"]], "历史程序夹具", "2026-01-02T00:00:00+08:00")
        version = self.store.snapshot("extraction")
        run = {"id": "prior-" + name, "line": "extraction", "case_id": case["id"], "partition": "development",
               "group_ids": [group], "skill_version": version["id"], "status": "audited", "result": {},
               "audit": {"issues": [{"severity": "minor", "description": "测试问题"}]}}
        self.store.put("runs", run["id"], run)
        return confirm_issue(self.store, run["id"], 0, "仅程序测试的核实记录")

    def test_candidate_waits_for_independent_groups_not_case_count(self):
        for index in range(3):
            self.add_confirmed("same" + str(index), "one-event")
        with patch("finresearch.pipeline.propose_candidate") as proposal:
            batch = self.run_batch()
        proposal.assert_not_called()
        entry = next(item for item in batch["candidate_waiting"] if item["line"] == "extraction")
        self.assertEqual(entry["confirmed_case_count"], 1)

    def test_candidate_attempt_is_bounded_and_failure_is_not_retried_next_batch(self):
        for index in range(3):
            self.add_confirmed("independent" + str(index), "event" + str(index))
        with patch("finresearch.pipeline.propose_candidate", side_effect=RuntimeError("模拟候选断流")) as proposal:
            batch = self.run_batch()
            for index in range(3):
                confirm_issue(self.store, "prior-independent" + str(index), 0, "同一问题再次人工登记，不应重新收费尝试")
            self.add_document("incoming-next", "next-event")
            self.run_batch()
        self.assertEqual(proposal.call_count, 1)
        self.assertEqual(batch["candidate_attempts"][0]["status"], "failed")
        self.assertFalse(self.store.list("adoptions"))

    def test_limits_and_cli(self):
        config = copy.deepcopy(self.config)
        config["pipeline"]["max_documents"] = 2
        with self.assertRaises(ValueError):
            settings(config)
        self.assertEqual(parser().parse_args(["pipeline"]).resume, None)
        self.assertEqual(parser().parse_args(["pipeline", "--resume", "p1"]).resume, "p1")
        self.assertEqual(parser().parse_args(["verify-feedback", "r1"]).limit, 2)


if __name__ == "__main__":
    unittest.main()
