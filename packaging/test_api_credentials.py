"""The logical api_credentials resource is owned by product_config, not a new module."""

import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import product_config


class ApiCredentialTests(unittest.TestCase):
    def test_owned_resource_metadata_is_distinct_and_io_free(self):
        owner = getattr(product_config, "ApiCredentials", None)
        self.assertIsNotNone(owner, "logical resource needs its own self_evaluate facade")
        assert owner is not None
        with (
            patch("builtins.open", side_effect=AssertionError("private read")),
            patch("pathlib.Path.open", side_effect=AssertionError("private read")),
            patch.object(product_config, "read_key", side_effect=AssertionError("secret access")),
        ):
            report = owner.self_evaluate()
        self.assertEqual(report["module"], "api_credentials")
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
        self.assertIn("read_key", report["current_version"])
        self.assertTrue(report["known_limitations"])
        self.assertTrue(report["improvement_directions"])
        self.assertIsNotNone(datetime.fromisoformat(report["last_evaluated_at"]).utcoffset())
        json.dumps(report)
        report["known_limitations"].clear()
        self.assertTrue(owner.self_evaluate()["known_limitations"])

    def test_real_anonymous_pipe_passes_only_explicit_credential_bytes(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"NONSECRET-FIXTURE-KEY\n")
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(product_config.read_key(key_fd=read_fd), "NONSECRET-FIXTURE-KEY")
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def test_channel_selection_is_exclusive_and_invalid_fd_is_rejected(self):
        with self.assertRaises(product_config.CredentialError):
            product_config.read_key()
        with self.assertRaises(product_config.CredentialError):
            product_config.read_key(key_fd=-1)
        with self.assertRaises(product_config.CredentialError):
            product_config.read_key(key_file=Path(__file__), key_fd=-1)


if __name__ == "__main__":
    unittest.main()
