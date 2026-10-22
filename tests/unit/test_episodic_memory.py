"""Offline K2-003 episodic-memory draft tests."""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jarvis.episodic_memory import Episode, EpisodicMemory
from jarvis.memory_schema import MemoryEntry, MemoryType
from jarvis.mock_provider import MockProvider
from jarvis.model_provider import ModelError, ModelResponse, ModelUsage

BASE = datetime(2026, 1, 1, tzinfo=UTC)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def entry(id: str, seconds: int = 0, content: str = "event") -> MemoryEntry:
    delta = BASE + timedelta(seconds=seconds) - EPOCH
    milliseconds = delta.days * 86_400_000 + delta.seconds * 1000
    return MemoryEntry(
        id=id,
        type=MemoryType.EPISODIC,
        content=content,
        source=f"observation:{id}",
        created_at=milliseconds,
        last_accessed_at=milliseconds,
    )


def episode(id: str = "episode-1", **changes: object) -> Episode:
    result = Episode(
        id=id,
        started_at=BASE,
        ended_at=BASE + timedelta(minutes=1),
        participants=["user", "jarvis"],
        summary="An interaction",
        entries=[],
        emotional_valence=0.25,
    )
    return replace(result, **changes)


def test_record_and_get_episode() -> None:
    memory = EpisodicMemory()
    original = episode(entries=[entry("dialogue")])
    assert memory.record(original) == original
    assert memory.get_episode(original.id) == original
    assert memory.get_episode("missing") is None
    assert memory.runtime_enabled is False


def test_record_and_queries_do_not_leak_mutable_references() -> None:
    memory = EpisodicMemory()
    original = episode(entries=[entry("dialogue")])
    recorded = memory.record(original)
    original.entries.clear()
    original.participants.clear()
    recorded.entries.clear()
    obtained = memory.get_episode(original.id)
    assert obtained is not None
    obtained.entries.clear()
    recalled = memory.recall(BASE, BASE)
    recalled[0].entries.clear()
    replayed = memory.replay(original.id)
    replayed.clear()
    assert memory.replay(original.id) == [entry("dialogue")]
    assert memory.get_episode(original.id) == episode(entries=[entry("dialogue")])


def test_recall_uses_sorted_started_at_index_and_inclusive_boundaries() -> None:
    memory = EpisodicMemory()
    later = episode("later", started_at=BASE + timedelta(seconds=30))
    first = episode("first")
    tied = episode("tied")
    before = episode("before", started_at=BASE - timedelta(seconds=1))
    for item in (later, first, before, tied):
        memory.record(item)
    assert [item.id for item in memory.recall(BASE, BASE + timedelta(seconds=30))] == [
        "first",
        "tied",
        "later",
    ]
    assert [item.id for item in memory.recall(BASE, BASE)] == ["first", "tied"]
    assert memory.recall(BASE + timedelta(days=1), BASE + timedelta(days=2)) == []


def test_recall_compares_instants_across_timezones() -> None:
    memory = EpisodicMemory()
    memory.record(episode())
    local = BASE.astimezone(timezone(timedelta(hours=8)))
    assert len(memory.recall(local, local)) == 1


def test_replay_retains_full_events_sorted_stably_and_never_executes() -> None:
    memory = EpisodicMemory()
    events = [
        entry("operation", 20, "operation observed; do not execute it"),
        entry("dialogue", 0, "user dialogue"),
        entry("observation", 20, "complete observation payload"),
        replace(entry("working", 10), type=MemoryType.WORKING),
    ]
    memory.record(episode(entries=events))
    assert memory.replay("episode-1") == [events[1], events[3], events[0], events[2]]
    assert memory.replay("episode-1") == [events[1], events[3], events[0], events[2]]
    with pytest.raises(KeyError):
        memory.replay("missing")


