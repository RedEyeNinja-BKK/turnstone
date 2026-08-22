#!/usr/bin/env python3
"""
Fleet Governance v1 — Deterministic BWP Validator + Adjudicator (design artifact).

PURPOSE
  Local, read-only validation of the Fleet Governance v1 design artifacts.
  NOT deployed; NOT part of any runtime. Stdlib only.

WHAT IT ENFORCES (design contract)
  1. Structural conformance to the four BWP schema objects (request/assignment/receipt/verdict).
  2. Sufficiency gate: only SUFFICIENT requests may dispatch (with a deterministic
     disposition for non-dispatchable work: CLARIFY / REJECT / DISPATCH).
  3. Identity rejection: provider/model/resource identities never appear as STRUCTURAL
     governance requirements (forbidden keys in requirement fields; capabilities must
     come from the semantic vocabulary).
  4. Vocab conformance: capabilities/actions/evidence types from the semantic vocabulary.
  5. Prose is NEVER authoritative routing metadata: prose mention of providers/models/
     resources is permitted and produces at most a warning/audit signal; structural work
     fields are REQUIRED and are the ONLY routing source. Missing structural metadata
     still fails hard.
  6. Executor selection: derive_executor_candidates()/select_executor() derive the
     executor from capabilities + authority + placement constraints ONLY. work_shape /
     reasoning_intent describe inference semantics for FleetRouter and NEVER select an
     executor (no mechanical work_shape->executor mapping).
  7. Hard vs preference: eligibility uses hard requirements only; preferences operate among
     eligible only and can never rescue an invalid option.
  8. Authority compliance: receipt actions_taken must be a subset of allowed_actions.
  9. Evidence sufficiency: PASS requires every BWP evidence requirement satisfied by a
     PROVEN claim (not PROPOSED/ASSUMED).
 10. Deterministic adjudication: verdict from acceptance + authority + evidence + escalation.

DEMONSTRATIONS (self-test; see VALIDATION.md)
  R1  Ambiguous/insufficient work must not dispatch (disposition CLARIFY/REJECT).
  R2  Provider/model/resource identity as STRUCTURAL governance requirement must be rejected.
  R3  Missing required structural work_shape must be rejected.
  R4  Preference must never override a hard constraint.
  R5  Executor broadening mutation authority must be flagged and FAIL the verdict.
  R6  Completion PASS without required evidence must be rejected (INDETERMINATE).
  R7  Prose mention of providers/models/resources must NOT invalidate a structurally valid packet.
  R8  Structural 'model'/'provider'/'resource' keys in requirements must be rejected.
  R9  Assignment must never carry provider/model identity.
  R10 Synonym-key identity smuggling (unknown properties) must be rejected (additionalProperties:false).
  R11 A blocking INDETERMINATE claim must prevent PASS (INDETERMINATE).
  R12 A PROVEN hosted inference locality under local_required must FAIL (VIOLATED).
  R13 local_required + unknown locality => INDETERMINATE (never inferred FAIL).
  R14 Provider/model names alone never prove locality => INDETERMINATE when locality unobserved.
  R15 One NOT_MET acceptance criterion => FAIL.
  R16 A required criterion without positive PROVEN adjudication => INDETERMINATE.
  R17 Absence of FAILED claims alone cannot make criteria MET.
  R18 Executor self-assessment cannot force PASS.
  R19 Unknown property nested under nullable resource_observed rejected.
  R20 Unknown property nested under inference_resource rejected.
  F1  inference_locality=local_required FAILS CLOSED before dispatch (UNSUPPORTED_V1).
  F2  inference_locality=hosted_allowed FAILS CLOSED before dispatch (UNSUPPORTED_V1).
  F3  context_size_requirement non-null FAILS CLOSED before dispatch (UNSUPPORTED_V1).
  F4  output_budget non-null FAILS CLOSED before dispatch (UNSUPPORTED_V1).
  C1  Executor selection must NOT follow work_shape (shape-independent).
  C2  Sufficiency disposition is deterministic (DISPATCH/CLARIFY/REJECT).
  C3  ESCALATED outcome is reachable (operator-gated) and does not evaluate hard constraints.
  C4  inference_locality does NOT select the executor (locality separation).
  C5  Executor assignment never carries/rewrites inference locality.
  C6  local_required + PROVEN local resource is compliant (PASS).
  C7  Local provider/model with PROVEN local locality is compliant (names never prove locality).
  C8  PASS requires all criteria MET + evidence sufficient.
  C9  Valid nullable resource_observed=null accepted.
  C10 No provider/model/resource-selection logic added to Turnstone.

USAGE
  python3 validate_bwp.py            # run all checks + self-test; exit 0 on expected results
  python3 validate_bwp.py --json     # machine-readable report
"""

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SCHEMA_DIR = BASE / "schema"
EXAMPLES_DIR = BASE / "examples"

# ---------------------------------------------------------------------------
# Load vocabularies + schemas (also verifies they are valid JSON)
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception as exc:  # pragma: no cover - guarded at startup
        raise SystemExit(f"FATAL: cannot parse {path}: {exc}")

VOCAB = load_json(SCHEMA_DIR / "vocabularies.json")
BWP_REQ_SCHEMA = load_json(SCHEMA_DIR / "bwp-request-v0.1.json")
ASSIGNMENT_SCHEMA = load_json(SCHEMA_DIR / "bwp-assignment-v0.1.json")
RECEIPT_SCHEMA = load_json(SCHEMA_DIR / "bwp-evidence-receipt-v0.1.json")
VERDICT_SCHEMA = load_json(SCHEMA_DIR / "bwp-verdict-v0.1.json")

SUFFICIENCY_STATES = set(VOCAB["sufficiency_states"])
WORK_SHAPES = set(VOCAB["work_shapes"])
REASONING_INTENTS = set(VOCAB["reasoning_intents"])
RISK_CLASSES = set(VOCAB["risk_classes"])
INFERENCE_LOCALITIES = set(VOCAB["inference_localities"])
OBSERVED_LOCALITIES = set(VOCAB["observed_localities"])
EVIDENCE_STATUSES = set(VOCAB["evidence_claim_statuses"])
WORK_OUTCOMES = set(VOCAB["work_outcomes"])
EXECUTORS = set(VOCAB["executors"])
CAPABILITIES = set(VOCAB["capabilities"])
ACTIONS = set(VOCAB["actions"])
EVIDENCE_TYPES = set(VOCAB["evidence_types"])
FORBIDDEN_KEYS = VOCAB["forbidden_resource_identity_keys"]
FORBIDDEN_TOKENS = VOCAB["forbidden_resource_identity_tokens"]

# Prose is NEVER authoritative routing metadata. Structural work fields are the ONLY
# routing source. Prose mention of identity/routing tokens (in descriptions, inputs,
# historical notes, quoted logs) produces a WARNING/audit signal, never a hard failure.
# Hard failures come only from STRUCTURAL identity encoding: forbidden identity keys in
# requirement fields (e.g. requirements.model='DeepSeek', requirements.resource='HTPC')
# and capabilities_required entries outside the semantic vocabulary.
PROSE_ROUTING_TOKENS = [
    "luna", "terra", "sol", "deepseek", "flash", "thinking", "gpt-", "llama", "qwen",
    "htpc", "comfyninja", "reninja", "switchyard", "openai", "codex", "gemma", "model",
    "provider", "resource", "endpoint", "gpu", ":8645", ":8647", ":8445", ":8081", ":8080",
]

# ---------------------------------------------------------------------------
# Issue model
# ---------------------------------------------------------------------------

class Issues:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    @property
    def valid(self):
        return not self.errors


def _scan_string_for_tokens(text: str, tokens, where: str, issues: Issues, kind="warning"):
    low = text.lower()
    for tok in tokens:
        if tok.lower() in low:
            msg = f"{where}: prose mentions identity/routing token '{tok}' (warning only; prose is not routing truth)"
            (issues.error if kind == "error" else issues.warn)(msg)


def _scan_subtree_for_identity_keys(obj, where: str, issues: Issues):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_KEYS:
                issues.error(f"{where}.{k}: forbidden resource-identity key '{k}'")
            _scan_subtree_for_identity_keys(v, f"{where}.{k}", issues)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_subtree_for_identity_keys(v, f"{where}[{i}]", issues)


