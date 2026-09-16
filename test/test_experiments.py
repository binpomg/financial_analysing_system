import copy
import os
import unittest
from unittest.mock import patch

from finresearch.evaluation import compare_batch
from finresearch.experiments import promote, run_experiment
from finresearch.storage import digest


class MemoryStore:
    def __init__(self):
        self.records = {
            "candidates": {"candidate-1": {"id": "candidate-1", "line": "extraction",
                "parent_version": "v1", "skill_version": "v2", "development_group_ids": ["development-group"]}},
            "cases": {"validation-1": {"id": "validation-1", "partition": "validation", "group_ids": ["new-group"]},
                      "regression-1": {"id": "regression-1", "partition": "regression", "group_ids": ["old-group"]}},
            "stable": {"extraction": {"skill_version": "v1"}},
        }

    def get(self, category, identifier):
        return copy.deepcopy(self.records[category][identifier])

    def list(self, category):
        return copy.deepcopy(list(self.records.get(category, {}).values()))

    def put(self, category, identifier, value, replace=False):
        target = self.records.setdefault(category, {})
        if identifier in target and not replace:
            raise ValueError("Existing record")
        target[identifier] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def snapshot(self, line):
        return {"id": self.records["stable"][line]["skill_version"]}


def policy(**overrides):
    # 此处的下限仅为测试输入，不是产品默认晋升政策。
    result = {"enabled": True, "min_validation_cases": 1, "min_regression_cases": 1, "min_improved_cases": 1}
    result.update(overrides)
    return {"promotion_policy": result}


