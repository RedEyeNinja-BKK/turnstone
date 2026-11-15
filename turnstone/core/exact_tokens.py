"""Exact state-token admission for the shadow sensor.

The serializer's historical ``len(text)/3.6`` ratio is a calibrated heuristic, not
a measurement, so it cannot be the thing that enforces a token invariant. This
module replaces it with the *pinned Laya tokenizer* — the same
``tokenizer.json`` from the same checkpoint revision the HTPC service runs — and
enforces ``serialized_state_tokens <= STATE_TOKEN_BUDGET`` before dispatch.

Three properties this is built around:

* **Exact, and verified against ground truth.** ``tokenizers`` is used with
  ``add_special_tokens=False``, which is precisely what Laya's ``encode_text``
  does in ``laya/common.py::build_sequence``. Verified to reproduce all seven
  corpus cases byte-for-byte against counts taken from Laya's own tokenizer.
  A bare ``Tokenizer.encode()`` adds special tokens and reads a constant +2 high
  on every case — a silent, uniform overcount that a ratio-based check would
  never have caught.
* **Fails open, and says so.** If the tokenizer is missing or misbehaves, the
  caller receives ``None`` from :func:`count_state_tokens` and
  :func:`enforce_budget` returns a *refusal*, never a fabricated number. The
  sensor then fails open as an unavailable observation, per the shadow contract.
  It never silently falls back to the character ratio.
* **Local footprint is tokenizer-only.** 3.5 MB of tokenizer JSON in a
  dedicated interpreter. The 803.6 MB of model weights are never downloaded, and
  the production Turnstone virtualenv is not modified to make this work.

Counting is a *state* count. It is deliberately not the same quantity as Laya's
``usage.input_tokens``, which is aggregate encoded input summed across every
question and includes the question head and framing.
"""

from __future__ import annotations

import os
import pathlib
import threading
from dataclasses import dataclass
from typing import Any

from .bounded_state import SERIALIZER_VERSION, STATE_TOKEN_BUDGET

#: The checkpoint revision the HTPC Laya service is pinned to. The tokenizer
#: must come from exactly this revision or the counts are a different model.
PINNED_LAYA_REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"

#: Tokenizer-only cache. A sibling venv holds the ``tokenizers`` runtime so the
#: production virtualenv stays untouched.
TOKENIZER_DIR = pathlib.Path(
    os.environ.get(
        "TURNSTONE_SENSOR_TOKENIZER_DIR",
        pathlib.Path.home() / ".cache" / "turnstone-sensor-tokenizer",
    )
)
TOKENIZER_JSON = TOKENIZER_DIR / "tokenizer.json"
TOKENIZER_VENV_PYTHON = TOKENIZER_DIR / "venv" / "bin" / "python"

#: How long a token count may take before it is treated as a counter failure.
#: Measured encode cost is well under a millisecond; this is pure headroom.
COUNT_TIMEOUT_SECONDS = 5.0

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"attempted": False, "handle": None, "error": None,
                          "revision": None, "source": None}


class TokenCounterUnavailable(RuntimeError):
    """The exact tokenizer could not be loaded or invoked.

    Raised only by the in-process fast path. The subprocess path reports the
    same condition as ``None`` so no exception can reach a task.
    """


@dataclass(frozen=True)
class Admission:
    """The outcome of enforcing the state-token budget on one serialized state."""

    admitted: bool
    #: Exact token count, or ``None`` when the counter itself was unavailable.
    serialized_state_tokens: int | None
    budget: int
    #: Why it was refused. ``None`` when admitted.
    reason: str | None
    #: ``True`` when the count is exact; ``False`` when the counter was unavailable.
    exact: bool
    #: Which mechanism produced the count.
    method: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "serialized_state_tokens": self.serialized_state_tokens,
            "budget": self.budget,
            "reason": self.reason,
            "exact": self.exact,
            "method": self.method,
        }


def _load_handle():
    """Load the pinned tokenizer once, in-process, if possible."""
    if _STATE["attempted"]:
        return _STATE["handle"]
    with _LOCK:
        if _STATE["attempted"]:
            return _STATE["handle"]
        _STATE["attempted"] = True
        try:
            from tokenizers import Tokenizer  # type: ignore

            if not TOKENIZER_JSON.is_file():
                raise TokenCounterUnavailable(f"tokenizer not present at {TOKENIZER_JSON}")
            handle = Tokenizer.from_file(str(TOKENIZER_JSON))
            _STATE["handle"] = handle
            _STATE["source"] = "in_process"
            return handle
        except Exception as error:  # noqa: BLE001
            _STATE["error"] = f"{type(error).__name__}: {error}"
            return None


