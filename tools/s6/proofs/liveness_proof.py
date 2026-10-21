#!/usr/bin/env python3
"""Proof that the liveness check detects a silent sensor.

The point of `liveness.py` is that an epoch with zero observations must not read
as healthy. These cases drive the pure `assess()` function with fixtures; no
telemetry, database or service is touched.

Two dimensions are exercised independently:

  * EVENT COVERAGE - was eligible work observed? The denominator claim for
    attachment turns depends on whether the epoch's code can see them, so the
    same fixture is asserted to FAIL for an attachment-aware epoch and to be a
    caveat for a text-only one.
  * GRADING QUALITY - was the observation grounded in the instruction? A
    descriptor-only grading is an event, never a qualified measurement, and is
    impossible on a text-only epoch.

Run: /opt/turnstone-venv/bin/python s6/evidence/liveness_proof.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

LIVENESS = pathlib.Path(__file__).resolve().parents[1] / "liveness.py"
spec = importlib.util.spec_from_file_location("s6liveness", LIVENESS)
liv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(liv)

PASS = 0
FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        print(f"PASS  {label}")
        PASS += 1
    else:
        print(f"FAIL  {label}: got {got!r} want {want!r}")
        FAIL += 1


def turns(*, plain=0, attachment=0, attachment_work=True, ack=0):
    rows = []
    rows += [{"text": "do the work", "has_attachment": False,
              "substantive": True, "attachment_work": False}] * plain
    rows += [{"text": ".", "has_attachment": True, "substantive": False,
              "attachment_work": attachment_work}] * attachment
    rows += [{"text": ".", "has_attachment": False, "substantive": False,
              "attachment_work": False}] * ack
    return rows


def cov(r):
    return r["dimensions"]["coverage"]


def grd(r):
    return r["dimensions"]["grading"]


def main() -> int:
    # ---- the failure this script exists for ---------------------------------
    r = liv.assess(acceptance_operator_observations=0,
                   acceptance_operator_admitted=0,
                   turns=turns(plain=5), elapsed_h=2.0)
    check("dead sensor with eligible work -> FAIL", r["verdict"], "FAIL")
    check("failure names the condition",
          any("0 operator observations" in f for f in r["failures"]), True)

    # ---- but silence with no eligible work is not a fault -------------------
    r = liv.assess(acceptance_operator_observations=0,
                   acceptance_operator_admitted=0,
                   turns=turns(ack=3), elapsed_h=2.0)
    check("only acknowledgements -> NO_ELIGIBLE_WORK", r["verdict"], "NO_ELIGIBLE_WORK")

    # ---- a single eligible turn is enough to demand an observation ----------
    r = liv.assess(acceptance_operator_observations=0,
                   acceptance_operator_admitted=0,
                   turns=turns(plain=1), elapsed_h=0.1)
    check("one eligible turn, zero observations -> FAIL", r["verdict"], "FAIL")

    # ---- healthy path --------------------------------------------------------
    r = liv.assess(acceptance_operator_observations=2,
                   acceptance_operator_admitted=2,
                   turns=turns(plain=2), elapsed_h=0.3)
    check("2 turns / 2 observations -> HEALTHY", r["verdict"], "HEALTHY")
    check("healthy coverage ratio", cov(r)["ratio"], 1.0)

    # ---- partial coverage FAILS ---------------------------------------------
    # Superseded 2026-10-18 on operator instruction: a shortfall against an
    # independently derived denominator used to be a caveat, which exited 0 and
    # let a partially dead seam report healthy. Suppressing most verified
    # operator work is a measurement failure and must alert.
    r = liv.assess(acceptance_operator_observations=1,
                   acceptance_operator_admitted=1,
                   turns=turns(plain=4), elapsed_h=1.0)
    check("1 of 4 covered -> FAIL", r["verdict"], "FAIL")
    check("low coverage is named as a failure",
          any("independently verified" in f for f in r["failures"]), True)
    check("and is not demoted to a caveat",
          any("possible suppression" in w for w in r["warnings"]), False)

    # Just above the floor stays a caveat-free pass.
    r = liv.assess(acceptance_operator_observations=2,
                   acceptance_operator_admitted=2,
                   turns=turns(plain=4), elapsed_h=1.0)
    check("2 of 4 covered (at the floor) -> not a failure",
          r["verdict"] == "FAIL", False)

    # ---- cadence/store divergence is its own failure -------------------------
    r = liv.assess(acceptance_operator_observations=1,
                   acceptance_operator_admitted=3,
                   turns=turns(plain=3), elapsed_h=1.0)
    check("admitted > observed -> FAIL", r["verdict"], "FAIL")
    check("divergence is named",
          any("cadence/store divergence" in f for f in r["failures"]), True)

    # ---- predecessor epoch: attachment turns are a measured blind spot -------
    r = liv.assess(acceptance_operator_observations=2,
                   acceptance_operator_admitted=2,
                   turns=turns(plain=2, attachment=2), elapsed_h=0.3)
    check("predecessor: attachment share of work", cov(r)["attachment_share_of_work"], 0.5)
    check("predecessor: attachment turns excluded from denominator",
          (r["eligible_turns"], r["excluded_attachment_turns"]), (2, 2))
    check("predecessor: blindness is a caveat, not a failure",
          r["verdict"], "HEALTHY_WITH_CAVEATS")

    # ---- an attachment-only predecessor epoch is not proof of a dead seam ----
    r = liv.assess(acceptance_operator_observations=0,
                   acceptance_operator_admitted=0,
                   turns=turns(attachment=6), elapsed_h=5.0)
    check("predecessor: attachment-only epoch does not FAIL",
          r["verdict"], "HEALTHY_WITH_CAVEATS")
    check("predecessor: attachment-only share is 1.0",
          cov(r)["attachment_share_of_work"], 1.0)

    # ---- attachment-aware epoch: the SAME silence IS a failure ---------------
    # This is the behaviour the fix has to earn. If the deployed code claims to
    # observe attachments, an attachment-only epoch with zero observations is a
    # dead seam, not a blind spot.
    r = liv.assess(acceptance_operator_observations=0,
                   acceptance_operator_admitted=0,
                   turns=turns(attachment=6), elapsed_h=5.0,
                   attachments_observed=True)
    check("attachment-aware: silent attachment turn -> FAIL",
          r["verdict"], "FAIL")
    check("attachment-aware: no turn is excluded",
          (r["eligible_turns"], r["excluded_attachment_turns"]), (6, 0))

    # ---- attachment-aware epoch, fully covered -------------------------------
    r = liv.assess(acceptance_operator_observations=4,
                   acceptance_operator_admitted=4,
                   turns=turns(plain=2, attachment=2), elapsed_h=1.0,
                   attachments_observed=True,
                   basis_counts={"text": 2, "text_plus_attachments": 2})
    # Deliberately re-pinned 2026-10-18: a fully covered mixed turn is healthy,
    # but its grade covers the TEXT only, so it carries a caveat rather than a
    # clean bill. The expectation changed because the reporting got more
    # honest, not because coverage regressed.
    check("attachment-aware: mixed work covered -> HEALTHY_WITH_CAVEATS",
          r["verdict"], "HEALTHY_WITH_CAVEATS")
    check("attachment-aware: attachment turns count as eligible",
          cov(r)["eligible_attachment"], 2)
    check("attachment-aware: qualified rate", grd(r)["qualified_rate"], 1.0)
    # A graded observation that also carried attachments is graded on the TEXT:
    # honest about what the grade does not cover, without calling it ungraded.
    check("text-graded turn with attachments is flagged",
          grd(r)["graded_text_with_attachment_context"], 2)
    check("...as a caveat, not a failure", r["verdict"], "HEALTHY_WITH_CAVEATS")
    check("...named as incomplete semantic coverage",
          any("not complete semantic coverage" in w for w in r["warnings"]), True)

    # ---- grading dimension: descriptor-only is an event, not a measurement ---
    r = liv.assess(acceptance_operator_observations=2,
                   acceptance_operator_admitted=2,
                   turns=turns(attachment=2), elapsed_h=1.0,
                   attachments_observed=True,
                   basis_counts={"attachments_only": 2})
    check("descriptor-only grading warns", r["verdict"], "HEALTHY_WITH_CAVEATS")
    check("descriptor-only is not counted as graded",
          grd(r)["graded_full"], 0)
    check("descriptor-only is counted as structural",
          grd(r)["structural_only"], 2)
    # Corrected 2026-10-18: descriptor-only rows are excluded from dimension B's
    # denominator, so the rate is UNDEFINED rather than zero. A zero would claim
    # the grader failed; it never received the document.
    check("descriptor-only qualified rate is undefined, not zero",
          grd(r)["qualified_rate"], None)
    check("descriptor-only contributes no gradeable events",
          grd(r)["gradeable"], 0)
    check("descriptor-only still counted as work (dimension A)",
          cov(r)["observed"], 2)
    # CORRECTED 2026-10-18 on operator instruction, and the earlier assertion
    # here was wrong. The approved Dimension A gate counts GENUINE OPERATOR-WORK
    # OBSERVATION: a descriptor-only row IS such an event and DOES count toward
    # the 100-event/48h gate. What it must never do is count as qualified
    # semantic grading in Dimension B. The old wording ("must not reach the
    # 100-observation gate") contradicted the gate, the collector and this
    # proof's own dimension-A assertion two lines above.
    check("descriptor-only counts toward the event gate (dimension A)",
          any("count toward the 100-observation gate" in w and "dimension A" in w
              for w in r["warnings"]), True)
    check("descriptor-only is excluded from semantic grading, not from the gate",
          any("excluded from qualified semantic grading" in w for w in r["warnings"]),
          True)
    check("the withdrawn wording is gone",
          any("must not reach the 100-observation gate" in w for w in r["warnings"]),
          False)
    check("the caveat still says the grader never saw the document",
          any("not the document" in w for w in r["warnings"]), True)

    # ---- descriptor-only on a text-only epoch is an integrity failure -------
    # The predecessor code cannot emit that basis. Seeing one means the running
    # drop-in is not the code the epoch was opened for.
    r = liv.assess(acceptance_operator_observations=2,
                   acceptance_operator_admitted=2,
                   turns=turns(plain=2), elapsed_h=1.0,
                   attachments_observed=False,
                   basis_counts={"attachments_only": 2})
    check("descriptor-only on a text-only epoch -> FAIL", r["verdict"], "FAIL")
    check("code/epoch mismatch is named",
          any("does not match the epoch" in f for f in r["failures"]), True)

    # ---- legacy rows: inferred basis is certain, so it grades, and is shown --
    r = liv.assess(acceptance_operator_observations=2,
                   acceptance_operator_admitted=2,
                   turns=turns(plain=2), elapsed_h=1.0,
                   basis_counts={"text_inferred": 2})
    check("inferred basis grades as full", grd(r)["graded_full"], 2)
    check("inferred basis is reported separately", grd(r)["inferred_basis"], 2)
    check("inferred basis alone is not a caveat", r["verdict"], "HEALTHY")

    # ---- an unrecognised basis is not silently trusted ----------------------
    r = liv.assess(acceptance_operator_observations=1,
                   acceptance_operator_admitted=1,
                   turns=turns(plain=1), elapsed_h=1.0,
                   basis_counts={"future_basis": 1})
    check("unrecognised basis warns", r["verdict"], "HEALTHY_WITH_CAVEATS")
    check("unrecognised basis is not graded", grd(r)["graded_full"], 0)
    check("unrecognised basis is counted", grd(r)["unknown_basis"], 1)

    # ---- capability detection is anchored to evidence, not to a label --------
    # A fresh epoch has no rows: only the running code can answer.
    def slot_tree(tmp: pathlib.Path, body: str) -> str:
        module = tmp / "turnstone" / "core"
        module.mkdir(parents=True, exist_ok=True)
        (module / "sensor_lifecycle.py").write_text(body)
        return str(tmp)

    import tempfile

    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmpdir:
        tmp = pathlib.Path(tmpdir)
        aware = slot_tree(tmp / "aware", "def attachment_projection(attachments):\n    pass\n")
        blind = slot_tree(tmp / "blind", "def is_substantive_instruction(text):\n    pass\n")

        # The running code answers ONLY for the epoch it is writing to, so
        # these cases pass current_epoch=tmp. A historical epoch deliberately
        # withholds the probe (see the next block).
        check("no rows + no running code -> blind",
              liv.detect_attachment_capability(tmp, units=[],
                                               current_epoch=tmp)[0], False)

        liv.live_code_slots = lambda units: [blind]  # type: ignore[assignment]
        got = liv.detect_attachment_capability(tmp, units=[], current_epoch=tmp)
        check("running text-only code -> blind", got[0], False)
        check("...and it says why", "predates" in got[1], True)

        liv.live_code_slots = lambda units: [aware]  # type: ignore[assignment]
        got = liv.detect_attachment_capability(tmp, units=[], current_epoch=tmp)
        check("running attachment-aware code -> observed", got[0], True)
        check("...anchored on the code path", "running code" in got[1], True)

        # A half-rolled-back cluster is the dangerous case: it must fail closed
        # rather than let one capable node speak for six.
        liv.live_code_slots = lambda units: [aware, blind]  # type: ignore[assignment]
        got = liv.detect_attachment_capability(tmp, units=[], current_epoch=tmp)
        check("mixed cluster -> blind (fails closed)", got[0], False)
        check("...named as mixed", got[1].startswith("MIXED cluster"), True)

        # A HISTORICAL epoch must ignore the same attachment-aware code: it did
        # not write that telemetry, so it cannot redefine which turns were
        # eligible. This is the defect behind s6-t1's false 23% suppression.
        other = tmp / "current"
        other.mkdir(exist_ok=True)
        liv.live_code_slots = lambda units: [aware]  # type: ignore[assignment]
        got = liv.detect_attachment_capability(tmp, units=[], current_epoch=other)
        check("historical epoch ignores today's attachment-aware code",
              got[0], False)
        check("...and says the code did not write it",
              "historical epoch" in got[1], True)

    # Epoch evidence outranks the probe: a row that CARRIES content_basis can
    # only have been written by attachment-aware code.
    got = liv.detect_attachment_capability(pathlib.Path("/nonexistent"), explicit_basis=3)
    check("explicit basis in the rows -> observed", got[0], True)
    check("...cited as epoch evidence", "epoch evidence" in got[1], True)

    # An override is honoured and labelled, never silently.
    check("override yes",
          liv.detect_attachment_capability(pathlib.Path("."), override="yes")[0], True)
    check("override yes is labelled",
          liv.detect_attachment_capability(pathlib.Path("."), override="yes")[1],
          "operator override")
    check("override no",
          liv.detect_attachment_capability(pathlib.Path("."), explicit_basis=9,
                                           override="no")[0], False)

    # An INFERRED legacy basis is not evidence of the new code.
    check("inferred legacy basis is not capability evidence",
          liv.detect_attachment_capability(pathlib.Path("/nonexistent"),
                                           explicit_basis=0)[0], False)

    # ---- ORIGIN: role is not the discriminator (2026-10-18) -------------------
    # A queued operator message is persisted as role='system',
    # _source='user_interjection'. A system nudge is persisted as role='user',
    # _source='system_nudge'. Keying the denominator on role alone therefore
    # BOTH undercounts real operator work and counts machine text as work.
    check("a user_interjection row is operator work",
          liv.distinguish_origin(role="system", source="user_interjection"), "operator")
    check("a plain user turn is operator work",
          liv.distinguish_origin(role="user", source=None), "operator")
    check("a system nudge riding role=user is NOT operator work",
          liv.distinguish_origin(role="user", source="system_nudge"), "system")
    check("a watch result is NOT operator work",
          liv.distinguish_origin(role="system", source="watch_triggered"), "system")
    check("an output-guard finding is NOT operator work",
          liv.distinguish_origin(role="system", source="output_guard"), "system")
    check("the interjection framing is stripped before judging substance",
          liv.operator_message_of(
              "The user sent a message while you were working. You MUST address "
              "this before continuing.\n\nUser message: stop and regroup"),
          "stop and regroup")
    # The fixture below is SYNTHETIC. An earlier revision of this proof used
    # a real operator message from the live epoch as its "plain message"
    # sample. Every string in this proof is now invented, so the file can be
    # published without carrying operator content.
    check("a plain message is passed through unchanged",
          liv.operator_message_of("synthetic probe message"),
          "synthetic probe message")

    # ---- an interjection is eligible work in the denominator ----------------
    r = liv.assess(acceptance_operator_observations=2,
                   acceptance_operator_admitted=2,
                   turns=turns(plain=2), elapsed_h=1.0)
    check("a substantive interjection is eligible", r["eligible_turns"], 2)
    check("and covered", cov(r)["ratio"], 1.0)

    # ---- classification ------------------------------------------------------
    check("attachment-delivered instruction is its own class",
          liv.classify_turn({"text": ".", "has_attachment": True,
                             "substantive": False, "attachment_work": True}),
          "attachment_substantive")
    check("substantive text with a tiny attachment stays plain work",
          liv.classify_turn({"text": "a real instruction",
                             "has_attachment": True, "substantive": True,
                             "attachment_work": False}),
          "plain_substantive")
    check("plain substantive turn",
          liv.classify_turn({"text": "a real instruction",
                             "has_attachment": False, "substantive": True,
                             "attachment_work": False}),
          "plain_substantive")
    check("acknowledgement turn",
          liv.classify_turn({"text": ".", "has_attachment": False,
                             "substantive": False, "attachment_work": False}),
          "acknowledgement")

    print("---")
    print(f"pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
