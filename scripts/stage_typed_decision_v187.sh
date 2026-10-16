#!/usr/bin/env bash
# Stage/test the narrowly scoped typed-decision adapter against a byte-for-byte
# copy of the INSTALLED Turnstone 1.8.7 package. Never touches live site-packages,
# systemd, PostgreSQL, model definitions, or provider settings.
set -euo pipefail
PY="/opt/turnstone-venv/bin/python"
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${HOME}/typed-decision-v187-candidate"
if [[ ! -x "$PY" ]]; then echo "Missing interpreter: $PY" >&2; exit 2; fi
VERSION="$("$PY" -c 'import importlib.metadata; print(importlib.metadata.version("turnstone"))')"
if [[ "$VERSION" != "1.8.7" ]]; then
  echo "Expected installed turnstone==1.8.7, found $VERSION. Refusing." >&2
  exit 2
fi
if [[ -e "$TARGET" ]]; then
  echo "Candidate already exists: $TARGET. Refusing overwrite." >&2
  exit 2
fi
SITE="$("$PY" -c 'import pathlib,turnstone; print(pathlib.Path(turnstone.__file__).resolve().parent)')"
case "$SITE" in
 /opt/turnstone-venv/lib/python*/site-packages/turnstone) ;;
 *) echo "Unexpected live package path: $SITE. Refusing." >&2; exit 2 ;;
esac
echo "Installed package: $SITE"
echo "Candidate path:    $TARGET"
mkdir -p "$TARGET"
cp -R -- "$SITE" "$TARGET/turnstone"
mkdir -p "$TARGET/tests"
cp -- "$SOURCE/turnstone/core/typed_decision.py" "$TARGET/turnstone/core/typed_decision.py"
cp -- "$SOURCE/turnstone/core/output_guard_judge.py" "$TARGET/turnstone/core/output_guard_judge.py"
cp -- "$SOURCE/tests/test_output_guard_typed_decision.py" "$TARGET/tests/test_output_guard_typed_decision.py"
cp -- "$SOURCE/mutation_battery_typed_decision.py" "$TARGET/mutation_battery_typed_decision.py"
# Verify *candidate* module path, not live site-packages, is imported.
PYTHONPATH="$TARGET" "$PY" - <<'PY'
import pathlib
import turnstone.core.typed_decision as typed
import turnstone.core.output_guard_judge as guard
assert "/typed-decision-v187-candidate/" in str(pathlib.Path(typed.__file__))
assert "/typed-decision-v187-candidate/" in str(pathlib.Path(guard.__file__))
print("Candidate imports:", typed.__file__, guard.__file__, sep="\n  ")
PY
echo "=== Regression suite ==="
PYTHONPATH="$TARGET" "$PY" "$TARGET/tests/test_output_guard_typed_decision.py" | tee "$TARGET/regression.log"
echo "=== Mutation battery (candidate copy only) ==="
(cd "$TARGET" && PYTHONPATH="$TARGET" "$PY" mutation_battery_typed_decision.py) | tee "$TARGET/mutation.log"
echo "STAGING TESTS COMPLETE. Candidate only; live Turnstone unchanged."
