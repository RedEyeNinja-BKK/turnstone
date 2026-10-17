# SPDX-License-Identifier: Apache-2.0
"""Executable failure-path tests for the combined activation.

`systemctl restart a b c` is not a transaction: it can fail after the first unit
is already on new code, a unit can come back unhealthy, or an environment
variable can be lost. These tests drive the REAL script against a stubbed
`systemctl`/`curl`/`journalctl` in a disposable sysroot, so the failure paths are
observed rather than argued from the source.

What is proven:

  * a partial restart rolls back every unit and exits nonzero;
  * an unhealthy port rolls back;
  * a lost environment key rolls back;
  * a failure during the drop-in write restarts nothing;
  * rollback removes only the package's drop-ins and proves the previous code;
  * a pre-existing superseded drop-in is refused before any write.

The stub models the only systemd facts the script reads (is-active, is-failed,
cat, show, daemon-reload, restart) and the health probe. `ACTIVATION_SYSROOT`
and `R2_SANDBOX=1` exist for this harness; both must be under /var/tmp, which
the script itself enforces.

Run standalone::

    TMPDIR=/var/tmp /opt/turnstone-venv/bin/python tests/test_activation_failure_paths.py
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

_ROOT = pathlib.Path(os.environ.get("R2_REPO") or pathlib.Path(__file__).resolve().parents[1])
SCRIPT = _ROOT / "scripts" / "stage_openai_oauth_v187.sh"
SLOT = "/var/tmp/activation-slot-placeholder"
ANCHOR = "/opt/turnstone-overlays/typed-decision-v187-finish"
UNITS = (
    "turnstone-server.service",
    "turnstone-node@2.service",
    "turnstone-node@3.service",
    "turnstone-node@4.service",
    "turnstone-node@5.service",
    "turnstone-console.service",
)
PORTS = ("8080", "8082", "8083", "8084", "8085", "8090")

STUB_SYSTEMCTL = r"""#!/usr/bin/env bash
set -u
S="$STUB_STATE"
PROCROOT="${PROCROOT:-$(dirname "$S")/procroot}"
cmd="${1:-}"; shift || true
case "$cmd" in
  is-active) cat "$S/active.$1" 2>/dev/null || echo inactive ;;
  is-failed) cat "$S/failed.$1" 2>/dev/null || echo "" ;;
  cat) [[ -f "$S/installed.$1" ]] ;; 
  daemon-reload) exit 0 ;;
  restart)
    n=0
    for u in "$@"; do
      n=$((n+1))
      if [[ -f "$S/fail_restart_after" ]] && (( n > $(cat "$S/fail_restart_after") )); then
        exit 1
      fi
      anchor=$(cat "$S/config.$u")
      # A template's drop-ins live under the TEMPLATE name and apply to every
      # instance, exactly as systemd merges them.
      base="$u"
      [[ "$u" == *@*.service ]] && base="${u%%@*}@.service"
      dropin="$ACTIVATION_SYSROOT/${base}.d/94-openai-oauth-v187-r2.conf"
      if [[ -f "$dropin" ]]; then
        eff=$(sed -n 's/^.*Environment="PYTHONPATH=\(.*\)"$/\1/p' "$dropin" | tail -1)
        [[ -n "$eff" ]] || eff="$anchor"
      else
        eff="$anchor"
      fi
      pid=$(cat "$S/pid.$u" 2>/dev/null || echo 0)
      mkdir -p "$PROCROOT/$pid"
      if [[ -f "$S/lose_env" && "$(cat "$S/lose_env")" == "$u" ]]; then
        printf 'PYTHONPATH=%s\n' "$eff" > "$S/env.$u"
        printf 'PYTHONPATH=%s\0' "$eff" > "$PROCROOT/$pid/environ"
      else
        printf 'PYTHONPATH=%s HOME=/var/lib/turnstone TURNSTONE_CONFIG=/etc/turnstone/config.toml\n' \
          "$eff" > "$S/env.$u"
        printf 'PYTHONPATH=%s\0HOME=/var/lib/turnstone\0TURNSTONE_CONFIG=/etc/turnstone/config.toml\0' \
          "$eff" > "$PROCROOT/$pid/environ"
      fi
    done
    exit 0 ;;
  show)
    prop="${2:-}"; val="${4:-}"
    case "$prop" in
      MainPID) cat "$S/pid.$val" 2>/dev/null || echo 0 ;;
      FragmentPath) echo "/etc/systemd/system/$val" ;;
      Environment)
        if [[ -f "$S/env.$val" ]]; then cat "$S/env.$val"; else
          printf 'PYTHONPATH=%s HOME=/var/lib/turnstone TURNSTONE_CONFIG=/etc/turnstone/config.toml\n' \
            "$(cat "$S/config.$val")"
        fi ;;
    esac ;;
  *) exit 0 ;;
