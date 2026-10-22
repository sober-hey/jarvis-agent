"""Offline K2-005 draft fixtures; no personal data or runtime wiring."""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from jarvis.memory_schema import MemoryType
from jarvis.preference_store import Preference, PreferenceSource, PreferenceStore, PreferenceValue


def test_set_get_json_preference(tmp_path: Path) -> None:
    with PreferenceStore(tmp_path / "preferences.sqlite", draft_fixture=True) as store:
        assert store.get("response.style") is None
        detail: list[PreferenceValue] = ["brief", True]
        value: dict[str, PreferenceValue] = {"detail": detail}
        saved = store.set("response.style", value, PreferenceSource.EXPLICIT)
        assert saved.key == "response.style"
        assert saved.value == {"detail": ["brief", True]}
        assert saved.source is PreferenceSource.EXPLICIT
        assert saved.confidence == 1.0
        assert saved.created_at == saved.updated_at
        assert saved.created_at > 0
        assert saved.evidence == []
        assert store.get(saved.key) == saved
        detail.append("local mutation")
        result = store.get(saved.key)
        assert result is not None
        assert result.value == {"detail": ["brief", True]}


@pytest.mark.parametrize("order", list(itertools.permutations(PreferenceSource)))
def test_source_priority_is_order_independent(order: tuple[PreferenceSource, ...]) -> None:
    with PreferenceStore(":memory:", draft_fixture=True) as store:
        for source in order:
            store.set("style", source.value, source, confidence=0.05)
        effective = store.get("style")
        assert effective is not None
        assert effective.source is PreferenceSource.EXPLICIT
        assert effective.value == "EXPLICIT"
        assert store.list_all() == [effective]


def test_inferred_beats_observed_despite_lower_confidence() -> None:
    with PreferenceStore(":memory:", draft_fixture=True) as store:
        store.set("style", "brief", PreferenceSource.INFERRED, confidence=0.2)
        result = store.set("style", "long", PreferenceSource.OBSERVED, confidence=1.0)
        assert result.source is PreferenceSource.INFERRED
        assert result.value == "brief"
        assert result.confidence == 0.2


def test_same_source_update_keeps_creation_and_replaces_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter([200_000_000, 100_000_000])  # Simulate a backward clock in epoch ns.
    monkeypatch.setattr("jarvis.preference_store.time_ns", lambda: next(clock))
    with PreferenceStore(":memory:", draft_fixture=True) as store:
        first = store.set("style", "long", PreferenceSource.EXPLICIT, evidence=["message:a"])
        second = store.set("style", "brief", PreferenceSource.EXPLICIT, evidence=["message:b"])
        assert second.created_at == first.created_at == 200
        assert second.updated_at >= first.updated_at
        assert second.value == "brief"
        assert second.evidence == ["message:b"]


def test_json_types_and_exact_keys_are_preserved() -> None:
    with PreferenceStore(":memory:", draft_fixture=True) as store:
        values: list[tuple[str, PreferenceValue]] = [
            ("A", None),
            ("a", False),
            (" a ", [1, 2.5, "中文"]),
            ("'--", {}),
        ]
        for key, value in values:
            store.set(key, value, PreferenceSource.OBSERVED)
            result = store.get(key)
            assert result is not None
            assert result.value == value
        assert [item.key for item in store.list_all()] == [" a ", "'--", "A", "a"]


def test_returned_snapshots_cannot_modify_stored_value_or_evidence() -> None:
    evidence = ["event:1"]
    with PreferenceStore(":memory:", draft_fixture=True) as store:
        store.set("style", ["brief"], PreferenceSource.EXPLICIT, evidence=evidence)
        evidence.append("event:2")
        result = store.get("style")
        assert result is not None
        assert isinstance(result.value, list)
        result.value.append("long")
        result.evidence.append("event:3")
        listed = store.list_all()[0]
        assert listed.value == ["brief"]
        assert listed.evidence == ["event:1"]


