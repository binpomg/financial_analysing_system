import os
import tempfile
import unittest
from unittest.mock import patch

from finresearch.lifecycle import owner_state, recover_interrupted
from finresearch.storage import Store, now
from finresearch.web import ResearchServer


class LifecycleTests(unittest.TestCase):
    def test_current_owner_is_alive_and_unknown_owner_is_not_dead(self):
        self.assertEqual(owner_state(os.getpid(), now()), "alive")
        self.assertEqual(owner_state(None, now()), "unknown")
        self.assertEqual(owner_state(-1, now()), "unknown")

    def test_recovery_preserves_checkpoint_and_never_overwrites_live_or_final(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            common = {"case_id": "case-1", "line": "extraction", "skill_version": "v1", "created_at": now(),
                      "worker_pid": 12345, "release_status": "pending", "result": {"summary": "已保存业务结果", "records": []}}
            for identifier, status in [("dead", "auditing"), ("live", "running"), ("unknown", "running"), ("done", "audited")]:
                store.put("runs", identifier, dict(common, id=identifier, status=status))
            store.put("jobs", "job-1", {"id": "job-1", "status": "queued", "worker_pid": 12345, "created_at": now()})
            with patch("finresearch.lifecycle.owner_state", side_effect=["dead", "alive", "unknown", "dead"]):
                recovered = recover_interrupted(store)
            self.assertEqual(recovered, ["dead", "job-1"])
            self.assertEqual(store.get("runs", "dead")["interrupted_stage"], "auditing")
            self.assertEqual(store.get("runs", "dead")["result"], common["result"])
            self.assertEqual(store.get("runs", "live")["status"], "running")
            self.assertEqual(store.get("runs", "unknown")["status"], "running")
            self.assertEqual(store.get("runs", "done")["status"], "audited")
            self.assertTrue((store.root / "outputs" / "dead" / "result.json").exists())

    def test_web_jobs_are_saved_and_visible_after_server_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            server = ResearchServer(("127.0.0.1", 0), store)
            try:
                job = server.submit(lambda: "saved-result")
                server.queue.shutdown(wait=True)
                persisted = store.get("jobs", job["job_id"])
                self.assertEqual(persisted["status"], "completed")
                self.assertEqual(persisted["result"], "saved-result")
            finally:
                server.server_close()
            restored = ResearchServer(("127.0.0.1", 0), store)
            try:
                self.assertEqual(restored.job_list()[0], persisted)
            finally:
                restored.server_close()
                restored.queue.shutdown(wait=True)

    def test_web_worker_system_exit_is_persisted_as_interrupted(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            server = ResearchServer(("127.0.0.1", 0), store)
            def interrupted():
                raise SystemExit(7)
            try:
                job = server.submit(interrupted)
                server.queue.shutdown(wait=True)
                persisted = store.get("jobs", job["job_id"])
                self.assertEqual(persisted["status"], "interrupted")
                self.assertIn("finished_at", persisted)
            finally:
                server.server_close()

    def test_orphan_experiment_is_marked_without_losing_completed_pairs(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.put("experiments", "trial-1", {"id": "trial-1", "status": "running", "worker_pid": 12345,
                                               "created_at": now(), "pairs": [{"baseline": "run-old", "candidate": "run-new"}]})
            with patch("finresearch.lifecycle.owner_state", return_value="dead"):
                self.assertEqual(recover_interrupted(store), ["trial-1"])
            saved = store.get("experiments", "trial-1")
            self.assertEqual(saved["status"], "interrupted")
            self.assertEqual(len(saved["pairs"]), 1)


if __name__ == "__main__":
    unittest.main()