esac
"""

STUB_CURL = r"""#!/usr/bin/env bash
# Invoked as: curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:PORT/health
url=""
for a in "$@"; do case "$a" in http*) url="$a" ;; esac; done
port="${url##*:}"; port="${port%%/*}"
[[ "$(cat "$STUB_STATE/health.$port" 2>/dev/null || echo 1)" == "1" ]] && exit 0
exit 22
"""

STUB_JOURNALCTL = r"""#!/usr/bin/env bash
if [[ -f "$STUB_STATE/journal_error" ]]; then cat "$STUB_STATE/journal_error"; fi
exit 0
"""


class Harness:
    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(dir="/var/tmp", prefix="activate-test.")
        self.root = pathlib.Path(self.tmp)
        self.sysroot = self.root / "etc-systemd"
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.slot = self.root / "slot"
        self.receipts = self.root / "receipts"
        self.procroot = self.root / "procroot"
        for d in (self.sysroot, self.state, self.bin, self.receipts, self.procroot):
            d.mkdir(parents=True, exist_ok=True)
        for name, body in (
            ("systemctl", STUB_SYSTEMCTL),
            ("curl", STUB_CURL),
            ("journalctl", STUB_JOURNALCTL),
        ):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        # A minimal slot the preflight will accept: marker files only.
        (self.slot / "turnstone" / "core").mkdir(parents=True)
        (self.slot / "turnstone" / "console").mkdir(parents=True)
        (self.slot / "turnstone" / "__init__.py").write_text("")
        (self.slot / "turnstone" / "core" / "oauth_token_store.py").write_text(
            "class OAuthTokenStore:\n    pass\n"
        )
        (self.slot / "turnstone" / "core" / "model_backend_auth.py").write_text(
            "def _resolve_direct_oauth_token():\n    pass\n"
            "def _require_direct_oauth_binding():\n    pass\n"
        )
        (self.slot / "turnstone" / "core" / "model_registry.py").write_text(
            "def list_generative_aliases():\n    pass\n"
            "follow_redirects=(\n"
        )
        (self.slot / "turnstone" / "core" / "providers").mkdir()
        (self.slot / "turnstone" / "core" / "providers" / "__init__.py").write_text(
            "def create_client(*, follow_redirects: bool | None = None):\n    pass\n"
        )
        (self.slot / "turnstone" / "console" / "server.py").write_text(
            "if auth_mode in DIRECT_OAUTH_MODEL_AUTH_MODES:\n    pass\n"
        )
        for index, u in enumerate(UNITS):
            (self.state / f"active.{u}").write_text("active\n")
            (self.state / f"failed.{u}").write_text("\n")
            (self.state / f"pid.{u}").write_text(f"{2000 + index}\n")
            (self.state / f"installed.{u}").write_text("1\n")
            anchor = "" if u == "turnstone-console.service" else ANCHOR
            (self.state / f"config.{u}").write_text(anchor + "\n")
            (self.state / f"proc.{u}").write_text(anchor + "\n")
            pd = self.procroot / str(2000 + index)
            pd.mkdir()
            (pd / "environ").write_text(
                f"PYTHONPATH={anchor}\0HOME=/var/lib/turnstone\0"
                "TURNSTONE_CONFIG=/etc/turnstone/config.toml\0"
            )
        for p in PORTS:
            (self.state / f"health.{p}").write_text("1\n")

    def dropin(self, unit: str) -> pathlib.Path:
        # Drop-ins for a template unit are filed under the template name.
        base = f"{unit.split('@')[0]}@.service" if "@" in unit else unit
        return self.sysroot / f"{base}.d" / "94-openai-oauth-v187-r2.conf"

    def env(self) -> dict[str, str]:
        e = os.environ.copy()
        e.update(
            {
                "PATH": f"{self.bin}:{e['PATH']}",
                "STUB_STATE": str(self.state),
                "R2_SANDBOX": "1",
                "OAUTH_ROOT": str(self.slot),
                "ACTIVATION_SYSROOT": str(self.sysroot),
                "ACTIVATION_RECEIPTS_DIR": str(self.receipts),
                "ACTIVATION_PROC_ROOT": str(self.procroot),
                "R2_REPO": str(_ROOT),
                "HEALTH_TIMEOUT": "3",
            }
        )
        return e

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv, stub PATH
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=self.env(),
            timeout=120,
            check=False,
        )

    def proc(self, unit: str) -> str:
        """The PYTHONPATH of the unit's simulated live process."""
        pid = 2000 + UNITS.index(unit)
        raw = (self.procroot / str(pid) / "environ").read_text()
        for field in raw.split("\0"):
            if field.startswith("PYTHONPATH="):
                return field.split("=", 1)[1]
        return ""

    def all_proc(self) -> set[str]:
        return {self.proc(u) for u in UNITS}

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def _with_harness(fn) -> None:
    h = Harness()
    try:
        fn(h)
    finally:
        h.cleanup()


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_happy_path_activates_and_records_a_receipt() -> None:
    def body(h: Harness) -> None:
        p = h.run("activate", "--confirm")
        assert p.returncode == 0, p.stderr + p.stdout
        assert h.all_proc() == {str(h.slot)}, h.all_proc()
        for u in UNITS:
            assert h.dropin(u).exists(), u
        assert list(h.receipts.glob("combined-v187-*.txt")), "no receipt written"
        assert list(h.receipts.glob("dropin-backup-*")), "no drop-in backup written"

    _with_harness(body)