def test_infer_returns_suggestions_without_writes_then_can_be_confirmed(tmp_path: Path) -> None:
    observations = ["event:1 selected concise reply", "event:2 requested shorter reply"]
    seen: list[list[str]] = []

    def infer(values: list[str]) -> list[Preference]:
        seen.append(values.copy())
        values.clear()  # A provider may mutate its input, not the caller's list.
        return [Preference("style", "brief", PreferenceSource.INFERRED, 0.8, 1, 1, observations)]

    with PreferenceStore(tmp_path / "prefs.sqlite", draft_fixture=True, inference=infer) as store:
        original = observations.copy()
        store.set("language", "zh", PreferenceSource.EXPLICIT)
        before = store.list_all()
        suggestions = store.infer(observations)
        assert seen == [original]
        assert observations == original
        assert suggestions[0].source is PreferenceSource.INFERRED
        assert suggestions[0].evidence == original
        assert store.get("style") is None
        assert store.list_all() == before
        candidate = suggestions[0]
        store.set(
            candidate.key,
            candidate.value,
            candidate.source,
            confidence=candidate.confidence,
            evidence=candidate.evidence,
        )
        saved = store.get("style")
        assert saved is not None
        assert saved.value == "brief"


def test_inference_is_not_a_hardcoded_observation_mapping() -> None:
    with PreferenceStore(":memory:", draft_fixture=True) as store:
        assert store.infer([]) == []
        with pytest.raises(RuntimeError, match="inference"):
            store.infer(["synthetic observation"])
        assert store.list_all() == []


@pytest.mark.parametrize("source", [PreferenceSource.EXPLICIT, PreferenceSource.OBSERVED])
def test_inference_cannot_claim_non_inferred_source(source: PreferenceSource) -> None:
    def infer(_: list[str]) -> list[Preference]:
        return [Preference("style", "brief", source, 0.8, 1, 1, ["event:1"])]

    with PreferenceStore(":memory:", draft_fixture=True, inference=infer) as store:
        with pytest.raises(ValueError, match="INFERRED"):
            store.infer(["event:1"])
        assert store.list_all() == []


def test_inference_failure_propagates_without_writing() -> None:
    def infer(_: list[str]) -> list[Preference]:
        raise RuntimeError("provider unavailable")

    with PreferenceStore(":memory:", draft_fixture=True, inference=infer) as store:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            store.infer(["event:1"])
        assert store.list_all() == []


def test_inference_requires_evidence_from_observations() -> None:
    def infer(_: list[str]) -> list[Preference]:
        return [Preference("style", "brief", PreferenceSource.INFERRED, 0.8, 1, 1, ["invented"])]

    with PreferenceStore(":memory:", draft_fixture=True, inference=infer) as store:
        with pytest.raises(ValueError, match="evidence"):
            store.infer(["event:1"])
        assert store.list_all() == []


def test_persistence_reopen_and_delete_all_sources(tmp_path: Path) -> None:
    path = tmp_path / "prefs.sqlite"
    with PreferenceStore(path, draft_fixture=True) as store:
        store.set("style", "long", PreferenceSource.OBSERVED, evidence=["event:1"])
        saved = store.set(
            "style", "brief", PreferenceSource.EXPLICIT, confidence=0.9, evidence=["message:1"]
        )
    with PreferenceStore(path, draft_fixture=True) as store:
        assert store.get("style") == saved
        assert store.list_all() == [saved]
        assert store.delete("style") is True
        assert store.delete("style") is False
        assert store.get("style") is None
    with PreferenceStore(path, draft_fixture=True) as store:
        assert store.list_all() == []
        assert store.get("style") is None  # Lower priority values must not reappear.


def test_requires_explicit_draft_opt_in_before_creating_file(tmp_path: Path) -> None:
    path = tmp_path / "prefs.sqlite"
    with pytest.raises(ValueError, match="draft_fixture"):
        PreferenceStore(path)
    assert not path.exists()
    assert PreferenceStore.runtime_enabled is False
    assert PreferenceStore.memory_type is MemoryType.PREFERENCE


def test_refuses_existing_unrelated_database_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "core.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE core (value TEXT)")
        connection.execute("INSERT INTO core VALUES ('keep')")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="standalone"):
        PreferenceStore(path, draft_fixture=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize("key", ["", "  ", "x\x00y", 1, None])
def test_invalid_keys_cannot_write(key: object) -> None:
    with PreferenceStore(":memory:", draft_fixture=True) as store:
        with pytest.raises((TypeError, ValueError)):
            store.set(cast(str, key), "brief", PreferenceSource.EXPLICIT)
        assert store.list_all() == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "invalid"}, {"set"}, (1, 2)])
