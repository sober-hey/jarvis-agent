"""Real local ToolRouter acceptance tests; no model/network calls."""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ToolRouterRealTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tool-router-real-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def router(self, **kwargs):
        from tool_router import ToolRouter

        return ToolRouter(allowed_roots=[self.root], repo_root=self.root, **kwargs)

    def test_real_write_read_has_private_audit_and_schema(self):
        self.assertIsNotNone(
            importlib.util.find_spec("tool_router"), "ToolRouter implementation is missing"
        )
        router = self.router(data_root=self.root / "not-created")
        self.assertFalse((self.root / "not-created").exists())
        written = router.route("write_file", {"path": "hello.txt", "content": "secret-token-123\n"})
        self.assertEqual(written["status"], "completed", written)
        self.assertTrue(written["ok"])
        read = router.route("read_file", {"path": "hello.txt"})
        self.assertEqual(read["result"]["content"], "secret-token-123\n")
        self.assertEqual(read["audit"]["category"], "tool_call")
        self.assertNotIn("secret-token-123", json.dumps(written["audit"]))
        self.assertIn("write_file", {t["function"]["name"] for t in router.to_openai_tools()})

    def test_registry_covers_existing_adapters_with_honest_risks(self):
        router = self.router()
        expected = {
            "search",
            "find_files",
            "count",
            "git_status",
            "git_diff",
            "git_log",
            "git_add",
            "git_commit",
            "git_branch",
            "git_checkout",
            "execute",
            "execute_sandboxed",
            "check_safety",
            "pip_install",
            "mcp_connect",
            "mcp_call_tool",
            "mcp_list_servers",
            "vision_capture_screen",
            "vision_describe_image",
            "goal_set_goal",
            "goal_get_status",
            "session_save_state",
            "session_resume",
            "session_list_sessions",
        }
        self.assertFalse(expected - router.registry.keys())
        for entry in router.registry.values():
            self.assertTrue(entry["description"].startswith("I "))
            self.assertIn(entry["risk"], {"read", "write", "exec", "network"})
            self.assertTrue(callable(entry["callable"]))
            self.assertFalse(entry["parameters"]["additionalProperties"])
        self.assertEqual(router.registry["mcp_call_tool"]["risk"], "exec")
        self.assertEqual(router.registry["vision_describe_image"]["risk"], "network")

    def test_real_git_status_and_shell_echo(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "untracked.txt").write_text("hi")
        router = self.router()
        status = router.route("git_status", {})
        self.assertTrue(status["ok"], status)
        self.assertIn("untracked.txt", [r["path"] for r in status["result"]["untracked"]])
        echo = router.route("execute", {"command": "echo router-real", "cwd": str(self.root)})
        self.assertEqual(echo["status"], "completed", echo)
        self.assertEqual(echo["result"]["stdout"].strip(), "router-real")

    def test_validation_denial_unknown_and_symlink_escape(self):
        router = self.router()
        for args in ({"path": "bad", "content": 42}, {"path": "bad", "content": "x", "extra": 1}):
            result = router.route("write_file", args)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "invalid_arguments")
        self.assertFalse((self.root / "bad").exists())
        self.assertEqual(router.route("not-a-tool-secret", {})["status"], "unknown")
        outside = self.root.parent / (self.root.name + "-outside")
        for target in (str(outside), "../escape"):
            self.assertEqual(
                router.route("write_file", {"path": target, "content": "x"})["status"], "denied"
            )
        (self.root / "link").symlink_to(self.root.parent, target_is_directory=True)
        self.assertEqual(
            router.route("write_file", {"path": "link/escape", "content": "x"})["status"], "denied"
        )
        self.assertEqual(
            router.route("execute", {"command": "rm -rf /", "cwd": str(self.root)})["status"],
            "denied",
        )
        self.assertEqual(
            router.route(
                "execute_sandboxed", {"command": "echo x", "allowed_writes": [str(outside)]}
            )["status"],
            "denied",
        )
        self.assertEqual(router.route("git_add", {"files": ["../escape"]})["status"], "denied")

    def test_audit_uses_actual_append_signature_and_never_retries(self):
        class Store:
            def __init__(self):
                self.events = []

            def append(self, kind, payload, *, source):
                self.events.append((kind, payload, source))

        store = Store()
        router = self.router(mind_store=store)
        result = router.route("check_safety", {"command": "echo secret-token-123"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["audit"]["persisted"])
        self.assertEqual(len(store.events), 1)
        self.assertEqual(store.events[0][0], "tool_call")
        self.assertEqual(store.events[0][1]["category"], "tool_call")
        self.assertNotIn("secret-token-123", json.dumps(store.events))
        calls = []

        def broken(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("credential-secret")

        router.registry["write_file"]["callable"] = broken
        failed = router.route("write_file", {"path": "a", "content": "credential-secret"})
        self.assertEqual(failed["status"], "unknown")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("credential-secret", json.dumps(failed))

    def test_shell_nonzero_timeout_and_malformed_numbers(self):
        router = self.router()
        nonzero = router.route("execute", {"command": "/usr/bin/false", "cwd": str(self.root)})
        self.assertEqual(nonzero["status"], "failed", nonzero)
        timeout = router.route(
            "execute", {"command": "sleep 2", "cwd": str(self.root), "timeout": 1}
        )
        self.assertEqual(timeout["status"], "unknown", timeout)
        for bad in (True, float("nan"), -1, "10"):
            self.assertEqual(
                router.route("read_file", {"path": "x", "start_line": bad})["error"],
                "invalid_arguments",
            )
        self.assertEqual(
            router.route("write_file", {"path": "x", "content": []})["error"], "invalid_arguments"
        )

    def test_git_metadata_symlink_is_denied_before_mutation(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".git" / "escape").symlink_to(self.root.parent, target_is_directory=True)
        result = self.router().route("git_add", {"files": []})
        self.assertEqual(result["status"], "denied", result)

    def test_git_environment_redirect_denied_before_mutation(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        router = self.router()
        with patch.dict(os.environ, {"GIT_INDEX_FILE": str(self.root.parent / "outside-index")}):
            result = router.route("git_add", {"files": []})
        self.assertEqual(result["status"], "denied", result)

    def test_real_search_edit_patch_delete_and_precheck_all_patch_paths(self):
        router = self.router()
        router.route("write_file", {"path": "hello.py", "content": "needle = 1\nneedle = 2\n"})
        result = router.route("search", {"pattern": "needle", "path": str(self.root)})
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["result"]), 2)
        edited = router.route(
            "edit_file", {"path": "hello.py", "old_string": " = 1", "new_string": " = 3"}
        )
        self.assertTrue(edited["ok"], edited)
        patched = router.route(
            "apply_patch",
            {"patch_text": "*** Begin Patch\n*** Add File: new.txt\n+new\n*** End Patch"},
        )
        self.assertTrue(patched["ok"], patched)
        denied = router.route(
            "apply_patch",
            {
                "patch_text": "*** Begin Patch\n*** Add File: should-not-exist\n+x\n"
                "*** Add File: ../bad\n+x\n*** End Patch"
            },
        )
        self.assertEqual(denied["status"], "denied", denied)
        self.assertFalse((self.root / "should-not-exist").exists())
        deleted = router.route("delete_file", {"path": "new.txt"})
        self.assertTrue(deleted["ok"])
        self.assertFalse((self.root / "new.txt").exists())

    def test_empty_allowlist_and_internal_symlinks_are_denied(self):
        from tool_router import ToolRouter

        router = ToolRouter(allowed_roots=[])
        self.assertEqual(
            router.route("read_file", {"path": str(self.root / "a")})["status"], "denied"
        )
        router = self.router()
        (self.root / "real").write_text("x")
        (self.root / "alias").symlink_to(self.root / "real")
        self.assertEqual(router.route("read_file", {"path": "alias"})["status"], "denied")
        old = Path.cwd()
        try:
            os.chdir(self.root)
            result = router.route(
                "execute_sandboxed",
                {"command": "echo x", "allowed_writes": [str(self.root.parent / "outside")]},
            )
            self.assertEqual(result["status"], "denied", result)
        finally:
            os.chdir(old)

    def test_constructor_defaults_are_lazy_and_openai_schema_is_detached(self):
        from tool_router import ToolRouter

        with patch("pathlib.Path.mkdir", side_effect=AssertionError("unexpected mkdir")):
            router = ToolRouter()
            tools = router.to_openai_tools()
        self.assertEqual(
            router.allowed_roots,
            [Path("/Volumes/扩展硬盘/jarvis"), Path.home() / ".jarvis/workspace"],
        )
        self.assertIsNone(router.mind_store)
        tools[0]["function"]["parameters"]["properties"].clear()
        self.assertTrue(router.registry["read_file"]["parameters"]["properties"])
        json.dumps(router.to_openai_tools(), allow_nan=False)

    def test_audit_failure_preserves_completed_result_and_unknown_is_audited(self):
        class Store:
            def append(self, *args, **kwargs):
                raise RuntimeError("audit-credential")

        router = self.router(mind_store=Store())
        completed = router.route("write_file", {"path": "done", "content": "x"})
        self.assertEqual(completed["status"], "completed")
        self.assertFalse(completed["audit"]["persisted"])
        self.assertTrue((self.root / "done").exists())
        unknown = router.route("unknown-secret", {"credential": "secret"})
        self.assertEqual(unknown["audit"]["category"], "tool_call")
        self.assertNotIn("unknown-secret", json.dumps(unknown["audit"]))
        self.assertNotIn("audit-credential", json.dumps(completed))

    def test_mcp_command_safety_and_invalid_context_have_no_side_effects(self):
        data = self.root / "product"
        router = self.router(data_root=data)
        denied = router.route(
            "mcp_add_server", {"name": "bad", "command": "sudo", "args": ["anything"]}
        )
        self.assertEqual(denied["status"], "denied", denied)
        bad = router.route("session_save_state", {"session_id": "x", "context": {}})
        self.assertFalse(bad["ok"])
        self.assertFalse(data.exists())

    def test_real_mindstore_persists_exact_tool_call_category(self):
        import sqlite3

        from inner_loop import MindStore

        # Explicitly granted product storage is separate from tool file roots.
        with tempfile.TemporaryDirectory(prefix="jarvis-router-mind-", dir="/Users/sober") as temp:
            data = Path(temp) / "data"
            data.mkdir(mode=0o700)
            store = MindStore(data)
            router = self.router(data_root=data, mind_store=store)
            result = router.route("check_safety", {"command": "echo private-audit-token"})
            self.assertTrue(result["audit"]["persisted"], result)
            db = sqlite3.connect(data / "mind.db")
            try:
                rows = db.execute(
                    "SELECT category,payload FROM entries WHERE category='tool_call'"
                ).fetchall()
            finally:
                db.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "tool_call")
            self.assertNotIn("private-audit-token", rows[0][1])

    def test_vision_unavailable_sentinel_is_not_success(self):
        router = self.router()
        router.registry["vision_describe_image"]["callable"] = lambda **args: "(视觉模型未配置)"
        with patch.object(router, "_precheck"):
            result = router.route("vision_describe_image", {"image_path": "missing"})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "unknown", result)


if __name__ == "__main__":
    unittest.main()
