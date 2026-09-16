import copy
import unittest
from decimal import Decimal

from finresearch.evaluation import (
    canonical_digest, check_numeric, check_sum, compare_batch, compare_runs, validate_partitions,
)


def issue(severity="major", description="续表收入单位误用"):
    return {"severity": severity, "category": "unit", "description": description,
            "evidence": [{"document_id": "report", "page": 12, "quote": "单位：万元"}],
            "correction": "按万元换算并保留原始金额。"}


def run(version, case_id="new-case", partition="validation", failed=False):
    return {
        "id": version + "-" + case_id, "case_id": case_id, "line": "extraction", "partition": partition,
        "group_ids": ["group-" + case_id], "skill_version": version, "model": "gpt-6-astra",
        "reasoning_effort": "xhigh", "evaluation_version": "eval-v1", "auditor_version": "audit-v1",
        "input_digest": "digest-" + case_id, "status": "completed", "result": {"records": []},
        "audit": {"verdict": "revise" if failed else "pass", "issues": [issue()] if failed else [],
                  "dimensions": [{"name": "单位与数值", "judgment": "fail" if failed else "pass", "reason": "核对表头及原值。"},
                                 {"name": "覆盖", "judgment": "pass", "reason": "逐页核对记录。"}],
                  "summary": "已依据原件独立核验。"},
    }


def regression():
    return [{"baseline": run("v1", "old-case", "regression"),
             "candidate": run("v2", "old-case", "regression")}]


