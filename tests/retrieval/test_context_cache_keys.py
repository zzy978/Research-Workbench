"""Persisted cache keys remain compatible when history management is shared."""

import pytest

from deepresearch_agent.cache_manager.strategies import (
    ContextAndKeywordAwareCacheKeyStrategy,
    ContextAwareCacheKeyStrategy,
)


@pytest.mark.parametrize("strategy_type", [ContextAwareCacheKeyStrategy, ContextAndKeywordAwareCacheKeyStrategy])
def test_history_is_bounded_versioned_and_isolated(strategy_type):
    strategy = strategy_type(context_window=2)
    other = strategy_type()
    for value in ["first", "second", "third"]:
        strategy.update_history(value, "a", max_history=2)
    strategy.update_history("independent", "b")
    assert strategy.conversation_history == {"a": ["second", "third"], "b": ["independent"]}
    assert strategy.history_versions == {"a": 3, "b": 1}
    assert other.conversation_history == {}
    before = strategy.generate_key("question", thread_id="a")
    strategy.update_history("third", "a", max_history=2)
    assert strategy.generate_key("question", thread_id="a") != before


@pytest.mark.parametrize("strategy_type,expected", [
    (ContextAwareCacheKeyStrategy, "71edc871474642f0c665122a3ce1779e"),
    (ContextAndKeywordAwareCacheKeyStrategy, "36e3fa726411e028b93fc7656e5fd1f9"),
])
def test_existing_key_format_is_preserved(strategy_type, expected):
    strategy = strategy_type(context_window=2)
    for query in ["old", "first", "second"]:
        strategy.update_history(query, "thread-a")
    assert strategy.generate_key(
        " question ", thread_id="thread-a", low_level_keywords=["z", "a"], high_level_keywords=["topic"],
    ) == expected


@pytest.mark.parametrize("strategy_type", [ContextAwareCacheKeyStrategy, ContextAndKeywordAwareCacheKeyStrategy])
def test_zero_window_ignores_history_text_but_tracks_version(strategy_type):
    left, right = strategy_type(context_window=0), strategy_type(context_window=0)
    left.update_history("left")
    right.update_history("right")
    assert left.generate_key("question") == right.generate_key("question")
    left.update_history("left")
    assert left.generate_key("question") != right.generate_key("question")
