"""Tests for turnstone.core.policy."""

import pytest

from turnstone.core.policy import (
    evaluate_tool_policies_batch,
    evaluate_tool_policy,
    invalidate_policy_cache,
)
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture
def storage(tmp_path):
    path = str(tmp_path / "test.db")
    backend = SQLiteBackend(path)
    yield backend
    backend.close()


def test_no_policies_returns_none(storage):
    result = evaluate_tool_policy(storage, "bash")
    assert result is None


def test_exact_match_allow(storage):
    storage.create_tool_policy("p1", "allow-read", "read_file", "allow", 0)
    assert evaluate_tool_policy(storage, "read_file") == "allow"
    assert evaluate_tool_policy(storage, "write_file") is None


def test_glob_match_deny(storage):
    storage.create_tool_policy("p1", "block-bash", "bash*", "deny", 0)
    assert evaluate_tool_policy(storage, "bash") == "deny"
    assert evaluate_tool_policy(storage, "bash_exec") == "deny"
    assert evaluate_tool_policy(storage, "read_file") is None


def test_wildcard_match(storage):
    storage.create_tool_policy("p1", "ask-all", "*", "ask", 0)
    assert evaluate_tool_policy(storage, "anything") == "ask"


def test_priority_ordering(storage):
    # Higher priority wins
    storage.create_tool_policy("p1", "allow-all", "*", "allow", 0)
    storage.create_tool_policy("p2", "deny-bash", "bash*", "deny", 100)
    assert evaluate_tool_policy(storage, "bash") == "deny"  # p2 matches first (higher priority)
    assert evaluate_tool_policy(storage, "read_file") == "allow"  # p1 matches


def test_disabled_policy_skipped(storage):
    storage.create_tool_policy("p1", "block-bash", "bash*", "deny", 100, enabled=False)
    storage.create_tool_policy("p2", "allow-all", "*", "allow", 0)
    assert evaluate_tool_policy(storage, "bash") == "allow"  # p1 disabled, falls through to p2


def test_batch_evaluation(storage):
    storage.create_tool_policy("p1", "block-bash", "bash*", "deny", 100)
    storage.create_tool_policy("p2", "allow-read", "read_*", "allow", 50)
    results = evaluate_tool_policies_batch(storage, ["bash", "read_file", "write_file"])
    assert results["bash"] == "deny"
    assert results["read_file"] == "allow"
    assert results["write_file"] is None


def test_storage_failure_returns_none():
    """Graceful degradation on storage failure."""

    class BrokenStorage:
        def list_tool_policies(self, org_id=""):
            raise RuntimeError("boom")

    assert evaluate_tool_policy(BrokenStorage(), "bash") is None


def test_batch_storage_failure():
    class BrokenStorage:
        def list_tool_policies(self, org_id=""):
            raise RuntimeError("boom")

    results = evaluate_tool_policies_batch(BrokenStorage(), ["a", "b"])
    assert results == {"a": None, "b": None}


def test_first_match_wins(storage):
    # Two policies match, first by priority wins
    storage.create_tool_policy("p1", "deny-bash", "bash*", "deny", 100)
    storage.create_tool_policy("p2", "allow-bash", "bash*", "allow", 50)
    assert evaluate_tool_policy(storage, "bash_exec") == "deny"


# ---------------------------------------------------------------------------
# Manual action
# ---------------------------------------------------------------------------


def test_manual_action_accepted(storage):
    """``manual`` is a valid policy action alongside allow/deny/ask."""
    storage.create_tool_policy("p1", "manual-gate", "mcp__*", "manual", 0)
    assert evaluate_tool_policy(storage, "mcp__server__tool") == "manual"
    assert evaluate_tool_policy(storage, "bash") is None


def test_manual_action_batch(storage):
    """Batch evaluation returns ``manual`` for matching tools."""
    storage.create_tool_policy("p1", "manual-gate", "mcp__*", "manual", 0)
    results = evaluate_tool_policies_batch(storage, ["mcp__server__tool", "bash"])
    assert results["mcp__server__tool"] == "manual"
    assert results["bash"] is None


def test_unknown_action_still_falls_back_to_ask(storage):
    """An unknown action retains the existing safe fallback to ``ask``."""
    # Storage does not validate action values; a row with a bogus action
    # must degrade to ``ask`` (never crash, never auto-approve).
    storage.create_tool_policy("p-unknown", "unknown", "*", "sneaky", 0)
    invalidate_policy_cache()
    assert evaluate_tool_policy(storage, "bash") == "ask"
    assert evaluate_tool_policies_batch(storage, ["bash"])["bash"] == "ask"