class ComparisonTests(unittest.TestCase):
    def test_improvement_never_automatically_promotes(self):
        before, after = run("v1", failed=True), run("v2")
        snapshot = copy.deepcopy((before, after))
        report = compare_runs(before, after, regression())
        self.assertEqual(report["decision"], "needs_review")
        self.assertFalse(report["promotion_allowed"])
        self.assertTrue(report["regressions"]["passed"])
        self.assertTrue(report["improvement_observed"])
        self.assertEqual((before, after), snapshot)

    def test_same_batch_and_frozen_settings_required(self):
        for field in ("case_id", "input_digest", "model", "reasoning_effort", "evaluation_version", "auditor_version", "group_ids", "partition"):
            with self.subTest(field=field):
                candidate = run("v2")
                candidate[field] = ["other"] if field == "group_ids" else "other"
                self.assertEqual(compare_runs(run("v1"), candidate)["decision"], "incomparable")

    def test_model_config_and_auditor_model_frozen_when_recorded(self):
        before, after = run("v1"), run("v2")
        before["model_config"] = {"temperature": "0"}
        after["model_config"] = {"temperature": "1"}
        self.assertEqual(compare_runs(before, after)["decision"], "incomparable")
        for field in ("audit_model", "material_digest"):
            before, after = run("v1"), run("v2")
            before[field], after[field] = "old", "new"
            self.assertEqual(compare_runs(before, after)["decision"], "incomparable")

    def test_new_major_error_cannot_be_averaged_away(self):
        before, after = run("v1", failed=True), run("v2")
        after["audit"]["issues"] = [issue("major", "把母公司现金余额写成合并现金余额")]
        report = compare_runs(before, after, regression())
        self.assertEqual(report["decision"], "keep_baseline")
        self.assertTrue(report["improvement_observed"])
        self.assertTrue(report["regression_detected"])

    def test_pass_to_uncertain_is_protected(self):
        after = run("v2")
        after["audit"]["dimensions"][1]["judgment"] = "uncertain"
        self.assertEqual(compare_runs(run("v1"), after)["decision"], "keep_baseline")

    def test_removed_dimension_and_empty_audit_fail_closed(self):
        after = run("v2")
        after["audit"]["dimensions"].pop()
        self.assertEqual(compare_runs(run("v1"), after)["decision"], "incomparable")
        after["audit"] = {}
        self.assertEqual(compare_runs(run("v1"), after)["decision"], "incomparable")

    def test_missing_issue_evidence_fails_closed(self):
        after = run("v2", failed=True)
        after["audit"]["issues"][0]["evidence"] = []
        self.assertEqual(compare_runs(run("v1"), after)["decision"], "incomparable")

    def test_no_improvement_keeps_stable(self):
        self.assertEqual(compare_runs(run("v1"), run("v2"), regression())["decision"], "keep_baseline")

    def test_missing_or_fabricated_regression_does_not_pass(self):
        for evidence in (None, [], [{"passed": True}], [{"baseline": run("v1"), "candidate": run("v2")} ]):
            with self.subTest(evidence=evidence):
                report = compare_runs(run("v1", failed=True), run("v2"), evidence)
                self.assertFalse(report["regressions"]["passed"])
                self.assertFalse(report["promotion_allowed"])

    def test_regression_failure_blocks_other_improvements(self):
        history = regression()
        history[0]["candidate"] = run("v2", "old-case", "regression", failed=True)
        report = compare_runs(run("v1", failed=True), run("v2"), history)
        self.assertEqual(report["decision"], "keep_baseline")

    def test_regression_must_test_same_candidate(self):
        history = regression()
        history[0]["candidate"]["skill_version"] = "other-v2"
        report = compare_runs(run("v1", failed=True), run("v2"), history)
        self.assertFalse(report["regressions"]["complete"])

    def test_corrupt_regression_identifiers_do_not_crash_or_pass(self):
        history = regression()
        history[0]["baseline"]["case_id"] = {}
        report = compare_runs(run("v1", failed=True), run("v2"), history)
        self.assertFalse(report["regressions"]["complete"])

    def test_relabeling_same_original_is_not_independent_regression(self):
        history = regression()
        for side in ("baseline", "candidate"):
            history[0][side]["group_ids"] = ["group-new-case"]
        result = compare_runs(run("v1", failed=True), run("v2"), history)
        self.assertFalse(result["regressions"]["complete"])

    def test_malformed_scalar_identifiers_fail_closed(self):
        candidate = run("v2")
        candidate["case_id"] = {"unexpected": "object"}
        self.assertEqual(compare_runs(run("v1"), candidate)["decision"], "incomparable")

    def test_batch_keeps_all_cases_and_blocks_regression(self):
        pairs = [{"baseline": run("v1", "a", failed=True), "candidate": run("v2", "a")},
                 {"baseline": run("v1", "b"), "candidate": run("v2", "b", failed=True)}]
        result = compare_batch(pairs, regression())
        self.assertEqual(result["case_count"], 2)
        self.assertEqual(result["improved_cases"], 1)
        self.assertEqual(result["regressed_cases"], 1)
        self.assertEqual(result["decision"], "keep_baseline")

    def test_batch_missing_and_duplicate_cases_are_not_dropped(self):
        pair = {"baseline": run("v1"), "candidate": run("v2")}
        self.assertEqual(compare_batch([pair, {}])["decision"], "incomparable")
        self.assertEqual(compare_batch([pair, pair])["decision"], "incomparable")

    def test_severity_escalation_blocks_candidate(self):
        before, after = run("v1", failed=True), run("v2", failed=True)
        before["audit"]["issues"][0]["severity"] = "minor"
        self.assertEqual(compare_runs(before, after)["decision"], "keep_baseline")


def case(identifier, partition, document_id, **metadata):
    meta = {"cutoff": "2026-09-14T16:00:00+08:00", "published_at": "2026-09-14T07:00:00Z"}
    meta.update(metadata)
    return {"id": identifier, "partition": partition, "line": "extraction",
            "document_ids": [document_id], "metadata": meta}


