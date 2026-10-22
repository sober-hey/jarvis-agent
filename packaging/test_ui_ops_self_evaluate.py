"""Regression tests for UI, socket and chat entry self-evaluation."""

import importlib
import unittest
from datetime import datetime


class SelfEvaluateTests(unittest.TestCase):
    modules = ("jarvis_socket", "jarvis_ui", "stream_response", "chat_entry", "chat_launcher")

    def test_self_evaluations_report_real_bounded_capability(self):
        for name in self.modules:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                evaluate = getattr(module, "self_evaluate", None)
                self.assertTrue(callable(evaluate), "missing self_evaluate()")
                result = evaluate()
                self.assertEqual(result["module"], name)
                for field in ("domain", "current_version", "last_evaluated_at"):
                    self.assertIsInstance(result[field], str)
                    self.assertTrue(result[field].strip())
                self.assertIsInstance(result["known_limitations"], list)
                self.assertIsInstance(result["improvement_directions"], list)
                stamp = datetime.fromisoformat(result["last_evaluated_at"])
                self.assertIsNotNone(stamp.tzinfo)

    def test_socket_and_stream_evaluation_probes_live_protocol_bounds(self):
        import jarvis_socket
        import stream_response

        socket_report = jarvis_socket.self_evaluate()
        stream_report = stream_response.self_evaluate()
        self.assertTrue(socket_report["checks"]["request_budget_positive"])
        self.assertTrue(socket_report["checks"]["chat_budget_within_request"])
        self.assertEqual(socket_report["checks"]["server_lifecycle"], "explicit_start_stop")
        self.assertTrue(stream_report["checks"]["chunk_contract"])
        self.assertTrue(stream_report["checks"]["socket_transport"])

    def test_launcher_and_entry_report_live_timeout_and_size_limits(self):
        import chat_entry
        import chat_launcher

        entry = chat_entry.self_evaluate()
        launcher = chat_launcher.self_evaluate()
        self.assertTrue(entry["checks"]["timeout_ordered"])
        self.assertEqual(entry["checks"]["json_input_budget"], chat_entry.MAX_BYTES)
        self.assertEqual(
            launcher["checks"]["chat_input_budget"], chat_launcher.CHAT_CONTEXT_BYTE_LIMIT
        )
        self.assertTrue(launcher["checks"]["credential_sources_explicit"])


if __name__ == "__main__":
    unittest.main()
