"""K2-007 offline candidate tests; no product/runtime wiring."""

from typing import Any, cast

import pytest

from jarvis.semantic_memory import KnowledgeEntry


def test_source_chain_is_chronological_detached_and_keeps_original_refs() -> None:
    import importlib.util

    assert importlib.util.find_spec("jarvis.provenance") is not None
    from jarvis.provenance import (
        DRAFT_STATUS,
        RUNTIME_ENABLED,
        Provenance,
        ProvenanceTracker,
        SourceType,
    )

    tracker = ProvenanceTracker()
    refs = ["conversation:c1/message:m1", "speaker:user"]
    first = Provenance(SourceType.USER_STATEMENT, "message:m1", 1000, 0.9, refs)
    second = Provenance(SourceType.INFERRED, "observation:o2", 2000, 0.5, ["message:m1"])
    tracker.record("memory:m1", second)
    tracker.record("memory:m1", first)
    tracker.record("memory:m1", first)
    refs.append("not-original")
    first.evidence_refs.append("not-stored")
    chain = tracker.get_chain("memory:m1")
    assert [item.source_id for item in chain] == ["message:m1", "observation:o2"]
    assert chain[0].evidence_refs == ["conversation:c1/message:m1", "speaker:user"]
    chain[0].evidence_refs.clear()
    chain.clear()
    assert len(tracker.get_chain("memory:m1")) == 2
    assert tracker.get_chain("unknown") == []
    assert tracker.get_chain("memory:m1")[0].recorded_at == 1000
    assert DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert RUNTIME_ENABLED is False
    assert tracker.runtime_enabled is False


def _entry(
    identifier: str,
    value: str = "astronomy",
    *,
    created_at: int = 1000,
    subject: str = "Sir",
    predicate: str = "studies",
) -> KnowledgeEntry:
    from jarvis.memory_schema import MemoryType
    from jarvis.semantic_memory import KnowledgeEntry

    return KnowledgeEntry(
        id=identifier,
        type=MemoryType.SEMANTIC,
        content="Unverified candidate",
        source=f"message:{identifier}",
        created_at=created_at,
        last_accessed_at=created_at,
        subject=subject,
        predicate=predicate,
        object_value=value,
        confidence=0.8,
    )


def test_detects_contradictions_and_duplicates_without_unrelated_false_positives() -> None:
    from jarvis.provenance import ConflictType, ProvenanceTracker

    tracker = ProvenanceTracker(clock_ms=lambda: 5000)
    first = _entry("a")
    second = _entry("b", "chemistry", created_at=2000)
    duplicate = _entry("c")
    unrelated = _entry("d", "tea", subject="Other", predicate="drinks")
    another_subject = _entry("e", "physics", subject="Third")
    conflicts = tracker.detect_conflicts([first, unrelated, second, another_subject, duplicate])
    assert [(item.memory_id_1, item.memory_id_2, item.conflict_type) for item in conflicts] == [
        ("a", "b", ConflictType.CONTRADICTION),
        ("a", "c", ConflictType.DUPLICATE),
        ("b", "c", ConflictType.CONTRADICTION),
    ]
    assert all(item.detected_at == 5000 and item.resolution is None for item in conflicts)
    assert len({item.conflict_id for item in conflicts}) == 3
    replay = tracker.detect_conflicts([duplicate, second, first, first])
    assert replay == conflicts
    assert tracker.detect_conflicts([]) == []
    assert tracker.get_effective_entry("a") == first
    assert tracker.get_effective_entry("b") == second