class PartitionTests(unittest.TestCase):
    def test_valid_dates_with_different_timezones(self):
        self.assertEqual(validate_partitions([case("one", "development", "doc")]), [])

    def test_original_shared_across_lines_leaks(self):
        first, second = case("a", "development", "doc"), case("b", "holdout", "doc")
        second["line"] = "valuation"
        self.assertTrue(any("盲测泄漏" in x for x in validate_partitions([first, second])))

    def test_revision_and_synthetic_parent_group_leaks(self):
        first = case("a", "train", "original", group_ids=["event-123"])
        second = case("b", "holdout", "degraded-scan", group_ids=["event-123"])
        self.assertTrue(any("group:event-123" in x for x in validate_partitions([first, second])))

    def test_source_group_leaks_even_if_document_ids_change(self):
        first = case("a", "regression", "doc1", source_groups=["source-1"])
        second = case("b", "test", "doc2", source_groups=["source-1"])
        self.assertTrue(any("source:source-1" in x for x in validate_partitions([first, second])))

    def test_two_frozen_holdout_lines_can_share_original(self):
        self.assertEqual(validate_partitions([case("a", "holdout", "doc"), case("b", "holdout", "doc")]), [])

    def test_future_information_is_detected_in_attachment(self):
        sample = case("a", "validation", "doc", documents=[{"id": "correction", "published_at": "2026-09-15T09:00:00+08:00"}])
        self.assertTrue(any("晚于" in x for x in validate_partitions([sample])))

    def test_naive_and_date_only_times_are_not_assigned_timezone(self):
        for value in ("2026-09-14", "2026-09-14T09:00:00"):
            errors = validate_partitions([case("a", "development", "doc", published_at=value)])
            self.assertTrue(any("时区" in x for x in errors))

    def test_unknown_publication_time_does_not_pass(self):
        self.assertTrue(any("published_at" in x for x in validate_partitions([case("a", "development", "doc", published_at=None)])))

    def test_case_date_cannot_hide_undated_attachment(self):
        sample = case("a", "validation", "doc", documents=[{"id": "attachment"}])
        self.assertTrue(any("attachment" in x and "published_at" in x for x in validate_partitions([sample])))

    def test_corrupt_case_identifiers_and_partitions_are_reported(self):
        sample = case("a", "validation", "doc")
        sample["id"], sample["partition"] = {}, []
        errors = validate_partitions([sample])
        self.assertTrue(any("标识" in x for x in errors))
        self.assertTrue(any("分区" in x for x in errors))


class FinancialNumericTests(unittest.TestCase):
    def test_ten_thousand_yuan_normalization(self):
        self.assertTrue(check_numeric("1250.25", "12502500", "万元", "元")["matches"])
        self.assertFalse(check_numeric("1250.25", "1250.25", "万元", "元")["matches"])

    def test_percentage_and_percentage_point_are_not_interchangeable(self):
        self.assertFalse(check_numeric("5", "5", "%", "百分点")["matches"])
        self.assertTrue(check_numeric("0.5", "50", "百分点", "bp")["matches"])
        self.assertTrue(check_numeric("12.5", "0.125", "%", "ratio")["matches"])

    def test_decimal_total_without_binary_rounding(self):
        self.assertTrue(check_sum([{"value": "0.1", "unit": "元"}, {"value": "0.2", "unit": "元"}],
                                  {"value": "0.3", "unit": "元"})["matches"])
        self.assertFalse(check_sum([{"value": "100", "unit": "万元"}, {"value": "0.01", "unit": "亿元"}],
                                   {"value": "1000001", "unit": "元"})["matches"])

    def test_large_and_tiny_values_preserve_small_difference(self):
        huge = "1000000000000000000000000000000000000000000000000000000000000"
        result = check_numeric(huge, huge + ".000001", "元", "元")
        self.assertFalse(result["matches"])
        self.assertEqual(Decimal(result["absolute_error"]), Decimal("0.000001"))

    def test_tolerance_is_explicit_and_in_base_units(self):
        self.assertFalse(check_numeric("1.000001", "1", "万元", "万元")["matches"])
        self.assertTrue(check_numeric("1.000001", "1", "万元", "万元", "0.01")["matches"])

    def test_float_nonfinite_and_unknown_units_are_rejected(self):
        for value in (0.1, True, "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                check_numeric(value, "1", "元", "元")
        with self.assertRaises(ValueError):
            check_numeric("1", "1", "美元", "元")

    def test_digest_is_order_independent_and_rejects_nan(self):
        self.assertEqual(canonical_digest({"甲": 1, "b": [2, 3]}), canonical_digest({"b": [2, 3], "甲": 1}))
        with self.assertRaises(ValueError):
            canonical_digest({"value": float("nan")})


if __name__ == "__main__":
    unittest.main()
