# The S6 deployment hash chain — what it covers, and what it does not

> **Status: NOT DEPLOYED.** The corrected chain is staged. The deployed
> driver pins six artifacts and trusts a seventh.

## 1. The defect

The operator driver verifies the SHA-256 of every artifact it is about to
execute, **before** running anything. The operator's own inline check reads
those same pins:

```bash
sha256sum -c SHA256SUMS
```

But the two reporters **load a third file at runtime**, by path:

```python
spec = importlib.util.spec_from_file_location(
    "s6prov", pathlib.Path(__file__).resolve().parent / "provenance.py")
```

`provenance.py` was **not pinned at all**.

A pin that is declared but never verified certifies nothing: the operator's
check would have passed while the trusted file was never compared to
anything. The static half of the driver proof listed six hash pairs and
never looked at the seventh, so it passed cleanly against a driver whose
`PROVENANCE_SHA` had been corrupted to zeros.

This is the *transitive* form of the same mistake. The chain covered the
files that were **executed** and missed the file that was **loaded**.

## 2. The corrected chain

Three axes, each with a check that fails when violated:

| axis | check | failure it prevents |
|---|---|---|
| declared vs verified | every `*_SHA` constant has a `verify_one` | a pin that certifies nothing |
| verified vs present | every `verify_one` names a file that exists | a chain that stops early, silently |
| loaded vs pinned | the reporters' transitive runtime-loader closure is pinned | trusted code the chain never looks at |

### 2.1 The closure is computed, not maintained

`tools/s6/driver/chain_closure.py` walks the **real**
`spec_from_file_location` calls from the reporters, so the closure is the
actual set of locally loaded files rather than a hand-maintained list that
drifts when someone adds a module.

Today the closure is:

```
collect.py
liveness.py
provenance.py
```

The proof asserts that every file in that closure is pinned by the driver. If
a fourth sibling is ever loaded at runtime, the proof **fails** until it is
pinned.

## 3. The negative control

A proof is only worth what it can catch, so
`tools/s6/driver/script-tests-execute-chain.sh` is a **negative control**.
It mutates a copy of the driver in a disposable sandbox and requires the
static half to report a specific failure.

| case | caught? |
|---|---|
| each of the seven pins corrupted, one at a time | yes |
| a pinned artifact whose bytes drifted under a correct pin | yes |
| a pin declared but never verified | yes |
| a `verify_one` for a file that is not there | yes |
| a stale announced artifact count | yes |

**13 checks, 0 failed.** The control was **red** (7 pass / 6 fail) against
the pre-fix proof and **green** (13/13) after. Before it existed, the static
half was blind to the exact defect in §1.

## 4. What this covers — and what it does not

### 4.1 Covered

- the seven local artifacts the driver executes or loads:
  the 444-entry manifest, the slot installer, the activation script, the
  rollback script, `collect.py`, `liveness.py`, `provenance.py`;
- the dynamic loader edges between them, computed from the real loader
  calls;
- the correspondence between a pin and the bytes on disk, for the operator's
  own inline check.

### 4.2 NOT covered

This chain is **not** a supply-chain attestation. It does **not** verify:

- any external library, Python dependency, or transitive package;
- the interpreter, the standard library, or the operating system;
- the venv's installed distributions or their integrity;
- systemd, the kernel, or any other platform component;
- the database, the decision backend, or any network peer;
- the *behaviour* of a pinned file — a pinned file that contains a bug is
  still pinned.

Those are trusted by the **platform**, not by this chain. The chain's job is
narrower and worth stating plainly: it proves that the files this
deployment executes and loads are **the files the operator reviewed**, and
that nothing was added to that set without the proof noticing.

### 4.3 The "do not re-run the driver" rule

The driver exists to **roll services**. It is not a reporting tool, and
re-running it because a hash changed during reporting development would
mutate production for no reason. The chain is verified by the **proof**,
which is read-only, sandboxed, and touches nothing.

Reporting-only work re-runs `collect.py` and `liveness.py` as the service
account, with a before/after snapshot of the epoch tree to prove they
changed nothing.

## 5. The artifacts are committed byte-identical

The driver, the installer, the activation script, the rollback script and
the 444-entry manifest are committed exactly as deployed, so the pins in the
driver still describe the files on disk. The chain is therefore verifiable
from a checkout alone.
