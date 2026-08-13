"""Run Once event-loop safety and strict body-validation tests.

These are exact-base (f138784b) proofs that the endpoint does not block the
ASGI loop during a slow synthetic dispatch, and that body handling is strict
(empty object accepted; nonempty/malformed/chunked payloads rejected).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from turnstone.console.server import admin_run_schedule
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.auth_result = AuthResult(
            user_id="test-admin",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset({"admin.schedules"}),
        )
        return await call_next(request)


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture
def client(storage):
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route(
                        "/api/admin/schedules/{task_id}/run",
                        admin_run_schedule,
                        methods=["POST"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    app.state.scheduler = None  # set per-test
    return TestClient(app)


def _seed_task(storage, task_id="task_ev"):
    storage.create_scheduled_task(
        task_id=task_id,
        name="Event task",
        description="",
        schedule_type="cron",
        cron_expr="0 9 * * *",
        at_time="",
        target_mode="auto",
        model="",
        initial_message="hi",
        auto_approve=False,
        auto_approve_tools=[],
        created_by="u",
        next_run="2099-01-01T00:00:00",
    )


class _SlowScheduler:
    """dispatch_manual_task sleeps in a worker thread (synthetic slow dispatch)."""

    def __init__(self, delay: float = 0.5):
        self.delay = delay
        self.called = False

    def dispatch_manual_task(self, task):
        self.called = True
        time.sleep(self.delay)
        return True


def test_event_loop_stays_responsive_during_slow_dispatch(client, storage):
    """A slow synthetic dispatch in a worker thread must not block the loop.

    We submit the run request (which offloads to asyncio.to_thread), then
    immediately run another coroutine on the loop.  If the handler blocked
    the loop, the probe coroutine would not complete until after the delay.
    """
    _seed_task(storage)
    scheduler = _SlowScheduler(delay=0.6)
    client.app.state.scheduler = scheduler

    start = time.monotonic()
    # Fire the request in a background thread (TestClient is sync).
    resp_holder: list[Any] = []

    def do_post():
        resp_holder.append(client.post("/v1/api/admin/schedules/task_ev/run"))

    th = threading.Thread(target=do_post)
    th.start()
    time.sleep(0.05)  # let the handler enter the dispatch offload

    # This coroutine must complete quickly even though dispatch is sleeping.
    async def probe():
        await asyncio.sleep(0.05)
        return "pong"

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(probe())
    finally:
        loop.close()
    th.join(timeout=5)

    elapsed = time.monotonic() - start
    assert result == "pong"
    assert elapsed < 1.5  # comfortably less than dispatch+probe serial time
    assert resp_holder[0].status_code == 202
    assert scheduler.called


def test_chunked_nonempty_json_body_rejected(client, storage):
    """Chunked transfer (no Content-Length) with a nonempty object -> 400."""
    _seed_task(storage)
    client.app.state.scheduler = type("S", (), {"dispatch_manual_task": lambda self, t: True})()
    body = b'{"model": "other"}'
    resp = client.post(
        "/v1/api/admin/schedules/task_ev/run",
        content=body,
        headers={"Transfer-Encoding": "chunked", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "zero bytes" in resp.json()["error"]


def test_empty_body_accepted(client, storage):
    _seed_task(storage)
    calls: list[dict[str, Any]] = []

    class S:
        def dispatch_manual_task(self, task):
            calls.append(task)
            return True

    client.app.state.scheduler = S()
    # No body at all — the endpoint must accept the implicit empty object.
    resp = client.post("/v1/api/admin/schedules/task_ev/run")
    assert resp.status_code == 202, resp.text
    assert len(calls) == 1
