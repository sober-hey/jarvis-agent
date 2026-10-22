"""Offline behavior tests for K2-004; no product wiring or model calls."""

from dataclasses import FrozenInstanceError, replace
from typing import Any, cast

import pytest


def _entry(identifier: str = "fact-1", **changes: Any) -> Any:
    from jarvis.memory_schema import MemoryType
    from jarvis.semantic_memory import KnowledgeEntry

    values: dict[str, Any] = {
        "id": identifier,
        "type": MemoryType.SEMANTIC,
        "content": "A structured knowledge candidate, not an authenticated fact.",
        "source": "event:fixture-1",
        "created_at": 1000,
        "last_accessed_at": 1000,
        "subject": "Sir",
        "predicate": "studies",
        "object_value": "astronomy",
        "confidence": 0.8,
        "last_verified_at": None,
    }
    values.update(changes)
    return KnowledgeEntry(**values)


def test_store_retrieve_preserves_shared_schema_and_is_explicitly_offline() -> None:
    from jarvis.memory_schema import MemoryEntry, MemoryType
    from jarvis.semantic_memory import DRAFT_STATUS, RUNTIME_ENABLED, SemanticMemory

    memory = SemanticMemory()
    entry = _entry(tags=["background"])
    stored = memory.store(entry)
    assert isinstance(stored, MemoryEntry)
    assert stored.type is MemoryType.SEMANTIC
    assert stored == entry
    assert stored.tags == ("background",)
    assert memory.retrieve("Sir") == [entry]
    assert memory.retrieve("unknown") == []
    assert entry.last_accessed_at == 1000
    assert entry.access_count == 0
    assert DRAFT_STATUS == "DRAFT_PENDING_DP"
    assert RUNTIME_ENABLED is False
    assert memory.runtime_enabled is False
    with pytest.raises(FrozenInstanceError):
        entry.confidence = 1.0


@pytest.mark.parametrize("field", ["subject", "predicate", "object_value"])
@pytest.mark.parametrize("value", ["", "   ", "bad\x00text", 42, None])
def test_triple_fields_require_nonblank_text(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        _entry(**{field: value})


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), True, "0.8", None])
def test_confidence_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        _entry(confidence=value)


@pytest.mark.parametrize("value", [-1, 999, 1000.0, True, "1000", 2**63])
def test_verification_requires_epoch_milliseconds_not_before_creation(value: object) -> None:
    with pytest.raises(ValueError):
        _entry(last_verified_at=value)


def test_semantic_type_shared_validation_and_never_verified_are_preserved() -> None:
    from jarvis.memory_schema import MemoryType

    with pytest.raises(ValueError):
        _entry(type=MemoryType.EPISODIC)
    with pytest.raises(ValueError):
        _entry(source="")
    assert _entry().last_verified_at is None
    assert _entry(last_verified_at=2000).last_verified_at == 2000
    assert _entry(confidence=0).confidence == 0
    assert _entry(confidence=1).confidence == 1


def test_store_replay_is_idempotent_and_conflicting_ids_cannot_overwrite() -> None:
    from jarvis.memory_schema import MemoryEntry, MemoryType
    from jarvis.semantic_memory import SemanticMemory

    memory = SemanticMemory()
    entry = _entry()
    memory.store(entry)
    assert memory.store(entry) == entry
    assert memory.retrieve("Sir") == [entry]
    with pytest.raises(ValueError, match="id"):
        memory.store(replace(entry, object_value="chemistry"))
    ordinary = MemoryEntry("event", MemoryType.EPISODIC, "event", "source", 0, 0)
    with pytest.raises(TypeError):
        memory.store(cast(Any, ordinary))
    assert memory.retrieve("Sir") == [entry]


def test_frozen_bypass_on_input_or_output_cannot_corrupt_storage_or_graph_indexes() -> None:
    from jarvis.semantic_memory import SemanticMemory

    memory = SemanticMemory()
    entry = _entry()
    stored = memory.store(entry)
    object.__setattr__(entry, "subject", "Changed")
    object.__setattr__(stored, "predicate", "changed")
    result = memory.retrieve("Sir")[0]
    object.__setattr__(result, "confidence", -1)
    assert memory.retrieve("Changed") == []
    assert memory.by_predicate("changed") == []
    assert memory.by_predicate("studies") == [_entry()]
    with pytest.raises(ValueError):
        memory.store(_entry(subject="Changed", predicate="changed"))
    assert memory.retrieve("Changed") == []
    assert memory.by_predicate("changed") == []
    assert memory.retrieve("Sir") == [_entry()]