def test_manual_priority_first_match(storage):
    """Existing first-match priority semantics hold for manual:
    higher-priority allow beats lower-priority manual, and
    higher-priority manual beats lower-priority deny."""
    storage.create_tool_policy("p1", "manual-low", "bash*", "manual", 10)
    storage.create_tool_policy("p2", "allow-high", "bash*", "allow", 100)
    assert evaluate_tool_policy(storage, "bash") == "allow"

    storage2 = storage
    # Flip: high manual + low deny → manual (first match wins)
    storage2.create_tool_policy("p3", "deny-low", "bash*", "deny", 5)
    storage2.create_tool_policy("p4", "manual-high", "bash*", "manual", 200)
    invalidate_policy_cache()
    assert evaluate_tool_policy(storage2, "bash") == "manual"


# ---------------------------------------------------------------------------
# MCP resource and prompt policy patterns
# ---------------------------------------------------------------------------


def test_mcp_resource_wildcard_deny(storage):
    """Deny all MCP resource reads via glob pattern."""
    storage.create_tool_policy("p1", "block-resources", "mcp_resource__*", "deny", 100)
    assert evaluate_tool_policy(storage, "mcp_resource__file:///secret.txt") == "deny"
    assert evaluate_tool_policy(storage, "mcp_resource__db://users") == "deny"
    assert evaluate_tool_policy(storage, "read_file") is None  # unrelated tool


def test_mcp_resource_per_server_pattern(storage):
    """Allow resources from a specific server, deny others."""
    storage.create_tool_policy("p1", "block-all-resources", "mcp_resource__*", "deny", 50)
    storage.create_tool_policy("p2", "allow-docs", "mcp_resource__file:///docs/*", "allow", 100)
    assert evaluate_tool_policy(storage, "mcp_resource__file:///docs/readme.md") == "allow"
    assert evaluate_tool_policy(storage, "mcp_resource__file:///etc/passwd") == "deny"


def test_mcp_prompt_wildcard_ask(storage):
    """Require approval for all MCP prompt invocations."""
    storage.create_tool_policy("p1", "ask-prompts", "mcp__*", "ask", 100)
    assert evaluate_tool_policy(storage, "mcp__github__code_review") == "ask"
    assert evaluate_tool_policy(storage, "mcp__templates__greeting") == "ask"
    assert evaluate_tool_policy(storage, "bash") is None


def test_mcp_prompt_per_server_allow(storage):
    """Auto-approve prompts from a trusted server."""
    storage.create_tool_policy("p1", "ask-all-mcp", "mcp__*", "ask", 50)
    storage.create_tool_policy("p2", "allow-trusted", "mcp__trusted__*", "allow", 100)
    assert evaluate_tool_policy(storage, "mcp__trusted__greeting") == "allow"
    assert evaluate_tool_policy(storage, "mcp__untrusted__evil") == "ask"


def test_mcp_batch_mixed(storage):
    """Batch evaluation with mixed MCP and built-in tools."""
    storage.create_tool_policy("p1", "block-resources", "mcp_resource__*", "deny", 100)
    storage.create_tool_policy("p2", "allow-prompts", "mcp__trusted__*", "allow", 100)
    results = evaluate_tool_policies_batch(
        storage,
        ["mcp_resource__file:///x", "mcp__trusted__greeting", "bash", "mcp__other__y"],
    )
    assert results["mcp_resource__file:///x"] == "deny"
    assert results["mcp__trusted__greeting"] == "allow"
    assert results["bash"] is None
    assert results["mcp__other__y"] is None


def test_normalize_resource_uri_prevents_traversal():
    """URI normalization resolves .. segments to prevent policy traversal bypass."""
    from turnstone.core.session import ChatSession

    # Normal URI unchanged
    assert ChatSession._normalize_resource_uri("file:///docs/readme.md") == "file:///docs/readme.md"
    # Traversal resolved
    assert ChatSession._normalize_resource_uri("file:///docs/../etc/passwd") == "file:///etc/passwd"
    # Double traversal
    assert ChatSession._normalize_resource_uri("file:///a/b/../../c") == "file:///c"
    # Non-file scheme (netloc preserved, path normalized)
    assert ChatSession._normalize_resource_uri("db://host/tables/../secrets") == "db://host/secrets"
    # Percent-encoded traversal decoded before normalization
    assert (
        ChatSession._normalize_resource_uri("file:///docs/%2e%2e/etc/passwd")
        == "file:///etc/passwd"
    )
    # Mixed percent-encoded and literal traversal
    assert ChatSession._normalize_resource_uri("file:///a/%2e%2e/b/../c") == "file:///c"


def test_mcp_tool_granular_policy(storage):
    """MCP tool calls use their prefixed func_name for granular policy matching."""
    storage.create_tool_policy("p1", "ask-all-mcp", "mcp__*", "ask", 50)
    storage.create_tool_policy("p2", "allow-github", "mcp__github__*", "allow", 100)
    # MCP tools now use func_name as approval_label
    assert evaluate_tool_policy(storage, "mcp__github__search") == "allow"
    assert evaluate_tool_policy(storage, "mcp__untrusted__exec") == "ask"
