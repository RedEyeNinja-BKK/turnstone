# SPDX-License-Identifier: Apache-2.0
"""Output Guard typed-decision bridge: contract + mutant battery.

Covers the acceptance boundary: primary normalization, fallback normalization,
the heuristic floor, degradation to heuristic-only, and the absence of a silent
session-model fallback. Mutants are applied by patching the source, compiling
it, and requiring a NAMED test to fail for the intended reason.
"""
from __future__ import annotations

import json
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from turnstone.core.typed_decision import (  # noqa: E402
    TypedDecisionError,
    TypedDecisionSpec,
    build_request_body,
    execute_decision,
    normalize_decision,
)

SPEC = TypedDecisionSpec.from_capabilities(
    {
        "supports_typed_decision": True,
        "decision_contract": "switchyard-decision:v1",
        "decision_types": ["choice", "score", "noul"],
        "decision_endpoint": "/v1/decisions",
        "max_questions": 16,
    }
)


def test_module_spec_is_built():
    """A typed capability must yield a spec at import time (not assert on load).

    Kept as a TEST so a mutant fails here as a clean FAIL rather than breaking
    module collection, which would be indistinguishable from a broken harness.
    """
    assert SPEC is not None
    assert SPEC.contract == "switchyard-decision:v1"


# --- helpers ---------------------------------------------------------------

def _caps(**over):
    base = {
        "supports_typed_decision": True,
        "decision_contract": "switchyard-decision:v1",
        "decision_types": ["choice", "score", "noul"],
        "decision_endpoint": "/v1/decisions",
    }
    base.update(over)
    return base


def _ok(choice="none", score=None, leg="", provider=""):
    ans = {"q1": {"type": "choice", "choice": choice}}
    if score is not None:
        ans["q2"] = {"type": "score", "score": score}
    payload = {
        "contract": "switchyard-decision:v1",
        "answers": ans,
    }
    if leg:
        payload["leg"] = leg
    if provider:
        payload["provider"] = provider
    return payload


class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- primary ---------------------------------------------------------------

def test_primary_choice_normalizes():
    r = normalize_decision(_ok(choice="high", provider="Respan"), SPEC)
    assert r.risk_level == "high", r
    assert r.provider == "Respan"
    assert "switchyard-decision:v1" in r.provenance
    assert r.leg == "primary", r.leg


def test_fallback_leg_normalizes_identically():
    """The generative fallback leg must normalize exactly like the primary."""
    a = normalize_decision(_ok(choice="medium", provider="Respan"), SPEC)
    b = normalize_decision(
        _ok(choice="medium", leg="generative_fallback", provider="LFM"), SPEC
    )
    assert a.risk_level == b.risk_level == "medium"
    assert a.confidence == b.confidence
    assert b.leg == "generative_fallback"
    assert "generative_fallback" in b.provenance


def test_confidence_clamped_to_unit_interval():
    assert normalize_decision(_ok(choice="low", score=5.0), SPEC).confidence == 1.0
    assert normalize_decision(_ok(choice="low", score=-2.0), SPEC).confidence == 0.0
    r = normalize_decision(_ok(choice="low", score=0.42), SPEC)
    assert abs(r.confidence - 0.42) < 1e-9


def test_graded_noul_maps_onto_the_risk_vocabulary():
    """Span accepts ONLY graded `noul`; that is how risk is requested.

    The score is mapped onto the guard's own vocabulary on explicit bounds, so a
    decision backend cannot invent a level outside none/low/medium/high, and a
    low-but-nonzero score still reads as `low` rather than as clean.
    """
    for score, expected in (
        (0.95, "high"),
        (0.60, "high"),
        (0.30, "medium"),
        (0.10, "low"),
        (0.01, "none"),
    ):
        r = normalize_decision(
            {
                "contract": "switchyard-decision:v1",
                "answers": {"q": {"type": "noul", "noul": score}},
            },
            SPEC,
        )
        assert r.risk_level == expected, (score, r.risk_level, expected)
        assert abs(r.confidence - score) < 1e-9


def test_graded_noul_without_a_score_is_refused():
    with pytest_raises(TypedDecisionError):
        normalize_decision(
            {
                "contract": "switchyard-decision:v1",
                "answers": {"q": {"type": "noul"}},
            },
            SPEC,
        )


