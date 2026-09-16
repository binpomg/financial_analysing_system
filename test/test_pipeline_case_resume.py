"""预约案例写入切点恢复；仅用隔离Store和程序Provider，不调用外部服务。"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finresearch.pipeline import TASK, run_pipeline
from finresearch.runner import run_case
from finresearch.storage import Store, configuration, digest, now
from test.test_pipeline import PipelineProvider


class PipelineCaseResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.config = configuration()
        self.config["sources"] = []
        self.provider = PipelineProvider()
        path = self.root / "original.md"
        path.write_text("独立程序夹具：营业收入100万元。", encoding="utf-8")
        self.doc = self.store.import_file(path, "https://example.org/case-resume", "case-resume-event",
                                          "2026-01-01T00:00:00+08:00")
        self.store.put("collections", "collection-resume", {"id": "collection-resume", "document_ids": [self.doc["id"]], "created_at": now()})

    def tearDown(self):
        self.temp.cleanup()

    def run_batch(self, identifier=None):
        with self.store.exclusive():
            return run_pipeline(self.store, identifier, self.provider, self.config)

    def interrupt_case_write(self, after_write=True):
        original_put = self.store.put
        def interrupted_put(category, identifier, value, replace=False):
            if category == "cases":
                if after_write:
                    original_put(category, identifier, value, replace)
                raise KeyboardInterrupt("case commit cut point")
            return original_put(category, identifier, value, replace)
        with patch.object(self.store, "put", side_effect=interrupted_put), self.assertRaises(KeyboardInterrupt):
            self.run_batch()
        batch = self.store.list("pipelines")[0]
        self.assertEqual(batch["status"], "interrupted")
        self.assertEqual(batch["case_ids"], [])
        self.assertEqual(batch["steps"], [])
        self.assertEqual(self.provider.calls, [])
        return batch

    def test_after_case_commit_resume_reuses_exact_case_without_rewriting(self):
        batch = self.interrupt_case_write()
        original = self.store.list("cases")[0]
        self.assertEqual(original["id"], batch["case_reservation"]["case_id"])
        self.assertEqual(original["origin"]["pipeline_id"], batch["id"])
        self.assertEqual(original["origin"]["reservation_digest"], digest(batch["case_reservation"]))
        with patch.object(self.store, "create_case", side_effect=AssertionError("must reuse committed case")):
            final = self.run_batch(batch["id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["case_ids"], [original["id"]])
        self.assertEqual(self.store.list("cases"), [original])
        self.assertEqual(len(self.store.list("runs")), 7)
        self.assertEqual(self.provider.business_count, 7)
        self.assertEqual(len(self.provider.calls), 14)

    def test_before_case_commit_resume_creates_reserved_id_once(self):
        batch = self.interrupt_case_write(after_write=False)
        self.assertEqual(self.store.list("cases"), [])
        final = self.run_batch(batch["id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["case_ids"], [batch["case_reservation"]["case_id"]])
        self.assertEqual(len(self.store.list("cases")), 1)
        self.assertEqual(self.provider.business_count, 7)

    def test_case_recovery_then_second_line_interrupt_never_reissues_attempted_line(self):
        batch = self.interrupt_case_write()
        self.provider.interrupt_business = 2
        with self.assertRaises(KeyboardInterrupt):
            self.run_batch(batch["id"])
        self.assertEqual(self.provider.business_count, 2)
        final = self.run_batch(batch["id"])
        self.assertEqual(final["status"], "completed_with_issues")
        self.assertEqual(final["steps"][1]["run_status"], "interrupted")
        self.assertEqual(len(self.store.list("cases")), 1)
        self.assertEqual(len(self.store.list("runs")), 7)
        self.assertEqual(self.provider.business_count, 7)
        self.assertEqual(len(self.provider.calls), 13)

    def test_same_input_unowned_case_is_not_adopted(self):
        batch = self.interrupt_case_write(after_write=False)
        reservation = batch["case_reservation"]
        unrelated = self.store.create_case(reservation["document_ids"], reservation["task"], reservation["cutoff"])
        final = self.run_batch(batch["id"])
        self.assertEqual(final["status"], "failed")
        self.assertIn("不能自动接管", final["error"])
        self.assertEqual(self.store.list("cases"), [unrelated])
        self.assertEqual(self.provider.calls, [])

    def test_prepatch_orphan_without_reservation_fails_closed(self):
        batch = self.interrupt_case_write()
        batch.pop("case_reservation")
        self.store.put("pipelines", batch["id"], batch, replace=True)
        original = self.store.list("cases")[0]
        final = self.run_batch(batch["id"])
        self.assertEqual(final["status"], "failed")
        self.assertIn("未预登记归属", final["error"])
        self.assertEqual(self.store.list("cases"), [original])
        self.assertEqual(self.provider.calls, [])

    def test_wrong_owner_or_scope_or_digest_never_adopted(self):
        batch = self.interrupt_case_write()
        original = self.store.list("cases")[0]
        mutations = {"task": "unrelated task", "cutoff": "2026-01-02T00:00:00+08:00",
                     "partition": "validation", "document_ids": ["unrelated-document"],
                     "expected_attachments": ["unprovided.pdf"], "input_digest": "0" * 64,
                     "group_ids": ["unrelated-event"], "documents": [],
                     "origin": {**original["origin"], "pipeline_id": "pipeline-unrelated"}}
        for field, value in mutations.items():
            with self.subTest(field=field):
                altered = {**original, field: value}
                self.store.put("cases", original["id"], altered, replace=True)
                self.store.put("pipelines", batch["id"], batch, replace=True)
                final = self.run_batch(batch["id"])
                self.assertEqual(final["status"], "failed")
                self.assertIn("不能自动接管", final["error"])
                self.assertEqual(self.store.list("cases"), [altered])
                self.assertEqual(self.provider.calls, [])

    def test_two_related_cases_are_not_merged_or_overwritten(self):
        batch = self.interrupt_case_write()
        original = self.store.list("cases")[0]
        copied = {**original, "id": "case-ambiguous-owner"}
        self.store.put("cases", copied["id"], copied)
        final = self.run_batch(batch["id"])
        self.assertEqual(final["status"], "failed")
        self.assertIn("归属不唯一", final["error"])
        self.assertEqual(len(self.store.list("cases")), 2)
        self.assertEqual(self.provider.calls, [])

    def test_orphan_with_manually_executed_run_does_not_restart_business(self):
        batch = self.interrupt_case_write()
        case = self.store.list("cases")[0]
        prior = run_case(self.store, case["id"], "extraction", provider=self.provider, config=self.config)
        self.assertEqual(prior["status"], "audited")
        self.assertEqual(len(self.provider.calls), 2)
        final = self.run_batch(batch["id"])
        self.assertEqual(final["status"], "failed")
        self.assertIn("已有执行记录", final["error"])
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.store.list("runs"), [prior])

    def test_invalid_reservation_does_not_create_a_case(self):
        batch = self.interrupt_case_write(after_write=False)
        for key, value in (("pipeline_id", "pipeline-other"), ("task", "other task"),
                           ("document_ids", []), ("partition", "holdout"), ("case_id", "../../escape")):
            with self.subTest(key=key):
                damaged = copy.deepcopy(batch)
                damaged["case_reservation"][key] = value
                self.store.put("pipelines", batch["id"], damaged, replace=True)
                final = self.run_batch(batch["id"])
                self.assertEqual(final["status"], "failed")
                self.assertEqual(self.store.list("cases"), [])
                self.assertEqual(self.provider.calls, [])

    def test_default_create_case_is_unchanged_and_has_no_origin(self):
        case = self.store.create_case([self.doc["id"]], "manual task", now())
        self.assertTrue(case["id"].startswith("case-"))
        self.assertNotIn("origin", case)

    def test_reserved_create_case_is_safe_and_cannot_overwrite(self):
        origin = {"kind": "pipeline", "pipeline_id": "pipeline-example", "reservation_digest": "a" * 64}
        case = self.store.create_case([self.doc["id"]], TASK, now(), case_id="case-reserved", origin=origin)
        origin["pipeline_id"] = "pipeline-mutated"
        self.assertEqual(case["origin"]["pipeline_id"], "pipeline-example")
        with self.assertRaises(ValueError):
            self.store.create_case([self.doc["id"]], "overwrite", now(), case_id="case-reserved")
        self.assertEqual(self.store.list("cases"), [case])

    def test_invalid_case_id_and_origin_rejected_before_writing(self):
        valid = {"kind": "pipeline", "pipeline_id": "pipeline-example", "reservation_digest": "a" * 64}
        proposals = [{"case_id": "../../escape"}, {"case_id": ""}, {"case_id": 7},
                     {"origin": valid}, {"case_id": "case-safe", "origin": []},
                     {"case_id": "case-safe", "origin": {**valid, "unknown": True}},
                     {"case_id": "case-safe", "origin": {**valid, "kind": "other"}},
                     {"case_id": "case-safe", "origin": {**valid, "pipeline_id": "../escape"}},
                     {"case_id": "case-safe", "origin": {**valid, "reservation_digest": "not-a-digest"}}]
        for kwargs in proposals:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.create_case([self.doc["id"]], TASK, now(), **kwargs)
        self.assertEqual(self.store.list("cases"), [])

    def test_reserved_create_case_keeps_time_and_partition_guards(self):
        origin = {"kind": "pipeline", "pipeline_id": "pipeline-example", "reservation_digest": "a" * 64}
        with self.assertRaisesRegex(ValueError, "晚于"):
            self.store.create_case([self.doc["id"]], TASK, "2025-01-01T00:00:00+08:00", case_id="case-time", origin=origin)
        with self.assertRaisesRegex(ValueError, "时区"):
            self.store.create_case([self.doc["id"]], TASK, "2026-01-02T00:00:00", case_id="case-zone", origin=origin)
        self.store.create_case([self.doc["id"]], "holdout", now(), "holdout")
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.store.create_case([self.doc["id"]], TASK, now(), case_id="case-conflict", origin=origin)
        self.assertEqual(len(self.store.list("cases")), 1)


if __name__ == "__main__":
    unittest.main()
