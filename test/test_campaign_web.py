"""连续训练的只读进度与延迟暂停边界，不请求模型。"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from finresearch.storage import Store
from finresearch.web import ResearchServer, run_pipeline_job


class CampaignWebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.server = ResearchServer(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.queue.shutdown(wait=True)
        self.temp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return json.load(response)

    def post(self, path, body, origin=None):
        request = urllib.request.Request(self.base + path, json.dumps(body).encode(),
                                         {"Content-Type": "application/json", "Origin": origin or self.base})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 202)
            return json.load(response)

    def campaign(self, status="running"):
        record = {"id": "campaign-one", "status": status, "target_documents": 100,
                  "created_at": "2026-09-15T02:00:00+00:00", "document_ids": ["doc-one"],
                  "pipelines": [{"pipeline_id": "pipeline-one", "status": "running"}],
                  "current_pipeline_id": "pipeline-one", "stop_requested": False,
                  "worker_pid": 123, "private_config": {"api_key": "must-not-appear-in-summary"},
                  "progress": {"processed_documents": 0, "audited_runs": 0, "failed_runs": 0,
                               "feedback_count": 2, "candidate_count": 1, "private_note": "private-context"}}
        return self.store.put("campaigns", record["id"], record)

    def test_summary_distinguishes_material_attempts_from_audit_and_omits_private_fields(self):
        record = self.campaign()
        self.store.put("pipelines", "pipeline-one", {"id": "pipeline-one", "document_ids": ["doc-one"],
                       "steps": [{"run_status": "audited", "release_status": "requires_review"},
                                 {"run_status": "failed"}, {"status": "running"}]})
        result = self.get("/api/campaigns")[0]
        self.assertEqual(result["progress"], {"processed_documents": 0, "attempted_documents": 1,
                         "audited_runs": 1, "failed_runs": 1, "feedback_count": 2, "candidate_count": 1})
        self.assertEqual(result["target_documents"], 100)
        self.assertNotIn("must-not-appear-in-summary", json.dumps(result))
        self.assertNotIn("private-context", json.dumps(result))
        self.assertNotIn("worker_pid", result)
        self.assertEqual(self.get("/api/state")["campaigns"], [result])
        self.assertEqual(self.get("/api/campaign?id=campaign-one"), record)

    def test_current_material_is_counted_once_and_completed_count_is_not_inferred(self):
        record = self.campaign()
        record["document_ids"] = ["doc-one", "doc-one"]
        record["progress"]["processed_documents"] = 0
        self.store.put("campaigns", record["id"], record, replace=True)
        self.store.put("pipelines", "pipeline-one", {"id": "pipeline-one", "document_ids": ["doc-one", "doc-two"],
                       "steps": [{"run_status": "audited"}] * 7})
        result = self.get("/api/campaigns")[0]
        self.assertEqual(result["progress"]["attempted_documents"], 2)
        self.assertEqual(result["progress"]["processed_documents"], 0)
        self.assertEqual(result["progress"]["audited_runs"], 7)

    def test_active_campaign_blocks_new_batch_and_resumption_before_queue(self):
        self.campaign()
        self.store.put("pipelines", "pipeline-paused", {"id": "pipeline-paused", "status": "interrupted"})
        for body in ({}, {"pipeline_id": "pipeline-paused"}):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as error:
                self.post("/api/pipeline", body)
            self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.server.job_list(), [])

    def test_worker_rechecks_active_campaign_before_calling_pipeline(self):
        self.campaign()
        with patch("finresearch.pipeline.run_pipeline") as operation:
            with self.assertRaisesRegex(ValueError, "连续训练"):
                run_pipeline_job(self.store)
        operation.assert_not_called()

    def test_pause_control_does_not_wait_for_writer_or_modify_campaign(self):
        record = self.campaign()
        with self.store.exclusive():
            request = self.post("/api/campaign/stop", {"campaign_id": record["id"]})
            repeated = self.post("/api/campaign/stop", {"campaign_id": record["id"]})
        self.assertEqual(request, repeated)
        self.assertTrue(request["stop_requested"])
        self.assertEqual(request["campaign_id"], record["id"])
        self.assertEqual(self.store.get("campaigns", record["id"]), record)
        self.assertEqual(self.store.get("campaign_stop_requests", record["id"]), request)
        self.assertTrue(self.get("/api/campaigns")[0]["stop_requested"])
        self.assertEqual(self.server.job_list(), [])

    def test_pause_requires_active_existing_campaign(self):
        record = self.campaign("completed")
        for identifier in (record["id"], "campaign-missing"):
            with self.subTest(identifier=identifier), self.assertRaises(urllib.error.HTTPError) as error:
                self.post("/api/campaign/stop", {"campaign_id": identifier})
            self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.store.list("campaign_stop_requests"), [])

    def test_control_rejects_unsafe_identifiers_extra_fields_and_wrong_origin(self):
        record = self.campaign()
        for body in ({"campaign_id": "../../config"}, {"campaign_id": record["id"], "target_documents": 1000},
                     {"campaign_id": None}, {}, [], "campaign-one"):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as error:
                self.post("/api/campaign/stop", body)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post("/api/campaign/stop", {"campaign_id": record["id"]}, "https://outside.example")
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get("/api/campaign?id=../../config")
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.store.list("campaign_stop_requests"), [])

    def test_completed_campaign_does_not_prevent_on_demand_batch(self):
        self.campaign("completed")
        with patch("finresearch.pipeline.run_pipeline", return_value={"id": "pipeline-new", "status": "completed"}) as operation:
            result = run_pipeline_job(self.store)
        operation.assert_called_once_with(self.store, pipeline_id=None)
        self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main()