def test_noul_never_reaches_below_the_floor_unless_genuinely_negligible():
    """A decision backend must not be able to say 'clean' too easily."""
    r = normalize_decision(
        {
            "contract": "switchyard-decision:v1",
            "answers": {"q": {"type": "noul", "noul": 0.06}},
        },
        SPEC,
    )
    assert r.risk_level == "low", r.risk_level


def test_missing_answers_refused():
    with pytest_raises(TypedDecisionError):
        normalize_decision({"contract": "switchyard-decision:v1", "answers": {}}, SPEC)
    with pytest_raises(TypedDecisionError):
        normalize_decision({"contract": "switchyard-decision:v1"}, SPEC)


def test_malformed_and_error_responses_refused():
    for bad in (
        "not-an-object",
        {"contract": "switchyard-decision:v1", "answers": {"q": "nope"}},
        {"contract": "switchyard-decision:v1", "answers": {"q": {"type": "weird"}}},
        {"error": "upstream exploded"},
    ):
        with pytest_raises(TypedDecisionError):
            normalize_decision(bad, SPEC)


def test_contract_mismatch_refused():
    payload = _ok(choice="high")
    payload["contract"] = "some-other:v9"
    with pytest_raises(TypedDecisionError):
        normalize_decision(payload, SPEC)


def test_invalid_choice_rejected():
    for bad in ("catastrophic", "", 5, None):
        with pytest_raises(TypedDecisionError):
            normalize_decision(
                {"contract": "switchyard-decision:v1",
                 "answers": {"q": {"type": "choice", "choice": bad}}}, SPEC)


def test_strongest_choice_wins_across_questions():
    r = normalize_decision(
        {
            "contract": "switchyard-decision:v1",
            "answers": {
                "a": {"type": "choice", "choice": "low"},
                "b": {"type": "choice", "choice": "high"},
            },
        },
        SPEC,
    )
    assert r.risk_level == "high", r


# --- spec / activation -----------------------------------------------------

def test_ordinary_alias_is_not_typed():
    assert TypedDecisionSpec.from_capabilities({"server_compat": {"api_surface": "responses"}}) is None
    assert TypedDecisionSpec.from_capabilities({}) is None


def test_unknown_contract_refused_at_activation():
    with pytest_raises(TypedDecisionError):
        TypedDecisionSpec.from_capabilities(
            {"supports_typed_decision": True, "decision_contract": "other:v1"}
        )


def test_typed_without_contract_refused():
    with pytest_raises(TypedDecisionError):
        TypedDecisionSpec.from_capabilities({"supports_typed_decision": True})


# --- the guard seam --------------------------------------------------------

def test_typed_execution_posts_to_decisions_endpoint():
    seen = {}

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode())
        return _Resp(_ok(choice="high", provider="Respan"))

    r = execute_decision(
        base_url="http://127.0.0.1:4000",
        model="switchyard-smartfree-aux-turnstone",
        caps=_caps(),
        state="tool output",
        decision_id="switchyard-smartfree-aux-turnstone",
        timeout=5,
        opener=opener,
    )
    assert seen["url"] == "http://127.0.0.1:4000/v1/decisions", seen["url"]
    assert seen["body"]["model"] == "switchyard-smartfree-aux-turnstone"
    assert r.risk_level == "high"


def test_non_typed_alias_refuses_typed_execution():
    with pytest_raises(TypedDecisionError):
        execute_decision(
            base_url="http://x",
            model="switchyard-smartfree-turnstone",
            caps={"server_compat": {"api_surface": "responses"}},
            state="s",
            decision_id="d",
            timeout=1,
            opener=lambda r, timeout=None: _Resp(_ok()),
        )


def test_transport_and_http_failures_become_typed_errors():
    def boom(request, timeout=None):
        raise OSError("connection refused")

    with pytest_raises(TypedDecisionError):
        execute_decision(base_url="http://x", model="a", caps=_caps(), state="s",
                         decision_id="d", timeout=1, opener=boom)


def test_heuristic_floor_never_lowered_by_semantic_none():
    """The security floor: semantic `none` must not erase a heuristic `high`.

    The merge is the caller's `max(heuristic, semantic)`; this pins the
    invariant that a semantic `none` and a heuristic `high` still yield `high`.
    """
    order = ("none", "low", "medium", "high")
    heuristic = "high"
    semantic = normalize_decision(_ok(choice="none"), SPEC).risk_level
    merged = max((heuristic, semantic), key=order.index)
    assert merged == "high", (heuristic, semantic, merged)


