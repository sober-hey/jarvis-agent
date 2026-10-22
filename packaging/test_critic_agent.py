"""Scoped acceptance tests; only the model transport is replaced."""

import importlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


class CriticAgentTests(unittest.TestCase):
    def test_single_independent_review_returns_structured_feedback(self):
        self.assertIsNotNone(
            importlib.util.find_spec("critic_agent"), "critic_agent feature missing"
        )
        critic = importlib.import_module("critic_agent")
        config = {"model": "configured-model"}
        result = {"passed": False, "reason": "错误地返回差值", "suggestions": ["改为 a + b"]}
        root = Path("/Users/sober/critic-unit-root")
        with patch.object(critic, "ProductModel") as model:
            model.return_value.return_value = {"text": json.dumps(result, ensure_ascii=False)}
            report = critic.CriticAgent().review(
                "计算两数之和",
                "def add(a, b): return a - b",
                config=config,
                key="FAKE-critic-test",
                data_root=root,
                max_iterations=2,
            )
        self.assertIs(report.passed, False)
        self.assertEqual(report.reason, result["reason"])
        self.assertEqual(report.suggestions, result["suggestions"])
        self.assertEqual(report.iterations, 1)
        model.assert_called_once_with(config, "FAKE-critic-test", root)
        self.assertEqual(
            model.return_value.call_count,
            1,
            "failed review must return to the caller for actual revision",
        )
        prompt = model.return_value.call_args.args[0]
        self.assertIn("任务要求：计算两数之和", prompt)
        self.assertIn("交付结果：def add(a, b): return a - b", prompt)
        self.assertIn("红队审查员", prompt)
        self.assertEqual(config, {"model": "configured-model"})

    def test_accepts_fenced_json_without_coercing_failed_status(self):
        critic = importlib.import_module("critic_agent")
        with patch.object(critic, "ProductModel") as model:
            model.return_value.return_value = {
                "text": '```json\n{"passed": false, "reason": "实现有误", "suggestions": ["修正运算"]}\n```'
            }
            try:
                report = critic.CriticAgent().review(
                    "求和",
                    "return a - b",
                    config={},
                    key="FAKE-critic-test",
                    data_root=Path("/Users/sober/critic-unit-root"),
                    max_iterations=1,
                )
            except ValueError as exc:
                self.fail(f"valid fenced JSON should be accepted: {exc}")
        self.assertIs(report.passed, False)
        self.assertEqual(report.suggestions, ["修正运算"])
        self.assertEqual(report.iterations, 1)

    def test_ambiguous_duplicate_status_is_not_approval(self):
        critic = importlib.import_module("critic_agent")
        with patch.object(critic, "ProductModel") as model:
            model.return_value.return_value = {
                "text": '{"passed": false, "passed": true, "reason": "冲突", "suggestions": []}'
            }
            with self.assertRaises(ValueError):
                critic.CriticAgent().review(
                    "求和",
                    "return a - b",
                    config={},
                    key="FAKE-critic-test",
                    data_root=Path("/Users/sober/critic-unit-root"),
                )

    def test_malformed_judgements_are_rejected_without_retry(self):
        critic = importlib.import_module("critic_agent")
        texts = [
            None,
            "",
            "not JSON",
            "[]",
            "{}",
            '{"passed":"false","reason":"不合格","suggestions":[]}',
            '{"passed":1,"reason":"不合格","suggestions":[]}',
            '{"passed":true,"reason":" ","suggestions":[]}',
            '{"passed":true,"reason":"合格","suggestions":"x"}',
            '{"passed":true,"reason":"合格","suggestions":[1]}',
        ]
        for text in texts:
            with self.subTest(text=text), patch.object(critic, "ProductModel") as model:
                model.return_value.return_value = {"text": text}
                with self.assertRaises(ValueError):
                    critic.CriticAgent().review(
                        "求和", "", config={}, key="FAKE-critic-test", data_root=Path("/unused")
                    )
                self.assertEqual(model.return_value.call_count, 1)

    def test_invalid_budgets_do_not_make_model_requests(self):
        critic = importlib.import_module("critic_agent")
        for budget in [0, -1, True, 1.5, None]:
            with self.subTest(budget=budget), patch.object(critic, "ProductModel") as model:
                with self.assertRaises(ValueError):
                    critic.CriticAgent().review(
                        "求和",
                        "x",
                        config={},
                        key="FAKE-critic-test",
                        data_root=Path("/unused"),
                        max_iterations=budget,
                    )
                model.assert_not_called()

    def test_transport_failure_is_not_retried_or_approved(self):
        critic = importlib.import_module("critic_agent")
        with patch.object(critic, "ProductModel") as model:
            model.return_value.side_effect = RuntimeError("transport unavailable")
            with self.assertRaises(RuntimeError):
                critic.CriticAgent().review(
                    "求和", "x", config={}, key="FAKE-critic-test", data_root=Path("/unused")
                )
            self.assertEqual(model.return_value.call_count, 1)

    def test_caller_can_revise_and_resubmit_until_passed(self):
        critic = importlib.import_module("critic_agent")
        agent = critic.CriticAgent()
        responses = [
            {"text": '{"passed":false,"reason":"减法不符合要求","suggestions":["改为加法"]}'},
            {"text": '{"passed":true,"reason":"满足要求","suggestions":[]}'},
        ]
        with patch.object(critic, "ProductModel") as model:
            model.return_value.side_effect = responses
            reports = []
            for result in ("return a - b", "return a + b"):
                report = agent.review(
                    "求和",
                    result,
                    config={},
                    key="FAKE-critic-test",
                    data_root=Path("/unused"),
                    max_iterations=2,
                )
                reports.append(report)
                if report.passed:
                    break
        self.assertEqual([r.passed for r in reports], [False, True])
        self.assertEqual(model.return_value.call_count, 2)
        self.assertEqual(model.call_count, 2)


class SelfEvaluationTests(unittest.TestCase):
    def evaluate(self):
        import importlib

        module = importlib.import_module("critic_agent")
        self.assertTrue(
            callable(getattr(module.CriticAgent, "self_evaluate", None)),
            "component-owned self_evaluate hook is missing",
        )
        return module.CriticAgent.__new__(module.CriticAgent).self_evaluate()

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
        self.assertEqual(report["module"], "critic_agent")
        self.assertIn("review", report["domain"].lower())
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

        importlib.import_module("critic_agent")
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
