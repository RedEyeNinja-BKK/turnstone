# SPDX-License-Identifier: Apache-2.0
"""Generative-alias exclusion and typed-decision error provenance.

Two narrow corrections to the typed-decision lane (LC-RD-007 revision 2):

(a) PROVENANCE.  A typed-decision transport/execution failure must record the
    actual configured judge alias.  ``_error_verdict`` previously fell back to
    ``self._model`` — on the typed path that is the SESSION lane's raw model,
    because provider construction (which sets a resolved alias) is skipped
    deliberately.  The recorded ``judge_model`` therefore named the wrong model
    in the audit trail.  Risk classification and fail-closed behaviour are
    unchanged: the verdict stays ``risk_level="none"`` under ``tier="llm_error"``.

(b) ALIAS EXPOSURE.  A registry row declaring ``supports_typed_decision``
    answers with a typed decision object and generates no assistant text, so
    offering it as a session/task-agent model choice is a guaranteed failure
    (measured: ``POST /responses`` -> 404, ``POST /v1/responses`` ->
    404 model_not_found).  Such rows must disappear from the *choice* surfaces
    while remaining in the authoritative registry and fully executable by the
    Output Guard's semantic tier.

Run standalone (matches the deployment receipt format)::

    PYTHONPATH=<tree> /opt/turnstone-venv/bin/python tests/test_generative_alias_exclusion.py
"""
from __future__ import annotations

import inspect
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from turnstone.core.model_registry import ModelConfig, ModelRegistry  # noqa: E402
from turnstone.core.output_guard_judge import (  # noqa: E402
    OutputGuardJudge,
    _typed_decision_spec,
)
from turnstone.core.typed_decision import TypedDecisionSpec  # noqa: E402

TYPED_CAPS = {
    "supports_typed_decision": True,
    "decision_contract": "switchyard-decision:v1",
    "decision_types": ["noul"],
    "decision_endpoint": "/v1/decisions",
    "server_compat": {"api_surface": "responses"},
}


def _cfg(alias: str, caps: dict[str, Any] | None = None) -> ModelConfig:
    return ModelConfig(
        alias=alias,
        base_url="https://example.invalid/v1",
        api_key="",
        model=alias,
        provider="openai-compatible",
        capabilities=dict(caps or {}),
    )


def _registry() -> ModelRegistry:
    """A registry shaped like production: two generative rows + one typed row."""
    return ModelRegistry(
        models={
            "smart-bounded": _cfg("smart-bounded"),
            "deepseek-deepseek-flash": _cfg("deepseek-deepseek-flash"),
            "tier1-decision": _cfg("tier1-decision", TYPED_CAPS),
        },
        default="smart-bounded",
    )


# ---------------------------------------------------------------------------
# (b) generative alias exclusion
# ---------------------------------------------------------------------------


def test_typed_alias_excluded_from_generative_aliases() -> None:
    assert "tier1-decision" not in _registry().list_generative_aliases()


def test_generative_aliases_keeps_ordinary_models() -> None:
    generative = _registry().list_generative_aliases()
    assert "smart-bounded" in generative
    assert "deepseek-deepseek-flash" in generative


def test_typed_alias_remains_in_authoritative_list_aliases() -> None:
    """Exclusion is a *choice-surface* filter, never a registry deletion.

    The admin Models tab and the reload endpoint enumerate ``list_aliases()``;
    a typed row must stay visible and manageable there.
    """
    reg = _registry()
    assert "tier1-decision" in reg.list_aliases()
    assert reg.get_config("tier1-decision").capabilities.get("supports_typed_decision")


def test_typed_alias_still_resolvable_by_output_guard() -> None:
    """The Output Guard must still detect and execute the typed row."""
    spec = _typed_decision_spec(_registry(), "tier1-decision")
    assert isinstance(spec, TypedDecisionSpec)
    assert spec.contract == "switchyard-decision:v1"
    assert spec.endpoint == "/v1/decisions"


def test_guard_treats_ordinary_aliases_as_generative() -> None:
    assert _typed_decision_spec(_registry(), "smart-bounded") is None


def test_session_choice_expression_excludes_typed_alias() -> None:
    """The exact expression session.py uses for task-agent choices."""
    reg = _registry()
    choices = sorted(a for a in reg.list_generative_aliases() if a != "default")
    assert choices == ["deepseek-deepseek-flash", "smart-bounded"]


def test_session_module_advertises_only_generative_aliases() -> None:
    """Wiring guard: every session.py choice surface uses the filtered accessor.

    The invariant under test is the ABSENCE of an unfiltered enumeration on a
    choice surface; the count is a floor, not an exact figure, so a legitimate
    refactor that adds or merges choice surfaces does not break the guard.
    """
    import turnstone.core.session as session_mod

    src = inspect.getsource(session_mod)
    assert src.count("list_generative_aliases()") >= 5
    # No session-side choice surface may still enumerate the unfiltered list.
    assert "self._registry.list_aliases()" not in src
    assert "self._registry.list_generative_aliases()" in src


def test_cli_banner_excludes_typed_aliases() -> None:
    """The interactive banner advertises models the user can SELECT.

    Read as source text rather than importing turnstone.cli: the module pulls
    the full interactive dependency stack, which a unit test should not need.
    """
    cli = pathlib.Path(__file__).resolve().parents[1] / "turnstone" / "cli.py"
    src = cli.read_text(encoding="utf-8")
    assert "registry.list_generative_aliases()" in src
    assert "registry.list_aliases()" not in src


# ---------------------------------------------------------------------------
# (a) typed-decision error-verdict provenance
# ---------------------------------------------------------------------------


def _bare_judge(**attrs: Any) -> OutputGuardJudge:
    """A judge instance carrying only the attributes ``_error_verdict`` reads."""
    judge = object.__new__(OutputGuardJudge)
    judge._judge_model_alias = attrs.get("judge_model_alias", "")
    judge._model = attrs.get("model", "")
    judge._typed_alias = attrs.get("typed_alias", "")
    return judge


def test_typed_error_verdict_records_judge_alias_not_session_model() -> None:
    """MEASURED BASELINE: this recorded 'deepseek-flash' (the session model)."""
    judge = _bare_judge(model="deepseek-flash", typed_alias="tier1-decision")
    verdict = judge._error_verdict("vid", "cid", time.monotonic(), "typed_decision: boom")
    assert verdict.judge_model == "tier1-decision"


def test_typed_error_verdict_fails_closed() -> None:
    """Fail-closed classification is unchanged: never a 'clean' success row."""
    judge = _bare_judge(model="deepseek-flash", typed_alias="tier1-decision")
    verdict = judge._error_verdict("vid", "cid", time.monotonic(), "typed_decision: boom")
    assert verdict.risk_level == "none"
    assert verdict.error == "typed_decision: boom"
    assert verdict.confidence == 0.0


def test_generative_error_verdict_unchanged_when_alias_resolved() -> None:
    judge = _bare_judge(model="tier1-decision", judge_model_alias="smart-bounded")
    verdict = judge._error_verdict("vid", "cid", time.monotonic(), "judge_retired")
    assert verdict.judge_model == "smart-bounded"


def test_generative_error_verdict_still_falls_back_to_session_model() -> None:
    """The pre-existing default-config behaviour must not regress."""
    judge = _bare_judge(model="deepseek-flash")
    verdict = judge._error_verdict("vid", "cid", time.monotonic(), "cancelled")
    assert verdict.judge_model == "deepseek-flash"


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
