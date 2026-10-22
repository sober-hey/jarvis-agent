"""Offline operational-module hooks: metadata, real policy and no execution."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import builtins
import importlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import unittest
from types import ModuleType
from typing import Any
from unittest.mock import patch


def evaluate(test: unittest.TestCase, name: str) -> tuple[ModuleType, dict[str, Any]]:
    module = importlib.import_module(name)
    test.assertTrue(callable(getattr(module, "self_evaluate", None)), name + " needs a real hook")
    # The hook may inspect its own source target via stat, but never open content,
    # create state, call children/network, load credentials or acquire owner locks.
    with ExitStack() as guards:
        for owner, attr in (
            (builtins, "open"),
            (socket, "create_connection"),
            (socket.socket, "connect"),
            (socket.socket, "bind"),
            (subprocess, "Popen"),
            (subprocess, "run"),
            (sqlite3, "connect"),
            (Path, "read_text"),
            (Path, "read_bytes"),
            (Path, "open"),
            (Path, "mkdir"),
            (Path, "write_text"),
            (os, "kill"),
            (os, "killpg"),
            (os, "system"),
        ):
            guards.enter_context(
                patch.object(owner, attr, side_effect=AssertionError("forbidden " + attr))
            )
        result = module.self_evaluate()
    test.assertEqual(result["module"], name)
    for key in ("domain", "current_version"):
        test.assertIsInstance(result[key], str)
        test.assertTrue(result[key].strip())
    for key in ("known_limitations", "improvement_directions"):
        test.assertIsInstance(result[key], list)
        test.assertTrue(result[key])
        test.assertTrue(all(isinstance(v, str) and v.strip() for v in result[key]))
    stamp = datetime.fromisoformat(result["last_evaluated_at"])
    test.assertIsNotNone(stamp.tzinfo)
    test.assertLess(abs((datetime.now(timezone.utc) - stamp).total_seconds()), 30)
    test.assertTrue(result["checks"])
    test.assertTrue(all(type(value) is bool for value in result["checks"].values()))
    test.assertEqual(result["ok"], all(result["checks"].values()))
    json.dumps(result, allow_nan=False)
    return module, result


class ContextLimitsEvaluationTests(unittest.TestCase):
    def test_hook_checks_real_byte_constants_without_io(self):
        module, result = evaluate(self, "context_limits")
        self.assertEqual(result["limits"]["inner"], module.INNER_CONTEXT_BYTE_LIMIT)
        self.assertEqual(result["limits"]["chat"], module.CHAT_CONTEXT_BYTE_LIMIT)
        self.assertEqual(result["limits"]["json"], module.CONTEXT_JSON_BYTE_LIMIT)
        self.assertTrue(result["ok"])

    def test_invalid_mutated_limit_is_reported_and_results_are_detached(self):
        module, first = evaluate(self, "context_limits")
        first["known_limitations"].append("caller mutation")
        first["limits"]["inner"] = -1
        with patch.object(module, "INNER_CONTEXT_BYTE_LIMIT", 0):
            _, bad = evaluate(self, "context_limits")
        self.assertFalse(bad["ok"])
        _, fresh = evaluate(self, "context_limits")
        self.assertNotIn("caller mutation", fresh["known_limitations"])
        self.assertGreater(fresh["limits"]["inner"], 0)
