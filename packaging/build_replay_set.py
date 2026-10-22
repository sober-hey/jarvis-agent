"""Deterministic structured SQLite replay builder; see replay_harness API docs.

CLI (from packaging, with project Python):
  python build_replay_set.py --backup-dir ~/.jarvis/context-p0-RUN --per-type 8
  python build_replay_set.py --mind-backup PATH --memory-backup PATH --graph-backup PATH
Optional --profile-backup, --as-of ISO8601, --name NAME.json. Discovery never
opens loop-data. Outputs counts/hash/path only, never source text or questions.
Missing evidence categories remain empty with a coverage reason, not fabricated
cases. Important-constraint semantics require human confirmation; mechanically
scorable cases ask only what a structured ledger asserts, not whether it is true.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

import replay_harness as rh


def build_replay_set(
    backups: dict[str, Path], *, per_type: int = 8, as_of: str | None = None
) -> dict[str, Any]:
    """Return deterministic v1 document. Does not write or invoke a model.

    Explicit roles: mind (required), memory, graph, profile. Inputs must be
    completed 0600 .backup copies in an owned 0700 context-p0-* directory.
    IDs are namespaced: mind:entries:ID, mind:belief_versions:SEQ,
    memory:facts:ID, graph:nodes:ID, graph:edges:ID, profile:facts:ID.
    Gold consists of directly required row IDs, not recursively inherited refs.
    """
    if type(per_type) is not int or per_type < 1:
        raise ValueError("per_type must be a positive integer")
    paths = {role: rh.validate_backup_path(Path(path)) for role, path in backups.items()}
    before = {role: rh.sha256_file(path) for role, path in paths.items()}
    all_sources = rh.read_sources(paths)
    boundary = rh.make_boundary(all_sources, as_of)
    sources = rh.filtered_sources(all_sources, boundary)
    cases = []
    counts = Counter()

    def add(kind, question, gold, *, at=None, review="structured", **extra):
        if counts[kind] >= per_type:
            return
        case = {
            "type": kind,
            "question": question,
            "gold_evidence_ids": sorted(set(gold)),
            "as_of": at or boundary,
            "review_status": review,
            **extra,
        }
        case["id"] = hashlib.sha256(rh.canonical(case)).hexdigest()[:24]
        cases.append(case)
        counts[kind] += 1

    versions = sources["mind"]["belief_versions"]
    by_belief = defaultdict(list)
    for row in sorted(versions, key=lambda r: r["seq"]):
        by_belief[row["belief_id"]].append(row)
    latest = [rows[-1] for _, rows in sorted(by_belief.items())]
    for row in latest:
        add(
            "facts",
            f"账本中 {row['subject']} 的 {row['attribute']} 当前记录是什么？",
            [f"mind:belief_versions:{row['seq']}"],
        )
    memories = sources.get("memory", {}).get("facts", [])
    memory_index = {r["id"]: r for r in memories}
    for row in sorted(memories, key=lambda r: (r["created_at"], r["id"])):
        if row["status"] == "active":
            add(
                "facts",
                f"来源消息 {row['source_message']} 的当前记忆记录是什么？",
                [f"memory:facts:{row['id']}"],
            )
        if row.get("supersedes") in memory_index:
            add(
                "update_correction",
                f"记忆 {row['supersedes']} 被修订后的当前记录是什么？",
                [f"memory:facts:{row['id']}"],
                superseded_evidence_ids=[f"memory:facts:{row['supersedes']}"],
            )
    for rows in by_belief.values():
        if len(rows) < 2:
            continue
        new, old = rows[-1], rows[-2]
        if new["value"] == old["value"]:
            continue
        add(
            "update_correction",
            f"{new['subject']} 的 {new['attribute']} 在修订后记录是什么？",
            [f"mind:belief_versions:{new['seq']}"],
            superseded_evidence_ids=[f"mind:belief_versions:{old['seq']}"],
        )
    # Mechanical rarity/high-importance selector, not a semantic classifier.
    frequency = Counter((r["subject"], r["attribute"]) for r in versions)
    rare = sorted(
        (
            r
            for r in latest
            if float(r["importance"]) >= 0.8 and frequency[(r["subject"], r["attribute"])] <= 2
        ),
        key=lambda r: (frequency[(r["subject"], r["attribute"])], -r["importance"], r["seq"]),
    )
    for row in rare:
        add(
            "important_constraints",
            f"{row['subject']} 的 {row['attribute']} 是否构成应长期保留的重要约束？请核实。",
            [f"mind:belief_versions:{row['seq']}"],
            review="待人工确认",
            selector="importance>=0.8;structured_key_frequency<=2",
        )
    graph = sources.get("graph", {})
    nodes = {r["id"]: r for r in graph.get("nodes", [])}
    edges = sorted(graph.get("edges", []), key=lambda r: r["id"])
    adjacency = defaultdict(list)
    for edge in edges:
        adjacency[edge["from_node"]].append((edge["to_node"], edge))
        adjacency[edge["to_node"]].append((edge["from_node"], edge))
    seen_paths = set()
    for middle in sorted(adjacency):
        neighbors = adjacency[middle]
        for left, e1 in neighbors:
            for right, e2 in neighbors:
                if left == right or counts["multihop"] >= per_type:
                    continue
                key = (min(left, right), middle, max(left, right))
                if key in seen_paths:
                    continue
                seen_paths.add(key)
                add(
                    "multihop",
                    f"{nodes[left]['key']} 与 {nodes[right]['key']} 经哪个中间节点以两条图边相连（不作因果判断）？",
                    [f"graph:nodes:{i}" for i in (left, middle, right)]
                    + [f"graph:edges:{e1['id']}", f"graph:edges:{e2['id']}"],
                    path_ids=[left, middle, right],
                )
    for row in sorted(nodes.values(), key=lambda r: r["id"]):
        add("facts", f"{row['category']} {row['key']}", [f"graph:nodes:{row['id']}"])
    for row in sources.get("profile", {}).get("facts", []):
        add("facts", f"{row['category']} {row['key']}", [f"profile:facts:{row['id']}"])
    # Proven absent literal identifier, not an unprovable semantic negative.
    source_fingerprint = hashlib.sha256(rh.canonical(before)).hexdigest()
    corpus = rh.canonical(all_sources).decode("utf-8").casefold()
    for index in range(per_type):
        nonce = f"jarvis_absent_{source_fingerprint}_{index}"
        while nonce.casefold() in corpus:
            nonce += "x"
        add(
            "no_answer", nonce, [], absence_scope="literal_identifier_not_present_in_frozen_sources"
        )
    thoughts = [r for r in sources["mind"]["entries"] if r["category"] == "thought"]
    thoughts.sort(key=lambda r: r["seq"])
    if len(thoughts) > 16:
        old = thoughts[:-6]
        # Equally spaced chronological strata, not newest-N selection.
        width = max(1, len(old) // per_type)
        for start in range(0, len(old), width):
            row = old[start]
            add(
                "long_backlog_fairness",
                f"历史念头 {row['id']} 记录了什么？",
                [f"mind:entries:{row['id']}"],
                backlog_quantile=start / len(old),
                thought_backlog_count=len(thoughts),
            )
    after = {role: rh.sha256_file(path) for role, path in paths.items()}
    if before != after:
        raise ValueError("Backup changed while freezing")
    document = {
        "schema_version": rh.SCHEMA_VERSION,
        "builder": "structured-sqlite-v1-no-model",
        "sources": {
            role: {"path": str(paths[role]), "sha256": before[role]} for role in sorted(paths)
        },
        "snapshot_as_of": boundary,
        "cases": cases,
        "counts": {kind: counts[kind] for kind in rh.CASE_TYPES},
        "coverage": {
            kind: "available" if counts[kind] else "no_eligible_structured_evidence"
            for kind in rh.CASE_TYPES
        },
        "limitations": [
            "Mechanical ledger assertions, not world-truth labels",
            "Important-constraint semantics need human review",
            "Mutable rows changed after as_of are excluded; deleted memory history has no deletion timestamp",
            "KG retrieval is measured separately; it is not part of current InnerLoop.context",
        ],
    }
    document["integrity_sha256"] = hashlib.sha256(rh.canonical(document)).hexdigest()
    return document


def materialize_filtered(document: dict[str, Any], root: Path) -> dict[str, Path]:
    """Materialize only filtered rows into a private disposable runtime root.

    This is intentionally a builder helper, not daemon integration. SQLite
    schemas are copied from source backups before inserts, and all inserted
    rows are selected by the stored frozen boundary. The public builder never
    returns materialized contents.
    """
    import shutil

    root = Path(root)
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    sources = {role: Path(info["path"]) for role, info in document["sources"].items()}
    raw = rh.read_sources(sources)
    selected = rh.filtered_sources(raw, document["snapshot_as_of"])
    outputs: dict[str, Path] = {}
    for role, source in sources.items():
        target = root / (
            "mind.db"
            if role == "mind"
            else "memory.db"
            if role == "memory"
            else "knowledge_graph.db"
            if role == "graph"
            else "sir_profile.db"
        )
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        outputs[role] = target
        with sqlite3.connect(target) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            if role == "mind":
                # Derived curiosity projections are tied to the source ledger's
                # original sequence numbers and cannot survive time filtering.
                # Rebuild them from the frozen rows via Curiosity initialization.
                for name, kind in db.execute(
                    "SELECT name,type FROM sqlite_master WHERE name GLOB 'curiosity_*'"
                ).fetchall():
                    if kind == "trigger":
                        db.execute(f'DROP TRIGGER IF EXISTS "{name}"')
                    elif kind == "table":
                        db.execute(f'DROP TABLE IF EXISTS "{name}"')
            for table, rows in selected[role].items():
                if table == "entries":
                    db.execute("DROP TRIGGER IF EXISTS no_delete")
                    db.execute("DELETE FROM entries")
                elif table == "belief_versions":
                    db.execute("DROP TRIGGER IF EXISTS belief_no_delete")
                    db.execute("DELETE FROM belief_versions")
                elif table == "edges":
                    db.execute('DELETE FROM "edges"')
                elif table in {"facts", "nodes"}:
                    db.execute(f'DELETE FROM "{table}"')
                if not rows:
                    continue
                columns = [key for key in rows[0] if key != "_rowid_"]
                placeholders = ",".join("?" for _ in columns)
                for row in rows:
                    db.execute(
                        f'INSERT INTO "{table}" ({",".join(columns)}) VALUES ({placeholders})',
                        [row.get(key) for key in columns],
                    )
            db.commit()
    return outputs


def write_replay(document: dict[str, Any], *, name: str | None = None) -> dict[str, Any]:
    """Exclusively write frozen 0600 JSON under ~/.jarvis/replay (0700).

    Does not overwrite an existing replay. Returns whole-file SHA-256 as well
    as the embedded canonical payload integrity hash. No private body in result.
    """
    rh.verify_document(document)
    rh._private(rh.PRIVATE_HOME, directory=True)
    root = rh._safe_path(rh.PRIVATE_HOME / "replay")
    if not root.exists():
        root.mkdir(mode=0o700)
    rh._private(root, directory=True)
    name = name or f"context-{document['integrity_sha256'][:20]}.json"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json", name):
        raise ValueError("A plain JSON basename is required")
    path = root / name
    payload = rh.canonical(document) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "integrity_sha256": document["integrity_sha256"],
        "case_count": len(document["cases"]),
        "counts": document["counts"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path)
    for role in rh.TABLES:
        parser.add_argument(f"--{role}-backup", type=Path)
    parser.add_argument("--per-type", type=int, default=8)
    parser.add_argument("--as-of")
    parser.add_argument("--name")
    args = parser.parse_args(argv)
    explicit = {
        role: getattr(args, role + "_backup")
        for role in rh.TABLES
        if getattr(args, role + "_backup") is not None
    }
    if args.backup_dir and explicit:
        parser.error("Choose --backup-dir or explicit role paths")
    try:
        paths = explicit or rh.discover_backups(args.backup_dir)
        print(
            json.dumps(
                write_replay(
                    build_replay_set(paths, per_type=args.per_type, as_of=args.as_of),
                    name=args.name,
                ),
                sort_keys=True,
            )
        )
    except (ValueError, OSError, rh.sqlite3.Error):
        # SQLite/OS errors can contain private filenames or SQL values.
        print(
            json.dumps(
                {
                    "error": "replay_build_failed",
                    "detail": "check private backup paths, schema and output permissions",
                }
            )
        )
        return 2
    return 0


def self_evaluate() -> dict[str, Any]:
    """Static description only; no private state, clocks, files or network access."""
    return {
        "module": "build_replay_set",
        "domain": "deterministic frozen replay-set construction from private ledger backups",
        "current_version": "v1 rule-based extraction with as_of filtering; no model question generation",
        "known_limitations": [
            "Labels are mechanical ledger assertions and need independent human review.",
            "Important-constraint semantics are marked for human confirmation, not auto-judged.",
            "Rows mutated after as_of are excluded; deleted memory history has no deletion time.",
            "No multi-hop edges matched the build rules, so multi-hop coverage is zero.",
        ],
        "evolution_directions": [
            "Add development/sealed-test split with hash-frozen manifests.",
            "Add temporal correction, paraphrase and short two-character entity cases.",
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