def _count_in_process(handle, text: str) -> int:
    # add_special_tokens=False is what Laya's build_sequence does. Omitting it
    # adds special tokens and overcounts by a constant.
    return len(handle.encode(text, add_special_tokens=False).ids)


def _count_subprocess(text: str) -> tuple[int | None, str | None]:
    """Count via the isolated tokenizer interpreter.

    Used when ``tokenizers`` is not importable in the calling process — which is
    the case for the production Turnstone virtualenv, by design, so that the
    shadow sensor does not add a dependency to the runtime that serves tasks.
    """
    import json
    import subprocess

    if not TOKENIZER_VENV_PYTHON.is_file():
        return None, f"tokenizer interpreter absent at {TOKENIZER_VENV_PYTHON}"
    program = (
        "import json,sys\n"
        "from tokenizers import Tokenizer\n"
        f"t=Tokenizer.from_file({str(TOKENIZER_JSON)!r})\n"
        "print(json.dumps(len(t.encode(sys.stdin.read(), add_special_tokens=False).ids)))\n"
    )
    try:
        completed = subprocess.run(
            [str(TOKENIZER_VENV_PYTHON), "-c", program],
            input=text,
            capture_output=True,
            text=True,
            timeout=COUNT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, "token_counter_timeout"
    except Exception as error:  # noqa: BLE001
        return None, f"token_counter:{type(error).__name__}"
    if completed.returncode != 0:
        return None, f"token_counter_exit_{completed.returncode}"
    try:
        return int(json.loads(completed.stdout.strip())), None
    except Exception:  # noqa: BLE001
        return None, "token_counter_unparseable"


def count_state_tokens(text: str) -> tuple[int | None, str | None, str]:
    """Exact state-token count for ``text``.

    Returns ``(count, error, method)``. ``count`` is ``None`` when the counter
    could not run — never an approximation. The character ratio is deliberately
    absent from this module: a heuristic that silently substitutes for a
    measurement is how a token invariant stops meaning anything.
    """
    handle = _load_handle()
    if handle is not None:
        try:
            return _count_in_process(handle, text), None, "in_process_tokenizers"
        except Exception as error:  # noqa: BLE001
            return None, f"token_counter:{type(error).__name__}", "in_process_tokenizers"
    return (*_count_subprocess(text), "isolated_subprocess_tokenizers")


def enforce_budget(
    text: str,
    *,
    budget: int = STATE_TOKEN_BUDGET,
) -> Admission:
    """Admit ``text`` only if its exact state-token count is within ``budget``.

    On counter failure this returns an **unadmitted** admission with
    ``exact=False``. It does not admit on a fallback estimate: an unknown token
    count cannot be shown to satisfy the invariant, so the request is not sent.
    The sensor then records an unavailable observation and the task proceeds.
    """
    count, error, method = count_state_tokens(text)
    if count is None:
        return Admission(
            admitted=False,
            serialized_state_tokens=None,
            budget=budget,
            reason=error or "token_counter_unavailable",
            exact=False,
            method=method,
        )
    if count > budget:
        return Admission(
            admitted=False,
            serialized_state_tokens=count,
            budget=budget,
            reason=f"state_token_budget_exceeded:{count}>{budget}",
            exact=True,
            method=method,
        )
    return Admission(
        admitted=True,
        serialized_state_tokens=count,
        budget=budget,
        reason=None,
        exact=True,
        method=method,
    )


def counter_status() -> dict[str, Any]:
    """Diagnostics for the sensor-health surface. No state, no secrets."""
    return {
        "serializer_version": SERIALIZER_VERSION,
        "pinned_laya_revision": PINNED_LAYA_REVISION,
        "tokenizer_present": TOKENIZER_JSON.is_file(),
        "tokenizer_bytes": TOKENIZER_JSON.stat().st_size if TOKENIZER_JSON.is_file() else 0,
        "interpreter_present": TOKENIZER_VENV_PYTHON.is_file(),
        "in_process_handle": _STATE["handle"] is not None,
        "load_error": _STATE["error"],
        "budget": STATE_TOKEN_BUDGET,
    }