# ---------------------------------------------------------------------------
# partial failures
# ---------------------------------------------------------------------------


def test_partial_restart_rolls_back_every_unit() -> None:
    """systemd fails after the second unit: the first is already new."""

    def body(h: Harness) -> None:
        (h.state / "fail_restart_after").write_text("2\n")
        p = h.run("activate", "--confirm")
        assert p.returncode != 0, "a partial restart must not report success"
        expected = {ANCHOR if u != "turnstone-console.service" else "" for u in UNITS}
        assert h.all_proc() == expected, f"units left on mixed code: {h.all_proc()}"
        for u in UNITS:
            assert not h.dropin(u).exists(), f"drop-in survived rollback: {u}"
        assert "ROLLBACK" in (p.stdout + p.stderr)

    _with_harness(body)


def test_unhealthy_port_rolls_back() -> None:
    def body(h: Harness) -> None:
        (h.state / "health.8084").write_text("0\n")
        p = h.run("activate", "--confirm")
        assert p.returncode != 0
        expected = {ANCHOR if u != "turnstone-console.service" else "" for u in UNITS}
        assert h.all_proc() == expected, h.all_proc()
        for u in UNITS:
            assert not h.dropin(u).exists()
        assert "no 2xx from port 8084" in (p.stdout + p.stderr + p.stderr)

    _with_harness(body)


def test_lost_environment_key_rolls_back() -> None:
    def body(h: Harness) -> None:
        (h.state / "lose_env").write_text("turnstone-console.service\n")
        p = h.run("activate", "--confirm")
        assert p.returncode != 0, "a lost environment variable must fail the switch"
        assert "ENV LOST" in (p.stdout + p.stderr)
        for u in UNITS:
            assert not h.dropin(u).exists()

    _with_harness(body)


def test_write_failure_restarts_nothing() -> None:
    def body(h: Harness) -> None:
        # Make the node drop-in directory unwritable by creating a FILE where
        # the directory must go. The server drop-in is written first, so this
        # also proves the partial write is unwound.
        (h.sysroot / "turnstone-node@.service.d").write_text("not a directory\n")
        before = {u: h.proc(u) for u in UNITS}
        p = h.run("activate", "--confirm")
        assert p.returncode != 0
        assert {u: h.proc(u) for u in UNITS} == before, "a write failure restarted something"
        assert not h.dropin("turnstone-server.service").exists(), (
            "the already-written drop-in was not unwound"
        )
        assert "nothing was restarted" in (p.stdout + p.stderr)

    _with_harness(body)


def test_journal_import_error_rolls_back() -> None:
    def body(h: Harness) -> None:
        (h.state / "journal_error").write_text("ModuleNotFoundError: No module named 'x'\n")
        p = h.run("activate", "--confirm")
        assert p.returncode != 0
        for u in UNITS:
            assert not h.dropin(u).exists()
        assert "fatal import error" in (p.stdout + p.stderr)

    _with_harness(body)


# ---------------------------------------------------------------------------
# refusals and rollback
# ---------------------------------------------------------------------------


def test_superseded_dropin_is_refused_before_any_write() -> None:
    def body(h: Harness) -> None:
        stale = h.sysroot / "turnstone-server.service.d" / "93-openai-oauth-v187.conf"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text('[Service]\nEnvironment="PYTHONPATH=/old"\n')
        p = h.run("activate", "--confirm")
        assert p.returncode != 0
        assert "superseded 93-" in p.stderr, p.stderr
        assert not h.dropin("turnstone-server.service").exists()
        assert h.all_proc() == {
            ANCHOR if u != "turnstone-console.service" else "" for u in UNITS
        }

    _with_harness(body)


def test_rollback_returns_to_the_anchor_and_verifies() -> None:
    def body(h: Harness) -> None:
        assert h.run("activate", "--confirm").returncode == 0
        p = h.run("rollback", "--confirm")
        assert p.returncode == 0, p.stderr + p.stdout
        assert h.all_proc() == {
            ANCHOR if u != "turnstone-console.service" else "" for u in UNITS
        }
        for u in UNITS:
            assert not h.dropin(u).exists()
        assert "ROLLBACK OK" in p.stdout

    _with_harness(body)


def test_already_active_is_idempotent_without_restart() -> None:
    def body(h: Harness) -> None:
        assert h.run("activate", "--confirm").returncode == 0
        # Any further restart would fail; proving no restart is attempted.
        (h.state / "fail_restart_after").write_text("0\n")
        p = h.run("activate", "--confirm")
        assert p.returncode == 0, p.stderr
        assert "ALREADY ACTIVE" in p.stdout
        assert "No restart performed" in p.stdout

    _with_harness(body)


if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_") and callable(o)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n  {passed} passed, {failed} failed, {len(tests)} total")
    sys.exit(1 if failed else 0)
