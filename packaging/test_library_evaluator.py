import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ARTIFACTS = Path("/Volumes/扩展硬盘/hermes/.hermes/cache/scratch/wave2-cognition/research-tests")
ARTIFACTS.mkdir(parents=True, exist_ok=True)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ARTIFACTS)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def module(self):
        try:
            return importlib.import_module("library_evaluator")
        except ModuleNotFoundError:
            self.fail("library_evaluator product module is not implemented")

    def test_eco_real_observations_and_tunable_thresholds(self):
        m = self.module()
        self.assertTrue(hasattr(m.LibraryEvaluator, "eco_score"), "ecosystem layer is missing")
        from datetime import datetime, timezone

        now = datetime(2026, 10, 18, tzinfo=timezone.utc).timestamp()
        calls = []

        def http(url, headers, timeout):
            calls.append(url)
            if "/commits?" in url:
                payload = [{"commit": {"committer": {"date": "2025-10-18T00:00:00Z"}}}]
            elif "/issues?" in url:
                payload = [
                    {"state": "closed"},
                    {"state": "open"},
                    {"state": "closed", "pull_request": {}},
                ]
            elif "/releases?" in url:
                payload = [
                    {"published_at": "2026-10-01T00:00:00Z", "draft": False, "prerelease": False},
                    {"published_at": "2026-09-01T00:00:00Z"},
                    {"published_at": "2020-01-01T00:00:00Z"},
                    {"published_at": "2026-10-02T00:00:00Z", "draft": True},
                ]
            else:
                payload = {
                    "stargazers_count": 5000,
                    "forks_count": 500,
                    "pushed_at": "2026-10-18T00:00:00Z",
                }
            return 200, {}, payload

        ev = m.LibraryEvaluator(
            self.root, http=http, clock=lambda: now, github_key_file=self.root / "absent.key"
        )
        self.assertAlmostEqual(
            ev.eco_score("https://github.com/owner/repo.git"),
            0.35 * 0.5 + 0.25 * 0.5 + 0.20 * 0 + 0.10 * 0.5 + 0.10 * (2 / 12),
        )
        self.assertEqual(len(calls), 4)
        self.assertEqual(ev.last_eco_details["observations"]["issue_close_rate"], 0.5)
        config = json.loads(ev.config_path.read_text())
        config["weights"] = dict(
            stars=1, forks=0, recency_days=0, issue_close_rate=0, release_freq=0
        )
        config["thresholds"]["stars"] = 5000
        ev.config_path.write_text(json.dumps(config))
        self.assertEqual(ev.eco_score("https://github.com/owner/repo"), 1)
        self.assertEqual(json.loads(ev.config_path.read_text()), config)

    def test_rate_limit_and_token_are_safe_nonblocking(self):
        m = self.module()
        key = self.root / "github.key"
        key.write_text("unit-test-github-token")
        key.chmod(0o600)
        now = [1000.0]
        calls = []

        def http(url, headers, timeout):
            calls.append((url, dict(headers)))
            return (
                429,
                {"Retry-After": "600", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1400"},
                None,
            )

        ev = m.LibraryEvaluator(self.root, http=http, clock=lambda: now[0], github_key_file=key)
        self.assertEqual(ev.eco_score("https://github.com/a/b"), 0)
        self.assertEqual(len(calls), 1, "must stop immediately at 429")
        self.assertEqual(calls[0][1]["Authorization"], "Bearer unit-test-github-token")
        self.assertNotIn(key.read_text(), json.dumps(ev.last_eco_details))
        self.assertEqual(ev.eco_score("https://github.com/a/b"), 0)
        self.assertEqual(len(calls), 1)
        now[0] = 1601
        ev.eco_score("https://github.com/a/b")
        self.assertEqual(len(calls), 2)

    def test_remaining_zero_blocks_and_recovers_at_reset(self):
        m = self.module()
        now = [1000.0]
        calls = []

        def http(*args):
            calls.append(args)
            return (
                200,
                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"},
                {"stargazers_count": 10000, "forks_count": 1000},
            )

        ev = m.LibraryEvaluator(
            self.root, http=http, clock=lambda: now[0], github_key_file=self.root / "absent"
        )
        self.assertAlmostEqual(ev.eco_score("https://github.com/a/b"), 0.6)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("Authorization", calls[0][1])
        now[0] = 1101
        ev.eco_score("https://github.com/a/b")
        self.assertEqual(len(calls), 2)

    def test_invalid_urls_fail_before_http_or_key_access(self):
        m = self.module()
        ev = m.LibraryEvaluator(self.root, http=lambda *a: self.fail("HTTP forbidden"))
        for url in [
            "http://github.com/a/b",
            "https://github.com.evil/a/b",
            "https://github.com@evil/a/b",
            "https://user:pass@github.com/a/b",
            "https://127.0.0.1/a/b",
            "https://github.com:443/a/b",
            "https://github.com/a/b/tree/main",
            "https://github.com/a/%2e%2e",
            "https://github.com/a/..",
            "https://github.com/a/b?x=1",
            "https://github.com/a/b#readme",
            "https://github.com/a/b\n",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                ev.eco_score(url)

    def test_transport_does_not_follow_redirect_with_token(self):
        m = self.module()
        from urllib.error import HTTPError

        request = m.Request(
            "https://api.github.com/repos/a/b", headers={"Authorization": "Bearer test-token"}
        )
        handler = m._NoRedirect()
        self.assertIsNone(
            handler.redirect_request(request, None, 302, "", {}, "https://evil.example/")
        )

        def fail(*args, **kwargs):
            raise HTTPError(
                request.full_url, 302, "Redirect", {"Location": "https://evil.example/"}, None
            )

        with patch.object(m, "build_opener") as opener:
            opener.return_value.open.side_effect = fail
            self.assertEqual(
                m._http_get(request.full_url, {"Authorization": "Bearer test-token"}, 1)[0], 302
            )
            self.assertIsInstance(opener.call_args.args[0], m._NoRedirect)

    def test_http_auth_failure_and_exception_do_not_invent_observations(self):
        m = self.module()
        for status in (401, 403, 404, 500):
            calls = []

            def http(*args):
                calls.append(args)
                return status, {}, None

            ev = m.LibraryEvaluator(self.root, http=http, github_key_file=self.root / "absent")
            self.assertEqual(ev.eco_score("https://github.com/a/b"), 0)
            self.assertTrue(all(x is None for x in ev.last_eco_details["observations"].values()))

        def broken(*args):
            raise RuntimeError("unit-test-secret-not-for-logs")

        ev = m.LibraryEvaluator(self.root, http=broken, github_key_file=self.root / "absent")
        self.assertEqual(ev.eco_score("https://github.com/a/b"), 0)
        self.assertNotIn("unit-test-secret", json.dumps(ev.last_eco_details))

    def test_content_is_model_review_with_search_citations(self):
        m = self.module()
        self.assertTrue(hasattr(m.LibraryEvaluator, "content_score"), "content model layer missing")
        prompts = []
        queries = []
        reviews = [
            {
                "title": "External evaluation",
                "url": "https://example.com/review",
                "snippet": "Independent review evidence",
            }
        ]

        def reviewer(prompt):
            prompts.append(prompt)
            return {
                "text": json.dumps(
                    {
                        "readme_completeness": 0.8,
                        "test_coverage": 0.2,
                        "documentation": 0.6,
                        "code_organization": 0.4,
                        "reason": "Coverage is unverified; README only.",
                    }
                )
            }

        def search(query, max_results):
            queries.append(query)
            return reviews

        ev = m.LibraryEvaluator(self.root, model=reviewer, search=search)
        self.assertFalse(prompts)
        self.assertFalse(queries)
        self.assertAlmostEqual(
            ev.content_score("https://github.com/a/b", "# b\nTests documented but not executed"),
            0.5,
        )
        for phrase in [
            "readme_completeness",
            "test_coverage",
            "documentation",
            "code_organization",
            "Independent review evidence",
            "Tests documented",
        ]:
            self.assertIn(phrase, prompts[0])
        self.assertEqual(len(queries), 1)
        from citations import CitationStore

        citation = CitationStore(self.root).get(ev.last_content_details["citation_id"])
        self.assertEqual(citation["results"], reviews)
        self.assertEqual(citation["query"], queries[0])
        self.assertTrue(ev.last_content_details["review"]["reason"])

    def test_no_reviewer_config_is_explicit_error_not_heuristic(self):
        m = self.module()
        self.assertTrue(hasattr(m.LibraryEvaluator, "content_score"), "content model layer missing")
        ev = m.LibraryEvaluator(
            self.root,
            model_config_file=self.root / "no-config.json",
            search=lambda *a, **k: self.fail("no search before missing config error"),
        )
        with self.assertRaises(m.ContentReviewError):
            ev.content_score("https://github.com/a/b", "# Extensive docs and tests")

    def test_content_default_uses_trusted_product_config_and_transport(self):
        m = self.module()
        self.assertTrue(hasattr(m.LibraryEvaluator, "content_score"), "content model layer missing")
        from types import SimpleNamespace
        from unittest.mock import Mock

        route = {"model": "configured-model"}
        with (
            patch(
                "product_config.read_config", return_value=SimpleNamespace(values=route)
            ) as config,
            patch("product_config.read_key", return_value="unit-test-api-key") as key,
            patch("inner_model.ProductModel") as model,
        ):
            model.return_value.return_value = {
                "text": json.dumps(
                    {
                        "readme_completeness": 1,
                        "test_coverage": 1,
                        "documentation": 1,
                        "code_organization": 1,
                        "reason": "Evidence-supported assessment",
                    }
                )
            }
            ev = m.LibraryEvaluator(self.root, search=lambda *a, **k: [])
            config.assert_not_called()
            key.assert_not_called()
            model.assert_not_called()
            self.assertEqual(ev.content_score("https://github.com/a/b", "README"), 1)
            config.assert_called_once()
            key.assert_called_once_with(key_file=Path.home() / ".jarvis" / "api.key")
            model.assert_called_once_with(route, "unit-test-api-key", self.root)

    def test_bad_model_output_is_never_a_plausible_score(self):
        m = self.module()
        self.assertTrue(hasattr(m.LibraryEvaluator, "content_score"), "content model layer missing")
        for text in [
            "not JSON",
            "{}",
            '{"score":0.9}',
            json.dumps(
                {
                    "readme_completeness": True,
                    "test_coverage": 0.5,
                    "documentation": 0.5,
                    "code_organization": 0.5,
                    "reason": "reason",
                }
            ),
            json.dumps(
                {
                    "readme_completeness": float("nan"),
                    "test_coverage": 0.5,
                    "documentation": 0.5,
                    "code_organization": 0.5,
                    "reason": "reason",
                }
            ),
        ]:
            ev = m.LibraryEvaluator(
                self.root, model=lambda prompt: {"text": text}, search=lambda *a, **k: []
            )
            with self.subTest(text=text), self.assertRaises(m.ContentReviewError):
                ev.content_score("https://github.com/a/b", "README")

    def test_malformed_api_observations_are_conservative_not_crashes(self):
        m = self.module()

        def http(url, *args):
            if "/issues?" in url:
                return 200, {}, [{"state": []}]
            if "/commits?" in url:
                return 200, {}, [{"commit": {"committer": {"date": None}}}]
            if "/releases?" in url:
                return 200, {}, [{"published_at": None}]
            return 200, {}, {"stargazers_count": 10**10000, "forks_count": float("nan")}

        ev = m.LibraryEvaluator(self.root, http=http, github_key_file=self.root / "absent")
        self.assertEqual(ev.eco_score("https://github.com/a/b"), 0)
        self.assertTrue(all(v is None for v in ev.last_eco_details["observations"].values()))

    def test_trigger_strict_boundary_and_mapping(self):
        ev = self.module().LibraryEvaluator(self.root)
        self.assertFalse(ev.should_review_content({"a": 0.8, "b": 0.7}))
        self.assertTrue(ev.should_review_content([0.8, 0.7000000000001]))
        self.assertFalse(ev.should_review_content([0.6]))

    def test_model_search_failure_disclosed_and_large_unicode_bounded(self):
        m = self.module()
        prompts = []

        def failed_search(*a, **k):
            raise RuntimeError("private-provider-details")

        def reviewer(prompt):
            prompts.append(prompt)
            return {
                "text": json.dumps(
                    {
                        "readme_completeness": 0,
                        "test_coverage": 0,
                        "documentation": 0,
                        "code_organization": 0,
                        "reason": "No evidence",
                    }
                )
            }

        ev = m.LibraryEvaluator(self.root, model=reviewer, search=failed_search)
        self.assertEqual(ev.content_score("https://github.com/a/b", "中文资料" * 10000), 0)
        from context_limits import INNER_CONTEXT_BYTE_LIMIT

        self.assertLessEqual(len(prompts[0].encode()), INNER_CONTEXT_BYTE_LIMIT)
        self.assertEqual(ev.last_content_details["search_status"], "unavailable")
        self.assertTrue(ev.last_content_details["readme_truncated"])
        self.assertNotIn("private-provider-details", prompts[0])

    def test_config_corruption_not_overwritten_and_missing_fields_rejected(self):
        m = self.module()
        ev = m.LibraryEvaluator(self.root)
        for config in [
            None,
            [],
            {},
            {"weights": {}},
            {"weights": dict(stars=0, forks=0, recency_days=0, issue_close_rate=0, release_freq=0)},
        ]:
            original = json.dumps(config)
            ev.config_path.write_text(original)
            with self.subTest(config=config), self.assertRaises(ValueError):
                m.LibraryEvaluator(self.root)
            self.assertEqual(ev.config_path.read_text(), original)

    def test_unsafe_token_file_no_api_or_secret_leak(self):
        m = self.module()
        key = self.root / "key"
        key.write_text("secret-test-github-token")
        key.chmod(0o644)
        calls = []
        ev = m.LibraryEvaluator(
            self.root, github_key_file=key, http=lambda *args: calls.append(args)
        )
        self.assertEqual(ev.eco_score("https://github.com/a/b"), 0)
        self.assertEqual(calls, [])
        self.assertEqual(ev.last_eco_details["errors"]["auth"], "unsafe_github_key")
        key.chmod(0o600)
        alias = self.root / "alias"
        alias.symlink_to(key)
        ev = m.LibraryEvaluator(
            self.root, github_key_file=alias, http=lambda *args: calls.append(args)
        )
        self.assertEqual(ev.eco_score("https://github.com/a/b"), 0)
        self.assertEqual(calls, [])

    def test_rate_limit_http_date_and_anonymous_budget(self):
        m = self.module()
        now = [1000.0]
        calls = []

        def http(*args):
            calls.append(args)
            return 429, {"Retry-After": "Thu, 01 Jan 1970 00:20:00 GMT"}, None

        ev = m.LibraryEvaluator(
            self.root, github_key_file=self.root / "absent", http=http, clock=lambda: now[0]
        )
        ev.eco_score("https://github.com/a/b")
        now[0] = 1199
        ev.eco_score("https://github.com/a/b")
        self.assertEqual(len(calls), 1)
        now[0] = 1201
        ev.eco_score("https://github.com/a/b")
        self.assertEqual(len(calls), 2)
        calls = []

        def ok(*args):
            calls.append(args)
            return 200, {}, []

        ev = m.LibraryEvaluator(
            self.root, github_key_file=self.root / "absent", http=ok, clock=lambda: now[0]
        )
        for _ in range(20):
            ev.eco_score("https://github.com/a/b")
        self.assertEqual(len(calls), 60)
        now[0] += 3601
        ev.eco_score("https://github.com/a/b")
        self.assertEqual(len(calls), 64)

    def test_config_formula_and_candidate_trigger(self):
        m = self.module()
        with patch(
            "urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")
        ):
            ev = m.LibraryEvaluator(self.root)
        path = self.root / "library_eval_config.json"
        config = json.loads(path.read_text())
        self.assertEqual(
            config["weights"],
            dict(
                stars=0.35, forks=0.25, recency_days=0.20, issue_close_rate=0.10, release_freq=0.10
            ),
        )
        self.assertIn("thresholds", config)
        self.assertAlmostEqual(ev.final_score(0.8, None), 0.8)
        self.assertAlmostEqual(ev.final_score(0.8, 0.2), 0.56)
        self.assertTrue(ev.should_review_content([0.4]))
        self.assertTrue(ev.should_review_content([0.81, 0.8]))
        self.assertFalse(ev.should_review_content([0.8, 0.6]))
        self.assertFalse(ev.should_review_content([]))
        config["content_review_threshold"] = 0.9
        path.write_text(json.dumps(config))
        self.assertTrue(ev.should_review_content([0.8]))
        self.assertTrue(m.LibraryEvaluator(self.root).should_review_content([0.8]))
        self.assertEqual(json.loads(path.read_text()), config)
        for x in [float("nan"), -1, 1.1, True]:
            with self.subTest(x=x), self.assertRaises(ValueError):
                ev.final_score(x, None)


class SelfEvaluationTests(unittest.TestCase):
    def evaluate(self):
        import importlib

        module = importlib.import_module("library_evaluator")
        self.assertTrue(
            callable(getattr(module.LibraryEvaluator, "self_evaluate", None)),
            "component-owned self_evaluate hook is missing",
        )
        return module.LibraryEvaluator.__new__(module.LibraryEvaluator).self_evaluate()

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
        self.assertEqual(report["module"], "library_evaluator")
        self.assertIn("library", report["domain"].lower())
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

        importlib.import_module("library_evaluator")
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
    import sys

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LibraryTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {
        "total": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
    }
    print(json.dumps(summary, sort_keys=True))
    sys.exit(not result.wasSuccessful())
