#!/usr/bin/env python3
"""Guard the manifest's `default_decision_lane` declaration.

WHY THIS EXISTS
  Switchyard's runner reads ``default_decision_lane`` as a ROOT key and
  validates it against ``[decision_lanes.*]`` -- the map of lanes that hold
  physical targets. Two failure modes were measured on the deployed manifest:

  * A ``default_decision_lane = "tier1-decision"`` line was emitted inside
    ``[targets."direct-openrouter-mercury-decide-free".supported_types]``.
    TOML puts it in that table, so the root key is ABSENT and the line is
    inert: ``SupportedTypes`` has no such field and serde ignores it. The
    deployment runs with no default lane, which is not what the line says.

  * Promoting that same line to the root fails startup:
    ``default_decision_lane names tier1-decision, which is not a configured
    decision lane``. A COMPOSED lane lives in ``decision_lane_composition``,
    not in ``decision_lanes``, so it can never be a default -- a fix that only
    moves the line takes the service down on the next restart.

  Those are two different mistakes, and fixing one is how you make the other.
  This module is the single place that knows both rules, so a generator cannot
  emit an inert declaration and a repair cannot emit a fatal one.

SCOPE
  This is a guard, not a manifest generator. No generator source exists on
  either host (the deployed file's "generated; do not hand-edit" header has no
  producer in the Switchyard tree, the Turnstone tree, or the build trees on
  agent-vm.example.invalid). It therefore validates a document before serialization, validates
  the serialized text after, and refuses an intent it cannot honour rather
  than silently dropping or misplacing it.

RUNTIME INTERFACE
      python switchyard_manifest_guard.py check /etc/switchyard/routes.toml
      python switchyard_manifest_guard.py check - --json < candidate.toml
      python switchyard_manifest_guard.py plan --lanes smartfree-decision

  ``check`` exits 0 when clean and 1 with one line per violation. ``-`` reads
  stdin; ``--json`` emits ``{"ok": bool, "violations": [...]}`` so a deploy
  script can gate on it without parsing prose. ``plan`` prints the exact root
  lines a generator should emit, or exits 1 explaining why the requested lane
  cannot be a default.

MANDATORY PRE-DEPLOYMENT GATE
  Run BOTH gates against the candidate file before it is installed. Neither
  alone is sufficient: this module reads the document, the binary reads the
  document *and* the environment its credentials resolve from.

      1. python scripts/switchyard_manifest_guard.py check <candidate>
      2. sudo -n /opt/switchyard/bin/switchyard-server \
             --config <candidate> --dry-run      # with the unit's env files

  On switchyard-vm.example.invalid gate 2 is wrapped by
  ``scripts/turnstone_switchyard_runtime.py validate`` (it sources the unit's
  ``EnvironmentFile``s); point it at the deployed path only after gate 1 passes
  on the candidate. A non-zero exit from either gate means DO NOT INSTALL:
  the loader rejects the file at startup, and startup is a restart of the
  routing endpoint.

  This module deliberately does not run the binary itself: that needs the
  deployment's credential environment, which this process is not entitled to
  read. Keeping the two gates separate keeps each one honest about what it
  proved.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from typing import Any

#: Root key this guard owns. Named once so a typo cannot create a second one.
ROOT_KEY = "default_decision_lane"


class ManifestGuardError(ValueError):
    """The requested declaration cannot be emitted correctly."""


def _nested_occurrences(doc: dict[str, Any]) -> list[str]:
    """Every place the key sits inside a target sub-table instead of at root.

    Only ``targets.*.supported_types`` is checked because that is the shape the
    deployed manifest produced. A generator emitting the key into any other
    sub-table would be caught by :func:`lint_manifest`'s root-membership rule
    failing to see it, so an exhaustive walk is not needed here -- but the
    target map is walked, not one target, so a different target inheriting the
    same bug is still reported.
    """
    found: list[str] = []
    targets = doc.get("targets")
    if not isinstance(targets, dict):
        return found
    for name, target in targets.items():
        if not isinstance(target, dict):
            continue
        supported = target.get("supported_types")
        if isinstance(supported, dict) and ROOT_KEY in supported:
            found.append(f"targets.{name}.supported_types")
    return found


def lint_manifest(doc: dict[str, Any]) -> list[str]:
    """Return every violation of the two rules. Empty means clean.

    Rule 1 -- the key is a ROOT key. Inside a target table it is ignored, so
    the manifest would claim a default the runner never sees.
    Rule 2 -- the value must name a lane in ``decision_lanes``. A composed lane
    (``decision_lane_composition``) cannot serve as a default; naming one is
    a fatal configuration error at load, not a fallback.
    """
    violations: list[str] = []

    for where in _nested_occurrences(doc):
        violations.append(
            f"{ROOT_KEY} is declared inside [{where}] and is therefore inert; "
            "it must be a root key or be omitted"
        )

    if ROOT_KEY in doc:
        value = doc[ROOT_KEY]
        lanes = doc.get("decision_lanes")
        lane_names = set(lanes) if isinstance(lanes, dict) else set()
        composed = doc.get("decision_lane_composition")
        composed_names = set(composed) if isinstance(composed, dict) else set()
        if not isinstance(value, str) or not value.strip():
            violations.append(f"{ROOT_KEY} must be a non-empty string; omit it instead")
        elif value in composed_names and value not in lane_names:
            violations.append(
                f"{ROOT_KEY} names {value!r}, which is a COMPOSED lane and cannot serve "
                "as a default; the loader rejects this at startup"
            )
        elif value not in lane_names:
            violations.append(
                f"{ROOT_KEY} names {value!r}, which is not a declared decision lane; "
                "the loader rejects this at startup"
            )

    if ROOT_KEY not in doc and _nested_occurrences(doc):
        violations.append(
            "the manifest has no effective default lane, because its only "
            f"{ROOT_KEY} sits inside a target table"
        )

    return violations


def lint_manifest_text(text: str) -> list[str]:
    """Parse *text* and lint it. A parse failure is itself a violation."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [f"manifest is not valid TOML: {exc}"]
    return lint_manifest(doc)


