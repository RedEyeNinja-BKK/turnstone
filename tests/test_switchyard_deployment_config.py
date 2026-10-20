# SPDX-License-Identifier: Apache-2.0
"""Where the Switchyard deployment address comes from.

The address of the deployment is configuration, not source. Two facts force it
apart from a constant in the script:

* a published copy of this repository must not name a private machine;
* the process that reaches the deployment does not necessarily carry the
  operator's environment -- the MCP server spawns the runtime script once per
  call with a fresh environment, so an exported variable alone is not enough.
  Something has to read the value from a file.

These tests pin the resolution order (environment, then file, then
placeholder) and the file grammar. They do not touch the network.

Run standalone::

    TMPDIR=/var/tmp /opt/turnstone-venv/bin/python tests/test_switchyard_deployment_config.py
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cfg = _load("switchyard_deployment_config", "switchyard_deployment_config.py")


def _write(directory: pathlib.Path, body: str) -> str:
    path = directory / "switchyard.env"
    path.write_text(body, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# file grammar
# --------------------------------------------------------------------------


def test_reads_plain_key_value_pairs(tmp_path=None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        path = _write(pathlib.Path(raw), "A=one\nB=two\n")
        assert cfg.load(path) == {"A": "one", "B": "two"}


def test_ignores_comments_and_blank_lines(tmp_path=None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        path = _write(
            pathlib.Path(raw),
            "# a comment\n\n   \nA=one\n  # indented comment\nB=two\n",
        )
        assert cfg.load(path) == {"A": "one", "B": "two"}


def test_keeps_a_value_that_contains_equals_signs(tmp_path=None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        path = _write(pathlib.Path(raw), "URL=https://host/v1?a=1&b=2\n")
        assert cfg.load(path)["URL"] == "https://host/v1?a=1&b=2"


def test_trims_surrounding_whitespace_but_not_inner(tmp_path=None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        path = _write(pathlib.Path(raw), "  A  =  one two  \n")
        assert cfg.load(path)["A"] == "one two"


def test_skips_lines_without_an_equals_sign(tmp_path=None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        path = _write(pathlib.Path(raw), "not a pair\nA=one\nalso-not-a-pair\n")
        assert cfg.load(path) == {"A": "one"}


def test_skips_a_pair_with_an_empty_name(tmp_path=None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        path = _write(pathlib.Path(raw), "=orphan\nA=one\n")
        assert cfg.load(path) == {"A": "one"}


def test_a_later_duplicate_wins(tmp_path=None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        path = _write(pathlib.Path(raw), "A=first\nA=second\n")
        assert cfg.load(path)["A"] == "second"


def test_an_empty_value_is_kept(tmp_path=None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        path = _write(pathlib.Path(raw), "A=\n")
        assert cfg.load(path)["A"] == ""


# --------------------------------------------------------------------------
# missing and unreadable files
# --------------------------------------------------------------------------


def test_a_missing_file_yields_nothing(tmp_path=None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        assert cfg.load(str(pathlib.Path(raw) / "absent.env")) == {}


def test_an_unreadable_path_yields_nothing(tmp_path=None) -> None:
    # A directory is not a file: reading it must fail closed, not raise.
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        directory = pathlib.Path(raw) / "a-directory"
        directory.mkdir()
        assert cfg.load(str(directory)) == {}


# --------------------------------------------------------------------------
# resolution order
# --------------------------------------------------------------------------


def test_the_environment_wins_over_the_file(tmp_path=None) -> None:
    import tempfile

    saved = os.environ.get("SWITCHYARD_SSH_TARGET")
    os.environ["SWITCHYARD_SSH_TARGET"] = "from-env"
    try:
        with tempfile.TemporaryDirectory() as raw:
            path = _write(pathlib.Path(raw), "SWITCHYARD_SSH_TARGET=from-file\n")
            saved_path = os.environ.get(cfg.CONFIG_PATH_ENV)
            os.environ[cfg.CONFIG_PATH_ENV] = path
            try:
                assert cfg.deployment_setting("SWITCHYARD_SSH_TARGET", "fallback") == "from-env"
            finally:
                if saved_path is None:
                    os.environ.pop(cfg.CONFIG_PATH_ENV, None)
                else:
                    os.environ[cfg.CONFIG_PATH_ENV] = saved_path
    finally:
        if saved is None:
            os.environ.pop("SWITCHYARD_SSH_TARGET", None)
        else:
            os.environ["SWITCHYARD_SSH_TARGET"] = saved


def test_the_file_is_used_when_the_environment_is_silent(tmp_path=None) -> None:
    import tempfile

    saved = os.environ.pop("SWITCHYARD_SSH_TARGET", None)
    saved_path = os.environ.get(cfg.CONFIG_PATH_ENV)
    try:
        with tempfile.TemporaryDirectory() as raw:
            path = _write(pathlib.Path(raw), "SWITCHYARD_SSH_TARGET=from-file\n")
            os.environ[cfg.CONFIG_PATH_ENV] = path
            assert cfg.deployment_setting("SWITCHYARD_SSH_TARGET", "fallback") == "from-file"
    finally:
        if saved is not None:
            os.environ["SWITCHYARD_SSH_TARGET"] = saved
        if saved_path is None:
            os.environ.pop(cfg.CONFIG_PATH_ENV, None)
        else:
            os.environ[cfg.CONFIG_PATH_ENV] = saved_path


def test_the_placeholder_is_used_when_nothing_is_configured(tmp_path=None) -> None:
    saved = os.environ.pop("SWITCHYARD_SSH_TARGET", None)
    saved_path = os.environ.get(cfg.CONFIG_PATH_ENV)
    os.environ[cfg.CONFIG_PATH_ENV] = "/nonexistent/switchyard.env"
    try:
        assert cfg.deployment_setting("SWITCHYARD_SSH_TARGET", "placeholder") == "placeholder"
    finally:
        if saved is not None:
            os.environ["SWITCHYARD_SSH_TARGET"] = saved
        if saved_path is None:
            os.environ.pop(cfg.CONFIG_PATH_ENV, None)
        else:
            os.environ[cfg.CONFIG_PATH_ENV] = saved_path


def test_an_empty_environment_value_does_not_shadow_the_file(tmp_path=None) -> None:
    import tempfile

    saved = os.environ.get("SWITCHYARD_SSH_TARGET")
    os.environ["SWITCHYARD_SSH_TARGET"] = ""
    saved_path = os.environ.get(cfg.CONFIG_PATH_ENV)
    try:
        with tempfile.TemporaryDirectory() as raw:
            path = _write(pathlib.Path(raw), "SWITCHYARD_SSH_TARGET=from-file\n")
            os.environ[cfg.CONFIG_PATH_ENV] = path
            assert cfg.deployment_setting("SWITCHYARD_SSH_TARGET", "fallback") == "from-file"
    finally:
        if saved is None:
            os.environ.pop("SWITCHYARD_SSH_TARGET", None)
        else:
            os.environ["SWITCHYARD_SSH_TARGET"] = saved
        if saved_path is None:
            os.environ.pop(cfg.CONFIG_PATH_ENV, None)
        else:
            os.environ[cfg.CONFIG_PATH_ENV] = saved_path


# --------------------------------------------------------------------------
# the runtime scripts use it
# --------------------------------------------------------------------------


def test_the_runtime_script_reads_the_file(tmp_path=None) -> None:
    # The runtime script is spawned with ``-I``, so the import of the sibling
    # module has to work without the script directory on ``sys.path``.
    import subprocess
    import tempfile

    saved = os.environ.pop("SWITCHYARD_SSH_TARGET", None)
    saved_path = os.environ.get("SWITCHYARD_DEPLOYMENT_CONFIG")
    try:
        with tempfile.TemporaryDirectory() as raw:
            path = _write(
                pathlib.Path(raw), "SWITCHYARD_SSH_TARGET=operator-host.example.invalid\n"
            )
            os.environ["SWITCHYARD_DEPLOYMENT_CONFIG"] = path
            proc = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(_SCRIPTS / "turnstone_switchyard_runtime.py"),
                    "status",
                ],
                capture_output=True,
                text=True,
                env=os.environ.copy(),
                check=False,
            )
            # The host is resolved at import time; the call itself fails to
            # connect, and the error text names the configured host.
            assert "operator-host.example.invalid" in proc.stdout + proc.stderr
    finally:
        if saved is not None:
            os.environ["SWITCHYARD_SSH_TARGET"] = saved
        if saved_path is None:
            os.environ.pop("SWITCHYARD_DEPLOYMENT_CONFIG", None)
        else:
            os.environ["SWITCHYARD_DEPLOYMENT_CONFIG"] = saved_path


def test_the_runtime_module_exposes_the_resolved_target() -> None:
    runtime = _load("switchyard_runtime_probe", "turnstone_switchyard_runtime.py")
    # The module must expose a target, and it must be the one the
    # configuration resolves to -- not a value baked into the script.
    assert runtime.SSH_TARGET
    expected = cfg.deployment_setting(
        "SWITCHYARD_SSH_TARGET", "turnstone-mgmt@switchyard-vm.example.invalid"
    )
    assert expected == runtime.SSH_TARGET


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
