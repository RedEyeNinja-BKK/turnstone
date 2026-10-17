#!/usr/bin/env python3
"""Read-only MagicDNS readiness check for Turnstone's management peers.

WHAT IT ANSWERS, PER PEER
  dns      : does the OS resolver resolve the peer's MagicDNS name?
  magicdns : does the Tailscale resolver (100.100.100.100) answer for it?
  tcp      : is the SSH port reachable on the peer's tailnet address?
  hostkey  : does the live SSH host key match what is pinned in known_hosts?
  auth     : is the Turnstone client key authorized? (UNPROVEN unless --probe-auth)

WHY THE TWO DNS COLUMNS
  "DNS failed" is ambiguous. Splitting it separates two very different faults:

    magicdns=OK   dns=FAIL  -> MagicDNS itself is healthy and the OS resolver is
                               bypassing it. The repair is local resolver
                               integration, not Tailscale state.
    magicdns=FAIL dns=FAIL  -> the tailnet's own DNS view is broken (accept-dns,
                               tailnet settings, or tailscaled).

  Collapsing these into one "DNS broken" line is what let the earlier silent
  failure look like a peer problem.

AUTH IS NOT CLAIMED
  A pinned host key and a reachable port are NOT access. Unless --probe-auth is
  passed, authorization is reported UNPROVEN. The probe uses BatchMode with
  IdentitiesOnly and runs `true`, so it cannot mutate anything, but it is
  opt-in because it is a real authentication attempt.

READ-ONLY
  No file is written, no service is touched, no credential is read. It needs no
  privilege: the trust file is group-readable by the service account.

Usage:
    python3 scripts/turnstone_magicdns_check.py
    python3 scripts/turnstone_magicdns_check.py --json
    python3 scripts/turnstone_magicdns_check.py --probe-auth

Exit codes: 0 all healthy, 1 warnings, 2 a failure.
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

KNOWN_HOSTS = Path("/srv/turnstone/credentials/ssh/known_hosts")
HOSTS_FILE = Path("/etc/hosts")
MAGICDNS_SUFFIX = "tailc8ef7b.ts.net"
MAGICDNS_RESOLVER = "100.100.100.100"
SSH_PORT = 22
# Intended management peers. The tailnet address is the documented fallback and
# is used ONLY for reachability/hostkey checks -- it never counts as DNS health.
PEERS: tuple[tuple[str, str], ...] = (
    ("agent-vm", "100.127.26.70"),
    ("switchyard-vm", "100.65.106.53"),
)


@dataclass
class PeerReport:
    peer: str
    fqdn: str
    tailnet_ip: str
    dns: str = "UNKNOWN"
    dns_address: str = ""
    dns_source: str = "NONE"
    magicdns: str = "UNKNOWN"
    magicdns_address: str = ""
    tcp: str = "UNKNOWN"
    hostkey: str = "UNKNOWN"
    hostkey_fingerprint: str = ""
    pinned_fingerprint: str = ""
    auth: str = "UNPROVEN"
    notes: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.dns == "OK" and self.tcp == "OK" and self.hostkey in {"OK", "NOT_PINNED"}


def _run(argv: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 124, "", type(exc).__name__
    return proc.returncode, proc.stdout, proc.stderr


def hosts_file_entry(name: str, path: Path = HOSTS_FILE) -> str:
    """Return the /etc/hosts address for *name*, or "".

    A hosts entry is NOT DNS. It is exactly the static workaround that must not
    be mistaken for a working resolver: it silences the symptom on one host and
    hides the real fault, and it does not survive the next rebuild.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        aliases = {alias.rstrip(".") for alias in parts[1:]}
        if name.rstrip(".") in aliases:
            return parts[0]
    return ""


def resolve_via_os(name: str) -> tuple[str, str]:
    """Resolve through NSS (the path applications actually take)."""
    rc, out, _ = _run(["getent", "ahostsv4", name])
    if rc != 0 or not out.strip():
        return "FAIL", ""
    first = out.split()[0]
    return "OK", first


def resolve_via_magicdns(name: str) -> tuple[str, str]:
    """Ask the Tailscale resolver directly, bypassing the OS resolver."""
    dig = shutil.which("dig")
    if not dig:
        return "UNKNOWN", ""
    rc, out, _ = _run([dig, "+short", "+time=3", "+tries=1", f"@{MAGICDNS_RESOLVER}", name, "A"])
    if rc != 0:
        return "FAIL", ""
    answer = [line.strip() for line in out.splitlines() if line.strip()]
    if not answer:
        return "FAIL", ""
    return "OK", answer[0]


def check_tcp(host: str, port: int = SSH_PORT, timeout: float = 5.0) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "OK"
    except TimeoutError:
        return "TIMEOUT"
    except ConnectionRefusedError:
        return "REFUSED"
    except OSError:
        return "UNREACHABLE"


