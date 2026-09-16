import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from finresearch.cninfo import announcements
from finresearch.collection import collect_once
from finresearch.storage import Store, configuration


class ListingProvider:
    def __init__(self):
        self.inputs = []

    def call(self, role, instructions, prompt, schema, **kwargs):
        rows = json.loads(prompt)
        self.inputs.append(rows)
        return {"items": [{"url": row["url"], "title": row["title"],
                           "document_type": "unknown", "metadata_note": "不按标题淘汰"} for row in rows]}


class CollectionWindowTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "runtime")
        self.config = copy.deepcopy(configuration())
        self.provider = ListingProvider()
        self.source = {"adapter": "cninfo", "query": "中标", "start_date": "2026-06-17", "end_date": "2026-09-15",
                       "page_size": 30, "max_pages": 10, "extensions": [".pdf"],
                       "allowed_hosts": ["www.cninfo.com.cn", "static.cninfo.com.cn"]}

    def raw(self, query="中标", count=0):
        return {"source": "https://www.cninfo.com.cn/new/hisAnnouncement/query", "query": query,
                "start_date": "2026-06-17", "end_date": "2026-09-15", "pages_requested": 10, "has_more": True,
                "items": [{"url": "https://static.cninfo.com.cn/%s-%d.pdf" % (query, index),
                           "title": "无明显相关标题%d" % index, "published_at": "2026-09-10T00:00:00+08:00"}
                          for index in range(count)]}

    def test_fixed_window_overrides_rolling_window_and_is_recorded(self):
        self.source["lookback_days"] = 7
        with patch("finresearch.cninfo.announcements", return_value=self.raw()) as listing:
            result = collect_once(self.store, self.source, config=self.config, provider=self.provider)
        listing.assert_called_once_with("中标", "2026-06-17", "2026-09-15", 30, 10)
        self.assertEqual(result["source_scope"]["start_date"], "2026-06-17")
        self.assertEqual(result["source_scope"]["end_date"], "2026-09-15")
        self.assertEqual(self.provider.inputs, [])

    def test_default_rolling_window_remains_local_date_based(self):
        self.source.pop("start_date")
        self.source.pop("end_date")
        with patch("finresearch.collection.datetime", wraps=datetime) as clock, \
                patch("finresearch.cninfo.announcements", return_value=self.raw()) as listing:
            clock.now.return_value = datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc)
            collect_once(self.store, self.source, config=self.config, provider=self.provider)
        listing.assert_called_once_with("中标", "2026-09-08", "2026-09-15", 30, 10)

    def test_incomplete_or_invalid_fixed_window_stops_before_network(self):
        for changes in ({"start_date": None}, {"end_date": None}, {"start_date": "2026-6-17"},
                        {"end_date": "2026-02-30"}, {"start_date": "2026-09-16"}, {"end_date": 20260915}):
            with self.subTest(changes=changes), patch("finresearch.cninfo.announcements") as listing, self.assertRaises(ValueError):
                collect_once(self.store, {**self.source, **changes}, config=self.config, provider=self.provider)
            listing.assert_not_called()

    def test_single_fixed_endpoint_is_rejected_without_guessing_the_other(self):
        self.source.pop("end_date")
        with patch("finresearch.cninfo.announcements") as listing, self.assertRaises(ValueError):
            collect_once(self.store, self.source, config=self.config, provider=self.provider)
        listing.assert_not_called()

    def test_invalid_rolling_days_stop_before_network(self):
        self.source.pop("start_date")
        self.source.pop("end_date")
        for value in (-1, True, 1.5, "90"):
            with self.subTest(value=value), patch("finresearch.cninfo.announcements") as listing, self.assertRaises(ValueError):
                collect_once(self.store, {**self.source, "lookback_days": value}, config=self.config, provider=self.provider)
            listing.assert_not_called()

    def test_100_downloads_reuse_two_complete_listings_without_title_filter(self):
        def listing(query, *args):
            return self.raw(query, 300)

        def fetch(store, url, published_at, **kwargs):
            self.assertEqual(published_at, "2026-09-10T00:00:00+08:00")
            return {"id": url.rsplit("/", 1)[-1]}

        batches = []
        with patch("finresearch.cninfo.announcements", side_effect=listing) as directory, \
                patch("finresearch.collection.fetch_document", side_effect=fetch) as download:
            for index in range(100):
                source = {**self.source, "query": "中标" if index % 2 == 0 else "经营数据"}
                batches.append(collect_once(Store(self.store.root), source, limit=1,
                                            config=self.config, provider=self.provider, reuse_pending_listing=True))
        self.assertEqual(directory.call_count, 2)
        self.assertEqual([len(rows) for rows in self.provider.inputs], [300, 300])
        self.assertEqual(download.call_count, 100)
        self.assertEqual(len({item["document_ids"][0] for item in batches}), 100)
        self.assertTrue(all(item["download_attempts"] == 1 for item in batches))
        self.assertTrue(all(item["listing_reused"] for item in batches[2:]))
        self.assertEqual(batches[-1]["pending_listing_count"], 250)
        self.assertTrue(batches[-1]["directory_has_more"])

    def test_cninfo_rejects_invalid_page_bounds_before_network(self):
        for args in ((True, 1), (30.0, 1), (31, 1), (30, True), (30, 1.5), (30, 21)):
            with self.subTest(args=args), patch("finresearch.cninfo._validate_url") as network, self.assertRaises(ValueError):
                announcements("中标", "2026-06-17", "2026-09-15", *args)
            network.assert_not_called()

    def test_cninfo_rejects_noncanonical_dates_before_network(self):
        for start, end in (("2026-6-17", "2026-09-15"), ("2026-06-17", "2026-9-15"), (None, "2026-09-15")):
            with self.subTest(start=start, end=end), patch("finresearch.cninfo._validate_url") as network, self.assertRaises(ValueError):
                announcements("中标", start, end)
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
