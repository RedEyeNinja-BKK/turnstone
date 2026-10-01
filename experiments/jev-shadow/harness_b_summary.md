# Harness B result - hosted Jev Router routing oracle

14 genuine post-T0 observations, replayed offline. 14/14 calls succeeded.

## What the router did

```text
selected models : openai/gpt-6-luna 11, deepseek/deepseek-v4.1-flash 3
compute classes : cheap/fast 14/14
latency         : p50 4074ms  p95 5972ms  max 8635ms
cost            : total $0.007661, mean $0.000547
```

Three findings that matter more than the numbers:

1. **It is a real router.** On identical-shaped bounded input it selected two
   different models across the corpus. In the earlier ad-hoc probe a hard-math
   prompt selected `openai/gpt-6.1-sol` at ~600x the cost of a trivial prompt
   selecting `gpt-6-luna`. Selection tracks the input.

2. **It is not free.** The catalogue lists `prompt=-1, completion=-1`, but every
   call reported a real `usage.cost`. The deepseek selections cost ~30x the luna
   ones. Any cost model built on the catalogue figure would be wrong by orders of
   magnitude.

3. **Deterministic per input, but not predictable from our signals.** Three
   repeats of one projection all returned the same model. Yet across the corpus
   the choice varied, and our five signals do **not** separate the two groups:
   `task_complexity` 0.0910 (deepseek) vs 0.0728 (luna), `tool_dependency`
   0.0634 vs 0.0957 - the ordering runs in opposite directions. So we cannot
   currently predict or steer what it will pick.

## Measured negatives

- **Selected reasoning effort is not exposed.** No effort field in any response.
- **Candidate controls do not bind.** `models:`, `exclude:`, `provider.order`,
  `route: "fallback"` all returned the same model, including an `exclude` naming
  the very model it had chosen. The candidate space is not governable.
- **`allow_fallbacks: false` had no observable effect** on selection.

## Privacy

The store contains no task text at all (see RECON addendum F1), so the projection
is five bounded numbers plus the event class, ~301 chars, asserted field by field.
`local_suitability` is never sent.

## Verdict

A genuine, self-consistent routing oracle. Not promotable: it is not free, its
candidate space cannot be governed, its effort choice is invisible, and its
selection is not predictable from the signals we currently measure.
