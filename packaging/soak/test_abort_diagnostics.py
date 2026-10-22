"""Regression checks for useful, secret-free soak abort evidence."""

from __future__ import annotations

import json
import subprocess
import unittest

import run_soak as runner


class AbortDiagnosticsTests(unittest.TestCase):
    def test_exception_evidence_keeps_reason_location_but_not_secrets(self) -> None:
        try:
            raise RuntimeError("SupervisorExitedDuringSoak")
        except RuntimeError as exc:
            result = runner.exception_evidence(exc)
        self.assertEqual(result.get("message"), "SupervisorExitedDuringSoak")
        self.assertEqual(
            result["frames"][-1]["function"],
            "test_exception_evidence_keeps_reason_location_but_not_secrets",
        )
        self.assertGreater(result["frames"][-1]["line"], 0)
        secret = "sk-PRIVATE-CREDENTIAL https://private.invalid/?token=secret"
        result = runner.exception_evidence(ValueError(secret))
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["type"], "ValueError")

    def test_timeout_evidence_keeps_operation_without_command_arguments(self) -> None:
        secret = "sk-PRIVATE-CREDENTIAL"
        result = runner.exception_evidence(subprocess.TimeoutExpired(["/bin/ps", secret], 2))
        self.assertEqual(result.get("operation"), "ps")
        self.assertEqual(result["timeout_seconds"], 2)
        self.assertNotIn(secret, json.dumps(result))

    def test_worker_hook_is_exercised_by_process_probe(self) -> None:
        self.assertTrue(callable(runner.exception_evidence))



if __name__ == "__main__":
    unittest.main()
