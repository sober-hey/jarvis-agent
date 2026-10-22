import importlib.util
import sys
import unittest
from unittest.mock import patch


class SearchTests(unittest.TestCase):
    def test_search_normalizes_results(self):
        import search

        self.assertTrue(callable(getattr(search, "search", None)))

        class Provider:
            def text(self, query, **kwargs):
                self.query, self.kwargs = query, kwargs
                return [
                    {"title": "天气", "href": "https://example.org", "body": "晴"},
                    {"title": 123, "href": None, "body": None},
                ]

        provider = Provider()
        with patch("ddgs.DDGS", return_value=provider) as factory:
            self.assertEqual(
                search.search("天水", 2),
                [
                    {"title": "天气", "url": "https://example.org", "snippet": "晴"},
                    {"title": "123", "url": "", "snippet": ""},
                ],
            )
        factory.assert_called_once_with(timeout=15)
        self.assertEqual(provider.query, "天水")
        self.assertEqual(provider.kwargs, {"max_results": 2})

    def test_search_retries_once_and_swallows_failure(self):
        import search

        class Provider:
            def text(self, query, **kwargs):
                raise RuntimeError("offline")

        with patch("ddgs.DDGS", return_value=Provider()) as factory:
            self.assertEqual(search.search("天水"), [])
        self.assertEqual(factory.call_count, 2)

    def test_search_timeout_bounds_each_attempt(self):
        import search
        import threading
        import time

        release = threading.Event()

        class Provider:
            def text(self, query, **kwargs):
                release.wait(1)
                return []

        started = time.monotonic()
        try:
            with (
                patch("ddgs.DDGS", return_value=Provider()) as factory,
                patch("search._TIMEOUT", 0.03),
            ):
                self.assertEqual(search.search("天水"), [])
                self.assertEqual(factory.call_count, 2)
            self.assertLess(time.monotonic() - started, 0.25)
        finally:
            release.set()

    def test_transient_failure_then_success(self):
        import search

        class Provider:
            def text(self, query, **kwargs):
                return [{"title": "恢复", "href": "https://example.org", "body": "结果"}]

        with patch("ddgs.DDGS", side_effect=[TimeoutError("slow"), Provider()]) as factory:
            self.assertEqual(search.search("天水")[0]["title"], "恢复")
        self.assertEqual(factory.call_count, 2)

    def test_invalid_inputs_do_not_touch_provider(self):
        import search

        with patch("ddgs.DDGS", side_effect=AssertionError("network forbidden")):
            self.assertEqual(search.search(""), [])
            self.assertEqual(search.search("天水", 0), [])
            self.assertEqual(search.search("天水", True), [])

    def test_search_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("search"))


class SelfEvaluationTests(unittest.TestCase):
    def evaluate(self):
        import importlib

        module = importlib.import_module("search")
        self.assertTrue(
            callable(getattr(module, "self_evaluate", None)),
            "component-owned self_evaluate hook is missing",
        )
        return module.self_evaluate()

    def test_metadata_schema_identity_and_utc_timestamp(self):
        from datetime import datetime, timezone

        before = datetime.now(timezone.utc)
        report = self.evaluate()
        self.assertEqual(
            set(report),
            {
                "module",
                "domain",
                "current_version",
                "known_limitations",
                "improvement_directions",
                "last_evaluated_at",
            },
        )
        self.assertEqual(report["module"], "search")
        self.assertIn("search", report["domain"].lower())
        for key in ("domain", "current_version"):
            self.assertIsInstance(report[key], str)
            self.assertTrue(report[key].strip())
        for key in ("known_limitations", "improvement_directions"):
            self.assertIsInstance(report[key], list)
            self.assertTrue(report[key])
            self.assertTrue(all(isinstance(x, str) and x.strip() for x in report[key]))
        stamp = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset().total_seconds(), 0)
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, datetime.now(timezone.utc))

    def test_metadata_never_reads_credentials_database_or_network(self):
        from unittest.mock import patch

        # Import first, then deny file, database and network boundaries. The
        # instance has no attributes, so even reading data_root would fail.
        import importlib

        importlib.import_module("search")
        with (
            patch("builtins.open", side_effect=AssertionError("file read forbidden")),
            patch("pathlib.Path.open", side_effect=AssertionError("path read forbidden")),
            patch("sqlite3.connect", side_effect=AssertionError("runtime DB forbidden")),
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        ):
            report = self.evaluate()
        import json

        self.assertIsInstance(json.dumps(report), str)

    def test_reports_do_not_share_mutable_metadata(self):
        first = self.evaluate()
        expected = self.evaluate()
        first["known_limitations"].clear()
        first["improvement_directions"].append("test-only mutation")
        fresh = self.evaluate()
        self.assertEqual(fresh["known_limitations"], expected["known_limitations"])
        self.assertEqual(fresh["improvement_directions"], expected["improvement_directions"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
