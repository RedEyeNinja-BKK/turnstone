# Switchyard — deployed provenance and divergence

Reference for the Switchyard running on `switchyard-vm`. Measured 2026-10-17.

> **The deployed Switchyard is not upstream `v0.3.0`.** It reports `0.3.0`
> because that is the crate version in both trees, but its commit is a fork
> head that upstream does not contain. Anything read from upstream `main` or
> the `v0.3.0` tag must be checked against the fork before acting.

---

## 1. Deployed identity

| Fact | Value |
|---|---|
| Host | `switchyard-vm` (`100.65.106.53`) |
| Unit | `switchyard-neutral-green.service` (`User=switchyard`) |
| Binary | `/opt/switchyard/bin/switchyard-server` |
| Binary sha256 (deployed) | `b183a87157eaf1917e61601343a84df2fb08be4b20f959d562d8b75cf4b1196c` (media gate; baseline was `74963b1d57c664…`) |
| Build id | `7f4d76212f363150ad13382fedb27cafec9c06da` |
| Reported version | `switchyard-server 0.3.0` |
| Config | `/etc/switchyard/routes.toml` sha256 `7b8445b20346670fa712f1e2b82b81730126df738054e6aef5340ea5dbcc326f` (V7.2; was `9a6448ef…` before the rerank `top_n` change) |
| Source of record | `RedEyeNinja-BKK/Switchyard`, branch `v6-decision-planes-20261005` |
| Source commit | `6de85da15b68294cfda1775a1afa6e30ebd3cf16` (2026-10-05), branch `fix/decision-named-lane-composition-20261004` |
| Fork on GitHub | `RedEyeNinja-BKK/Switchyard`, branch `v6-decision-planes-20261005` |
| Built with | rustc `1.99.0 (b940084d7 2026-09-28)`, Ubuntu 24.04, GCC 13.3 — reproduces this hash bitwise |
| Decision | `fix(decision): an unaddressed request is served by the DECLARED default lane` |
| Bind | `127.0.0.1:4000`, published on the tailnet TLS listener (`:443`) |
| Started | restarted 2026-10-18 00:39 +07 for the media gate |

`--version` prints the crate version only; no commit hash is embedded. The two
40-hex strings the image does carry are toolchain identifiers, not revisions:
`b940084d7…` is the rustc commit for 1.99.0 and `1b9c0d5f…` is LLD 23.1.1.
They identify the build environment, not the source, so **record the source
commit, not just `0.3.0`, in any deployment receipt**.

### The local source bundle does not match this binary

Measured 2026-10-17. The git bundle at
`/var/lib/turnstone/switchyard-upstream/fork/src.bundle` (HEAD `2c6e6df5`, 543
commits) is **not** the tree this binary was built from:

| Check | Deployed binary | Bundle-built binary |
|---|---|---|
| Accepts `[decision_lane_composition]` from the live config | yes | **no** — `unknown field` |
| `decision_lane_composition` present in the image | yes (4 occurrences) | **no** (0) |
| `--dry-run` on the live config | passes | fails |
| sha256 | `74963b1d57c664…` | `2c7061ad987738…` |

So the bundle predates at least one fork commit that the deployed binary
contains. Treat the bundle as a *reference for behaviour*, never as the
build input for a replacement artifact.

### The cited `production-src` is a clone of the same bundle

Measured 2026-10-17. `/home/vincent/switchyard-migration/production-src` was
believed to be the authoritative tree. It is not:

| Check | Result |
|---|---|
| `git reflog` | `clone: from /home/vincent/switchyard-migration/switchyard-production-source.bundle` |
| HEAD | `2c6e6df5`, detached, 543 commits — the bundle's HEAD |
| `git status --porcelain` | empty; no local modifications, no stashes |
| `git for-each-ref` | no branches, no tags |
| `git fsck --unreachable` | no unreachable commits |
| `decision_lane_composition` in the tree | absent (0 files) |

Both artifacts carry the same content: the bundle's sha256 is
`cd58531b88110e1d805681bf8be28c27d88d1d8c6663cc753ac7ddc282a23ca3`, matching the
checksum recorded when it was staged.

The gap is now exact rather than "at least one commit". `DeploymentConfig` in
`crates/switchyard-runner/src/config.rs` declares `schema_version`,
`fallback_client`, `llm_clients`, `targets`, `routes`, `capability_clients`,
`capabilities`, `decision_lanes`, `composites`. The deployed binary's struct
also understands `default_decision_lane` and `decision_lane_composition`, and
embeds `validate_decision_lane` / `validate_default_decision_lane`, which the
source has no equivalent of. Decision *lanes* exist in the source (36
references); only those two fields and their behaviour are new.

