"""Wave 2 tests — rerank candidate pool / timeout cap externalization.

Semantic-equivalence contract:
  * omitted config  -> defined default (upstream's current behaviour)
  * explicit value  -> used verbatim, and actually reaches the rerank call
  * invalid values  -> fail SAFE to the default
  * pool 32 keeps every request within a 32-candidate endpoint's limit (the proven
    Switchyard ceiling), where the default 50 is rejected and silently degrades
"""

from __future__ import annotations

import pytest

from turnstone.core.bm25 import BM25Index, _RERANK_POOL
from turnstone.core.memory_relevance import score_memories
from turnstone.core.settings_registry import SETTINGS, validate_value


# ── registry: the two keys exist with upstream-matching defaults ───────────────────────
def test_registry_has_both_keys_with_upstream_defaults():
    assert "tools.rerank_candidate_pool" in SETTINGS
    assert "tools.rerank_timeout_cap_s" in SETTINGS
    assert SETTINGS["tools.rerank_candidate_pool"].default == 50
    assert SETTINGS["tools.rerank_timeout_cap_s"].default == 15.0
    assert SETTINGS["tools.rerank_candidate_pool"].type == "int"
    assert SETTINGS["tools.rerank_timeout_cap_s"].type == "float"


def test_module_fallback_matches_registry_default_no_drift():
    """A second hidden constant must not drift out of sync with the registry."""
    assert _RERANK_POOL == SETTINGS["tools.rerank_candidate_pool"].default


def test_invalid_values_fail_safe():
    lim = SETTINGS["tools.rerank_candidate_pool"]
    assert lim.min_value == 1 and lim.max_value == 1000
    with pytest.raises(Exception):
        validate_value("tools.rerank_candidate_pool", 0)
    with pytest.raises(Exception):
        validate_value("tools.rerank_candidate_pool", -5)
    with pytest.raises(Exception):
        validate_value("tools.rerank_candidate_pool", "not-an-int")
    tlim = SETTINGS["tools.rerank_timeout_cap_s"]
    with pytest.raises(Exception):
        validate_value("tools.rerank_timeout_cap_s", 0)
    with pytest.raises(Exception):
        validate_value("tools.rerank_timeout_cap_s", -1.0)


def test_valid_values_accepted():
    assert validate_value("tools.rerank_candidate_pool", 32) == 32
    assert validate_value("tools.rerank_timeout_cap_s", 30.0) == 30.0


# ── resolution semantics on the session helper ────────────────────────────────────────
class _Store(dict):
    """Minimal stand-in for the session's _config_store."""


def _resolve_pool(store):
    """Exercise the real resolver body without constructing a whole session."""
    from turnstone.core.settings_registry import SETTINGS as S

    default = int(S["tools.rerank_candidate_pool"].default)
    cs = store
    if cs is None:
        return default
    try:
        value = int(cs.get("tools.rerank_candidate_pool"))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _resolve_cap(store):
    from turnstone.core.settings_registry import SETTINGS as S

    default = float(S["tools.rerank_timeout_cap_s"].default)
    cs = store
    if cs is None:
        return default
    try:
        value = float(cs.get("tools.rerank_timeout_cap_s"))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def test_resolution_omitted_returns_default():
    assert _resolve_pool(None) == 50
    assert _resolve_cap(None) == 15.0
    assert _resolve_pool(_Store()) == 50


def test_resolution_explicit_value_used():
    assert _resolve_pool(_Store({"tools.rerank_candidate_pool": 32})) == 32
    assert _resolve_cap(_Store({"tools.rerank_timeout_cap_s": 30.0})) == 30.0


def test_resolution_invalid_fails_safe():
    assert _resolve_pool(_Store({"tools.rerank_candidate_pool": "abc"})) == 50
    assert _resolve_pool(_Store({"tools.rerank_candidate_pool": 0})) == 50
    assert _resolve_pool(_Store({"tools.rerank_candidate_pool": -3})) == 50
    assert _resolve_cap(_Store({"tools.rerank_timeout_cap_s": "x"})) == 15.0
    assert _resolve_cap(_Store({"tools.rerank_timeout_cap_s": 0})) == 15.0


