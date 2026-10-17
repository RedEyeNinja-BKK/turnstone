# Patch: media admission — explicit video capability, audio refused

Status: **deployed and verified in production (2026-10-18).**

- Base: `6de85da15b68294cfda1775a1afa6e30ebd3cf16` (see
  `01-provenance-and-divergence.md`).
- Fork branch: `fix/media-admission-gate-20261017`, commits `5721d34`,
  `03bcbcb`, `23b869d`. The preservation branch `v6-decision-planes-20261005`
  is left at `6de85da1`.
- Patch: `0001-gate-audio-video-on-vision.patch` covers the first commit.

Two stages, both deployed:

| Stage | Binary | Behaviour |
|---|---|---|
| 1. Media gate | `b183a871…` | Video and audio refused unconditionally |
| 2. Video capability | `25f92620…` | Video admitted where an explicit `video = true` is declared; audio still refused |

## The defect

`request_fit::unsupported_capability` is the single admission predicate used
both by HTTP ingress and by composite child selection. It tested
`ContentBlock::Image` against the `vision` declaration but never
`ContentBlock::Audio` or `ContentBlock::Video`, and the preserved-body walk
matched only image type strings. A media request therefore passed admission on
a route that declares `vision = false` and was forwarded to a provider that
cannot serve it.

Measured against route `direct-openrouter-xing4_0-29b-a4b-free` (`vision = false`):

| input | before | after |
|---|---|---|
| image | 400 `unsupported_capability` (local) | 400 (local), unchanged |
| video | **502 upstream** (surfaced after forward) | **400** `unsupported_capability` (local) |
| audio | **404 upstream** | **400** `unsupported_capability` (local) |

## Why video got its own declaration

The first stage refused video outright, which closed the defect but also denied
video to a model that can serve it. `vision` is a declaration about images and
is not evidence of video support: on this deployment one target accepts video
and another does not, and both are reachable through routes that declare
`vision = true`.

`video` is therefore a target fact, a route promise and a composite promise,
separate from `vision`:

- Video is admitted only when the selected route declares `video = true`.
- A target that declares nothing is video-incapable. This is the opposite of
  the rule for `vision`, where absence means permitted, because no deployment
  carried video before the field existed.
- A route claiming video with no video-capable target fails at load.
- A composite claiming video with no video-capable child fails at load.
- A target claiming video on a non-`openai_responses` client fails at load:
  video has one faithful representation, the Responses `input_video` part, and
  the Chat codec would otherwise deliver its content as text.

Audio has no declaration at all and remains refused unconditionally: no
configured model or transport has been verified to accept it.

## Media detection

Two walks, because the evidence differs in strength:

1. Explicit `type` discriminators, wherever they appear, with `output` followed
   only from `*_call_output` objects.
2. Bare keys (`video`, `video_url`, `input_video`, `audio`, `input_audio`),
   matched only on message objects and content-part objects.

Tool definitions are never walked, so a tool that declares a property named
`video` is not mistaken for a request carrying video. Tool output that merely
contains such a field, without a type discriminator, is ordinary data and is
preserved.

## Wire format: measured, not assumed

The two OpenRouter APIs disagree, and this is the single most important
operational fact in this patch:

| API | `video_url` shape | free endpoint |
|---|---|---|
| Chat Completions | object: `{"url": …}` | accepted |
| Responses | string | accepted |
| either, `https://` URL | — | **404** `No endpoints found that support video URLs` |
| either, `data:video/mp4;base64,…` | — | accepted |

So a public video URL is refused by the free endpoint regardless of shape,
while the same bytes inline as base64 are accepted. Switchyard targets
`openrouter` with `format = "openai_responses"`, so the working shape there is
`{"type":"input_video","video_url":"data:video/mp4;base64,…"}` — a string.

Tested end to end through Switchyard against `smartfree`: with the video the
model answered **"rabbit"** (the actual character in the test clip); without
it, **"Dog"**. Through composite `tier1` the same request also answered
"rabbit", with `composition_attempt_total{child_route="smartfree",
outcome="selected"}` confirming the video-capable child was chosen.

## Deployment

- Binary `25f9262051b656d439645e574ebbd4d70b07cd7ca03d6228178bdc180c5543ca`
  (27,086,512 bytes), built from the branch with rustc 1.99.0.
- Config `0b653d3e2ffe4edd19ba95ca8f1c9be34889c43b9cfd9008fa6c852975ea4776`:
  eight `video = true` insertions — the Nemotron target, its three routes, and
  the four composites that contain a video-capable child. Xing is untouched
  (`vision = false`, no video).
- Rollback anchor
  `/var/lib/switchyard/rollback/video-capability-20261017T185328Z/`.
- `--dry-run` under the unit's real `EnvironmentFile`s: exit 0, 29 routes.
- Verified by `/proc/<pid>/exe`, not a version string.

## Regression

All 29 routes intact; text on `smartfree`, `smartfree-bounded`, `smart`,
`tier1`, `tier1-bounded`, `tier2` and the Xing direct route returns 200; image
on a vision route 200; audio 400; video to a text-only route 400; decision
lane 200; metrics families present.

Workspace tests: 1061 passed, 0 failed.
