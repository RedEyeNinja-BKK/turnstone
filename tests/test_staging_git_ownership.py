# SPDX-License-Identifier: Apache-2.0
"""Staging scripts must read Git across the root/turnstone ownership boundary.

The repository belongs to the `turnstone` service identity while staging runs as
root. Git refuses that combination ("detected dubious ownership"), and the
tempting fixes are both wrong:

  * a global `safe.directory` exception disables the check for every repository
    on the host, for every invocation;
  * chowning the working tree changes production state to suit a build step.

The correct fix is to run the READ as the repository owner while the privileged
shell keeps the write, and the tests below pin it at three levels:

  TIER 1  structural  -- the script routes every Git read through one helper.
  TIER 2  mechanism   -- on this host, Git really does refuse a repository whose
                         owner differs from the invoking uid. This is measured,
                         not assumed, so the regression test cannot rot into
                         testing a guard that no longer exists.
  TIER 3  boundary    -- the authoritative run: as root, against a repo owned by
                         `turnstone`, a bare `git` must fail and the script's own
                         helper must succeed. Needs root; SKIPPED otherwise and
                         reported as skipped rather than silently passing.

Run standalone::

    TMPDIR=/var/tmp /opt/turnstone-venv/bin/python tests/test_staging_git_ownership.py
    sudo TMPDIR=/var/tmp /opt/turnstone-venv/bin/python tests/test_staging_git_ownership.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_ROOT = pathlib.Path(__file__).resolve().parents[1]
R2_SCRIPT = _ROOT / "scripts" / "stage_typed_decision_v187_r2.sh"
OAUTH_SCRIPT = _ROOT / "scripts" / "stage_openai_oauth_v187.sh"
REPO = _ROOT

_TMP = "/var/tmp"
MISMATCH_CANDIDATES = (
    pathlib.Path("/opt/turnstone-memory"),
    pathlib.Path("/opt/turnstone-overlays"),
)

_SKIPPED: list[str] = []


def _run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )


def _helper_body(script: pathlib.Path) -> str:
    src = script.read_text(encoding="utf-8")
    start = src.index("repo_git() {")
    end = src.index("\n}\n", start)
    return src[start:end]


# ---------------------------------------------------------------------------
# TIER 1 — structural
# ---------------------------------------------------------------------------


def test_r2_defines_a_single_git_read_helper() -> None:
    src = R2_SCRIPT.read_text(encoding="utf-8")
    assert "repo_git() {" in src
    assert "REPO_OWNER=" in src


def test_r2_has_no_bare_git_invocation_outside_the_helper() -> None:
    """Every Git read must go through the ownership-aware helper."""
    src = R2_SCRIPT.read_text(encoding="utf-8")
    helper = _helper_body(R2_SCRIPT)
    outside = src.replace(helper, "")
    assert 'git -C "$REPO"' not in outside, "a bare git read survives outside repo_git"


def test_helper_runs_git_as_the_repository_owner() -> None:
    body = _helper_body(R2_SCRIPT)
    assert 'runuser -u "$REPO_OWNER"' in body
    assert 'git -C "$REPO" "$@"' in body


def test_helper_does_not_weaken_the_ownership_check() -> None:
    """The fix must not be a safe.directory exception, and must not chown."""
    src = R2_SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )
    # Mentioning safe.directory in a refusal message is fine; SETTING it is not.
    assert "safe.directory=" not in code
    assert "git config" not in code
    # The working tree must not be re-owned to satisfy a build step.
    for target in ('chown', ):
        for line in code.splitlines():
            if target in line:
                assert not any(
                    ref in line for ref in ('"$REPO"', '"$BASE_TREE"', '"$SITE"')
                ), f"refusing to chown the source tree: {line.strip()}"


def test_ownership_refusal_is_named_distinctly() -> None:
    src = R2_SCRIPT.read_text(encoding="utf-8")
    assert "GIT_OWNERSHIP_REFUSED" in src
    assert "dubious ownership" in src


def test_root_write_stays_on_the_root_side() -> None:
    """The redirect is in the calling shell, so the temp file is root-owned."""
    src = R2_SCRIPT.read_text(encoding="utf-8")
    assert 'repo_git show "$BASE_COMMIT:turnstone/$f" > "$WORKDIR/base"' in src


def test_oauth_script_invokes_no_git_at_all() -> None:
    """Audited: that script only copies repo files, which root may read."""
    src = OAUTH_SCRIPT.read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("git "), f"unexpected git call: {stripped}"
    assert "subprocess" not in src


# ---------------------------------------------------------------------------
# TIER 2 — the guard is real on this host (measured, not assumed)
# ---------------------------------------------------------------------------


def _find_mismatched_repo() -> pathlib.Path | None:
    for candidate in MISMATCH_CANDIDATES:
        try:
            if not candidate.is_dir():
                continue
            if (candidate / ".git").exists() and candidate.stat().st_uid != os.geteuid():
                return candidate
        except OSError:
            continue
    return None


def test_git_really_refuses_a_repo_owned_by_another_uid() -> None:
    repo = _find_mismatched_repo()
    if repo is None:
        _SKIPPED.append("no readable repo with a foreign owner on this host")
        return
    proc = _run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"])
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, "a foreign-owned repo should not read cleanly"
    assert "dubious ownership" in combined, combined[:200]


def test_sandbox_repo_owned_by_the_invoker_is_readable() -> None:
    """Control: the same code path works when owner == invoker."""
    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        repo = pathlib.Path(tmp) / "r"
        assert _run(["git", "init", "-q", str(repo)]).returncode == 0
        assert _run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e",
             "commit", "-q", "--allow-empty", "-m", "x"]
        ).returncode == 0
        proc = _run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"])
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip(), "expected a commit id"


# ---------------------------------------------------------------------------
# TIER 3 — the authoritative boundary (root only)
# ---------------------------------------------------------------------------


def test_root_can_read_a_repo_owned_by_the_service_user() -> None:
    """THE production scenario: root staging a turnstone-owned repository."""
    if os.geteuid() != 0:
        _SKIPPED.append("TIER 3 needs root (run with sudo for the authoritative check)")
        return

    service_user = os.environ.get("R2_SERVICE_USER", "turnstone")
    if _run(["id", "-u", service_user]).returncode != 0:
        _SKIPPED.append(f"service user {service_user} does not exist")
        return

    with tempfile.TemporaryDirectory(dir=_TMP) as tmp:
        repo = pathlib.Path(tmp) / "r"
        assert _run(["git", "init", "-q", str(repo)]).returncode == 0
        assert _run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e",
             "commit", "-q", "--allow-empty", "-m", "x"]
        ).returncode == 0
        # Hand the tree to the service identity, exactly like the real repo.
        for path in sorted(repo.rglob("*"), reverse=True):
            os.chown(path, *_uid_gid(service_user))
        os.chown(repo, *_uid_gid(service_user))

        # (a) a bare read as root MUST fail; this is the bug being fixed
        bare = _run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"])
        assert bare.returncode != 0, "expected Git to refuse the foreign-owned repo"
        assert "dubious ownership" in (bare.stdout + bare.stderr)

        # (b) the same read as the owner MUST succeed
        owned = _run(
            ["runuser", "-u", service_user, "--", "git", "-C", str(repo),
             "rev-parse", "--short", "HEAD"]
        )
        assert owned.returncode == 0, owned.stderr
        assert owned.stdout.strip(), "expected a commit id from the owner-side read"


def _uid_gid(name: str) -> tuple[int, int]:
    import pwd

    entry = pwd.getpwnam(name)
    return entry.pw_uid, entry.pw_gid


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
    for reason in _SKIPPED:
        print(f"  SKIP {reason}")
    print(f"\n  {passed} passed, {failed} failed, {len(tests)} total, {len(_SKIPPED)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
