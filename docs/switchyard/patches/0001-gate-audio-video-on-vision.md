# Patch: gate video and audio on the vision capability

Status: **prepared, compiled, behaviourally verified — NOT deployed.**
See "Why it is not deployed" below.

## The defect

`request_fit::unsupported_capability` is the single admission predicate used
both by HTTP ingress and by composite child selection. It tested
`ContentBlock::Image` against the `vision` declaration but never
`ContentBlock::Audio` or `ContentBlock::Video`, and the preserved-body walk
matched only image type strings. A media request therefore passed admission on
a route that declares `vision = false` and was forwarded to a provider that
cannot serve it.

Measured against the live deployment, route
`direct-openrouter-xing4_0-29b-a4b-free` (`vision = false`):

| input | before | after |
|---|---|---|
| image | 400 `unsupported_capability` (local) | 400 (local) |
| video | **404 upstream_error** — forwarded | 400 `unsupported_capability` (local) |
| audio | **404 upstream_error** — forwarded | 400 `unsupported_capability` (local) |

The upstream message was OpenRouter's `No endpoints found that support input
video`, which proves the request left the process. Audio is reachable from
Turnstone (`turnstone/core/audio.py` emits `input_audio`); video is not emitted
today, so that half of the gap is latent.

## The change

Two sites in `crates/switchyard-runner/src/request_fit.rs`:

1. `unsupported_preserved_content` — add `video`, `video_url`, `input_video`,
   `audio`, `input_audio` to the type strings rejected when `vision = false`.
2. `unsupported_content` — match `ContentBlock::Audio` and `ContentBlock::Video`
   alongside `ContentBlock::Image`.

The rejection reason stays `"vision"` because `vision` is the manifest's only
multimodal-input declaration; the config has no separate audio/video capability
field. The resulting message ("declares vision = false") is therefore accurate
about the field, though it names the multimedia gate rather than the modality.

Two regression tests are included:
`rejects_audio_and_video_when_vision_is_disabled` (preserved body, Responses)
and `rejects_decoded_audio_and_video_blocks` (decoded block, Anthropic).

## Verification performed

- `cargo build --release -p switchyard-server` on `agent-vm.example.invalid` with rustc 1.99.0:
  success, 2m51s.
- `cargo test --release -p switchyard-runner`: **140 passed, 0 failed**,
  including both new tests.
- Patched binary run as an isolated instance on `switchyard-vm.example.invalid:4012` against a
  copy of the deployed manifest; the table above is from that instance.
- Artifact sha256 `2c7061ad98773874ce691b76d1c6868f7499a885af53b438303a37ca5277e6ba`.

## Why it is not deployed

The source bundle this patch applies to is **not** the tree the deployed binary
was built from. The deployed binary accepts `[decision_lane_composition]` from
the live manifest; the bundle's source has no such field and its binary refuses
the manifest at startup (`unknown field decision_lane_composition`). See
`docs/switchyard/01-provenance-and-divergence.md` for the four-way comparison.

Deploying this artifact would stop the service. It must first be rebuilt from
the true source tree (`/home/vincent/switchyard-migration/production-src`) or
from a regenerated bundle, then diffed against the running binary and
re-verified.

## To apply once the correct source is available

1. Apply this patch to the true tree; confirm `cargo test -p switchyard-runner`.
2. Build release; record the artifact sha256.
3. `--dry-run` against the live manifest must print `server OK: …` with all 28
   routes.
4. Run the isolated-instance check from the table above (ports 4000 vs a
   scratch port, `vision = false` route, image/video/audio).
5. Back up `/opt/switchyard/bin/switchyard-server`, install with mode 0755,
   restart `switchyard-neutral-green.service`, verify `/health` and
   `/v1/models`, and keep the previous binary as the rollback anchor.