def test_store_revalidates_tampered_candidate_without_partial_write() -> None:
    from jarvis.semantic_memory import SemanticMemory

    entry = _entry()
    object.__setattr__(entry, "confidence", float("nan"))
    memory = SemanticMemory()
    with pytest.raises(ValueError):
        memory.store(entry)
    assert memory.retrieve("Sir") == []


def test_graph_queries_are_exact_and_intersect_subject_with_predicate() -> None:
    from jarvis.semantic_memory import SemanticMemory

    memory = SemanticMemory()
    entries = [
        _entry("c", subject="World", predicate="contains", object_value="stars"),
        _entry("b", predicate="contains", object_value="books"),
        _entry("a"),
        _entry("old", subject="World", created_at=0, last_accessed_at=0),
    ]
    for entry in entries:
        memory.store(entry)
    assert [item.id for item in memory.retrieve("Sir")] == ["a", "b"]
    assert [item.id for item in memory.by_predicate("studies")] == ["old", "a"]
    assert [item.id for item in memory.retrieve("Sir", predicate="contains")] == ["b"]
    assert memory.retrieve("sir") == []
    assert memory.by_predicate("STUDIES") == []
    assert memory.retrieve("Sir", predicate="missing") == []
    result = memory.retrieve("Sir")
    result.clear()
    assert len(memory.retrieve("Sir")) == 2


def test_search_is_unicode_casefolded_literal_over_triple_and_content_only() -> None:
    from jarvis.semantic_memory import SemanticMemory

    memory = SemanticMemory()
    memory.store(_entry("a", subject="Straße", predicate="knows", object_value="星空"))
    memory.store(_entry("b", predicate="Studies", content="book %_ value", source="secret-source"))
    assert [item.id for item in memory.search("STRASSE")] == ["a"]
    assert [item.id for item in memory.search("KNOWS")] == ["a"]
    assert [item.id for item in memory.search("星空")] == ["a"]
    assert [item.id for item in memory.search("studies")] == ["b"]
    assert [item.id for item in memory.search("%_")] == ["b"]
    assert memory.search("secret-source") == []
    assert memory.retrieve("Straße")[0].access_count == 0


@pytest.mark.parametrize("value", ["", "  ", "x\x00", 3, None])
def test_queries_reject_invalid_text(value: Any) -> None:
    from jarvis.semantic_memory import SemanticMemory

    memory = SemanticMemory()
    for operation in (memory.retrieve, memory.by_predicate, memory.search):
        with pytest.raises(ValueError):
            operation(value)
    if value is None:
        assert memory.retrieve("Sir", predicate=None) == []  # Explicit no-filter sentinel.
    else:
        with pytest.raises(ValueError):
            memory.retrieve("Sir", predicate=value)


def test_update_confidence_clamps_and_does_not_verify_or_refresh_access() -> None:
    from jarvis.semantic_memory import SemanticMemory

    memory = SemanticMemory()
    entry = _entry(last_verified_at=1500)
    memory.store(entry)
    assert memory.update_confidence(entry.id, 0.1).confidence == pytest.approx(0.9)
    assert memory.update_confidence(entry.id, 5).confidence == 1
    assert memory.update_confidence(entry.id, -5).confidence == 0
    updated = memory.update_confidence(entry.id, 0.4)
    assert updated == replace(entry, confidence=0.4)
    assert memory.retrieve("Sir") == [updated]
    assert entry.confidence == 0.8
    with pytest.raises(KeyError):
        memory.update_confidence("missing", 0.1)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "0.1", None, 10**400])
def test_update_confidence_rejects_nonfinite_or_nonnumeric_delta_atomically(value: Any) -> None:
    from jarvis.semantic_memory import SemanticMemory

    memory = SemanticMemory()
    entry = _entry()
    memory.store(entry)
    with pytest.raises(ValueError):
        memory.update_confidence(entry.id, value)
    assert memory.retrieve("Sir") == [entry]


