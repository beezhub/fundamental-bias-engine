# 0012. `available()` answers about configuration, never about reachability

Status: Accepted

## Context

`BaseDataSource.available` has said the same thing since the base landed: "The
check is about configuration, not connectivity: a missing API key or a missing
manual directory makes a source unavailable, a network blip does not."
`tests/test_datasource_base.py::test_available_never_reaches_the_network`
enforces it for every class in `ALL_SOURCES`, and `FredSource` (#56),
`ManualSource` (#60), `CotSource` (#174) and `CalendarSource` (#178) all
followed it when they landed.

Three stubs said the opposite. The scaffolded `available()` on `CurvesSource`,
`OecdSource` and `PricesSource` each promised a reachability check: true when
"at least one provider is reachable", true when "the OECD API answers", true
when Stooq "answers with CSV rather than the HTML anti-bot challenge page".
Issue #208, which routed the three sources, copied those promises into its
third and fourth acceptance criteria, so the issue asked for behaviour the base
contract forbids and the test suite would have refused.

Pull request #215 followed the base contract, returned `True` from all three,
rewrote the docstrings to say why, and asked the architect to confirm or
overturn. This record confirms it.

## Decision

**`available()` is a question about the run's configuration. It never makes a
request, and it never reports on whether a provider is up.**

- A source with nothing to configure returns `True`. That is the correct
  answer, not a placeholder: `CurvesSource`, `OecdSource`, `PricesSource` and
  `CotSource` all read open endpoints, and there is nothing in `DataConfig`
  that could rule any of them out.
- A source with a prerequisite answers for it and nothing else: `FredSource`
  for its key, `ManualSource` for its directory.
- Reachability is reported in the two places that already make the request.
  `fetch` raises `SourceError` naming the provider and the failure, which the
  collector records as a `FAILED` outcome. `probe_request` names the one
  request `fbe doctor` should judge the source on, and the check that tells
  the source's own content from anything else, which is where the Stooq
  challenge page is caught.
- An offline run with a cold cache is a `SourceError` from `fetch`, not a
  `False` from `available()`. The base's `_request` is the only place that
  distinction can be drawn without touching the network, and it already is.

The two criteria on #208 that asked for the opposite are struck on the issue,
with the replacement wording beside them, so a reader checking the closed issue
against the merged code does not find them disagreeing.

## Alternatives considered

**Let `available()` probe, as the stubs promised.** Rejected for three reasons,
each sufficient.

It fails quieter. The collector prints a `False` as `skipped (unavailable, not
configured)`. That label sends the operator looking for a credential that does
not exist, and it says nothing about which of seven curve providers refused.
`fetch` raising says `ecb could not supply EUR: HTTP 503`, and the collector
prints it under `failed`, which is the line an outage belongs on.

It is a third copy of a check the engine already makes twice. `fetch` makes the
request and raises. `doctor` makes the request and verifies the body. A probe in
`available()` adds a request per provider per run, seven for the curves, and
creates a window in which `available()` said yes at 07:00:00 and `fetch` failed
at 07:00:04, or the reverse, with two different lines on the report for one
outage.

It is already forbidden. The test predates the stubs, and the four sources that
shipped before #208 honoured it. Reversing it would have meant changing the base
docstring, the test and four sources to match three docstrings that nobody had
implemented.

**Return `True` from `available()` but keep the challenge-page check there for
Stooq only.** Rejected: it is the same check `fetch_stooq` makes and raises on,
and `probe_request` already hands it to doctor. A special case for one source
would be a fourth copy.

## Consequences

- No `available()` in the package makes a request, and
  `SCAFFOLDED_AVAILABLE` in `tests/test_datasource_base.py` is empty, so the
  contract test demands a bool from all seven sources.
- A source that is configured but blocked is asked anyway. The cost is the
  retry policy's attempts and backoff, seconds of wall clock in a run against a
  morning deadline, and then a `failed` line naming the provider. That is the
  cost of being wrong here, and it is time rather than a wrong number.
- `fbe doctor` is the pre-flight for reachability. A source with no
  `base_url`, which today means `curves` and `manual`, prints "names no base
  URL to probe" and is not probed. Whether the curves fan-out should be probed
  per provider is decided in 0013, not here.
- A future source whose stub docstring promises a reachability check is wrong
  at the docstring, and the docstring is what gets fixed.

## Status

Accepted, 2026-09-21. Ruled on #208, implemented in #215.
