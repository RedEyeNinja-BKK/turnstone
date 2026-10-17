# SPDX-License-Identifier: Apache-2.0
"""MagicDNS readiness check and SSH host-pin failure classification.

Two things are pinned here.

1. A /etc/hosts entry must NEVER be reported as healthy DNS. That static
   workaround is what made this host look half-working: switchyard-vm resolved
   and agent-vm did not, so the fault read as a peer problem rather than a
   resolver-integration gap.

2. The host-pin helper must fail LOUDLY and SPECIFICALLY. Its original
   ssh-keyscan pipeline, under `set -o pipefail`, exited with no output at all --
   which is how a failed pin produced no visible evidence.

Everything here is offline: the hosts parser takes an explicit path and the
network-touching helpers are exercised through their classification inputs.

Run standalone::

    TMPDIR=/var/tmp /opt/turnstone-venv/bin/python tests/test_magicdns_readiness.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "turnstone_magicdns_check.py"
PIN_SCRIPT = _ROOT / "scripts" / "turnstone_ssh_host_pin.sh"

_spec = importlib.util.spec_from_file_location("magicdns_check", _SCRIPT)
assert _spec and _spec.loader
mdns = importlib.util.module_from_spec(_spec)
# dataclasses resolves annotations through sys.modules[cls.__module__], so the
# module must be registered before it is executed.
sys.modules["magicdns_check"] = mdns
_spec.loader.exec_module(mdns)

_TMP = "/var/tmp"


# ---------------------------------------------------------------------------
# hosts-file attribution
# ---------------------------------------------------------------------------


def _hosts(tmp: str, body: str) -> pathlib.Path:
    path = pathlib.Path(tmp) / "hosts"
    path.write_text(body, encoding="utf-8")
    return path


def test_hosts_entry_is_found_by_alias() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        path = _hosts(tmp, "10.0.0.9 other\n100.65.106.53 switchyard-vm.tailc8ef7b.ts.net switchyard-vm\n")
        assert mdns.hosts_file_entry("switchyard-vm.tailc8ef7b.ts.net", path) == "100.65.106.53"
        assert mdns.hosts_file_entry("switchyard-vm", path) == "100.65.106.53"


def test_hosts_entry_ignores_comments_and_absent_names() -> None:
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        path = _hosts(tmp, "# 1.2.3.4 commented.example\n127.0.0.1 localhost\n")
        assert mdns.hosts_file_entry("commented.example", path) == ""
        assert mdns.hosts_file_entry("absent.example", path) == ""


def test_hosts_entry_matches_whole_alias_only() -> None:
    """A substring must not be mistaken for an alias."""
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        path = _hosts(tmp, "10.0.0.1 agent-vm-extra\n")
        assert mdns.hosts_file_entry("agent-vm", path) == ""


def test_hosts_resolution_is_not_healthy_dns() -> None:
    report = mdns.PeerReport(
        peer="switchyard-vm",
        fqdn="switchyard-vm.tailc8ef7b.ts.net",
        tailnet_ip="100.65.106.53",
        dns="HOSTS_FILE",
        dns_source="HOSTS_FILE",
        tcp="OK",
        hostkey="OK",
    )
    assert report.healthy is False, "a static hosts entry must not count as DNS health"


def test_failed_dns_is_unhealthy_even_when_reachable() -> None:
    report = mdns.PeerReport(
        peer="agent-vm",
        fqdn="agent-vm.tailc8ef7b.ts.net",
        tailnet_ip="100.127.26.70",
        dns="FAIL",
        tcp="OK",
        hostkey="OK",
    )
    assert report.healthy is False


def test_healthy_requires_dns_tcp_and_hostkey() -> None:
    good = mdns.PeerReport(
        peer="p", fqdn="p.tailc8ef7b.ts.net", tailnet_ip="100.0.0.1",
        dns="OK", dns_source="DNS", tcp="OK", hostkey="OK",
    )
    assert good.healthy is True
    # NOT_PINNED is a warning, not a failure: the peer is reachable and its DNS works.
    unpinned = mdns.PeerReport(
        peer="p", fqdn="p.tailc8ef7b.ts.net", tailnet_ip="100.0.0.1",
        dns="OK", dns_source="DNS", tcp="OK", hostkey="NOT_PINNED",
    )
    assert unpinned.healthy is True


def test_peer_table_covers_the_management_set() -> None:
    peers = {name for name, _ip in mdns.PEERS}
    assert peers == {"agent-vm", "switchyard-vm"}
    assert mdns.MAGICDNS_SUFFIX == "tailc8ef7b.ts.net"


def test_magicdns_resolver_is_the_tailscale_stub() -> None:
    assert mdns.MAGICDNS_RESOLVER == "100.100.100.100"


# ---------------------------------------------------------------------------
# host-pin helper: loud, specific failures
# ---------------------------------------------------------------------------


def _run_pin(*args: str, timeout: int = 40) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", str(PIN_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_unresolvable_host_name_fails_loudly_as_dns() -> None:
    """THE regression: this used to exit with no output whatsoever."""
    proc = _run_pin("preflight", "agent-vm", "no-such-peer.tailc8ef7b.ts.net")
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert combined.strip(), "a failed pin must never be silent"
    assert "DNS_RESOLUTION_FAILED" in combined


def test_usage_error_is_specific() -> None:
    proc = _run_pin("preflight")
    assert proc.returncode != 0
    assert "Usage" in (proc.stdout + proc.stderr)


def test_pin_requires_root() -> None:
    import os

    if os.geteuid() == 0:
        return
    proc = _run_pin("pin", "agent-vm", "100.127.26.70")
    assert proc.returncode != 0
    assert "root" in (proc.stdout + proc.stderr).lower()


def test_preflight_does_not_require_root() -> None:
    """Read-only diagnosis must not need privilege."""
    import os

    if os.geteuid() == 0:
        return
    proc = _run_pin("preflight", "agent-vm", "no-such-peer.tailc8ef7b.ts.net")
    assert "requires root" not in (proc.stdout + proc.stderr).lower()


def test_helper_defines_every_failure_class() -> None:
    src = PIN_SCRIPT.read_text(encoding="utf-8")
    for token in (
        "DNS_RESOLUTION_FAILED",
        "PEER_UNREACHABLE",
        "PORT_CLOSED",
        "KEYSCAN_FAILED",
        "HOSTKEY_MISMATCH",
        "FINGERPRINT_UNREADABLE",
    ):
        assert token in src, f"missing failure class {token}"


def test_helper_does_not_swallow_keyscan_stderr() -> None:
    """The original bug: `ssh-keyscan ... 2>/dev/null | awk` under pipefail."""
    src = PIN_SCRIPT.read_text(encoding="utf-8")
    assert "ssh-keyscan -T 7 -t ed25519 \"$HOST\" 2>/dev/null |" not in src
    assert 'scan_err=' in src
    # The scan must be a plain command with its status captured, not a pipeline.
    assert '> "$tmp" 2> "$scan_err" || scan_rc=$?' in src


def _run_all() -> int:
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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