### Where the deployed binary actually came from

`switchyard-vm` never compiled this project. There is no `cargo`, `rustc`,
`~/.cargo`, `~/.rustup` or `target/` directory on the host. `~/.bash_history`
shows the deployment was an unpack-and-install of a prebuilt tarball:
`switchyard-runtime.tgz` (sha256 `2dd21ecf42a3…`) →
`runtime-unpacked/opt/switchyard/bin/switchyard-server` → `sudo install` into
`/opt/switchyard/bin/`. All four copies on disk are byte-identical
(`74963b1d…`), except the `decision-lanes-20261005T033957Z` rollback anchor
(`2de55530…`), which is the binary that preceded the lane feature.

The tarball contains no sources and records no commit: the only provenance
file inside it, `PRE-STATE`, lists hashes, the unit fragment and the argv the
process was started with.

### The source of record

The source for any change to this deployment is the fork at
`RedEyeNinja-BKK/Switchyard`, branch `v6-decision-planes-20261005`, at
`6de85da15b68294cfda1775a1afa6e30ebd3cf16`. That branch is the artifact this
work is based on and the only one that should be treated as authoritative.

| Fact | Value |
|---|---|
| Branch | `v6-decision-planes-20261005` |
| HEAD | `6de85da15b68294cfda1775a1afa6e30ebd3cf16` |
| Cargo.lock | `5262f62deb3ca69e7900ea3b89dd68af5e857bd3a3c3efe6cc66d03cb7e61f16` |
| Toolchain pin | `rust-toolchain.toml` → `1.99` |

The six commits between `2c6e6df5` and `6de85da1` are the missing work, and
they touch exactly five files: `config.rs`, `decision_executor.rs`,
`runner.rs`, `tests/decision_lane_naming.rs`, and `decision_surface.rs`.
`default_decision_lane` and `decision_lane_composition` appear in `config.rs`.

Provenance for the earlier history of the tree is not established here. The
deployment arrived as a prebuilt tarball carrying no sources and no commit
(see below), so the correspondence between `6de85da1` and the running binary
rests on the reproduction in the next section, not on a recorded build.

### Reproduction is bitwise

`git archive` of `6de85da1`, built with rustc **1.99.0**
(`b940084d7 2026-09-28`) on Ubuntu 24.04 with GCC 13.3, reproduces the
baseline deployed binary **exactly**:

```
74963b1d57c664172175f820f2b94c8ad790629feb2965538c9a09983387d147
```

Recipe: `cargo build --release --locked -p switchyard-server` from a checkout
at `6de85da1`. Bitwise identity depends on all four: the commit, that rustc
release, that GCC, and the crate-registry path. Building the same source on
`agent-vm` (rustc 1.99.1, Debian GCC 14.2) compiles and passes every test but
yields a different hash — 59,600 bytes larger, with `.text` differing by 0.15%
and every crate disambiguator different. That is a toolchain difference, not a
source difference.

`rust-toolchain.toml` names the channel `1.99`, which now resolves to 1.99.1.
A build today therefore does **not** reproduce the deployed hash even from the
same commit. Build with `cargo +1.99.0` until that pin is made exact.

`switchyard-vm` itself never compiled the project: no `cargo`, `rustc`,
`~/.cargo`, `~/.rustup` or `target/` exists there. `~/.bash_history` shows an
unpack-and-install of a prebuilt tarball, `switchyard-runtime.tgz`
(sha256 `2dd21ecf42a3…`) → `runtime-unpacked/…/switchyard-server` → `sudo install`.
All four copies on that host are byte-identical (`74963b1d…`) except the
`decision-lanes-20261005T033957Z` rollback anchor (`2de55530…`), which is the
binary that preceded the lane feature. The tarball carries no sources and no
commit; its only provenance file, `PRE-STATE`, lists hashes and the unit argv.

### Consequence for the stale bundle

A rebuild from the bundle — or from the `production-src` clone on
`switchyard-vm`, which is the same tree — cannot be deployed: it rejects the
live manifest at startup (`unknown field decision_lane_composition`). Use
`6de85da1` from the fork for any source change.

---

## 2. Divergence from upstream

Upstream studied: `NVIDIA-NeMo/Switchyard`, default branch `main`, HEAD
`6f4e7749083a86484a0140078a42d487aed7259e` (2026-10-16, 583 commits, 470-model
context irrelevant here). Licence Apache-2.0.