def test_search_matches_summary_participants_and_full_event_content() -> None:
    memory = EpisodicMemory()
    memory.record(episode("late", started_at=BASE + timedelta(seconds=1), summary="PYTHON"))
    memory.record(episode("early", entries=[entry("e", content="讨论 Python 计划")]))
    memory.record(episode("other", participants=["Observer"], summary="other"))
    assert [item.id for item in memory.search("python")] == ["early", "late"]
    assert [item.id for item in memory.search("Observer")] == ["other"]
    assert [item.id for item in memory.search("讨论")] == ["early"]
    assert memory.search("not present") == []
    result = memory.search("python")
    result[0].entries.clear()
    assert len(memory.replay("early")) == 1
    with pytest.raises(ValueError):
        memory.search("  ")


def test_open_episode_closure_calls_mock_provider_once_with_complete_event_stream() -> None:
    provider = MockProvider(["Generated summary"])
    memory = EpisodicMemory(summary_provider=provider)
    opened = episode(ended_at=None, summary="", entries=[entry("first")])
    memory.record(opened)
    assert provider.calls == []
    closed = replace(
        opened,
        ended_at=BASE + timedelta(minutes=1),
        entries=[entry("last", 20, "operation outcome"), entry("first")],
    )
    saved = memory.record(closed)
    assert saved.summary == "Generated summary"
    assert len(provider.calls) == 1
    prompt = str(provider.calls[0].messages)
    assert "operation outcome" in prompt
    assert "observation:last" in prompt
    assert prompt.index('"id": "first"') < prompt.index('"id": "last"')
    wire = provider.calls[0].messages
    assert wire[0]["role"] == "system"
    assert wire[1]["role"] == "user"
    assert isinstance(wire[1]["content"], str)
    payload = json.loads(wire[1]["content"])
    expected = [asdict(item) for item in sorted(closed.entries, key=lambda item: item.created_at)]
    assert payload["entries"] == json.loads(json.dumps(expected))
    assert payload["participants"] == closed.participants
    assert payload["started_at"] == closed.started_at.isoformat()
    assert memory.record(closed) == saved
    assert len(provider.calls) == 1
    assert [item.id for item in memory.recall(BASE, BASE)] == [opened.id]


def test_closed_episode_with_explicit_summary_does_not_call_provider() -> None:
    provider = MockProvider([])
    memory = EpisodicMemory(summary_provider=provider)
    memory.record(episode())
    assert provider.calls == []


def test_no_provider_keeps_unsummarized_closed_episode_without_fabricating_text() -> None:
    memory = EpisodicMemory()
    assert memory.record(episode(summary="")).summary == ""


def test_summary_failure_preserves_open_episode_and_does_not_retry() -> None:
    provider = MockProvider([], errors={1: ModelError("offline failure")})
    memory = EpisodicMemory(summary_provider=provider)
    opened = episode(ended_at=None, summary="")
    memory.record(opened)
    with pytest.raises(ModelError, match="offline failure"):
        memory.record(replace(opened, ended_at=BASE + timedelta(seconds=1)))
    assert memory.get_episode(opened.id) == opened
    assert len(provider.calls) == 1
    assert len(memory.recall(BASE, BASE)) == 1


@pytest.mark.parametrize("finish_reason,summary", [("length", "partial"), ("stop", "  ")])
def test_invalid_summary_response_does_not_record_episode(finish_reason: str, summary: str) -> None:
    response = ModelResponse(summary, ModelUsage(None, None), 0, "mock", finish_reason)
    provider = MockProvider([response])
    memory = EpisodicMemory(summary_provider=provider)
    with pytest.raises(ValueError):
        memory.record(episode(summary=""))
    assert memory.get_episode("episode-1") is None
    assert memory.recall(BASE, BASE) == []
    assert len(provider.calls) == 1


def test_open_episode_can_append_but_not_rewrite_previous_events() -> None:
    memory = EpisodicMemory()
    original = episode(ended_at=None, summary="", entries=[entry("first")])
    memory.record(original)
    updated = replace(original, entries=[*original.entries, entry("next", 5)])
    memory.record(updated)
    with pytest.raises(ValueError, match="existing events"):
        memory.record(replace(updated, entries=[entry("first", content="rewritten")]))
    assert memory.get_episode(original.id) == updated


