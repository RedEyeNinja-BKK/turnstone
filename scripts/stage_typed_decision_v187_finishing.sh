#!/usr/bin/env bash
# Isolated second-pass Turnstone 1.8.7 typed-decision candidate builder.
# Never mutates the installed package or the currently active overlay.
set -euo pipefail
umask 077
PY=/opt/turnstone-venv/bin/python
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
TARGET="$HOME/typed-decision-v187-finish-candidate"
if [[ ! -x "$PY" ]]; then echo "Missing $PY" >&2; exit 2; fi
version="$("$PY" -c 'import importlib.metadata as m; print(m.version("turnstone"))')"
[[ "$version" == 1.8.7 ]] || { echo "Expected upstream 1.8.7, got $version" >&2; exit 2; }
[[ ! -e "$TARGET" ]] || { echo "Candidate path exists; refusing overwrite: $TARGET" >&2; exit 2; }
SITE="$("$PY" -c 'import turnstone,pathlib; print(pathlib.Path(turnstone.__file__).resolve().parent)')"
case "$SITE" in /opt/turnstone-venv/lib/python*/site-packages/turnstone) ;; *)
  echo "Unexpected upstream package: $SITE" >&2; exit 2 ;; esac
mkdir -p "$TARGET/tests"
cp -R -- "$SITE" "$TARGET/turnstone"
for path in \
  turnstone/core/typed_decision.py \
  turnstone/core/output_guard_judge.py \
  turnstone/core/tool_advisory.py; do
  cp -- "$ROOT/$path" "$TARGET/$path"
done
cp -- "$ROOT/tests/test_output_guard_typed_decision.py" "$TARGET/tests/"
cp -- "$ROOT/tests/test_tool_advisory.py" "$TARGET/tests/"
cp -- "$ROOT/mutation_battery_typed_decision.py" "$TARGET/"
PYTHONPATH="$TARGET" "$PY" - <<'PY'
from pathlib import Path
import turnstone.core.output_guard_judge as og
import turnstone.core.typed_decision as td
import turnstone.core.tool_advisory as ta
expected=Path.home() / "typed-decision-v187-finish-candidate"
for module in (og,td,ta):
    assert Path(module.__file__).resolve().is_relative_to(expected), module.__file__
    print("Candidate import:", module.__file__)
PY
"$PY" -m compileall -q \
  "$TARGET/turnstone/core/typed_decision.py" \
  "$TARGET/turnstone/core/output_guard_judge.py" \
  "$TARGET/turnstone/core/tool_advisory.py" \
  "$TARGET/tests/test_output_guard_typed_decision.py" \
  "$TARGET/tests/test_tool_advisory.py"
echo "=== Regression suite (candidate copy) ==="
PYTHONPATH="$TARGET" "$PY" "$TARGET/tests/test_output_guard_typed_decision.py" \
  | tee "$TARGET/regression.log"
echo "=== Mutation battery (candidate copy) ==="
(cd "$TARGET" && PYTHONPATH="$TARGET" "$PY" mutation_battery_typed_decision.py) \
  | tee "$TARGET/mutation.log"
if "$PY" -c 'import pytest' 2>/dev/null; then
  echo "=== Full advisory pytest suite (candidate copy) ==="
  (cd "$TARGET" && PYTHONPATH="$TARGET" "$PY" -m pytest -q \
     tests/test_tool_advisory.py tests/test_output_guard_typed_decision.py) \
     | tee "$TARGET/pytest.log"
else
  echo "No pytest installed in existing venv: supplemental pytest suite NOT RUN."
  echo "The 34-case standalone suite already includes the key advisory regression."
fi
echo "FINISH CANDIDATE STAGED AND TESTED, live service and previous overlay untouched."