def pinned_fingerprint(host: str) -> str:
    """Fingerprint stored in known_hosts for *host*, or "" when absent."""
    keygen = shutil.which("ssh-keygen")
    if not keygen or not KNOWN_HOSTS.is_file():
        return ""
    rc, out, _ = _run([keygen, "-F", host, "-f", str(KNOWN_HOSTS)])
    if rc != 0 or not out.strip():
        return ""
    # Feed the matched line to ssh-keygen -lf to get the SHA256 fingerprint.
    line = next((ln for ln in out.splitlines() if ln and not ln.startswith("#")), "")
    if not line:
        return ""
    try:
        proc = subprocess.run(  # noqa: S603
            [keygen, "-lf", "/dev/stdin"],
            input=line + "\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    parts = proc.stdout.split()
    return parts[1] if len(parts) >= 2 else ""


def scan_fingerprint(host: str) -> tuple[str, str]:
    """Return (fingerprint, error_class) from a live ED25519 keyscan."""
    keyscan = shutil.which("ssh-keyscan")
    if not keyscan:
        return "", "KEYSSCAN_ABSENT"
    rc, out, err = _run([keyscan, "-T", "7", "-t", "ed25519", host], timeout=15)
    lines = [ln for ln in out.splitlines() if ln.split()[1:2] == ["ssh-ed25519"]]
    if not lines:
        low = err.lower()
        if "name or service not known" in low or "could not resolve" in low:
            return "", "DNS_RESOLUTION_FAILED"
        if "connection refused" in low:
            return "", "PORT_CLOSED"
        if "timed out" in low or "operation timed out" in low:
            return "", "UNREACHABLE"
        return "", f"KEYSSCAN_FAILED(rc={rc})"
    try:
        proc = subprocess.run(  # noqa: S603
            ["ssh-keygen", "-lf", "/dev/stdin"],
            input=lines[0] + "\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "", "FINGERPRINT_UNAVAILABLE"
    parts = proc.stdout.split()
    return (parts[1] if len(parts) >= 2 else ""), ""


def probe_auth(peer: str, host: str) -> str:
    """Opt-in: attempt a no-op authenticated command. Never mutates anything."""
    ssh = shutil.which("ssh")
    key = Path(f"/srv/turnstone/credentials/ssh/keys/turnstone-{peer}-ed25519")
    if not ssh or not key.is_file():
        return "UNPROVEN"
    rc, _, err = _run(
        [
            ssh,
            "-i", str(key),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=6",
            f"turnstone@{host}",
            "true",
        ],
        timeout=20,
    )
    if rc == 0:
        return "AUTHORIZED"
    low = err.lower()
    if "permission denied" in low:
        return "NOT_AUTHORIZED"
    if "host key verification failed" in low:
        return "HOSTKEY_UNTRUSTED"
    return "UNPROVEN"


def build_report(peer: str, ip: str, *, auth: bool) -> PeerReport:
    fqdn = f"{peer}.{MAGICDNS_SUFFIX}"
    report = PeerReport(peer=peer, fqdn=fqdn, tailnet_ip=ip)

    report.magicdns, report.magicdns_address = resolve_via_magicdns(fqdn)
    report.dns, report.dns_address = resolve_via_os(fqdn)
    # Attribute an OS success to its real source. A /etc/hosts answer is
    # reported as HOSTS_FILE and does not count as healthy DNS.
    if report.dns == "OK":
        host_entry = hosts_file_entry(fqdn)
        if host_entry and host_entry == report.dns_address:
            report.dns_source = "HOSTS_FILE"
            report.dns = "HOSTS_FILE"
            report.notes.append(
                f"resolved from /etc/hosts ({host_entry}), not from DNS; "
                "a static entry masks the resolver fault and will not survive a rebuild"
            )
        else:
            report.dns_source = "DNS"

    # Reachability is measured on the tailnet address so a DNS fault cannot mask
    # a connectivity result -- and so the IP fallback stays an honest datapoint.
    report.tcp = check_tcp(ip)

    fp, err = scan_fingerprint(ip)
    if err:
        report.hostkey = err
    else:
        report.hostkey_fingerprint = fp
        by_ip = pinned_fingerprint(ip)
        by_name = pinned_fingerprint(fqdn)
        report.pinned_fingerprint = by_ip or by_name
        if not by_ip and not by_name:
            report.hostkey = "NOT_PINNED"
        elif fp not in {by_ip, by_name}:
            report.hostkey = "MISMATCH"
        elif not by_name:
            report.hostkey = "OK"
            report.notes.append(
                f"pinned by address only; an SSH by name would fail strict checking "
                f"until {fqdn} is pinned too"
            )
        else:
            report.hostkey = "OK"

    if auth:
        report.auth = probe_auth(peer, ip)

    if report.dns == "HOSTS_FILE":
        report.notes.append(
            "MagicDNS answers correctly; treat this peer as NOT resolved by DNS "
            "until the OS resolver is integrated"
        )
    if report.dns == "FAIL" and report.magicdns == "OK":
        report.notes.append(
            "MagicDNS answers correctly; the OS resolver bypasses it "
            "(local resolver integration, not Tailscale state)"
        )
    if report.dns == "FAIL" and report.magicdns == "FAIL":
        report.notes.append("the tailnet's own DNS view does not answer for this peer")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only MagicDNS readiness check")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--probe-auth",
        action="store_true",
        help="attempt an authenticated no-op (off by default: a login attempt)",
    )
    args = parser.parse_args(argv)

    reports = [build_report(peer, ip, auth=args.probe_auth) for peer, ip in PEERS]
    healthy = all(r.healthy for r in reports)
    failed = [r for r in reports if r.healthy is False]

    if args.json:
        print(json.dumps(
            {"healthy": healthy, "peers": [asdict(r) for r in reports]}, indent=2
        ))
        return 0 if healthy else 2

    for r in reports:
        print(f"{r.peer} ({r.fqdn})")
        print(f"  magicdns   {r.magicdns:<10} {r.magicdns_address}")
        print(f"  dns (os)   {r.dns:<10} {r.dns_address}  [{r.dns_source}]")
        print(f"  tcp/{SSH_PORT}     {r.tcp}")
        print(f"  hostkey    {r.hostkey:<10} {r.hostkey_fingerprint}")
        print(f"  auth       {r.auth}")
        for note in r.notes:
            print(f"  note       {note}")
    print()
    print("MAGICDNS_READY" if healthy else "MAGICDNS_NOT_READY")
    return 0 if healthy else (2 if failed else 1)


if __name__ == "__main__":
    raise SystemExit(main())
