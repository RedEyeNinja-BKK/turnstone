# SPDX-License-Identifier: Apache-2.0
"""Direct-OAuth (SIWC) Responses request profile.

The direct ChatGPT OAuth lane accepts a narrower request than an API-key
Responses endpoint.  The provider must therefore:

* send ``store=false`` (no server-side persistence for a subscriber account);
* stream (a non-streamed call is not the supported shape on this lane);
* carry the full conversation in ``input``;
* never emit ``tool_search`` or a hosted ``web_search`` tool;
* never emit a system-role *input item* (instructions go top-level).

``store`` and ``stream`` are already the provider's defaults.  These tests pin
them, and pin that the profile is expressible through EXISTING capability flags
rather than a new bespoke mechanism.

Run standalone::

    TMPDIR=/var/tmp PYTHONPATH=<tree> /opt/turnstone-venv/bin/python tests/test_direct_oauth_request_profile.py
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from turnstone.core.providers._openai_responses import OpenAIResponsesProvider  # noqa: E402
from turnstone.core.providers._protocol import ModelCapabilities  # noqa: E402

# The posture a direct-OAuth alias must declare.
DIRECT_OAUTH_CAPS = ModelCapabilities(
    supports_web_search=False,
    supports_tool_search=False,
    supports_mid_conversation_system=False,
    supports_reasoning_replay=False,
)


def _provider() -> OpenAIResponsesProvider:
    # Construct normally: __init__ sets the API-compat flag the builder reads.
    return OpenAIResponsesProvider()


def _kwargs(caps: ModelCapabilities, messages: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    return _provider()._build_kwargs(
        model="gpt-6.1-sol",
        messages=messages,
        tools=over.pop("tools", None),
        max_tokens=1024,
        temperature=None,
        reasoning_effort="low",
        deferred_names=over.pop("deferred_names", None),
        capabilities=caps,
        **over,
    )


def test_store_is_false() -> None:
    kwargs = _kwargs(DIRECT_OAUTH_CAPS, [{"role": "user", "content": "hi"}])
    assert kwargs["store"] is False, "direct OAuth must never persist a response server-side"


def test_full_conversation_is_in_input() -> None:
    kwargs = _kwargs(
        DIRECT_OAUTH_CAPS,
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ],
    )
    blob = str(kwargs["input"])
    assert "first" in blob and "second" in blob and "third" in blob


def test_no_system_role_input_item() -> None:
    kwargs = _kwargs(
        DIRECT_OAUTH_CAPS,
        [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "hi"},
        ],
    )
    assert kwargs.get("instructions") == "You are terse."
    for item in kwargs["input"]:
        assert item.get("role") != "system", "system must be top-level instructions"
        assert item.get("role") != "developer"


def test_no_tool_search_tool() -> None:
    kwargs = _kwargs(
        DIRECT_OAUTH_CAPS,
        [{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "local_tool", "description": "x", "parameters": {}},
                "defer_loading": True,
            }
        ],
        deferred_names=frozenset({"local_tool"}),
    )
    types = [t.get("type") for t in kwargs.get("tools", [])]
    assert "tool_search" not in types, "direct OAuth must not request the hosted tool_search tool"


def test_no_hosted_web_search_tool() -> None:
    kwargs = _kwargs(
        DIRECT_OAUTH_CAPS,
        [{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "web_search", "description": "x", "parameters": {}},
            }
        ],
    )
    types = [t.get("type") for t in kwargs.get("tools", [])]
    assert "web_search" not in types, "client-side search stays client-side on this lane"
    assert "function" in types, "compatible client-side tools must survive"


def test_client_side_tools_are_preserved() -> None:
    kwargs = _kwargs(
        DIRECT_OAUTH_CAPS,
        [{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "shell", "description": "run", "parameters": {}},
            }
        ],
    )
    names = [t.get("name") for t in kwargs.get("tools", []) if t.get("type") == "function"]
    assert "shell" in names


def test_stream_is_forced_on_the_call_path() -> None:
    """The provider sets ``stream=True`` at the call site, not in kwargs."""
    import inspect

    source = inspect.getsource(OpenAIResponsesProvider)
    assert "stream=True" in source
    assert source.count("stream=True") >= 2


def _run_all() -> int:
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_") and callable(o)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n  {passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
