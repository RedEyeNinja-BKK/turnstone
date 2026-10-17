# SPDX-License-Identifier: Apache-2.0
"""The combined R2 + native-OAuth activation must be one gated, reversible step.

The staged slot is inert until the three `93-` drop-ins point the units at it.
That switch is the production boundary, so the tests below pin the properties
that make it safe to hand to an operator:

  * one restart window, one rollback (never per-unit drift),
  * an explicit `--confirm` and root, not an accidental invocation,
  * atomic, root-owned drop-ins that sort AFTER the 90-/91- anchors,
  * proof of the code that is actually running (PID PYTHONPATH + /health),
  * automatic rollback when the proof fails,
  * no model-row or credential mutation in the activation path.

They are structural plus behavioural: the refusals and the dry run are executed
against the real host, because a grep cannot tell whether the gate actually
holds.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

_ROOT = pathlib.Path(os.environ.get("R2_REPO") or pathlib.Path(__file__).resolve().parents[1])
SCRIPT = _ROOT / "scripts" / "stage_openai_oauth_v187.sh"
SLOT = "/opt/turnstone-overlays/openai-oauth-v187-r2"
DROPIN_NAMES = (
    "turnstone-server.service.d/94-openai-oauth-v187-r2.conf",
    "turnstone-node@.service.d/94-openai-oauth-v187-r2.conf",
    "turnstone-console.service.d/94-openai-oauth-v187-r2.conf",
)
DROPINS = tuple(f"/etc/systemd/system/{n}" for n in DROPIN_NAMES)
UNITS = (
    "turnstone-server.service",
    "turnstone-node@2.service",
    "turnstone-node@3.service",
    "turnstone-node@4.service",
    "turnstone-node@5.service",
    "turnstone-console.service",
)

_SKIPPED: list[str] = []


def _run(
    argv: list[str], timeout: int = 60, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, capture_output=True, text=True, timeout=timeout, check=False, env=env
    )


def _src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _body(name: str) -> str:
    src = _src()
    start = src.index(f"{name}() {{")
    end = src.index("\n}\n", start)
    return src[start:end]


def _units_active() -> bool:
    for u in UNITS:
        p = _run(["systemctl", "is-active", u], timeout=10)
        if p.stdout.strip() != "active":
            return False
    return True


# ---------------------------------------------------------------------------
# Shape of the switch
# ---------------------------------------------------------------------------


def test_the_switch_is_three_dropins_for_six_units() -> None:
    src = _src()
    assert 'SYSROOT=${ACTIVATION_SYSROOT:-/etc/systemd/system}' in src
    for name in DROPIN_NAMES:
        leaf = name.split("/")[-1]
        assert f'{"/".join(name.split("/")[:-1])}/{leaf}' in src, f"missing drop-in: {name}"
        assert leaf in src, f"missing drop-in name: {leaf}"
    for unit in UNITS:
        assert unit.replace(".service", "") in src, f"unit not in the restart set: {unit}"
    # Every unit is restarted by one command, not one at a time.
    activate = _body("cmd_activate")
    assert 'systemctl restart "${UNITS[@]}"' in activate, (
        "units must be restarted in a single window"
    )
    assert activate.count("systemctl restart") == 1
    # ...and a nonzero status must not abort before the rollback branch.
    assert 'if ! systemctl restart "${UNITS[@]}"; then' in activate


def test_the_activation_prefix_sorts_after_the_existing_anchors() -> None:
    ours = pathlib.Path(DROPINS[0]).name
    assert ours.startswith("94-"), ours
    for anchor in ("90-typed-decision-v187.conf", "91-typed-decision-v187-finish.conf"):
        assert anchor < ours, f"{anchor} would override {ours}"
    # The superseded families are refused rather than silently overridden.
    assert "93-openai-oauth*" in _src(), "the pre-correction 93- family must be refused"
    assert "superseded 92- drop-in" in _src()
    # A later drop-in that sets PYTHONPATH must refuse to coexist silently.
    assert "higher_pythonpath_override" in _src()
    assert "drop-in would be overridden by" in _src()


def test_dropin_writes_are_atomic_and_root_owned() -> None:
    body = _body("write_dropins_atomically")
    assert "mktemp" in body, "write through a temporary file, not in place"
    assert "mv -f" in body, "rename into place atomically"
    assert "cleanup_partial_writes" in body, "a failed write must be unwound"
    assert "chmod 0644" in body
    # root:root in production; the sandbox substitutes the invoking identity.
    assert 'chown "${OWNER[0]}"' in body
    assert '> "$path"' not in body, "never write the live drop-in in place"


def test_staging_patches_one_setting_instead_of_copying_the_whole_file() -> None:
    """Copying the repo file would expose settings this 1.8.7 base cannot implement."""
    src = _src()
    assert '"core/settings_registry.py"' not in src, (
        "settings_registry must be patched, not copied whole"
    )
    assert "patch_settings_registry" in src
    assert "unimplemented setting" in src


# ---------------------------------------------------------------------------
# The operator gate (executed)
# ---------------------------------------------------------------------------


def test_activation_refuses_without_confirmation() -> None:
    p = _run(["bash", str(SCRIPT), "activate"])
    assert p.returncode != 0
    assert "without --confirm" in p.stderr, p.stderr


def test_rollback_refuses_without_confirmation() -> None:
    p = _run(["bash", str(SCRIPT), "rollback"])
    assert p.returncode != 0
    assert "without --confirm" in p.stderr, p.stderr


def test_activation_refuses_without_root() -> None:
    if os.geteuid() == 0:
        _SKIPPED.append("running as root; the unprivileged refusal is not exercised")
        return
    # Exercise the production gate: no sandbox privileges, no dry run.
    env = os.environ.copy()
    env["R2_SANDBOX"] = "0"
    p = _run(["bash", str(SCRIPT), "activate", "--confirm"], env=env)
    assert p.returncode != 0
    assert "must run as root" in p.stderr, p.stderr


_SANDBOX_SLOT = "/var/tmp/v187-sbx/oauth"


def _sandbox_env() -> dict[str, str] | None:
    """Env that points the read-only verbs at the sandbox-staged candidate."""
    if not pathlib.Path(_SANDBOX_SLOT).is_dir():
        return None
    env = os.environ.copy()
    env.update(
        {
            "R2_SANDBOX": "1",
            "OAUTH_ROOT": _SANDBOX_SLOT,
            "ACTIVATION_SYSROOT": "/var/tmp/v187-sbx/sysroot",
            "ACTIVATION_PROC_ROOT": "/var/tmp/v187-sbx/procroot",
        }
    )
    return env


def test_dry_run_writes_nothing() -> None:
    env = _sandbox_env()
    if env is None:
        _SKIPPED.append("sandbox candidate not staged at " + _SANDBOX_SLOT)
        return
    if not _units_active():
        _SKIPPED.append("units are not all active; preflight would refuse on this host")
        return
    before = {d: pathlib.Path(d).exists() for d in DROPINS}
    receipts = pathlib.Path("/var/log/turnstone-v187")
    had_receipts = receipts.exists()
    p = subprocess.run(  # noqa: S603 - fixed argv
        ["bash", str(SCRIPT), "activate", "--confirm", "--dry-run"],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )
    assert p.returncode == 0, p.stderr
    assert "DRY RUN" in p.stdout
    for d in DROPINS:
        assert pathlib.Path(d).exists() is before[d], f"dry run wrote {d}"
    assert receipts.exists() is had_receipts, "dry run wrote a receipt"


def test_plan_names_the_slot_all_units_and_the_inertness_argument() -> None:
    env = _sandbox_env()
    if env is None:
        _SKIPPED.append("sandbox candidate not staged at " + _SANDBOX_SLOT)
        return
    if not _units_active():
        _SKIPPED.append("units are not all active; plan requires a live pre-state")
        return
    p = subprocess.run(  # noqa: S603 - fixed argv
        ["bash", str(SCRIPT), "plan"],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )
    assert p.returncode == 0, p.stderr
    assert _SANDBOX_SLOT in p.stdout
    for u in UNITS:
        assert u in p.stdout, f"plan does not mention {u}"
    assert "No model row is changed" in p.stdout
    assert "separate gate" in p.stdout


# ---------------------------------------------------------------------------
# Proof before success, and rollback on failure
# ---------------------------------------------------------------------------


def test_activation_proves_provenance_and_health_before_declaring_success() -> None:
    body = _body("cmd_activate")
    assert 'wait_healthy "$NEW"' in body, "must verify the running code, not just restart"
    assert body.index('wait_healthy "$NEW"') < body.index("ACTIVATED:")
    # Rollback is wired to both failure modes.
    assert body.count("rollback_core || true") >= 2
    assert "journal_scan" in body
    assert body.index("journal_scan") < body.index("ACTIVATED:")


def test_health_check_covers_every_listening_port() -> None:
    assert "HEALTH_PORTS=(8080 8082 8083 8084 8085 8090)" in _src()
    body = _body("wait_healthy")
    assert 'check_http "$p"' in body
    assert "proc_pythonpath" in body, "must check the PYTHONPATH of the live process"
    assert '[[ "$cur" == "$want" ]]' in body


def test_rollback_removes_only_the_three_dropins() -> None:
    body = _body("remove_dropins")
    assert 'rm -f "$p"' in body
    assert "rm -rf" not in body
    assert "dropin_paths" in body
    # Rollback proves the previous overlay is live again.
    assert "configured_pythonpath" in _body("rollback_core")


def test_previous_state_is_recorded_before_the_switch() -> None:
    body = _body("cmd_activate")
    assert body.index("capture_receipt") < body.index("write_dropin")
    receipt = _body("capture_receipt")
    for field in ("slot_python_manifest_sha256=", "before unit=", "before dropin="):
        assert field in receipt, field


# ---------------------------------------------------------------------------
# Scope discipline
# ---------------------------------------------------------------------------


def test_activation_touches_no_model_rows_or_credentials() -> None:
    # Only the ACTIVATION functions, not the staging/stage text: the staged
    # settings help string legitimately names the canonical store path.
    full = _src()
    src = full[full.index("# Activation / rollback") :]
    for forbidden in (
        "admin_create_model_definition",
        "model-definitions",
        "/srv/turnstone/credentials",
        "refresh_token",
    ):
        assert forbidden not in src, f"activation path must not touch {forbidden}"
    # The slot itself is read-only to this script: no writes into $NEW.
    for pattern in ('> "$NEW', '>> "$NEW', 'install -m .. "$NEW', "cp .. \"$NEW"):
        assert pattern not in src, f"activation must not write into the slot: {pattern}"


if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_") and callable(o)]
    passed = failed = skipped = 0
    for name, fn in tests:
        before = len(_SKIPPED)
        try:
            fn()
            if len(_SKIPPED) > before:
                skipped += 1
                print(f"  SKIP {name}")
            else:
                passed += 1
                print(f"  PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    for reason in _SKIPPED:
        print(f"  SKIP {reason}")
    print(
        f"\n  executed: {len(tests) - skipped}  passed: {passed}  failed: {failed}  "
        f"skipped: {skipped}  total: {len(tests)}"
    )
    sys.exit(1 if failed else 0)
