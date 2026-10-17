"""MCP server exposing the verified Switchyard read-only operations.

This is a typed front end for ``scripts/turnstone_switchyard_runtime.py`` — it
does not reimplement any remote access. Every tool maps to exactly one of that
script's closed verbs, so the same guarantees hold here:

* no tool accepts a command, a path or a URL;
* ``route`` and ``log`` take typed parameters that are passed to the script as
  one bounded JSON object on stdin, and the script applies them as an
  in-process filter after fetching;
* nothing mutates the deployment — ``validate`` runs the server binary's
  ``--dry-run``.

Credential handling
-------------------
Transfer of the SSH key is done by the script under the service user's own
permissions. ``validate`` sources the unit's environment files *inside that
process* and returns only the binary's own stdout/stderr; this server never
reads, receives or forwards an environment value, and the runtime script never
prints one.

Usage::

    python -m mcp_switchyard_ops

Environment variables
---------------------
MCP_SWITCHYARD_PYTHON   Interpreter that can import the runtime module
                        (default: /opt/turnstone-venv/bin/python)
MCP_SWITCHYARD_SCRIPT   Path to turnstone_switchyard_runtime.py
                        (default: located by walking up from this file)
MCP_SWITCHYARD_TIMEOUT  Per-call timeout in seconds (default: 120)

Trust boundary
--------------
These overrides are part of the server's *own* configuration, not of a tool
call. The guarantee this module makes is about tool input: no argument a model
supplies can change the executable, the script, the verb or the remote command.
Whoever can rewrite this process's environment can already run code as the
service user; that is a different trust boundary, not something a schema can
close.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from mcp.server.fastmcp import FastMCP


def _default_script() -> str:
    """Locate the runtime module by walking up from this file.

    Counting parent directories is brittle: the package moves, and a wrong
    count silently points at a directory that happens to exist. Walk up until
    the script is actually there.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        candidate = os.path.join(here, "scripts", "turnstone_switchyard_runtime.py")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return os.path.join(
        "/var/lib/turnstone/github/turnstone", "scripts", "turnstone_switchyard_runtime.py"
    )


_DEFAULT_SCRIPT = _default_script()

PYTHON = os.environ.get("MCP_SWITCHYARD_PYTHON", "/opt/turnstone-venv/bin/python")
SCRIPT = os.environ.get("MCP_SWITCHYARD_SCRIPT", _DEFAULT_SCRIPT)
_TIMEOUT = int(os.environ.get("MCP_SWITCHYARD_TIMEOUT", "120"))

# The runtime script's exit codes, mapped to a caller-visible meaning.
EXIT_MEANING = {
    0: "ok",
    2: "refused",
    3: "transport_failure",
    4: "remote_failure",
}

INSTRUCTIONS = """
Read-only tools for the Switchyard deployment on switchyard-vm.example.invalid. Start with
switchyard_status to confirm the unit is healthy, then use switchyard_catalog /
switchyard_route to answer what the manifest declares, and switchyard_stats /
switchyard_metrics / switchyard_log to answer what actually happened.

Composition truth: a composite's served child and promotion reason are on
switchyard_composition_attempt_total / composition_cross_total in Prometheus.
switchyard_log gives the per-request sequence when the aggregate is not enough.

Nothing here changes the deployment. There is no deploy, restart or rollback
tool; those are deliberately out of scope.
""".strip()

mcp = FastMCP("turnstone-switchyard-ops", instructions=INSTRUCTIONS)


class SwitchyardToolError(RuntimeError):
    """A tool call failed in a way the caller should see verbatim."""


def _run(verb: str, stdin_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke one closed verb and return its parsed JSON result.

    ``verb`` is always a module constant at each call site, and
    ``stdin_payload`` is serialised here from typed arguments; a caller cannot
    add fields the runtime does not already validate, and cannot reach any of
    the argv slots above.
    """
    argv = [PYTHON, "-I", "-B", SCRIPT, verb]
    payload = json.dumps(stdin_payload).encode() if stdin_payload is not None else b""
    try:
        proc = subprocess.run(
            argv, input=payload, capture_output=True, timeout=_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise SwitchyardToolError(f"switchyard {verb} timed out after {_TIMEOUT}s") from exc
    except OSError as exc:
        raise SwitchyardToolError(f"switchyard {verb} could not start: {exc}") from exc

    raw = proc.stdout.decode("utf-8", "replace").strip()
    if not raw:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SwitchyardToolError(
            f"switchyard {verb} produced no output (rc={proc.returncode}"
            f"{': ' + detail[-1] if detail else ''})"
        )
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SwitchyardToolError(
            f"switchyard {verb} returned non-JSON output (rc={proc.returncode})"
        ) from exc
    if proc.returncode != 0:
        reason = EXIT_MEANING.get(proc.returncode, f"exit {proc.returncode}")
        message = result.get("error") if isinstance(result, dict) else None
        raise SwitchyardToolError(f"{reason}: {message or 'no detail'}")
    return result


@mcp.tool()
def switchyard_status() -> dict[str, Any]:
    """Report deployment health: unit state, PID, uptime, binary version, /health, manifest hash.

    Read-only. Use this first — every other tool assumes the unit is running.
    """
    return _run("status")


@mcp.tool()
def switchyard_catalog() -> dict[str, Any]:
    """Return what the manifest declares: routes, composites, targets, capabilities, decision lanes.

    Structural fields only. Credential references and client blocks are never
    returned. Includes ``referenced_by`` so a primitive route's composites are
    visible without opening the manifest.
    """
    return _run("catalog")


@mcp.tool()
def switchyard_route(route: str) -> dict[str, Any]:
    """Inspect one route or composite with its resolved target and candidates.

    For a composite, each child is expanded in order with the target it would
    use, so capability gaps between children are visible before sending traffic.

    Args:
        route: Route or composite name from ``switchyard_catalog``, e.g.
            ``smartfree`` or ``tier2-bounded``.
    """
    return _run("route", {"route": route})


@mcp.tool()
def switchyard_stats() -> dict[str, Any]:
    """Return the in-process ``/v1/stats`` snapshot: totals and per-model calls and latency.

    This is a different window from the Prometheus counters and is cleared by
    ``POST /v1/stats/reset``. Do not compare the two without saying so.
    """
    return _run("stats")


@mcp.tool()
def switchyard_metrics() -> dict[str, Any]:
    """Return the declared Prometheus families: type, help and sample counts.

    A family listed here with no samples is declared but idle. A family missing
    from this list is not implemented by this build.
    """
    return _run("metrics")


@mcp.tool()
def switchyard_validate() -> dict[str, Any]:
    """Run the server binary's ``--dry-run`` against the deployed manifest.

    Diagnostic, read-only: the binary parses configuration and exits without
    binding a socket. It runs under the unit's own environment so credential
    references resolve; no environment value is ever returned.
    """
    return _run("validate")


@mcp.tool()
def switchyard_log(route: str | None = None, limit: int = 25) -> dict[str, Any]:
    """Return recent routing-log records, newest last.

    The routing log is the only place a per-request composition sequence exists.
    Prefer the Prometheus counters for aggregate questions; use this when the
    sequence or the served model for one request matters.

    Args:
        route: Optional ``route_id`` filter, matched exactly.
        limit: How many matching records to return, 1-200.
    """
    payload: dict[str, Any] = {"limit": limit}
    if route is not None:
        payload["route"] = route
    return _run("log", payload)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
