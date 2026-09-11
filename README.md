# fundamental-bias-engine

A relative-value fundamental scoring model for the G10 currencies. It ranks
each currency on seven macro pillars, differences the two legs of every pair,
and publishes a directional bias with a conviction level to a CLI and an HTML
dashboard.

## The problem it solves

The owner's trading plan (`docs/trading-plan.md`) is a complete technical
method: trendlines, trend channels, 1h and 4h charts, candlestick confirmation,
a 1-2% risk cap. What it has no answer for is which way to lean before the
chart is opened. A clean break of a descending trendline on EURUSD is a
different proposition depending on whether the euro is fundamentally the
stronger leg or the weaker one.

This engine supplies that missing layer and nothing else. It does not generate
entries, it does not connect to a broker, and it does not tell anyone to take a
trade. Entry timing, stop placement, and pattern confirmation stay
discretionary and technical.

## The seven pillars

Each pillar scores every currency on a `-3..+3` band. The composite is the
weighted sum. Weights live in `ScoringConfig` in `src/fbe/config.py` and must
sum to 1.0.

| Pillar | Weight | What it measures |
| --- | --- | --- |
| `MONETARY` | 0.30 | Policy rates and short-end yield differentials, plus the expected path |
| `INFLATION` | 0.15 | Deviation from each central bank's own target, not raw CPI |
| `GROWTH` | 0.15 | Activity momentum: GDP, PMI, industrial production |
| `EMPLOYMENT` | 0.10 | Labour market tightness and its direction of travel |
| `EXTERNAL` | 0.10 | Current account, trade balance, terms of trade |
| `POSITIONING` | 0.10 | CFTC Commitment of Traders net speculative positioning |
| `RISK` | 0.10 | Risk regime, scored through each currency's haven or high-beta character |

Monetary policy carries the largest weight because rate expectations dominate
G10 FX over the multi-day to multi-week horizon this engine targets. That is a
prior, not a measured result. See `docs/scoring-spec.md` for the reasoning and
`docs/roadmap.md` for how it will be tested.

## Quickstart

```bash
git clone <repo-url>
cd fundamental-bias-engine

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
```

Get a free FRED API key from
<https://fredaccount.stlouisfed.org/apikeys>. The request form asks for a short
description of the application; a paragraph describing a personal macro
research tool is enough, and the key is issued on submission. It backs the
monetary, inflation, growth, and employment pillars.

Put the key in `.env` as `FRED_API_KEY` so you have a record of it, then export
it. The code reads the environment only. Nothing loads `.env` yet, so a key that
lives only in that file is invisible to the engine.

```bash
export FRED_API_KEY=your_key_here
```

```powershell
$env:FRED_API_KEY = "your_key_here"
```

Then check the install:

```bash
fbe doctor
```

`doctor` reports which data sources are reachable, whether the FRED key works,
what is in the cache, and which parts of the pipeline are not yet implemented.
Until it lands, confirm the key with one direct request. A working key returns
a JSON body with one observation; a bad one returns 400.

```bash
curl -s "https://api.stlouisfed.org/fred/series/observations?series_id=FEDFUNDS&file_type=json&limit=1&sort_order=desc&api_key=$FRED_API_KEY"
```

## Status

Skeleton. Honestly stated:

**Implemented and working.**

- `src/fbe/types.py` The shared data contracts every module speaks in.
- `src/fbe/universe.py` The G10 set, per-currency metadata, and the 28 crosses in market quoting convention.
- `src/fbe/config.py` Risk, scoring, and data configuration with validation and a run digest. `load_config()` is the one exception and still raises.

**Not implemented.** The data sources, the seven pillars, the scorer, the bias
layer, risk sizing, the calendar guard, the journal, the CLI, and the
dashboard. Module files and docstrings exist; the algorithms do not. Stubs
raise `NotImplementedError` rather than returning placeholder numbers.

No part of this model has been validated against out-of-sample returns. There
is no backtest. Every weight above is a prior.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/trading-plan.md](docs/trading-plan.md) | The owner's trading plan, transcribed, plus an appendix on what the engine automates and what stays manual |
| [docs/methodology.md](docs/methodology.md) | The relative-value framework and why these seven pillars |
| [docs/scoring-spec.md](docs/scoring-spec.md) | Normalisation, weighting, conviction bands, worked example |
| [docs/data-sources.md](docs/data-sources.md) | Every source, the indicator registry, caching and freshness rules |
| [docs/risk-and-execution.md](docs/risk-and-execution.md) | Position sizing, news blackout windows, the trade journal |
| [docs/interfaces.md](docs/interfaces.md) | CLI commands, report schema, dashboard contract |
| [docs/roadmap.md](docs/roadmap.md) | Phases from skeleton to v1, validation plan, open questions |
| [CLAUDE.md](CLAUDE.md) | Repository guide: architecture, conventions, house rules |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Branches, commits, required checks |
| [docs/reference/](docs/reference/) | Source material the docs are derived from |

## Risk disclaimer

This is a research tool. It is not financial advice, not a recommendation to
buy or sell anything, and not a promise of any result. The model has not been
validated. Its outputs are one opinion among many and should be treated as a
starting point for your own analysis, not a signal. Trading foreign exchange on
margin carries a high risk of loss, including the loss of your entire deposit.
Anyone using this code does so at their own risk and is responsible for their
own decisions.
