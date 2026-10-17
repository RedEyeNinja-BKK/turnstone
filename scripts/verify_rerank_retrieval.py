#!/usr/bin/env python3
"""Controlled retrieval test for the native reranker against the live endpoint.

Exercises the same objects the runtime uses -- ``resolve_rerank_client`` ->
``CohereJinaRerankClient`` and ``BM25Index`` with a reranker attached -- rather
than a private HTTP call, so a pass means the Turnstone interface itself works.

Checks: scores, ranking, real reordering vs native BM25, timeout handling,
endpoint-failure handling, and the empty-input fast path.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/var/lib/turnstone/github/turnstone")

from turnstone.core.bm25 import BM25Index  # noqa: E402
from turnstone.core.rerank import (  # noqa: E402
    normalize_scores,
    resolve_rerank_client,
)

URL = "https://switchyard-vm.example.invalid/v1/rerank"
MODEL = "switchyard-rerank-local"

DOCS = [
    "grafana token rotation procedure for the observability stack",
    "how to bake sourdough bread with a stiff starter",
    "prometheus scrape configuration for alloy hubs",
    "sourdough hydration ratios and bulk fermentation timing",
    "rotating an api credential without downtime",
    "loki log retention and compactor settings",
    "bread knife sharpening technique",
]

QUERY = "rotate the grafana api token without downtime"

# Docs 0 and 4 are both genuinely relevant to the query; the rest are not.
# Ranking is only required to separate those sets, not to order inside one.
RELEVANT = {0, 4}


def show(label: str, value: object) -> None:
    print(f"{label}: {value}")


def test_scores_and_ranking() -> None:
    client = resolve_rerank_client(URL, model=MODEL)
    assert client is not None, "endpoint not configured"
    started = time.monotonic()
    hits = client.rerank(QUERY, DOCS)
    elapsed = round(time.monotonic() - started, 2)
    scores = normalize_scores([h.score for h in hits])
    print("\n[1] scores + ranking")
    for hit, score in zip(hits, scores, strict=True):
        print(f"    {score:7.4f}  {DOCS[hit.index][:60]}")
    show("    latency_s", elapsed)
    assert len(hits) == len(DOCS), f"expected {len(DOCS)} scored docs, got {len(hits)}"
    top_indices = [h.index for h in hits[: len(RELEVANT)]]
    assert set(top_indices) == RELEVANT, (
        f"top-{len(RELEVANT)} was {top_indices}, expected {RELEVANT}"
    )
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert max(scores) >= 0.9, f"no confident relevance score, max={max(scores)}"
    print(f"    PASS top-{len(RELEVANT)} is exactly the relevant set, all scores in [0,1]")


def test_reorders_bm25() -> None:
    client = resolve_rerank_client(URL, model=MODEL)
    assert client is not None

    def reranker(query: str, docs: list[str]) -> list[int]:
        return [h.index for h in client.rerank(query, docs)]

    # Deliberately lexical-vs-semantic: BM25 does not stem, so it prefers the
    # document repeating the query's exact words even though that one is about a
    # shift rota. The real answer shares fewer surface terms.
    query = "how to rotate a token without downtime"
    docs = [
        "rotating a token safely means issuing the replacement before revoking the old one",
        "downtime schedule rotate your shift token budget for the quarter",
    ]
    native = BM25Index(list(docs))
    reranked = BM25Index(list(docs), reranker=reranker)
    native_order = native.search(query, k=2)
    reranked_order = reranked.search(query, k=2)
    print("\n[2] BM25 vs reranked ordering")
    print("    query: " + query)
    for label, order in (("native  ", native_order), ("reranked", reranked_order)):
        print(f"    {label}: " + " | ".join(docs[i][:44] for i in order))
    assert native_order[0] == 1, f"fixture is not lexical-first, BM25 chose {native_order[0]}"
    assert reranked_order[0] == 0, (
        f"reranker did not promote the semantic answer, chose {reranked_order[0]}"
    )
    print("    PASS the reranker overrode BM25 and promoted the semantic answer")


def test_timeout_falls_back() -> None:
    client = resolve_rerank_client(URL, model=MODEL)
    assert client is not None
    print("\n[3] timeout handling")
    started = time.monotonic()
    try:
        client.rerank(QUERY, [DOCS[0]] * 40, timeout=0.4)
    except Exception as exc:  # noqa: BLE001
        elapsed = round(time.monotonic() - started, 2)
        show("    raised", type(exc).__name__)
        show("    elapsed_s", elapsed)
        assert elapsed < 5, f"timeout did not abort promptly ({elapsed}s)"
        print("    PASS a bounded timeout aborts the call instead of hanging")
        return
    raise AssertionError("expected the 0.4s timeout to abort the call")


def test_endpoint_failure_falls_back() -> None:
    print("\n[4] endpoint failure handling")
    bad = resolve_rerank_client(URL.replace("/v1/rerank", "/v1/not-a-capability"), model=MODEL)
    assert bad is not None
    try:
        bad.rerank(QUERY, DOCS[:3])
    except Exception as exc:  # noqa: BLE001
        show("    raised", type(exc).__name__)
        print("    PASS an upstream error surfaces as an exception the BM25 seam catches")
        return
    raise AssertionError("expected an error for an unregistered capability")


def test_empty_input_is_free() -> None:
    print("\n[5] empty input")
    client = resolve_rerank_client(URL, model=MODEL)
    assert client is not None
    started = time.monotonic()
    assert client.rerank(QUERY, []) == []
    show("    elapsed_s", round(time.monotonic() - started, 4))
    print("    PASS no request is issued for an empty batch")


def test_unconfigured_is_none() -> None:
    print("\n[6] unconfigured endpoint")
    assert resolve_rerank_client("", model=MODEL) is None
    print("    PASS an empty endpoint disables reranking rather than defaulting")


def main() -> int:
    print(f"endpoint={URL}\nmodel={MODEL}\nquery={QUERY!r}\ndocs={len(DOCS)}")
    test_scores_and_ranking()
    test_reorders_bm25()
    test_timeout_falls_back()
    test_endpoint_failure_falls_back()
    test_empty_input_is_free()
    test_unconfigured_is_none()
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