| | |
|---|---|
| Merge base | `16cbe5939937f3106bc5f13d9b11cdfa64f3242a` (2026-10-01, #900) |
| Fork-only commits | **28** |
| Upstream-only commits | **68** |
| Upstream `v0.3.0` tag | `336196f6fbfc97ddc71c1700f6092e564e9f23c2` — **not an ancestor of the deployment** |

The fork is upstream at the merge base plus a local feature branch. Upstream
then advanced 68 commits, including the `v0.3.0` release, which the deployment
does not contain.

### 2.1 Local features with no upstream equivalent

Verified by grepping both trees. None of these strings appears anywhere in
upstream:

| Local concept | Upstream |
|---|---|
| `composites` (whole-route tiers) | absent |
| `decision_lanes`, `decision_lane_composition` | absent |
| `route_reasoning_policy` | absent |
| `escalation_max_input_tokens`, `escalation_target` | absent |
| Typed `/v1/decisions` | absent (upstream has `/v1/decision` only) |
| Embeddings + rerank over HTTP | absent |
| Ordered fallback candidates on a route | absent |

The 28 fork commits, oldest first, are the authoritative list:

```
8979c10e feat(reasoning): authoritative per-target reasoning policy and dialect
1071f0b9 feat(routes): ordered candidate lists on a deterministic route
de05076c feat(capabilities): embedding and rerank contract configuration
24457cb5 feat(capabilities): serve embeddings and reranking over HTTP
47598a3f fix(capabilities): enforce the declared rerank top_n as a ceiling
4fd2577c feat(decision): SystemOne and alpha-decisions transports with target eligibility
e5154811 fix(decision): encode the canonical SystemOne and alpha-decisions contract
170826a6 feat(decision): fulfil native decision calls against configured backends
87c8cf25 fix(decision): terminal contract failures, redacted candidates, pinned targets
3c58600f feat(decision): retained typed compatibility surface over the shared lane
eb93a596 fix(decision): explicit lane order, fail-closed identity, honest status codes
744296eb feat(decision): per-type lane ordering from the requalification evidence
e29b62d3 fix(decision): correct the recorded NOUL order and refuse ambiguous lane scopes
8118a30a feat(routing): whole-route composition over complete child routes
0a7f63e1 fix(routing): serve model, composition context, conflict and capability status
694e43ec fix(routing): filter ordered candidates by target capability before dispatch
6282008c feat(metrics): emit composition and capability decisions
4697a883 fix(metrics): carry composition into non-streaming routing records
0e26ac28 fix(metrics): carry composition on early-committed streams; own the data
1e291b41 feat(routing): route reasoning policy and the 61,440 context boundary
9b0b67ad feat(routing): honour escalation_max_input_tokens by reordering before dispatch
2258fd78 feat(metrics): report a context-pressure promotion
22d03a74 style(libsy): pass the promote function directly instead of wrapping it
c8140304 fix(routing): require the escalation leg to be a candidate, and report only real promotions
406541e6 fix(routing): reject a zero escalation threshold
140372eb fix(routing): reject a route reasoning policy its targets cannot carry
6cc1db05 feat(routing): let a route impose a graded reasoning effort, not only on/off
2c6e6df5 fix(translation): let a Responses upstream require a list-shaped input
```

### 2.2 Upstream features the deployment does not have

Selected from the 68 upstream-only commits; these are not available locally
and must not be assumed:

- `d93bcd97` externalized State surviving restarts
- `0c398045` native Claude routing plugin
- `9c0924f2` custom-schema classifiers in composite routing
- `f3baf439` composite stage policy selected from the classified tier
- `7f0602d2` turn ID in the routing log
- `58a33572` nested routing spans export
- several translation fixes (`0ae4384b`, `8e7d72be`, `286425ec`, `f6441cc4`, `9a6d26d9`)

> A "composite" exists on both sides but is **not the same feature**. Upstream
> composite routing classifies into stages; this fork composes whole child
> routes with ordered fallback. Do not port upstream composite semantics.

### 2.3 What cannot yet be established

- Whether any of the 68 upstream fixes address a defect present here.
- The build command and toolchain used for the deployed binary (no receipt
  found beside the source tree).
- Whether the fork was rebased, since the merge base is a single point.

---

## 3. Consequences for management

1. **Never treat upstream `main` as the source of truth for this deployment.**
   The fork is ahead in some areas and behind in others.
2. **Version `0.3.0` is ambiguous.** It cannot distinguish fork from release.
   Use the source commit plus the binary sha256.
3. **Upstream schema documentation is a partial reference.** `fallback_on_rate_limit`,
   `failure_cooldown_ms` and `max_retries` do exist in both trees, but
   `composites`, `decision_lanes`, `route_reasoning_policy` and escalation are
   fork-only and documented only by the code and this package.
4. **A future upstream upgrade is a merge, not a swap.** The 28 local commits
   would have to be replayed onto a newer upstream, and 68 upstream commits
   would arrive at once. That is a project, not a maintenance step.