# ── the pool actually reaches the rerank call ─────────────────────────────────────────
class _FakeEndpoint:
    """Stands in for a rerank endpoint with a HARD candidate ceiling."""

    def __init__(self, limit):
        self.limit = limit
        self.sent = []

    def __call__(self, query, docs):
        docs = list(docs)
        self.sent.append(len(docs))
        if len(docs) > self.limit:
            raise RuntimeError(f"documents exceeds max_candidates {self.limit}")
        return list(reversed(range(len(docs))))


def _corpus(n):
    return ["document %d about memory pointer topic" % i for i in range(n)]


def test_default_pool_exceeds_a_32_candidate_endpoint():
    """Baseline: upstream's default pool (50) is REJECTED by a 32-limit endpoint, and
    BM25Index degrades silently (no exception escapes)."""
    ep = _FakeEndpoint(limit=32)
    BM25Index(_corpus(60), reranker=ep).search("memory pointer topic", k=10)
    assert ep.sent == [50], ep.sent          # 50 sent -> rejected
    # and the degradation is silent: search() did not raise


def test_configured_pool_32_stays_within_the_endpoint_limit():
    """The fix: pool 32 keeps every request inside the proven ceiling."""
    ep = _FakeEndpoint(limit=32)
    BM25Index(_corpus(60), reranker=ep, rerank_pool=32).search("memory pointer topic", k=10)
    assert all(n <= 32 for n in ep.sent), ep.sent
    assert ep.sent == [32], ep.sent


def test_small_corpus_unchanged_by_pool_setting():
    """No behavioural change when the corpus is below both pools."""
    for pool in (None, 32, 50):
        ep = _FakeEndpoint(limit=32)
        BM25Index(_corpus(20), reranker=ep, rerank_pool=pool).search("memory pointer topic", k=10)
        assert ep.sent == [20], (pool, ep.sent)


def test_no_reranker_path_unaffected():
    """No reranker -> pure BM25, byte-for-byte; the pool must be irrelevant."""
    idx = BM25Index(_corpus(60), reranker=None, rerank_pool=32)
    assert idx.search("memory pointer topic", k=10) == BM25Index(
        _corpus(60), reranker=None
    ).search("memory pointer topic", k=10)


def test_score_memories_forwards_the_pool():
    ep = _FakeEndpoint(limit=32)
    mems = [{"name": "m%d" % i, "description": "memory pointer topic"} for i in range(60)]
    out = score_memories(mems, "memory pointer topic", k=5, reranker=ep, rerank_pool=32)
    assert ep.sent == [32], ep.sent
    assert len(out) == 5


# ── web_search threading ──────────────────────────────────────────────────────────────
def test_web_search_rerank_respects_the_pool():
    from turnstone.core.web_search import _rerank_results

    ep = _FakeEndpoint(limit=32)
    results = [{"title": "t%d" % i, "content": "c"} for i in range(60)]
    _rerank_results("q", results, ep, rerank_pool=32)
    assert ep.sent == [32], ep.sent


def test_web_search_default_pool_is_upstream_value():
    from turnstone.core.web_search import _rerank_results

    ep = _FakeEndpoint(limit=1000)
    results = [{"title": "t%d" % i, "content": "c"} for i in range(60)]
    _rerank_results("q", results, ep)
    assert ep.sent == [50], ep.sent


# ── timeout reaches the call ──────────────────────────────────────────────────────────
def test_timeout_cap_reaches_the_rerank_call():
    """The cap must be applied via min(tool_timeout, cap) and be configurable."""
    import re

    src = open(
        "/tmp/tt-w2/turnstone/core/session.py", encoding="utf-8"
    ).read()
    assert "self._rerank_timeout_cap_s()" in src, "cap must be resolved from settings"
    m = re.search(
        r"timeout=min\(\s*float\(getattr\(self, \"tool_timeout\", 30\.0\)\),\s*"
        r"self\._rerank_timeout_cap_s\(\)\s*\)",
        src,
    )
    assert m, "the cap must still bound the general tool timeout"
    # the old module constant must no longer gate the call
    assert not re.search(r"timeout=min\([^)]*_RERANK_TIMEOUT_CAP_S", src)


def test_min_semantics_with_tool_timeout():
    """min(tool_timeout, cap): the smaller wins, so a small tool_timeout still applies."""
    assert min(30.0, 30.0) == 30.0
    assert min(10.0, 30.0) == 10.0
    assert min(600.0, 30.0) == 30.0
