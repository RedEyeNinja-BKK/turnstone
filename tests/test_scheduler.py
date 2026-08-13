"""Tests for turnstone.console.scheduler — TaskScheduler tick and dispatch."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from turnstone.console.scheduler import TaskScheduler
from turnstone.sdk._types import TurnstoneAPIError


def _wire_lock_storage(storage: MagicMock, acquired: bool = True) -> None:
    """Configure *storage* mock for the atomic scheduler leadership lease.

    The scheduler now calls ``try_acquire_scheduler_lock`` once and trusts
    the storage engine's atomic ``INSERT ... ON CONFLICT`` result — there is
    no read-modify-write and no read-back.  ``acquired=False`` simulates
    another console holding a non-expired lease.
    """
    storage.try_acquire_scheduler_lock.return_value = acquired
    storage.release_scheduler_lock.return_value = True


@pytest.fixture
def mocks():
    """Collector and storage mocks for scheduler tests."""
    collector = MagicMock()
    storage = MagicMock()
    # Default: lock acquisition succeeds
    _wire_lock_storage(storage, acquired=True)
    # Dispatch claims are free by default
    storage.claim_scheduled_task_execution.return_value = True
    storage.renew_scheduled_task_execution.return_value = True
    storage.release_scheduled_task_execution.return_value = True
    return collector, storage


def _make_task(**overrides):
    """Build a minimal task dict matching storage row format."""
    defaults = {
        "task_id": "task_001",
        "name": "Test task",
        "description": "",
        "schedule_type": "cron",
        "cron_expr": "0 9 * * *",
        "at_time": "",
        "target_mode": "auto",
        "model": "gpt-5",
        "initial_message": "Run the tests",
        "auto_approve": 0,
        "auto_approve_tools": "",
        "enabled": 1,
        "created_by": "u_admin",
        "next_run": "2020-01-01T09:00:00",
        "last_run": "",
        "created": "2020-01-01T00:00:00",
        "updated": "2020-01-01T00:00:00",
    }
    defaults.update(overrides)
    return defaults


def _make_node(node_id="node-001", reachable=True, ws_total=2, max_ws=10):
    """Build a minimal node dict matching collector output."""
    return {
        "node_id": node_id,
        "reachable": reachable,
        "ws_total": ws_total,
        "max_ws": max_ws,
    }


def _mock_create_response(ws_id: str = "ws_abc123") -> MagicMock:
    """Build a mock CreateWorkstreamResponse with the given ws_id."""
    resp = MagicMock()
    resp.ws_id = ws_id
    return resp


class TestSchedulerTick:
    """Tests for _tick() lock acquisition and dispatch logic."""

    def test_tick_acquires_lock(self, mocks):
        collector, storage = mocks
        storage.list_due_tasks.return_value = []

        scheduler = TaskScheduler(collector, storage)
        scheduler._tick()

        storage.try_acquire_scheduler_lock.assert_called()
        storage.release_scheduler_lock.assert_called()
        storage.list_due_tasks.assert_called_once()

    def test_tick_skips_when_locked(self, mocks):
        collector, storage = mocks
        # Another instance holds a non-expired lease — storage says False.
        _wire_lock_storage(storage, acquired=False)

        scheduler = TaskScheduler(collector, storage)
        scheduler._tick()

        storage.list_due_tasks.assert_not_called()

    def test_tick_takes_expired_lock(self, mocks):
        """An expired lease from another instance is reclaimed by storage."""
        collector, storage = mocks
        storage.list_due_tasks.return_value = []

        scheduler = TaskScheduler(collector, storage)
        scheduler._tick()

        storage.list_due_tasks.assert_called_once()

    def test_dispatch_auto_mode(self, mocks):
        collector, storage = mocks

        task = _make_task(target_mode="auto")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        mock_create.assert_called_once()
        storage.record_task_run.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["node_id"] == "node-001"
        assert run_kwargs["status"] == "dispatched"
        assert run_kwargs["ws_id"] == "ws_abc123"

    def test_dispatch_passes_persona_and_project(self, mocks):
        """persona + project_id ride to create_workstream; created_by becomes
        the user_id the node gates the project attach against."""
        collector, storage = mocks

        task = _make_task(persona="researcher", project_id="proj_42")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {"server_url": "http://node-001:8080"}

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["persona"] == "researcher"
        assert call_kwargs["project_id"] == "proj_42"
        assert call_kwargs["user_id"] == "u_admin"

    def test_dispatch_defaults_persona_project_empty(self, mocks):
        """A task row without persona/project keys dispatches with empty
        strings — the node then resolves the current kind default / no attach."""
        collector, storage = mocks

        task = _make_task()
        task.pop("persona", None)
        task.pop("project_id", None)
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {"server_url": "http://node-001:8080"}

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["persona"] == ""
        assert call_kwargs["project_id"] == ""

    def test_dispatch_pool_mode(self, mocks):
        collector, storage = mocks

        task = _make_task(target_mode="pool")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node("node-001")], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        mock_create.assert_called_once()
        storage.record_task_run.assert_called_once()

    def test_dispatch_all_mode(self, mocks):
        collector, storage = mocks

        task = _make_task(target_mode="all")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = (
            [_make_node("node-001"), _make_node("node-002")],
            2,
        )
        collector.get_node_detail.side_effect = lambda nid: {
            "server_url": f"http://{nid}:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        assert mock_create.call_count == 2
        assert storage.record_task_run.call_count == 2

    def test_dispatch_specific_node(self, mocks):
        collector, storage = mocks

        task = _make_task(target_mode="node-001")
        storage.list_due_tasks.return_value = [task]
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        mock_create.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["node_id"] == "node-001"

    def test_at_task_disables_after_dispatch(self, mocks):
        collector, storage = mocks

        task = _make_task(schedule_type="at", cron_expr="", at_time="2099-01-01T00:00:00")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ):
            scheduler._tick()

        # At-task should be disabled after dispatch
        update_calls = storage.update_scheduled_task.call_args_list
        assert len(update_calls) == 1
        args, kwargs = update_calls[0]
        assert args[0] == "task_001"
        assert kwargs["enabled"] is False
        assert kwargs["next_run"] == ""

    def test_cron_task_updates_next_run(self, mocks):
        collector, storage = mocks

        task = _make_task(schedule_type="cron", cron_expr="0 9 * * *")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ):
            scheduler._tick()

        update_calls = storage.update_scheduled_task.call_args_list
        assert len(update_calls) == 1
        _, kwargs = update_calls[0]
        assert kwargs["next_run"] != ""
        assert "enabled" not in kwargs  # cron tasks stay enabled

    def test_no_reachable_nodes_records_failure(self, mocks):
        collector, storage = mocks

        task = _make_task(target_mode="auto")
        storage.list_due_tasks.return_value = [task]
        # No reachable nodes
        collector.get_nodes.return_value = (
            [_make_node("node-001", reachable=False)],
            1,
        )

        scheduler = TaskScheduler(collector, storage)
        scheduler._tick()

        storage.record_task_run.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["status"] == "failed"
        assert run_kwargs["error"] != ""

    def test_failure_does_not_advance_schedule(self, mocks):
        """When dispatch fails, last_run/next_run should not be updated."""
        collector, storage = mocks

        task = _make_task(target_mode="auto")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([], 0)  # no nodes at all

        scheduler = TaskScheduler(collector, storage)
        scheduler._tick()

        # update_scheduled_task should NOT be called (no last_run/next_run advance)
        storage.update_scheduled_task.assert_not_called()

    def test_fan_out_capped(self, mocks):
        """Fan-out 'all' mode should respect max_fan_out limit."""
        collector, storage = mocks

        task = _make_task(target_mode="all")
        storage.list_due_tasks.return_value = [task]
        # 10 reachable nodes but max_fan_out=3
        nodes = [_make_node(f"node-{i:03d}") for i in range(10)]
        collector.get_nodes.return_value = (nodes, 10)
        collector.get_node_detail.side_effect = lambda nid: {
            "server_url": f"http://{nid}:8080",
        }

        scheduler = TaskScheduler(collector, storage, max_fan_out=3)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        assert mock_create.call_count == 3
        assert storage.record_task_run.call_count == 3

    def test_specific_node_target(self, mocks):
        """Non-enum target_mode is treated as a specific node_id."""
        collector, storage = mocks

        task = _make_task(target_mode="node-custom-123")
        storage.list_due_tasks.return_value = [task]
        collector.get_node_detail.return_value = {
            "server_url": "http://node-custom-123:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        mock_create.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["node_id"] == "node-custom-123"

    def test_user_id_in_dispatched_call(self, mocks):
        """Dispatched SDK call should include created_by as user_id."""
        collector, storage = mocks

        task = _make_task(target_mode="auto", created_by="u_scheduler_admin")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            scheduler._tick()

        _, kwargs = mock_create.call_args
        assert kwargs["user_id"] == "u_scheduler_admin"

    def test_sdk_failure_records_failure(self, mocks):
        """SDK errors during dispatch should record a failure."""
        collector, storage = mocks

        task = _make_task(target_mode="auto")
        storage.list_due_tasks.return_value = [task]
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {
            "server_url": "http://node-001:8080",
        }

        scheduler = TaskScheduler(collector, storage)
        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            side_effect=TurnstoneAPIError(502, "Bad Gateway"),
        ):
            scheduler._tick()

        storage.record_task_run.assert_called_once()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["status"] == "failed"


class TestManualSchedulerRun:
    """Tests for TaskScheduler.dispatch_manual_task() (Run Once).

    Manual runs must reuse the normal scheduler dispatch path, preserve the
    stored schedule definition (enabled/next_run/last_run/cadence), and
    record ``trigger="manual"`` in run history.
    """

    def test_disabled_manual_run_uses_dispatch_path_and_preserves_schedule(self, mocks):
        collector, storage = mocks
        task = _make_task(enabled=0, next_run="", target_mode="auto")
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {"server_url": "http://node-001:8080"}
        scheduler = TaskScheduler(collector, storage)

        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ) as mock_create:
            assert scheduler.dispatch_manual_task(task) is True

        mock_create.assert_called_once()
        storage.update_scheduled_task.assert_not_called()
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["trigger"] == "manual"
        assert run_kwargs["status"] == "dispatched"
        # The durable claim was acquired and released around dispatch.
        storage.claim_scheduled_task_execution.assert_called_once()
        storage.release_scheduled_task_execution.assert_called_once()

    def test_manual_at_run_does_not_disable_or_consume_task(self, mocks):
        collector, storage = mocks
        task = _make_task(schedule_type="at", at_time="2099-01-01T00:00:00", target_mode="node-001")
        collector.get_node_detail.return_value = {"server_url": "http://node-001:8080"}
        scheduler = TaskScheduler(collector, storage)

        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ):
            assert scheduler.dispatch_manual_task(task) is True

        storage.update_scheduled_task.assert_not_called()
        assert storage.record_task_run.call_args[1]["trigger"] == "manual"

    def test_manual_run_rejected_when_task_claimed(self, mocks):
        collector, storage = mocks
        task = _make_task(target_mode="auto")
        # A scheduler tick (or another manual run) already holds the claim.
        storage.claim_scheduled_task_execution.return_value = False
        scheduler = TaskScheduler(collector, storage)

        assert scheduler.dispatch_manual_task(task) is False
        storage.record_task_run.assert_not_called()

    def test_manual_run_release_is_owner_checked(self, mocks):
        collector, storage = mocks
        task = _make_task(target_mode="auto")
        collector.get_nodes.return_value = ([_make_node()], 1)
        collector.get_node_detail.return_value = {"server_url": "http://node-001:8080"}
        scheduler = TaskScheduler(collector, storage)

        with patch(
            "turnstone.console.scheduler.TurnstoneServer.create_workstream",
            return_value=_mock_create_response(),
        ):
            scheduler.dispatch_manual_task(task)

        # Release must pass the claim_id this attempt actually owns.
        claim_id = storage.claim_scheduled_task_execution.call_args.args[1]
        release_args = storage.release_scheduled_task_execution.call_args.args
        assert release_args[1] == claim_id

    def test_manual_run_records_failure_with_manual_trigger(self, mocks):
        collector, storage = mocks
        task = _make_task(target_mode="auto")
        collector.get_nodes.return_value = ([], 0)  # no nodes
        scheduler = TaskScheduler(collector, storage)

        assert scheduler.dispatch_manual_task(task) is True  # admitted
        run_kwargs = storage.record_task_run.call_args[1]
        assert run_kwargs["status"] == "failed"
        assert run_kwargs["trigger"] == "manual"
