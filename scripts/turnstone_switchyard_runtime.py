#!/usr/bin/env python3
"""Bounded read-only access to the Switchyard deployment on ``switchyard-vm.example.invalid``.

WHY IT LOOKS LIKE THIS
  The Turnstone service user already owns the SSH key that reaches
  ``switchyard-vm.example.invalid`` (``/srv/turnstone/credentials/ssh/keys/`` is
  ``turnstone:turnstone``, 0600), so this module needs no sudo grant, no
  privileged launcher and no long-running daemon. It is an ordinary script run
  as ``turnstone``. It adds structure, not privilege.

WHAT IT GUARANTEES
  * The verb set is closed. There is no free-form command parameter and no
    free-form URL parameter.
  * Every remote command is a module-level constant. Caller input is never
    interpolated into an SSH command line, a shell word or a path. The two
    verbs that accept input (``route``, ``log``) use it only as a filter
    applied in this process, after the remote output has been fetched.
  * The remote reads are themselves bounded -- the log is scanned with
    ``tail -n 5000`` and the manifest and API bodies are size-checked before
    parsing -- and returned lists are capped, so an oversized source is
    refused rather than returned.
  * Nothing here mutates the deployment. ``validate`` runs the server binary
    with ``--dry-run``, which parses configuration and exits without binding a
    socket. Restaging, restarting and rollback are deliberately absent.
  * ``/etc/switchyard/routes.toml`` holds no literal credentials on this
    deployment -- every client uses an ``api_key_env`` reference -- and the
    parsers below return structural fields only, never whole client blocks.

RUNTIME INTERFACE
      python turnstone_switchyard_runtime.py status
      python turnstone_switchyard_runtime.py catalog
      python turnstone_switchyard_runtime.py stats
      python turnstone_switchyard_runtime.py metrics
      python turnstone_switchyard_runtime.py validate
      printf '%s' '{"route":"smartfree"}' \\
        | python turnstone_switchyard_runtime.py route
      printf '%s' '{"route":"smartfree","limit":20}' \\
        | python turnstone_switchyard_runtime.py log

  Output is one JSON object on stdout. Exit codes: 0 success, 2 refused
  request, 3 transport failure, 4 remote failure.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import sys
import tomllib
from typing import Any

SSH_TARGET = "turnstone-mgmt@switchyard-vm.example.invalid"
SSH_KEY = "/srv/turnstone/credentials/ssh/keys/turnstone-switchyard-vm-ed25519"
SSH_KNOWN_HOSTS = "/srv/turnstone/credentials/ssh/known_hosts"

UNIT = "switchyard-neutral-green.service"
BINARY = "/opt/switchyard/bin/switchyard-server"
CONFIG_PATH = "/etc/switchyard/routes.toml"
LOG_PATH = "/var/log/switchyard/routing.jsonl"
BASE_URL = "http://127.0.0.1:4000"

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_API_BYTES = 8 * 1024 * 1024
MAX_LOG_SCAN_LINES = 5000
MAX_LOG_RETURN = 200
MAX_STDIN_BYTES = 4096
MAX_ROUTE_NAME = 64
MAX_TEXT_LINES = 40
MAX_DIAGNOSTIC_BYTES = 8 * 1024
MAX_DIAGNOSTIC_LINE = 400

# ``validate`` sources the unit's environment files, so the binary's own
# diagnostics are the one place a credential value could reach this process.
# The binary does not print them today; treat that as a property to enforce,
# not to assume. Only ``NAME=value`` shapes are redacted, because that is the
# only shape an environment dump can take.
_SECRET_ASSIGNMENT_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})=([^\s\"']{16,})")

# Declared by switchyard-neutral-green.service. Used only by ``validate`` so the
# dry-run sees the same environment the deployed process does.
ENV_FILES = (
    "/etc/switchyard/secrets/providers.env",
    "/var/lib/switchyard/oauth/switchyard-openai.env",
)

ROUTE_NAME_RE = re.compile(r"^[A-Za-z0-9._:-]{1," + str(MAX_ROUTE_NAME) + r"}$")

ROUTE_FIELDS = (
    "type",
    "target",
    "candidates",
    "context_window",
    "tool_calling",
    "reasoning",
    "vision",
    "route_reasoning_policy",
    "escalation_max_input_tokens",
    "escalation_target",
)
TARGET_FIELDS = (
    "id",
    "llm_client",
    "vision",
    "tool_calling",
    "reasoning_policy",
    "reasoning_dialect",
    "max_context_tokens",
)
COMPOSITE_FIELDS = ("children", "context_window", "tool_calling", "reasoning", "vision")
CAPABILITY_FIELDS = (
    "id",
    "target",
    "contract",
    "dimensions",
    "normalization",
    "max_batch",
    "max_candidates",
    "top_n",
    "max_doc_chars",
    "max_query_chars",
)
DECISION_LANE_FIELDS = ("targets", "children")
LLM_CLIENT_FIELDS = ("format", "base_url", "requires_list_input", "max_retries")
LOG_FIELDS = (
    "ts",
    "route_id",
    "algorithm",
    "model",
    "tier",
    "child_route",
    "final_target",
    "cross_child_reason",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "total_tokens",
)

HISTOGRAM_SUFFIXES = ("_bucket", "_count", "_sum")


class RequestRefusedError(Exception):
    """The caller asked for something outside the closed surface."""


class TransportError(Exception):
    """SSH could not reach the host."""


class RemoteError(Exception):
    """The remote command ran and failed."""


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


def _shquote(value: str) -> str:
    """Single-quote a literal for a POSIX shell. Inputs are module constants."""
    return "'" + value.replace("'", "'\\''") + "'"


def _ssh(remote: str, *, timeout: int = 45) -> subprocess.CompletedProcess:
    """Run one fixed remote command. ``remote`` is always a module constant."""
    argv = [
        "ssh",
        "-F",
        "/dev/null",
        "-i",
        SSH_KEY,
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        SSH_TARGET,
        remote,
    ]
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TransportError(f"ssh timed out after {timeout}s") from exc
    except OSError as exc:
        raise TransportError(f"ssh could not start: {exc}") from exc


def _ssh_text(remote: str, *, timeout: int = 45, limit: int = MAX_API_BYTES) -> str:
    proc = _ssh(remote, timeout=timeout)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RemoteError(
            f"remote command failed (rc={proc.returncode}): {detail[-1] if detail else 'no detail'}"
        )
    if len(proc.stdout) > limit:
        raise RemoteError(f"remote output exceeded {limit} bytes")
    return proc.stdout.decode("utf-8", "replace")


def _curl_json(path: str) -> Any:
    raw = _ssh_text(f"curl -sS -m 20 {BASE_URL}{path}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RemoteError(f"{path} did not return JSON") from exc


def _read_config() -> dict[str, Any]:
    raw = _ssh_text(f"sudo -n cat {CONFIG_PATH}", limit=MAX_MANIFEST_BYTES)
    try:
        return tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise RemoteError(f"manifest is not valid TOML: {exc}") from exc


# --------------------------------------------------------------------------
# request parsing
# --------------------------------------------------------------------------


def read_request(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_STDIN_BYTES:
        raise RequestRefusedError(f"request exceeds {MAX_STDIN_BYTES} bytes")
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RequestRefusedError(f"request is not valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise RequestRefusedError("request must be a JSON object")
    return body


def parse_route_name(value: Any, *, field: str = "route") -> str:
    if not isinstance(value, str) or not ROUTE_NAME_RE.match(value):
        raise RequestRefusedError(f"{field} must match {ROUTE_NAME_RE.pattern}")
    return value


def parse_route_request(raw: bytes) -> str:
    body = read_request(raw)
    unknown = set(body) - {"route"}
    if unknown:
        raise RequestRefusedError("unknown field(s): {}".format(", ".join(sorted(unknown))))
    if "route" not in body:
        raise RequestRefusedError("'route' is required")
    return parse_route_name(body["route"])


def parse_log_request(raw: bytes) -> tuple[str | None, int]:
    body = read_request(raw)
    unknown = set(body) - {"route", "limit"}
    if unknown:
        raise RequestRefusedError("unknown field(s): {}".format(", ".join(sorted(unknown))))
    route = body.get("route")
    if route is not None:
        route = parse_route_name(route)
    limit = body.get("limit", 25)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LOG_RETURN:
        raise RequestRefusedError(f"limit must be an integer in 1..{MAX_LOG_RETURN}")
    return route, limit


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------


def _pick(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: source[name] for name in fields if name in source}


def build_catalog(doc: dict[str, Any]) -> dict[str, Any]:
    routes_in = doc.get("routes") or {}
    targets_in = doc.get("targets") or {}
    composites_in = doc.get("composites") or {}

    routes = {name: _pick(body, ROUTE_FIELDS) for name, body in sorted(routes_in.items())}
    targets = {name: _pick(body, TARGET_FIELDS) for name, body in sorted(targets_in.items())}
    composites = {
        name: _pick(body, COMPOSITE_FIELDS) for name, body in sorted(composites_in.items())
    }

    # A route that is named by a composite is entered through the same
    # composition path, so surface that relationship rather than making the
    # reader reconstruct it.
    for name, body in routes.items():
        body["referenced_by"] = sorted(
            composite
            for composite, spec in composites.items()
            if name in (spec.get("children") or [])
        )

    return {
        "schema_version": doc.get("schema_version"),
        "routes": routes,
        "composites": composites,
        "targets": targets,
        "capabilities": sorted((doc.get("capabilities") or {}).keys()),
        # Bodies as well as names. A caller that has to render what the
        # deployment declares would otherwise re-parse the manifest itself,
        # which is how a second, drifting copy of the inventory gets created.
        # These sections hold contracts and limits, never credentials.
        "capability_defs": {
            name: _pick(body, CAPABILITY_FIELDS)
            for name, body in sorted((doc.get("capabilities") or {}).items())
        },
        "decision_lanes": sorted((doc.get("decision_lanes") or {}).keys()),
        "decision_lane_defs": {
            name: _pick(body, DECISION_LANE_FIELDS)
            for name, body in sorted((doc.get("decision_lanes") or {}).items())
        },
        "decision_lane_composition": {
            name: _pick(body, DECISION_LANE_FIELDS)
            for name, body in sorted((doc.get("decision_lane_composition") or {}).items())
        },
        "llm_clients": {
            name: _pick(body, LLM_CLIENT_FIELDS)
            for name, body in sorted((doc.get("llm_clients") or {}).items())
        },
        "counts": {
            "routes": len(routes),
            "composites": len(composites),
            "targets": len(targets),
            "llm_clients": len(doc.get("llm_clients") or {}),
        },
    }


def inspect_route(doc: dict[str, Any], name: str) -> dict[str, Any]:
    routes = doc.get("routes") or {}
    targets = doc.get("targets") or {}
    composites = doc.get("composites") or {}

    if name in composites:
        spec = _pick(composites[name], COMPOSITE_FIELDS)
        children = []
        for child in spec.get("children") or []:
            definition = _pick(routes.get(child) or {}, ROUTE_FIELDS)
            target_name = definition.get("target")
            children.append(
                {
                    "name": child,
                    "definition": definition,
                    "target": {
                        "name": target_name,
                        "definition": _pick(targets.get(target_name) or {}, TARGET_FIELDS),
                    }
                    if target_name
                    else None,
                }
            )
        return {"name": name, "kind": "composite", "definition": spec, "children": children}

    if name not in routes:
        raise RequestRefusedError(f"unknown route or composite: {name}")

    route = _pick(routes[name], ROUTE_FIELDS)
    target_name = route.get("target")
    candidates = [
        {"name": candidate, "definition": _pick(targets.get(candidate) or {}, TARGET_FIELDS)}
        for candidate in route.get("candidates") or []
    ]
    return {
        "name": name,
        "kind": "route",
        "definition": route,
        "target": {
            "name": target_name,
            "definition": _pick(targets.get(target_name) or {}, TARGET_FIELDS),
        }
        if target_name
        else None,
        "candidates": candidates,
        "referenced_by": sorted(
            composite
            for composite, spec in composites.items()
            if name in (spec.get("children") or [])
        ),
    }


def _family_for(sample: str, families: dict[str, Any]) -> str | None:
    if sample in families:
        return sample
    for suffix in HISTOGRAM_SUFFIXES:
        if sample.endswith(suffix):
            base = sample[: -len(suffix)]
            if base in families:
                return base
    # Counters are normally declared with the _total suffix already present;
    # an undeclared _created series is still reported under its own name.
    return None


def parse_metrics(text: str) -> dict[str, Any]:
    families: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            if line.startswith("# TYPE "):
                rest = line[len("# TYPE ") :]
                name, _, kind = rest.partition(" ")
                families.setdefault(name, {"type": kind, "help": "", "samples": 0, "value": None})
                families[name]["type"] = kind
            elif line.startswith("# HELP "):
                rest = line[len("# HELP ") :]
                name, _, help_text = rest.partition(" ")
                families.setdefault(name, {"type": "", "help": "", "samples": 0, "value": None})
                families[name]["help"] = help_text
            continue

        head = line.split(" ", 1)[0]
        sample_name = head.split("{", 1)[0]
        family = _family_for(sample_name, families)
        if family is None:
            continue
        entry = families[family]
        entry["samples"] += 1
        if "{" not in head and entry["value"] is None:
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                with contextlib.suppress(ValueError):
                    entry["value"] = float(parts[1])

    returned = {}
    for name in dict.fromkeys(families):
        entry = families[name]
        item = {"type": entry["type"], "samples": entry["samples"]}
        if entry["help"]:
            item["help"] = entry["help"]
        if entry["value"] is not None:
            item["value"] = entry["value"]
        returned[name] = item
    return {"family_count": len(returned), "families": returned}


def summarize_stats(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise RemoteError("/v1/stats did not return an object")
    models = body.get("models") or {}
    rows = []
    for name, entry in models.items():
        if not isinstance(entry, dict):
            continue
        latency = entry.get("model_call_latency") or {}
        rows.append(
            {
                "model": name,
                "calls": entry.get("calls"),
                "errors": entry.get("errors"),
                "total_tokens": entry.get("total_tokens"),
                "reasoning_tokens": entry.get("reasoning_tokens"),
                "avg_ms": latency.get("avg_ms"),
                "p99_ms": latency.get("p99_ms"),
            }
        )
    rows.sort(key=lambda row: row["calls"] or 0, reverse=True)
    return {
        "total_requests": body.get("total_requests"),
        "total_errors": body.get("total_errors"),
        "total_tokens": body.get("total_tokens"),
        "models": rows[:50],
        "model_count": len(models),
    }


def filter_log(lines: list[str], route: str | None, limit: int) -> list[dict[str, Any]]:
    picked: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if route is not None and record.get("route_id") != route:
            continue
        picked.append({name: record.get(name) for name in LOG_FIELDS if name in record})
    return picked[-limit:]


def _redact_assignments(text: str) -> str:
    """Replace the value of any ``NAME=long-value`` pair in diagnostic text.

    The environment is never printed by the binary, but a dry-run is the one
    path that sources it, so the boundary is enforced rather than trusted.
    """
    return _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)


def _diagnostic(raw: bytes, limit: int = MAX_DIAGNOSTIC_BYTES) -> list[str]:
    """Project one captured stream to bounded, redacted lines.

    Byte-bounded before decoding so a single enormous line cannot slip past a
    line-count limit, and redacted because this is the one verb that runs with
    the unit environment loaded.
    """
    text = raw[:limit].decode("utf-8", "replace")
    lines = [line[:MAX_DIAGNOSTIC_LINE] for line in text.splitlines() if line.strip()]
    if len(raw) > limit:
        lines.append(f"[output truncated at {limit} bytes]")
    return [_redact_assignments(line) for line in lines[-MAX_TEXT_LINES:]]


def _tail(text: str, limit: int = MAX_TEXT_LINES) -> list[str]:
    return [line for line in text.strip().splitlines() if line][-limit:]


# --------------------------------------------------------------------------
# verbs
# --------------------------------------------------------------------------


STATUS_SCRIPT = f"""set -u
printf 'unit_state=%s\\n' "$(systemctl is-active {UNIT} 2>/dev/null || true)"
printf 'unit_enabled=%s\\n' "$(systemctl is-enabled {UNIT} 2>/dev/null || true)"
printf 'main_pid=%s\\n' "$(systemctl show -p MainPID --value {UNIT} 2>/dev/null || true)"
printf 'active_since=%s\\n' "$(systemctl show -p ActiveEnterTimestamp --value {UNIT} 2>/dev/null || true)"
printf 'version=%s\\n' "$({BINARY} --version 2>/dev/null | head -n 1 || true)"
printf 'health=%s\\n' "$(curl -sS -m 5 {BASE_URL}/health 2>/dev/null || true)"
printf 'config_sha256=%s\\n' "$(sudo -n sha256sum {CONFIG_PATH} 2>/dev/null | cut -d' ' -f1 || true)"
"""


def verb_status() -> dict[str, Any]:
    text = _ssh_text(STATUS_SCRIPT)
    fields: dict[str, Any] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()

    health = fields.pop("health", "")
    try:
        fields["health"] = json.loads(health) if health else None
    except json.JSONDecodeError:
        fields["health"] = health or None

    fields["unit"] = UNIT
    fields["ok"] = fields.get("unit_state") == "active" and isinstance(fields.get("health"), dict)
    return fields


def verb_catalog() -> dict[str, Any]:
    return build_catalog(_read_config())


def verb_stats() -> dict[str, Any]:
    return summarize_stats(_curl_json("/v1/stats"))


def verb_metrics() -> dict[str, Any]:
    text = _ssh_text(f"curl -sS -m 20 {BASE_URL}/metrics")
    return parse_metrics(text)


def verb_validate() -> dict[str, Any]:
    # The binary resolves every credential through api_key_env, so a bare
    # invocation fails with "environment variable not found" even though the
    # deployed manifest is valid. Source the two files the unit itself names,
    # exactly as systemd does, and run the same dry-run the deploy used.
    # Only the binary's own output is captured; the environment is never
    # echoed.
    script = (
        f"set -a; . {ENV_FILES[0]}; . {ENV_FILES[1]}; set +a; "
        "export SWITCHYARD_DEPLOYMENT=v6; "
        f"exec {BINARY} --config {CONFIG_PATH} --dry-run"
    )
    remote = f"sudo -n bash -c {_shquote(script)}"
    proc = _ssh(remote, timeout=60)
    # 255 is ssh's own failure status, not the binary's. Reporting it as a
    # failed dry-run would let an unreachable host look like an invalid config.
    if proc.returncode == 255:
        detail = _diagnostic(proc.stderr)
        raise TransportError(
            "ssh could not run the dry-run" + (f": {detail[-1]}" if detail else "")
        )
    return {
        "unit": UNIT,
        "dry_run_ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": _diagnostic(proc.stdout),
        "stderr": _diagnostic(proc.stderr),
    }


def verb_route(raw: bytes) -> dict[str, Any]:
    name = parse_route_request(raw)
    return inspect_route(_read_config(), name)


def verb_log(raw: bytes) -> dict[str, Any]:
    route, limit = parse_log_request(raw)
    text = _ssh_text(f"sudo -n tail -n {MAX_LOG_SCAN_LINES} {LOG_PATH}")
    records = filter_log(text.splitlines(), route, limit)
    return {
        "route_filter": route,
        "scanned_lines": MAX_LOG_SCAN_LINES,
        "returned": len(records),
        "records": records,
    }


VERBS = {
    "status": lambda raw: verb_status(),
    "catalog": lambda raw: verb_catalog(),
    "stats": lambda raw: verb_stats(),
    "metrics": lambda raw: verb_metrics(),
    "validate": lambda raw: verb_validate(),
    "route": verb_route,
    "log": verb_log,
}
STDIN_VERBS = {"route", "log"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in VERBS:
        payload = {"ok": False, "error": "usage: {} {{{}}}".format(sys.argv[0], ",".join(VERBS))}
        print(json.dumps(payload, indent=2))
        return 2

    verb = argv[0]
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1) if verb in STDIN_VERBS else b""
    try:
        result = VERBS[verb](raw)
    except RequestRefusedError as exc:
        print(json.dumps({"ok": False, "verb": verb, "error": str(exc)}, indent=2))
        return 2
    except TransportError as exc:
        print(json.dumps({"ok": False, "verb": verb, "error": str(exc)}, indent=2))
        return 3
    except RemoteError as exc:
        print(json.dumps({"ok": False, "verb": verb, "error": str(exc)}, indent=2))
        return 4
    try:
        print(json.dumps(result, indent=2, default=str))
    except BrokenPipeError:
        # Caller closed the pipe (e.g. piped into head); nothing left to say.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
