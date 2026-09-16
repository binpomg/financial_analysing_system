"""自动批次 HTTP 边界；模拟作业而不下载原件或调用模型。"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from finresearch.storage import Store
from finresearch.web import ResearchServer


class PipelineWebTest(unittest.TestCase):
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

    def post(self, body, origin=None):
        request = urllib.request.Request(self.base + "/api/pipeline", json.dumps(body).encode(),
                                         {"Content-Type": "application/json", "Origin": origin or self.base})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 202)
            return json.load(response)

    def finished_job(self, identifier):
        for _ in range(100):
            job = next(item for item in self.server.job_list() if item["id"] == identifier)
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(.01)
        self.fail("本地模拟任务未结束")

    def test_new_batch_uses_persistent_queue_and_write_lock(self):
        def run(store, pipeline_id):
            self.assertIs(store, self.store)
            self.assertIsNone(pipeline_id)
            with self.assertRaisesRegex(RuntimeError, "写入任务"):
                with store.exclusive():
                    self.fail("自动批次必须持有写锁")
            return {"id": "pipeline-new", "status": "no_documents"}
        with patch("finresearch.web.run_pipeline_job", side_effect=run) as operation:
            job = self.finished_job(self.post({})["job_id"])
        operation.assert_called_once_with(self.store, None)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["status"], "no_documents")
        self.assertEqual(self.store.get("jobs", job["id"])["result"], job["result"])

    def test_scope_tampering_and_non_object_bodies_are_rejected_before_queue(self):
        for body in ({"max_documents": 2}, {"lines": ["extraction"]}, {"config": {}},
                     {"pipeline_id": None}, {"pipeline_id": ""}, [], "pipeline-x", None):
            with self.subTest(body=body):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.post(body)
                self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.server.job_list(), [])

    def test_only_interrupted_batch_can_be_resumed(self):
        for status in ("running", "completed", "completed_with_issues", "no_documents", "blocked", "failed"):
            self.store.put("pipelines", "pipeline-" + status,
                           {"id": "pipeline-" + status, "status": status})
            with self.subTest(status=status):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.post({"pipeline_id": "pipeline-" + status})
                self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.server.job_list(), [])
        self.store.put("pipelines", "pipeline-interrupted", {"id": "pipeline-interrupted", "status": "interrupted"})
        with patch("finresearch.web.run_pipeline_job", return_value={"id": "pipeline-interrupted", "status": "completed_with_issues"}) as operation:
            job = self.finished_job(self.post({"pipeline_id": "pipeline-interrupted"})["job_id"])
        operation.assert_called_once_with(self.store, "pipeline-interrupted")
        self.assertEqual(job["result"]["status"], "completed_with_issues")

    def test_state_distinguishes_feedback_candidates_and_adoptions(self):
        batch = {"id": "pipeline-a", "status": "completed_with_issues", "private_large_trace": "full original prompt",
                 "steps": [{"line": "extraction", "run_id": "run-a", "run_status": "audited",
                            "release_status": "requires_review", "feedback_status": "completed", "full_result": "large"}],
                 "candidate_attempts": [], "candidate_waiting": []}
        self.store.put("pipelines", batch["id"], batch)
        self.store.put("feedback", "feedback-human", {"id": "feedback-human", "note": "private feedback detail"})
        self.store.put("feedback", "feedback-model", {"id": "feedback-model", "verification_kind": "independent_model_check"})
        self.store.put("feedback", "feedback-unfinished", {"id": "feedback-unfinished", "verification_kind": "independent_model_check"})
        self.store.put("feedback_verifications", "verification-completed", {"id": "verification-completed", "status": "completed", "feedback_ids": ["feedback-model"]})
        self.store.put("feedback_verifications", "verification-unfinished", {"id": "verification-unfinished", "status": "interrupted", "feedback_ids": ["feedback-unfinished"]})
        self.store.put("candidates", "candidate-a", {"id": "candidate-a", "status": "candidate_unvalidated", "proposal": "large proposal"})
        state = self.get("/api/state")
        self.assertEqual(state["training_summary"], {"feedback_count": 3, "model_confirmed_feedback_count": 1,
                         "candidate_count": 1, "unvalidated_candidate_count": 1, "adoption_count": 0})
        self.assertEqual(state["pipelines"][0]["steps"][0]["release_status"], "requires_review")
        self.assertNotIn("private_large_trace", state["pipelines"][0])
        self.assertNotIn("full_result", state["pipelines"][0]["steps"][0])
        self.assertNotIn("private feedback detail", json.dumps(state))
        self.assertNotIn("large proposal", json.dumps(state))
        self.assertEqual(self.get("/api/pipeline?id=pipeline-a"), batch)

    def test_pipeline_host_boundary_and_unsafe_ids(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post({}, "https://outside.example")
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get("/api/pipeline?id=../../config")
        self.assertEqual(error.exception.code, 400)

    def test_run_shows_only_own_safe_verification_decisions_without_changing_audit(self):
        run = {"id": "run-reviewed", "status": "audited", "result": {"status": "needs_data"},
               "audit": {"verdict": "revise", "issues": [{"description": "原审核疑点"}]}}
        self.store.put("runs", run["id"], run)
        record = {"id": "verification-owned", "run_id": run["id"], "line": "valuation", "status": "completed",
                  "expert_calibrated": False, "feedback_ids": [], "remaining_issue_indices": [2],
                  "model_config": {"api_key": "must-not-be-exposed"}, "request": "private model prompt",
                  "decisions": [{"issue_index": 0, "verdict": "rejected", "reason": "原件已披露，原审核疑点不成立",
                                 "result_pointers": ["/records/0"], "internal_secret": "must-not-be-exposed",
                                 "evidence": [{"document_id": "doc-one", "unit_id": "p1", "quote": "披露原文",
                                               "api_key": "must-not-be-exposed"}]},
                                {"issue_index": 1, "verdict": "uncertain", "reason": "给定原件无法确认统计范围",
                                 "pending_reason": "需要补充口径说明", "evidence": []}]}
        self.store.put("feedback_verifications", record["id"], record)
        self.store.put("feedback_verifications", "verification-other", {"id": "verification-other", "run_id": "run-other",
                       "status": "completed", "decisions": [{"reason": "other-run-private"}]})
        response = self.get("/api/run?id=" + run["id"])
        self.assertEqual(response["audit"], run["audit"])
        self.assertEqual(response["effective_release_status"], "requires_review")
        self.assertEqual(len(response["feedback_verifications"]), 1)
        view = response["feedback_verifications"][0]
        self.assertEqual([item["verdict"] for item in view["decisions"]], ["rejected", "uncertain"])
        self.assertEqual(view["decisions"][0]["reason"], record["decisions"][0]["reason"])
        self.assertEqual(view["decisions"][1]["pending_reason"], "需要补充口径说明")
        self.assertEqual(view["decisions"][0]["evidence"], [{"document_id": "doc-one", "unit_id": "p1", "quote": "披露原文"}])
        self.assertTrue(all(not item["feedback_registered"] for item in view["decisions"]))
        self.assertNotIn("must-not-be-exposed", json.dumps(response))
        self.assertNotIn("private model prompt", json.dumps(response))
        self.assertNotIn("other-run-private", json.dumps(response))
        self.assertEqual(self.store.get("runs", run["id"]), run)
        self.assertEqual(self.store.get("feedback_verifications", record["id"]), record)

    def test_incomplete_verifications_cannot_claim_feedback_registration(self):
        run = {"id": "run-registration", "status": "failed"}
        self.store.put("runs", run["id"], run)
        self.store.put("feedback", "feedback-own", {"id": "feedback-own", "run_id": run["id"], "issue_index": 0})
        self.store.put("feedback", "feedback-other", {"id": "feedback-other", "run_id": "run-other", "issue_index": 0})
        for status in ("completed", "failed", "interrupted", "running", "registering_feedback"):
            record = {"id": "verification-" + status, "run_id": run["id"], "status": status,
                      "feedback_ids": ["feedback-own", "feedback-other", "feedback-missing"],
                      "decisions": [{"issue_index": 0, "verdict": "confirmed", "reason": "已保留的核实内容", "evidence": []},
                                    {"issue_index": 1, "verdict": "confirmed", "reason": "缺少登记记录", "evidence": []}]}
            self.store.put("feedback_verifications", record["id"], record)
        response = self.get("/api/run?id=" + run["id"])
        for record in response["feedback_verifications"]:
            with self.subTest(status=record["status"]):
                registered = record["status"] == "completed"
                self.assertEqual(record["decisions"][0]["feedback_registered"], registered)
                self.assertEqual(record["decisions"][0]["registered_feedback_ids"], ["feedback-own"] if registered else [])
                self.assertFalse(record["decisions"][1]["feedback_registered"])
        self.assertEqual(response["status"], "failed")

    def test_run_without_verification_returns_empty_list(self):
        self.store.put("runs", "run-no-verification", {"id": "run-no-verification", "status": "running"})
        self.assertEqual(self.get("/api/run?id=run-no-verification")["feedback_verifications"], [])


if __name__ == "__main__":
    unittest.main()
