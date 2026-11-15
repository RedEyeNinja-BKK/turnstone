#!/usr/bin/env python3
"""Mutation controls for the typed-decision dispatch port.

Each mutant is a semantic inversion of ONE defence in the port. A mutant that
SURVIVES means the suite is not measuring that defence, so it over-reports.
A mutant that reports "not applied"/"wrong reason" is also a failure: a broken
program is not evidence.

Restoration is from bytes held in memory, never from git -- a checkout here
would silently revert the applied port.

Usage: mutate_typed_decision.py <tree-under-test> <python>
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

TREE = pathlib.Path(sys.argv[1]).resolve()
PY = sys.argv[2]
TEST = "tests/test_typed_decision_dispatch.py"
OGJ = "turnstone/core/output_guard_judge.py"
REG = "turnstone/core/model_registry.py"
SRV = "turnstone/console/server.py"
SES = "turnstone/core/session.py"
CLI = "turnstone/cli.py"
TD = "turnstone/core/typed_decision.py"

MUTANTS = [
    (
        "M1 typed dispatch removed (falls through to a generative call)",
        OGJ,
        '        if getattr(self, "_typed_spec", None) is not None:\n',
        "        if False:\n",
    ),
    (
        "M2 typed row identified as an ordinary generative row",
        OGJ,
        '    if not caps.get("supports_typed_decision"):\n        return None\n',
        '    if True:\n        return None\n',
    ),
    (
        "M3 typed failure recorded against the session model",
        OGJ,
        '                self._judge_model_alias or getattr(self, "_typed_alias", "") or self._model\n',
        "                self._model\n",
    ),
    (
        "M4 typed failure stops being a labelled failure (looks clean)",
        OGJ,
        "            error=reason,\n",
        "            error=None,\n",
    ),
    (
        "M5 typed row offered as a chat model again",
        REG,
        '    if capabilities.get("supports_typed_decision"):\n        return False\n',
        '    if capabilities.get("supports_typed_decision") and False:\n        return False\n',
    ),
    (
        "M6 console model picker keeps typed rows",
        SRV,
        "        if not _is_typed_decision_row(r)\n",
        "        if True\n",
    ),
    (
        "M7 typed alias resolves a generative binding again",
        OGJ,
        '            binding = resolve_judge_binding("", session_binding, config_store)\n',
        "            binding = resolve_judge_binding(requested_alias, session_binding, config_store)\n",
    ),
    (
        "M8 ungraded noul downgraded to a zero score instead of an error",
        TD,
        '                raise TypedDecisionError("noul answer carried no usable score")\n',
        "                graded = 0.0\n",
    ),
    (
        "M9 /model switch list offers the decision row again",
        SES,
        '                    avail = ", ".join(self._registry.offered_aliases(None))\n',
        '                    avail = ", ".join(self._registry.list_aliases())\n',
    ),
    (
        "M10 unknown-alias refusal lists the decision row again",
        SES,
        'self._registry.offered_aliases(None))}"\n',
        'self._registry.list_aliases())}"\n',
    ),
    (
        "M11 CLI banner advertises the decision row again",
        CLI,
        "            a for a in registry.offered_aliases(None) if a != registry.default\n",
        "            a for a in registry.list_aliases() if a != registry.default\n",
    ),
]


def run_suite() -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(TREE)
    env["TMPDIR"] = "/var/tmp"
    proc = subprocess.run(
        [PY, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider", "--no-header"],
        cwd=str(TREE),
        capture_output=True,
        text=True,
        env=env,
    )
    out = proc.stdout.strip() or proc.stderr.strip()
    return proc.returncode, out.splitlines()[-1] if out else "<no output>"


def main() -> int:
    clean = TREE / TEST
    if not clean.exists():
        print(f"no suite at {clean}")
        return 2
    base_rc, base_tail = run_suite()
    print(f"baseline: rc={base_rc} {base_tail}")
    if base_rc != 0:
        print("baseline is not green -- refusing to grade mutants against it")
        return 2

    bad: list[str] = []
    for name, rel, old, new in MUTANTS:
        path = TREE / rel
        original = path.read_bytes()
        try:
            text = original.decode()
            hits = text.count(old)
            if hits != 1:
                print(f"NOT APPLIED ({hits} matches): {name}")
                bad.append(name)
                continue
            mutated = text.replace(old, new, 1)
            if mutated == text:
                print(f"NO-OP: {name}")
                bad.append(name)
                continue
            path.write_text(mutated, encoding="utf-8")
            rc, tail = run_suite()
            if rc == 0:
                print(f"SURVIVED: {name}  -> {tail}")
                bad.append(name)
            else:
                print(f"caught:   {name}  -> {tail}")
        finally:
            path.write_bytes(original)

    print()
    after_rc, after_tail = run_suite()
    print(f"restored: rc={after_rc} {after_tail}")
    if after_rc != 0:
        print("tree did not return to green after restoration")
        return 2
    if bad:
        print(f"{len(bad)} uncaught/unapplied: {bad}")
        return 1
    print(f"all {len(MUTANTS)} mutants caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
