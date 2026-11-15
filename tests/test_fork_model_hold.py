"""A fork whose source model isn't loaded (#1359).

The node's create path forks through the production hooks: with no model in the
request the fork is held on its source's model, as a reopen is; a request that
names a model switches the fork to it, and the create's reservation-checked
write records that model with its own sampling settings.
"""

from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING, Any

import pytest

from turnstone.core.model_registry import ModelConfig, ModelRegistry
from turnstone.core.model_turn import resolve_model_binding
from turnstone.core.session import ChatSession
from turnstone.core.storage._registry import get_storage

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.testclient import TestClient

    from turnstone.core.session_manager import SessionManager

_SOURCE = "f" * 32


@pytest.fixture
def fork_app(tmp_db: str) -> Iterator[tuple[TestClient, SessionManager, ModelRegistry]]:
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.routing import Mount, Route
    from starlette.testclient import TestClient

    from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
    from turnstone.core.auth import AuthResult
    from turnstone.core.session_manager import SessionManager
    from turnstone.core.session_routes import SessionEndpointConfig, make_create_handler
    from turnstone.server import (
        WebUI,
        _interactive_create_build_kwargs,
        _interactive_create_post_install,
        _interactive_create_pre_commit,
        _interactive_create_prepare_install,
        _interactive_create_validate_request,
        _interactive_manager_lookup,
        _interactive_tenant_check,
    )

    registry = ModelRegistry(
        models={
            "default": ModelConfig("default", "http://default.example/v1", "k", "d-model"),
            "chan": ModelConfig(
                "chan",
                "http://chan.example/v1",
                "k",
                "c-model",
                temperature=0.2,
                reasoning_effort="low",
                max_tokens=4000,
            ),
        },
        default="default",
    )
    # Store-backed, as the node's factory is: a fork's switch re-resolves the
    # sampling settings through the store's resolvers, as /model does.
    from turnstone.core.config_store import ConfigStore

    config_store = ConfigStore(get_storage())

    class _Auth(BaseHTTPMiddleware):
        async def dispatch(self, request: Any, call_next: Any) -> Any:
            request.state.auth_result = AuthResult(
                user_id="test-user",
                scopes=frozenset({"approve"}),
                token_source="config",
                permissions=frozenset({"read", "write", "approve"}),
            )
            return await call_next(request)

    def _session_factory(
        ui: Any, model_alias: str | None = None, ws_id: str | None = None, **kw: Any
    ) -> ChatSession:
        alias = model_alias or registry.default
        binding = resolve_model_binding(registry, alias)
        return ChatSession(
            client=binding.lane.client,
            model=binding.lane.model,
            ui=ui,
            instructions=None,
            temperature=0.5,
            max_tokens=1000,
            tool_timeout=10,
            ws_id=ws_id,
            user_id=getattr(ui, "_user_id", ""),
            registry=registry,
            model_alias=alias,
            model_binding=binding,
            config_store=config_store,
            persona_snapshot=kw.get("persona_snapshot"),
            workstream_lease=kw.get("workstream_lease"),
        )

    gq: queue.Queue[dict[str, Any]] = queue.Queue()
    WebUI._global_queue = gq
    adapter = InteractiveAdapter(
        global_queue=gq,
        ui_factory=lambda ws: WebUI(
            ws_id=ws.id,
            user_id=ws.user_id,
            kind=ws.kind,
            parent_ws_id=ws.parent_ws_id,
        ),
        session_factory=_session_factory,
    )
    mgr = SessionManager(
        adapter,
        storage=get_storage(),
        max_active=10,
        event_emitter=adapter,
        model_validator=registry.has_alias,
        default_alias_reader=lambda: registry.default,
    )
    handler = make_create_handler(
        SessionEndpointConfig(
            permission_gate=None,
            manager_lookup=_interactive_manager_lookup,
            tenant_check=_interactive_tenant_check,
            not_found_label="Workstream not found",
            audit_action_prefix="workstream",
            create_supports_attachments=True,
            create_supports_user_id_override=True,
            create_supports_resume=True,
            create_validate_request=_interactive_create_validate_request,
            create_build_kwargs=_interactive_create_build_kwargs,
            create_pre_commit=_interactive_create_pre_commit,
            create_post_install=_interactive_create_post_install,
            create_prepare_install=_interactive_create_prepare_install,
        )
    )
    app = Starlette(
        routes=[Mount("/v1", routes=[Route("/api/workstreams/new", handler, methods=["POST"])])],
        middleware=[Middleware(_Auth)],
    )
    app.state.workstreams = mgr
    app.state.skip_permissions = True
    app.state.global_queue = gq
    app.state.global_listeners = []
    app.state.global_listeners_lock = threading.Lock()
    try:
        yield TestClient(app, raise_server_exceptions=False), mgr, registry
    finally:
        for ws in mgr.list_all():
            mgr.close(ws.id)
        WebUI._global_queue = None