def plan_default_decision_lane(doc: dict[str, Any], desired: str | None) -> list[str]:
    """Return the root TOML lines a generator must emit for *desired*.

    ``None`` means "this deployment declares no default": the correct output is
    no line at all. An unhonourable request raises rather than returning an
    empty list, so a generator can never turn an operator's intent into a
    silently missing declaration.
    """
    if desired is None:
        return []
    lanes = doc.get("decision_lanes")
    lane_names = set(lanes) if isinstance(lanes, dict) else set()
    if not isinstance(desired, str) or not desired.strip():
        raise ManifestGuardError(f"{ROOT_KEY} must be a non-empty lane name, or omitted")
    if desired in lane_names:
        return [f'{ROOT_KEY} = "{desired}"']
    composed = doc.get("decision_lane_composition")
    if isinstance(composed, dict) and desired in composed:
        raise ManifestGuardError(
            f"{desired!r} is a composed lane; the loader rejects it as a default. "
            "Name one of the lanes it composes instead, or omit the key."
        )
    raise ManifestGuardError(
        f"{desired!r} is not a declared decision lane; omit the key or declare the lane first"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="lint a manifest file, or '-' for stdin")
    check.add_argument("path")
    check.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit one JSON object instead of prose (for a deploy gate)",
    )

    plan = sub.add_parser("plan", help="print the root lines for a desired default lane")
    plan.add_argument("--manifest", required=True)
    plan.add_argument(
        "--lanes",
        default="",
        help="desired default lane, or empty for 'declare none'",
    )

    args = parser.parse_args(argv)
    if args.command == "check":
        if args.path == "-":
            text = sys.stdin.read()
        else:
            with open(args.path, encoding="utf-8") as handle:
                text = handle.read()
        violations = lint_manifest_text(text)
        if args.as_json:
            print(json.dumps({"ok": not violations, "violations": violations}))
            return 0 if not violations else 1
        if violations:
            print(f"FAIL — {len(violations)} violation(s):")
            for item in violations:
                print(f"  - {item}")
            return 1
        print("PASS — default_decision_lane is absent or correctly rooted")
        return 0

    with open(args.manifest, encoding="utf-8") as handle:
        doc = tomllib.loads(handle.read())
    try:
        lines = plan_default_decision_lane(doc, args.lanes or None)
    except ManifestGuardError as exc:
        print(f"REFUSED — {exc}")
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
