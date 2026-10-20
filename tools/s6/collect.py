#!/usr/bin/env python3
"""S6 cluster measurement collector — READ ONLY.

Reads ONE epoch's cadence + observation JSONL files and reports progress toward
the acceptance gate. It never writes telemetry, never calls the decision
backend, and never touches a service.

ACCEPTANCE BOUNDARY (2026-10-18)
--------------------------------
The rolling upgrade to cb7fece restarts six services one at a time, so
observations start landing in the s6-t1 directories BEFORE the epoch is
authoritative. Those rows are ROLLOUT-VALIDATION data: useful to prove the
sensor works, but not evidence about steady-state operator behaviour.

T1 is written only after all six services are verified on cb7fece. This
collector therefore:

  * splits every row into acceptance (timestamp >= T1) and rollout (< T1);
  * pairs each admitted cadence row with its observation by event_id and
    classifies the PAIR by max(timestamps), so a row straddling T1 cannot
    appear as an orphan on the other side;
  * treats a row with no parseable timestamp as NOT acceptance (fail closed);
  * counts nothing as acceptance while T1 is absent (fail closed);
  * never reads s6-t0, and refuses to present a historical epoch as
    acceptance data (s6-t0 is preserved as INCOMPLETE / UNDERCOUNTED).

Usage:
    /opt/turnstone-venv/bin/python s6/collect.py
    ... collect.py --json
    ... collect.py --epoch /var/lib/turnstone/sensor/s6-t1

Runs as the `turnstone` user: the epoch tree is 0750 turnstone:turnstone.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

#: Where the epochs live. Selection is made per-call by
#: :func:`resolve_epoch`, never by a literal directory name: a monitor that
#: hardcodes an epoch name keeps reporting a finished campaign after the
#: cluster has moved on, which is exactly what the scheduled watch did when
#: s6-t2 went live.
EPOCH_ROOT = pathlib.Path("/var/lib/turnstone/sensor")
#: Names that look like epochs. The marker file decides whether one IS an
#: epoch; this only narrows the scan.
EPOCH_GLOB = "s6-t*"
#: Deprecated and deliberately wrong-looking: retained only so an external
#: caller that imported the old name keeps working. NEVER use it to choose an
#: epoch -- resolve_epoch() is the only selection mechanism.
DEFAULT_EPOCH = EPOCH_ROOT / "s6-t1"
# Epochs kept only as historical evidence. Their counts are real but
# UNDERCOUNTED (the pre-cb7fece sensor missed every mid-turn interjection) and
# must never be merged into, or presented as, acceptance data.
HISTORICAL_EPOCHS = {"s6-t0"}
SERVICE_DIRS = [
    "turnstone-node-5",
    "turnstone-node-4",
    "turnstone-node-3",
    "turnstone-node-2",
    "console",
    "turnstone-head-1",
]
GATE_OBSERVATIONS = 100
GATE_HOURS = 48
#: The five signals the decision lane is qualified for. `local_suitability` is
#: deliberately excluded: the lane is not qualified for it, so requiring it
#: would make the gate unsatisfiable by design.
GRADED_SIGNALS = (
    "task_complexity",
    "reasoning_demand",
    "tool_dependency",
    "agentic_complexity",
    "route_sufficiency",
)


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def read_marker(path: pathlib.Path) -> datetime | None:
    """Parse an epoch marker; an unreadable marker is treated as absent."""
    if not path.is_file():
        return None
    try:
        return datetime.strptime(
            path.read_text().strip(), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except (ValueError, OSError):
        return None


#: Filenames an epoch may use for its acceptance boundary, in precedence order.
#:
#: The NAME follows the epoch numbering, the MEANING does not change: one UTC
#: instant after which an admission counts as acceptance work for that epoch.
#:   s6-t0, s6-t1 -> `T1`
#:   s6-t2        -> `T2`
#:
#: Both are read through :func:`read_boundary`, so a reader never has to know
#: which epoch it is looking at, and a writer never has to guess a name. The
#: activation script writes the epoch's OWN name; the readers accept either.
BOUNDARY_MARKERS = ("T2", "T1")


def read_boundary(
    epoch: pathlib.Path,
) -> tuple[datetime | None, str, str]:
    """Resolve one epoch's acceptance boundary.

    Returns ``(instant, marker_name, problem)``.

    * ``(None, "", "")``          no marker: the epoch has no window yet.
    * ``(dt, "T2", "")``          normal.
    * ``(None, "", reason)``       ambiguous: both markers exist and disagree.
      Fail closed. Two different acceptance instants for one epoch cannot both
      be authoritative, and silently preferring one would move the boundary
      under an operator who is reading the other file.
    """
    found: dict[str, datetime] = {}
    for name in BOUNDARY_MARKERS:
        path = epoch / name
        if not path.is_file():
            continue
        instant = read_marker(path)
        if instant is None:
            return None, "", f"{name} exists but is not a valid UTC timestamp"
        found[name] = instant
    if not found:
        return None, "", ""
    if len(set(found.values())) > 1:
        detail = ", ".join(
            f"{name}={instant.isoformat()}" for name, instant in sorted(found.items())
        )
        return None, "", f"conflicting boundary markers: {detail}"
    name = next(iter(found))
    return found[name], name, ""


def epoch_window(epoch: pathlib.Path) -> tuple[datetime | None, datetime | None]:
    """The half-open acceptance window ``[start, end)`` for one epoch.

    An epoch is superseded, not extended: when ``s6-t2`` opened, ``s6-t1``
    stopped being current, and its acceptance window closed at T2. Reporting an
    historical epoch without an upper bound silently absorbs every later turn,
    which makes a epoch that covered its whole window look like a sensor that
    had stopped working (s6-t1 read 3 of 8 instead of 3 of 3).

    ``end`` is the earliest later epoch's boundary, or ``None`` for the current
    epoch (whose window is still open).
    """
    start, _marker, _problem = read_boundary(epoch)
    later: list[datetime] = []
    for candidate in sorted(EPOCH_ROOT.glob(EPOCH_GLOB)):
        if not candidate.is_dir() or candidate.resolve() == epoch.resolve():
            continue
        instant, _m, _p = read_boundary(candidate)
        if instant is not None and start is not None and instant > start:
            later.append(instant)
    return start, (min(later) if later else None)


def resolve_epoch(
    explicit: str | pathlib.Path | None = None,
    root: str | pathlib.Path | None = None,
) -> tuple[pathlib.Path, str, list[str]]:
    """Which epoch a report should describe, and why.

    Selection is by the epoch's OWN authoritative boundary, not by directory
    name, directory mtime, and above all not by which overlay the services are
    running. Moving the cluster forward must not silently move a report: when
    ``s6-t2`` went live, every default-epoch report kept describing ``s6-t1``
    and said so only in a line nobody reads.

    Returns ``(epoch, reason, skipped)``:

    * an explicit ``--epoch`` always wins (``reason == "explicit"``);
    * otherwise the epoch whose boundary instant is LATEST;
    * a directory with no boundary is not yet an epoch and is skipped;
    * a directory whose markers disagree is corrupt and is skipped rather than
      folded into a plausible answer.

    ``skipped`` is returned so the caller can say what it ignored instead of
    presenting a confident number derived from an unknown set.
    """
    if explicit not in (None, ""):
        return pathlib.Path(explicit), "explicit", []

    root = pathlib.Path(root) if root is not None else EPOCH_ROOT
    candidates: list[tuple[datetime, str, pathlib.Path]] = []
    skipped: list[str] = []
    for candidate in sorted(root.glob(EPOCH_GLOB)):
        if not candidate.is_dir():
            continue
        instant, marker, problem = read_boundary(candidate)
        if problem:
            skipped.append(f"{candidate.name}: {problem}")
            continue
        if instant is None:
            if candidate.name in HISTORICAL_EPOCHS:
                skipped.append(
                    f"{candidate.name}: historical epoch, kept as evidence and "
                    "never selected for current measurement"
                )
            else:
                skipped.append(
                    f"{candidate.name}: no boundary marker "
                    f"({'/'.join(BOUNDARY_MARKERS)}); not an epoch yet"
                )
            continue
        candidates.append((instant, marker, candidate))

    if not candidates:
        raise LookupError(
            f"no epoch with an authoritative boundary under {root} "
            f"(markers: {'/'.join(BOUNDARY_MARKERS)}); pass --epoch explicitly"
        )

    # (instant, marker, path) sorts by instant; the name breaks a tie so the
    # answer never depends on readdir order.
    instant, marker, chosen = max(candidates, key=lambda c: (c[0], c[2].name))
    reason = (
        f"newest boundary {marker}={instant.strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"among {len(candidates)} epoch(s) under {root}"
    )
    return chosen, reason, skipped


def row_time(row: dict) -> float | None:
    """Epoch seconds for one row: observations use `timestamp`, cadence `ts`."""
    for key in ("timestamp", "ts"):
        value = row.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def split_service(
    cadence: list[dict],
    observations: list[dict],
    boundary: float | None,
    historical: bool,
) -> tuple[dict, dict, dict, list[dict]]:
    """Split one service's rows into (acceptance, rollout, straddle, orphans).

    ACCEPTANCE IS ANCHORED ON THE ADMISSION TIMESTAMP (2026-10-18). The
    acceptance unit is one admitted cadence row, and it counts only when that
    row's own timestamp is at or after T1:

      * acceptance  cadence >= T1. Its observation is carried with it.
      * straddle    cadence < T1 but the observation arrived >= T1. A real
                    observation produced by a pre-T1 admission. Reported
                    separately and NEVER counted: counting it would inflate the
                    100-observation threshold with work that was admitted before
                    the epoch existed.
      * rollout     cadence < T1 and observation < T1 (or no observation).
      * orphan      observation with no matching admitted cadence row. An
                    integrity failure, not a data point.

    Fail-closed rules:

      * no T1 (boundary is None) or a historical epoch -> the acceptance slice
        is empty, whatever the timestamps say;
      * a cadence row with no parseable timestamp is never acceptance;
      * an observation only ever inherits its admission's class, never the
        other way round.
    """
    admitted = [r for r in cadence if r.get("outcome") == "sensor_call"]
    suppressed = [r for r in cadence if r.get("outcome") == "dedupe_suppressed"]
    failed = [r for r in cadence if r.get("outcome") == "observation_write_failed"]
    hook_errors = [r for r in cadence if r.get("outcome") == "hook_error"]

    admitted_by_event = {r.get("event_id"): r for r in admitted if r.get("event_id")}
    obs_by_event = {r.get("event_id"): r for r in observations if r.get("event_id")}

    def _accepts(ts: float | None) -> bool:
        if historical or boundary is None or ts is None:
            return False
        return ts >= boundary

    def _blank() -> dict[str, list[dict]]:
        return {
            "admitted": [], "suppressed": [], "failed": [],
            "hook_error": [], "observations": [], "unmatched": [],
        }

    acc, rv, straddle = _blank(), _blank(), _blank()
    consumed: set[str] = set()

    for row in admitted:
        event_id = row.get("event_id")
        partner = obs_by_event.get(event_id)
        if event_id:
            consumed.add(event_id)
        if _accepts(row_time(row)):
            target = acc
        elif partner is not None and _accepts(row_time(partner)):
            # Pre-T1 admission, post-T1 observation: real work, wrong epoch.
            target = straddle
        else:
            target = rv
        target["admitted"].append(row)
        if partner is not None:
            target["observations"].append(partner)
        else:
            target["unmatched"].append(row)

    for rows, key in (
        (suppressed, "suppressed"),
        (failed, "failed"),
        (hook_errors, "hook_error"),
    ):
        for row in rows:
            (acc if _accepts(row_time(row)) else rv)[key].append(row)

    # Observations whose admission row is absent or was not an admitted call.
    orphans = [
        row for row in observations
        if row.get("event_id") not in consumed
    ]
    return acc, rv, straddle, orphans


def _fold_metrics(slices: dict[str, dict], latencies, tokens, signals_available,
                  signals_total, reasons, backends, lanes, workstreams,
                  content_modes, content_bases, event_ids,
                  event_services) -> None:
    """Accumulate the acceptance-slice metrics (the only ones that gate)."""
    for service, part in slices.items():
        for row in part["observations"]:
            if row.get("event_id"):
                event_ids[row["event_id"]] += 1
                event_services[row["event_id"]].add(service)
            if row.get("workstream"):
                workstreams.add(row["workstream"])
            if row.get("backend"):
                backends[row["backend"]] += 1
            extra = row.get("extra") or {}
            for key in ("child_lane", "served_model", "requested"):
                if extra.get(key):
                    lanes[f"{key}={extra[key]}"] += 1
            if row.get("content_mode"):
                content_modes[row["content_mode"]] += 1
            if row.get("content_basis"):
                content_bases[row["content_basis"]] += 1
            if isinstance(row.get("latency_ms"), (int, float)):
                latencies.append(float(row["latency_ms"]))
            if isinstance(row.get("serialized_state_tokens"), int):
                tokens.append(row["serialized_state_tokens"])
            for sig in row.get("signals", []):
                name = sig.get("name")
                if not name:
                    continue
                signals_total[name] += 1
                if sig.get("available"):
                    signals_available[name] += 1
                elif sig.get("reason"):
                    reasons[sig["reason"]] += 1


def collect(epoch: pathlib.Path, *, selection: str = "", provenance: dict | None = None) -> dict:
    historical = epoch.name in HISTORICAL_EPOCHS
    t1, marker_name, marker_problem = read_boundary(epoch)
    boundary = t1.timestamp() if t1 else None
    now = datetime.now(timezone.utc)
    elapsed_h = (now - t1).total_seconds() / 3600 if t1 else None

    per_service: dict[str, dict] = {}
    acceptance: dict[str, dict] = {}
    rollout: dict[str, dict] = {}
    straddle: dict[str, dict] = {}
    orphan_rows: list[dict] = []

    latencies: list[float] = []
    token_counts: list[int] = []
    signals_available: Counter = Counter()
    signals_total: Counter = Counter()
    reasons: Counter = Counter()
    backends: Counter = Counter()
    lanes: Counter = Counter()
    workstreams: set[str] = set()
    content_modes: Counter = Counter()
    content_bases: Counter = Counter()
    graded_full = 0          # observations whose five signals are all available
    graded_structural = 0    # observations grounded only in structural metadata
    graded_attachment_context = 0   # graded subset whose turn also carried attachments
    gradeable = 0            # dimension B denominator: content was available
    gradeable_ungraded = 0   # ...of which the five signals did not all arrive
    event_ids: Counter = Counter()
    event_services: dict[str, set] = defaultdict(set)
    fingerprint_procs: dict[tuple, set] = defaultdict(set)

    for service in SERVICE_DIRS:
        sdir = epoch / service
        cadence = read_jsonl(sdir / "shadow-cadence.jsonl")
        observations = read_jsonl(sdir / "shadow-observations.jsonl")
        acc, rv, strad, orphans = split_service(
            cadence, observations, boundary, historical
        )
        acceptance[service] = acc
        rollout[service] = rv
        straddle[service] = strad
        orphan_rows.extend(orphans)
        per_service[service] = {
            "acceptance_observations": len(acc["observations"]),
            "acceptance_admitted": len(acc["admitted"]),
            "rollout_observations": len(rv["observations"]),
            "rollout_admitted": len(rv["admitted"]),
            "straddle_observations": len(strad["observations"]),
            "straddle_admitted": len(strad["admitted"]),
            "orphan_observations": len(orphans),
            "suppressed": len(acc["suppressed"]),
            "write_failed": len(acc["failed"]),
            "hook_error": len(acc["hook_error"]),
            "unmatched_admitted": len(acc["unmatched"]),
            "workstreams": len({r.get("workstream") for r in acc["observations"]
                                if r.get("workstream")}),
        }
        for row in acc["observations"]:
            fp, ts = row.get("state_fingerprint"), row_time(row)
            if fp and ts is not None:
                fingerprint_procs[(fp, int(ts // 60))].add(row.get("process_id") or "?")

    _fold_metrics(
        acceptance, latencies, token_counts, signals_available, signals_total,
        reasons, backends, lanes, workstreams, content_modes, content_bases,
        event_ids, event_services,
    )

    # ---- the two coverage dimensions, computed from the same acceptance slice
    # A: OPERATOR-WORK EVENT COVERAGE. Denominator from liveness.py against the
    #    conversation store; here we report the numerator and the basis mix.
    #    STRUCTURAL-ONLY EVENTS COUNT HERE: they are evidence that work was
    #    observed, which is exactly what dimension A measures.
    #
    # B: QUALIFIED SEMANTIC GRADING COVERAGE, restricted to events whose task
    #    content was actually available to the grader:
    #      denominator  basis in {text, text_plus_attachments}
    #      numerator    those with all five graded signals available
    #    `attachments_only` is EXCLUDED from B's denominator, not counted as a
    #    failure: the grader never received the document, so it cannot be said
    #    to have graded it badly. Counting it either way would be a category
    #    error. It is reported on its own line.
    #
    # GRADING IS REFINED THREE WAYS, not two (2026-10-18):
    #   text                  text instruction, no attachment
    #   text_plus_attachments text instruction + attachments. The TEXT was
    #                         graded; the attachment's contents were not, so
    #                         this is never complete coverage of an
    #                         attachment-heavy task. Reported separately.
    #   attachments_only      the attachment WAS the instruction; the backend
    #                         graded a structural descriptor.
    for service, part in acceptance.items():
        for row in part["observations"]:
            available = {
                sig.get("name") for sig in row.get("signals", []) if sig.get("available")
            }
            complete = len(available & set(GRADED_SIGNALS)) == len(GRADED_SIGNALS)
            basis = row.get("content_basis") or "text"
            if basis == "attachments_only":
                # Dimension A only. Never in B's denominator.
                if complete:
                    graded_structural += 1
                continue
            # Dimension B denominator: content was available to the grader.
            gradeable += 1
            if not complete:
                gradeable_ungraded += 1
                continue
            graded_full += 1
            if basis == "text_plus_attachments":
                graded_attachment_context += 1
    qualified_signals = graded_full

    unique_observations = len([i for i in event_ids if i])
    duplicate_event_ids = [k for k, v in event_ids.items() if v > 1]
    cross_service_ids = [k for k, v in event_services.items() if len(v) > 1]
    buckets_over_one = sum(1 for v in fingerprint_procs.values() if len(v) > 1)

    # ONE derivation of the gate's numerator. The gate, the rate and the ETA
    # must all count the SAME events: attributable operator events, not raw
    # observation rows. A raw row that cannot be attributed to an operator turn
    # can never certify the campaign, so it cannot count toward the forecast
    # of how long the campaign will take either. (This is the same defect as
    # the third reporting defect, in the two surfaces that fix did not reach:
    # the rate/ETA and the human-readable labels.)
    #
    # An EMPTY reconciliation carries no bucket counts, so it cannot support an
    # attributable count. It is treated as NO reconciliation - the same
    # fallback, and the same explicit "raw basis" marking - so the basis
    # label and `provenance_available` can never disagree.
    has_provenance = bool(provenance)
    if has_provenance:
        attributable_observations = (
            provenance.get("B_verified_operator", 0)
            + provenance.get("B2_verified_operator_audit", 0)
        )
        rate_basis = "attributable"
    else:
        # No reconciliation: nothing is VERIFIED, so the count that can
        # certify the campaign is ZERO. The raw count stays available for
        # INFORMATION - "how many rows exist at all" is worth knowing - but
        # it is never the gate's count.
        #
        # The rate and the ETA are computed over the same count as the gate,
        # so with no reconciliation they are unavailable too. That is
        # deliberate: a forecast built on rows nobody has attributed to an
        # operator turn is a forecast of unverified work, and publishing it
        # next to a real forecast invites the reader to treat them alike.
        attributable_observations = 0
        rate_basis = "raw"

    admitted_total = sum(v["acceptance_admitted"] for v in per_service.values())
    suppressed_total = sum(v["suppressed"] for v in per_service.values())
    missing_observation = sum(v["unmatched_admitted"] for v in per_service.values())
    write_failures = sum(v["write_failed"] for v in per_service.values())
    rollout_observations = sum(v["rollout_observations"] for v in per_service.values())
    rollout_admitted = sum(v["rollout_admitted"] for v in per_service.values())
    straddle_observations = sum(v["straddle_observations"] for v in per_service.values())
    straddle_admitted = sum(v["straddle_admitted"] for v in per_service.values())
    orphan_total = len(orphan_rows)

    # A rate needs BOTH a usable window and a usable sample. 1 observation in
    # 7 minutes extrapolates to "11.6 h to 100", which is arithmetic, not a
    # forecast; the campaign is bursty and follows operator activity.
    MIN_RATE_SAMPLE = 5
    MIN_RATE_HOURS = 0.5
    rateable = bool(
        elapsed_h is not None
        and elapsed_h >= MIN_RATE_HOURS
        and attributable_observations >= MIN_RATE_SAMPLE
    )
    obs_per_hour = ((attributable_observations / elapsed_h) if rateable else None)
    remaining = max(0, GATE_OBSERVATIONS - attributable_observations)
    eta_hours = (remaining / obs_per_hour) if obs_per_hour and obs_per_hour > 0 else None

    return {
        "epoch": str(epoch),
        # How this epoch was chosen. Empty when the caller passed --epoch and
        # the choice was not ours to make.
        "epoch_selection": selection,
        "historical": historical,
        # The boundary is the epoch's own acceptance instant. `boundary_marker`
        # names the file it came from (T2 for s6-t2, T1 for earlier epochs) so a
        # reader can point at the exact evidence instead of guessing.
        "boundary_marker": marker_name or None,
        "boundary_problem": marker_problem or None,
        "t1": t1.isoformat().replace("+00:00", "Z") if t1 else None,
        "now": now.isoformat().replace("+00:00", "Z"),
        "elapsed_hours": round(elapsed_h, 2) if elapsed_h is not None else None,
        "per_service": per_service,
        "rollout_validation": {
            "observations": rollout_observations,
            "admitted": rollout_admitted,
            "excluded": True,
        },
        # Pre-T1 admission whose observation landed at/after T1. Real work, but
        # admitted before the epoch existed: reported and never counted.
        "straddling_admissions": {
            "admitted": straddle_admitted,
            "observations": straddle_observations,
            "excluded": True,
        },
        "orphan_observations": orphan_total,
        "unique_observations": unique_observations,
        "admitted_rows": admitted_total,
        "distinct_workstreams": len(workstreams),
        "admission_rate": (
            round(admitted_total / (admitted_total + suppressed_total), 3)
            if (admitted_total + suppressed_total) else None
        ),
        "suppressed_total": suppressed_total,
        "write_failures": write_failures,
        "unmatched_admitted": missing_observation,
        "duplicate_event_ids": len(set(duplicate_event_ids)),
        "event_ids_in_multiple_services": len(cross_service_ids),
        "possible_cross_node_duplicates": buckets_over_one,
        "content_bases": dict(content_bases),
        "coverage": {
            "admitted_observations": unique_observations,
            "graded_full": graded_full,
            "graded_structural_only": graded_structural,
            # Of the fully graded observations, the ones whose turn ALSO carried
            # an attachment. The instruction was graded; the attachment's
            # contents were not in the graded representation. A text-only grade
            # on an attachment-heavy task is NOT complete semantic coverage.
            "graded_text_with_attachment_context": graded_attachment_context,
            # Fraction of admitted observations that are a real five-signal
            # measurement. A structural-only record is an EVENT, not a graded
            # measurement, and must never be counted as one.
            # DIMENSION B. Denominator excludes attachments_only by design:
            # the grader never received those documents, so they are neither a
            # success nor a failure of semantic grading.
            "semantic_gradeable_events": gradeable,
            "semantic_graded_events": graded_full,
            "semantic_ungraded_events": gradeable_ungraded,
            "qualified_semantic_rate": (
                round(graded_full / gradeable, 3) if gradeable else None
            ),
            # Kept for continuity with earlier reports; NOT the acceptance
            # metric, and never a rate to gate on.
            "qualified_signal_rate": (
                round(graded_full / unique_observations, 3)
                if unique_observations else None
            ),
            "attachment_context_rate": (
                round(graded_attachment_context / graded_full, 3)
                if graded_full else None
            ),
        },
        "backends": dict(backends),
        # The backend-lane counter. It lives under its OWN key: `provenance` is
        # reserved for the event reconciliation, and sharing the key let one
        # overwrite the other (a collector whose lanes were non-empty but whose
        # reconciliation was unavailable raised KeyError while rendering).
        "lanes": dict(lanes),
        # The event reconciliation, when the caller supplied one. It is
        # carried HERE rather than merged in by the caller, so the key can
        # never be claimed by two different dicts.
        "provenance": ({k: v for k, v in provenance.items() if k != "ledger"}
                       if provenance else None),
        "content_modes": dict(content_modes),
        "signal_availability": {
            name: {
                "available": signals_available.get(name, 0),
                "total": total,
                "rate": round(signals_available.get(name, 0) / total, 3) if total else None,
            }
            for name, total in sorted(signals_total.items())
        },
        "unavailable_reasons": dict(reasons),
        "latency_ms": {
            "n": len(latencies),
            "p50": round(statistics.median(latencies), 1) if latencies else None,
            "max": round(max(latencies), 1) if latencies else None,
        },
        "serialized_state_tokens": {
            "n": len(token_counts),
            "max": max(token_counts) if token_counts else None,
        },
        "gate": {
            # The gate counts INDEPENDENTLY ATTRIBUTABLE operator events, not
            # raw observation rows. When provenance reconciliation is
            # available the verified count is what `met` keys on, so an
            # unattributable row can never help certify the campaign.
            #
            # WITHOUT a reconciliation the gate FAILS CLOSED. The raw count
            # is still reported beside it, and is still useful information,
            # but it can never satisfy the 100-event requirement: a campaign
            # is certified by events VERIFIED against the conversation store,
            # and with no reconciliation nothing is verified. The count is
            # zero in that case, so `met` is false no matter how many raw
            # rows exist.
            "observations": {
                "have": attributable_observations,
                "need": GATE_OBSERVATIONS,
                "met": bool(has_provenance
                           and attributable_observations >= GATE_OBSERVATIONS),
            },
            "provenance_available": has_provenance,
            "raw_observations": unique_observations,
            "unverified": provenance.get("C_unverified", 0) if has_provenance else None,
            "distinct_turns_suppressed": (
                provenance.get("F_distinct_turns_suppressed", 0)
                if has_provenance else None
            ),
            "elapsed": {"have_hours": round(elapsed_h, 2) if elapsed_h is not None else None,
                        "need_hours": GATE_HOURS,
                        "met": bool(elapsed_h is not None and elapsed_h >= GATE_HOURS),
                        # The count is only half the gate. The time floor is
                        # reported with what is LEFT of it, so a reader can
                        # see which of the two conditions is the binding one.
                        "hours_remaining": (
                            round(max(0.0, GATE_HOURS - elapsed_h), 2)
                            if elapsed_h is not None else None
                        )},
            "unmatched_ok": missing_observation == 0,
            "no_duplicates_ok": not duplicate_event_ids,
            "no_orphans_ok": orphan_total == 0,
            # A corrupt or ambiguous boundary is its own integrity failure: the
            # acceptance slice is empty by construction, so every other gate
            # would read trivially satisfied.
            "boundary_ok": not marker_problem,
        },
        # The rate's BASIS, and the count it was computed over. Published so a
        # reader can tell an attributable rate from a raw one. The raw count
        # is kept beside it, never in place of it.
        "rate_basis": rate_basis,
        "rate_events": attributable_observations,
        "observed_per_hour": round(obs_per_hour, 2) if obs_per_hour is not None else None,
        "eta_hours_to_100": round(eta_hours, 1) if eta_hours is not None else None,
        "rate_note": (
            "no provenance reconciliation: nothing is verified, so there is "
            "no attributable rate and no ETA. The raw count is reported "
            "separately as information"
            if not has_provenance else (
                None if rateable else (
                    f"needs >= {MIN_RATE_SAMPLE} acceptance observations and >= "
                    f"{MIN_RATE_HOURS}h after the epoch boundary (have "
                    f"{attributable_observations} in "
                    f"{(round(elapsed_h, 2) if elapsed_h is not None else 0)}h)"
                )
            )
        ),
        "note": (
            "Only observations at or after this epoch's boundary count toward "
            "acceptance; the boundary marker is named in `boundary_marker` "
            "(T2 for s6-t2, T1 for earlier epochs). Rows "
            "written during the rolling upgrade are reported separately as "
            "rollout-validation and never merged in. Tokenizer subprocess "
            "latency is measured separately (evidence/tokenizer-concurrency.txt); "
            "the decision-call latency below is the observation latency_ms field."
        ),
    }


def render(result: dict) -> str:
    lines = []
    gate = result["gate"]
    lines.append("S6 CLUSTER MEASUREMENT REPORT (read-only)")
    lines.append("=" * 62)
    lines.append(f"epoch         : {result['epoch']}")
    if result.get("epoch_selection"):
        lines.append(f"selected by   : {result['epoch_selection']}")
    marker = result.get("boundary_marker") or "none"
    lines.append(f"boundary      : {result['t1'] or 'NOT SET'}"
                 f"   (acceptance boundary; marker {marker})")
    if result.get("boundary_problem"):
        lines.append(f"BOUNDARY FAULT: {result['boundary_problem']}"
                     "   -> acceptance slice is empty (fail closed)")
    lines.append(f"now (UTC)     : {result['now']}")
    lines.append(f"elapsed       : {result['elapsed_hours']} h / {GATE_HOURS} h")
    # The gate's count, not the raw row count. A raw row that cannot be
    # attributed to an operator turn cannot certify the campaign, so it is
    # never presented as progress toward 100. The raw count is reported
    # separately, and labelled as raw.
    if not gate.get("provenance_available"):
        lines.append("PROVENANCE UNAVAILABLE - the gate FAILS CLOSED.")
        lines.append("  No reconciliation means nothing is VERIFIED against the")
        lines.append("  conversation store, so the attributable count is 0 and")
        lines.append("  acceptance CANNOT be met, whatever the raw row count is.")
        lines.append(f"  rate basis    : {result['rate_basis']}"
                     "  (raw rows are informational; no attributable rate,"
                     " no ETA)")
    lines.append(f"attributable  : {gate['observations']['have']} / {GATE_OBSERVATIONS}"
                 "  (events that count toward the gate)")
    lines.append(f"raw obs rows  : {result['unique_observations']}"
                 "  (informational only; not all attributable to a stored turn)")
    lines.append(f"admitted rows : {result['admitted_rows']}  (suppressed"
                 f" {result['suppressed_total']}, admission rate {result['admission_rate']})")
    lines.append(f"workstreams   : {result['distinct_workstreams']}")
    if result["observed_per_hour"] is None:
        lines.append(f"rate          : (not yet rateable: {result['rate_note']})")
    else:
        lines.append(f"rate          : {result['observed_per_hour']} events/h"
                     f"  ({result['rate_basis']} basis,"
                     f" {result['rate_events']} events)"
                     f"  ETA {result['eta_hours_to_100']} h to 100")
    hours_left = gate["elapsed"].get("hours_remaining")
    if hours_left is not None:
        lines.append(f"time floor    : {hours_left} h of the {GATE_HOURS} h"
                     " minimum still to run")
    rv = result["rollout_validation"]
    st = result["straddling_admissions"]
    lines.append(f"rollout-only  : {rv['observations']} observations /"
                 f" {rv['admitted']} admitted rows  (EXCLUDED from acceptance)")
    pre = marker if marker != "none" else "the boundary"
    lines.append(f"pre-{pre:<10s}: {st['admitted']} admitted rows whose observation"
                 f" arrived at/after {pre}  (EXCLUDED, reported separately)")
    lines.append(f"orphan obs    : {result['orphan_observations']}"
                 "  (observation with no admitted cadence row)")
    lines.append("")
    lines.append("PER SERVICE (acceptance / rollout-validation)")
    lines.append(f"  {'service':18s} {'acc_obs':>7s} {'acc_adm':>7s} {'rv_obs':>7s}"
                 f" {'rv_adm':>6s} {'strad':>5s} {'orph':>4s} {'ws':>3s} {'unmatch':>8s}")
    for name, s in result["per_service"].items():
        lines.append(f"  {name:18s} {s['acceptance_observations']:7d}"
                     f" {s['acceptance_admitted']:7d} {s['rollout_observations']:7d}"
                     f" {s['rollout_admitted']:6d} {s['straddle_observations']:5d}"
                     f" {s['orphan_observations']:4d} {s['workstreams']:3d}"
                     f" {s['unmatched_admitted']:8d}")
    lines.append("")
    lines.append("INTEGRITY (acceptance slice only)")
    lines.append(f"  unmatched admitted        : {result['unmatched_admitted']}")
    lines.append(f"  duplicate event ids       : {result['duplicate_event_ids']}")
    lines.append(f"  ids seen in >1 service    : {result['event_ids_in_multiple_services']}")
    lines.append(f"  possible cross-node dups  : {result['possible_cross_node_duplicates']}")
    lines.append(f"  storage write failures    : {result['write_failures']}")
    lines.append("")
    lines.append("PROVENANCE")
    lines.append(f"  backends : {result['backends']}")
    lines.append(f"  lanes    : {result['lanes']}")
    lines.append(f"  modes    : {result['content_modes']}")
    cov = result["coverage"]
    lines.append(f"  basis    : {result['content_bases']}")
    lines.append("")
    lines.append("COVERAGE (two independent dimensions)")
    # The raw admitted count is a DIMENSION-A figure. It is not the gate's
    # count: the gate keys on attributable operator events (B + B2), which
    # this report states separately, at the top. Labelling this line "these
    # gate" would reintroduce the exact defect this report was corrected for
    # - two contradictory statements about the same number.
    lines.append(f"  admitted observations        : {cov['admitted_observations']}"
                 "  (dimension A events; NOT the gate count)")
    lines.append(f"  five-signal graded           : {cov['graded_full']}")
    lines.append(f"  structural-only -- an EVENT  : {cov['graded_structural_only']}"
                 "  (counts in A, excluded from B)")
    lines.append(f"  graded w/ attachment context : "
                 f"{cov['graded_text_with_attachment_context']}"
                 "  (instruction graded; attachment contents were NOT)")
    lines.append("")
    lines.append("  DIMENSION B - qualified semantic grading (content available to grader)")
    lines.append(f"  gradeable events             : {cov['semantic_gradeable_events']}"
                 "  (basis text / text_plus_attachments)")
    lines.append(f"  graded in full               : {cov['semantic_graded_events']}")
    lines.append(f"  gradeable but ungraded       : {cov['semantic_ungraded_events']}")
    lines.append(f"  qualified semantic rate      : {cov['qualified_semantic_rate']}")
    lines.append(f"  attachments_only (not in B)  : {cov['graded_structural_only']}"
                 "  - grader never saw the document")
    lines.append(f"  qualified signal rate (all)  : {cov['qualified_signal_rate']}"
                 "  (informational)")
    lines.append("  operator-work event coverage : see liveness.py (vs the")
    lines.append("                                 conversation store, incl. attachments)")
    lines.append("")
    lines.append("SIGNAL COVERAGE")
    for name, s in result["signal_availability"].items():
        lines.append(f"  {name:22s} {s['available']:5d}/{s['total']:<5d} rate={s['rate']}")
    lines.append(f"  unavailable reasons: {result['unavailable_reasons']}")
    lines.append("")
    lines.append("LATENCY")
    lines.append(f"  decision ms : {result['latency_ms']}")
    lines.append(f"  state tokens: {result['serialized_state_tokens']}")
    lines.append("")
    pv = result.get("provenance")
    if pv and "A_raw_observed" in pv:
        lines.append("PROVENANCE RECONCILIATION (separate, never summed)")
        lines.append(f"  A raw admitted + observed    : {pv['A_raw_observed']}")
        lines.append(f"  B verified operator (in store): {pv['B_verified_operator']}")
        if pv.get("B2_verified_operator_audit"):
            lines.append(f"  B2 attributed outside store  : "
                         f"{pv['B2_verified_operator_audit']}"
                         "  (history erased; audit+usage evidence)")
        lines.append(f"  B+B2 total attributable      : "
                     f"{pv['B_verified_operator'] + pv.get('B2_verified_operator_audit', 0)}")
        lines.append(f"  C unverified provenance      : {pv['C_unverified']}")
        if pv.get("C2_system_generated"):
            lines.append(f"      system-generated        : {pv['C2_system_generated']}")
        if pv.get("C3_system_transitions"):
            lines.append(f"  C3 system transitions        : "
                         f"{pv['C3_system_transitions']}"
                         "  (retry/delegation/task-agent - not operator work)")
        lines.append(f"  E duplicates suppressed      : "
                     f"{pv['E_legitimate_duplicates_suppressed']}")
        lines.append(f"  F distinct turns suppressed  : "
                     f"{pv['F_distinct_turns_suppressed']}")
        lines.append("")
    lines.append("GATE")
    lines.append(f"  verified events >= 100 : "
                 f"{'MET' if gate['observations']['met'] else 'PENDING'}"
                 f"  ({gate['observations']['have']} attributable"
                 + (f"; raw {gate['raw_observations']}, unverified "
                    f"{gate['unverified']}" if gate.get('provenance_available') else "")
                 + ")")
    if not gate.get("provenance_available"):
        lines.append("  provenance available   : NO - the gate fails closed, so")
        lines.append("                           the raw count cannot certify it")
    lines.append(f"  elapsed >= 48h         : "
                 f"{'MET' if gate['elapsed']['met'] else 'PENDING'}")
    lines.append(f"  zero unmatched         : {'MET' if gate['unmatched_ok'] else 'FAILED'}")
    lines.append(f"  zero duplicates        : {'MET' if gate['no_duplicates_ok'] else 'FAILED'}")
    lines.append(f"  zero orphans           : {'MET' if gate['no_orphans_ok'] else 'FAILED'}")
    total = (gate["observations"]["met"] and gate["elapsed"]["met"]
             and gate["unmatched_ok"] and gate["no_duplicates_ok"]
             and gate["no_orphans_ok"])
    lines.append("")
    if result["historical"]:
        status = ("HISTORICAL EPOCH - INCOMPLETE / UNDERCOUNTED; preserved as "
                  "evidence, never acceptance data")
    elif total:
        status = "ACCEPTED"
    elif result["t1"] is None:
        status = ("ROLLOUT IN PROGRESS - no boundary marker set; no acceptance "
                  "data. Rows present are rollout-validation only")
    else:
        status = "MEASUREMENT ACTIVE - gate not yet satisfied"
    lines.append(f"STATUS: {status}")
    lines.append("")
    lines.append(result["note"])
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--epoch",
        default=None,
        help=(
            "epoch directory to report. Default: the epoch with the latest "
            "authoritative boundary marker under "
            f"{EPOCH_ROOT} -- so a scheduled run follows the live campaign "
            "instead of a hardcoded one."
        ),
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        epoch, selection, skipped = resolve_epoch(args.epoch)
    except LookupError as error:
        print(str(error), file=sys.stderr)
        return 2
    if not epoch.is_dir():
        print(f"epoch directory not found: {epoch}", file=sys.stderr)
        return 2
    # stderr is appended to stdout by the watch runner; in --json mode that
    # would corrupt the JSON. Notes ride in the payload instead.
    if not args.json:
        for line in skipped:
            print(f"skipping epoch: {line}", file=sys.stderr)
    # Provenance is computed when the store is reachable. It is the difference
    # between "13 rows exist" and "13 rows are attributable operator work", and
    # the gate must key on the latter. A failure here degrades to the raw count
    # and says so rather than failing the report.
    provenance = None
    try:
        spec = importlib.util.spec_from_file_location(
            "s6prov", pathlib.Path(__file__).resolve().parent / "provenance.py")
        prov = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prov)
        events, boundary, _wend = prov.collect_events(epoch)
        dsn = "dbname=turnstone"
        provenance = prov.reconcile(events=events,
                                    store=prov.fetch_store(dsn),
                                    audit=prov.fetch_audit(dsn))
    except Exception as error:  # noqa: BLE001 - a report must never crash
        print(f"provenance unavailable ({type(error).__name__}); gate uses the raw "
              "observation count", file=sys.stderr)

    result = collect(epoch, selection=selection, provenance=provenance)
    result["epochs_skipped"] = skipped
    print(json.dumps(result, indent=1) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