def test_non_json_values_cannot_write(value: object) -> None:
    with PreferenceStore(":memory:", draft_fixture=True) as store:
        with pytest.raises((TypeError, ValueError)):
            store.set("style", cast(PreferenceValue, value), PreferenceSource.EXPLICIT)
        assert store.list_all() == []


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), float("inf"), True, "0.5"])
def test_invalid_confidence_cannot_write(confidence: object) -> None:
    with PreferenceStore(":memory:", draft_fixture=True) as store:
        with pytest.raises((TypeError, ValueError)):
            store.set(
                "style", "brief", PreferenceSource.EXPLICIT, confidence=cast(float, confidence)
            )
        assert store.list_all() == []


@pytest.mark.parametrize("evidence", ["event:1", [""], ["x\x00y"], [1]])
def test_invalid_evidence_cannot_write(evidence: object) -> None:
    with PreferenceStore(":memory:", draft_fixture=True) as store:
        with pytest.raises((TypeError, ValueError)):
            store.set(
                "style", "brief", PreferenceSource.EXPLICIT, evidence=cast(list[str], evidence)
            )
        assert store.list_all() == []


def test_inference_cannot_override_explicit_preference() -> None:
    def infer(values: list[str]) -> list[Preference]:
        return [Preference("style", "long", PreferenceSource.INFERRED, 1.0, 1, 1, values)]

    with PreferenceStore(":memory:", draft_fixture=True, inference=infer) as store:
        explicit = store.set("style", "brief", PreferenceSource.EXPLICIT, confidence=0.1)
        proposal = store.infer(["event:1"])[0]
        assert store.get("style") == explicit
        store.set(proposal.key, proposal.value, proposal.source)
        assert store.get("style") == explicit


def test_multiple_connections_read_committed_writes(tmp_path: Path) -> None:
    path = tmp_path / "prefs.sqlite"
    with (
        PreferenceStore(path, draft_fixture=True) as first,
        PreferenceStore(path, draft_fixture=True) as second,
    ):
        first.set("style", "brief", PreferenceSource.EXPLICIT)
        second.set("style", "long", PreferenceSource.OBSERVED)
        assert first.get("style") == second.get("style")
        assert second.delete("style") is True
        assert first.get("style") is None


def test_write_failure_rolls_back_candidate(tmp_path: Path) -> None:
    path = tmp_path / "prefs.sqlite"
    with PreferenceStore(path, draft_fixture=True) as store:
        before = store.set("style", "brief", PreferenceSource.EXPLICIT)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_update BEFORE UPDATE ON preference_candidates "
                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            store.set("style", "long", PreferenceSource.EXPLICIT)
        assert store.get("style") == before
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TRIGGER fail_update")
        assert store.set("style", "updated", PreferenceSource.EXPLICIT).value == "updated"


@pytest.mark.parametrize("times", [(-1, 0), (1, 0), (True, 1), (0, 2**63)])
def test_preference_rejects_invalid_timestamps(times: tuple[int, int]) -> None:
    with pytest.raises(ValueError):
        Preference("style", "brief", PreferenceSource.INFERRED, 0.8, *times)


def test_inference_rejects_duplicate_keys_without_writing() -> None:
    def infer(values: list[str]) -> list[Preference]:
        return [
            Preference("style", "brief", PreferenceSource.INFERRED, 0.8, 1, 1, values),
            Preference("style", "long", PreferenceSource.INFERRED, 0.5, 1, 1, values),
        ]

    with PreferenceStore(":memory:", draft_fixture=True, inference=infer) as store:
        with pytest.raises(ValueError, match="duplicate"):
            store.infer(["event:1"])
        assert store.list_all() == []


def test_infer_does_not_change_sqlite_bytes(tmp_path: Path) -> None:
    def infer(values: list[str]) -> list[Preference]:
        return [Preference("style", "brief", PreferenceSource.INFERRED, 0.8, 1, 1, values)]

    path = tmp_path / "prefs.sqlite"
    with PreferenceStore(path, draft_fixture=True, inference=infer) as store:
        store.set("language", "zh", PreferenceSource.EXPLICIT)
        before = path.read_bytes()
        assert store.infer(["event:1"])
        assert path.read_bytes() == before


def test_closed_store_rejects_access() -> None:
    store = PreferenceStore(":memory:", draft_fixture=True)
    store.close()
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        store.get("style")


def test_string_source_does_not_silently_become_explicit() -> None:
    with PreferenceStore(":memory:", draft_fixture=True) as store:
        with pytest.raises(TypeError, match="PreferenceSource"):
            store.set("style", "brief", cast(PreferenceSource, "EXPLICIT"))
        assert store.list_all() == []
