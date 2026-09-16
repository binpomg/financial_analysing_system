import tempfile
import unittest
import copy
from pathlib import Path

from finresearch.runner import run_case, resume_audit, effective_release_status
from finresearch.storage import Store, configuration, digest, read_json
from finresearch.evaluation import compare_runs
from test.test_system import FakeProvider


class ResumeTest(unittest.TestCase):
    def test_complete_execution_can_continue_audit_without_reexecution(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'sample.txt'
            source.write_text('演练公司2025年营业收入100万元。',encoding='utf-8')
            store=Store(root/'data');doc=store.import_file(source,published_at='2026-01-01T00:00:00+08:00')
            case=store.create_case([doc['id']],'提取','2026-01-02T00:00:00+08:00')
            original_provider=FakeProvider()
            call=original_provider.call
            def interrupted(role,*args,**kwargs):
                if role=='audit': raise RuntimeError('模拟审核中断')
                return call(role,*args,**kwargs)
            original_provider.call=interrupted
            original=run_case(store,case['id'],provider=original_provider)
            self.assertEqual(original['status'],'failed')
            provider=FakeProvider()
            resumed=resume_audit(store,original['id'],provider=provider)
            self.assertEqual(resumed['status'],'audited')
            self.assertEqual([c['role'] for c in provider.calls],['audit'])
            self.assertEqual(store.get('runs',original['id'])['status'],'failed')
            self.assertEqual(resumed['result'],original['result'])

    def test_resume_refuses_changed_material(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'sample.txt'
            source.write_text('演练公司2025年营业收入100万元。',encoding='utf-8')
            store=Store(root/'data');doc=store.import_file(source,published_at='2026-01-01T00:00:00+08:00')
            case=store.create_case([doc['id']],'提取','2026-01-02T00:00:00+08:00')
            original=run_case(store,case['id'],provider=FakeProvider())
            Path(doc['raw_path']).write_text('篡改原文',encoding='utf-8')
            provider=FakeProvider();resumed=resume_audit(store,original['id'],provider=provider)
            self.assertEqual(resumed['status'],'failed');self.assertFalse(provider.calls)


class RecoveryProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        source = root / "sample.txt"
        source.write_text("演练公司2025年营业收入100万元。", encoding="utf-8")
        self.store = Store(root / "data")
        doc = self.store.import_file(source, published_at="2026-01-01T00:00:00+08:00")
        self.case = self.store.create_case([doc["id"]], "提取", "2026-01-02T00:00:00+08:00")
        self.config = configuration()

    def tearDown(self):
        self.temporary.cleanup()

    def test_resume_records_new_api_environment_without_rewriting_execution(self):
        original = run_case(self.store, self.case["id"], provider=FakeProvider(), config=self.config)
        before = digest(self.store.get("runs", original["id"]))
        changed = copy.deepcopy(self.config)
        changed["api"]["max_output_tokens"] += 1
        changed["api"]["max_tool_rounds"] = changed["api"].get("max_tool_rounds", 12) + 1
        resumed = resume_audit(self.store, original["id"], provider=FakeProvider(), config=changed)
        self.assertEqual(resumed["status"], "audited")
        self.assertEqual(resumed["execution_model_config"], original["model_config"])
        self.assertEqual(resumed["audit_model_config"]["max_output_tokens"], changed["api"]["max_output_tokens"])
        self.assertNotEqual(resumed["environment_digest"], original["environment_digest"])
        self.assertEqual(resumed["execution_environment_digest"], original["environment_digest"])
        self.assertEqual(digest(self.store.get("runs", original["id"])), before)
        environment = read_json(self.store.root / "traces" / resumed["id"] / "environment.json")
        self.assertEqual(digest(environment), resumed["environment_digest"])

    def test_current_audit_model_is_recorded_separately_from_original_business(self):
        original = run_case(self.store, self.case["id"], provider=FakeProvider(), config=self.config)
        changed = copy.deepcopy(self.config)
        changed["models"]["audit"]["reasoning_effort"] = "test-alternate-effort"
        resumed = resume_audit(self.store, original["id"], provider=FakeProvider(), config=changed)
        self.assertEqual(resumed["audit_model"], changed["models"]["audit"])
        self.assertEqual(resumed["model"], original["model"])
        self.assertEqual(resumed["reasoning_effort"], original["reasoning_effort"])
        self.assertFalse(resumed["evaluation_eligible"])

    def test_resumed_result_is_excluded_from_candidate_comparison(self):
        original = run_case(self.store, self.case["id"], provider=FakeProvider(), config=self.config)
        resumed = resume_audit(self.store, original["id"], provider=FakeProvider(), config=self.config)
        report = compare_runs(original, resumed)
        self.assertEqual(report["decision"], "incomparable")
        self.assertTrue(any("恢复" in reason for reason in report["reasons"]))

    def test_normal_and_resumed_audit_use_identical_role_inputs(self):
        first_provider = FakeProvider()
        original = run_case(self.store, self.case["id"], provider=first_provider, config=self.config)
        second_provider = FakeProvider()
        resume_audit(self.store, original["id"], provider=second_provider, config=self.config)
        original_audit = next(call for call in first_provider.calls if call["role"] == "audit")
        self.assertEqual(original_audit["instructions"], second_provider.calls[0]["instructions"])
        self.assertEqual(original_audit["prompt"], second_provider.calls[0]["prompt"])

    def test_repeated_resume_retains_original_execution_environment(self):
        original = run_case(self.store, self.case["id"], provider=FakeProvider(), config=self.config)
        first = resume_audit(self.store, original["id"], provider=FakeProvider(), config=self.config)
        second = resume_audit(self.store, first["id"], provider=FakeProvider(), config=self.config)
        self.assertEqual(second["execution_environment"], first["execution_environment"])
        self.assertEqual(second["execution_environment_digest"], original["environment_digest"])
        self.assertEqual(second["execution_model_config"], original["model_config"])

    def test_normal_run_keeps_audit_revision_ahead_of_missing_data(self):
        def revise(role, value):
            if role == "business":
                value["result"].update(status="needs_data", missing_data=["需补充历史期间原始数据"])
            else:
                unit = value["coverage"][0]
                value["audit"].update(verdict="revise", issues=[{
                    "severity": "major", "category": "scope", "description": "已提取字段仍有需要修订的问题",
                    "evidence": [{"document_id": unit["document_id"], "unit_id": unit["unit_id"], "quote": "营业收入100万元"}],
                    "correction": "回查已提供资料并修订提取范围。"}])
                value["audit"]["dimensions"][0]["judgment"] = "fail"
        run = run_case(self.store, self.case["id"], provider=FakeProvider(revise), config=self.config)
        self.assertEqual(run["status"], "audited")
        self.assertEqual(run["result"]["status"], "needs_data")
        self.assertEqual(run["release_status"], "requires_review")
        historical = {**run, "release_status": "needs_data"}
        snapshot = digest(historical)
        self.assertEqual(effective_release_status(historical), "requires_review")
        self.assertEqual(digest(historical), snapshot)
        for result_status in ("needs_data", "not_applicable"):
            reviewed = {**run, "audit": {"verdict": "pass"}, "result": {"status": result_status}}
            self.assertEqual(effective_release_status(reviewed), result_status)

    def test_resumed_audit_revision_is_not_hidden_by_missing_data(self):
        def needs_data(role, value):
            if role == "business":
                value["result"].update(status="needs_data", missing_data=["需补充历史期间原始数据"])
        original = run_case(self.store, self.case["id"], provider=FakeProvider(needs_data), config=self.config)
        original_digest = digest(self.store.get("runs", original["id"]))
        def revise(role, value):
            unit = value["coverage"][0]
            value["audit"].update(verdict="revise", issues=[{
                "severity": "major", "category": "scope", "description": "当前成果的范围仍需修订",
                "evidence": [{"document_id": unit["document_id"], "unit_id": unit["unit_id"], "quote": "营业收入100万元"}],
                "correction": "先修订已提供资料支持的内容，再补充缺失资料。"}])
            value["audit"]["dimensions"][0]["judgment"] = "fail"
        resumed = resume_audit(self.store, original["id"], provider=FakeProvider(revise), config=self.config)
        self.assertEqual(resumed["status"], "audited")
        self.assertEqual(resumed["result"]["status"], "needs_data")
        self.assertEqual(resumed["release_status"], "requires_review")
        self.assertEqual(digest(self.store.get("runs", original["id"])), original_digest)

    def test_interrupt_during_business_is_persisted_and_lock_released(self):
        def interrupt(role, value):
            if role == "business":
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            with self.store.exclusive():
                run_case(self.store, self.case["id"], provider=FakeProvider(interrupt), config=self.config)
        run = self.store.list("runs")[0]
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(run["interrupted_stage"], "running")
        self.assertEqual(run["release_status"], "blocked")
        self.assertIn("finished_at", run)
        self.assertIsNone(run["result"])
        with self.store.exclusive():
            self.assertTrue((self.store.root / "outputs" / run["id"] / "result.json").is_file())

    def test_interrupt_during_audit_preserves_business_checkpoint(self):
        def interrupt(role, value):
            if role == "audit":
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            run_case(self.store, self.case["id"], provider=FakeProvider(interrupt), config=self.config)
        run = self.store.list("runs")[0]
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(run["interrupted_stage"], "auditing")
        self.assertEqual(run["result"]["status"], "completed")
        resumed = resume_audit(self.store, run["id"], provider=FakeProvider(), config=self.config)
        self.assertEqual(resumed["status"], "audited")
        self.assertEqual(self.store.get("runs", run["id"])["status"], "interrupted")

    def test_system_exit_during_resumed_audit_is_persisted_and_reraised(self):
        original = run_case(self.store, self.case["id"], provider=FakeProvider(), config=self.config)
        def interrupt(role, value):
            raise SystemExit(7)
        with self.assertRaises(SystemExit) as caught:
            resume_audit(self.store, original["id"], provider=FakeProvider(interrupt), config=self.config)
        self.assertEqual(caught.exception.code, 7)
        resumed = next(run for run in self.store.list("runs") if run["id"] != original["id"])
        self.assertEqual(resumed["status"], "interrupted")
        self.assertEqual(resumed["release_status"], "blocked")
        self.assertIn("finished_at", resumed)
        self.assertFalse(resumed["evaluation_eligible"])


if __name__=='__main__': unittest.main()
