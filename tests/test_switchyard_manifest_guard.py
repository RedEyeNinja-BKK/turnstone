# SPDX-License-Identifier: Apache-2.0
"""`default_decision_lane` placement and target rules.

Two measured failure modes drive these tests:

1. The deployed manifest declares the key inside a target's
   ``supported_types`` table, where TOML puts it in that table and the runner
   never sees a root key. The declaration is inert.
2. Moving it to the root as written fails ``--dry-run`` with
   ``default_decision_lane names tier1-decision, which is not a configured
   decision lane`` -- a COMPOSED lane is not in ``decision_lanes``.

So the tests assert both halves: a misplaced key is rejected, and a composed
lane can never be planned as a default even when a generator asks for it.

Run standalone::

    TMPDIR=/var/tmp /opt/turnstone-venv/bin/python tests/test_switchyard_manifest_guard.py
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load("switchyard_manifest_guard", "switchyard_manifest_guard.py")


# A manifest with the deployed shape: two target lanes and one composed lane,
# plus a decision target whose ``supported_types`` is a sub-table.
MANIFEST = """
schema_version = 1

[targets.direct-openrouter-mercury-decide-free]
id = "inception/mercury-decide:free"
llm_client = "openrouter"

[targets."direct-openrouter-mercury-decide-free".supported_types]
noul = true
choice = true
score = true

[decision_lanes.smartfree-decision]
targets = ["direct-openrouter-mercury-decide-free"]

[decision_lanes.smartlocal-decision]
targets = ["direct-htpc-jev-style"]

[decision_lane_composition.tier1-decision]
children = ["smartfree-decision", "smartlocal-decision"]
"""

# The deployed manifest's actual defect: the key inside the sub-table.
NESTED = MANIFEST.replace(
    "score = true\n",
    'score = true\ndefault_decision_lane = "tier1-decision"\n',
)

ROOT_COMPOSED = "default_decision_lane = \"tier1-decision\"\n" + MANIFEST
ROOT_TARGET_LANE = "default_decision_lane = \"smartfree-decision\"\n" + MANIFEST


def _doc(text: str) -> dict:
    import tomllib

    return tomllib.loads(text)


# --------------------------------------------------------------------------
# lint: placement
# --------------------------------------------------------------------------


def test_clean_manifest_has_no_violations() -> None:
    assert guard.lint_manifest_text(MANIFEST) == []


def test_nested_declaration_is_reported_as_inert() -> None:
    found = guard.lint_manifest_text(NESTED)
    assert any("inside [targets.direct-openrouter-mercury-decide-free.supported_types]" in f
               for f in found), found
    assert any("no effective default lane" in f for f in found), found


def test_inline_supported_types_table_is_also_caught() -> None:
    """The span-01-lite target uses an inline table; same rule must apply."""
    text = MANIFEST.replace(
        '[targets."direct-openrouter-mercury-decide-free".supported_types]\n'
        "noul = true\nchoice = true\nscore = true\n",
        'supported_types = { noul = true, default_decision_lane = "tier1-decision" }\n',
    )
    found = guard.lint_manifest_text(text)
    assert any("is therefore inert" in f for f in found), found


def test_nested_key_does_not_become_a_root_key() -> None:
    """Parsing proves the key lands in the table, not at root."""
    doc = _doc(NESTED)
    assert "default_decision_lane" not in doc
    nested = doc["targets"]["direct-openrouter-mercury-decide-free"]["supported_types"]
    assert nested["default_decision_lane"] == "tier1-decision"


# --------------------------------------------------------------------------
# lint: the value must name a real target lane
# --------------------------------------------------------------------------


def test_root_composed_lane_is_rejected() -> None:
    found = guard.lint_manifest_text(ROOT_COMPOSED)
    assert any("COMPOSED lane" in f and "cannot serve as a default" in f for f in found), found


def test_root_unknown_lane_is_rejected() -> None:
    found = guard.lint_manifest_text('default_decision_lane = "nope"\n' + MANIFEST)
    assert any("not a declared decision lane" in f for f in found), found


def test_root_target_lane_is_accepted() -> None:
    assert guard.lint_manifest_text(ROOT_TARGET_LANE) == []


def test_empty_root_value_is_rejected() -> None:
    found = guard.lint_manifest_text('default_decision_lane = ""\n' + MANIFEST)
    assert any("omit it instead" in f for f in found), found


def test_invalid_toml_is_a_violation_not_a_crash() -> None:
    found = guard.lint_manifest_text("this is not toml = = =")
    assert found and "not valid TOML" in found[0]


# --------------------------------------------------------------------------
# plan: what a generator is allowed to emit
# --------------------------------------------------------------------------


def test_plan_none_emits_nothing_for_the_current_configuration() -> None:
    """Current policy declares no default, so the correct output is no line."""
    assert guard.plan_default_decision_lane(_doc(MANIFEST), None) == []


def test_plan_refuses_a_composed_lane() -> None:
    try:
        guard.plan_default_decision_lane(_doc(MANIFEST), "tier1-decision")
    except guard.ManifestGuardError as exc:
        assert "composed lane" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a composed lane must never be planned as a default")


def test_plan_refuses_an_unknown_lane() -> None:
    try:
        guard.plan_default_decision_lane(_doc(MANIFEST), "no-such-lane")
    except guard.ManifestGuardError as exc:
        assert "not a declared decision lane" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unknown lane must be refused")


def test_plan_emits_a_root_line_for_a_target_lane() -> None:
    assert guard.plan_default_decision_lane(
        _doc(MANIFEST), "smartfree-decision"
    ) == ['default_decision_lane = "smartfree-decision"']


def test_planned_line_is_clean_when_placed_at_root() -> None:
    """The emitter's output must lint clean once a generator writes it."""
    lines = guard.plan_default_decision_lane(_doc(MANIFEST), "smartfree-decision")
    assert lines
    assert guard.lint_manifest_text("\n".join(lines) + "\n" + MANIFEST) == []


# --------------------------------------------------------------------------
# the deployed defect, repaired the way a generator must repair it
# --------------------------------------------------------------------------


def test_omitting_the_nested_declaration_yields_a_clean_manifest() -> None:
    """Our configuration: drop the inert line, declare no default."""
    repaired = NESTED.replace('default_decision_lane = "tier1-decision"\n', "")
    assert guard.lint_manifest_text(repaired) == []
    assert "default_decision_lane" not in _doc(repaired)


def test_repair_keeps_every_other_supported_type() -> None:
    repaired = NESTED.replace('default_decision_lane = "tier1-decision"\n', "")
    supported = _doc(repaired)["targets"]["direct-openrouter-mercury-decide-free"][
        "supported_types"
    ]
    assert supported == {"noul": True, "choice": True, "score": True}


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


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
