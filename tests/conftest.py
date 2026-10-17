"""Minimal conftest: the sensor suite needs no MCP/session fixtures."""

# Hermetic test environment. Several process-wide values are computed at IMPORT
# time from the environment, so they must be set before any turnstone module is
# imported by a test module:
#
#   * TURNSTONE_SENSOR_STORE_DIR -> shadow_driver.DEFAULT_STORE and
#     sensor_lifecycle.DEFAULT_CADENCE_STORE.
#   * the storage singleton's SQLite fallback, which otherwise creates
#     .turnstone.db in os.getcwd().
#
# Without this, a test that drives the real seam writes observation rows into
# /var/lib/turnstone/sensor, the production telemetry root. That happened during
# S6: 48 cadence rows and one observation, all synthetic (workstream "ws",
# pytest pids), landed in the production path and had to be quarantined.
#
# 2026-10-18: this suite is ALSO run from inside a live Turnstone service
# environment — the operator's shell is turnstone-node-3's own unit, which
# exports TURNSTONE_SENSOR_ENABLED=1 and that node's production store path.
# Inheriting those made the disabled-by-default gate untestable and aimed real
# sensor calls at production telemetry. Every sensor runtime variable is
# therefore cleared here, BEFORE any turnstone import, and only the store is
# re-pointed at a fresh directory.
#
# TURNSTONE_SENSOR_TOKENIZER_DIR is deliberately PRESERVED: the exact-token
# tests need the installed pinned tokenizer, and there is no fallback path that
# works — clearing it fails those tests with "tokenizer interpreter absent".
import os as _os
import pathlib as _pathlib
import tempfile as _tempfile

_HERMETIC = _pathlib.Path(_tempfile.mkdtemp(prefix="turnstone-sensor-tests-", dir="/var/tmp"))

for _var in (
    "TURNSTONE_SENSOR_ENABLED",
    "TURNSTONE_SENSOR_CONTENT_MODE",
    "TURNSTONE_SENSOR_STORE_DIR",
    "TURNSTONE_SENSOR_TIMEOUT_SECONDS",
    "TURNSTONE_SENSOR_DEADLINE_SECONDS",
    "TURNSTONE_SENSOR_MAX_ABANDONED",
):
    _os.environ.pop(_var, None)

_os.environ["TURNSTONE_SENSOR_STORE_DIR"] = str(_HERMETIC / "telemetry")
(_HERMETIC / "telemetry").mkdir(parents=True, exist_ok=True)

# The hermetic root must never be the production telemetry root, and the store
# must resolve inside it. A future edit that reintroduces leakage fails here
# rather than in the operator's measurement data.
_PRODUCTION_TELEMETRY = _pathlib.Path("/var/lib/turnstone/sensor")
assert not str(_HERMETIC).startswith(str(_PRODUCTION_TELEMETRY)), _HERMETIC
assert str(_os.environ["TURNSTONE_SENSOR_STORE_DIR"]).startswith(str(_HERMETIC))

from turnstone.core.storage import init_storage as _init_storage  # noqa: E402
from turnstone.core.storage._registry import is_storage_initialized as _initialized  # noqa: E402

if not _initialized():
    _init_storage("sqlite", path=str(_HERMETIC / "sensor-tests.db"), run_migrations=False)
