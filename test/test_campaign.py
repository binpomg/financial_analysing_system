"""有限100份调度与中断边界；模拟Provider不作为业务质量证据。"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finresearch.campaign import campaign_configuration, create_campaign, run_campaign
from finresearch.pipeline import run_pipeline
from finresearch.storage import Store, configuration, now
from test.test_pipeline import PipelineProvider


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.config = configuration()
        self.provider = PipelineProvider()

    def tearDown(self):
        self.temp.cleanup()

    def document(self, name):
        path = self.root / (name + ".md")
        path.write_text(name + "：营业收入100万元。程序测试夹具。", encoding="utf-8")
        doc = self.store.import_file(path, "https://example.org/" + name, name, "2026-01-01T00:00:00+08:00")
        self.store.put("collections", "collection-" + name, {"id": "collection-" + name, "document_ids": [doc["id"]], "created_at": now()})
        return doc

    def create(self, count=2):
        return create_campaign(self.store, count, self.config, self.provider)

    def run_job(self, identifier):
        return run_campaign(self.store, identifier, self.provider, self.config)

    def fake_batch(self, *, failed=False, no_document=False):
        def run(store, pipeline_id=None, provider=None, config=None, new_pipeline_id=None, campaign_id=None):
            if pipeline_id:
                return store.get("pipelines", pipeline_id)
            identifier = new_pipeline_id
            doc = "doc-" + identifier
            steps = []
            if not no_document:
                for line in config["default_enabled_lines"]:
                    run_id = identifier + "-" + line
                    record = {"id": run_id, "status": "failed" if failed else "audited",
                              "release_status": "blocked" if failed else "needs_data",
                              "audit": None if failed else {"verdict": "pass"}}
                    store.put("runs", run_id, record)
                    steps.append({"line": line, "run_id": run_id, "status": "done", "run_status": record["status"]})
            batch = {"id": identifier, "campaign_id": campaign_id,
                     "status": "no_documents" if no_document else "completed_with_issues" if failed else "completed",
                     "document_ids": [] if no_document else [doc], "steps": steps,
                     "candidate_attempts": [], "intake": {"download_attempts": 0 if no_document else 1},
                     "finished_at": now()}
            store.put("pipelines", identifier, batch)
            return batch
        return run

    def test_exactly_100_new_documents_without_101st_and_completed_is_idempotent(self):
        job = self.create(100)
        with patch("finresearch.campaign.run_pipeline", side_effect=self.fake_batch()) as runner:
            final = self.run_job(job["id"])
            again = self.run_job(job["id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["progress"]["processed_documents"], 100)
        self.assertEqual(final["progress"]["audited_runs"], 700)
        self.assertEqual(len(set(final["document_ids"])), 100)
        self.assertEqual(runner.call_count, 100)
        self.assertEqual(again, final)
        self.assertFalse(final["automatic_promotion"])

    def test_two_real_local_cases_execute_fourteen_business_and_fourteen_audits(self):
        originals = {self.document(name)["id"] for name in ("one", "two")}
        job = self.create()
        final = self.run_job(job["id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(set(final["document_ids"]), originals)
        self.assertEqual(final["progress"]["processed_documents"], 2)
        self.assertEqual(self.provider.business_count, 14)
        self.assertEqual(len(self.provider.calls), 28)
        self.assertTrue(all(b["campaign_id"] == job["id"] for b in self.store.list("pipelines")))

    def test_previous_material_is_not_counted_or_reprocessed(self):
        old = self.document("old")
        prior = run_pipeline(self.store, provider=self.provider, config=self.config)
        new = self.document("new")
        job = self.create(1)
        final = self.run_job(job["id"])
        self.assertEqual(final["document_ids"], [new["id"]])
        self.assertNotIn(old["id"], final["document_ids"])
        self.assertNotIn(prior["id"], [x["pipeline_id"] for x in final["pipelines"]])
        self.assertEqual(self.provider.business_count, 14)

    def test_stop_request_waits_for_current_material_and_no_next_document(self):
        job = self.create()
        actual = self.fake_batch()
        def stop_after(*args, **kwargs):
            result = actual(*args, **kwargs)
            self.store.put("campaign_stop_requests", job["id"], {"id": job["id"], "stop_requested": True})
            return result
        with patch("finresearch.campaign.run_pipeline", side_effect=stop_after) as runner:
            final = self.run_job(job["id"])
        self.assertEqual(final["status"], "paused")
        self.assertTrue(final["stop_requested"])
        self.assertEqual(final["progress"]["processed_documents"], 1)
        self.assertEqual(final["progress"]["audited_runs"], 7)
        self.assertEqual(runner.call_count, 1)

    def test_empty_source_is_not_success_and_does_not_loop(self):
        job = self.create(100)
        with patch("finresearch.campaign.run_pipeline", side_effect=self.fake_batch(no_document=True)) as runner:
            final = self.run_job(job["id"])
        self.assertEqual(final["status"], "source_exhausted")
        self.assertEqual(final["progress"]["processed_documents"], 0)
        self.assertEqual(runner.call_count, 1)

    def test_feedback_or_candidate_failure_is_not_hidden_by_successful_audits(self):
        job = self.create(1)
        actual = self.fake_batch()
        def feedback_failed(*args, **kwargs):
            batch = actual(*args, **kwargs)
            batch["status"] = "completed_with_issues"
            self.store.put("pipelines", batch["id"], batch, replace=True)
            return batch
        with patch("finresearch.campaign.run_pipeline", side_effect=feedback_failed):
            final = self.run_job(job["id"])
        self.assertEqual(final["status"], "completed_with_issues")
        self.assertEqual(final["progress"]["audited_runs"], 7)
        self.assertEqual(final["progress"]["failed_runs"], 0)

    def test_worker_preflight_failure_always_leaves_exit_record(self):
        from finresearch.campaign_worker import main
        job = self.create(1)
        args = ["worker", job["id"], "--data-dir", str(self.store.root)]
        with patch("sys.argv", args), patch("finresearch.campaign.run_campaign", side_effect=ValueError("changed environment")):
            self.assertEqual(main(), 2)
        import json
        files = list((self.store.root / "campaign_logs" / job["id"]).glob("*.worker.json"))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["exit_code"], 2)
        self.assertIn("finished_at", record)
        self.assertEqual(self.store.get("campaigns", job["id"])["status"], "prepared")

    def test_three_fully_failed_materials_pause_without_retrying_any(self):
        job = self.create(100)
        with patch("finresearch.campaign.run_pipeline", side_effect=self.fake_batch(failed=True)) as runner:
            final = self.run_job(job["id"])
        self.assertEqual(final["status"], "paused")
        self.assertEqual(final["progress"]["processed_documents"], 3)
        self.assertEqual(final["progress"]["audited_runs"], 0)
        self.assertEqual(final["progress"]["failed_runs"], 21)
        self.assertEqual(runner.call_count, 3)

    def test_completed_child_before_campaign_bookkeeping_recovers_without_calling_again(self):
        job = self.create(2)
        actual = self.fake_batch()
        def interrupted(*args, **kwargs):
            actual(*args, **kwargs)
            raise KeyboardInterrupt()
        with patch("finresearch.campaign.run_pipeline", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.run_job(job["id"])
        original = self.store.list("pipelines")[0]
        with patch("finresearch.campaign.run_pipeline", side_effect=actual) as runner:
            final = self.run_job(job["id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(len(self.store.list("pipelines")), 2)
        self.assertEqual(self.store.get("pipelines", original["id"]), original)
        self.assertEqual(runner.call_count, 2)  # one reads old terminal; only one creates a new child

    def test_reserved_child_before_creation_is_reused(self):
        job = self.create(1)
        with patch("finresearch.campaign.run_pipeline", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_job(job["id"])
        pending = self.store.get("campaigns", job["id"])["pipelines"][0]["pipeline_id"]
        with patch("finresearch.campaign.run_pipeline", side_effect=self.fake_batch()):
            final = self.run_job(job["id"])
        self.assertEqual([p["pipeline_id"] for p in final["pipelines"]], [pending])
        self.assertEqual(final["status"], "completed")

    def test_real_pipeline_interruption_resumes_unattempted_lines_only(self):
        self.document("interrupt")
        job = self.create(1)
        self.provider.interrupt_business = 2
        with self.assertRaises(KeyboardInterrupt):
            self.run_job(job["id"])
        final = self.run_job(job["id"])
        self.assertEqual(final["status"], "completed_with_issues")
        self.assertEqual(self.provider.business_count, 7)
        self.assertEqual(len(self.store.list("cases")), 1)
        self.assertEqual(final["progress"]["failed_runs"], 1)

    def test_configuration_change_blocks_resume_before_api(self):
        job = self.create(1)
        self.config["reading"]["max_images"] = 399
        with self.assertRaisesRegex(ValueError, "配置已变化"):
            self.run_job(job["id"])
        self.assertEqual(self.provider.calls, [])

    def test_live_owner_and_foreign_child_are_not_adopted(self):
        job = self.create(1)
        with patch("finresearch.campaign.run_pipeline", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_job(job["id"])
        pending = self.store.get("campaigns", job["id"])
        pending["status"] = "running"
        self.store.put("campaigns", job["id"], pending, replace=True)
        with self.assertRaisesRegex(ValueError, "重复"):
            self.run_job(job["id"])
        pending["status"] = "interrupted"
        self.store.put("campaigns", job["id"], pending, replace=True)
        child = pending["pipelines"][0]["pipeline_id"]
        self.store.put("pipelines", child, {"id": child, "campaign_id": "campaign-other", "document_ids": []})
        with self.assertRaisesRegex(ValueError, "归属"):
            self.run_job(job["id"])
        self.assertEqual(self.provider.calls, [])

    def test_fixed_dates_models_and_no_secret_snapshot_fields(self):
        original = copy.deepcopy(self.config)
        cfg = campaign_configuration(self.config, "2026-09-15")
        self.assertEqual(self.config, original)
        self.assertEqual([s["start_date"] for s in cfg["sources"]], ["2026-06-17"] * 2)
        self.assertEqual([s["end_date"] for s in cfg["sources"]], ["2026-09-15"] * 2)
        self.assertEqual(cfg["models"], original["models"])
        self.config["api"]["api_key"] = "fixture-secret-must-not-persist"
        with self.assertRaisesRegex(ValueError, "快照"):
            self.create(1)
        self.assertFalse(self.store.list("campaigns"))

    def test_count_limit_and_promotion_guard(self):
        for value in (0, 101, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.create(value)
        self.config["promotion_policy"]["enabled"] = True
        with self.assertRaisesRegex(ValueError, "晋升"):
            self.create(1)

    def test_second_prepared_campaign_is_rejected(self):
        original = self.create(100)
        with self.assertRaisesRegex(ValueError, "重复"):
            self.create(100)
        self.assertEqual([job["id"] for job in self.store.list("campaigns")], [original["id"]])


if __name__ == "__main__":
    unittest.main()
