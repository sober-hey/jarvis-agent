"""Historical acknowledgement storms: private ledgers, never the resident store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from curiosity import Curiosity, MAX_HISTORICAL_ORIGINS_PER_ROUND, event_evidence
from inner_loop import MindStore, Record


class BeliefOriginDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="belief-origin-test-", dir=Path.home() / ".jarvis"
        )
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.store = MindStore(self.root)
        self.catalog = [{"id": "sensor", "call": {"arguments": {"source": "sample"}}}]
        self.curiosity = Curiosity(self.store)

    def historical_event(self) -> tuple[Record, Record]:
        event = self.store.ingest(
            "tool_result",
            {
                "sense_event": {
                    "source": "sample",
                    "kind": "sample",
                    "payload": {"status": "available", "sampled_at": "2020-01-01T00:00:00+00:00"},
                }
            },
            source="senses:sample",
        )
        evidence = event_evidence(event, self.catalog)
        assert evidence is not None
        if not self.store.beliefs():
            self.store.append_belief(
                belief_id=evidence["belief_id"],
                subject="Sir",
                attribute="sample",
                value="newer",
                source="observed",
                confidence=0.9,
                importance=0.5,
                last_verified="2021-01-01T00:00:00+00:00",
            )
        return event, evidence

    def replay_projection(self, catalog: list[Record], event: Record, evidence: Record) -> None:
        # A different catalog's durable projection may lag acknowledgements from
        # another catalog; its cursor has already passed the marker in entries.
        key = hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()
        with self.store.connection() as db:
            seq = db.execute("SELECT seq FROM entries WHERE id=?", (event["id"],)).fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO curiosity_pending_v1 VALUES(?,?,?,?,?,?)",
                (
                    key,
                    seq,
                    event["id"],
                    evidence["belief_id"],
                    evidence["verified_at"],
                    json.dumps(evidence),
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO curiosity_cursor_v1 VALUES(?,?)",
                (key, self.store.latest_seq()),
            )

    def test_same_pair_is_not_acknowledged_again_after_restart_and_catalog_replay(self):
        event, evidence = self.historical_event()
        self.curiosity.context([], self.catalog, 1)
        self.assertEqual(self.store.count("belief_origin"), 1)
        other_catalog = [{**self.catalog[0], "name": "new catalog view"}]
        self.replay_projection(other_catalog, event, evidence)
        reopened = MindStore(self.root)
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,sys; from pathlib import Path; "
                "from inner_loop import MindStore; from curiosity import Curiosity; "
                "store=MindStore(Path(sys.argv[1])); Curiosity(store).context([],json.loads(sys.argv[2]),2); "
                "print(store.count('belief_origin'))",
                str(self.root),
                json.dumps(other_catalog),
            ],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        self.assertEqual(child.stdout.strip(), "1")
        Curiosity(reopened).context([], other_catalog, 3)
        self.assertEqual(reopened.count("belief_origin"), 1)
        self.assertEqual(reopened.beliefs()[0]["revision"], 1)

    def test_one_round_historical_acknowledgements_are_bounded(self):
        for _ in range(80):
            self.historical_event()
        self.curiosity.context([], self.catalog, 1)
        self.assertLessEqual(self.store.count("belief_origin"), 32)
        self.assertGreater(self.store.count("belief_origin"), 0)
        self.assertEqual(self.store.count("recall"), 1)

    def test_backlog_drains_across_restart_without_starving_fresh_evidence(self) -> None:
        for _ in range(80):
            self.historical_event()
        fresh = self.store.ingest(
            "tool_result",
            {
                "sense_event": {
                    "source": "sample",
                    "kind": "sample",
                    "payload": {"status": "available", "sampled_at": "2022-01-01T00:00:00+00:00"},
                }
            },
            source="senses:sample",
        )
        for number, expected in enumerate((32, 64, 80, 80), 1):
            curiosity = Curiosity(MindStore(self.root))
            context = curiosity.context([], self.catalog, number)
            self.assertEqual(self.store.count("belief_origin"), expected)
            self.assertEqual([row["id"] for row in context["pending_observations"]], [fresh["id"]])
            self.assertFalse(self.store.exists("belief_origin", where={"origin_id": fresh["id"]}))
        self.assertEqual(MAX_HISTORICAL_ORIGINS_PER_ROUND, 32)

    def test_concurrent_writers_and_repeated_pairs_append_once(self) -> None:
        pair = ("origin", "belief")

        def write_receipt(_: int) -> None:
            self.curiosity._acknowledge_historical([pair, pair])

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write_receipt, range(8)))
        self.assertEqual(self.store.count("belief_origin"), 1)
        self.curiosity._acknowledge_historical(
            [("other-origin", "belief"), ("origin", "other-belief")]
        )
        self.assertEqual(self.store.count("belief_origin"), 3)

    def test_existing_duplicates_survive_idempotent_migration_and_pair_query_is_indexed(
        self,
    ) -> None:
        for _ in range(2):
            self.store.append(
                "belief_origin", {"origin_id": "origin", "belief_id": "belief"}, source="test"
            )
        original = self.store.entries()
        with self.store.connection() as db:
            db.execute("DROP INDEX entries_origin_belief_v1")
        for _ in range(2):
            MindStore(self.root)
        self.curiosity._acknowledge_historical([("origin", "belief")])
        self.assertEqual(self.store.entries(), original)
        with self.store.connection() as db:
            plan = [
                row[3]
                for row in db.execute(
                    "EXPLAIN QUERY PLAN SELECT EXISTS(SELECT 1 FROM entries WHERE category='belief_origin' "
                    "AND json_extract(payload,'$.origin_id')=? AND json_extract(payload,'$.belief_id')=?)",
                    ("origin", "belief"),
                )
            ]
            self.assertTrue(
                any("entries_origin_belief_v1" in row and "SEARCH" in row for row in plan), plan
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append only"):
                db.execute("DELETE FROM entries WHERE category='belief_origin'")

    def test_failed_batch_rolls_back_all_new_receipts(self) -> None:
        with self.store.connection() as db:
            db.execute(
                "CREATE TRIGGER fail_receipt BEFORE INSERT ON entries "
                "WHEN NEW.category='belief_origin' AND json_extract(NEW.payload,'$.origin_id')='bad' "
                "BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            self.curiosity._acknowledge_historical([("ok", "belief"), ("bad", "belief")])
        self.assertEqual(self.store.count("belief_origin"), 0)


if __name__ == "__main__":
    unittest.main()
