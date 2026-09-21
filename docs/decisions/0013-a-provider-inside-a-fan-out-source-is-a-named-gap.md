# 0013. A provider inside a fan-out source is a named gap, not the end of the source

Status: Accepted

## Context

`CurvesSource` reads the two-year government yield from seven institutions, one
per currency, because FRED carries none of them. Its module docstring reasons:
"Each provider covers exactly one currency. None can substitute for another, so
losing one provider is losing a currency's heaviest pillar rather than
degrading a series. Fail loudly here." `_from_provider` acts on that by
re-raising any provider's `SourceError`, and `fetch` lets it out, so one
provider failing returns nothing for any currency.

The scaffolded `available()` docstring, replaced in #215, argued the other way
for availability: "the ECB being down costs the euro's front end and nothing
else, and refusing to run the whole engine over that would be the wrong
trade." Issue #212, a dispatch bug that makes every GBP request raise, made the
disagreement visible: because of the whole-source contract, the GBP bug costs
every two-year yield in the run and MONETARY prints n/a for six currencies.
#212's fifth criterion kept the contract on purpose and asked for a separate
ruling. This is it.

The collector already has the rule one level up. `collect.py`: "A source that
fails is a gap, not the end of the run. One dead provider at 07:00 must leave
the other five to fill the cache." And `docs/data-sources.md` records a
standing case: the RBNZ refuses every data-centre and cloud vantage with HTTP
403, so from any such vantage the current contract turns one known refusal into
zero two-year yields.

## Decision

**One provider failing costs that provider's currencies and nothing else. The
failure is reported by name, as a failure, distinct from a provider that
answered with nothing. The currency it lost reaches the score as an explicit
absence.**

Three rules follow, and all three are required. Any one alone reproduces a
defect already in the standards table.

1. **The other providers' observations are returned.** Nothing raised by one
   provider may withhold what another served.
2. **The failure is a status, not a swallowed exception and not detail text on
   a completed line.** `SourceStatus.FAILED` with the provider's name and the
   error, so the refresh output prints `ecb failed: ...` on its own line, and
   `curves served nothing` cannot be mistaken for it. This is ADR 0002's fourth
   rule at the provider level.
3. **A provider that answers with no rows is still a raised error for that
   provider.** The module's existing argument holds at that scope: silence from
   a single-currency provider is a lost pillar, not an empty result. Only the
   scope changes.

**The preferred shape is one source class per provider.** Seven thin subclasses
of a shared curves base, each carrying its own `name` equal to the registry's
provider key, its own `base_url`, and a `refs()` filtered to that key. The
shared base keeps `_body`, `_session`, `_reading` and the parsers. The reasons:

- It reuses the seam the collector already has. `SourceOutcome` is per source,
  so a per-provider outcome needs no new status and no change to `collect.py`.
- It fails loudest. Each provider gets the collector's full treatment: its own
  construction, its own `available()`, its own retry policy, its own line.
- `Observation.source` already carries the provider key, so nothing downstream
  changes and the source's `name` finally matches what its observations say.
- The contract tests in `tests/test_datasource_base.py` parametrise over
  `ALL_SOURCES`, so each provider is covered without new tests being written.
- `fbe doctor` probes each provider's own `base_url`, which answers the
  question of whether the fan-out should override `probe_request`: it should
  not, because after the split there is no fan-out to probe.

The shape is the data engineer's to confirm. If the split is refused for a
reason this record has not weighed, one class may stay, provided rules 1 to 3
hold: a new outcome status for a partial source, the failed providers named on
it, and the refresh output printing each. What may not happen under either
shape is a partial result whose missing providers are invisible on the report.

## Alternatives considered

**Keep the whole-source contract.** This is what the module docstring argues
and what #212 preserves. Rejected because the loud failure it wants already
exists at the right scope: a currency without `yield_2y` falls below the
monetary pillar's component floor, the pillar prints n/a for that currency,
and coverage is demoted. Raising for the whole source does not make that
louder; it makes five other currencies pay for it. The cost of being wrong the
other way, keeping this contract, is a morning with no MONETARY for anyone
because one central bank's website was down, and on any server vantage that
morning is every morning.

**Return the partial result and log the failure.** Rejected. A log line is not
on the report, and ADR 0002's third rule says a marker that is not rendered
does not exist. This is the `except: pass` from the standards table wearing a
logger.

**Return the partial result and raise afterwards.** Rejected. `fetch` returns a
sequence or raises; it cannot do both, and an exception carrying a payload is a
side channel the collector would have to know to unpack.

**Add a `PARTIAL` status to `SourceOutcome` and keep one class.** Acceptable
and second choice, as stated above. It loses to the split because it adds a
status the other six sources can never produce, and because it leaves the
fan-out unprobed by doctor.

## Consequences

- `ALL_SOURCES` grows from seven entries to thirteen if the split is taken.
  Refresh prints one line per provider. `-s curves` becomes `-s ecb`, `-s boc`
  and so on, which is more typing and more honest.
- Cache directories are re-keyed from `curves` to the provider names. The cache
  is reproducible by contract, so nothing is lost; the first refresh after the
  change refetches.
- The bad consequence: a provider that is down is now a quiet n/a for one
  currency on a report that otherwise looks normal. The whole-source failure
  was impossible to miss. Rule 2 is what stands in for that: the refresh
  output names the provider, and the score table prints n/a for the currency.
  If that proves too easy to overlook in practice, the fix is on the report,
  not a return to failing the source.
- `docs/data-sources.md` and the module docstring are corrected to say what
  fails loudly and at what scope.
- Not in the #211 cut. With #212 merged and the owner running from a
  residential connection, all seven providers answer today. Issue #218 carries
  the work at p2.

## Status

Accepted, 2026-09-21. Ruled on #212; work in #218. Question 3 on #212, whether
the fan-out should override `probe_request` for doctor, is answered by this
record as unnecessary under the preferred shape and deferred otherwise.