def test_merge_conflicts_preserve_evidence_and_never_promote_trust() -> None:
    from jarvis.semantic_memory import SemanticMemory

    memory = SemanticMemory()
    weak = _entry("weak", confidence=0.5, last_verified_at=9000, object_value="chemistry")
    strong = _entry("strong", confidence=0.8, source="event:other", tags=("learned",))
    memory.store(weak)
    memory.store(strong)
    assert memory.merge(weak, strong) == strong
    assert memory.merge(strong, weak) == strong
    assert memory.retrieve("Sir") == [strong, weak]
    assert memory.merge(strong, strong) == strong
    duplicate = replace(strong, id="duplicate", confidence=0.7)
    assert memory.merge(strong, duplicate).confidence == 0.8
    assert memory.merge(strong, weak).last_verified_at is None


def test_merge_ties_prefer_verification_then_creation_then_lexical_id() -> None:
    from jarvis.semantic_memory import SemanticMemory

    memory = SemanticMemory()
    first = _entry("b")
    verified = _entry("a", last_verified_at=1000)
    assert memory.merge(first, verified) == verified
    newer = _entry("c", last_verified_at=2000)
    assert memory.merge(verified, newer) == newer
    created = _entry("d", created_at=1500, last_accessed_at=1500, last_verified_at=2000)
    assert memory.merge(newer, created) == created
    lexical = replace(created, id="a")
    assert memory.merge(created, lexical) == lexical
    assert memory.merge(lexical, created) == lexical


@pytest.mark.parametrize("changes", [{"subject": "World"}, {"predicate": "contains"}])
def test_merge_rejects_unrelated_keys(changes: dict[str, Any]) -> None:
    from jarvis.semantic_memory import SemanticMemory

    with pytest.raises(ValueError, match="subject.*predicate"):
        SemanticMemory().merge(_entry("a"), _entry("b", **changes))


def test_merge_rejects_same_id_conflict_and_invalid_entries() -> None:
    from jarvis.semantic_memory import SemanticMemory

    memory = SemanticMemory()
    with pytest.raises(ValueError, match="id"):
        memory.merge(_entry(), _entry(object_value="chemistry"))
    with pytest.raises(TypeError):
        memory.merge(None, _entry())  # type: ignore[arg-type]
    broken = _entry("b")
    object.__setattr__(broken, "confidence", -1)
    with pytest.raises(ValueError):
        memory.merge(_entry(), broken)


def test_decay_uses_last_verification_or_creation_and_never_mutates_storage() -> None:
    from jarvis.semantic_memory import SemanticMemory

    memory = SemanticMemory()
    entry = _entry(last_verified_at=2000, last_accessed_at=9000)
    memory.store(entry)
    assert memory.decayed_confidence(entry, now_ms=3000, half_life_ms=1000) == pytest.approx(0.4)
    assert memory.decayed_confidence(entry, now_ms=4000, half_life_ms=1000) == pytest.approx(0.2)
    assert memory.decayed_confidence(entry, now_ms=0, half_life_ms=1000) == 0.8
    never = _entry(last_accessed_at=9000)
    assert memory.decayed_confidence(never, now_ms=2000, half_life_ms=1000) == pytest.approx(0.4)
    assert memory.decayed_confidence(never, now_ms=2**63 - 1, half_life_ms=1) == 0
    assert memory.decayed_confidence(replace(never, confidence=0), now_ms=1000) == 0
    assert memory.retrieve("Sir") == [entry]
    assert memory.search("Sir") == [entry]


@pytest.mark.parametrize("value", [-1, 1.0, True, 2**63, None])
def test_decay_rejects_invalid_now(value: Any) -> None:
    from jarvis.semantic_memory import SemanticMemory

    with pytest.raises(ValueError):
        SemanticMemory.decayed_confidence(_entry(), now_ms=value)


@pytest.mark.parametrize("value", [0, -1, 1.0, True, 2**63, None])
def test_decay_rejects_invalid_half_life(value: Any) -> None:
    from jarvis.semantic_memory import SemanticMemory

    with pytest.raises(ValueError):
        SemanticMemory.decayed_confidence(_entry(), now_ms=1000, half_life_ms=value)