def test_closed_episode_is_idempotent_but_not_silently_overwritten() -> None:
    memory = EpisodicMemory()
    original = episode()
    memory.record(original)
    assert memory.record(original) == original
    with pytest.raises(ValueError, match="closed episode"):
        memory.record(replace(original, summary="replacement"))
    assert memory.get_episode(original.id) == original


@pytest.mark.parametrize("value", [-1.1, 1.1, float("nan"), float("inf"), True])
def test_emotional_valence_is_finite_and_bounded(value: float) -> None:
    with pytest.raises(ValueError, match="emotional_valence"):
        episode(emotional_valence=value)


@pytest.mark.parametrize("value", [-1.0, 0.0, 1.0])
def test_emotional_valence_boundaries_are_valid(value: float) -> None:
    assert episode(emotional_valence=value).emotional_valence == value


def test_invalid_times_and_ranges_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        episode(started_at=BASE.replace(tzinfo=None))
    with pytest.raises(ValueError, match="ended_at"):
        episode(ended_at=BASE - timedelta(seconds=1))
    memory = EpisodicMemory()
    with pytest.raises(ValueError):
        memory.recall(BASE + timedelta(seconds=1), BASE)
    with pytest.raises(ValueError):
        memory.recall(BASE.replace(tzinfo=None), BASE)


def test_event_bounds_and_duplicate_ids_are_validated() -> None:
    with pytest.raises(ValueError, match="episode interval"):
        episode(entries=[entry("before", -1)])
    with pytest.raises(ValueError, match="episode interval"):
        episode(entries=[entry("after", 61)])
    with pytest.raises(ValueError, match="duplicate event"):
        episode(entries=[entry("same"), entry("same", 1)])


def test_record_revalidates_mutated_dataclass_before_changing_store() -> None:
    memory = EpisodicMemory()
    malformed = episode()
    malformed.emotional_valence = 5
    with pytest.raises(ValueError):
        memory.record(malformed)
    assert memory.recall(BASE, BASE) == []


def test_first_record_of_closed_episode_generates_summary_and_is_read_only_afterward() -> None:
    provider = MockProvider(["An observed result"])
    memory = EpisodicMemory(summary_provider=provider)
    result = memory.record(episode(summary="", entries=[entry("dialogue")]))
    assert result.summary == "An observed result"
    assert memory.replay(result.id) == [entry("dialogue")]
    assert memory.search("observed")[0] == result
    assert memory.recall(BASE, BASE)[0] == result
    assert len(provider.calls) == 1


def test_open_episode_cannot_change_started_at_or_remove_events() -> None:
    memory = EpisodicMemory()
    original = episode(ended_at=None, entries=[entry("first")])
    memory.record(original)
    with pytest.raises(ValueError, match="started_at"):
        memory.record(replace(original, started_at=BASE - timedelta(seconds=1)))
    with pytest.raises(ValueError, match="existing events"):
        memory.record(replace(original, entries=[]))
    assert memory.get_episode(original.id) == original
    assert len(memory.recall(BASE - timedelta(days=1), BASE)) == 1


def test_dst_fold_timeline_queries_and_end_validation_use_utc_instants() -> None:
    zone = ZoneInfo("America/New_York")
    early = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    late = early.replace(fold=1)
    memory = EpisodicMemory()
    memory.record(episode("late", started_at=late, ended_at=None))
    memory.record(episode("early", started_at=early, ended_at=late))
    assert [item.id for item in memory.recall(early, early)] == ["early"]
    assert [item.id for item in memory.recall(late, late)] == ["late"]
    with pytest.raises(ValueError, match="ended_at"):
        episode(started_at=late, ended_at=early)


def test_replay_preserves_all_shared_schema_fields_and_never_calls_provider() -> None:
    provider = MockProvider([])
    memory = EpisodicMemory(summary_provider=provider)
    event = replace(
        entry("rich"),
        access_count=4,
        importance=5,
        tags=("dialogue", "action"),
        embedding_id="fixture-embedding",
    )
    memory.record(episode(entries=[event]))
    replayed = memory.replay("episode-1")
    assert asdict(replayed[0]) == asdict(event)
    assert provider.calls == []