def fake_run(store, case_id, line_id, candidate_id=None, provider=None, config=None):
    case = store.get("cases", case_id)
    version = "v2" if candidate_id else "v1"
    failed = case["partition"] == "validation" and not candidate_id
    record = {
        "id": version + "-" + case_id, "case_id": case_id, "line": line_id,
        "partition": case["partition"], "group_ids": case["group_ids"], "skill_version": version,
        "model": "gpt-6-astra", "reasoning_effort": "xhigh", "audit_model": {"model": "gpt-6-astra", "reasoning_effort": "xhigh"},
        "evaluation_version": "evaluation-v1", "auditor_version": "auditor-v1", "input_digest": "input-" + case_id,
        "status": "audited", "result": {"summary": "校验结果"},
        "audit": {"verdict": "revise" if failed else "pass", "issues": [],
                  "dimensions": [{"name": "可复算性", "judgment": "fail" if failed else "pass", "reason": "独立计算核验。"}],
                  "summary": "独立审核完成。"},
    }
    store.put("runs", record["id"], record)
    return record


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()

    def experiment(self, config=None):
        with patch("finresearch.experiments.run_case", side_effect=fake_run):
            return run_experiment(self.store, "candidate-1", ["validation-1"], ["regression-1"], config=config or policy())

    def test_policy_frozen_before_any_model_call(self):
        settings = policy()
        seen = []

        def observe(*args, **kwargs):
            registered = self.store.list("experiments")
            self.assertEqual(len(registered), 1)
            self.assertEqual(registered[0]["promotion_policy_digest"], digest(settings["promotion_policy"]))
            self.assertEqual(registered[0]["validation_case_ids"], ["validation-1"])
            self.assertEqual(registered[0]["worker_pid"], os.getpid())
            seen.append(kwargs["config"])
            return fake_run(*args, **kwargs)

        with patch("finresearch.experiments.run_case", side_effect=observe):
            experiment = run_experiment(self.store, "candidate-1", ["validation-1"], ["regression-1"], config=settings)
        settings["promotion_policy"]["min_improved_cases"] = 100
        self.assertEqual(experiment["promotion_policy"]["min_improved_cases"], 1)
        self.assertEqual(len(seen), 4)

    def test_default_disabled_cannot_be_overridden_by_reason(self):
        experiment = self.experiment({"promotion_policy": {"enabled": False}})
        with self.assertRaisesRegex(ValueError, "晋升政策"):
            promote(self.store, experiment["id"], "我认为已经足够成熟")
        self.assertEqual(self.store.snapshot("extraction")["id"], "v1")
        self.assertEqual(self.store.list("adoptions"), [])

    def test_absent_policy_is_frozen_as_disabled(self):
        with patch("finresearch.experiments.run_case", side_effect=fake_run):
            experiment = run_experiment(self.store, "candidate-1", ["validation-1"], ["regression-1"], config={})
        self.assertEqual(experiment["promotion_policy"], {"enabled": False})
        with self.assertRaisesRegex(ValueError, "晋升政策"):
            promote(self.store, experiment["id"], "结果出来以后再采用")

    def test_enabled_policy_requires_all_explicit_thresholds(self):
        for missing in ("min_validation_cases", "min_regression_cases", "min_improved_cases"):
            configuration = policy()
            del configuration["promotion_policy"][missing]
            with self.assertRaisesRegex(ValueError, missing):
                self.experiment(configuration)

    def test_policy_rejects_boolean_or_zero_threshold(self):
        for value in (True, 0, -1, "1"):
            with self.assertRaises(ValueError):
                self.experiment(policy(min_validation_cases=value))

    def test_unimplemented_threshold_is_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, "静默忽略"):
            self.experiment(policy(min_accuracy="0.99"))

    def test_other_line_candidate_data_cannot_be_new_validation(self):
        self.store.put("candidates", "other", {"id": "other", "line": "financial-report", "development_group_ids": ["new-group"]})
        with patch("finresearch.experiments.run_case") as call:
            with self.assertRaisesRegex(ValueError, "全库"):
                run_experiment(self.store, "candidate-1", ["validation-1"], ["regression-1"], config=policy())
        call.assert_not_called()

    def test_confirmed_feedback_exposes_group_before_candidate_creation(self):
        self.store.put("runs", "other-line-run", {"group_ids": ["new-group"], "line": "valuation"})
        self.store.put("feedback", "feedback", {"run_id": "other-line-run"})
        with self.assertRaisesRegex(ValueError, "全库"):
            self.experiment()

    def test_failed_prior_experiment_still_reserves_exposed_groups(self):
        self.store.put("experiments", "prior", {"status": "failed", "validation_group_ids": ["new-group"]})
        with self.assertRaisesRegex(ValueError, "全库"):
            self.experiment()

    def test_same_original_cannot_be_relabelled_as_regression(self):
        self.store.records["cases"]["regression-1"]["group_ids"] = ["new-group"]
        with self.assertRaisesRegex(ValueError, "共享原始资料"):
            self.experiment()

    def test_duplicate_case_does_not_inflate_sample_count(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            run_experiment(self.store, "candidate-1", ["validation-1", "validation-1"], ["regression-1"], config=policy())

    def test_incomplete_experiment_keeps_failure_record(self):
        with patch("finresearch.experiments.run_case", side_effect=RuntimeError("network failed")):
            with self.assertRaises(RuntimeError):
                run_experiment(self.store, "candidate-1", ["validation-1"], ["regression-1"], config=policy())
        experiment = self.store.list("experiments")[0]
        self.assertEqual(experiment["status"], "failed")
        self.assertIn("finished_at", experiment)
        self.assertEqual(experiment["validation_group_ids"], ["new-group"])

    def test_keyboard_interrupt_preserves_completed_pairs_and_reraises(self):
        count = [0]
        def interrupt_after_validation(*args, **kwargs):
            count[0] += 1
            if count[0] == 3:
                raise KeyboardInterrupt()
            return fake_run(*args, **kwargs)
        with patch("finresearch.experiments.run_case", side_effect=interrupt_after_validation):
            with self.assertRaises(KeyboardInterrupt):
                run_experiment(self.store, "candidate-1", ["validation-1"], ["regression-1"], config=policy())
        experiment = self.store.list("experiments")[0]
        self.assertEqual(experiment["status"], "interrupted")
        self.assertEqual(len(experiment["pairs"]), 1)
        self.assertEqual(experiment["regression_pairs"], [])
        self.assertEqual(experiment["worker_pid"], os.getpid())
        self.assertIn("finished_at", experiment)

    def test_system_exit_marks_experiment_interrupted_and_preserves_exit_code(self):
        with patch("finresearch.experiments.run_case", side_effect=SystemExit(9)):
            with self.assertRaises(SystemExit) as caught:
                run_experiment(self.store, "candidate-1", ["validation-1"], ["regression-1"], config=policy())
        self.assertEqual(caught.exception.code, 9)
        experiment = self.store.list("experiments")[0]
        self.assertEqual(experiment["status"], "interrupted")
        self.assertIn("finished_at", experiment)

    def test_clean_evidence_and_preregistered_policy_can_be_adopted(self):
        experiment = self.experiment()
        action = promote(self.store, experiment["id"], "已核验原件、公式及预登记全部案例")
        self.assertEqual(action["to_version"], "v2")
        self.assertEqual(self.store.snapshot("extraction")["id"], "v2")

    def test_insufficient_sample_count_blocks_adoption(self):
        for field in ("min_validation_cases", "min_regression_cases", "min_improved_cases"):
            self.store = MemoryStore()
            experiment = self.experiment(policy(**{field: 2}))
            with self.assertRaisesRegex(ValueError, "下限"):
                promote(self.store, experiment["id"], "主观认可")

    def test_policy_changed_after_results_is_rejected(self):
        experiment = self.experiment()
        self.store.records["experiments"][experiment["id"]]["promotion_policy"]["min_improved_cases"] = 2
        with self.assertRaisesRegex(ValueError, "指纹"):
            promote(self.store, experiment["id"], "修改后的政策")

    def test_removing_regression_pairs_cannot_be_bypassed_by_needs_review(self):
        experiment = self.experiment()
        saved = self.store.records["experiments"][experiment["id"]]
        saved["regression_pairs"] = []
        saved["comparison"] = compare_batch(saved["pairs"], [])
        with self.assertRaisesRegex(ValueError, "预登记"):
            promote(self.store, experiment["id"], "虽然回归缺失但感觉更好")

    def test_editing_paired_result_is_detected(self):
        experiment = self.experiment()
        self.store.records["experiments"][experiment["id"]]["pairs"][0]["candidate"]["audit"]["summary"] = "修改后的评分"
        with self.assertRaisesRegex(ValueError, "原始运行"):
            promote(self.store, experiment["id"], "接受修改后的评分")

    def test_saved_decision_is_recomputed_from_evidence(self):
        experiment = self.experiment()
        self.store.records["experiments"][experiment["id"]]["comparison"]["improved_cases"] = 1000
        with self.assertRaisesRegex(ValueError, "比较结果"):
            promote(self.store, experiment["id"], "修改数字不能提升")

    def test_unverified_resolved_error_cannot_be_promoted(self):
        experiment = self.experiment()
        saved = self.store.records["experiments"][experiment["id"]]
        before = saved["pairs"][0]["baseline"]
        before["audit"]["issues"] = [{"severity": "major", "category": "unit", "description": "单位错",
                                       "evidence": ["原件万元"], "correction": "换算为元"}]
        self.store.records["runs"][before["id"]] = copy.deepcopy(before)
        saved["comparison"] = compare_batch(saved["pairs"], saved["regression_pairs"])
        with self.assertRaisesRegex(ValueError, "不确定性"):
            promote(self.store, experiment["id"], "错误未再报告但未独立确认")

    def test_changed_stable_version_is_not_overwritten(self):
        experiment = self.experiment()
        self.store.records["stable"]["extraction"]["skill_version"] = "other-version"
        with self.assertRaisesRegex(ValueError, "基线"):
            promote(self.store, experiment["id"], "忽略稳定版变化")


if __name__ == "__main__":
    unittest.main()
