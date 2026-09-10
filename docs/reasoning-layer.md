# The reasoning layer

A written brief that explains what the engine's numbers say, and why each pillar
landed where it did.

This document is a plan. Nothing in it is built yet.

## A note on naming

`CLAUDE.md` forbids model and tool names anywhere in the repository. That rule
exists so nothing here claims to be AI-generated. It does not apply to a model
used as a product dependency, which is named the same way FRED, CFTC and httpx
are named. This layer calls the Claude API, the code says so, and that is not a
breach of the rule. Do not "correct" it later.

## What this layer is for

The engine produces a ranking, 28 pair spreads, and a pile of pillar detail. A
table does not tell you that the euro's rise is coming almost entirely from the
front end while its growth pillar quietly rolled over, or that this week's
biggest mover changed because a series went stale rather than because anything
happened. The brief says that in sentences.

Its job is explanation. It does not score, rank, select, or size anything.

## Where it sits

One new stage, at the end, reading only what the deterministic pipeline already
produced.

```
Observation -> PillarScore -> CurrencyScore -> PairBias -> BiasReport
                                                               |
                                                               v
                                              hot list (deterministic)
                                                               |
                                                               v
                                                context pack -> model -> MacroBrief
                                                               |
                                                               v
                                                  verifier -> stored, rendered
```

**The reasoning layer cannot change a number.** It reads the report and writes
prose. Where it disagrees with the model, it says so in words and the score
stands. The arrow never points back up, for the same reason no other arrow in
this project does.

## "Hot" is computed, not asked for

The detector is ordinary Python in `hotlist.py`. The model never decides what is
interesting; it explains what the detector found.

This is the load-bearing design decision in the whole layer. "Hot" chosen by a
language model is unfalsifiable and different every run. "Hot" defined as a
score change above a threshold is auditable, reproducible, and testable.

What the detector surfaces, all from the report and its predecessor:

| Signal | Definition |
| --- | --- |
| Movers | Largest absolute change in composite since the last run |
| Sign flips | A pillar that changed direction on a currency |
| Extremes | Widest current spreads, and any change at the top or bottom of the ranking |
| Crowding | Positioning past the contrarian flip at \|p\| = 2 |
| Hollow moves | A conviction change caused by coverage or staleness rather than by fundamentals |
| Catalysts | High-impact events in the next 7 days on the top and bottom ranked currencies |

The last row matters more than it looks. A currency whose conviction fell
because a series went stale has not weakened, and a brief that narrates it as
weakening is worse than no brief. The detector separates the two so the prose
can too.

## Modules

```
src/fbe/reasoning/
  types.py      MacroBrief, BriefSection, Claim, Evidence, BriefKind
  hotlist.py    the detector above. No model call, no network.
  context.py    assembles the context pack, token-budgeted
  sources.py    the official-domain allowlist for search
  client.py     the API call: model, thinking, structured output, caching
  verify.py     the grounding gate
  render.py     MacroBrief to Markdown and to a dashboard section
  prompts/      system.md, weekly.md, daily.md, each version-stamped
```

## The call

- **Model: `claude-opus-5`.** This is judgement over a small context, which is
  where the capable model earns its price. Not a candidate for a cheaper tier.
- **Adaptive thinking**, which is on by default on this model.
- **Effort `high` for the weekly brief, `low` for the daily one.** The weekly
  sets the watchlist and deserves the thinking. The daily reports overnight
  changes against a picture that has not moved much.
- **Structured output** via `output_config.format` with a JSON schema, parsed
  into `MacroBrief`. Prose comes back in typed fields, not as a blob to
  regex over.
- **Search restricted to official domains**: central banks, statistics offices,
  debt management offices. The same institutions the data layer already pulls
  from. A brief that explains a CPI print should cite the office that published
  it, not a commentary about it.
- **Streaming** for the weekly brief, because a long output on a non-streaming
  request is how you meet an HTTP timeout.

