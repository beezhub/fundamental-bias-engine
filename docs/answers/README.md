# Answers to the open questions

Four specialists worked the open questions in `docs/scoring-spec.md` section 10,
`docs/roadmap.md` and `docs/reasoning-layer.md`. Their findings are here, one
file per domain, under four verdicts: answered, narrowed, blocked on trade data,
or a judgement call.

## Read these as dated snapshots, not as current state

**Every file here records what was true when it was written.** Some findings
have since been acted on, and the file that reported them was not updated,
because a finding is a record of an investigation rather than a description of
the repository.

Before acting on anything in this directory, check the current code or
specification. A known example: `scoring-maths.md` reports that
`docs/scoring-spec.md` section 2.3 and `pillars/base.py::blend_divisor`
disagree about the re-standardisation divisor. That was true when written and
was fixed in the same session. Section 2.3 now describes the rolling divisor
that the code implements.

The canonical status of each open question is `docs/scoring-spec.md` section 10,
which is kept current. This directory holds the evidence behind those statuses.

| File | Domain |
| --- | --- |
| `scoring-maths.md` | Normalisation, sub-weights, sample size, conviction |
| `framework.md` | Inflation credibility, carry, risk beta, pillar weights |
| `data.md` | Source availability, coverage, revisions |
| `product.md` | Cadence, rendering, the reasoning layer's parameters |
