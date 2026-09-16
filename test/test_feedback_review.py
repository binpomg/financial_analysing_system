import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finresearch.feedback_review import review_run_feedback
from finresearch.runner import confirm_issue, run_case
from finresearch.storage import Store, configuration, digest, read_json, write_json
from test.test_system import FakeProvider


class VerificationProvider:
    def __init__(self, change=None, failure=None):
        self.calls = []
        self.change = change
        self.failure = failure

    def call(self, role, instructions, prompt, schema, images=None, trace_dir=None):
        self.calls.append({"role": role, "instructions": instructions, "prompt": prompt, "images": images})
        if self.failure:
            raise self.failure
        material, review = prompt.split("\n待核实业务成果与问题：\n")
        units = json.loads(material)["source_units"]
        issues = json.loads(review)["issues"]
        result = {"coverage": [{"document_id": u["document_id"], "unit_id": u["unit_id"], "status": "read", "note": "程序夹具阅读记录"} for u in units],
                  "decisions": [{"issue_index": i["issue_index"], "verdict": "confirmed", "reason": "原文100，成果记录200，数值不一致。",
                                 "result_pointers": ["/records/0/value"], "evidence": [{"document_id": units[0]["document_id"], "unit_id": units[0]["unit_id"], "quote": "营业收入100万元"}]} for i in issues]}
        if self.change:
            self.change(result)
        return result


class FeedbackReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        first = self.root / "original.txt"
        first.write_text("演练公司2025年营业收入100万元。", encoding="utf-8")
        second = self.root / "notes.txt"
        second.write_text("附注全文也必须进入核实请求。统计范围为合并口径。", encoding="utf-8")
        self.docs = [self.store.import_file(path, published_at="2026-01-01T00:00:00+08:00") for path in (first, second)]
        self.case = self.store.create_case([doc["id"] for doc in self.docs], "提取所有数字", "2026-01-02T00:00:00+08:00")
        self.run = self.make_run()

    def tearDown(self):
        self.temp.cleanup()

    def make_run(self, issue_count=1, case_id=None):
        def change(role, result):
            if role == "business":
                result["result"]["records"][0]["value"] = "200"
            else:
                unit = self.docs[0]["prepared"]["units"][0]
                result["audit"].update(verdict="revise", issues=[{"severity": "major", "category": "数值", "description": "演练错误%s" % index,
                                     "evidence": [{"document_id": unit["document_id"], "unit_id": unit["unit_id"], "quote": "营业收入100万元"}], "correction": "应保留100万元"} for index in range(issue_count)])
                result["audit"]["dimensions"][0]["judgment"] = "fail"
        run = run_case(self.store, case_id or self.case["id"], provider=FakeProvider(change))
        self.assertEqual(run["status"], "audited", run.get("error"))
        return run

    def test_confirmed_reads_all_originals_and_keeps_independent_provenance(self):
        before = digest(self.store.get("runs", self.run["id"]))
        provider = VerificationProvider()
        reviewed = review_run_feedback(self.store, self.run["id"], provider=provider)
        self.assertEqual(reviewed["status"], "completed", reviewed.get("error"))
        self.assertEqual(len(reviewed["feedback_ids"]), 1)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["role"], "audit")
        self.assertIn("附注全文也必须进入核实请求", provider.calls[0]["prompt"])
        self.assertNotIn(self.run["skill_version"], provider.calls[0]["prompt"])
        self.assertNotIn("execution_coverage", provider.calls[0]["prompt"])
        feedback = self.store.get("feedback", reviewed["feedback_ids"][0])
        self.assertEqual(feedback["verification_id"], reviewed["id"])
        self.assertEqual(feedback["verification_kind"], "independent_model_check")
        self.assertFalse(feedback["expert_calibrated"])
        self.assertEqual(digest(self.store.get("runs", self.run["id"])), before)
        self.assertFalse(reviewed["promotion_allowed"])

    def test_rejected_and_uncertain_are_saved_without_feedback(self):
        run = self.make_run(2)
        def change(output):
            output["decisions"][0]["verdict"] = "rejected"
            output["decisions"][1]["verdict"] = "uncertain"
        reviewed = review_run_feedback(self.store, run["id"], provider=VerificationProvider(change))
        self.assertEqual(reviewed["status"], "completed", reviewed.get("error"))
        self.assertEqual([item["verdict"] for item in reviewed["decisions"]], ["rejected", "uncertain"])
        self.assertEqual(self.store.list("feedback"), [])

    def test_confirmed_without_evidence_is_demoted(self):
        provider = VerificationProvider(lambda output: output["decisions"][0].update(evidence=[]))
        reviewed = review_run_feedback(self.store, self.run["id"], provider=provider)
        self.assertEqual(reviewed["status"], "completed", reviewed.get("error"))
        self.assertEqual(reviewed["decisions"][0]["model_verdict"], "confirmed")
        self.assertEqual(reviewed["decisions"][0]["verdict"], "uncertain")
        self.assertEqual(self.store.list("feedback"), [])

    def test_visual_citation_needing_manual_check_does_not_auto_confirm(self):
        with patch("finresearch.feedback_review.schemas.validate_evidence", return_value=["需视觉核验引文：doc/page1"]):
            reviewed = review_run_feedback(self.store, self.run["id"], provider=VerificationProvider())
        self.assertEqual(reviewed["status"], "completed", reviewed.get("error"))
        self.assertEqual(reviewed["decisions"][0]["verdict"], "uncertain")
        self.assertFalse(reviewed["feedback_ids"])

    def test_invented_quote_fails_without_feedback(self):
        provider = VerificationProvider(lambda output: output["decisions"][0]["evidence"][0].update(quote="营业收入999万元"))
        reviewed = review_run_feedback(self.store, self.run["id"], provider=provider)
        self.assertEqual(reviewed["status"], "failed")
        self.assertIn("不匹配", reviewed["error"])
        self.assertEqual(self.store.list("feedback"), [])

    def test_missing_full_coverage_fails_without_feedback(self):
        provider = VerificationProvider(lambda output: output["coverage"].pop())
        reviewed = review_run_feedback(self.store, self.run["id"], provider=provider)
        self.assertEqual(reviewed["status"], "failed")
        self.assertIn("覆盖", reviewed["error"])
        self.assertEqual(self.store.list("feedback"), [])

    def test_invalid_issue_indices_cannot_partially_register_batch(self):
        mutations = [lambda output: output["decisions"].pop(),
                     lambda output: output["decisions"][1].update(issue_index=0),
                     lambda output: output["decisions"][1].update(issue_index=9)]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                run = self.make_run(2)
                reviewed = review_run_feedback(self.store, run["id"], provider=VerificationProvider(mutation))
                self.assertEqual(reviewed["status"], "failed")
                self.assertIn("索引", reviewed["error"])
                self.assertEqual(self.store.list("feedback"), [])

    def test_invalid_result_pointer_fails(self):
        provider = VerificationProvider(lambda output: output["decisions"][0].update(result_pointers=["/records/99/value"]))
        reviewed = review_run_feedback(self.store, self.run["id"], provider=provider)
        self.assertEqual(reviewed["status"], "failed")
        self.assertIn("位置不存在", reviewed["error"])

    def test_changed_raw_refuses_request_and_no_automatic_retry(self):
        Path(self.docs[0]["raw_path"]).write_text("被修改的内容", encoding="utf-8")
        provider = VerificationProvider()
        reviewed = review_run_feedback(self.store, self.run["id"], provider=provider)
        self.assertEqual(reviewed["status"], "failed")
        self.assertIn("原件已变化", reviewed["error"])
        again = review_run_feedback(self.store, self.run["id"], provider=provider)
        self.assertEqual(again["status"], "already_reviewed")
        self.assertFalse(provider.calls)

    def test_changed_original_audit_record_is_refused(self):
        audit_path = self.store.root / "traces" / self.run["id"] / "original-audit.json"
        changed = read_json(audit_path)
        changed["audit"]["issues"][0]["description"] = "替换审核"
        write_json(audit_path, changed)
        provider = VerificationProvider()
        reviewed = review_run_feedback(self.store, self.run["id"], provider=provider)
        self.assertEqual(reviewed["status"], "failed")
        self.assertIn("原始记录不一致", reviewed["error"])
        self.assertFalse(provider.calls)

    def test_holdout_and_calibration_cannot_feed_improvement(self):
        for partition in ("holdout", "calibration"):
            run = copy.deepcopy(self.run)
            run["partition"] = partition
            self.store.put("runs", run["id"], run, replace=True)
            with self.subTest(partition=partition), self.assertRaisesRegex(ValueError, "不能进入"):
                review_run_feedback(self.store, run["id"], provider=VerificationProvider())
        self.assertEqual(self.store.list("feedback_verifications"), [])

    def test_same_group_holdout_from_other_line_is_refused(self):
        other_case = {**self.case, "id": "case-isolation-corruption", "partition": "holdout"}
        self.store.put("cases", other_case["id"], other_case)
        with self.assertRaisesRegex(ValueError, "同源资料组"):
            review_run_feedback(self.store, self.run["id"], provider=VerificationProvider())

    def test_max_issues_consumes_new_indices_only(self):
        run = self.make_run(3)
        provider = VerificationProvider()
        first = review_run_feedback(self.store, run["id"], max_issues=1, provider=provider)
        second = review_run_feedback(self.store, run["id"], max_issues=2, provider=provider)
        third = review_run_feedback(self.store, run["id"], max_issues=2, provider=provider)
        self.assertEqual(first["issue_indices"], [0])
        self.assertEqual(second["issue_indices"], [1, 2])
        self.assertEqual(third["status"], "already_reviewed")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(self.store.list("feedback")), 3)

    def test_severe_issues_are_selected_first_and_remaining_are_explicit(self):
        run = self.make_run(3)
        for item, severity in zip(run["audit"]["issues"], ["minor", "critical", "major"]):
            item["severity"] = severity
        self.store.put("runs", run["id"], run, replace=True)
        original_path = self.store.root / "traces" / run["id"] / "original-audit.json"
        original = read_json(original_path)
        original["audit"] = run["audit"]
        write_json(original_path, original)
        reviewed = review_run_feedback(self.store, run["id"], max_issues=2, provider=VerificationProvider())
        self.assertEqual(reviewed["status"], "completed", reviewed.get("error"))
        self.assertEqual(reviewed["issue_indices"], [1, 2])
        self.assertEqual(reviewed["remaining_issue_indices"], [0])
        self.assertEqual(reviewed["total_issues"], 3)

    def test_feedback_first_write_already_contains_verification_provenance(self):
        original_put = self.store.put
        captured = []
        def inspect_put(category, identifier, value, replace=False):
            if category == "feedback":
                captured.append(copy.deepcopy(value))
                self.assertFalse(replace)
                self.assertEqual(value["verification_kind"], "independent_model_check")
                self.assertTrue(value["verification_id"])
                self.assertFalse(value["expert_calibrated"])
            return original_put(category, identifier, value, replace=replace)
        with patch.object(self.store, "put", side_effect=inspect_put):
            reviewed = review_run_feedback(self.store, self.run["id"], provider=VerificationProvider())
        self.assertEqual(reviewed["status"], "completed", reviewed.get("error"))
        self.assertEqual(len(captured), 1)

    def test_run_change_during_request_cannot_register_feedback(self):
        def mutate_run(output):
            changed = self.store.get("runs", self.run["id"])
            changed["result"]["summary"] = "请求期间被改写"
            self.store.put("runs", changed["id"], changed, replace=True)
        reviewed = review_run_feedback(self.store, self.run["id"], provider=VerificationProvider(mutate_run))
        self.assertEqual(reviewed["status"], "failed")
        self.assertIn("请求期间", reviewed["error"])
        self.assertEqual(self.store.list("feedback"), [])

    def test_incomplete_audit_cannot_enter_feedback_verification(self):
        changed = {**self.run, "status": "failed"}
        self.store.put("runs", changed["id"], changed, replace=True)
        with self.assertRaisesRegex(ValueError, "完整执行"):
            review_run_feedback(self.store, changed["id"], provider=VerificationProvider())

    def test_failed_and_interrupted_verifications_are_not_retried(self):
        for failure in (RuntimeError("API失败"), KeyboardInterrupt()):
            run = self.make_run()
            provider = VerificationProvider(failure=failure)
            if isinstance(failure, KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    review_run_feedback(self.store, run["id"], provider=provider)
                self.assertEqual([item for item in self.store.list("feedback_verifications") if item["run_id"] == run["id"]][0]["status"], "interrupted")
            else:
                reviewed = review_run_feedback(self.store, run["id"], provider=provider)
                self.assertEqual(reviewed["status"], "failed")
            again = review_run_feedback(self.store, run["id"], provider=provider)
            self.assertEqual(again["status"], "already_reviewed")
            self.assertEqual(len(provider.calls), 1)

    def test_permanent_claim_survives_crash_before_record_registration(self):
        claim = self.store.root / "feedback_review_claims" / self.run["id"] / "issue-00000"
        claim.mkdir(parents=True)
        provider = VerificationProvider()
        reviewed = review_run_feedback(self.store, self.run["id"], provider=provider)
        self.assertEqual(reviewed["status"], "already_reviewed")
        self.assertFalse(provider.calls)

    def test_existing_manually_confirmed_feedback_is_not_duplicated(self):
        confirm_issue(self.store, self.run["id"], 0, "人工独立核实原件100万元。")
        provider = VerificationProvider()
        reviewed = review_run_feedback(self.store, self.run["id"], provider=provider)
        self.assertEqual(reviewed["status"], "already_reviewed")
        self.assertFalse(provider.calls)
        self.assertEqual(len(self.store.list("feedback")), 1)

    def test_complete_input_capacity_exceeded_blocks_paid_request(self):
        config = configuration()
        config["reading"]["max_input_characters"] = 10
        provider = VerificationProvider()
        reviewed = review_run_feedback(self.store, self.run["id"], provider=provider, config=config)
        self.assertEqual(reviewed["status"], "failed")
        self.assertIn("不完整", reviewed["error"])
        self.assertFalse(provider.calls)

    def test_failure_log_redacts_current_provider_key(self):
        provider = VerificationProvider(failure=RuntimeError("API token test-secret-value"))
        provider.credentials = lambda: ("https://example.org/v1", "test-secret-value", "responses")
        reviewed = review_run_feedback(self.store, self.run["id"], provider=provider)
        self.assertEqual(reviewed["status"], "failed")
        self.assertNotIn("test-secret-value", json.dumps(reviewed))
        self.assertIn("[REDACTED]", reviewed["error"])


if __name__ == "__main__":
    unittest.main()