def _scan_subtree_for_identity_tokens(obj, where: str, issues: Issues, kind="warning"):
    """Mention of identity tokens in values => warning by default (prose is not routing
    truth). Structural identity encoding is caught by key scan + vocab conformance."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _scan_subtree_for_identity_tokens(v, f"{where}.{k}", issues, kind=kind)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_subtree_for_identity_tokens(v, f"{where}[{i}]", issues, kind=kind)
    elif isinstance(obj, str):
        _scan_string_for_tokens(obj, FORBIDDEN_TOKENS, where, issues, kind=kind)


def _enforce_schema_shape(obj, schema, where: str, issues: Issues):
    """Recursive additionalProperties:false enforcement DRIVEN BY THE ACTUAL SCHEMA
    JSON files (LB-1 depth fix + union-type handling). Mirrors the declared nested
    structure so synonym-key identity smuggling fails at any depth (e.g.
    requirements.context.required_model, assignment.assigned.required_model,
    receipt.required_model). Handles union types such as ["object", "null"]
    (nullable nested objects are shape-checked when present; null values pass).
    Scope note: the runtime design retires this mirror in favor of the native
    Turnstone/Pydantic validation idiom (see NATIVE-INTEGRATION-ANALYSIS)."""
    types = schema.get("type")
    type_list = types if isinstance(types, list) else [types]
    if not isinstance(obj, dict) or "object" not in type_list:
        return
    props = schema.get("properties", {})
    allowed = set(props.keys())
    for k in obj:
        if k not in allowed:
            issues.error(f"{where}: unknown property '{k}' (additionalProperties:false)")
    for k, v in obj.items():
        if k not in props:
            continue
        sub = props[k]
        sub_types = sub.get("type")
        type_list = sub_types if isinstance(sub_types, list) else [sub_types]
        if isinstance(v, dict) and "object" in type_list and "properties" in sub:
            _enforce_schema_shape(v, sub, f"{where}.{k}", issues)
        elif isinstance(v, list) and "array" in type_list:
            items = sub.get("items") or {}
            item_types = items.get("type")
            itype_list = item_types if isinstance(item_types, list) else [item_types]
            if "object" in itype_list and "properties" in items:
                for i, item in enumerate(v):
                    if isinstance(item, dict):
                        _enforce_schema_shape(item, items, f"{where}.{k}[{i}]", issues)


# ---------------------------------------------------------------------------
# REQUEST validation
# ---------------------------------------------------------------------------

def validate_bwp(packet: dict) -> Issues:
    """Structural + semantic validation of a BWP REQUEST."""
    issues = Issues()

    # kind / schema version
    if packet.get("kind") != "bwp-request":
        issues.error(f"kind: expected 'bwp-request', got {packet.get('kind')!r}")
    if packet.get("schema_version") != "0.1":
        issues.error(f"schema_version: expected '0.1', got {packet.get('schema_version')!r}")

    # required top-level sections
    for section in ["provenance", "intent", "work", "requirements", "authority", "acceptance", "evidence", "control"]:
        if section not in packet:
            issues.error(f"missing required section: {section}")

    if not issues.valid:
        return issues

    # LB-1: recursive additionalProperties:false enforcement driven by the actual
    # schema JSON (unknown keys at ANY depth, e.g. requirements.context.required_model,
    # are rejected) + whole-packet structural identity-key scan (belt-and-suspenders:
    # any dict key named model/provider/resource/endpoint/gpu/device/node/route/backend
    # in a REQUEST is structural identity encoding).
    _enforce_schema_shape(packet, BWP_REQ_SCHEMA, "packet", issues)
    _scan_subtree_for_identity_keys(packet, "packet", issues)

    # --- provenance ---
    prov = packet["provenance"]
    for field in ["operator", "workstream_id", "correlation_id", "created_at"]:
        if field not in prov:
            issues.error(f"provenance: missing '{field}'")
    # R7: prose is never authoritative routing metadata. Description may mention
    # providers/models/resources; mention alone produces a WARNING, never a hard
    # failure. Structural work fields remain the only routing source (R3/R7b).
    if isinstance(prov.get("description"), str) and prov["description"].strip():
        _scan_string_for_tokens(prov["description"], PROSE_ROUTING_TOKENS, "provenance.description", issues, kind="warning")

    # --- intent / sufficiency ---
    intent = packet["intent"]
    for field in ["outcome", "non_goals", "sufficiency"]:
        if field not in intent:
            issues.error(f"intent: missing '{field}'")
    if "sufficiency" in intent:
        suff = intent["sufficiency"]
        if suff.get("state") not in SUFFICIENCY_STATES:
            issues.error(f"intent.sufficiency.state: invalid {suff.get('state')!r}")
        if suff.get("adjudicated_by") != "Turnstone":
            issues.error("intent.sufficiency.adjudicated_by: must be 'Turnstone'")
        if not isinstance(suff.get("rationale"), str) or not suff["rationale"].strip():
            issues.error("intent.sufficiency.rationale: required non-empty")

    # --- work (structural, REQUIRED) ---
    work = packet["work"]
    # R3: work_shape is required structural
    if "work_shape" not in work:
        issues.error("work.work_shape: REQUIRED structural field missing")
    elif work["work_shape"] not in WORK_SHAPES:
        issues.error(f"work.work_shape: invalid {work['work_shape']!r}")
    if "reasoning_intent" not in work:
        issues.error("work.reasoning_intent: REQUIRED structural field missing")
    elif work["reasoning_intent"] not in REASONING_INTENTS:
        issues.error(f"work.reasoning_intent: invalid {work['reasoning_intent']!r}")
    # The work object must not carry extra prose fields that could masquerade as routing input.
    for k in work:
        if k not in ("work_shape", "reasoning_intent"):
            issues.error(f"work.{k}: unexpected field; routing metadata is structural only")

    # --- requirements ---
    reqs = packet["requirements"]
    caps = reqs.get("capabilities_required", [])
    if not isinstance(caps, list) or not caps:
        issues.error("requirements.capabilities_required: required non-empty list")
    for c in caps:
        # R2: capability must be semantic vocab, not an inference-resource identity
        if c not in CAPABILITIES:
            issues.error(f"requirements.capabilities_required: '{c}' is not a semantic capability "
                         f"(resource identities are forbidden; use the vocabulary)")
    if reqs.get("inference_locality") not in INFERENCE_LOCALITIES:
        issues.error(f"requirements.inference_locality: invalid {reqs.get('inference_locality')!r} "
                     f"(inference-resource locality only; NEVER selects the executor)")
    # R2: scan the requirements subtree for forbidden identity keys/tokens
    _scan_subtree_for_identity_keys(reqs, "requirements", issues)
    _scan_subtree_for_identity_tokens(reqs, "requirements", issues, kind="warning")
    # context seam (minimal)
    ctx = reqs.get("context") or {}
    if "required_inputs" in ctx and not isinstance(ctx["required_inputs"], list):
        issues.error("requirements.context.required_inputs: must be a list")
    if "context_size_requirement" in ctx and ctx["context_size_requirement"] is not None:
        n = ctx["context_size_requirement"]
        if not isinstance(n, int) or n < 0:
            issues.error("requirements.context.context_size_requirement: non-negative integer or null")
    if "retrieval_required" in ctx and not isinstance(ctx.get("retrieval_required"), bool):
        issues.error("requirements.context.retrieval_required: boolean required")
    # preferences must be inactive in v1 (R4 groundwork)
    for pref in reqs.get("preferences", []):
        if pref.get("active") is not False:
            issues.error(f"requirements.preferences[{pref.get('name')!r}]: preferences must be active:false in v1")
        if pref.get("name") not in ("latency", "cost"):
            issues.error(f"requirements.preferences: unknown preference {pref.get('name')!r}")

    # --- authority ---
    auth = packet["authority"]
    if auth.get("risk_class") not in RISK_CLASSES:
        issues.error(f"authority.risk_class: invalid {auth.get('risk_class')!r}")
    allowed = auth.get("allowed_actions", [])
    forbidden = auth.get("forbidden_actions", [])
    if not isinstance(allowed, list) or not allowed:
        issues.error("authority.allowed_actions: required non-empty list")
    for a in allowed:
        if a not in ACTIONS:
            issues.error(f"authority.allowed_actions: '{a}' not in semantic action vocabulary")
    for f in forbidden:
        if f not in ACTIONS:
            issues.error(f"authority.forbidden_actions: '{f}' not in semantic action vocabulary")
    overlap = set(allowed) & set(forbidden)
    if overlap:
        issues.error(f"authority: actions both allowed and forbidden: {sorted(overlap)}")
    if auth.get("credential_rule") != "logical-refs-only":
        issues.error("authority.credential_rule: must be 'logical-refs-only'")

    # --- acceptance ---
    criteria = packet["acceptance"].get("criteria", [])
    if not isinstance(criteria, list) or not criteria:
        issues.error("acceptance.criteria: required non-empty list")
    for c in criteria:
        if not isinstance(c, str) or not c.strip():
            issues.error("acceptance.criteria: each criterion must be non-empty string")

    # --- evidence ---
    ev_reqs = packet["evidence"].get("requirements", [])
    if not isinstance(ev_reqs, list) or not ev_reqs:
        issues.error("evidence.requirements: required non-empty list")
    for e in ev_reqs:
        if e not in EVIDENCE_TYPES:
            issues.error(f"evidence.requirements: '{e}' not in semantic evidence-type vocabulary")

    # --- control ---
    ctrl = packet["control"]
    if not isinstance(ctrl.get("timeout_seconds"), int) or ctrl["timeout_seconds"] < 1:
        issues.error("control.timeout_seconds: positive integer required")
    if not isinstance(ctrl.get("cancel_policy"), str) or not ctrl["cancel_policy"].strip():
        issues.error("control.cancel_policy: required non-empty")
    if not isinstance(ctrl.get("escalation_conditions"), list) or not ctrl["escalation_conditions"]:
        issues.error("control.escalation_conditions: required non-empty list")

    return issues


# ---------------------------------------------------------------------------
# Dispatch gate
# ---------------------------------------------------------------------------

def sufficiency_disposition(packet: dict) -> str:
    """Deterministic governance disposition for the sufficiency gate.

    DISPATCH  — SUFFICIENT: assign and execute.
    CLARIFY   — AMBIGUOUS: outcome/boundary insufficiently defined; operator
                clarification required; packet does NOT dispatch and is NOT
                adjudicated. It is a CLARIFY record (no new lifecycle object;
                reuse the existing workstream for the clarification exchange).
    REJECT    — INSUFFICIENT: cannot be made executable at governance level;
                return to operator for re-authoring (or supersede). Escalation
                remains available where the operator's governance context
                demands it (see design doc §sufficiency lifecycle).
    """
    state = None
    try:
        state = packet["intent"]["sufficiency"]["state"]
    except (KeyError, TypeError):
        return "REJECT"  # malformed/unreadable packet: cannot dispatch (N3 hardening)
    if state == "SUFFICIENT":
        return "DISPATCH"
    if state == "AMBIGUOUS":
        return "CLARIFY"
    return "REJECT"  # INSUFFICIENT


def unsupported_v1_reason(bwp: dict) -> str | None:
    """Hard-requirement support audit gate (direct-review #5000030053).

    A BWP hard requirement must be ENFORCED BEFORE EXECUTION by the current
    production Switchyard/FleetRouter structured ingress. Fields with no
    pre-selection ingress are UNSUPPORTED_V1 and must fail closed BEFORE
    dispatch (never dispatch and rely on post-hoc receipt detection).

    Supported (ENFORCED_NOW):
      - work_shape  (work_shape_source="request" -> extensions.fields["work_shape"])
      - reasoning_intent (require_reasoning from normalized reasoning controls /
        legacy work_class=reasoning; candidate must advertise reasoning=true)

    Unsupported (UNSUPPORTED_V1, fail closed):
      - inference_locality != "any" (no FleetRouter ingress/config)
      - context_size_requirement non-null (no pre-selection ingress; S2-E
        context admission not wired in production config)
      - output_budget non-null (no pre-selection ingress to max_output_tokens
        in production admission)
    """
    reqs = bwp.get("requirements", {})
    il = reqs.get("inference_locality")
    if il not in (None, "any"):
        return (f"UNSUPPORTED_V1: requirements.inference_locality={il!r} has no pre-selection "
                "FleetRouter ingress; only 'any' is dispatchable in v1")
    ctx = reqs.get("context") or {}
    if ctx.get("context_size_requirement") is not None:
        return ("UNSUPPORTED_V1: requirements.context.context_size_requirement has no "
                "pre-selection ingress; set null in v1")
    if reqs.get("output_budget") is not None:
        return ("UNSUPPORTED_V1: requirements.output_budget has no pre-selection ingress; "
                "set null in v1")
    return None


def validate_for_dispatch(packet: dict):
    """R1/C2 + support gate: only a structurally valid, SUFFICIENT request whose
    hard requirements are all ENFORCED_NOW may dispatch."""
    issues = validate_bwp(packet)
    disposition = sufficiency_disposition(packet)
    unsupported = unsupported_v1_reason(packet)
    if not issues.valid:
        # A structurally invalid packet is never dispatchable; disposition is
        # descriptive only, but REJECT is the coherent disposition (it cannot
        # DISPATCH). The `allowed` boolean is the authoritative gate.
        return {"allowed": False, "reason": "validation_errors", "disposition": "REJECT",
                "errors": issues.errors, "warnings": issues.warnings}
    if packet["intent"]["sufficiency"]["state"] != "SUFFICIENT":
        return {"allowed": False,
                "reason": f"sufficiency_gate: state={packet['intent']['sufficiency']['state']}",
                "disposition": disposition, "errors": [], "warnings": issues.warnings}
    if unsupported:
        return {"allowed": False, "reason": unsupported, "disposition": "REJECT",
                "errors": [], "warnings": issues.warnings}
    return {"allowed": True, "reason": "dispatch_ok", "disposition": "DISPATCH",
            "errors": [], "warnings": issues.warnings}


# ---------------------------------------------------------------------------
# Executor eligibility / assignment (SPECIFICATION functions — reference logic
# for the MAPPING, not runtime code). EXECUTOR SELECTION IS NOT A SECOND ROUTER.
#
# Two strictly separate decision chains:
#   1. BWP capability + authority requirements
#        -> derive_executor_candidates()   (executor eligibility)
#        -> select_executor()              (Turnstone / Hermes / OpenClaw assignment)
#   2. BWP work_shape + reasoning_intent
#        -> Switchyard / FleetRouter       (inference resource eligibility + preference)
#
# work_shape / reasoning_intent describe INFERENCE SEMANTICS for chain 2 ONLY.
# They NEVER select an executor: no mechanical agentic->Hermes or bounded->Turnstone
# mapping. Executor selection derives from required capabilities, authority/risk,
# available execution surfaces, and operator gates. `inference_locality` is a
# RESOURCE constraint for chain 2 ONLY and NEVER selects an executor (ownership
# separation, direct-review correction 1). Provider/model/resource selection
# remains FleetRouter's, never Turnstone's.
# ---------------------------------------------------------------------------

MUTATING_ACTIONS = {"manage_services", "manage_containers", "deploy_production",
                    "install_packages", "create_credentials", "destroy_assets"}


def derive_executor_candidates(bwp: dict) -> dict:
    """Executor ELIGIBILITY: lanes whose capability surface satisfies the required
    semantic capabilities. `inference_locality` is deliberately NOT consulted:
    executor placement derives from capabilities + authority + sanctioned
    execution surfaces (Turnstone-owned), never from inference-resource locality.
    Returns {'candidates': [...], 'capability_gap': bool}."""
    reqs = bwp.get("requirements", {})
    caps = set(reqs.get("capabilities_required", []))
    LANES = {
        "turnstone-native": {"text_generation", "structured_extraction", "filesystem_read", "filesystem_write",
                             "local_execution", "code_execution", "deterministic_computation", "memory_retrieval",
                             "web_retrieval", "long_context_processing"},
        "hermes": {"text_generation", "image_generation", "structured_extraction", "code_execution", "web_retrieval",
                   "web_browsing", "filesystem_read", "filesystem_write", "local_execution", "process_management",
                   "network_access", "gpu_workload_execution", "document_parsing", "image_analysis",
                   "browser_automation", "memory_retrieval", "long_context_processing", "install_packages",
                   "manage_services", "manage_containers", "inspect_remote_hosts"},
        "openclaw-local": {"text_generation", "structured_extraction", "external_emission", "web_retrieval",
                           "memory_retrieval", "filesystem_read"},
        "openclaw-remote": {"text_generation", "structured_extraction", "external_emission", "web_retrieval",
                            "memory_retrieval", "gpu_workload_execution"},
        "operator": set(ACTIONS),
    }
    capable = [lane for lane, lane_caps in LANES.items() if caps <= lane_caps]
    capable = [l for l in capable if l not in ("operator", "external")]
    return {"candidates": capable, "capability_gap": not capable}


def select_executor(bwp: dict) -> dict:
    """Executor ASSIGNMENT precedence (capability + authority + operator gates only).
    Never consults work_shape / reasoning_intent; never consults inference_locality;
    never selects a provider/model. `inference_locality` is a FleetRouter resource
    constraint and does NOT choose Hermes/OpenClaw/Turnstone."""
    auth = bwp.get("authority", {})
    reqs = bwp.get("requirements", {})
    caps = set(reqs.get("capabilities_required", []))
    risk = auth.get("risk_class", "advisory")
    approval_required = bool((auth.get("mutation_envelope") or {}).get("approval_required"))

    # 1. Operator gate (authority/governance constraint)
    if risk == "critical" or approval_required:
        return {"executor": "operator",
                "rationale": "risk_class critical or approval_required => operator gate"}
    # 2. Capability gap (eligibility failure => report, do not assign)
    derived = derive_executor_candidates(bwp)
    capable = derived["candidates"]
    if derived["capability_gap"]:
        return {"executor": None,
                "rationale": f"executor capability gap: no execution surface provides {sorted(caps)}; report, do not assign"}
    # 3. Emission-bound work (capability-driven lane; emission target by authority envelope)
    if "external_emission" in caps:
        return {"executor": "openclaw-local",
                "rationale": "external_emission capability => OpenClaw lane (target by authority envelope)"}
    # 4. Browser/web automation (capability-driven lane)
    if caps & {"web_browsing", "browser_automation"}:
        return {"executor": "hermes", "rationale": "browser/web automation capability => Hermes engineering lane"}
    # 5. Mutation-capable + operational/elevated risk (authority-driven lane)
    if caps & MUTATING_ACTIONS and risk in ("operational", "elevated"):
        return {"executor": "hermes" if "hermes" in capable else capable[0],
                "rationale": "mutation-capable + operational/elevated risk => Hermes (capability-derived)"}
    # 6. Smallest capable path: non-mutating + advisory + Turnstone-native capable
    #    (no work_shape involved — pure capability/authority/placement)
    if (risk == "advisory" and not (caps & MUTATING_ACTIONS) and "turnstone-native" in capable):
        return {"executor": "turnstone-native",
                "rationale": "advisory + non-mutating + Turnstone-native capable => smallest path"}
    # 7. Capability-derived default among eligible candidates
    for preferred in ("hermes", "openclaw-local", "openclaw-remote"):
        if preferred in capable:
            return {"executor": preferred,
                    "rationale": f"executor eligibility default: {sorted(caps)} => {preferred}"}
    return {"executor": capable[0] if capable else None,
            "rationale": "executor eligibility fallback"}


# ---------------------------------------------------------------------------
# Eligibility vs preference (R4 doctrine)
# ---------------------------------------------------------------------------

def eligibility(candidates, bwp: dict):
    """Hard requirements only (FleetRouter-chain demonstration). Candidates:
    [{'id', 'capabilities', 'locality' (inference-resource locality), 'context_capacity',
    'supports_reasoning', 'supports_work_shapes'}]. Consumes inference_locality
    (resource constraint) ONLY; never executor placement."""
    reqs = bwp.get("requirements", {})
    caps = set(reqs.get("capabilities_required", []))
    inference_locality = reqs.get("inference_locality", "any")
    ctx_req = (reqs.get("context") or {}).get("context_size_requirement")
    work = bwp.get("work", {})
    reasoning = work.get("reasoning_intent") == "deliberate"
    eligible = []
    for c in candidates:
        if not caps <= set(c.get("capabilities", [])):
            continue
        if inference_locality == "local_required" and c.get("locality") != "local":
            continue
        if inference_locality == "hosted_allowed" and c.get("locality") not in ("hosted", "any"):
            continue
        if ctx_req is not None and c.get("context_capacity") is not None and ctx_req > c["context_capacity"]:
            continue
        if reasoning and not c.get("supports_reasoning", False):
            continue
        if work.get("work_shape") == "bounded" and c.get("supports_work_shapes") is not None \
                and "bounded" not in c["supports_work_shapes"]:
            continue
        eligible.append(c["id"])
    return eligible


def apply_preferences(eligible_ids, candidates, preferences):
    """Preferences operate ONLY among eligible choices. Never rescues an ineligible option."""
    by_id = {c["id"]: c for c in candidates}
    eligible = [by_id[i] for i in eligible_ids if i in by_id]
    if not preferences:
        return [e["id"] for e in eligible]
    # v1: preferences are inactive; ranking hooks reserved. Return eligible as-is.
    return [e["id"] for e in eligible]


# ---------------------------------------------------------------------------
# ASSIGNMENT validation
# ---------------------------------------------------------------------------

def validate_assignment(assignment: dict, bwp: dict) -> Issues:
    issues = Issues()
    if assignment.get("schema_version") != "0.1":
        issues.error(f"schema_version: expected '0.1', got {assignment.get('schema_version')!r}")
    if assignment.get("kind") != "bwp-assignment":
        issues.error(f"kind: expected 'bwp-assignment', got {assignment.get('kind')!r}")
    _enforce_schema_shape(assignment, ASSIGNMENT_SCHEMA, "assignment", issues)
    if assignment.get("packet_id") != bwp.get("packet_id"):
        issues.error("packet_id mismatch with request")
    if assignment.get("correlation_id") != bwp["provenance"].get("correlation_id"):
        issues.error("correlation_id mismatch with request")
    gate = assignment.get("dispatch_gate", {})
    if gate.get("sufficiency_at_dispatch") != "SUFFICIENT":
        issues.error("dispatch_gate.sufficiency_at_dispatch must be SUFFICIENT")
    if gate.get("validated") is not True:
        issues.error("dispatch_gate.validated must be true")
    ex = assignment.get("assigned", {}).get("executor")
    if ex not in EXECUTORS:
        issues.error(f"assigned.executor: invalid {ex!r}")
    if assignment.get("assigned", {}).get("derivation") != "capability+authority":
        issues.error("assigned.derivation must be 'capability+authority'")
    mh = assignment.get("metadata_handoff", {})
    if mh.get("work_shape") != bwp["work"]["work_shape"]:
        issues.error("metadata_handoff.work_shape mismatch with request work_shape")
    if mh.get("reasoning_intent") != bwp["work"]["reasoning_intent"]:
        issues.error("metadata_handoff.reasoning_intent mismatch with request")
    # assignment must never name a provider/model
    _scan_subtree_for_identity_keys(assignment, "assignment", issues)
    _scan_subtree_for_identity_tokens(assignment, "assignment", issues, kind="warning")
    return issues


# ---------------------------------------------------------------------------
# RECEIPT validation (authority compliance + evidence mapping)
# ---------------------------------------------------------------------------

def validate_receipt(receipt: dict, bwp: dict, assignment: dict) -> Issues:
    issues = Issues()
    if receipt.get("schema_version") != "0.1":
        issues.error(f"schema_version: expected '0.1', got {receipt.get('schema_version')!r}")
    if receipt.get("kind") != "bwp-evidence-receipt":
        issues.error(f"kind: expected 'bwp-evidence-receipt', got {receipt.get('kind')!r}")
    _enforce_schema_shape(receipt, RECEIPT_SCHEMA, "receipt", issues)
    if receipt.get("packet_id") != bwp.get("packet_id"):
        issues.error("packet_id mismatch with request")
    if receipt.get("correlation_id") != bwp["provenance"].get("correlation_id"):
        issues.error("correlation_id mismatch with request")
    if receipt.get("executor", {}).get("declared_executor") != assignment.get("assigned", {}).get("executor"):
        issues.error("executor.declared_executor mismatch with assignment")
    if receipt.get("executor", {}).get("actual_executor") != receipt.get("executor", {}).get("declared_executor"):
        issues.error("executor.actual_executor mismatch with declared_executor (material finding)")

    # R5: authority compliance
    allowed = set(bwp["authority"].get("allowed_actions", []))
    forbidden = set(bwp["authority"].get("forbidden_actions", []))
    taken = set(receipt.get("execution", {}).get("actions_taken", []))
    if not taken:
        issues.error("execution.actions_taken: empty")
    outside = taken - allowed
    touched_forbidden = taken & forbidden
    if outside:
        issues.error(f"authority violation: actions_taken outside allowed_actions: {sorted(outside)}")
    if touched_forbidden:
        issues.error(f"authority violation: actions_taken touch forbidden_actions: {sorted(touched_forbidden)}")

    # evidence claims
    claims = receipt.get("evidence_claims", [])
    if not claims:
        issues.error("evidence_claims: required non-empty")
    for i, claim in enumerate(claims):
        if claim.get("status") not in EVIDENCE_STATUSES:
            issues.error(f"evidence_claims[{i}].status: invalid {claim.get('status')!r}")
        for s in claim.get("satisfies", []):
            if s not in EVIDENCE_TYPES:
                issues.error(f"evidence_claims[{i}].satisfies: '{s}' not in evidence-type vocabulary")

    # R6: evidence sufficiency mapping — every BWP evidence requirement must be
    # satisfied by at least one claim whose status is PROVEN (actually verified).
    required = set(bwp["evidence"].get("requirements", []))
    satisfied = set()
    for claim in claims:
        if claim.get("status") == "PROVEN":
            satisfied.update(claim.get("satisfies", []))
    missing = required - satisfied
    if missing:
        issues.error(f"evidence gap: BWP evidence requirements not satisfied by PROVEN claims: {sorted(missing)}")
    return issues


# ---------------------------------------------------------------------------
# Deterministic adjudication
# ---------------------------------------------------------------------------

def adjudicate(receipt: dict, bwp: dict, assignment: dict, escalated: bool = False) -> dict:
    """Return a verdict object per the deterministic rules. Evidence epistemology
    (claim statuses) is kept separate from the work outcome.

    Direct-review corrections (2026-08-22):
      - Hard-constraint adjudication uses the OBSERVED inference-resource locality
        (resource_observed.inference_resource.locality), never provider/model names.
        local_required + PROVEN hosted => VIOLATED => FAIL; + PROVEN local => compliant;
        + unknown/unobservable => INDETERMINATE (blocking), never inferred FAIL.
      - Acceptance is adjudicated PER CRITERION from positive PROVEN evidence
        references (criterion_refs): MET only when a PROVEN claim references the
        criterion; NOT_MET from a FAILED reference; INDETERMINATE otherwise.
        Absence of a FAILED claim alone never makes a criterion MET; the executor's
        acceptance self-assessment is never authoritative.
    """
    rv = validate_receipt(receipt, bwp, assignment)
    authority_violation = any("authority violation" in e for e in rv.errors)
    evidence_gap = any("evidence gap" in e for e in rv.errors)

    # --- Hard constraints from OBSERVED locality (not provider/model presence) ---
    hard_violation = False
    locality_indeterminate = False
    inference_locality = bwp["requirements"].get("inference_locality")
    observed = receipt.get("resource_observed") or {}
    inferred = observed.get("inference_resource") or {}
    observed_locality = inferred.get("locality")
    if inference_locality == "local_required":
        if observed_locality == "hosted":
            hard_violation = True
        elif observed_locality not in ("local",):
            # unknown or absent => locality not proven => INDETERMINATE, never FAIL.
            locality_indeterminate = True

    # A materially INDETERMINATE claim blocks PASS.
    blocking_indeterminate = any(
        c.get("status") == "INDETERMINATE" for c in receipt.get("evidence_claims", [])
    )

    # --- Per-criterion acceptance adjudication (direct-review correction 3) ---
    claims = receipt.get("evidence_claims", [])
    criteria = bwp["acceptance"]["criteria"]
    adjudicated = []
    for idx, criterion in enumerate(criteria):
        refs = {str(idx), criterion, f"c{idx}"}
        met = any(cl.get("status") == "PROVEN" and (set(cl.get("criterion_refs", [])) & refs)
                  for cl in claims)
        not_met = any(cl.get("status") == "FAILED" and (set(cl.get("criterion_refs", [])) & refs)
                      for cl in claims)
        if not_met:
            status = "NOT_MET"
        elif met:
            status = "MET"
        else:
            status = "INDETERMINATE"  # no positive PROVEN adjudication => not MET
        adjudicated.append({"criterion": criterion, "status": status,
                            "evidence_refs": [cl.get("evidence_ref", "") for cl in claims
                                               if (set(cl.get("criterion_refs", [])) & refs)],
                            "rationale": ("positive PROVEN evidence" if met else
                                          "proven failure" if not_met else
                                          "not positively adjudicated from evidence")})

    basis = {
        "acceptance": adjudicated,
        "authority_compliance": "VIOLATION" if authority_violation else "COMPLIANT",
        "evidence_sufficiency": "INSUFFICIENT" if (evidence_gap or blocking_indeterminate or locality_indeterminate) else "SUFFICIENT",
        "evidence_epistemology": [
            {"claim": cl.get("claim", ""), "status": cl.get("status", "PROPOSED")}
            for cl in claims
        ],
        "hard_constraint_status": ("NOT_EVALUATED" if escalated else
                                   "VIOLATED" if hard_violation else
                                   "UNVERIFIABLE" if locality_indeterminate else "SATISFIED"),
    }

    if escalated:
        outcome = "ESCALATED"
    elif authority_violation:
        outcome = "FAIL"
    elif hard_violation:
        outcome = "FAIL"
    elif any(a["status"] == "NOT_MET" for a in adjudicated):
        outcome = "FAIL"
    elif (evidence_gap or blocking_indeterminate or locality_indeterminate
          or any(a["status"] == "INDETERMINATE" for a in adjudicated)):
        outcome = "INDETERMINATE"
    else:
        outcome = "PASS"

    verdict = {
        "schema_version": "0.1",
        "kind": "bwp-verdict",
        "packet_id": bwp["packet_id"],
        "correlation_id": bwp["provenance"]["correlation_id"],
        "workstream_id": bwp["provenance"]["workstream_id"],
        "adjudicated_at": receipt.get("received_at", ""),
        "work_outcome": outcome,
        "basis": basis,
        "closeout": {
            "workstream_state": "escalated" if outcome in ("ESCALATED", "FAIL") and (authority_violation or hard_violation) else
                               ("closed" if outcome == "PASS" else "reopened"),
            "receipt_links": [f"workstream/{bwp['provenance']['workstream_id']}"],
            "escalation_ref": ("authority-violation" if authority_violation else
                               "hard-constraint-violation" if hard_violation else None),
            "next_action": ("escalate authority violation to operator" if authority_violation else
                            "escalate hard-constraint violation to operator" if hard_violation else
                            "close" if outcome == "PASS" else "reconcile and re-adjudicate"),
        },
    }
    return verdict


def validate_verdict(verdict: dict) -> Issues:
    issues = Issues()
    if verdict.get("schema_version") != "0.1":
        issues.error(f"schema_version: expected '0.1', got {verdict.get('schema_version')!r}")
    if verdict.get("kind") != "bwp-verdict":
        issues.error(f"kind: expected 'bwp-verdict', got {verdict.get('kind')!r}")
    _enforce_schema_shape(verdict, VERDICT_SCHEMA, "verdict", issues)
    if verdict.get("work_outcome") not in WORK_OUTCOMES:
        issues.error(f"work_outcome: invalid {verdict.get('work_outcome')!r}")
    if verdict["basis"].get("authority_compliance") not in ("COMPLIANT", "VIOLATION"):
        issues.error("basis.authority_compliance: invalid")
    if verdict["basis"].get("evidence_sufficiency") not in ("SUFFICIENT", "INSUFFICIENT"):
        issues.error("basis.evidence_sufficiency: invalid")
    return issues


# ---------------------------------------------------------------------------
# Self-test suite
# ---------------------------------------------------------------------------

def load_example(name: str) -> dict:
    return load_json(EXAMPLES_DIR / name)


def build_assignment(bwp: dict, executor=None, run_ids=None, extra=None) -> dict:
    lane = select_executor(bwp) if executor is None else {"executor": executor, "rationale": "fixture"}
    a = {
        "schema_version": "0.1",
        "kind": "bwp-assignment",
        "packet_id": bwp["packet_id"],
        "correlation_id": bwp["provenance"]["correlation_id"],
        "workstream_id": bwp["provenance"]["workstream_id"],
        "created_at": "2026-08-22T17:00:00+07:00",
        "dispatch_gate": {"sufficiency_at_dispatch": "SUFFICIENT", "validated": True,
                          "validator_version": "bwp-validator-v0.1"},
        "assigned": {
            "executor": lane["executor"],
            "derivation": "capability+authority",
            "rationale": lane["rationale"],
            "delegation_envelope_ref": f"delegation/{bwp['packet_id']}",
            "run_correlation_ids": run_ids or [],
        },
        "metadata_handoff": {
            "work_shape": bwp["work"]["work_shape"],
            "reasoning_intent": bwp["work"]["reasoning_intent"],
            "note": "Passed structurally to executor ingress / extra_body. FleetRouter selects the resource; this object never names one.",
        },
    }
    if extra:
        for k, v in extra.items():
            a.setdefault(k, {})  # placeholder; used only by negative fixtures via direct construction
    return a


def run_self_test() -> dict:
    """Run all qualification examples + rejection demonstrations. Returns report."""
    report = {"examples": [], "rejections": [], "controls": [], "ok": True}

    # ---- Qualification examples (lifecycle demonstration) ----
    examples = [
        ("T1 bounded routine", "qualification-1-bounded-routine.json"),
        ("T2 agentic non-deliberate", "qualification-2-agentic-nondeliberate.json"),
        ("T3 deliberate/reasoning", "qualification-3-deliberate-reasoning.json"),
        ("T5 real-estate workload", "qualification-5-real-estate-workload.json"),
    ]
    for label, fname in examples:
        bwp = load_example(fname)
        issues = validate_bwp(bwp)
        dispatch = validate_for_dispatch(bwp)
        assignment = build_assignment(bwp)
        a_issues = validate_assignment(assignment, bwp)
        receipt = build_receipt(bwp, assignment, label)
        r_issues = validate_receipt(receipt, bwp, assignment)
        verdict = adjudicate(receipt, bwp, assignment)
        v_issues = validate_verdict(verdict)
        ok = (issues.valid and dispatch["allowed"] and a_issues.valid and r_issues.valid
              and v_issues.valid and verdict["work_outcome"] == "PASS")
        report["examples"].append({
            "label": label, "packet": fname, "ok": ok,
            "bwp_valid": issues.valid, "dispatch": dispatch,
            "assignment_valid": a_issues.valid,
            "receipt_valid": r_issues.valid, "verdict": verdict["work_outcome"],
            "errors": issues.errors + a_issues.errors + r_issues.errors + v_issues.errors,
        })
        if not ok:
            report["ok"] = False

    # ---- R1 + C2: insufficient/ambiguous must NOT dispatch; deterministic disposition ----
    bwp4 = load_example("qualification-4-insufficient-ambiguous.json")
    issues4 = validate_bwp(bwp4)
    dispatch4 = validate_for_dispatch(bwp4)
    r1_ok = (issues4.valid and not dispatch4["allowed"]
             and dispatch4["reason"].startswith("sufficiency_gate")
             and dispatch4["disposition"] == "CLARIFY")
    report["rejections"].append({"id": "R1", "label": "ambiguous work must not dispatch (CLARIFY)",
                                 "ok": r1_ok, "detail": f"{dispatch4['reason']}; disposition={dispatch4['disposition']}"})

    bwp_insuff = json.loads(json.dumps(load_example("qualification-4-insufficient-ambiguous.json")))
    bwp_insuff["intent"]["sufficiency"]["state"] = "INSUFFICIENT"
    dispatch_insuff = validate_for_dispatch(bwp_insuff)
    c2_ok = (not dispatch_insuff["allowed"] and dispatch_insuff["disposition"] == "REJECT"
             and dispatch4["disposition"] == "CLARIFY"
             and validate_for_dispatch(load_example("qualification-1-bounded-routine.json"))["disposition"] == "DISPATCH")
    report["controls"].append({"id": "C2", "label": "sufficiency disposition deterministic (DISPATCH/CLARIFY/REJECT)",
                               "ok": c2_ok,
                               "detail": f"SUFFICIENT->DISPATCH, AMBIGUOUS->CLARIFY, INSUFFICIENT->REJECT"})

    # ---- R2: resource identity as STRUCTURAL governance requirement ----
    bwp_bad = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp_bad["requirements"]["capabilities_required"] = ["htpc-llm"]
    r2_ok = any("not a semantic capability" in e for e in validate_bwp(bwp_bad).errors)
    report["rejections"].append({"id": "R2", "label": "resource identity rejected as capability",
                                 "ok": r2_ok, "detail": "capabilities_required=['htpc-llm']"})

    bwp_bad2 = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp_bad2["requirements"]["resource"] = "HTPC"
    r2b_ok = any("forbidden resource-identity key" in e for e in validate_bwp(bwp_bad2).errors)
    report["rejections"].append({"id": "R2b", "label": "resource key rejected in requirements",
                                 "ok": r2b_ok, "detail": "requirements.resource='HTPC'"})

    # ---- R8: structural model/provider identity key as governance requirement ----
    bwp_bad8 = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp_bad8["requirements"]["model"] = "DeepSeek"
    r8_ok = any("forbidden resource-identity key" in e for e in validate_bwp(bwp_bad8).errors)
    report["rejections"].append({"id": "R8", "label": "structural model key rejected in requirements",
                                 "ok": r8_ok, "detail": "requirements.model='DeepSeek'"})

    # ---- R3: missing structural work_shape ----
    bwp_bad3 = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    del bwp_bad3["work"]["work_shape"]
    r3_ok = any("work_shape" in e and "REQUIRED" in e for e in validate_bwp(bwp_bad3).errors)
    report["rejections"].append({"id": "R3", "label": "missing work_shape rejected",
                                 "ok": r3_ok, "detail": "work.work_shape removed"})

    # ---- R4: preference must never override a hard constraint ----
    bwp4x = load_example("qualification-2-agentic-nondeliberate.json")
    candidates = [
        {"id": "A", "capabilities": ["text_generation", "web_retrieval", "network_access", "structured_extraction",
                                     "long_context_processing"], "locality": "hosted",
         "context_capacity": 750000, "supports_reasoning": False, "supports_work_shapes": ["agentic"]},
        {"id": "B", "capabilities": ["text_generation"], "locality": "hosted",
         "context_capacity": 266000, "supports_reasoning": False, "supports_work_shapes": ["bounded"]},
    ]
    eligible = eligibility(candidates, bwp4x)
    prefs = [{"name": "cost", "value": "low", "active": False}]
    ranked = apply_preferences(eligible, candidates, prefs)
    # A is the only eligible (B lacks web_retrieval/network_access/etc.). Preference for low cost
    # must NOT rescue B. B is ineligible and must stay out of ranked.
    r4_ok = ("A" in eligible and "B" not in eligible and "B" not in ranked and eligible == ranked)
    report["rejections"].append({"id": "R4", "label": "preference never rescues ineligible (ranking not exercised in v1: apply_preferences is a stub; eligibility filtering proven)",
                                 "ok": r4_ok, "detail": f"eligible={eligible}, ranked={ranked}"})

    # ---- R5: executor broadening mutation authority ----
    bwp5 = load_example("qualification-1-bounded-routine.json")
    assignment5 = build_assignment(bwp5)
    receipt5 = build_receipt(bwp5, assignment5, "T1")
    receipt5["execution"]["actions_taken"].append("deploy_production")
    rv5 = validate_receipt(receipt5, bwp5, assignment5)
    verdict5 = adjudicate(receipt5, bwp5, assignment5)
    r5_ok = any("authority violation" in e for e in rv5.errors) and verdict5["work_outcome"] == "FAIL"
    report["rejections"].append({"id": "R5", "label": "executor broadening authority -> FAIL",
                                 "ok": r5_ok, "detail": f"verdict={verdict5['work_outcome']}"})

    # ---- R6: PASS without required evidence ----
    bwp6 = load_example("qualification-1-bounded-routine.json")
    assignment6 = build_assignment(bwp6)
    receipt6 = build_receipt(bwp6, assignment6, "T1")
    # drop the hash evidence claim (BWP requires hash)
    receipt6["evidence_claims"] = [c for c in receipt6["evidence_claims"] if "hash" not in c.get("satisfies", [])]
    rv6 = validate_receipt(receipt6, bwp6, assignment6)
    verdict6 = adjudicate(receipt6, bwp6, assignment6)
    r6_ok = any("evidence gap" in e for e in rv6.errors) and verdict6["work_outcome"] == "INDETERMINATE"
    report["rejections"].append({"id": "R6", "label": "PASS without required evidence -> INDETERMINATE",
                                 "ok": r6_ok, "detail": f"verdict={verdict6['work_outcome']}"})

    # ---- R7 (corrected): prose mention of providers/models/resources must NOT
    #      invalidate a structurally valid packet; at most a warning ----
    bwp7 = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp7["provenance"]["description"] = ("Verify that HTPC was not selected and that the flash "
                                         "thinking lane was not used, per the routing observations; "
                                         "also confirm no DeepSeek fallback occurred in the quoted log.")
    iss7 = validate_bwp(bwp7)
    dispatch7 = validate_for_dispatch(bwp7)
    r7_ok = (iss7.valid and dispatch7["allowed"] and len(iss7.warnings) > 0
             and any("prose mentions identity/routing token" in w for w in iss7.warnings))
    report["controls"].append({"id": "R7", "label": "prose mention of identities is warning-only (packet stays valid)",
                               "ok": r7_ok, "detail": f"valid={iss7.valid}, dispatch={dispatch7['allowed']}, warnings={len(iss7.warnings)}"})

    bwp7b = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    del bwp7b["work"]["reasoning_intent"]
    r7b_ok = any("reasoning_intent" in e and "REQUIRED" in e for e in validate_bwp(bwp7b).errors)
    report["rejections"].append({"id": "R7b", "label": "structural metadata cannot be omitted (prose cannot substitute)",
                                 "ok": r7b_ok, "detail": "work.reasoning_intent removed"})

    # ---- R9: ASSIGNMENT must never carry provider/model identity ----
    bwp9 = load_example("qualification-1-bounded-routine.json")
    assignment9 = build_assignment(bwp9)
    assignment9["assigned"]["model"] = "gpt-5.6-luna"
    r9_ok = any("forbidden resource-identity key" in e for e in validate_assignment(assignment9, bwp9).errors)
    report["rejections"].append({"id": "R9", "label": "assignment provider/model identity rejected",
                                 "ok": r9_ok, "detail": "assigned.model='gpt-5.6-luna'"})

    # ---- C1: executor selection is SHAPE-INDEPENDENT (capability+authority only) ----
    bwp_c1a = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp_c1a["work"]["work_shape"] = "agentic"   # bounded task re-labeled agentic: executor must NOT change
    bwp_c1b = json.loads(json.dumps(load_example("qualification-2-agentic-nondeliberate.json")))
    bwp_c1b["work"]["work_shape"] = "bounded"   # agentic task re-labeled bounded: executor must NOT change
    ex1 = select_executor(bwp_c1a)
    ex2 = select_executor(bwp_c1b)
    c1_ok = (ex1["executor"] == "turnstone-native" and ex2["executor"] == "hermes"
             and "work_shape" not in ex1["rationale"].lower() and "work_shape" not in ex2["rationale"].lower())
    report["controls"].append({"id": "C1", "label": "executor selection follows capability/authority, not work_shape",
                               "ok": c1_ok,
                               "detail": f"T1 with work_shape=agentic -> {ex1['executor']}; "
                                         f"T2 with work_shape=bounded -> {ex2['executor']}"})

    # ---- LB-1 fix demos: synonym-key identity smuggling rejected (additionalProperties:false) ----
    bwp10 = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp10["requirements"]["required_model"] = "gpt-5.6-luna"
    r10_ok = any("unknown property" in e for e in validate_bwp(bwp10).errors)
    report["rejections"].append({"id": "R10", "label": "synonym-key identity smuggling rejected (requirements)",
                                 "ok": r10_ok, "detail": "requirements.required_model='gpt-5.6-luna'"})

    bwp10b = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp10b["provenance"]["resource"] = "HTPC"
    r10b_ok = any("unknown property" in e for e in validate_bwp(bwp10b).errors)
    report["rejections"].append({"id": "R10b", "label": "synonym-key identity smuggling rejected (provenance)",
                                 "ok": r10b_ok, "detail": "provenance.resource='HTPC'"})

    # Depth-1 LB-1 fix demos (Hermes confirmation EXT-1..6): nested synonym keys rejected
    bwp10c = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp10c["requirements"]["context"]["required_model"] = "gpt-5.6-luna"
    r10c_ok = any("unknown property" in e for e in validate_bwp(bwp10c).errors)
    report["rejections"].append({"id": "R10c", "label": "nested synonym-key identity smuggling rejected (requirements.context)",
                                 "ok": r10c_ok, "detail": "requirements.context.required_model='gpt-5.6-luna'"})

    bwp10d = load_example("qualification-1-bounded-routine.json")
    assignment10d = build_assignment(bwp10d)
    assignment10d["assigned"]["required_model"] = "gpt-5.6-luna"
    r10d_ok = any("unknown property" in e for e in validate_assignment(assignment10d, bwp10d).errors)
    report["rejections"].append({"id": "R10d", "label": "nested synonym-key identity smuggling rejected (assignment.assigned)",
                                 "ok": r10d_ok, "detail": "assigned.required_model='gpt-5.6-luna'"})

    bwp10e = load_example("qualification-1-bounded-routine.json")
    assignment10e = build_assignment(bwp10e)
    receipt10e = build_receipt(bwp10e, assignment10e, "T1")
    receipt10e["required_model"] = "gpt-5.6-luna"
    r10e_ok = any("unknown property" in e for e in validate_receipt(receipt10e, bwp10e, assignment10e).errors)
    report["rejections"].append({"id": "R10e", "label": "unknown top-level key rejected in receipt",
                                 "ok": r10e_ok, "detail": "receipt.required_model='gpt-5.6-luna'"})

    # ---- LB-2 fix demos: blocking INDETERMINATE + hard-constraint violation ----
    bwp11 = load_example("qualification-1-bounded-routine.json")
    assignment11 = build_assignment(bwp11)
    receipt11 = build_receipt(bwp11, assignment11, "T1")
    receipt11["evidence_claims"].append({"claim": "material claim cannot be verified",
                                         "status": "INDETERMINATE", "satisfies": []})
    verdict11 = adjudicate(receipt11, bwp11, assignment11)
    r11_ok = (verdict11["work_outcome"] == "INDETERMINATE"
              and verdict11["basis"]["evidence_sufficiency"] == "INSUFFICIENT")
    report["rejections"].append({"id": "R11", "label": "blocking INDETERMINATE claim -> INDETERMINATE",
                                 "ok": r11_ok, "detail": f"verdict={verdict11['work_outcome']}"})

    bwp12 = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp12["requirements"]["inference_locality"] = "local_required"
    assignment12 = build_assignment(bwp12)
    receipt12 = build_receipt(bwp12, assignment12, "T1")
    # PROVEN hosted locality (not provider/model presence) => hard-constraint violation
    receipt12["resource_observed"]["inference_resource"] = {"provider": "openai", "model": "gpt-5.6-luna", "locality": "hosted"}
    verdict12 = adjudicate(receipt12, bwp12, assignment12)
    r12_ok = (verdict12["work_outcome"] == "FAIL"
              and verdict12["basis"]["hard_constraint_status"] == "VIOLATED")
    report["rejections"].append({"id": "R12", "label": "hard-constraint violation (local_required + PROVEN hosted locality) -> FAIL",
                                 "ok": r12_ok,
                                 "detail": f"verdict={verdict12['work_outcome']}, hard_constraint_status={verdict12['basis']['hard_constraint_status']}"})

    # ---- R13: local_required + unknown/unobservable locality -> INDETERMINATE (not FAIL) ----
    bwp13 = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp13["requirements"]["inference_locality"] = "local_required"
    assignment13 = build_assignment(bwp13)
    receipt13 = build_receipt(bwp13, assignment13, "T1")
    receipt13["resource_observed"]["inference_resource"] = {"provider": "openai", "model": "gpt-5.6-luna", "locality": "unknown"}
    verdict13 = adjudicate(receipt13, bwp13, assignment13)
    r13_ok = (verdict13["work_outcome"] == "INDETERMINATE"
              and verdict13["basis"]["hard_constraint_status"] == "UNVERIFIABLE")
    report["rejections"].append({"id": "R13", "label": "local_required + unknown locality -> INDETERMINATE (never inferred FAIL)",
                                 "ok": r13_ok,
                                 "detail": f"verdict={verdict13['work_outcome']}, hard_constraint_status={verdict13['basis']['hard_constraint_status']}"})

    # ---- R14: provider/model names alone never establish locality (no locality field) ----
    bwp14 = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp14["requirements"]["inference_locality"] = "local_required"
    assignment14 = build_assignment(bwp14)
    receipt14 = build_receipt(bwp14, assignment14, "T1")
    receipt14["resource_observed"]["inference_resource"] = {"provider": "openai", "model": "gpt-5.6-luna"}  # no locality field
    verdict14 = adjudicate(receipt14, bwp14, assignment14)
    r14_ok = (verdict14["work_outcome"] == "INDETERMINATE"
              and verdict14["basis"]["hard_constraint_status"] == "UNVERIFIABLE")
    report["rejections"].append({"id": "R14", "label": "provider/model presence alone cannot prove locality -> INDETERMINATE",
                                 "ok": r14_ok,
                                 "detail": f"verdict={verdict14['work_outcome']} (no locality fact observed)"})

    # ---- R15: one NOT_MET criterion -> FAIL ----
    bwp15 = load_example("qualification-1-bounded-routine.json")
    assignment15 = build_assignment(bwp15)
    receipt15 = build_receipt(bwp15, assignment15, "T1")
    receipt15["evidence_claims"].append({"claim": "criterion 0 failed", "status": "FAILED",
                                          "satisfies": [], "criterion_refs": ["0"]})
    verdict15 = adjudicate(receipt15, bwp15, assignment15)
    r15_ok = (verdict15["work_outcome"] == "FAIL"
              and any(a["status"] == "NOT_MET" for a in verdict15["basis"]["acceptance"]))
    report["rejections"].append({"id": "R15", "label": "one NOT_MET criterion -> FAIL",
                                 "ok": r15_ok, "detail": f"verdict={verdict15['work_outcome']}"})

    # ---- R16: one INDETERMINATE required criterion (no positive PROVEN ref) -> INDETERMINATE ----
    bwp16 = load_example("qualification-1-bounded-routine.json")
    assignment16 = build_assignment(bwp16)
    receipt16 = build_receipt(bwp16, assignment16, "T1")
    # remove criterion_refs from all claims: no criterion positively adjudicated
    for cl in receipt16["evidence_claims"]:
        cl.pop("criterion_refs", None)
    verdict16 = adjudicate(receipt16, bwp16, assignment16)
    r16_ok = (verdict16["work_outcome"] == "INDETERMINATE"
              and all(a["status"] == "INDETERMINATE" for a in verdict16["basis"]["acceptance"]))
    report["rejections"].append({"id": "R16", "label": "INDETERMINATE required criterion (no positive adjudication) -> INDETERMINATE",
                                 "ok": r16_ok, "detail": f"verdict={verdict16['work_outcome']}"})

    # ---- R17: absence of FAILED claims alone cannot produce MET (R16 is the proof; explicit assert) ----
    r17_ok = r16_ok and verdict16["work_outcome"] == "INDETERMINATE"
    report["rejections"].append({"id": "R17", "label": "absence of FAILED claims alone cannot make criteria MET",
                                 "ok": r17_ok, "detail": "no FAILED claim present, yet verdict != PASS"})

    # ---- R18: executor self-assessment claiming success cannot force PASS ----
    bwp18 = load_example("qualification-1-bounded-routine.json")
    assignment18 = build_assignment(bwp18)
    receipt18 = build_receipt(bwp18, assignment18, "T1")
    receipt18["acceptance_self_assessment"]["claimed"] = True
    for cl in receipt18["evidence_claims"]:
        cl.pop("criterion_refs", None)  # self-assessment alone, no PROVEN adjudication refs
    verdict18 = adjudicate(receipt18, bwp18, assignment18)
    r18_ok = (verdict18["work_outcome"] == "INDETERMINATE")
    report["rejections"].append({"id": "R18", "label": "executor self-assessment cannot force PASS",
                                 "ok": r18_ok, "detail": f"verdict={verdict18['work_outcome']} despite self-assessment claimed=True"})

    # ---- R19: unknown property nested under nullable resource_observed rejected ----
    bwp19 = load_example("qualification-1-bounded-routine.json")
    assignment19 = build_assignment(bwp19)
    receipt19 = build_receipt(bwp19, assignment19, "T1")
    receipt19["resource_observed"]["required_model"] = "gpt-5.6-luna"
    r19_ok = any("unknown property" in e for e in validate_receipt(receipt19, bwp19, assignment19).errors)
    report["rejections"].append({"id": "R19", "label": "unknown nested property under nullable resource_observed rejected",
                                 "ok": r19_ok, "detail": "resource_observed.required_model"})

    # ---- R20: unknown property nested under inference_resource rejected ----
    bwp20 = load_example("qualification-1-bounded-routine.json")
    assignment20 = build_assignment(bwp20)
    receipt20 = build_receipt(bwp20, assignment20, "T1")
    receipt20["resource_observed"]["inference_resource"] = {"locality": "local", "required_model": "gpt-5.6-luna"}
    r20_ok = any("unknown property" in e for e in validate_receipt(receipt20, bwp20, assignment20).errors)
    report["rejections"].append({"id": "R20", "label": "unknown property under nested inference_resource rejected",
                                 "ok": r20_ok, "detail": "inference_resource.required_model"})

    # ---- C3: ESCALATED outcome is reachable and NOT_EVALUATED (review finding N4) ----
    bwp_c3 = load_example("qualification-1-bounded-routine.json")
    assignment_c3 = build_assignment(bwp_c3)
    receipt_c3 = build_receipt(bwp_c3, assignment_c3, "T1")
    verdict_c3 = adjudicate(receipt_c3, bwp_c3, assignment_c3, escalated=True)
    c3_ok = (verdict_c3["work_outcome"] == "ESCALATED"
             and verdict_c3["basis"]["hard_constraint_status"] == "NOT_EVALUATED")
    report["controls"].append({"id": "C3", "label": "ESCALATED outcome reachable (operator-gated)",
                               "ok": c3_ok, "detail": f"verdict={verdict_c3['work_outcome']}"})

    # ---- C4: inference_locality does NOT select the executor (direct-review correction 1) ----
    bwp_c4a = load_example("qualification-1-bounded-routine.json")
    bwp_c4b = json.loads(json.dumps(bwp_c4a))
    bwp_c4a["requirements"]["inference_locality"] = "local_required"
    bwp_c4b["requirements"]["inference_locality"] = "hosted_allowed"
    ex_a = select_executor(bwp_c4a)
    ex_b = select_executor(bwp_c4b)
    c4_ok = (ex_a["executor"] == ex_b["executor"]
             and "locality" not in ex_a["rationale"].lower()
             and "locality" not in ex_b["rationale"].lower())
    report["controls"].append({"id": "C4", "label": "inference_locality does not select executor",
                               "ok": c4_ok,
                               "detail": f"local_required -> {ex_a['executor']}; hosted_allowed -> {ex_b['executor']}"})

    # ---- C5: executor assignment never rewrites/contains inference locality ----
    bwp_c5 = load_example("qualification-1-bounded-routine.json")
    assignment_c5 = build_assignment(bwp_c5)
    ser = json.dumps(assignment_c5)
    c5_ok = ("inference_locality" not in ser and "locality" not in ser)
    report["controls"].append({"id": "C5", "label": "executor assignment does not carry or rewrite inference locality",
                               "ok": c5_ok, "detail": "assignment contains no locality fields"})

    # ---- C6: local_required + PROVEN local resource is compliant (PASS).
    # Adjudication-semantics demo (receipt observed-locality rules). NOTE: this
    # packet would FAIL the v1 dispatch gate (inference_locality=local_required is
    # UNSUPPORTED_V1) — it exercises adjudication semantics only, not dispatch.
    bwp_c6 = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp_c6["requirements"]["inference_locality"] = "local_required"  # override: adjudication-only
    assignment_c6 = build_assignment(bwp_c6)
    receipt_c6 = build_receipt(bwp_c6, assignment_c6, "T1")  # build_receipt sets observed locality=local
    verdict_c6 = adjudicate(receipt_c6, bwp_c6, assignment_c6)
    c6_ok = (verdict_c6["work_outcome"] == "PASS"
             and verdict_c6["basis"]["hard_constraint_status"] == "SATISFIED")
    report["controls"].append({"id": "C6", "label": "local_required + PROVEN local resource compliant (adjudication semantics)",
                               "ok": c6_ok, "detail": f"verdict={verdict_c6['work_outcome']}"})

    # ---- C7: provider/model identity alone never proves locality (local + hosted both valid facts) ----
    bwp_c7 = json.loads(json.dumps(load_example("qualification-1-bounded-routine.json")))
    bwp_c7["requirements"]["inference_locality"] = "local_required"  # adjudication-only override
    assignment_c7 = build_assignment(bwp_c7)
    receipt_c7 = build_receipt(bwp_c7, assignment_c7, "T1")
    receipt_c7["resource_observed"]["inference_resource"] = {"provider": "comfy", "model": "qwen", "locality": "local"}
    verdict_c7 = adjudicate(receipt_c7, bwp_c7, assignment_c7)
    c7_ok = (verdict_c7["work_outcome"] == "PASS"
             and verdict_c7["basis"]["hard_constraint_status"] == "SATISFIED")
    report["controls"].append({"id": "C7", "label": "local provider/model with PROVEN local locality is compliant (names never prove locality)",
                               "ok": c7_ok, "detail": f"verdict={verdict_c7['work_outcome']}"})

    # ---- C8: PASS requires all criteria explicitly MET + evidence sufficient ----
    bwp_c8 = load_example("qualification-1-bounded-routine.json")
    assignment_c8 = build_assignment(bwp_c8)
    receipt_c8 = build_receipt(bwp_c8, assignment_c8, "T1")
    verdict_c8 = adjudicate(receipt_c8, bwp_c8, assignment_c8)
    c8_ok = (verdict_c8["work_outcome"] == "PASS"
             and all(a["status"] == "MET" for a in verdict_c8["basis"]["acceptance"]))
    report["controls"].append({"id": "C8", "label": "PASS requires all criteria MET + evidence sufficient",
                               "ok": c8_ok, "detail": f"verdict={verdict_c8['work_outcome']}"})

    # ---- C9: valid nullable resource_observed = null accepted ----
    bwp_c9 = json.loads(json.dumps(load_example("qualification-2-agentic-nondeliberate.json")))
    assignment_c9 = build_assignment(bwp_c9)
    receipt_c9 = build_receipt(bwp_c9, assignment_c9, "T2")
    receipt_c9["resource_observed"] = None
    c9_ok = validate_receipt(receipt_c9, bwp_c9, assignment_c9).valid
    report["controls"].append({"id": "C9", "label": "valid nullable resource_observed=null accepted",
                               "ok": c9_ok, "detail": f"receipt_valid={c9_ok}"})

    # ---- F1-F4: UNSUPPORTED_V1 hard requirements FAIL CLOSED before dispatch ----
    base_f = load_example("qualification-1-bounded-routine.json")

    bwp_f1 = json.loads(json.dumps(base_f))
    bwp_f1["requirements"]["inference_locality"] = "local_required"
    d_f1 = validate_for_dispatch(bwp_f1)
    f1_ok = (not d_f1["allowed"] and d_f1["disposition"] == "REJECT"
             and d_f1["reason"].startswith("UNSUPPORTED_V1") and "inference_locality" in d_f1["reason"])
    report["rejections"].append({"id": "F1", "label": "inference_locality=local_required FAILS CLOSED (UNSUPPORTED_V1, no pre-selection ingress)",
                                 "ok": f1_ok, "detail": d_f1["reason"]})

    bwp_f2 = json.loads(json.dumps(base_f))
    bwp_f2["requirements"]["inference_locality"] = "hosted_allowed"
    d_f2 = validate_for_dispatch(bwp_f2)
    f2_ok = (not d_f2["allowed"] and d_f2["disposition"] == "REJECT"
             and d_f2["reason"].startswith("UNSUPPORTED_V1"))
    report["rejections"].append({"id": "F2", "label": "inference_locality=hosted_allowed FAILS CLOSED (UNSUPPORTED_V1)",
                                 "ok": f2_ok, "detail": d_f2["reason"]})

    bwp_f3 = json.loads(json.dumps(base_f))
    bwp_f3["requirements"]["context"]["context_size_requirement"] = 200000
    d_f3 = validate_for_dispatch(bwp_f3)
    f3_ok = (not d_f3["allowed"] and d_f3["disposition"] == "REJECT"
             and d_f3["reason"].startswith("UNSUPPORTED_V1") and "context_size_requirement" in d_f3["reason"])
    report["rejections"].append({"id": "F3", "label": "context_size_requirement non-null FAILS CLOSED (UNSUPPORTED_V1)",
                                 "ok": f3_ok, "detail": d_f3["reason"]})

    bwp_f4 = json.loads(json.dumps(base_f))
    bwp_f4["requirements"]["output_budget"] = 4000
    d_f4 = validate_for_dispatch(bwp_f4)
    f4_ok = (not d_f4["allowed"] and d_f4["disposition"] == "REJECT"
             and d_f4["reason"].startswith("UNSUPPORTED_V1") and "output_budget" in d_f4["reason"])
    report["rejections"].append({"id": "F4", "label": "output_budget non-null FAILS CLOSED (UNSUPPORTED_V1)",
                                 "ok": f4_ok, "detail": d_f4["reason"]})

    # ---- C10: no provider/model/resource-selection logic added to Turnstone ----
    # select_executor/derive_executor_candidates return executor lanes only, never
    # provider/model/resource identities; supported hard fields (work_shape,
    # reasoning_intent) are the only ENFORCED_NOW ingress fields.
    c10_ok = True
    for label, fname in examples:
        b = load_example(fname)
        ex = select_executor(b)
        c10_ok = c10_ok and ex["executor"] in EXECUTORS and ex["executor"] is not None
    # Static: derive_executor_candidates/select_executor never reference forbidden keys
    # in CODE (docstrings excluded — they legitimately mention provider/model to
    # document what the functions must NOT do).
    import inspect as _inspect, re as _re
    src = _inspect.getsource(derive_executor_candidates) + _inspect.getsource(select_executor)
    src_code = _re.sub(r'""".*?"""', "", src, flags=_re.DOTALL)  # strip docstrings
    # Forbidden identity keys as standalone identifiers (word-boundary), so semantic
    # capability names like gpu_workload_execution do not false-positive.
    c10_ok = c10_ok and not any(_re.search(rf"\b{_re.escape(k)}\b", src_code) for k in FORBIDDEN_KEYS)
    report["controls"].append({"id": "C10", "label": "no provider/model/resource-selection logic added to Turnstone",
                               "ok": c10_ok,
                               "detail": "executor functions return lanes only; forbidden identity keys absent from executor code"})

    # ---- select_executor sanity (no capability gap -> never assigns operator-only or None) ----
    for label, fname in examples:
        b = load_example(fname)
        lane = select_executor(b)
        if lane["executor"] is None:
            report["ok"] = False

    report["ok"] = (report["ok"] and all(r["ok"] for r in report["rejections"])
                    and all(c["ok"] for c in report["controls"]))
    return report


def build_receipt(bwp: dict, assignment: dict, label: str) -> dict:
    """Fixture receipt for a positive example: claims satisfy the BWP evidence
    requirements with PROVEN status and reference ALL acceptance criteria; actions
    stay inside allowed_actions; observed inference locality is set to 'local' when
    the packet requires local inference (else null)."""
    allowed = bwp["authority"]["allowed_actions"]
    n_criteria = len(bwp["acceptance"]["criteria"])
    criterion_refs = [str(i) for i in range(n_criteria)]
    claims = []
    for ev in bwp["evidence"]["requirements"]:
        claims.append({"claim": f"{label}: evidence '{ev}' satisfied by read-back/verification",
                       "status": "PROVEN", "satisfies": [ev],
                       "criterion_refs": list(criterion_refs),
                       "evidence_ref": f"receipt/{bwp['packet_id']}/{ev}"})
    inf = None
    if bwp["requirements"].get("inference_locality") == "local_required":
        inf = {"locality": "local"}
    return {
        "schema_version": "0.1",
        "kind": "bwp-evidence-receipt",
        "packet_id": bwp["packet_id"],
        "correlation_id": bwp["provenance"]["correlation_id"],
        "workstream_id": bwp["provenance"]["workstream_id"],
        "received_at": "2026-08-22T18:00:00+07:00",
        "executor": {"declared_executor": assignment["assigned"]["executor"],
                     "actual_executor": assignment["assigned"]["executor"]},
        "execution": {
            "actions_taken": [a for a in allowed if a in ("read_files", "run_commands", "write_shared_workspace",
                                                          "access_network", "retrieve_memory", "execute_code",
                                                          "inspect_remote_hosts", "code_execution")],
            "artifacts_produced": [{"path": f"/home/vincent/shared-workspace/operations/fgv1/{bwp['packet_id']}.md",
                                    "hash_sha256": "ab" * 32}],
            "run_ids": assignment["assigned"]["run_correlation_ids"],
            "ledger_refs": [],
        },
        "resource_observed": {
            "inference_resource": inf,
            "routing_evidence_ref": "routing.jsonl#fixture",
            "reasoning_tokens": None,
            "note": "Observed from routing telemetry/journal/logs. Never an input to the BWP; never inferred when unobservable.",
        },
        "failures": [],
        "evidence_claims": claims,
        "acceptance_self_assessment": {"claimed": True, "per_criterion": bwp["acceptance"]["criteria"]},
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    as_json = "--json" in sys.argv
    report = run_self_test()
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print("Fleet Governance v1 — Validator self-test")
        print("=" * 70)
        print("Qualification examples:")
        for e in report["examples"]:
            print(f"  [{'OK ' if e['ok'] else 'FAIL'}] {e['label']}: bwp_valid={e['bwp_valid']} "
                  f"dispatch={e['dispatch']['allowed']} assignment={e['assignment_valid']} "
                  f"receipt={e['receipt_valid']} verdict={e['verdict']}")
        print("Rejection demonstrations (must fail closed):")
        for r in report["rejections"]:
            print(f"  [{'OK ' if r['ok'] else 'FAIL'}] {r['id']}: {r['label']} — {r['detail']}")
        print("Positive controls (must remain valid / shape-independent):")
        for c in report["controls"]:
            print(f"  [{'OK ' if c['ok'] else 'FAIL'}] {c['id']}: {c['label']} — {c['detail']}")
        print("=" * 70)
        print("RESULT:", "ALL PASS" if report["ok"] else "FAILURES PRESENT")
        for e in report["examples"]:
            if not e["ok"]:
                for err in e["errors"]:
                    print("  ERR:", err)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
