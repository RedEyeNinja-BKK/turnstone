"""Minimal conftest: the sensor suite needs no MCP/session fixtures."""

# Hermetic test environment. Two process-wide values are computed at IMPORT
# time from the environment, so they must be set before any turnstone module
# is imported by a test module:
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
import os as _os
import pathlib as _pathlib
import tempfile as _tempfile

_HERMETIC = _pathlib.Path(_tempfile.mkdtemp(prefix="turnstone-sensor-tests-", dir="/var/tmp"))
_os.environ["TURNSTONE_SENSOR_STORE_DIR"] = str(_HERMETIC / "telemetry")
(_HERMETIC / "telemetry").mkdir(parents=True, exist_ok=True)

from turnstone.core.storage import init_storage as _init_storage  # noqa: E402
from turnstone.core.storage._registry import is_storage_initialized as _initialized  # noqa: E402

if not _initialized():
    _init_storage("sqlite", path=str(_HERMETIC / "sensor-tests.db"), run_migrations=False)
