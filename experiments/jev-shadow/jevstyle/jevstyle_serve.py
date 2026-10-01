#!/usr/bin/env python3
"""jevstyle-serve: expose the pinned Jev-Style scorer over a typed decision endpoint.

Deliberately thin. It owns no decision logic: rendering, verdict readout and
calibration are done by the upstream reference implementation
(``jev_style_decision_gguf.JevStyleDecisionGGUF``), which is the qualified path.
This module only:

  * starts the scorer as a managed subprocess,
  * verifies the verdict slot ids come from the pinned readout_config.json and
    that the tokenizer agrees (the upstream Renderer already raises on mismatch),
  * translates a Laya-compatible /v1/systemone request into upstream question
    dicts, and
  * returns the scorer's own numbers without inventing any.

Contract-compatible with Laya's /v1/systemone so one client shape addresses
either backend:

    POST-equivalent line on stdin:
      {"state": {...}, "questions": {name: {"type","instructions","criteria"}}}
    -> {"ok": true, "result": {"model", "answers": {...}, "usage", "provenance"}}

Honesty rules, inherited from the sensor contract:
  * an unanswerable signal carries a reason, never 0.0
  * raw scorer evidence is preserved alongside the normalised answer
  * confidence is reported exactly as the scorer defines it, and labelled
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from typing import Any

SRC = pathlib.Path("/opt/jevstyle/src")
sys.path.insert(0, str(SRC))

MODEL_DIR = SRC
LLAMA_LIB = pathlib.Path("/opt/jevstyle/build/llama.cpp/build/bin")
SCORER = pathlib.Path("/opt/jevstyle/build/build/jev-score")
MODEL_PATH = pathlib.Path(
    "/opt/jevstyle/models/Jev-Style-0.8B-Decision-v3-Q8_0.gguf"
)
READOUT = SRC / "readout_config.json"

MODEL_REVISION = "edf37c26a1098f83cf4264b8adbe0dca2d2ebb0c"
LLAMA_COMMIT = "441df11f65ea0b6d0c72965aaf70c8241070ddcb"
QUANT = "Q8_0"

os.environ["LD_LIBRARY_PATH"] = (
    f"{LLAMA_LIB}:{os.environ.get('LD_LIBRARY_PATH', '')}"
)

_engine = None
_provenance: dict[str, Any] = {}


def log(msg: str) -> None:
    print(f"[jevstyle] {msg}", flush=True)


def verify_verdict_ids() -> dict[str, Any]:
    """Read the ids from the pinned config and prove the tokenizer agrees.

    Never hardcoded here. A mismatch raises rather than scoring wrong slots.
    """
    from tokenizers import Tokenizer

    cfg = json.loads(READOUT.read_text())
    st = cfg["slot_tokens"]
    ids = {
        "yes": int(st["yes"]["id"]),
        "no": int(st["no"]["id"]),
        "verdict_slot": int(st["verdict_slot"]["id"]),
    }
    tk = Tokenizer.from_file(str(SRC / "tokenizer" / "tokenizer.json"))
    for text, key in ((" yes", "yes"), (" no", "no"), (" ->", "verdict_slot")):
        got = tk.encode(text, add_special_tokens=False).ids
        if got != [ids[key]]:
            raise RuntimeError(
                f"tokenizer/readout mismatch: {text!r} -> {got}, "
                f"readout_config expects [{ids[key]}]"
            )
    return {
        "verdict_slot_ids": ids,
        "source": str(READOUT),
        "readout": cfg.get("readout"),
        "template": cfg.get("template"),
        "verified_against_tokenizer": True,
    }


def start() -> None:
    global _engine
    import jev_style_decision_gguf as ref

    verifier = verify_verdict_ids()
    log(f"verdict slot ids verified from pinned config: {verifier['verdict_slot_ids']}")

    _engine = ref.JevStyleDecisionGGUF(
        model_dir=MODEL_DIR,
        # The class otherwise looks for the GGUF inside model_dir; ours lives in
        # a separate read-only models/ dir, so the path is passed explicitly.
        gguf=MODEL_PATH,
        quant=QUANT,
        binary=SCORER,
        n_gpu_layers=int(os.environ.get("JEVSTYLE_NGL", "0")),  # CPU-first
        threads=int(os.environ.get("JEVSTYLE_THREADS", "6")),
        many_mode="exact",   # bit-identical to per-question decide()
        verify=False,        # hashes already verified on-host at install time
    )
    _provenance.update(verifier)
    _provenance.update({
        "model_repo": "chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF",
        "model_revision": MODEL_REVISION,
        "quant": QUANT,
        "llama_cpp_commit": LLAMA_COMMIT,
        "runtime": "jev_score (upstream scorer)",
        "scorer_info": _engine.info,
        "device": os.environ.get("JEVSTYLE_DEVICE", "cpu"),
    })
    log(f"engine ready: {_engine.info.get('desc')}")


def to_upstream_questions(questions: dict[str, Any]) -> list[dict[str, Any]]:
    """Laya-shaped question objects -> upstream {t, ins, crit} dicts.

    Only ``score`` with level criteria is accepted, because that is the shape the
    five shared signals use and the shape Laya answers. Anything else is refused
    rather than silently coerced.
    """
    out = []
    for name, q in questions.items():
        qtype = q.get("type")
        if qtype != "score":
            raise ValueError(
                f"question {name!r}: only type 'score' is supported, got {qtype!r}"
            )
        crit = q.get("criteria")
        if not isinstance(crit, list) or len(crit) < 2:
            raise ValueError(
                f"question {name!r}: 'criteria' must be a list of >=2 level descriptions"
            )
        out.append({
            "t": "score",
            "ins": q.get("instructions", name),
            "crit": [str(c) for c in crit],
        })
    return out


def handle(body: dict[str, Any]) -> dict[str, Any]:
    if body.get("op") == "health":
        return {"ok": True, "result": health()}

    state = body.get("state")
    questions = body.get("questions")
    if not isinstance(state, dict):
        raise ValueError("'state' is required and must be an object")
    if not isinstance(questions, dict) or not questions:
        raise ValueError("'questions' is required and must be a non-empty object")

    upstream = to_upstream_questions(questions)
    started = time.monotonic()
    results = _engine.decide_many(state, upstream)
    ms = int((time.monotonic() - started) * 1000)

    answers: dict[str, Any] = {}
    for (name, _q), res in zip(questions.items(), results):
        # Field names come from the upstream _result() contract, not guessed:
        # it returns answer / probabilities / scores / temperature /
        # top_probability / entropy_concentration / input_tokens / head_tokens.
        # There is NO "score" and NO "confidence" field, so none is invented.
        probs = res.get("probabilities") or {}
        answers[name] = {
            "type": "score",
            "answer": res.get("answer"),
            "probabilities": probs,
            "scores": res.get("scores"),
            "temperature": res.get("temperature"),
            "top_probability": res.get("top_probability"),
            "entropy_concentration": res.get("entropy_concentration"),
            "legend": {str(i): str(k) for i, k in enumerate(probs)},
            "input_tokens": res.get("input_tokens"),
            "head_tokens": res.get("head_tokens"),
            "backend": res.get("backend"),
            # the scorer's own record, kept verbatim for debugging
            "raw": res.get("raw", res),
        }
    return {
        "ok": True,
        "result": {
            "model": f"jev-style-0.8b-decision-v3-{QUANT.lower()}",
            "answers": answers,
            "usage": {"scorer_ms": ms},
            "provenance": _provenance,
        },
    }


def health() -> dict[str, Any]:
    return {
        "status": "ok" if _engine is not None else "starting",
        "model_revision": MODEL_REVISION,
        "llama_cpp_commit": LLAMA_COMMIT,
        "device": os.environ.get("JEVSTYLE_DEVICE", "cpu"),
        "provenance": _provenance,
    }


def main() -> None:
    start()
    log("service ready")
    # EOF on stdin must NOT end the service. The control channel is a FIFO: every
    # writer that closes its end delivers EOF, so exiting here made the service
    # die after each single request and systemd restart it. Reopen and keep serving.
    while True:
        if sys.stdin.closed:
            try:
                sys.stdin = open("/dev/stdin", "r")
            except OSError:
                time.sleep(0.2)
                continue
        line = sys.stdin.readline()
        if not line:
            time.sleep(0.05)
            continue
        line = line.strip()
        if not line:
            continue
        try:
            body = json.loads(line)
            print(json.dumps(handle(body)), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }), flush=True)


if __name__ == "__main__":
    main()
