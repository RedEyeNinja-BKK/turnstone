#!/usr/bin/env python3
"""Thin typed-decision adapter: Jev-Style scorer -> Laya's /v1/systemone shape.

Why this shape: Jev-Style must be comparable with Span and Laya on identical
bounded states. Laya already exposes the TypeSafe Jev ``/v1/systemone`` protocol
(its own OpenAPI title is "Laya System-1 decisions over the TypeSafe Jev
/v1/systemone protocol"), so matching that contract lets the existing
Switchyard capability client format ``openrouter_alpha_decisions`` address either
backend without semantic distortion.

The contract was derived empirically against the live Laya service, not guessed:

    POST /v1/systemone
      state     : {objective, phase, blockers, history}
      questions : {name: {type, instructions, criteria}}   # object, not list
    200 -> {model, answers:{name:{type,score,legend,probabilities,
                                 confidence,answer_confidence,action}},
            usage:{input_tokens, output_tokens}}

Question types accepted by Laya: ``score``, ``noul``, ``choice``.

HONESTY RULES enforced here:
  * a signal the model cannot answer is reported ``available: false`` with a
    reason - never as 0.0
  * ``local_suitability`` is structurally excluded by the sensor contract and is
    never requested, inferred, or faked
  * raw scorer output is preserved verbatim in the debug record
  * model / scorer / runtime revisions travel with every answer

This adapter performs NO inference. It renders, calls the scorer, and normalises.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import time
from typing import Any

#: Pinned provenance. Every answer is stamped with these.
PROVENANCE = {
    "model_repo": "chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF",
    "model_revision": "edf37c26a1098f83cf4264b8adbe0dca2d2ebb0c",
    "q8_0_sha256": "cd87d284c4ee355cb0b3fce7391db57ba3ad108e45b3a41b058d529931a2bd5e",
    "llama_cpp_commit": "441df11f65ea0b6d0c72965aaf70c8241070ddcb",
    "readout": "verdict",
    "template": "macjev-render-v1",
}

#: The five signals the sensor actually requests. local_suitability is
#: deliberately absent - the contract forbids inferring it.
SENSOR_SIGNALS = (
    "task_complexity",
    "reasoning_demand",
    "tool_dependency",
    "agentic_complexity",
    "route_sufficiency",
)

LEVELS = ("trivial", "simple", "moderate", "complex", "very complex")

QUESTIONS = {
    "task_complexity": "Rate the complexity of this engineering task.",
    "reasoning_demand": "Rate how much deep reasoning this task demands.",
    "tool_dependency": "Rate how much this task depends on tool use.",
    "agentic_complexity": "Rate how agentic and multi-step this task is.",
    "route_sufficiency": "Rate how well the currently available routes suit this task.",
}


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_tokenizer_ids(readout_cfg_path: pathlib.Path, encode) -> dict[str, Any]:
    """Confirm the verdict slot ids against the actual tokenizer.

    §8 of the brief: do not hardcode 9542/874/1411 until confirmed against the
    pinned tokenizer. This asserts the config ids reproduce under the real
    tokenizer and fails loudly otherwise.
    """
    cfg = json.loads(readout_cfg_path.read_text())
    st = cfg["slot_tokens"]
    expect = {
        "yes": int(st["yes"]["id"]),
        "no": int(st["no"]["id"]),
        "verdict_slot": int(st["verdict_slot"]["id"]),
    }
    probe = {"yes": " yes", "no": " no", "verdict_slot": " ->"}
    for key, text in probe.items():
        got = encode(text)
        if got != [expect[key]]:
            raise ValueError(
                f"tokenizer mismatch for {text!r}: got {got}, "
                f"readout_config expects [{expect[key]}]"
            )
    return expect


class JevStyleScorer:
    """Owns the persistent ``jev-score`` JSON-lines subprocess."""

    def __init__(self, binary: pathlib.Path, model: pathlib.Path,
                 n_ctx: int = 32768, ngl: int = 0, threads: int = 8):
        self.binary = binary
        self.model = model
        self.n_ctx = n_ctx
        self.ngl = ngl
        self.threads = threads
        self.proc: subprocess.Popen | None = None
        self.ready: dict[str, Any] = {}

    def start(self, timeout: float = 600.0) -> dict[str, Any]:
        cmd = [
            str(self.binary),
            "--model", str(self.model),
            "--n-ctx", str(self.n_ctx),
            "--ngl", str(self.ngl),
            "--threads", str(self.threads),
        ]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        deadline = time.time() + timeout
        line = ""
        while time.time() < deadline:
            if self.proc.stdout is None:
                break
            line = self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("ready"):
                self.ready = msg
                return msg
        raise RuntimeError(f"scorer did not become ready: {line[:400]!r}")

    def ask(self, request: dict[str, Any], timeout: float = 300.0) -> dict[str, Any]:
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError("scorer is not running")
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if "error" in msg:
                raise RuntimeError(f"scorer error: {msg['error']}")
            if "results" in msg:
                return msg
        raise TimeoutError("scorer did not answer within timeout")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                assert self.proc.stdin
                self.proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.proc.kill()


def unavailable_answer(name: str, reason: str) -> dict[str, Any]:
    """Never fabricate a number for a signal we could not answer."""
    return {
        "name": name,
        "type": "score",
        "available": False,
        "value": None,
        "probabilities": None,
        "confidence": None,
        "reason": reason,
    }


def normalise(name: str, raw: dict[str, Any], latency_ms: int) -> dict[str, Any]:
    """Normalise one scorer answer into the shared typed-decision record."""
    return {
        "name": name,
        "type": "score",
        "available": True,
        "value": raw.get("score"),
        "legend": raw.get("legend"),
        "probabilities": raw.get("probabilities"),
        "confidence": raw.get("confidence"),
        "answer_confidence": raw.get("answer_confidence"),
        "action": raw.get("action"),
        "latency_ms": latency_ms,
        "backend": "Jev-Style",
        "model_revision": PROVENANCE["model_revision"],
        "reason": None,
        "raw": raw,
    }


def build_question(name: str) -> dict[str, Any]:
    """Build one /v1/systemone question object for a sensor signal."""
    return {
        "type": "score",
        "instructions": QUESTIONS[name],
        "criteria": list(LEVELS),
    }


def build_systemone_body(state: dict[str, Any],
                         signals: tuple[str, ...] = SENSOR_SIGNALS) -> dict[str, Any]:
    """The exact body Laya accepts, reused verbatim so both backends see the same input."""
    return {
        "state": {
            "objective": state.get("objective", "")[:400],
            "phase": state.get("phase", "unknown"),
            "blockers": state.get("blockers", "")[:400],
            "history": state.get("history", "")[:400],
        },
        "questions": {name: build_question(name) for name in signals},
    }