def test_supersession_explicitly_prefers_newer_not_confidence_and_keeps_history() -> None:
    from dataclasses import replace

    from jarvis.provenance import ConflictType, Provenance, ProvenanceTracker, SourceType
    from jarvis.semantic_memory import SemanticMemory

    tracker = ProvenanceTracker(clock_ms=lambda: 5000)
    older = replace(_entry("a"), confidence=1.0)
    newer = replace(_entry("b", "chemistry", created_at=2000), confidence=0.1)
    memory = SemanticMemory()
    memory.store(older)
    memory.store(newer)
    tracker.record("a", Provenance(SourceType.USER_STATEMENT, "msg:old", 1000, 1.0))
    tracker.record("b", Provenance(SourceType.OBSERVED, "event:new", 2000, 0.1))
    conflict = tracker.detect_conflicts([newer, older])[0]
    assert conflict.resolution is None
    resolved = tracker.resolve(conflict.conflict_id, "SUPERSESSION")
    assert resolved.conflict_type is ConflictType.SUPERSESSION
    assert resolved.resolution == "SUPERSESSION"
    assert tracker.get_effective_entry("a") == newer
    assert tracker.get_effective_entry("b") == newer
    assert tracker.get_chain("a")[0].source_id == "msg:old"
    assert tracker.get_chain("b")[0].source_id == "event:new"
    assert tracker.get_conflict(conflict.conflict_id) == resolved
    assert tracker.resolve(conflict.conflict_id, "SUPERSESSION") == resolved
    assert tracker.detect_conflicts([older, newer]) == [resolved]
    assert memory.retrieve("Sir") == [older, newer]


def test_duplicates_merge_candidate_and_provenance_without_boosting_confidence() -> None:
    from dataclasses import replace

    from jarvis.provenance import ConflictType, Provenance, ProvenanceTracker, SourceType

    tracker = ProvenanceTracker(clock_ms=lambda: 5000)
    first = _entry("a")
    second = replace(_entry("b", created_at=2000), confidence=0.6)
    tracker.record("a", Provenance(SourceType.USER_STATEMENT, "msg:a", 1000, 0.8))
    tracker.record("b", Provenance(SourceType.EXTERNAL_DOC, "doc:b", 2000, 0.6))
    conflict = tracker.detect_conflicts([second, first])[0]
    assert conflict.conflict_type is ConflictType.DUPLICATE
    assert conflict.resolution is None
    tracker.resolve(conflict.conflict_id, "DUPLICATE")
    assert tracker.get_effective_entry("b") == first
    assert tracker.get_chain("a") == tracker.get_chain("b")
    assert [item.source_id for item in tracker.get_chain("a")] == ["msg:a", "doc:b"]
    assert tracker.get_effective_entry("a").confidence == 0.8
    tracker.record("b", Provenance(SourceType.INFERRED, "derived:c", 3000, 0.2))
    assert len(tracker.get_chain("a")) == 3


def test_contradiction_is_not_resolved_by_recency_confidence_or_detection() -> None:
    from jarvis.provenance import ConflictType, ProvenanceTracker

    tracker = ProvenanceTracker(clock_ms=lambda: 5000)
    first, second = _entry("a"), _entry("b", "chemistry", created_at=2000)
    conflict = tracker.detect_conflicts([first, second])[0]
    assert conflict.conflict_type is ConflictType.CONTRADICTION
    assert tracker.get_conflict(conflict.conflict_id).resolution is None
    assert tracker.get_effective_entry("a") == first
    assert tracker.get_effective_entry("b") == second
    noted = tracker.resolve(conflict.conflict_id, "reviewed: both claims retained")
    assert noted.resolution == "reviewed: both claims retained"
    assert noted.conflict_type is ConflictType.CONTRADICTION
    assert tracker.get_effective_entry("a") == first
    assert tracker.get_effective_entry("b") == second


def test_resolution_guards_unknown_invalid_tied_and_conflicting_replays() -> None:
    from jarvis.provenance import ProvenanceTracker

    tracker = ProvenanceTracker(clock_ms=lambda: 5000)
    with pytest.raises(KeyError):
        tracker.resolve("unknown", "SUPERSESSION")
    first, second = _entry("a"), _entry("b", "chemistry")
    conflict = tracker.detect_conflicts([first, second])[0]
    for invalid in ("", "  ", "bad\x00note"):
        with pytest.raises(ValueError):
            tracker.resolve(conflict.conflict_id, invalid)
    with pytest.raises(ValueError, match="duplicate"):
        tracker.resolve(conflict.conflict_id, "DUPLICATE")
    with pytest.raises(ValueError, match="timestamp"):
        tracker.resolve(conflict.conflict_id, "SUPERSESSION")
    assert tracker.get_conflict(conflict.conflict_id).resolution is None
    tracker.resolve(conflict.conflict_id, "reviewed")
    with pytest.raises(ValueError, match="already resolved"):
        tracker.resolve(conflict.conflict_id, "SUPERSESSION")