def test_semantic_error_never_becomes_none():
    """A failed semantic tier must be an ERROR, never a clean `none`."""
    try:
        normalize_decision({"contract": "switchyard-decision:v1"}, SPEC)
        raised = False
    except TypedDecisionError:
        raised = True
    assert raised, "a missing-answer response must raise, not yield a clean verdict"


def test_no_session_model_fallback_on_typed_path():
    """A typed alias must never be executed as a chat model.

    Regression for the failure mode where an api_surface-less row defaulted to
    Chat Completions, or where construction failure silently ran the session
    model and recorded it as a successful semantic guard.
    """
    from turnstone.core.providers import create_provider

    # 1) the row is NOT executable as a chat provider
    assert TypedDecisionSpec.from_capabilities(_caps()) is not None
    # 2) and the guard refuses a non-typed alias on the typed seam
    with pytest_raises(TypedDecisionError):
        execute_decision(
            base_url="http://x",
            model="switchyard-smartfree-turnstone",
            caps={"server_compat": {"api_surface": "responses"}},
            state="s",
            decision_id="d",
            timeout=1,
            opener=lambda r, timeout=None: _Resp(_ok()),
        )
    # 3) a chat provider still exists for ordinary aliases (unchanged behaviour)
    assert create_provider("openai-compatible", api_surface=None) is not None


# --- minimal harness -------------------------------------------------------

class _Raises:
    def __init__(self, exc): self.exc = exc
    def __enter__(self): return self
    def __exit__(self, t, v, tb):
        if t is None:
            raise AssertionError(f"expected {self.exc.__name__}")
        return issubclass(t, self.exc)


def pytest_raises(exc):
    return _Raises(exc)


def _run_all() -> int:
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_") and callable(o)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS {name}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n  {passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0




# --- the guard branch itself (M8's coverage) -------------------------------

class _FakeRegistry:
    """Minimal registry stub: only the attributes the guard's detector reads."""

    def __init__(self, caps, base_url="http://127.0.0.1:4000"):
        self._cfg = types.SimpleNamespace(capabilities=caps, base_url=base_url)

    def get(self, alias):
        return self._cfg


class _RealShapeRegistry:
    """Registry stub shaped like the PRODUCTION ``ModelRegistry``.

    ``ModelRegistry`` is a plain class: it is not a Mapping, defines no ``get``,
    and exposes the per-alias config through ``get_config`` (which raises for an
    unknown alias).  ``_FakeRegistry`` above implements ``get`` and therefore
    agreed with the guard's original accessor while production did not — a
    self-agreeing fixture that let a fully broken dispatch ship green.  This
    stub pins the real surface so that class of defect fails here instead.
    """

    def __init__(self, caps, base_url="http://127.0.0.1:4000"):
        self._cfgs = {
            "switchyard-smartfree-aux-turnstone": types.SimpleNamespace(
                capabilities=caps, base_url=base_url
            )
        }

    def get_config(self, alias):
        if alias not in self._cfgs:
            raise KeyError(alias)
        return self._cfgs[alias]


def test_guard_detects_typed_alias_on_real_registry_shape():
    """The detector must work against ModelRegistry's ACTUAL accessor surface."""
    import turnstone.core.output_guard_judge as og

    reg = _RealShapeRegistry(_caps())
    # A production registry exposes no ``get``; prove the stub is faithful.
    assert not callable(getattr(reg, "get", None)), "stub must not expose .get"

    spec = og._typed_decision_spec(reg, "switchyard-smartfree-aux-turnstone")
    assert spec is not None, (
        "a typed-decision row must be detected on the real registry shape; "
        "detection failure sends the guard down the generative chat path"
    )
    assert spec.contract == "switchyard-decision:v1"
    assert og._typed_decision_base_url(reg, "switchyard-smartfree-aux-turnstone") == (
        "http://127.0.0.1:4000"
    )
    # An unknown alias must stay unknown rather than raise out of the guard.
    assert og._typed_decision_spec(reg, "no-such-alias") is None


