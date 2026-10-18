"""Correlation identity for gateway-hosted lanes.

These tests pin the boundary as much as the behaviour: what is sent, to whom,
and what is never sent.
"""

from __future__ import annotations

import re

from turnstone.core import correlation


IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def test_no_allowlist_means_no_headers(monkeypatch):
    monkeypatch.delenv(correlation.HOSTS_ENV, raising=False)
    correlation.reset()
    correlation.start_session("ws-1")
    correlation.start_turn("turn-1")
    correlation.start_operation("op-1")
    assert correlation.headers_for("https://switchyard.example/v1") is None


def test_a_host_outside_the_allowlist_never_receives_headers(monkeypatch):
    monkeypatch.setenv(correlation.HOSTS_ENV, "gateway.internal")
    correlation.start_session("ws-1")
    correlation.start_turn("turn-1")
    correlation.start_operation("op-1")
    # A direct provider is not the gateway: no Turnstone-specific headers.
    assert correlation.headers_for("https://api.openai.com/v1") is None
    assert correlation.headers_for("http://127.0.0.1:8000/v1") is None


def test_an_allowlisted_host_receives_every_identifier():
    correlation.start_session("ws-abc")
    correlation.start_turn("turn-abc")
    correlation.start_operation("op-abc")
    headers = correlation.headers_for(
        "https://gateway.internal/v1", hosts=frozenset({"gateway.internal"})
    )
    assert headers == {
        correlation.ORIGIN_HEADER: "turnstone",
        correlation.SESSION_HEADER: "ws-abc",
        correlation.TURN_HEADER: "turn-abc",
        correlation.REQUEST_HEADER: "op-abc",
    }


def test_host_matching_accepts_a_port_and_ignores_case():
    correlation.start_session("ws-abc")
    correlation.start_turn("turn-abc")
    correlation.start_operation("op-abc")
    hosts = frozenset({"gateway.internal:8443"})
    assert correlation.headers_for("https://GATEWAY.internal:8443/v1", hosts=hosts)
    # A different port is a different endpoint: the allowlist entry is exact.
    assert correlation.headers_for("https://gateway.internal/v1", hosts=hosts) is None


def test_minted_identifiers_are_opaque_hex():
    correlation.start_session()
    correlation.start_turn()
    correlation.start_operation()
    session_id, turn_id, request_id = correlation.current()
    for value in (session_id, turn_id, request_id):
        assert re.fullmatch(r"[0-9a-f]{32}", value), value
    # None of them may echo anything an operator typed.
    assert IPV4.match(session_id) is None


def test_the_operation_id_is_stable_across_retries_of_one_operation():
    """A retried request must stay one operation, not become several."""
    correlation.start_session("ws-abc")
    correlation.start_turn("turn-abc")
    correlation.start_operation("op-abc")
    hosts = frozenset({"gateway.internal"})
    first = correlation.headers_for("https://gateway.internal/v1", hosts=hosts)
    # Every retry re-derives headers from the same bound operation id.
    for _ in range(3):
        again = correlation.headers_for("https://gateway.internal/v1", hosts=hosts)
        assert again == first


def test_a_new_operation_changes_only_the_request_id():
    correlation.start_session("ws-abc")
    correlation.start_turn("turn-abc")
    hosts = frozenset({"gateway.internal"})
    correlation.start_operation("op-1")
    first = correlation.headers_for("https://gateway.internal/v1", hosts=hosts)
    correlation.start_operation("op-2")
    second = correlation.headers_for("https://gateway.internal/v1", hosts=hosts)
    assert first[correlation.SESSION_HEADER] == second[correlation.SESSION_HEADER]
    assert first[correlation.TURN_HEADER] == second[correlation.TURN_HEADER]
    assert first[correlation.REQUEST_HEADER] != second[correlation.REQUEST_HEADER]


def test_nothing_is_sent_without_a_bound_identity():
    correlation.reset()
    assert correlation.headers_for(
        "https://gateway.internal/v1", hosts=frozenset({"gateway.internal"})
    ) is None


def test_the_sensor_state_fingerprint_is_untouched_by_correlation():
    """Correlation must not become sensor content."""
    from turnstone.core.shadow_observation import state_fingerprint

    state = {"objective": "do the thing", "phase": "operator_instruction"}
    before = state_fingerprint(state)
    correlation.start_session("ws-abc")
    correlation.start_turn("turn-abc")
    correlation.start_operation("op-abc")
    correlation.headers_for(
        "https://gateway.internal/v1", hosts=frozenset({"gateway.internal"})
    )
    assert state_fingerprint(state) == before