## Prompt caching

The stable half of every request is large and never changes: the system prompt,
a condensed form of the methodology and scoring spec, the pillar definitions,
the sign conventions, and the conviction table. That is the expensive part and
it is identical on every run.

Cache it. Put the volatile half, today's context pack and hot list, after the
last breakpoint.

One trap, worth stating because it is the most common way this silently fails:
**no timestamps, run identifiers or as-of dates anywhere in the cached prefix.**
Caching is a prefix match, so a single date string at the top invalidates
everything after it and the saving disappears with no error. Verify with
`usage.cache_read_input_tokens` on every run and fail the build if it is zero
across two identical-prefix requests.

## Cost

Rough figures at current pricing, with the stable prefix cached:

| | Cached read | Fresh input | Output | Approx |
| --- | --- | --- | --- | --- |
| Weekly deep | ~12k | ~6k | ~3k | $0.11 |
| Daily short | ~12k | ~4k | ~1k | $0.04 |

Five dailies and one weekly per week is roughly **$1.35 a month**. On a R2,000
account that is about R25, which is less than one trade's risk at the bottom of
the ladder. Worth stating plainly so the cost never becomes a reason to reach
for a weaker model.

## The verifier

Three gates run before anything reaches the reader.

1. **Numeric grounding.** Every number in the prose must match a value in the
   report within tolerance. Unmatched, the sentence is stripped.
2. **Forbidden language.** The list already in `CLAUDE.md`: proven, backtested,
   edge, high win rate, and any figure presented as a historical result. The
   standing instruction on claims applies to generated prose exactly as it
   applies to a docstring.
3. **No levels.** No entry price, no target, no stop. Any price-like token not
   present in the report is stripped. This is the gate that stops the brief
   drifting into being a signal service, which is the failure this whole
   project is built to avoid.

Every strip is logged to `data/reports/briefs/<date>.rejected.jsonl`.

That log is the most useful artefact here, and not for the reason it looks. A
model repeatedly trying to say something true that the verifier cannot ground
is telling you the context pack is missing a field. The rejection log is the
backlog for what to add next, and later it is the eval set.

## Reproducibility

Every brief records the model id, the prompt version, the config digest, a hash
of the context pack, and any search results used. A brief you cannot reproduce
is an opinion of unknown origin, and this project already keeps reports for the
same reason.

## Phases

| Phase | Deliverable | Done when |
| --- | --- | --- |
| R1 | `types.py`, `hotlist.py`, `context.py`, and `fbe hot` | The detector prints a correct hot list from two stored reports, with no model call anywhere |
| R2 | `client.py`, the weekly brief, structured output | A weekly brief renders from a real report |
| R3 | `verify.py` and the rejection log | Every gate has a test that feeds it a bad brief and confirms the strip |
| R4 | Daily short brief, caching tuned | `cache_read_input_tokens` is non-zero on the second run |
| R5 | Official-domain search | A brief cites a primary source for a data explanation |
| R6 | Dashboard section, eval set built from the rejection log | The brief appears on the phone dashboard |

R1 is useful on its own and needs no API key, so it ships first. R3 lands before
R5 on purpose: the gates exist before external content is allowed in, not after.

## Open questions

1. How much of the scoring spec belongs in the cached prefix? Too little and the
   explanations are shallow, too much and every run pays to re-read the whole
   document.
2. Should the daily brief see the previous day's brief? It would read better and
   it risks compounding an early misreading across a week.
3. What is the right tolerance for numeric grounding, given the report publishes
   two decimals and the model will round in prose?
4. The current design explains the ranking. An adversarial mode that argues
   against it was considered and deferred. Worth revisiting once there are
   enough briefs to judge whether explanation alone changes any decision.
5. Nothing here has been measured. Whether a brief improves a single trading
   decision is unknown, and the journal is the only thing that will ever say.
