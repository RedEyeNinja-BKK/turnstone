# Patch: refuse video and audio at admission

Status: **applied to the authentic source, reviewed, built, deployed and
verified in production.** Binary `b183a87157eaf1917e61601343a84df2fb08be4b20f959d562d8b75cf4b1196c`.

- Base: `6de85da15b68294cfda1775a1afa6e30ebd3cf16` (Authentic production
  lineage; see `01-provenance-and-divergence.md`.)
- Patch: `0001-gate-audio-video-on-vision.patch` (436 lines, 3 files)
- Fork branch carrying the commit:
  `fix/media-admission-gate-20261017` at `5721d34` (one commit on
  `6de85da1`). The preservation branch `v6-decision-planes-20261005`
  is left at `6de85da1` so the lineage it records stays intact.

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
| image | 400 `unsupported_capability` (local) | 400 (local), unchanged |
| video | **502 upstream** (rate limit surfaced after forward) | **400** `unsupported_capability` (local) |
| audio | **404 upstream_error** `No endpoints found that support input audio` | **400** `unsupported_capability` (local) |

Audio is reachable from Turnstone (`turnstone/core/audio.py` emits
`input_audio`); video is not emitted today, so that half of the gap is latent.

## Design decision

The deployment declares multimodal support with one boolean, `vision`. It says
nothing about video or audio, and there is no field that could. Inferring
either from `vision` is precisely the defect, so both are refused
unconditionally:

- `ContentBlock::Video` / `ContentBlock::Audio` in decoded content, including
  inside tool results;
- `input_video`, `video_url`, `video`, `input_audio`, `audio` as preserved JSON
  type discriminators, and as bare keys on message and content-part objects.

The refusal is checked **before** the image/tool scan, so a request carrying
both an image and a video reports the medium nobody can serve.

Limitation, stated rather than hidden: there is no configuration surface for
video or audio capability, so the refusal is total. Admitting either later
requires a schema addition, not an inference from `vision`.

## False-positive guards

The walk is deliberately narrow, and each restriction has a test:

- Tool **definitions** are never walked: a tool may declare a parameter named
  `video`.
- Bare media **keys** are matched only on message and content-part objects, so
  a field named `video` inside tool output — ordinary data — is not read as
  caller-supplied media.
- `output` is followed only from a `*_call_output` part, matching the
  pre-existing image check.

## Tests

| Level | Test | Proves |
|---|---|---|
| unit | `video_and_audio_are_refused_even_when_vision_is_enabled` | every representation, three wire formats, `vision = true` included |
| unit | `media_preserved_as_a_bare_key_is_refused` | keyed media with no `type` |
| unit | `text_is_admitted_after_the_media_rule` | text unaffected |
| unit | `a_tool_schema_naming_video_is_not_treated_as_media` | no false refusal on tool schemas |
| unit | `media_named_fields_inside_tool_output_are_not_request_media` | no false refusal on tool output |
| unit | `media_named_data_in_unrelated_nesting_is_not_request_media` | no false refusal on unrelated nesting |
| integration | `video_and_audio_never_reach_the_upstream` | 400 on all three endpoints and `MockUpstream` records **zero** calls |

Full workspace: **1046 passed, 0 failed** (rustc 1.99.1). Focused suites also
pass under the production toolchain (rustc 1.99.0).

## Review

Four independent review rounds. Rounds 1–3 blocked on real defects, each fixed
and regression-tested: untyped media keys were missed; `preserved_media`
followed every `output`; bare-key matching was too deep. Round 4: **PASS**.

## Deployment

| Item | Value |
|---|---|
| Binary before | `74963b1d57c664172175f820f2b94c8ad790629feb2965538c9a09983387d147` |
| Binary after | `b183a87157eaf1917e61601343a84df2fb08be4b20f959d562d8b75cf4b1196c` |
| Size | 26,984,312 → 26,990,680 bytes |
| Rollback anchor | `/var/lib/switchyard/rollback/media-gate-20261017T173857Z/` |
| Config | unchanged, `7b8445b20346670fa712f1e2b82b81730126df738054e6aef5340ea5dbcc326f` |
| Running executable | `/proc/138695/exe` = new hash |
| `--dry-run` | exit 0 under the unit's real environment, 29 routes |
