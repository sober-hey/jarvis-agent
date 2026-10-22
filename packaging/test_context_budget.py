"""Context budgets use synthetic records and disposable ledgers only."""

from __future__ import annotations

import json
import unittest
from typing import Any
from copy import deepcopy

from context_limits import INNER_CONTEXT_BYTE_LIMIT
from inner_runtime import bound_context


def byte_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def padded(rows: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    """Pin the observed block byte counts without copying private payloads."""
    for row in rows:
        row["padding"] = ""
    remaining = target - byte_size(rows)
    for index, row in enumerate(rows):
        count = remaining // (len(rows) - index)
        row["padding"] = "x" * count
        remaining -= count
    assert byte_size(rows) == target
    return rows


def stalled_context(capability_count: int = 11) -> dict[str, Any]:
    catalog = padded(
        [
            {
                "id": f"cap-{i}",
                "name": f"source-{i}",
                "persona_description": "I can read a source",
                "implementation": "senses_read_only",
                "call": {"name": "senses_poll", "arguments": {"source": f"source-{i}"}},
                "available": True,
                "costs_money": False,
                "security_sensitive": False,
                "provides": "read-only synthetic source",
                "freshness": "latest",
            }
            for i in range(11)
        ],
        5932,
    )
    if capability_count != 11:
        catalog = [deepcopy(catalog[i % 11]) for i in range(capability_count)]
        for i, entry in enumerate(catalog):
            entry["id"] = f"cap-{i}"
    return {
        "instructions": "i" * 4489,  # JSON string occupies 4491 bytes.
        "capabilities": catalog,
        "beliefs": padded([{"belief_id": f"belief-{i}"} for i in range(8)], 5307),
        "pending_observations": padded(
            [
                {
                    "id": f"event-{i}",
                    "source": f"cap-{i}",
                    "payload": {"kind": "tool_result", "data": {"belief_id": f"belief-{i}"}},
                }
                for i in range(6)
            ],
            6903,
        ),
        **{
            key: []
            for key in (
                "events",
                "thoughts",
                "memories",
                "pending_memories",
                "relevance_candidates",
                "cached_relevance",
                "recent_thoughts",
                "recalled",
                "unresolved_questions",
            )
        },
    }


class ContextBudgetTests(unittest.TestCase):
    def test_stalled_distribution_fits_without_raising_shared_limit(self):
        original = stalled_context()
        snapshot = deepcopy(original)
        try:
            context = bound_context(original)
        except ValueError as exc:
            self.fail(f"reproduced the stalled daemon context failure: {exc}")
        self.assertEqual(INNER_CONTEXT_BYTE_LIMIT, 10600)
        self.assertLessEqual(byte_size(context) + 256, INNER_CONTEXT_BYTE_LIMIT)
        self.assertGreater(context["context_omitted"].get("pending_observations", 0), 0)
        self.assertGreater(len(context["pending_observations"]), 0)
        self.assertEqual(original, snapshot, "bounding must only change the request view")

    def test_late_catalog_trimming_preserves_sensor_alias_provenance(self):
        from curiosity import event_evidence

        original = stalled_context(22)
        for entry in original["capabilities"]:
            entry["call"]["arguments"]["source"] = "unrelated"
        for index in (1, 21):
            original["capabilities"][index]["call"]["arguments"]["source"] = "shared"
        event = {
            "id": "alias-observation",
            "source": "senses:shared",
            "created_at": "2026-10-01T00:00:00+00:00",
            "payload": {
                "kind": "tool_result",
                "data": {
                    "belief_id": "b",
                    "evidence": {"capability_id": "cap-21"},
                    "sense_event": {
                        "source": "shared",
                        "kind": "sample",
                        "observed_at": "2026-10-01T00:00:00+00:00",
                        "payload": {
                            "status": "available",
                            "sampled_at": "2026-10-01T00:00:00+00:00",
                        },
                    },
                    "padding": "x" * 900,
                },
            },
        }
        original["pending_observations"] = [event]
        original["beliefs"] = []
        original["relevance_candidates"] = [{"belief_id": "b", "padding": "x" * 900}]
        for instructions_size in (5500, 6000, 6500, 7000, 7500):
            with self.subTest(instructions=instructions_size):
                context = bound_context({**original, "instructions": "i" * instructions_size})
                for retained in context["pending_observations"]:
                    self.assertIsNotNone(event_evidence(retained, context["capabilities"]))

    def test_hidden_capabilities_cannot_trigger_source_free_ask(self):
        import tempfile
        from pathlib import Path
        from datetime import UTC, datetime
        from curiosity import Curiosity
        from inner_loop import MindStore

        with tempfile.TemporaryDirectory(
            prefix="jarvis-context-missing-scores-", dir=Path.home()
        ) as folder:
            root = Path(folder)
            root.chmod(0o700)
            store = MindStore(root)
            belief = store.append_belief(
                belief_id="missing-source",
                subject="Sir",
                attribute="record",
                value="unknown",
                source="inferred",
                confidence=0.3,
                last_verified="2000-01-01T00:00:00+00:00",
                importance=1.0,
            )
            context = {
                "relevance_candidates": [belief],
                "cached_relevance": [],
                "context_omitted": {"capabilities": 1},
                "as_of": datetime.now(UTC).isoformat(),
            }
            decision = Curiosity(store).decide([], context, [], 1)
            self.assertEqual(decision["kind"], "associate")
            self.assertEqual(store.entries("belief_score")[-1]["payload"]["decision"], decision)

    def test_subset_relevance_cache_is_read_after_budgeting(self):
        import tempfile
        from pathlib import Path
        from curiosity import fingerprint
        from inner_loop import MindStore
        from inner_runtime import InnerLoop

        with tempfile.TemporaryDirectory(prefix="jarvis-context-cache-", dir=Path.home()) as folder:
            root = Path(folder)
            root.chmod(0o700)
            store = MindStore(root)
            loop = InnerLoop(store, lambda _: {}, catalog=stalled_context(22)["capabilities"])
            belief = store.append_belief(
                belief_id="cached",
                subject="Sir",
                attribute="record",
                value="unknown",
                source="inferred",
                confidence=0.3,
                last_verified="2000-01-01T00:00:00+00:00",
                importance=1.0,
            )
            context = loop.context()
            visible = loop.context_catalog(context)
            self.assertLess(len(visible), len(loop.catalog))
            cached_rows = [
                {
                    "belief_id": belief["belief_id"],
                    "capability_id": c["id"],
                    "score": 0.0,
                    "reason": "cached synthetic relevance",
                }
                for c in visible
            ]
            store.append(
                "relevance_cache",
                {"fingerprint": fingerprint(belief, visible), "rows": cached_rows},
                source="test",
            )
            following = loop.context()
            self.assertTrue(following["cached_relevance"])
            self.assertLessEqual(byte_size(following) + 256, INNER_CONTEXT_BYTE_LIMIT)
            self.assertTrue(
                {r["capability_id"] for r in following["cached_relevance"]}.issubset(
                    {r["id"] for r in following["capabilities"]}
                )
            )
            self.assertEqual(
                len(following["cached_relevance"])
                + following["context_omitted"].get("cached_relevance", 0),
                len(visible),
            )

    def test_relevance_fingerprint_matches_full_and_compact_catalog(self):
        from curiosity import fingerprint

        original = stalled_context(22)
        context = bound_context({**original, "context_round": 2})
        full = [
            row
            for row in original["capabilities"]
            if row["id"] in {c["id"] for c in context["capabilities"]}
        ]
        belief = {
            "belief_id": "b",
            "revision": 1,
            "subject": "Sir",
            "attribute": "record",
            "value": "unknown",
            "source": "inferred",
            "importance": 0.8,
        }
        self.assertEqual(fingerprint(belief, full), fingerprint(belief, context["capabilities"]))

    def test_step_validates_visible_catalog_but_executes_full_descriptor(self):
        import tempfile
        from pathlib import Path
        from inner_loop import MindStore
        from inner_runtime import InnerLoop
        from test_inner_loop import answer
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(
            prefix="jarvis-context-catalog-", dir=Path.home()
        ) as folder:
            root = Path(folder)
            root.chmod(0o700)
            store = MindStore(root)
            catalog = stalled_context(22)["capabilities"]
            observed: list[dict[str, Any]] = []
            executed: list[dict[str, Any]] = []

            def model(prompt: str) -> dict[str, Any]:
                prompt_context = json.loads(prompt)
                observed.append(prompt_context)
                response_context = deepcopy(prompt_context)
                response_context["events"] = [{"id": "check-source"}]
                result = json.loads(answer(json.dumps(response_context))["text"])
                for row in result["relevance"]:
                    row["score"] = 1.0
                return {"text": json.dumps(result)}

            def executor(entry: dict[str, Any]) -> dict[str, Any]:
                executed.append(deepcopy(entry))
                return {"synthetic": True, "city": "synthetic"}

            loop = InnerLoop(store, model, catalog=catalog, executor=executor, max_rounds=1)
            store.append_belief(
                belief_id="check-source",
                subject="Sir",
                attribute="record",
                value="unknown",
                source="inferred",
                confidence=0.3,
                last_verified="2000-01-01T00:00:00+00:00",
                importance=1.0,
            )
            # This synthetic catalog is intentionally not registered. The leaf
            # recognizer is the only fake; selection and descriptor resolution
            # execute through the real step/Curiosity/scheduler path.
            with patch("inner_runtime.capabilities.recognized", return_value=True):
                self.assertTrue(loop.step())
            self.assertEqual(store.count("validation_error"), 0)
            self.assertEqual(store.count("thought"), 1)
            self.assertTrue(
                executed,
                store.entries("belief_score")
                + store.entries("decision")
                + store.entries("curiosity_decision")
                + store.entries("action_blocked"),
            )
            self.assertEqual(executed[0], next(c for c in catalog if c["id"] == executed[0]["id"]))
            self.assertIn("provides", executed[0])
            self.assertLess(len(observed[0]["capabilities"]), len(catalog))
            self.assertEqual(loop.catalog, catalog)

    def test_doubled_and_growing_catalog_remains_bounded_and_rotates(self):
        for count in (22, 100):
            with self.subTest(capabilities=count):
                original = stalled_context(count)
                contexts = [bound_context({**original, "context_round": n}) for n in (1, 2)]
                for context in contexts:
                    self.assertLessEqual(byte_size(context) + 256, INNER_CONTEXT_BYTE_LIMIT)
                    self.assertGreater(context["context_omitted"].get("capabilities", 0), 0)
                    self.assertEqual(len(original["capabilities"]), count)
                self.assertNotEqual(
                    contexts[0]["capabilities"][0]["id"], contexts[1]["capabilities"][0]["id"]
                )

    def test_compact_capabilities_preserve_provenance_flags_and_call(self):
        from curiosity import event_evidence

        original = stalled_context()
        context = bound_context(original)
        for entry in context["capabilities"]:
            trusted = next(c for c in original["capabilities"] if c["id"] == entry["id"])
            for key in (
                "id",
                "implementation",
                "call",
                "available",
                "costs_money",
                "security_sensitive",
            ):
                self.assertEqual(entry[key], trusted[key])
            event = {
                "source": "senses:" + trusted["call"]["arguments"]["source"],
                "payload": {
                    "kind": "tool_result",
                    "data": {
                        "sense_event": {
                            "source": trusted["call"]["arguments"]["source"],
                            "kind": "sample",
                            "payload": {
                                "status": "available",
                                "sampled_at": "2026-10-01T00:00:00+00:00",
                            },
                        }
                    },
                },
            }
            self.assertEqual(event_evidence(event, [entry]), event_evidence(event, [trusted]))

    def test_omitted_observations_are_not_settled_and_next_batch_can_see_them(self):
        import tempfile
        from pathlib import Path
        from daemon_wiring import ContextMind, settled_ids
        from inner_loop import MindStore

        with tempfile.TemporaryDirectory(
            prefix="jarvis-context-budget-", dir=Path.home()
        ) as folder:
            root = Path(folder)
            root.chmod(0o700)
            store = MindStore(root)
            loop = ContextMind(store, lambda _: {}, catalog=[])
            events = [
                loop.ingest(
                    "conversation",
                    {
                        "daemon_event_id": f"synthetic-{i}",
                        "role": "user",
                        "text": "中" * 100,
                    },
                    source="sir",
                )
                for i in range(6)
            ]
            all_ids = {event["id"] for event in events}
            context = loop.context()
            exposed = {event["id"] for event in context["pending_observations"]}
            omitted = all_ids - exposed
            self.assertTrue(omitted)
            self.assertTrue(exposed)
            self.assertGreater(context["context_omitted"].get("pending_observations", 0), 0)
            for key in ("events", "daemon_pending_senses"):
                self.assertFalse(omitted & {r["id"] for r in context[key]})
            self.assertFalse(settled_ids(store, list(all_ids)))
            self.assertEqual(store.count("belief_origin"), 0)
            # A real persisted response + thoughts exercises daemon's receipt path.
            from daemon_wiring import Wiring

            # Only the receipt reconciler needs this state, not fixture/model
            # setup (which deliberately requires a separate /tmp acceptance root).
            wiring = Wiring.__new__(Wiring)
            wiring.mind = loop
            wiring.reconciliation_seq = lambda kind: 0
            wiring.checkpoint = lambda kind, seq: None
            call = store.append(
                "model_call", {"phase": "thought", "context": context}, source="test"
            )
            store.append(
                "model_response",
                {"call_id": call["id"], "text": json.dumps({"thoughts": [{}]})},
                source="test",
            )
            store.append("thought", {"call_id": call["id"]}, source="test")
            wiring.reconcile_consumption()
            self.assertFalse(settled_ids(store, list(omitted)))
            self.assertFalse(
                any(
                    store.exists("belief_origin", where={"origin_id": identity})
                    for identity in omitted
                )
            )
            # Complete the exposed batch; no synthetic deletion or receipt for omissions.
            for identity in exposed:
                if not store.exists("daemon_sense_consumed", where={"event_id": identity}):
                    store.append("daemon_sense_consumed", {"event_id": identity}, source="test")
            following = ContextMind(store, lambda _: {}, catalog=[]).context()
            self.assertTrue(omitted & {r["id"] for r in following["pending_observations"]})
            self.assertLessEqual(byte_size(following) + 512, INNER_CONTEXT_BYTE_LIMIT)


if __name__ == "__main__":
    unittest.main()
