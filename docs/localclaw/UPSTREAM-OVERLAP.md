# Upstream overlap

Where LocalClaw work touches an area upstream is already discussing, this file
records the relationship **for our own future reference**.

> **Posture: measured evidence first, architecture proposals second, no
> unsolicited roadmap pressure.**
>
> Nothing in this file authorises contacting the maintainer, opening an issue,
> or opening a pull request. No comment has been added upstream to advertise
> LocalClaw work, and none should be.

## Our posture

| rule | consequence |
|---|---|
| we do not open an upstream sensor issue | the sensor is local experimental infrastructure |
| we do not comment upstream to advertise our work | no drive-by comments |
| we do not submit a PR without a separate operator decision | not authorised by this document |
| we may reference upstream issues/PRs in **our** docs | this file |

Contact the maintainer only when:

1. they ask for evidence we now possess, or
2. our completed shadow measurements materially answer an open upstream design
   question.

Neither condition currently holds.

## Relevant upstream work

| ref | area | relationship to our local work |
|---|---|---|
| #1201 | Switchyard provider boundary | **overlaps.** Our `server_compat.extra_headers` (`4d115d38`) and the typed-decision guard seam sit in the same boundary area. Resolved and verified locally; deliberately **not** submitted upstream. |
| #1202 | reasoning replay | **overlaps.** Our responses-wire replay and round repair (`02db136d`, `fc611fb9`, `ee794cb6`, `cfe1b29c`) address the same problem from the LocalClaw side. |
| #1213 | provider descriptors | **adjacent.** Relevant to the typed-decision seam's provider model; no shared code today. |
| #1214 | multiple backends / future selection policy | **adjacent.** Our continuation work (`0956fd2f`, C2 group) assumes a future selection policy upstream has not settled. Do not pre-empt it. |
| #1215 | PAIR / Switchyard backend | **adjacent.** Background for why a non-chat decision backend exists at all. |
| — | the shadow sensor | **no upstream equivalent.** `upstream/dev` contains no sensor, shadow or bounded-state module. Not proposed. |

## Why the sensor is not proposed upstream

Not modesty — three substantive reasons:

1. **It is not finished.** The gate is `>=100 observations AND >=48h` and the
   measurement is still running. Proposing an API before the evidence exists
   would be proposing a guess.
2. **It is LocalClaw-shaped.** The provider-neutral contract happens to be
   generic, but the deployment is bound to one Switchyard AUX lane and a
   tokenizer cache in a home directory. Upstream has no such dependency.
3. **Nobody asked.** No maintainer request for it exists. Volunteering a
   roadmap for a system the maintainer did not ask about is pressure, not
   contribution.

If the measurements later answer an open upstream design question, that is
condition (2) above and the operator decides whether to act.

## What would change this file

- upstream `dev` gaining a sensor/shadow/bounded-state module
- a maintainer request for evidence
- an operator decision to submit upstream

Until then this file is a lookup table, not a plan.
