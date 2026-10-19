#!/usr/bin/env python3
"""Print the files the S6 reporters LOAD at runtime, transitively.

The reporters do not import each other through sys.path: they resolve their
siblings by path at import time
(``importlib.util.spec_from_file_location(..., HERE / "collect.py")``).
A file reached that way is trusted code - it runs inside the report - and if
the operator's hash chain does not pin it, the chain certifies an artifact
it never looked at.

This walks the real loader calls, so the answer is the ACTUAL closure rather
than a hand-maintained list that drifts.

Usage: chain_closure.py <s6-dir>
Prints one relative path per line, sorted, including the reporters themselves.
"""
from __future__ import annotations

import pathlib
import re
import sys

#: ``spec_from_file_location("name", <expr>)`` - the second argument is the
#: path expression. Only sibling-relative forms are followed; anything else
#: (an absolute path, a database handle) is not a source file.
CALL = re.compile(
    r"spec_from_file_location\(\s*[\"'][^\"']+[\"']\s*,\s*"
    r"(?:pathlib\.Path\(__file__\)\.resolve\(\)\.parent|HERE)\s*/\s*"
    r"[\"']([^\"']+)[\"']"
)


def closure(root: pathlib.Path) -> set[str]:
    """Every source file reachable by the loader from the reporters."""
    seen: set[str] = set()
    queue = ["collect.py", "liveness.py"]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        path = root / name
        if not path.is_file():
            continue
        seen.add(name)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for target in CALL.findall(text):
            if target not in seen:
                queue.append(target)
    return seen


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    root = pathlib.Path(argv[1]).resolve()
    for name in sorted(closure(root)):
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