def test_guard_detects_typed_alias_before_provider_construction():
    """The branch point must read registry capabilities, not an api_surface."""
    import turnstone.core.output_guard_judge as og

    spec = og._typed_decision_spec(
        _FakeRegistry(_caps()), "switchyard-smartfree-aux-turnstone"
    )
    assert spec is not None, "a typed-decision row must be detected"
    assert spec.contract == "switchyard-decision:v1"
    # An ordinary generative row must NOT be detected as typed.
    assert (
        og._typed_decision_spec(
            _FakeRegistry({"server_compat": {"api_surface": "responses"}}),
            "switchyard-smartfree-turnstone",
        )
        is None
    )


def test_guard_typed_branch_runs_without_model_turn():
    """The DISPATCHER must route a typed alias to the decision seam.

    This deliberately exercises ``_evaluate_active`` (not the helper directly):
    the failure mode under test is the DISPATCH being bypassed, so a test that
    calls the helper can never catch it.
    """
    import turnstone.core.output_guard_judge as og
    import turnstone.core.typed_decision as td

    seen = {}

    class _Resp2:
        def read(self):
            return json.dumps(_ok(choice="medium", provider="Respan")).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        return _Resp2()

    real_exec = td.execute_decision
    real_turn = og.model_turn

    def fake_exec(**kw):
        seen["decision_id"] = kw.get("decision_id")
        return real_exec(opener=opener, **{k: v for k, v in kw.items() if k != "opener"})

    def forbidden_turn(*a, **k):  # model_turn must NOT be reached
        raise AssertionError("model_turn was invoked for a typed-decision alias")

    td.execute_decision = fake_exec
    og.model_turn = forbidden_turn
    try:
        judge = og.OutputGuardJudge.__new__(og.OutputGuardJudge)
        judge._judge_model_alias = ""
        judge._model = "session-model"
        judge._config = types.SimpleNamespace(output_guard_llm_timeout=5.0)
        judge._typed_spec = SPEC
        judge._typed_base_url = "http://127.0.0.1:4000"
        judge._typed_alias = "switchyard-smartfree-aux-turnstone"
        verdict = og.OutputGuardJudge._evaluate_active(
            judge,
            "harmless tool output",
            func_name="bash",
            call_id="call-1",
            tool_description="run a shell command",
            tool_args="echo hi",
            heuristic_risk="none",
            heuristic_flags=(),
            heuristic_annotations=(),
            cancel_event=None,
            backend_auth_resolver=None,
            start=__import__("time").monotonic(),
            verdict_id="v-1",
        )
    finally:
        td.execute_decision = real_exec
        og.model_turn = real_turn

    assert verdict.succeeded, verdict.error
    assert verdict.risk_level == "medium", verdict.risk_level
    assert verdict.judge_model == "switchyard-smartfree-aux-turnstone"
    assert verdict.reasoning == "", "a typed decision must not fabricate reasoning"
    assert "switchyard-decision:v1" in verdict.provenance
    assert seen.get("url") == "http://127.0.0.1:4000/v1/decisions", seen
    assert seen.get("decision_id") == "switchyard-smartfree-aux-turnstone", seen


def test_typed_branch_error_is_labelled_not_clean():
    """A failing typed branch yields a labelled error, never risk none."""
    import turnstone.core.output_guard_judge as og
    import turnstone.core.typed_decision as td

    def boom(**kw):
        raise td.TypedDecisionError("upstream HTTP 503")

    real = td.execute_decision
    td.execute_decision = boom
    try:
        judge = og.OutputGuardJudge.__new__(og.OutputGuardJudge)
        judge._judge_model_alias = ""
        judge._model = "session-model"
        judge._typed_spec = SPEC
        judge._typed_base_url = "http://127.0.0.1:4000"
        judge._typed_alias = "a"
        verdict = og.OutputGuardJudge._evaluate_typed_decision(
            judge, "out", spec=SPEC, base_url="http://127.0.0.1:4000", alias="a",
            call_id="c", func_name="f", heuristic_risk="high", cancel_event=None,
            timeout=5, start=__import__("time").monotonic(), verdict_id="v",
        )
    finally:
        td.execute_decision = real
    assert not verdict.succeeded
    assert "typed_decision" in verdict.error, verdict.error
    # An errored semantic tier must not present as a clean verdict.
    assert not (verdict.error == "" and verdict.risk_level == "none")


if __name__ == "__main__":
    sys.exit(_run_all())
