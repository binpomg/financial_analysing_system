import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finresearch.collection import collect_once
from finresearch.ingestion_queue import collect_for_pipeline, mark_assigned
from finresearch.storage import Store, configuration


class ListingProvider:
    def __init__(self):
        self.calls = []

    def call(self, role, instructions, prompt, schema, **kwargs):
        self.calls.append({"role": role, "instructions": instructions})
        return {"items": [{"url": row["url"], "title": "低成本模型认为无关", "document_type": "irrelevant",
                           "metadata_note": "不要分析"} for row in json.loads(prompt)]}


class IngestionQueueTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "runtime")
        self.config = copy.deepcopy(configuration())
        self.config["sources"] = []
        self.provider = ListingProvider()
        self.index = 0

    def document(self, published_at="2026-09-10T00:00:00+08:00", group=None, incomplete=False):
        self.index += 1
        path = Path(self.temporary.name) / ("material-%d.md" % self.index)
        path.write_text("完整公告 %s" % self.index + (" ![缺少图片](missing.png)" if incomplete else ""), encoding="utf-8")
        return self.store.import_file(path, "https://example.org/%d.pdf" % self.index, group, published_at)

    def collection(self, *documents):
        identifier = "collection-%d" % len(self.store.list("collections"))
        return self.store.put("collections", identifier, {"id": identifier, "document_ids": [item["id"] for item in documents],
                              "created_at": "2026-09-10T00:00:00+00:00", "has_more": True})

    def source(self, suffix):
        return {"url": "https://example.org/" + suffix, "allowed_hosts": ["example.org"], "extensions": [".pdf"]}

    def test_existing_queue_is_reused_without_download_or_luna(self):
        first, second = self.document(), self.document()
        collection = self.collection(first, second)
        self.config["sources"] = [self.source("list")]
        with patch("finresearch.ingestion_queue.collect_once") as collect:
            result = collect_for_pipeline(self.store, config=self.config, provider=self.provider)
        collect.assert_not_called()
        self.assertEqual(len(result["document_ids"]), 1)
        self.assertEqual(result["collection_ids"], [collection["id"]])
        self.assertEqual(result["download_attempts"], 0)
        self.assertTrue(result["has_more"])
        self.assertEqual(self.provider.calls, [])
        chosen = result["document_ids"][0]
        self.store.put("pipelines", "pipeline-first", {"id": "pipeline-first", "document_ids": [chosen], "status": "failed"})
        mark_assigned(self.store, chosen, "pipeline-first")
        next_batch = collect_for_pipeline(self.store, config=self.config)
        self.assertEqual(len(next_batch["document_ids"]), 1)
        self.assertNotEqual(next_batch["document_ids"], result["document_ids"])

    def test_existing_case_or_claimed_group_cannot_be_reused(self):
        first, sibling, untouched = self.document(group="same-event"), self.document(group="same-event"), self.document()
        self.collection(first, sibling, untouched)
        self.store.create_case([first["id"]], "研究", "2026-09-11T00:00:00+08:00", partition="holdout")
        result = collect_for_pipeline(self.store, config=self.config)
        self.assertEqual(result["document_ids"], [untouched["id"]])
        reserved = next(item for item in result["blocked"] if item["document_id"] == sibling["id"])
        self.assertEqual(reserved["reasons"], ["document_group_already_used"])
        explicit = collect_for_pipeline(self.store, config=self.config, claimed_document_ids=[untouched["id"]])
        self.assertEqual(explicit["document_ids"], [])

    def test_missing_date_and_incomplete_source_remain_pending(self):
        undated, incomplete = self.document(published_at=None), self.document(incomplete=True)
        self.collection(undated, incomplete)
        result = collect_for_pipeline(self.store, config=self.config)
        self.assertEqual(result["document_ids"], [])
        self.assertEqual(result["status"], "needs_material_review")
        self.assertEqual(len(result["blocked"]), 2)
        self.assertEqual(self.store.list("cases"), [])
        undated["published_at"] = "2026-09-10T00:00:00+08:00"
        self.store.put("documents", undated["id"], undated, replace=True)
        repaired = collect_for_pipeline(self.store, config=self.config)
        self.assertEqual(repaired["document_ids"], [undated["id"]])

    def test_source_failure_and_empty_listing_do_not_block_next_source(self):
        self.config["sources"] = [self.source("broken"), self.source("empty"), self.source("good")]
        doc = self.document()
        def collect(store, source, **kwargs):
            self.assertIs(kwargs["config"], self.config)
            self.assertTrue(kwargs["reuse_pending_listing"])
            if source["url"].endswith("broken"):
                raise ValueError("目录格式发生变化")
            if source["url"].endswith("empty"):
                return {"status": "no_documents", "document_ids": [], "download_attempts": 0, "has_more": False}
            value = self.collection(doc)
            value.update(download_attempts=1, status="needs_case_review")
            return value
        with patch("finresearch.ingestion_queue.collect_once", side_effect=collect) as mocked:
            result = collect_for_pipeline(self.store, config=self.config)
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(result["document_ids"], [doc["id"]])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["source_index"], 0)
        self.assertEqual(self.store.get("ingestion_batches", result["id"])["errors"], result["errors"])

    def test_successful_first_source_does_not_starve_other_source_in_later_batches(self):
        self.config["sources"] = [self.source("contracts"), self.source("operating-data")]
        visited = []
        def collect(store, source, **kwargs):
            self.assertEqual(kwargs["limit"], 1)
            visited.append(source["url"])
            value = self.collection(self.document())
            value.update(download_attempts=1, status="needs_case_review")
            return value
        batches = []
        with patch("finresearch.ingestion_queue.collect_once", side_effect=collect):
            for index in range(3):
                # 新Store模拟下次进程启动；不能依赖进程内游标。
                batch = collect_for_pipeline(Store(self.store.root), config=self.config)
                batches.append(batch)
                self.store.put("pipelines", "pipeline-%d" % index,
                               {"id": "pipeline-%d" % index, "document_ids": batch["document_ids"]})
        self.assertEqual(visited, [self.config["sources"][index]["url"] for index in (0, 1, 0)])
        self.assertEqual([batch["source_start_index"] for batch in batches], [0, 1, 0])
        self.assertEqual(batches[1]["source_order"], [1, 0])
        self.assertEqual(batches[1]["source_scope"][0]["source_index"], 1)
        self.assertTrue(all(len(batch["document_ids"]) == batch["download_attempts"] == 1 for batch in batches))

    def test_existing_ready_queue_does_not_advance_source_rotation_and_disabled_source_is_skipped(self):
        self.config["sources"] = [self.source("first"), {**self.source("disabled"), "enabled": False}, self.source("third")]
        def collect(*args, **kwargs):
            value = self.collection(self.document())
            value.update(download_attempts=1, status="needs_case_review")
            return value
        with patch("finresearch.ingestion_queue.collect_once", side_effect=collect) as fetch:
            first = collect_for_pipeline(self.store, config=self.config)
            self.store.put("pipelines", "pipeline-first", {"id": "pipeline-first", "document_ids": first["document_ids"]})
            queued = self.document()
            self.collection(queued)
            from_queue = collect_for_pipeline(self.store, config=self.config)
            self.assertEqual(from_queue["document_ids"], [queued["id"]])
            self.assertFalse(from_queue["source_rotation_advanced"])
            self.assertIsNone(from_queue["source_start_index"])
            self.assertEqual(fetch.call_count, 1)
            self.store.put("pipelines", "pipeline-queued", {"id": "pipeline-queued", "document_ids": [queued["id"]]})
            second_fetch = collect_for_pipeline(self.store, config=self.config)
        self.assertEqual(second_fetch["source_start_index"], 2)
        self.assertEqual(second_fetch["source_order"], [2, 0, 1])
        self.assertEqual(fetch.call_args.args[1]["url"], self.config["sources"][2]["url"])

    def test_download_failure_exhausts_global_attempt_limit(self):
        self.config["sources"] = [self.source("first"), self.source("second")]
        failure = {"status": "no_documents", "document_ids": [], "download_attempts": 1,
                   "errors": [{"url": "https://example.org/a.pdf", "error": "HTTP 503"}], "has_more": True}
        with patch("finresearch.ingestion_queue.collect_once", return_value=failure) as collect:
            result = collect_for_pipeline(self.store, config=self.config)
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(result["download_attempts"], 1)
        self.assertEqual(result["source_scope"][1]["status"], "not_attempted")
        self.assertEqual(result["errors"][0]["stage"], "download")

    def test_incomplete_download_does_not_start_unbounded_replacement_fetches(self):
        self.config["sources"] = [self.source("first"), self.source("second")]
        doc = self.document(published_at=None)
        def collect(*args, **kwargs):
            value = self.collection(doc)
            value.update(download_attempts=1)
            return value
        with patch("finresearch.ingestion_queue.collect_once", side_effect=collect) as mocked:
            result = collect_for_pipeline(self.store, config=self.config)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(result["document_ids"], [])
        self.assertEqual(result["blocked"][0]["reasons"], ["missing_publication_time"])

    def test_assignment_requires_persistent_pipeline_claim(self):
        doc = self.document()
        self.collection(doc)
        self.store.put("pipelines", "pipeline-empty", {"id": "pipeline-empty", "document_ids": []})
        with self.assertRaisesRegex(ValueError, "先在自动批次"):
            mark_assigned(self.store, doc["id"], "pipeline-empty")

    def test_future_publication_is_blocked_before_pipeline_can_claim_it(self):
        doc = self.document(published_at="2099-09-10T00:00:00+08:00")
        self.collection(doc)
        result = collect_for_pipeline(self.store, config=self.config)
        self.assertEqual(result["document_ids"], [])
        self.assertEqual(result["blocked"][0]["reasons"], ["future_publication_time"])

    def test_source_and_download_failure_records_redact_actual_key(self):
        secret = "fake-sensitive-key-only-for-test"
        self.config["sources"] = [self.source("failing")]
        with patch("finresearch.collection.APIProvider.credentials", return_value=("https://relay.example", secret, "responses")), \
                patch("finresearch.ingestion_queue.collect_once", side_effect=ValueError("failed " + secret)):
            result = collect_for_pipeline(self.store, config=self.config)
        self.assertNotIn(secret, json.dumps(result))
        rows = [{"url": "https://example.org/new.pdf", "title": "公告"}]
        with patch("finresearch.collection.APIProvider.credentials", return_value=("https://relay.example", secret, "responses")), \
                patch("finresearch.collection.discover_links", return_value=rows), \
                patch("finresearch.collection.fetch_document", side_effect=RuntimeError("download error " + secret)):
            collected = collect_once(self.store, self.source("list"), provider=self.provider, config=self.config)
        self.assertNotIn(secret, json.dumps(collected))
        self.assertIn("[REDACTED]", collected["errors"][0]["error"])

    def test_interrupted_directory_does_not_leave_batch_as_collecting(self):
        self.config["sources"] = [self.source("interrupt")]
        with patch("finresearch.ingestion_queue.collect_once", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            collect_for_pipeline(self.store, config=self.config)
        self.assertEqual(self.store.list("ingestion_batches")[0]["status"], "interrupted")

    def test_import_completed_before_interruption_can_be_queued_without_redownload(self):
        rows = [{"url": "https://example.org/1.pdf", "title": "公告", "published_at": "2026-09-10T00:00:00+08:00"}]
        def interrupted_after_import(*args, **kwargs):
            self.document()
            raise KeyboardInterrupt
        with patch("finresearch.collection.discover_links", return_value=rows), \
                patch("finresearch.collection.fetch_document", side_effect=interrupted_after_import), self.assertRaises(KeyboardInterrupt):
            collect_once(self.store, self.source("list"), provider=self.provider, config=self.config, reuse_pending_listing=True)
        collection = self.store.list("collections")[0]
        self.assertEqual(collection["status"], "interrupted")
        self.assertEqual(collection["download_records"][0]["status"], "downloading")
        with patch("finresearch.ingestion_queue.collect_once") as fetch:
            result = collect_for_pipeline(self.store, config=self.config)
        fetch.assert_not_called()
        self.assertEqual(result["document_ids"], [self.store.list("documents")[0]["id"]])
        self.assertEqual(result["collection_ids"], [collection["id"]])

    def test_failed_download_is_not_retried_by_cached_listing(self):
        source = self.source("list")
        rows = [{"url": "https://example.org/new.pdf", "title": "公告"}]
        with patch("finresearch.collection.discover_links", return_value=rows), \
                patch("finresearch.collection.fetch_document", side_effect=RuntimeError("HTTP 503")) as download:
            first = collect_once(self.store, source, provider=self.provider, config=self.config, reuse_pending_listing=True)
            second = collect_once(self.store, source, provider=self.provider, config=self.config, reuse_pending_listing=True)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(first["download_attempts"], 1)
        self.assertEqual(second["download_attempts"], 0)

    def test_invalid_limits_are_rejected_before_collecting(self):
        for value in (0, -1, True, 1.5, 1001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                collect_for_pipeline(self.store, max_documents=value, config=self.config)

    def test_listing_continuation_uses_cached_luna_and_original_source_metadata(self):
        source = self.source("index")
        docs = [self.document(), self.document()]
        rows = [{"url": "https://example.org/new-%d.pdf" % i, "title": "公告%d" % i,
                 "published_at": "2026-09-10T00:00:00+08:00"} for i in range(2)]
        with patch("finresearch.collection.discover_links", return_value=rows) as discover, \
                patch("finresearch.collection.fetch_document", side_effect=docs) as fetch:
            first = collect_once(self.store, source, limit=1, provider=self.provider, config=self.config, reuse_pending_listing=True)
            second = collect_once(self.store, source, limit=1, provider=self.provider, config=self.config, reuse_pending_listing=True)
            third = collect_once(self.store, source, limit=1, provider=self.provider, config=self.config, reuse_pending_listing=True)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(discover.call_count, 2)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(first["pending_listing_count"], 1)
        self.assertTrue(second["listing_reused"])
        self.assertEqual(first["discovery_id"], second["discovery_id"])
        self.assertEqual(third["document_ids"], [])
        self.assertEqual(fetch.call_args_list[0].args[2], rows[0]["published_at"])
        self.assertFalse(first["coverage_complete"])
        self.assertIsNone(first["directory_has_more"])

    def test_source_config_reaches_luna_provider(self):
        self.config["models"]["discovery"]["model"] = "explicit-config-test"
        rows = [{"url": "https://example.org/new.pdf", "title": "目录"}]
        doc = self.document()
        with patch("finresearch.collection.discover_links", return_value=rows), \
                patch("finresearch.collection.APIProvider", return_value=self.provider) as provider, \
                patch("finresearch.collection.fetch_document", return_value=doc):
            result = collect_once(self.store, self.source("list"), provider=None, config=self.config)
        provider.assert_called_once_with(self.config)
        listing = self.store.get("discoveries", result["discovery_id"])
        self.assertEqual(listing["model"]["model"], "explicit-config-test")

    def test_cninfo_empty_is_persisted_without_luna_and_keeps_scope(self):
        source = {"adapter": "cninfo", "query": "中标", "allowed_hosts": ["static.cninfo.com.cn"]}
        raw = {"items": [], "source": "https://www.cninfo.com.cn/index", "has_more": True,
               "query": "中标", "start_date": "2026-09-08", "end_date": "2026-09-15", "pages_requested": 1}
        with patch("finresearch.cninfo.announcements", return_value=raw):
            result = collect_once(self.store, source, provider=self.provider, config=self.config)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(result["status"], "no_documents")
        self.assertTrue(result["has_more"])
        self.assertEqual(result["source_scope"]["start_date"], raw["start_date"])
        self.assertEqual(self.store.get("collections", result["id"]), result)


if __name__ == "__main__":
    unittest.main()