def test_detection_validates_whole_batch_before_any_write_and_snapshots_results() -> None:
    from dataclasses import replace

    from jarvis.provenance import ProvenanceTracker

    tracker = ProvenanceTracker(clock_ms=lambda: 5000)
    first, second = _entry("a"), _entry("b", "chemistry")
    with pytest.raises(ValueError, match="id"):
        tracker.detect_conflicts([first, second, replace(first, object_value="physics")])
    with pytest.raises(KeyError):
        tracker.get_effective_entry("a")
    conflict = tracker.detect_conflicts([first, second])[0]
    object.__setattr__(first, "object_value", "mutated")
    object.__setattr__(conflict, "resolution", "forged")
    assert tracker.get_conflict(conflict.conflict_id).resolution is None
    result = tracker.get_effective_entry("a")
    object.__setattr__(result, "subject", "mutated")
    assert tracker.get_effective_entry("a") == _entry("a")
    with pytest.raises(ValueError):
        tracker.detect_conflicts([first])


def test_transitive_merges_supersession_and_stale_pairs_cannot_create_cycles() -> None:
    from jarvis.provenance import ProvenanceTracker

    tracker = ProvenanceTracker(clock_ms=lambda: 5000)
    first, second, third = (
        _entry("a"),
        _entry("b", created_at=2000),
        _entry("c", "chemistry", created_at=3000),
    )
    conflicts = tracker.detect_conflicts([first, second, third])
    by_pair = {(item.memory_id_1, item.memory_id_2): item for item in conflicts}
    tracker.resolve(by_pair["a", "b"].conflict_id, "DUPLICATE")
    tracker.resolve(by_pair["a", "c"].conflict_id, "SUPERSESSION")
    assert tracker.get_effective_entry("a") == third
    assert tracker.get_effective_entry("b") == third
    assert tracker.get_effective_entry("c") == third
    with pytest.raises(ValueError, match="stale"):
        tracker.resolve(by_pair["b", "c"].conflict_id, "SUPERSESSION")
    assert tracker.get_conflict(by_pair["b", "c"].conflict_id).resolution is None


def test_requested_subject_only_heuristic_does_not_overwrite_unrelated_predicates() -> None:
    from jarvis.provenance import ConflictType, ProvenanceTracker

    tracker = ProvenanceTracker(clock_ms=lambda: 5000)
    study = _entry("a")
    drink = _entry("b", "tea", predicate="drinks", created_at=2000)
    conflict = tracker.detect_conflicts([study, drink])[0]
    assert conflict.conflict_type is ConflictType.CONTRADICTION
    with pytest.raises(ValueError, match="predicate"):
        tracker.resolve(conflict.conflict_id, "SUPERSESSION")
    assert tracker.get_effective_entry("a") == study
    assert tracker.get_effective_entry("b") == drink
    equal_value_other_predicate = _entry("c", predicate="likes")
    assert tracker.detect_conflicts([study, equal_value_other_predicate]) == []


def test_equal_timestamps_do_not_supersede_but_duplicates_have_stable_winner() -> None:
    from jarvis.provenance import ProvenanceTracker

    tracker = ProvenanceTracker(clock_ms=lambda: 5000)
    first, second = _entry("a"), _entry("b")
    conflict = tracker.detect_conflicts([second, first])[0]
    tracker.resolve(conflict.conflict_id, "DUPLICATE")
    assert tracker.get_effective_entry("a") == first
    assert tracker.get_effective_entry("b") == first


