"""Edit Model form round-trip: both JSON textareas must survive every save.

Issue #19: a stored row lost ``server_compat.extra_body`` and the advanced
capability keys.  The form lifts structured capabilities out of the raw JSON
on edit-load (``server_compat``, ``thinking_*``, ``effort_param``, rerank
calibration) and re-merges them on save; anything lifted but not re-merged is
dropped by an *unrelated* save, silently.

The JavaScript cases run the real ``admin.js`` under node (see
``tests/_admin_model_form_js.py``): each one asserts on the PUT body the form
actually emits, not on source text.  The server cases pin the other half of
the contract — capabilities are persisted verbatim (literal ``false``
included) and the audit row records what the form sent.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests._admin_model_form_js import node_available, run_scenarios

if TYPE_CHECKING:
    from turnstone.core.storage._sqlite import SQLiteBackend

# A synthetic structural copy of the reported row (issue #19 / Switchyard #33):
# openai-compatible on the Responses surface, tile capabilities on, and the
# base-template extras NOT materialised in the row.
ROW: dict[str, Any] = {
    "definition_id": "def-1",
    "alias": "smart-bounded",
    "model": "gpt-6-luna",
    "provider": "openai-compatible",
    "base_url": "http://switchyard.internal/v1",
    "context_window": 262144,
    "max_concurrency": 0,
    "temperature": None,
    "max_tokens": None,
    "reasoning_effort": "none",
    "capabilities": json.dumps(
        {
            "supports_tools": True,
            "supports_vision": True,
            "supports_pdf": True,
            "supports_web_search": True,
            "supports_effort": True,
            "server_parses_reasoning": True,
            "server_compat": {"api_surface": "responses"},
        }
    ),
    "enabled": True,
    "surface_persisted_reasoning": True,
    "replay_reasoning_to_model": False,
    "auth_mode": "static",
    "obo_audience": "",
    "obo_scopes": "",
}

ADVANCED_CAPS = json.dumps(
    {
        "max_output_tokens": 32000,
        "reasoning_effort_values": ["low", "high"],
        "default_reasoning_effort": "low",
    },
    indent=2,
)
EXTRA_BODY = json.dumps({"skip_special_tokens": False, "nested": {"off": False}}, indent=2)

#: Tile keys the reported row pins to true.
TRUE_TILES = (
    "supports_tools",
    "supports_vision",
    "supports_pdf",
    "supports_web_search",
    "supports_effort",
    "server_parses_reasoning",
)


def _with_compat(**overrides: Any) -> dict[str, Any]:
    """A row whose ``server_compat`` carries an uneditable key and an extra body."""
    row = json.loads(json.dumps(ROW))
    caps = json.loads(row["capabilities"])
    caps["server_compat"] = {
        "server_type": "vllm",
        "api_surface": "responses",
        "extra_body": {"skip_special_tokens": False},
    }
    caps.update(overrides.pop("caps", {}))
    row["capabilities"] = json.dumps(caps)
    row.update(overrides)
    return row


def _only_request(result: dict[str, Any]) -> dict[str, Any]:
    assert len(result["requests"]) == 1, (
        f"expected exactly one save request, got {result['requests']}"
    )
    return result["requests"][0]["body"]


needs_node = pytest.mark.skipif(not node_available(), reason="node not available")


@needs_node
class TestEditSaveWithoutTyping:
    """Opening a row and saving an unrelated field must change nothing."""

    def test_tiles_and_server_compat_round_trip(self) -> None:
        (result,) = run_scenarios([{"row": ROW}])
        caps = _only_request(result)["capabilities"]

        for key in TRUE_TILES:
            assert caps[key] is True, f"{key} flipped on a no-op save"
        assert caps["server_compat"] == {"api_surface": "responses"}

        body = _only_request(result)
        assert body["context_window"] == 262144
        assert body["reasoning_effort"] == "none"
        assert body["provider"] == "openai-compatible"

    def test_untyped_textareas_stay_empty_and_invent_nothing(self) -> None:
        (result,) = run_scenarios([{"row": ROW}])
        caps = _only_request(result)["capabilities"]
        assert "extra_body" not in caps["server_compat"]
        for key in ("max_output_tokens", "reasoning_effort_values", "default_reasoning_effort"):
            assert key not in caps


@needs_node
class TestTypedJSONReachesTheWire:
    def test_both_textareas_are_serialised(self) -> None:
        (result,) = run_scenarios(
            [{"row": ROW, "typed_capabilities": ADVANCED_CAPS, "typed_extra_body": EXTRA_BODY}]
        )
        caps = _only_request(result)["capabilities"]

        assert caps["max_output_tokens"] == 32000
        assert caps["reasoning_effort_values"] == ["low", "high"]
        assert caps["default_reasoning_effort"] == "low"
        # Literal JSON false and nesting survive — no truthiness or flattening.
        assert caps["server_compat"]["extra_body"] == {
            "skip_special_tokens": False,
            "nested": {"off": False},
        }

    def test_invalid_extra_body_json_blocks_the_save(self) -> None:
        (result,) = run_scenarios([{"row": ROW, "typed_extra_body": "{not json"}])
        assert result["requests"] == [], "an unparseable extra body must not be sent"
        assert result["error"]

    def test_invalid_capabilities_json_blocks_the_save(self) -> None:
        (result,) = run_scenarios([{"row": ROW, "typed_capabilities": "[1, 2]"}])
        assert result["requests"] == [], "a non-object capabilities blob must not be sent"
        assert result["error"]


@needs_node
class TestStoredStructuredKeysAreNeverDiscarded:
    """Keys the selected lane cannot edit must round-trip, not vanish."""

    def test_provider_switch_to_non_compat_keeps_server_compat(self) -> None:
        (result,) = run_scenarios([{"row": _with_compat(), "provider": "openai"}])
        caps = _only_request(result)["capabilities"]
        assert caps["server_compat"] == {
            "server_type": "vllm",
            "api_surface": "responses",
            "extra_body": {"skip_special_tokens": False},
        }

    def test_anthropic_compatible_keeps_keys_it_cannot_edit(self) -> None:
        (result,) = run_scenarios([{"row": _with_compat(), "provider": "anthropic-compatible"}])
        caps = _only_request(result)["capabilities"]
        # The lane renders only the Extra JSON box; server_type/api_surface
        # have no control, so the stored values must survive verbatim.
        assert caps["server_compat"]["server_type"] == "vllm"
        assert caps["server_compat"]["api_surface"] == "responses"
        assert caps["server_compat"]["extra_body"] == {"skip_special_tokens": False}

    def test_openai_compatible_lane_can_clear_an_extra_body(self) -> None:
        """Round-tripping must not make an editable key unclearable."""
        (result,) = run_scenarios([{"row": _with_compat(), "typed_extra_body": ""}])
        caps = _only_request(result)["capabilities"]
        assert "extra_body" not in caps["server_compat"]
        assert caps["server_compat"]["api_surface"] == "responses"

    def test_openai_compatible_lane_can_clear_a_server_type(self) -> None:
        """An emptied control on the lane that renders it means "removed"."""
        (result,) = run_scenarios([{"row": _with_compat(), "server_type": ""}])
        caps = _only_request(result)["capabilities"]
        assert "server_type" not in caps["server_compat"]
        assert caps["server_compat"]["api_surface"] == "responses"

    def test_create_form_does_not_inherit_a_previous_rows_server_compat(self) -> None:
        """A new row must not adopt the last edited row's structured keys.

        The provider is deliberately one that renders NO server-compat
        controls: with everything editable the emptied fields would clear the
        keys by themselves, which would mask a leaked lift-then-restore stash.
        """
        (result,) = run_scenarios(
            [
                {
                    "row": _with_compat(),
                    "then_create": True,
                    "alias": "fresh",
                    "model": "m",
                    "provider": "openai",
                }
            ]
        )
        caps = _only_request(result)["capabilities"]
        assert "server_compat" not in caps


@needs_node
class TestReloadRoundTripIsStable:
    def test_a_saved_row_re_saves_identically(self) -> None:
        """Feed the emitted payload back as the stored row, as a reload would."""
        (first,) = run_scenarios(
            [{"row": ROW, "typed_capabilities": ADVANCED_CAPS, "typed_extra_body": EXTRA_BODY}]
        )
        saved = _only_request(first)
        reloaded = dict(ROW)
        reloaded["capabilities"] = json.dumps(saved["capabilities"])

        (second,) = run_scenarios([{"row": reloaded}])
        assert _only_request(second)["capabilities"] == saved["capabilities"]


class TestServerPersistsWhatTheFormSent:
    """The server half: verbatim storage plus an audit trail of the payload."""

    def _client(self, storage: SQLiteBackend):
        from tests.test_admin_model_registry_refresh import _make_client

        return _make_client(storage, registry=None)

    def test_capabilities_are_persisted_verbatim(self, storage: SQLiteBackend) -> None:
        client = self._client(storage)
        created = client.post(
            "/v1/api/admin/model-definitions",
            json={
                "alias": "roundtrip",
                "model": "m",
                "provider": "openai-compatible",
                "capabilities": {},
            },
        )
        assert created.status_code == 200, created.text
        definition_id = created.json()["definition_id"]

        caps = {
            "supports_tools": True,
            "max_output_tokens": 32000,
            "reasoning_effort_values": ["low", "high"],
            "default_reasoning_effort": "low",
            "server_compat": {
                "api_surface": "responses",
                "extra_body": {"skip_special_tokens": False},
            },
        }
        updated = client.put(
            f"/v1/api/admin/model-definitions/{definition_id}",
            json={"capabilities": caps},
        )
        assert updated.status_code == 200, updated.text

        read_back = json.loads(storage.get_model_definition(definition_id)["capabilities"])
        assert read_back == caps, "literal false or nested keys were not stored verbatim"

    def test_audit_row_records_the_submitted_capabilities(self, storage: SQLiteBackend) -> None:
        client = self._client(storage)
        created = client.post(
            "/v1/api/admin/model-definitions",
            json={"alias": "audited", "model": "m", "capabilities": {}},
        )
        definition_id = created.json()["definition_id"]
        caps = {"server_compat": {"extra_body": {"skip_special_tokens": False}}}
        client.put(
            f"/v1/api/admin/model-definitions/{definition_id}",
            json={"capabilities": caps},
        )

        events = storage.list_audit_events(action="model_definition.update")
        assert events, "the save must leave an audit row"
        detail = json.loads(events[0]["detail"])
        assert json.loads(detail["capabilities"]) == caps
