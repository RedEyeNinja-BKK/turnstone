#!/usr/bin/env python3
"""S6 measurement LIVENESS check — READ ONLY.

`collect.py` answers "how many observations do we have?". That is the wrong
question to ask about health: an epoch with ZERO observations has zero unmatched
admitted rows, zero duplicates and zero orphans, so every integrity gate reads
MET and a dead sensor looks perfect.

This script compares the sensor's output against independently measured
eligible work — operator turns recorded in the conversation store — and fails
loudly when real work exists but nothing was observed.

It is read-only in every direction: SELECT-only SQL, JSONL reads, no writes to
telemetry, no service interaction, no network calls beyond the local database
socket.

Usage:
    /opt/turnstone-venv/bin/python s6/liveness.py
    /opt/turnstone-venv/bin/python s6/liveness.py --json
    /opt/turnstone-venv/bin/python s6/liveness.py --epoch /var/lib/turnstone/sensor/s6-t1

Exit codes: 0 healthy or not-yet-eligible, 1 LIVENESS FAILURE, 2 setup error.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
#: `--epoch` overrides it; the default is resolved per run by
#: collect.resolve_epoch() to the epoch with the LATEST authoritative boundary.
#: A literal default here is how the standing liveness watch kept reporting a
#: superseded campaign after s6-t2 went live.
DEFAULT_DSN = "dbname=turnstone"
# A workstream whose instruction volume is dominated by attachments will never
# produce observations for those turns. Below this many plain-text eligible
# turns a zero reading is not yet evidence of a fault.
MIN_EVIDENCE_TURNS = 1
# If at least this many eligible turns exist and the sensor has observed fewer
# than this fraction of them, the sensor is not merely quiet: independently
# verified operator work is going unobserved, which is a FAILURE verdict (and a
# non-zero exit so the scheduled watch alerts), not a caveat.
COVERAGE_FLOOR = 0.5
COVERAGE_MIN_TURNS = 3

# Operator turns whose text is an acknowledgement are correctly skipped. These
# are the same tokens sensor_lifecycle.ACKNOWLEDGEMENTS covers; kept local so
# this script does not need the sensor package on its path.
ACK_TOKENS = {".", "ok", "okay", "k", "thanks", "thank you", "continue", "go on",
              "yes", "y", "no", "n", "sure", "got it", "nice", "great", "hm",
              "hmm", "..."}


def load_collector():
    spec = importlib.util.spec_from_file_location("s6collect", HERE / "collect.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def classify_turn(row: dict) -> str:
    """Bucket one operator turn into the eligible-work classes.

    ``row`` needs ``text``, ``has_attachment``, ``substantive`` and
    ``attachment_work`` (the latter two computed locally by the caller).

    An attachment turn that also carries substantive text is plain work with a
    stray attachment, not an attachment-only instruction: the text alone is a
    valid observation basis. Only turns whose instruction lives *in* the
    payload are ``attachment_substantive``.
    """
    if row.get("has_attachment") and row.get("attachment_work"):
        return "attachment_substantive"
    if row.get("substantive"):
        return "plain_substantive"
    return "acknowledgement"


# How an observation's content_basis maps onto the two measurement dimensions.
# "text" and "text_plus_attachments" grade real instruction text; a legacy row
# written before the field existed can only have come from the text-only
# predecessor path, so it is inferred (and counted separately) rather than
# silently assumed.
FULL_BASIS = ("text", "text_plus_attachments", "text_inferred")
STRUCTURAL_BASIS = "attachments_only"


def assess(
    *,
    acceptance_operator_observations: int,
    acceptance_operator_admitted: int = 0,
    turns: list[dict],
    elapsed_h: float | None,
    attachments_observed: bool = False,
    basis_counts: dict | None = None,
    raw_operator_observations: int | None = None,
) -> dict:
    """Pure liveness verdict over already-classified turns.

    Two independent dimensions, deliberately kept apart:

    1. EVENT COVERAGE - did a substantive operator turn produce an operator
       observation? The denominator comes from the conversation store, never
       from the sensor. Whether attachment-delivered turns belong in that
       denominator depends on whether the *running* code can see them:
       ``attachments_observed=False`` (the predecessor sensor) excludes them
       and reports them as a blind share; ``True`` counts them as eligible
       work, because the fix makes them observable and a silent attachment
       turn is then a real fault.

    2. GRADING QUALITY - was the observation grounded in the instruction? An
       ``attachments_only`` observation means the backend graded a descriptor,
       not the document. It is an event, never a qualified five-signal
       measurement. On a text-only sensor such a row cannot legitimately exist
       at all, so it is an integrity failure rather than a caveat.
    """
    buckets = {"plain_substantive": 0, "attachment_substantive": 0,
               "acknowledgement": 0}
    for row in turns:
        buckets[classify_turn(row)] += 1

    plain = buckets["plain_substantive"]
    attachment = buckets["attachment_substantive"]
    # `observed` is the ATTRIBUTABLE count and drives coverage: a row that
    # cannot be matched to a stored operator turn must not raise coverage
    # against a store-derived denominator. The internal cross-checks (admitted
    # vs written, rows vs basis) must still compare like with like, so they use
    # the raw row count when the caller supplies it.
    observed = acceptance_operator_observations
    raw_observed = (raw_operator_observations
                    if raw_operator_observations is not None else observed)

    # Dimension 1 denominator.
    eligible = plain + (attachment if attachments_observed else 0)
    unobservable = 0 if attachments_observed else attachment
    substantive_total = plain + attachment
    attachment_share = (
        round(attachment / substantive_total, 3) if substantive_total else None
    )

    failures: list[str] = []
    warnings: list[str] = []

    if acceptance_operator_admitted > raw_observed:
        failures.append(
            f"{acceptance_operator_admitted} operator event(s) were ADMITTED but "
            f"only {raw_observed} observation(s) exist: cadence/store divergence"
        )

    # Dimension 2 accounting.
    basis = dict(basis_counts or {})
    structural = int(basis.get(STRUCTURAL_BASIS, 0))
    graded_full = sum(int(basis.get(k, 0)) for k in FULL_BASIS)
    inferred_basis = int(basis.get("text_inferred", 0))
    attachment_context = int(basis.get("text_plus_attachments", 0))
    known = set(FULL_BASIS) | {STRUCTURAL_BASIS, "text_inferred"}
    unknown_basis = sum(v for k, v in basis.items() if k not in known)
    # Everything whose content reached the grader: plain/inferred text and
    # text-with-attachment. Structural-only is deliberately absent.
    gradeable = graded_full + unknown_basis
    attributed = graded_full + structural + unknown_basis

    if structural and not attachments_observed:
        failures.append(
            f"{structural} observation(s) carry content_basis=attachments_only on "
            "an epoch whose code cannot observe attachments: the running drop-in "
            "does not match the epoch it is writing to"
        )
    elif structural:
        warnings.append(
            f"{structural} observation(s) graded a descriptor, not the document "
            "(content_basis=attachments_only). Those ARE genuine operator-work "
            "events and DO count toward the 100-observation gate (dimension A); "
            "they are excluded from qualified semantic grading (dimension B), "
            "because the grader never received the document"
        )
    if attachments_observed and structural > attachment:
        warnings.append(
            f"{structural} descriptor-only observation(s) but only {attachment} "
            "attachment-delivered turn(s) in the store: attribution mismatch"
        )
    if attachment_context:
        warnings.append(
            f"{attachment_context} graded observation(s) were graded on the "
            "operator's TEXT while the turn also carried attachments: the "
            "attachment contents were not in the graded representation, so they "
            "are not complete semantic coverage of an attachment-heavy task"
        )
    if unknown_basis:
        warnings.append(
            f"{unknown_basis} observation(s) carry an unrecognised content_basis: "
            "the graded/structural split cannot be proven for them"
        )
    if attributed and attributed != raw_observed:
        warnings.append(
            f"{raw_observed} operator observation(s) but {attributed} classified by "
            "content_basis: some rows are outside the admitted operator slice"
        )

    # Dimension 1 verdict. The denominator is independently derived from the
    # conversation store, so a shortfall is not a matter of opinion.
    if eligible >= MIN_EVIDENCE_TURNS and observed == 0:
        failures.append(
            f"{eligible} independently verified eligible turn(s) since the epoch "
            "boundary and 0 operator observations: the sensor is not observing "
            "real work"
        )
    elif eligible >= COVERAGE_MIN_TURNS:
        coverage = observed / eligible
        if coverage < COVERAGE_FLOOR:
            # A CAVEAT here would exit 0 and let a partially dead seam look
            # healthy. Suppression of most independently verified operator work
            # is a failure of this epoch's measurement, and the scheduled watch
            # keys its alert on the FAIL verdict.
            failures.append(
                f"observed {observed} of {eligible} independently verified "
                f"eligible turns ({coverage:.0%}, floor "
                f"{COVERAGE_FLOOR:.0%}): most operator work produced no "
                "observation - possible suppression, dedupe over-firing, or a "
                "failing seam"
            )
    elif 0 < observed and eligible == 0:
        warnings.append(
            "observations exist but no eligible turn was found in the "
            "conversation store - attribution mismatch, investigate"
        )

    if unobservable:
        warnings.append(
            f"{unobservable} substantive operator turn(s) delivered their "
            "instruction as an attachment; the running code sees only the "
            "accompanying text, so they are excluded from the denominator "
            "(known blind spot of this epoch, not a fault)"
        )

    if failures:
        verdict = "FAIL"
    elif elapsed_h is None:
        verdict = "NOT_STARTED"
    elif eligible == 0 and observed == 0 and substantive_total == 0:
        verdict = "NO_ELIGIBLE_WORK"
    elif warnings:
        verdict = "HEALTHY_WITH_CAVEATS"
    else:
        verdict = "HEALTHY"

    return {
        "verdict": verdict,
        "failures": failures,
        "warnings": warnings,
        "buckets": buckets,
        "attachments_observed": attachments_observed,
        "acceptance_operator_observations": observed,
        "acceptance_operator_admitted": acceptance_operator_admitted,
        "eligible_turns": eligible,
        "excluded_attachment_turns": unobservable,
        "elapsed_hours": round(elapsed_h, 2) if elapsed_h is not None else None,
        "dimensions": {
            "coverage": {
                "eligible_plain": plain,
                "eligible_attachment": attachment if attachments_observed else 0,
                "unobservable_attachment": unobservable,
                "eligible_total": eligible,
                "observed": observed,
                "ratio": round(observed / eligible, 3) if eligible else None,
                "attachment_share_of_work": attachment_share,
            },
            "grading": {
                "basis": basis,
                "graded_full": graded_full,
                "graded_text_with_attachment_context": attachment_context,
                "inferred_basis": inferred_basis,
                "structural_only": structural,
                "unknown_basis": unknown_basis,
                # DIMENSION B denominator. `attachments_only` is EXCLUDED, not
                # counted as a failure: the grader never received the document,
                # so it cannot have graded it badly. Counting it in the
                # denominator would make a document-heavy workload look like
                # grader malfunction.
                "gradeable": gradeable,
                "qualified_rate": (
                    round(graded_full / gradeable, 3) if gradeable else None
                ),
            },
        },
    }


#: ``_source`` values that carry a REAL operator message. A queued operator
#: message is persisted as ``role='system'`` with the framing below; everything
#: else in the system-turn sources is machine-authored.
OPERATOR_SOURCES = frozenset({"user_interjection"})
#: A machine-authored turn persisted with ``role='user'``. Proof that role is
#: not an origin signal in either direction.
NON_OPERATOR_USER_SOURCES = frozenset({"system_nudge"})
#: The fixed framing ``render_user_interjection`` puts in front of the message.
_INTERJECTION_MARKER = "User message: "


def distinguish_origin(*, role: str, source: str | None) -> str:
    """``operator`` or ``system`` for one stored turn.

    Two fields are needed and neither is sufficient: ``role='user'`` also
    carries ``system_nudge``, and ``role='system'`` also carries
    ``user_interjection``. Counting by role alone undercounts genuine operator
    work AND counts machine text as work.
    """
    if source in OPERATOR_SOURCES:
        return "operator"
    if source in NON_OPERATOR_USER_SOURCES:
        return "system"
    return "operator" if role == "user" else "system"


def operator_message_of(content: str) -> str:
    """The operator's own words from a stored turn.

    An interjection row stores a fixed instruction preamble around the message.
    Judging substance on the whole row would grade the preamble, which is
    substantive prose written by the machine - so it is stripped first.
    """
    text = content or ""
    index = text.rfind(_INTERJECTION_MARKER)
    if index == -1:
        return text
    return text[index + len(_INTERJECTION_MARKER):].strip()


def fetch_turns(dsn: str, since_iso: str, until_iso: str | None = None) -> list[dict]:
    """Read operator turns at/after ``since_iso`` and classify substance locally.

    Only ``content`` of the operator's own message is examined, and only to run
    the shipped substance filter in-process. Nothing is printed, stored or sent
    anywhere. Attachment bytes are never read here.
    """
    import psycopg

    from turnstone.core.sensor_lifecycle import is_substantive_instruction

    # Attachment turns: the visible text (".") is NOT the instruction, so a
    # text-only substance test calls them acknowledgements. They are fetched
    # with their attachment STRUCTURE (kind/mime/size) - never their bytes and
    # never their filenames - and classified on the same rule the live seam
    # uses at admission: a text attachment is real work when it is large enough
    # to be an instruction; other kinds when they are non-trivial.
    #
    # The monitor deliberately does not re-derive the sensor's byte-level
    # decision. It asks: was there a substantive payload the sensor was
    # supposed to see? That is the independent denominator.
    query = """
        select coalesce(c.content, ''),
               (c.attachments is not null and c.attachments <> ''
                and c.attachments <> '[]'),
               coalesce((
                 select jsonb_agg(jsonb_build_object(
                          'kind', a.kind,
                          'size', a.size_bytes,
                          'mime', a.mime_type))
                   from workstream_attachments a
                  where a.attachment_id in (
                        select jsonb_array_elements_text(c.attachments::jsonb))
               ), '[]'::jsonb),
               c.role,
               c._source
          from conversations c
         where c.timestamp >= %s
           and (%s::text is null or c.timestamp < %s::text)
           and (   (c.role = 'user'
                    and coalesce(c._source, '') not in ('system_nudge'))
                or c._source = 'user_interjection')
         order by c.timestamp
    """
    rows: list[dict] = []
    with psycopg.connect(dsn) as conn:
        conn.execute("set transaction read only")
        for (content, has_attachment, attachment_meta,
             role, source) in conn.execute(query, (since_iso, until_iso, until_iso)):
            attachments = list(attachment_meta or [])
            # An interjection's own words, not the machine-written preamble.
            message = operator_message_of(content)
            rows.append({
                "text": message,
                "has_attachment": bool(has_attachment),
                "substantive": bool(is_substantive_instruction(message)),
                "attachment_work": _attachment_is_work(attachments),
                "attachment_kinds": sorted(
                    {str(a.get("kind") or "?") for a in attachments}
                ),
                "origin": distinguish_origin(role=role or "", source=source),
                "interjection": source in OPERATOR_SOURCES,
            })
    return rows


def _attachment_is_work(attachments: list[dict]) -> bool:
    """Whether an attachment set carries an instruction, judged structurally.

    Mirrors the admission rule's SHAPE without touching content: the monitor
    knows only kind and size, so it applies a size floor. A text attachment at
    or above the floor is presumed to carry the instruction (which is what the
    audit measured: 54/54 such turns were substantive); pdf/image/audio above
    the floor are presumed real operator work too.

    Deliberately conservative in the SAME direction as the sensor: a turn the
    monitor counts as work is one the sensor is expected to observe.
    """
    MIN_BYTES = 64
    for att in attachments:
        try:
            size = int(att.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if size >= MIN_BYTES:
            return True
    return False


#: Marker string that only attachment-aware code contains. Reading the file is
#: enough: the module is not imported, so this cannot execute anything from the
#: tree being inspected.
_ATTACHMENT_SEAM_MARKER = "def attachment_projection("


def _dropin_slot(unit: str) -> str:
    """The overlay a unit is configured to run, from its sensor drop-in."""
    import subprocess

    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "Environment", "-p", "ExecStart",
             "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:  # noqa: BLE001 - a probe must never break the report
        out = ""
    for token in out.replace("\\n", " ").split():
        if token.startswith("PYTHONPATH="):
            return token.split("=", 1)[1].replace(":", " ")
    return ""


def live_code_slots(units: list[str]) -> list[str]:
    """Overlay roots that the RUNNING services are executing.

    Read-only: the drop-in is inspected for the configured PYTHONPATH and the
    process environment confirms it. Nothing is imported or restarted.
    """
    import subprocess

    slots: list[str] = []
    for unit in units:
        pid = ""
        try:
            pid = subprocess.run(
                ["systemctl", "show", unit, "-p", "MainPID", "--value"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            continue
        overlay = ""
        if pid and pid not in {"0", "1"}:
            try:
                environ = pathlib.Path(f"/proc/{pid}/environ").read_bytes()
            except OSError:
                environ = b""
            for entry in environ.split(b"\0"):
                if entry.startswith(b"PYTHONPATH="):
                    overlay = entry.split(b"=", 1)[1].decode("utf-8", "replace")
                    break
        if not overlay:
            overlay = _dropin_slot(unit)
        for part in overlay.split(":"):
            if part.startswith("/opt/turnstone-overlays/") and part not in slots:
                slots.append(part)
    return slots


def read_capability_record(epoch: pathlib.Path) -> tuple[bool | None, str, str]:
    """The epoch's own dated capability statement, if it has one.

    Written by the activation script at the instant it verified 6/6 services
    against a slot, so it describes the code that CREATED this epoch -- not the
    code running today. Returns ``(value, provenance, problem)``.
    """
    path = epoch / "CAPABILITY"
    if not path.is_file():
        return None, "", ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        return None, "", f"CAPABILITY unreadable: {error}"
    fields: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    raw = fields.get("capability.attachments_observed")
    if raw is None:
        return None, "", "CAPABILITY exists but does not state attachments_observed"
    if raw not in {"0", "1"}:
        return None, "", f"CAPABILITY states attachments_observed={raw!r}"
    detail = [
        f"slot={fields['slot']}" if fields.get("slot") else "",
        f"verified {fields['verified_utc']}" if fields.get("verified_utc") else "",
        f"services={fields['services']}" if fields.get("services") else "",
    ]
    detail = ", ".join(d for d in detail if d)
    provenance = "epoch CAPABILITY record: " + detail if detail else (
        "epoch CAPABILITY record"
    )
    return raw == "1", provenance, ""


def detect_attachment_capability(
    epoch: pathlib.Path,
    *,
    explicit_basis: int = 0,
    override: str = "auto",
    units: list[str] | None = None,
    current_epoch: pathlib.Path | None = None,
) -> tuple[bool, str]:
    """Whether the code that PRODUCED this epoch's telemetry saw attachments.

    Anchored to this epoch's own evidence, in this order -- never to an epoch
    label, and never to "whatever happens to be running":

    1. AN EXPLICIT OPERATOR OVERRIDE, recorded as such in the output.
    2. THE EPOCH'S OWN CAPABILITY RECORD. The activation script writes one when
       it verifies the six services against a slot; it is a dated statement
       about the code that created the epoch.
    3. THE EPOCH'S OWN ROWS. ``content_basis`` did not exist in the predecessor,
       so a row carrying it was written by attachment-aware code.
    4. THE RUNNING CODE -- ONLY for the CURRENT epoch, i.e. the one the running
       services are writing to. It answers "yes" on a fresh epoch with no rows
       yet, and catches a partially rolled-back cluster.

    Anything older than the current epoch gets 4's answer withheld: the overlay
    running today did not write that telemetry, so consulting it would
    retrospectively re-define which historical turns were eligible. That is not
    a hypothetical: it turned s6-t1's 3-of-3 coverage into a false 3-of-13
    suppression alarm the moment the cluster moved to attachment-aware code.

    Returns (attachments_observed, provenance-of-the-answer).
    """
    if override == "yes":
        return True, "operator override"
    if override == "no":
        return False, "operator override"

    if current_epoch is None:
        try:
            current_epoch = load_collector().resolve_epoch()[0]
        except LookupError:
            current_epoch = None
    is_current = (
        current_epoch is not None
        and pathlib.Path(current_epoch).resolve() == pathlib.Path(epoch).resolve()
    )

    recorded, provenance, record_problem = read_capability_record(epoch)
    if recorded is not None:
        if recorded is False and explicit_basis:
            return False, (
                f"CONFLICT: {provenance} says text-only, but {explicit_basis} "
                "admitted observation(s) carry content_basis; failing closed"
            )
        return recorded, provenance

    if explicit_basis:
        return True, (
            f"epoch evidence: {explicit_basis} admitted observation(s) carry "
            "content_basis, a field the predecessor cannot emit"
        )

    if not is_current:
        reason = (
            "historical epoch: no capability record and no content_basis rows; "
            "the code running today did not write this telemetry, so this "
            "epoch keeps the predecessor's text-only eligibility"
        )
        if record_problem:
            reason += f" (CAPABILITY: {record_problem})"
        return False, reason

    if units is None:
        units = [
            "turnstone-node@5", "turnstone-node@4", "turnstone-node@3",
            "turnstone-node@2", "turnstone-console", "turnstone-server",
        ]
    slots = live_code_slots(units)
    capable, plain = [], []
    for slot in slots:
        module = pathlib.Path(slot) / "turnstone" / "core" / "sensor_lifecycle.py"
        try:
            text = module.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        (capable if _ATTACHMENT_SEAM_MARKER in text else plain).append(slot)
    if capable and not plain:
        return True, "running code carries the attachment seam: " + ", ".join(capable)
    if capable and plain:
        return False, (
            "MIXED cluster: attachment-aware " + ", ".join(capable)
            + " but text-only " + ", ".join(plain)
        )
    if plain:
        return False, "running code predates the attachment seam: " + ", ".join(plain)
    if record_problem:
        return False, f"no evidence: {record_problem}"
    return False, "no evidence: no rows with content_basis and no running code located"


def count_operator_observations(epoch: pathlib.Path, provenance: dict | None = None):
    """Post-T1 ``operator_instruction`` observations in the acceptance slice.

    Deliberately NOT "all acceptance observations": a delegation result or a
    task-agent failure is also an observation, and counting those would let a
    dead operator seam hide behind unrelated traffic. The comparison is
    turn-for-turn, so it has to be the same event class.

    ``provenance`` is handed to the collector so the acceptance gate it returns
    is built from the ATTRIBUTABLE event count. Without it the collector's
    gate FAILS CLOSED: its count is zero, ``met`` is false, and the raw
    observation count stays informational - so this script can never publish a
    raw count as the gate the scheduled watch reads.
    """
    collector = load_collector()
    result = collector.collect(epoch, provenance=provenance)
    # Same resolver as the collector: the epoch's own marker name (T2 for
    # s6-t2, T1 earlier), never a hardcoded filename.
    t1, _marker, _problem = collector.read_boundary(epoch)
    boundary = t1.timestamp() if t1 else None
    historical = epoch.name in collector.HISTORICAL_EPOCHS

    operator_observations = 0
    admitted_operator = 0
    explicit_basis = 0
    basis_counts: dict[str, int] = {}
    for service in collector.SERVICE_DIRS:
        sdir = epoch / service
        cadence = collector.read_jsonl(sdir / "shadow-cadence.jsonl")
        observations = collector.read_jsonl(sdir / "shadow-observations.jsonl")
        acc, _rv, _strad, _orph = collector.split_service(
            cadence, observations, boundary, historical
        )
        admitted = {r.get("event_id") for r in acc["admitted"]
                    if r.get("event") == "operator_instruction"}
        admitted_operator += len([e for e in admitted if e])
        for row in acc["observations"]:
            if row.get("event_id") not in admitted:
                continue
            operator_observations += 1
            # Legacy rows predate the field. The predecessor path only ever
            # admitted text, so the basis is inferable - but counted on its own
            # so "we know" and "we infer" never blur. `explicit_basis` counts
            # ONLY rows that actually carried the field: those, and only those,
            # prove the writer was attachment-aware.
            raw = row.get("content_basis")
            key = raw or "text_inferred"
            basis_counts[key] = basis_counts.get(key, 0) + 1
            if raw:
                explicit_basis += 1
    return operator_observations, admitted_operator, basis_counts, explicit_basis, result


def render(result: dict, liveness: dict, epoch: pathlib.Path) -> str:
    lines = ["S6 MEASUREMENT LIVENESS (read-only)", "=" * 62]
    lines.append(f"epoch        : {epoch}")
    if liveness.get("epoch_selection"):
        lines.append(f"selected by  : {liveness['epoch_selection']}")
    lines.append(f"verdict      : {liveness['verdict']}")
    lines.append("")
    dim = liveness["dimensions"]
    cov = dim["coverage"]
    grd = dim["grading"]
    lines.append("DIMENSION 1 - EVENT COVERAGE (denominator from the conversation store)")
    b = liveness["buckets"]
    lines.append(f"  plain-text substantive turns : {b['plain_substantive']}")
    lines.append(f"  attachment substantive turns : {b['attachment_substantive']}")
    lines.append(f"  acknowledgements             : {b['acknowledgement']}")
    lines.append(f"  attachment turns counted     : "
                 f"{'yes' if liveness['attachments_observed'] else 'NO (blind)'}")
    lines.append(f"  capability anchored on       : "
                 f"{liveness.get('capability_source', 'not determined')}")
    lines.append(f"  eligible turns               : {cov['eligible_total']}")
    lines.append(f"  observed operator events     : {cov['observed']}")
    raw = liveness.get("raw_operator_observations")
    if raw is not None and raw != cov["observed"]:
        # A DIFFERENT population from the "raw operator rows" figure in the
        # SENSOR OUTPUT block: that one counts every unique observation row,
        # this one counts the operator-class rows only. Same label for two
        # different numbers in one report is how a reader ends up comparing
        # them, so they are named apart.
        lines.append(f"      raw operator-class rows: {raw}"
                     "  (not all attributable to a stored turn)")
    cov_pct = "n/a" if cov["ratio"] is None else f"{cov['ratio']:.0%}"
    lines.append(f"  coverage                     : {cov_pct}")
    lines.append(f"  attachment share of work     : "
                 f"{cov['attachment_share_of_work']}")
    lines.append("")
    lines.append("DIMENSION 2 - QUALIFIED SEMANTIC GRADING (content available to grader)")
    lines.append(f"  gradeable events             : {grd['gradeable']}")
    lines.append(f"  graded in full               : {grd['graded_full']}")
    lines.append(f"  structural-only (not in B)   : {grd['structural_only']}"
                 "  - grader never saw the document")
    lines.append(f"  graded w/ attachment context : "
                 f"{grd['graded_text_with_attachment_context']}")
    lines.append(f"  basis inferred (pre-field)   : {grd['inferred_basis']}")
    lines.append(f"  unrecognised basis           : {grd['unknown_basis']}")
    q_pct = "n/a" if grd["qualified_rate"] is None else f"{grd['qualified_rate']:.0%}"
    lines.append(f"  qualified rate               : {q_pct}")
    lines.append("")
    lines.append("SENSOR OUTPUT")
    # The gate's count. The raw row count is reported beside it and labelled
    # raw: a row that cannot be attributed to a stored operator turn cannot
    # certify the campaign, so it is never presented as progress toward 100.
    gate = result.get("gate") or {}
    gate_obs = gate.get("observations") or {}
    lines.append(f"  attributable events : {gate_obs.get('have', 0)}"
                 f" / {gate_obs.get('need', 100)}"
                 "  (B + B2, the count that gates)")
    lines.append(f"  raw operator rows    : {result['unique_observations']}"
                 "  (every unique observation row; not all attributable"
                 " to a stored turn)")
    elapsed = gate.get("elapsed") or {}
    hours_left = elapsed.get("hours_remaining")
    lines.append(f"  elapsed             : {liveness['elapsed_hours']} h"
                 + (f"  ({hours_left} h of the {elapsed.get('need_hours')} h"
                    " minimum still to run)" if hours_left is not None else ""))
    pv = liveness.get("provenance")
    if pv:
        lines.append("")
        lines.append("PROVENANCE (A-E, separate from coverage and grading)")
        lines.append(f"  A raw admitted + observed    : {pv['A_raw_observed']}")
        lines.append(f"  B verified operator (in store): {pv['B_verified_operator']}")
        if pv.get("B2_verified_operator_audit"):
            lines.append(f"  B2 attributed outside store  : "
                         f"{pv['B2_verified_operator_audit']}"
                         "  (history erased; audit+usage evidence)")
        lines.append(f"  C unverified provenance      : {pv['C_unverified']}")
        if pv.get("C2_system_generated"):
            lines.append(f"  C2 system-generated rows     : {pv['C2_system_generated']}")
        if pv.get("C3_system_transitions"):
            lines.append(f"  C3 system transitions        : "
                         f"{pv['C3_system_transitions']}"
                         "  (retry/delegation/task-agent - not operator work)")
        lines.append(f"  E duplicate firings suppressed: "
                     f"{pv['E_legitimate_duplicates_suppressed']}")
        lines.append(f"  F distinct turns suppressed  : "
                     f"{pv['F_distinct_turns_suppressed']}")
    if liveness["failures"]:
        lines.append("")
        lines.append("FAILURES")
        for f in liveness["failures"]:
            lines.append(f"  - {f}")
    if liveness["warnings"]:
        lines.append("")
        lines.append("CAVEATS")
        for w in liveness["warnings"]:
            lines.append(f"  - {w}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--epoch",
        default=None,
        help=(
            "epoch directory to report. Default: the epoch with the latest "
            "authoritative boundary marker, so a scheduled run follows the "
            "live campaign instead of a hardcoded one. Pass an older epoch "
            "explicitly to report history."
        ),
    )
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--capability",
        choices=("auto", "yes", "no"),
        default="auto",
        help=(
            "whether the epoch's code observes attachment-delivered "
            "instructions, which decides if those turns are eligible work. "
            "'auto' (default) uses this epoch's own CAPABILITY record, then its "
            "own content_basis rows, then - for the CURRENT epoch only - the "
            "code the live services are executing. 'yes'/'no' are explicit "
            "overrides and are reported as such."
        ),
    )
    args = ap.parse_args()

    # Selection and boundary resolution both come from collect.py: one
    # implementation, so the two reports can never disagree about which epoch
    # they are describing or where its window starts.
    collector = load_collector()
    try:
        epoch, selection, skipped = collector.resolve_epoch(args.epoch)
    except LookupError as error:
        print(str(error), file=sys.stderr)
        return 2
    # Anything written to stderr is APPENDED to stdout by the watch runner, so a
    # note here would make the JSON unparsable and fire the watch with a
    # condition error. Machine output stays clean; notes ride inside it.
    if not args.json:
        for line in skipped:
            print(f"skipping epoch: {line}", file=sys.stderr)

    if not epoch.is_dir():
        print(f"epoch directory not found: {epoch}", file=sys.stderr)
        return 2

    # One resolver for every epoch: the marker's NAME follows the epoch number,
    # its MEANING does not. A reader that hardcodes a filename is a reader that
    # breaks the moment an epoch is named differently -- which is exactly what
    # nearly shipped for s6-t2.
    boundary_dt, marker_name, marker_problem = collector.read_boundary(epoch)
    if marker_problem:
        print(f"epoch boundary is corrupt in {epoch}: {marker_problem}",
              file=sys.stderr)
        return 2
    if boundary_dt is None:
        print(f"no boundary marker in {epoch} "
              f"(looked for {'/'.join(collector.BOUNDARY_MARKERS)}): "
              "no acceptance window yet", file=sys.stderr)
        return 2
    t1_text = boundary_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    # An epoch is SUPERSEDED, not extended. Without an upper bound a historical
    # epoch absorbs every later turn and a sensor that covered its whole window
    # reports as broken.
    _start, window_end = collector.epoch_window(epoch)
    until_text = window_end.strftime("%Y-%m-%dT%H:%M:%SZ") if window_end else None
    elapsed_h = (datetime.now(timezone.utc) - boundary_dt).total_seconds() / 3600

    try:
        turns = fetch_turns(args.dsn, t1_text, until_text)
    except Exception as error:  # noqa: BLE001 - report, never crash a monitor
        print(f"cannot read the conversation store: {type(error).__name__}: {error}",
              file=sys.stderr)
        return 2

    # Provenance FIRST: the collector builds the acceptance gate, so the
    # reconciliation has to exist before the collector runs. Without it the
    # collector's gate FAILS CLOSED - its count is zero, `met` is false, and
    # the raw observation count stays informational - so this script can never
    # publish a raw count as the gate the scheduled watch reads.
    #
    # Provenance compares store turns with the observations that can be
    # ATTRIBUTED to them. Raw counts cancel two different errors against each
    # other - an event with no stored turn and a stored turn with no event -
    # and report perfect coverage. The store-matched count is the honest
    # numerator for a store-derived denominator; anything attributable only
    # outside the store is reported beside it, never folded in.
    provenance = None
    try:
        spec = importlib.util.spec_from_file_location("s6prov", HERE / "provenance.py")
        prov = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prov)
        events, _boundary, _wend = prov.collect_events(epoch)
        dsn = args.dsn
        provenance = prov.reconcile(events=events,
                                    store=prov.fetch_store(dsn),
                                    audit=prov.fetch_audit(dsn))
    except Exception as error:  # noqa: BLE001 - a monitor must never crash
        print(f"provenance unavailable ({type(error).__name__})", file=sys.stderr)

    (observed, admitted_operator, basis_counts,
     explicit_basis, result) = count_operator_observations(epoch, provenance)

    observed_for_coverage = (
        provenance["B_verified_operator"] if provenance else observed
    )
    attachments_observed, capability_source = detect_attachment_capability(
        epoch, explicit_basis=explicit_basis, override=args.capability,
        current_epoch=collector.resolve_epoch()[0],
    )
    liveness = assess(
        acceptance_operator_observations=observed_for_coverage,
        acceptance_operator_admitted=admitted_operator,
        turns=turns,
        elapsed_h=elapsed_h,
        attachments_observed=attachments_observed,
        basis_counts=basis_counts,
        raw_operator_observations=observed,
    )
    liveness["capability_source"] = capability_source
    liveness["epoch_selection"] = selection
    liveness["epochs_skipped"] = skipped
    liveness["raw_operator_observations"] = observed
    if provenance is not None:
        liveness["provenance"] = {k: v for k, v in provenance.items() if k != "ledger"}
        suppressed_distinct = provenance["F_distinct_turns_suppressed"]
        if suppressed_distinct:
            liveness["warnings"].append(
                f"{suppressed_distinct} distinct operator turn(s) were discarded by "
                "state-dedupe (a different submission collided with an earlier "
                "turn's descriptor): coverage is understated by that many and "
                "the event is missing from the acceptance count"
            )
            if liveness["verdict"] == "HEALTHY":
                liveness["verdict"] = "HEALTHY_WITH_CAVEATS"
    if args.json:
        print(json.dumps({"epoch": str(epoch), "boundary": t1_text,
                          "boundary_marker": marker_name,
                          "window_end": until_text, **liveness,
                          "acceptance": result["gate"]}, indent=1))
    else:
        print(render(result, liveness, epoch))
    return 1 if liveness["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