def _seed_source() -> None:
    from turnstone.core.memory import register_workstream, save_message, save_workstream_config

    register_workstream(_SOURCE, user_id="test-user")
    save_message(_SOURCE, "user", "earlier")
    save_workstream_config(
        _SOURCE,
        {
            "model_alias": "gone",
            "model": "gone-model",
            "temperature": "0.9",
            "reasoning_effort": "max",
            "max_tokens": "123",
        },
    )


def test_a_fork_without_a_model_is_held_on_its_sources(
    fork_app: tuple[TestClient, SessionManager, ModelRegistry],
) -> None:
    client, mgr, _registry = fork_app
    _seed_source()

    resp = client.post("/v1/api/workstreams/new", json={"resume_ws": _SOURCE})

    assert resp.status_code == 200, resp.text
    ws_id = resp.json()["ws_id"]
    ws = mgr.get(ws_id)
    assert ws is not None and ws.session is not None
    assert ws.session.model_held
    assert ws.session.model_alias == "gone"
    stored = get_storage().load_workstream_config(ws_id)
    assert stored["model_alias"] == "gone"
    # The held model's own settings stay, for when it loads again.
    assert (stored["temperature"], stored["reasoning_effort"], stored["max_tokens"]) == (
        "0.9",
        "max",
        "123",
    )


def test_a_fork_naming_a_model_switches_to_it(
    fork_app: tuple[TestClient, SessionManager, ModelRegistry],
) -> None:
    client, mgr, _registry = fork_app
    _seed_source()

    resp = client.post("/v1/api/workstreams/new", json={"resume_ws": _SOURCE, "model": "chan"})

    assert resp.status_code == 200, resp.text
    ws_id = resp.json()["ws_id"]
    ws = mgr.get(ws_id)
    assert ws is not None and ws.session is not None
    assert not ws.session.model_held
    assert ws.session.model_alias == "chan"
    assert ws.session.reasoning_effort == "low"
    stored = get_storage().load_workstream_config(ws_id)
    assert (stored["model_alias"], stored["model"]) == ("chan", "c-model")
    # The named model's own settings, never the source's (#1272).
    assert (stored["temperature"], stored["reasoning_effort"], stored["max_tokens"]) == (
        "0.2",
        "low",
        "4000",
    )
    # The rest of the clone stays the source's.
    assert [m.text for m in ws.session.messages][0] == "earlier"


def test_the_switch_needs_no_reopen_to_stick(
    fork_app: tuple[TestClient, SessionManager, ModelRegistry],
) -> None:
    """A reopen of the switched fork binds the model the create recorded."""
    client, mgr, _registry = fork_app
    _seed_source()
    resp = client.post("/v1/api/workstreams/new", json={"resume_ws": _SOURCE, "model": "chan"})
    assert resp.status_code == 200, resp.text
    ws_id = resp.json()["ws_id"]
    mgr.close(ws_id)

    reopened = mgr.open(ws_id)

    assert reopened is not None and reopened.session is not None
    assert not reopened.session.model_held
    assert reopened.session.model_alias == "chan"
    assert (
        reopened.session.temperature,
        reopened.session.reasoning_effort,
        reopened.session.max_tokens,
    ) == (0.2, "low", 4000)


def test_a_fork_whose_model_is_out_only_for_now_holds_despite_its_request(
    fork_app: tuple[TestClient, SessionManager, ModelRegistry],
) -> None:
    """The source's model was left out because its secret store didn't answer,
    so it may load by itself: the fork waits for it instead of moving to the
    requested model for good, as a Slack DM's recovery would (#1359)."""
    client, mgr, registry = fork_app
    _seed_source()
    registry.retryable_skips = frozenset({"gone"})

    resp = client.post("/v1/api/workstreams/new", json={"resume_ws": _SOURCE, "model": "chan"})

    assert resp.status_code == 200, resp.text
    ws_id = resp.json()["ws_id"]
    ws = mgr.get(ws_id)
    assert ws is not None and ws.session is not None
    assert ws.session.model_held
    assert ws.session.model_alias == "gone"
    stored = get_storage().load_workstream_config(ws_id)
    assert (stored["model_alias"], stored["reasoning_effort"]) == ("gone", "max")
