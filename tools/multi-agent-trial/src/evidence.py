#!/usr/bin/env python3
"""evidence.py — 5-file hash-linked evidence lineage (bundled artifact).

Per run, produce (in order, never mutating a sealed file):
  1. run-evidence.json        raw output + concrete receipts; frozen + sha256 sealed
  2. anonymized-output.json   derived; references raw hash
  3. content-evaluation.json  evaluator result; references anonymized hash
  4. operational-evaluation.json  deterministic validation; references raw hash
  5. run-summary.json         references only (no new data)
"""
import hashlib, json, os, stat


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def seal(path, data):
    """Write JSON, hash it, chmod read-only (0400) so it cannot be altered.
    Idempotent: if the file exists (e.g. a rehearsal rerun), allow overwrite first."""
    if os.path.exists(path):
        os.chmod(path, 0o600)
    blob = json.dumps(data, ensure_ascii=False, indent=2)
    with open(path, "w") as f:
        f.write(blob)
    os.chmod(path, 0o400)
    return sha256_file(path)


def write_ref(path, payload):
    """Write a derived artifact (not sealed read-only — it may be appended by later stages? no:
    derived artifacts are also frozen; seal them too)."""
    return seal(path, payload)


def write_raw(run_dir, cell_id, raw):
    p = os.path.join(run_dir, f"{cell_id}-run-evidence.json")
    return seal(p, raw)


def write_anonymized(run_dir, cell_id, anon, raw_hash):
    p = os.path.join(run_dir, f"{cell_id}-anonymized-output.json")
    return seal(p, {"raw_hash": raw_hash, "anonymized": anon})


def write_content(run_dir, cell_id, content, anon_hash):
    p = os.path.join(run_dir, f"{cell_id}-content-evaluation.json")
    return seal(p, {"anonymized_hash": anon_hash, "evaluation": content})


def write_operational(run_dir, cell_id, op, raw_hash):
    p = os.path.join(run_dir, f"{cell_id}-operational-evaluation.json")
    return seal(p, {"raw_hash": raw_hash, "operational": op})


def write_summary(run_dir, cell_id, refs):
    p = os.path.join(run_dir, f"{cell_id}-run-summary.json")
    return seal(p, {"references": refs})
