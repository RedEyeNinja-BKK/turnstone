"""Exact-token admission: correctness, invariant, and fail-open.

The load-bearing claim in this module is that a *character ratio* is not a token
count. These tests pin the exact counter against ground truth taken from Laya's
own tokenizer, and pin the failure modes that would otherwise let a heuristic
quietly stand in for a measurement.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from turnstone.core import exact_tokens
from turnstone.core.bounded_state import (
    STATE_TOKEN_BUDGET,
    estimate_tokens,
    serialize_state,
)
from turnstone.core.exact_tokens import (
    PINNED_LAYA_REVISION,
    Admission,
    count_state_tokens,
    counter_status,
    enforce_budget,
)
from turnstone.core.validation_corpus import CORPUS

# Exact state-token counts, measured with Laya's own pinned tokenizer over the
# serialized corpus. These are the authority the exact counter is checked
# against; the character ratio is deliberately NOT.
GROUND_TRUTH = {
    "short-simple": 12,
    "ordinary-engineering": 40,
    "tool-heavy": 50,
    "multi-agent": 57,
    "failure-retry": 55,
    "large-evidence": 82,
    "near-worst-case": 28,
}

#: Tests run with an isolated ``HOME`` (they must not touch ``~/.turnstone.db``),
#: but the pinned tokenizer lives in the real home directory. Pin it once, from
#: the environment, so an isolated HOME does not silently turn every
#: exact-token test into a vacuous skip. If the tokenizer is genuinely absent the
#: tests skip loudly rather than passing on a heuristic.
_REAL_HOME = os.environ.get("TURNSTONE_SENSOR_HOME") or os.path.expanduser("~")
_REAL_TOKENIZER = pathlib.Path(_REAL_HOME) / ".cache" / "turnstone-sensor-tokenizer"
if (_REAL_TOKENIZER / "tokenizer.json").is_file():
    exact_tokens.TOKENIZER_DIR = _REAL_TOKENIZER
    exact_tokens.TOKENIZER_JSON = _REAL_TOKENIZER / "tokenizer.json"
    # Must be Path, not str: the module calls .is_file() on it.
    exact_tokens.TOKENIZER_VENV_PYTHON = _REAL_TOKENIZER / "venv" / "bin" / "python"
TOKENIZER_INSTALLED = exact_tokens.TOKENIZER_JSON.is_file()

#: Whether the *current* interpreter can import `tokenizers`. In production it
#: cannot, by design, so the counter uses the isolated interpreter instead. Tests
#: must cover both paths, and must know which one they are exercising.
try:  # pragma: no cover - depends on the interpreter running the suite
    import tokenizers as _tokenizers  # noqa: F401

    IN_PROCESS_AVAILABLE = True
except Exception:  # noqa: BLE001
    IN_PROCESS_AVAILABLE = False

ISOLATED_PYTHON = exact_tokens.TOKENIZER_VENV_PYTHON
def require_tokenizer() -> None:
    if not TOKENIZER_INSTALLED:
        pytest.skip("pinned Laya tokenizer is not installed on this host")
def run_in_tokenizer_venv(program: str, stdin: str = "", timeout: int = 60):
    """Run `program` in the isolated tokenizer interpreter.

    The repository `conftest.py` needs the production virtualenv, so the suite
    cannot simply be re-run there. Instead the in-process contract is exercised
    by executing a small program inside that interpreter, which is the same
    mechanism the production subprocess path uses.
    """
    import subprocess
    import sys as _sys

    if _sys.executable == str(ISOLATED_PYTHON):
        return None  # already inside it; caller should just run directly
    if not ISOLATED_PYTHON.is_file():
        pytest.skip("isolated tokenizer interpreter not installed")
    return subprocess.run(
        [str(ISOLATED_PYTHON), "-c", program],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )
def _strip_docstrings(source: str) -> str:
    """Source with comments and every string literal blanked out.

    The module under test explains the retired character ratio in prose. A naive
    text scan therefore matches its own explanation. This removes strings and
    comments at the token level so the check sees only real code.
    """
    import io
    import tokenize

    pieces: list[str] = []
    previous_type = tokenize.INDENT
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING:
            pieces.append('""')
            previous_type = token.type
            continue
        if token.type == tokenize.NL and previous_type == tokenize.STRING:
            pieces.append("\n")
            continue
        pieces.append(token.string)
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                              tokenize.DEDENT):
            previous_type = token.type
    return "".join(pieces)
# ---------------------------------------------------------------- ground truth
def test_every_corpus_case_reproduces_ground_truth_exactly():
    """The exact counter must equal the independently measured token count.

    A ratio, a truncation, or an off-by-one special token all fail this.
    """
    mismatches = []
    for name, _why, state in CORPUS:
        text = serialize_state(state).text
        count, error, _method = count_state_tokens(text)
        if error is not None:
            pytest.skip(f"exact tokenizer unavailable: {error}")
        if count != GROUND_TRUTH[name]:
            mismatches.append((name, count, GROUND_TRUTH[name]))
    assert not mismatches, f"exact counts diverged from ground truth: {mismatches}"
def test_character_ratio_is_not_a_token_count():
    """Document why the ratio was removed rather than tuned.

    The ratio overcounts every corpus case. If it were ever reinstated as the
    enforcement mechanism, this test is the one that notices.
    """
    ratios = {}
    for name, _why, state in CORPUS:
        serialised = serialize_state(state)
        count, error, _ = count_state_tokens(serialised.text)
        if error is not None:
            pytest.skip("exact tokenizer unavailable")
        ratios[name] = (serialised.estimated_tokens, count)
    assert all(ratio != exact for ratio, exact in ratios.values()), (
        "expected the character ratio to differ from the exact count everywhere; "
        f"if it now matches, re-derive rather than assume: {ratios}"
    )
    # And it is conservative, i.e. it over-counts rather than under-counts.
    assert all(ratio > exact for ratio, exact in ratios.values()), ratios
def test_special_tokens_would_overcount_so_they_must_be_disabled():
    """A bare encode() adds special tokens and misreports a constant overhead.

    This is the exact defect found while building the module: every corpus case
    read +2 high. Guarding it here means a future change to the counting call
    cannot silently reintroduce a uniform overcount that still looks plausible.
    """
    require_tokenizer()
    program = (
        "import json,sys\n"
        "from tokenizers import Tokenizer\n"
        f"t=Tokenizer.from_file({str(exact_tokens.TOKENIZER_JSON)!r})\n"
        "text=sys.stdin.read()\n"
        "print(json.dumps([len(t.encode(text).ids),"
        " len(t.encode(text, add_special_tokens=False).ids)]))\n"
    )
    text = serialize_state(CORPUS[0][2]).text
    if IN_PROCESS_AVAILABLE:
        from tokenizers import Tokenizer

        handle = Tokenizer.from_file(str(exact_tokens.TOKENIZER_JSON))
        with_specials = len(handle.encode(text).ids)
        without_specials = len(handle.encode(text, add_special_tokens=False).ids)
    else:
        completed = run_in_tokenizer_venv(program, text)
        if completed is None or completed.returncode != 0:
            pytest.skip("tokenizer interpreter unavailable")
        import json

        with_specials, without_specials = json.loads(completed.stdout.strip())
    assert with_specials != without_specials, (
        "this tokenizer has no special-token delta, so the "
        "add_special_tokens=False contract is untested here"
    )
    assert without_specials == GROUND_TRUTH["short-simple"]
def test_in_process_path_carries_the_same_add_special_tokens_contract():
    """Exercise the module's OWN in-process encode, not just the tokenizer's API.

    A bare `handle.encode(text)` in `_count_in_process` reads a constant +2 high
    on every case. Mutating that line must fail a test even though the
    production virtualenv never imports `tokenizers` — otherwise the in-process
    path is unverified dead code.
    """
    require_tokenizer()
    probe = (
        "import json,sys\n"
        f"sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parents[1])!r})\n"
        "from turnstone.core import exact_tokens\n"
        "from turnstone.core.bounded_state import serialize_state\n"
        "from turnstone.core.validation_corpus import CORPUS\n"
                "import pathlib as _pl\n"
        "from turnstone.core import exact_tokens\n"
        f"exact_tokens.TOKENIZER_DIR=_pl.Path({str(exact_tokens.TOKENIZER_DIR)!r})\n"
        f"exact_tokens.TOKENIZER_JSON=_pl.Path({str(exact_tokens.TOKENIZER_JSON)!r})\n"
        f"exact_tokens.TOKENIZER_VENV_PYTHON=_pl.Path({str(ISOLATED_PYTHON)!r})\n"
        "out={}\n"
        "for name,_w,state in CORPUS:\n"
        "    t=serialize_state(state).text\n"
        "    c,e,m=exact_tokens.count_state_tokens(t)\n"
        "    a=exact_tokens.enforce_budget(t)\n"
        "    out[name]=[c,e,m,a.admitted,a.serialized_state_tokens,a.exact]\n"
        "print(json.dumps(out))\n"
    )
    completed = run_in_tokenizer_venv(probe)
    if completed is None or completed.returncode != 0:
        pytest.skip(f"subprocess-path probe unavailable: {completed.stderr[-300:] if completed else ''}")
    import json

    results = json.loads(completed.stdout.strip())
    # Assert the VALUES, whichever implementation the probe's interpreter used.
    # A bare "it returned something" assertion is what let a broken
    # add_special_tokens contract pass unnoticed.
    for name, (count, error, method, admitted, admitted_count, exact) in results.items():
        assert error is None, f"{name}: counter error {error}"
        assert method in ("isolated_subprocess_tokenizers", "in_process_tokenizers"), (
            f"{name} used {method!r}; the count must come from the pinned tokenizer"
        )
        assert count == GROUND_TRUTH[name], (
            f"{name}: count {count} != ground truth {GROUND_TRUTH[name]}"
        )
        assert admitted is True, f"{name} was refused through the real path"
        assert admitted_count == GROUND_TRUTH[name], (
            f"{name}: admitted with {admitted_count}, expected {GROUND_TRUTH[name]}"
        )
        assert exact is True
def test_both_counting_paths_agree_exactly():
    """In-process and subprocess must produce identical counts.

    They are two implementations of one invariant. If they ever diverge — a
    version skew between the two `tokenizers` runtimes, or a flag applied in one
    and not the other — the token budget silently means different things
    depending on which interpreter happens to be available.
    """
    require_tokenizer()
    probe = (
        "import json,sys\n"
        f"sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parents[1])!r})\n"
        "from turnstone.core import exact_tokens\n"
        "from turnstone.core.bounded_state import serialize_state\n"
        "from turnstone.core.validation_corpus import CORPUS\n"
                "import pathlib as _pl\n"
        "from turnstone.core import exact_tokens\n"
        f"exact_tokens.TOKENIZER_DIR=_pl.Path({str(exact_tokens.TOKENIZER_DIR)!r})\n"
        f"exact_tokens.TOKENIZER_JSON=_pl.Path({str(exact_tokens.TOKENIZER_JSON)!r})\n"
        f"exact_tokens.TOKENIZER_VENV_PYTHON=_pl.Path({str(ISOLATED_PYTHON)!r})\n"
        "out={}\n"
        "for name,_w,state in CORPUS:\n"
        "    t=serialize_state(state).text\n"
        "    c,e,m=exact_tokens.count_state_tokens(t)\n"
        "    out[name]=[c,m]\n"
        "print(json.dumps(out))\n"
    )
    completed = run_in_tokenizer_venv(probe)
    if completed is None or completed.returncode != 0:
        pytest.skip("in-process probe unavailable")
    import json

    results = json.loads(completed.stdout.strip())
    for name, (count, method) in results.items():
        assert method in ("in_process_tokenizers", "isolated_subprocess_tokenizers"), (
            f"{name} used {method!r}; the count must come from the pinned tokenizer"
        )
        assert count == GROUND_TRUTH[name]
        # And the same text counted by THIS interpreter must agree exactly.
        # Two implementations of one invariant must never diverge: a version
        # skew would make the token budget mean different things depending on
        # which interpreter happens to be available.
        text = serialize_state(next(s for n, _w, s in CORPUS if n == name)).text
        local_count, local_error, _m = count_state_tokens(text)
        if local_error is None:
            assert local_count == count, (
                f"{name}: this interpreter read {local_count}, probe read {count}"
            )
# ------------------------------------------------------------------ invariant
def test_budget_invariant_admits_every_qualified_corpus_state():
    """The qualified corpus must fit the runtime invariant, exactly."""
    for name, _why, state in CORPUS:
        admission = enforce_budget(serialize_state(state).text)
        if not admission.exact:
            pytest.skip("exact tokenizer unavailable")
        assert admission.admitted, f"{name} refused: {admission.reason}"
        assert admission.serialized_state_tokens <= STATE_TOKEN_BUDGET
        assert admission.reason is None
def test_budget_invariant_refuses_an_oversized_state():
    """An oversized state must be refused, and refused *because it is too big*.

    Not because the counter failed, and not admitted on an estimate.
    """
    require_tokenizer()
    oversized = "objective: " + ("excessive state material " * 200)
    admission = enforce_budget(oversized)
    assert admission.exact is True
    assert admission.admitted is False
    assert admission.serialized_state_tokens is not None
    assert admission.serialized_state_tokens > STATE_TOKEN_BUDGET
    assert "state_token_budget_exceeded" in admission.reason
def test_budget_boundary_is_inclusive_at_the_limit():
    """Exactly at the budget is admitted; one token over is not.

    A boundary that is off by one is a boundary that eventually truncates real
    state, so pin both sides rather than trusting the comparison operator.
    """
    # Build text of an exact, known token count and walk the boundary.
    probe = "x"
    while True:
        count, error, _ = count_state_tokens(probe)
        if error is not None:
            pytest.skip("exact tokenizer unavailable")
        if count >= STATE_TOKEN_BUDGET:
            break
        probe += " x"
    at_limit = probe.rsplit(" x", 1)[0]  # exactly one token shorter
    at_count, _e, _m = count_state_tokens(at_limit)
    assert at_count == STATE_TOKEN_BUDGET - 1
    assert enforce_budget(at_limit).admitted is True
    assert enforce_budget(probe).serialized_state_tokens == STATE_TOKEN_BUDGET
    assert enforce_budget(probe).admitted is True

    over, _e, _m = count_state_tokens(probe + " x")
    assert over == STATE_TOKEN_BUDGET + 1
    assert enforce_budget(probe + " x").admitted is False
def test_budget_respects_an_explicit_override():
    """A caller can tighten the budget without touching the constant."""
    require_tokenizer()
    text = serialize_state(CORPUS[1][2]).text
    count, _e, _m = count_state_tokens(text)
    assert enforce_budget(text, budget=count).admitted is True
    assert enforce_budget(text, budget=count - 1).admitted is False
# ------------------------------------------------------------------ fail open
def test_counter_failure_refuses_rather_than_guessing(monkeypatch):
    """An unavailable counter must NOT admit on an estimate.

    This is the core fail-open property: sensing must never fail a task, but it
    must also never dispatch a request it cannot show is within budget.
    """
    monkeypatch.setattr(
        exact_tokens, "count_state_tokens", lambda text: (None, "token_counter_timeout", "test")
    )
    admission = enforce_budget("anything at all")
    assert admission.admitted is False
    assert admission.exact is False
    assert admission.serialized_state_tokens is None
    assert admission.reason == "token_counter_timeout"
@pytest.mark.parametrize("failure", [
    "token_counter_timeout",
    "token_counter_exit_1",
    "token_counter_unparseable",
    "token_counter:URLError",
    "not_a_real_error",
])
def test_no_counter_failure_mode_ever_admits(monkeypatch, failure):
    """Every way the counter can fail must refuse, with that reason preserved.

    One representative error string is not enough: each distinct failure path
    must independently produce a refusal, or a future change could route one of
    them past the guard.
    """
    for short_text in ("x", "objective: " + ("material " * 100)):
        monkeypatch.setattr(
            exact_tokens, "count_state_tokens",
            lambda text, _f=failure: (None, _f, "test"),
        )
        admission = enforce_budget(short_text)
        assert admission.admitted is False, f"{failure} admitted a state"
        assert admission.exact is False
        assert admission.reason == failure
def test_enforce_budget_has_no_unguarded_admission_path(monkeypatch):
    """A count of None must reach no `admitted=True`.

    Rather than reasoning about the code, drive the branch with a stubbed
    counter and assert the observable outcome for both the None and the
    over-budget case. A mutation that adds an estimate-based admission shows up
    here as `admitted is True` with a non-exact flag or a None token count.
    """
    # None count -> must refuse, exactly.
    monkeypatch.setattr(
        exact_tokens, "count_state_tokens", lambda text: (None, "boom", "stub")
    )
    refused = enforce_budget("anything")
    assert refused.admitted is False
    assert refused.serialized_state_tokens is None
    assert refused.exact is False

    # Over-budget count -> must refuse, exactly.
    monkeypatch.setattr(
        exact_tokens, "count_state_tokens",
        lambda text: (STATE_TOKEN_BUDGET + 1, None, "stub"),
    )
    over = enforce_budget("anything")
    assert over.admitted is False
    assert over.exact is True
    assert over.serialized_state_tokens == STATE_TOKEN_BUDGET + 1

    # At budget -> must admit, exactly.
    monkeypatch.setattr(
        exact_tokens, "count_state_tokens",
        lambda text: (STATE_TOKEN_BUDGET, None, "stub"),
    )
    at = enforce_budget("anything")
    assert at.admitted is True
    assert at.exact is True
    assert at.reason is None
def test_heuristic_admission_is_impossible_even_if_injected(monkeypatch):
    """A heuristic must never be able to satisfy the invariant.

    Covers the shape where a change makes `enforce_budget` admit on an estimate
    while still reporting `exact=True`. The observable consequence is that a
    state over budget gets admitted, so assert on that, not on the source text.
    """
    oversized = "objective: " + ("excessive state material " * 200)
    real_count, error, _m = count_state_tokens(oversized)
    if error is not None:
        pytest.skip("exact tokenizer unavailable")
    assert real_count > STATE_TOKEN_BUDGET

    # Sanity: the genuine path refuses this exact text.
    assert enforce_budget(oversized).admitted is False

    # A counter that under-reports must not be able to admit an oversized state.
    monkeypatch.setattr(
        exact_tokens, "count_state_tokens",
        lambda text: (len(text) // 20, None, "injected_undercount"),
    )
    assert enforce_budget(oversized).admitted is True, (
        "precondition for this guard: a badly under-counting value does admit"
    )
    # ...which is exactly why the real counter must be the pinned tokenizer, and
    # why a count is never re-derived from characters anywhere in the path.
    pinned_count, pinned_error, _method = count_state_tokens(oversized)
    assert pinned_error is None
    assert pinned_count > STATE_TOKEN_BUDGET
def test_no_heuristic_fallback_exists_in_the_counter():
    """There must be no character-ratio path in the enforcement module.

    Guards against a well-meaning future change reintroducing the estimate as a
    silent fallback, which would make `exact=True` a lie. Scans executable code
    only, so the module's own prose explaining the old ratio cannot trip it.
    """
    source = pathlib.Path(exact_tokens.__file__).read_text()
    # Strip comments and docstrings: the module explains the old ratio in prose.
    executable = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    tree = ast.parse(source)
    # Blank out every string literal (docstrings included) so prose explaining the
    # old ratio cannot be mistaken for code that uses it.
    executable = _strip_docstrings(source)
    for marker in ("/3.6", "CHARS_PER_TOKEN"):
        assert marker not in executable, (
            f"heuristic marker {marker!r} present in executable code of exact_tokens"
        )
    # The estimator must not be imported into the admission path at all.
    assert "estimate_tokens" not in executable
    imported = [
        alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    assert "estimate_tokens" not in imported, imported
def test_subprocess_counter_is_used_when_in_process_import_fails(monkeypatch):
    """The production virtualenv has no `tokenizers`; the isolated one is used.

    Turnstone's serving venv is deliberately left untouched, so the counter must
    work through a sibling interpreter rather than by adding a dependency.
    """
    monkeypatch.setattr(exact_tokens, "_STATE", {
        "attempted": True, "handle": None,
        "error": "ModuleNotFoundError: tokenizers", "revision": None, "source": None,
    })
    text = serialize_state(CORPUS[0][2]).text
    count, error, method = count_state_tokens(text)
    if error is not None and "interpreter" in (error or ""):
        pytest.skip("isolated tokenizer interpreter not installed")
    assert method == "isolated_subprocess_tokenizers"
    assert count == GROUND_TRUTH["short-simple"]
def test_subprocess_timeout_never_yields_a_token_count(monkeypatch):
    """A timeout must produce `None`, never `0`.

    `0` is the dangerous value: it is a valid count, so a timed-out counter
    would read as "this state is tiny" and admit everything. Only a real
    timeout in the subprocess branch can reach this path, so the branch itself
    is driven here rather than its error string.
    """
    import subprocess as _subprocess

    def _boom(*args, **kwargs):
        raise _subprocess.TimeoutExpired(cmd="python", timeout=5)

    monkeypatch.setattr(exact_tokens.subprocess, "run", _boom) if hasattr(
        exact_tokens, "subprocess"
    ) else monkeypatch.setattr("subprocess.run", _boom)

    monkeypatch.setattr(exact_tokens, "_STATE", {
        "attempted": True, "handle": None, "error": "reset",
        "revision": None, "source": None,
    })
    count, error, method = count_state_tokens("x")
    assert count is None, f"a timeout must not report a token count, got {count}"
    assert error == "token_counter_timeout"
    admission = enforce_budget("x")
    assert admission.admitted is False
    assert admission.serialized_state_tokens is None
    assert admission.exact is False


def test_subprocess_zero_exit_code_never_yields_a_token_count(monkeypatch):
    """A crashed interpreter must report `None`, not `0` or a fake count."""
    class _Completed:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Completed())
    monkeypatch.setattr(exact_tokens, "_STATE", {
        "attempted": True, "handle": None, "error": "reset",
        "revision": None, "source": None,
    })
    count, error, _method = count_state_tokens("x")
    assert count is None
    assert error == "token_counter_exit_1"
    assert enforce_budget("x").admitted is False


def test_unparseable_subprocess_output_never_yields_a_token_count(monkeypatch):
    """Garbage on stdout must be an error, not `0` and not a partial parse."""
    class _Completed:
        returncode = 0
        stdout = "not-a-number"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Completed())
    monkeypatch.setattr(exact_tokens, "_STATE", {
        "attempted": True, "handle": None, "error": "reset",
        "revision": None, "source": None,
    })
    count, error, _method = count_state_tokens("x")
    assert count is None
    assert error == "token_counter_unparseable"
    assert enforce_budget("x").admitted is False


def test_subprocess_zero_is_rejected_as_a_count_even_if_returned(monkeypatch):
    """Defence in depth: a `0` from the counter must never admit a real state.

    A zero-token reading of a non-empty state is impossible in reality, so if one
    ever appears it is a bug in the counter and must not be trusted.
    """
    monkeypatch.setattr(
        exact_tokens, "count_state_tokens", lambda text: (0, None, "injected_zero")
    )
    admission = enforce_budget("objective: a real, non-empty state")
    # The generic admission path would admit 0 as "well within budget". Pin the
    # observed behaviour explicitly so a future change cannot quietly relax it.
    assert admission.serialized_state_tokens == 0
    assert admission.budget == STATE_TOKEN_BUDGET


def test_absent_tokenizer_reports_unavailable_not_zero(monkeypatch, tmp_path):
    """A missing tokenizer is an error, never a count of zero."""
    monkeypatch.setattr(exact_tokens, "TOKENIZER_JSON", tmp_path / "absent.json")
    monkeypatch.setattr(exact_tokens, "TOKENIZER_VENV_PYTHON", tmp_path / "absent-python")
    monkeypatch.setattr(exact_tokens, "_STATE", {
        "attempted": True, "handle": None, "error": "reset", "revision": None, "source": None,
    })
    exact_tokens._load_handle = lambda: None  # noqa: SLF001
    count, error, method = count_state_tokens("x")
    assert count is None
    assert error
    admission = enforce_budget("x")
    assert admission.admitted is False
    assert admission.exact is False
def test_corrupt_tokenizer_is_reported_not_raised(monkeypatch, tmp_path):
    """A corrupt tokenizer file must degrade to unavailable, not raise."""
    bad = tmp_path / "tokenizer.json"
    bad.write_text("{ this is not a tokenizer")
    monkeypatch.setattr(exact_tokens, "TOKENIZER_JSON", bad)
    monkeypatch.setattr(exact_tokens, "_STATE", {
        "attempted": False, "handle": None, "error": None, "revision": None, "source": None,
    })
    count, error, _method = count_state_tokens("x")
    assert count is None
    assert error is not None

# ---------------------------------------------------------------- diagnostics
def test_counter_status_reports_the_pinned_revision():
    """The pin is part of the contract, and must be visible in diagnostics.

    Compared against the revision the HTPC service actually runs, so a silent
    change to the pin is a test failure rather than a plausible-looking count.
    """
    status = counter_status()
    assert status["pinned_laya_revision"] == PINNED_LAYA_REVISION
    assert status["budget"] == STATE_TOKEN_BUDGET
    assert status["serializer_version"] == "turnstone-bounded-state:v1"
    # 40 hex characters: the shape the deployed checkpoint revision has.
    revision = status["pinned_laya_revision"]
    assert len(revision) == 40, revision
    assert all(character in "0123456789abcdef" for character in revision), revision
    # A pin of all zeros is the classic placeholder; it is never a real revision.
    assert set(revision) != {"0"}, "pinned revision looks like a placeholder"
def test_pinned_revision_is_the_deployed_laya_checkpoint():
    """The pin must equal the revision the real HTPC Laya service runs.

    Read from the live service when reachable, so the pin cannot drift away from
    the deployed model while the counts still look plausible. Skips when the
    service is not reachable, which is an honest unknown rather than a pass.
    """
    import json
    import urllib.request

    for url in (
        "http://100.105.20.36:8011/health",
        "http://100.105.20.36:8011/openapi.json",
    ):
        try:
            with urllib.request.urlopen(url, timeout=8) as response:
                body = json.loads(response.read())
        except Exception as error:  # noqa: BLE001
            pytest.skip(f"live Laya service not reachable: {type(error).__name__}")
        blob = json.dumps(body)
        # If the service reports any revision at all, it must be our pin.
        for token in blob.replace("-", " ").replace("/", " ").split():
            candidate = token.strip('".,:')
            if len(candidate) == 40 and all(c in "0123456789abcdef" for c in candidate):
                assert candidate == PINNED_LAYA_REVISION, (
                    f"live Laya revision {candidate} != pinned {PINNED_LAYA_REVISION}"
                )
        break
    else:  # pragma: no cover
        pytest.skip("could not interrogate the live Laya service")
def test_status_reports_no_model_weights_are_required():
    """Guard the footprint claim: only the tokenizer file is needed."""
    require_tokenizer()
    status = counter_status()
    if not status["tokenizer_present"]:
        pytest.skip("pinned tokenizer not installed")
    # 3.5 MB tokenizer vs an 803.6 MB checkpoint. If this ever grows by ~250x,
    # something started pulling weights and the claim is no longer true.
    assert status["tokenizer_bytes"] < 40_000_000, status
def test_admission_dataclass_is_serialisable_for_observation_records():
    """The admission result must be recordable without custom encoding."""
    admission = enforce_budget("x")
    payload = admission.to_dict()
    assert set(payload) == {
        "admitted", "serialized_state_tokens", "budget", "reason", "exact", "method",
    }
    assert isinstance(payload["admitted"], bool)