def test_provenance_default_lists_are_independent_and_all_origins_remain_untrusted() -> None:
    from jarvis.provenance import Provenance, ProvenanceTracker, SourceType

    tracker = ProvenanceTracker()
    for kind in SourceType:
        provenance = Provenance(kind, f"source:{kind}", 0, 0)
        tracker.record("any-memory-id", provenance)
    assert len(tracker.get_chain("any-memory-id")) == 4
    first = Provenance(SourceType.OBSERVED, "one", 0, 1)
    second = Provenance(SourceType.INFERRED, "two", 0, 1)
    first.evidence_refs.append("one-ref")
    assert second.evidence_refs == []


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), True, "0.8", 10**1000])
def test_provenance_rejects_invalid_confidence(value: Any) -> None:
    from jarvis.provenance import Provenance, SourceType

    with pytest.raises(ValueError):
        Provenance(SourceType.OBSERVED, "event:one", 1000, value)


@pytest.mark.parametrize("value", [-1, 1.5, True, "1000", 2**63])
def test_provenance_and_conflicts_reject_invalid_timestamps(value: Any) -> None:
    from jarvis.provenance import ConflictRecord, ConflictType, Provenance, SourceType

    with pytest.raises(ValueError):
        Provenance(SourceType.OBSERVED, "event:one", value, 0.8)
    with pytest.raises(ValueError):
        ConflictRecord("a", "b", ConflictType.CONTRADICTION, value)


@pytest.mark.parametrize("value", ["", "  ", "bad\x00text", None, 42])
def test_source_ids_refs_and_memory_ids_require_nonblank_text(value: Any) -> None:
    from jarvis.provenance import Provenance, ProvenanceTracker, SourceType

    tracker = ProvenanceTracker()
    valid = Provenance(SourceType.OBSERVED, "event:one", 1000, 0.8)
    with pytest.raises(ValueError):
        Provenance(SourceType.OBSERVED, value, 1000, 0.8)
    with pytest.raises(ValueError):
        Provenance(SourceType.OBSERVED, "event:one", 1000, 0.8, [value])
    with pytest.raises(ValueError):
        tracker.record(value, valid)
    with pytest.raises(ValueError):
        tracker.get_chain(value)


def test_type_validation_and_invalid_clock_leave_no_partial_writes() -> None:
    from dataclasses import replace

    from jarvis.provenance import ConflictRecord, Provenance, ProvenanceTracker, SourceType

    with pytest.raises(TypeError):
        Provenance(cast(Any, "OBSERVED"), "event:one", 1000, 0.8)
    with pytest.raises(TypeError):
        Provenance(SourceType.OBSERVED, "event:one", 1000, 0.8, cast(Any, ("ref",)))
    with pytest.raises(TypeError):
        ConflictRecord("a", "b", cast(Any, "CONTRADICTION"), 0)
    tracker = ProvenanceTracker(clock_ms=lambda: -1)
    with pytest.raises(ValueError):
        tracker.detect_conflicts([_entry("a"), _entry("b", "chemistry")])
    with pytest.raises(KeyError):
        tracker.get_effective_entry("a")
    with pytest.raises(TypeError):
        tracker.record("a", cast(Any, object()))
    with pytest.raises(TypeError):
        tracker.detect_conflicts([cast(Any, object())])
    provenance = Provenance(SourceType.OBSERVED, "event:one", 1000, 0.8)
    object.__setattr__(provenance, "confidence", float("nan"))
    with pytest.raises(ValueError):
        tracker.record("a", provenance)
    assert tracker.get_chain("a") == []
    tampered = replace(_entry("a"))
    object.__setattr__(tampered, "object_value", "")
    with pytest.raises(ValueError):
        tracker.detect_conflicts([tampered])


def test_conflict_identifiers_do_not_collide_and_unknown_queries_fail() -> None:
    from jarvis.provenance import ConflictRecord, ConflictType, ProvenanceTracker

    first = ConflictRecord("a:b", "c", ConflictType.CONTRADICTION, 0)
    second = ConflictRecord("a", "b:c", ConflictType.DUPLICATE, 0)
    reversed_pair = ConflictRecord("c", "a:b", ConflictType.SUPERSESSION, 1000)
    assert first.conflict_id != second.conflict_id
    assert first.conflict_id == reversed_pair.conflict_id
    with pytest.raises(ValueError):
        ConflictRecord("a", "a", ConflictType.DUPLICATE, 0)
    with pytest.raises(KeyError):
        ProvenanceTracker().get_conflict("unknown")
