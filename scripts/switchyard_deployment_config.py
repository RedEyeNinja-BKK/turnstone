"""Where the Switchyard deployment lives, read from operator configuration.

The address of the deployment is configuration, not source. Two facts force
this apart from a constant in the script:

* A published copy of this repository must not name a private machine.
* The process that reaches the deployment does not necessarily carry the
  operator's environment. The MCP server spawns the runtime script once per
  call with a fresh environment, so an exported variable is not enough on its
  own -- something has to read the value from a file.

So the address lives in a file the operator owns, outside the repository, and
the scripts read it. The file is ``KEY=VALUE`` lines; ``#`` comments and blank
lines are ignored. Values are read literally: no shell expansion, no
``source``, no quoting rules. A line whose name is not one the caller asks
for is ignored, so one file can carry several settings.

The default path is ``/etc/turnstone/switchyard.env``; a different path is
named by ``SWITCHYARD_DEPLOYMENT_CONFIG``. A missing or unreadable file is not
an error: the caller then falls back to its own placeholder, which names no
reachable host. That keeps a plain checkout working, and it keeps a broken
configuration from turning into a crash.
"""

from __future__ import annotations

import os

DEFAULT_CONFIG_PATH = "/etc/turnstone/switchyard.env"
CONFIG_PATH_ENV = "SWITCHYARD_DEPLOYMENT_CONFIG"


def config_path() -> str:
    """The operator configuration file this process should read."""
    return os.environ.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH


def load(path: str | None = None) -> dict[str, str]:
    """Read the operator configuration file.

    A missing or unreadable file yields an empty mapping: the caller has a
    placeholder to fall back on, and a configuration problem should surface
    as "the deployment is unreachable", not as a traceback.
    """
    target = path if path is not None else config_path()
    try:
        with open(target, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator:
            continue
        name = name.strip()
        if not name:
            continue
        values[name] = value.strip()
    return values


def deployment_setting(name: str, default: str) -> str:
    """Resolve one setting: the environment first, then the file, then the default.

    The environment wins so an operator can override one value for a single
    invocation without editing the file.
    """
    from_env = os.environ.get(name)
    if from_env:
        return from_env
    return load().get(name) or default
