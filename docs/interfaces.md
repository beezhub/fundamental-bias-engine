# Interfaces: the CLI, the report and the dashboard

Two surfaces sit on top of the engine. The `fbe` command line is the working
surface, used at the desk. The HTML dashboard is the read-only surface, built
once in the morning and opened on a phone during the session. Both render the
same `BiasReport`, so they cannot disagree.

Everything here is the directional bias layer only. Entries and exits stay with
the trendline and channel rules on the 1h and 4h charts.

## Design rules

**One command per step of the routine.** The trading plan already fixes a daily
routine. The command surface follows it rather than inventing a new one, so
there is never a question of which command belongs to which part of the day.

**Fetching is separate from scoring.** `refresh` is the only command that can be
slow or fail on the network. Everything downstream reads the cache, which means
scoring is instant, repeatable, and still works when the connection is bad. A
failed fetch at 07:00 leaves yesterday's cache intact and scoreable rather than
leaving the morning with nothing.

**Every run is reproducible.** A run is pinned by two things: `--asof`, which
excludes observations released after that date, and the config digest, which
pins the weights. Both appear on every report.

**Exit codes are part of the contract**, so commands can be chained in a shell
script without parsing output:

| Code | Meaning |
|---:|---|
| 0 | The command did what it was asked. |
| 1 | It ran, but the result should not be traded on: every source failed, coverage collapsed, `doctor --strict` found a warning, or the smallest size the broker accepts breaks the risk cap. |
| 2 | Usage error, raised by the argument parser. |
| 3 | A guard rule refused: `size` inside a blackout window without `--force`. Not an error, a decision. |

## Global options

| Option | Default | Meaning |
|---|---|---|
| `--config`, `-c` | `config.yaml` at the repo root if present | Config file for this run. |
| `--offline` | off, so the file or the defaults decide | Read the cache only, never touch the network. One-way: passing it forces offline on, omitting it changes nothing. There is no `--online`, because the flag exists to make a run reproducible and a flag that can undo it is a flag that will. |
| `--verbose`, `-v` | off | Repeatable. Once shows per-source timings, twice shows every request and every pillar input. |

The global callback only captures these flags. The config itself is resolved
lazily, so `fbe <command> --help` still works when the config file is broken.
A tool you cannot ask for help is a poor tool to debug with.

One invocation resolves one config, cached on the Typer context, so two
commands in the same run cannot produce two digests.

### Where a setting comes from

Three layers, each overriding the one before it, field by field:

1. The built-in defaults in `fbe.config`.
2. `config.yaml`, or the file given to `--config`. Top-level keys are the
   section names `risk`, `scoring` and `data`, each holding that section's
   field names.
3. `FBE_<SECTION>_<FIELD>` environment variables, upper-cased, such as
   `FBE_DATA_OFFLINE` or `FBE_SCORING_MIN_SPREAD_LOW`. `FRED_API_KEY` keeps
   its own name, because that is the name the vendor documents.

A key in the file that names no field, and a variable carrying the `FBE_`
prefix that names no field, both raise and name the offending setting. A
setting that silently does nothing is a setting the operator believes is
applied. `scoring.weights` is the one field that must be given in full: a
block naming some pillars would be merged over the defaults and leave a total
nobody chose.

Loading is not judging. A config that `Config.validate` would reject still
loads, so `fbe doctor` can print the problem rather than the resolver hiding
it behind an exception.

Once resolved, the config is validated before it is used. `fbe doctor` and
every command that scores or sizes from the config (`score`, `bias`, `report`,
`dashboard` and `size`) call `Config.validate` first and refuse to run on a
non-empty result, printing each problem on its own line. `validate` rejects
weights that do not cover every pillar, are negative or do not sum to 1.0, a
risk cap above the plan's 2%, and any threshold ordering that would leave a
scoring formula undefined or a conviction band empty. A config that fails here
would not crash the engine. It would produce a report that looks normal and is
wrong, which is why the refusal happens before any number is computed.

## The daily sequence

| Routine step (trading plan) | Command |
|---|---|
| 1. Morning: news and economic calendar review | `fbe refresh` then `fbe calendar --hours 24` |
| 2. Pre-market analysis | `fbe score --pillars`, `fbe bias --majors --min-conviction medium --tradeable-only` |
| 2. Pre-market, durable record | `fbe report`, `fbe dashboard` |
| 3. Trading session, at the moment of entry | `fbe calendar --pair EURUSD --hours 4`, `fbe size EURUSD --entry ... --stop ...` |
| 4. Trade management, position open | `fbe calendar --hours 6` |
| 5. Post-market review, 7. evening journal | `fbe journal add ...`, `fbe journal review --days 7` |
| Anything looks wrong, any time | `fbe doctor` |

A whole morning is four commands:

```console
$ fbe refresh && fbe calendar --hours 24 && fbe report && fbe dashboard
```

`doctor` sits outside the routine on purpose. When the output looks wrong the
temptation is to blame the model. `doctor` checks the four cheaper explanations
first: bad config, missing key, stale cache, dead source.

## Command reference

Every console block below is illustrative. The numbers show the shape and
wording of the output, not measured results: nothing in this project has been
validated against out-of-sample returns yet.

### `fbe doctor`

Checks, in order: `Config.validate` problems such as pillar weights that do not
sum to 1.0 or a risk cap above the plan's 2%; presence of each credential;
cache writability, entry count and age against `cache_ttl_hours`;
`DataSource.available` plus a live probe for every source unless `--offline`;
and whether a previous report exists to diff against. Each check prints its own
verdict, so one failure does not hide the rest.

| Option | Default | Meaning |
|---|---|---|
| `--timeout` | `5.0` | Seconds per reachability probe. |
| `--strict` | off | Exit 1 on warnings, not only on hard failures. |
| `--show-keys` | off | Print the first four characters of each key. Off by default so output can be pasted anywhere. |

```console
$ fbe doctor
config          ok        weights sum to 1.000, risk cap 2.0%
credentials     ok        FRED_API_KEY present
cache           warn      37 entries, oldest 19h (ttl 12h): run fbe refresh
sources         ok        fred 240ms, stooq 310ms, cftc 890ms
                warn      forexfactory unreachable (timeout after 5.0s)
reports         ok        last report 2026-09-08, config digest matches
1 warning. Calendar coverage is incomplete: treat blackouts as unknown.
```

### `fbe refresh`

| Option | Default | Meaning |
|---|---|---|
| `--source`, `-s` | all available | Repeatable subset, for example `-s fred -s cftc`. |
| `--force`, `-f` | off | Refetch inside the TTL. Use after a data correction or a source outage. |
| `--since` | scoring lookback | Earliest period to request, `YYYY-MM-DD`. |

```console
$ fbe refresh -s fred --force
fred        142 series   1,284 observations   8.2s   cached
stooq       skipped (not selected)
cftc        skipped (not selected)
Cache: 41 entries, newest 0m old.
```

### `fbe score`

One row per currency, strongest to weakest, on the -3 to +3 band. Ranks always
come from the full universe even when `--currency` narrows the printed rows,
because the scores are cross-sectional: a currency has no standing except
relative to the others.

| Option | Default | Meaning |
|---|---|---|
| `--asof` | today | Point-in-time cutoff, `YYYY-MM-DD`. Later releases are excluded. |
| `--format` | `table` | `table`, `json` or `csv`. |
| `--pillars` / `--no-pillars` | off | Show the seven pillar scores behind each composite. |
| `--currency`, `-C` | all | Repeatable row filter. |

```console
$ fbe score --pillars
asof 2026-09-09   config 8f2c1a9d4b70

  #  CCY  Composite  Disp  Cov    Mon    Inf    Gro    Emp    Ext    Pos    Rsk
  1  USD      +1.42  0.61  100%  +1.90  +0.80  +1.10  +0.60  -0.40  +1.20  +0.90
  2  CHF      +0.77  0.44  100%  +0.30  +1.10  +0.20  +0.50  +1.40  +0.60  +1.10
  3  GBP      +0.31  1.02   86%  +1.20  +1.40  -0.60  -0.30  -1.10  +0.40  +0.20
  4  CAD      -0.05  0.55  100%  -0.20  +0.10  +0.30  -0.40  +0.20  -0.30  -0.10
  5  EUR      -0.89  0.38  100%  -1.30  -0.70  -0.90  -0.50  +0.30  -0.80  -0.20
  6  AUD      -0.94  0.71  100%  -0.80  -0.20  -1.40  +0.10  -0.90  -1.30  -1.10
  7  NZD      -1.06  0.66  100%  -1.10  -0.40  -1.20  -0.30  -0.70  -1.40  -1.00
  8  JPY      -1.18  1.31   71%  -2.10  -1.60  +0.40  +0.20  +1.10  -1.90  +0.80

Dispersion is the spread across pillars: JPY at 1.31 means the pillars
disagree, so cut conviction on anything with a JPY leg.
Coverage below 100% means part of the pillar weight had no usable data.
```

The pillar columns are the reason `--pillars` exists. A composite of +1.42 that
rests on one dominant pillar and a composite of +1.42 where seven pillars agree
are different trades, and the ranking alone cannot tell them apart.

### `fbe bias`

| Option | Default | Meaning |
|---|---|---|
| `--asof` | today | Point-in-time cutoff. |
| `--majors` / `--all-pairs` | all pairs | Restrict to the seven dollar majors. |
| `--min-conviction` | `none` | Drop pairs below `none`, `low`, `medium` or `high`. |
| `--tradeable-only` | off | Hide pairs carrying a blocker. |
| `--matrix` / `--ranked` | ranked | Grid layout instead of the ranked list. |
| `--top`, `-n` | all | Truncate the ranked list. |
| `--format` | `table` | `table`, `json` or `csv`. |

```console
$ fbe bias --majors --min-conviction medium --tradeable-only
asof 2026-09-09   config 8f2c1a9d4b70

Pair    Dir    Conv    Spread   Base   Quote  Agree  Notes
USDJPY  long   high     +2.60  +1.42   -1.18   86%   -
NZDUSD  short  high     -2.48  -1.06   +1.42   86%   -
AUDUSD  short  high     -2.36  -0.94   +1.42   71%   -
EURUSD  short  medium   -2.31  -0.89   +1.42   71%   -

3 majors hidden by the filters:
  USDCAD  spread 1.47, just under the 1.50 medium threshold
  GBPUSD  spread 1.11, and GBP coverage is 86%
  USDCHF  spread 0.65, neutral
```

The matrix view answers a different question:

```console
$ fbe bias --matrix
base \ quote   USD    EUR    GBP    JPY    CHF    CAD    AUD    NZD
USD              .  +2.31  +1.11  +2.60  +0.65  +1.47  +2.36  +2.48
EUR          -2.31      .  -1.20  +0.29  -1.66  -0.84  +0.05  +0.17
...
```

Reading down the USD column shows every other currency losing to it. That is
the moment to notice the trade is the dollar, not the pair, and to pick the leg
with the best chart rather than the widest spread.

A run holds 28 pairs in market convention and the grid has 56 populated cells,
so half of them are mirrors. `fbe.report._grid` builds them: spread negated,
legs swapped, direction inverted, conviction unchanged. Both renderers print
what they are given. That arithmetic lives in one function on purpose, because
a mirrored cell left in convention still shows a plausible number under the
wrong row header, and the error would be invisible in exactly the half of the
grid nobody double-checks.

### `fbe calendar`

Each event is expanded into a window using the configured minutes either side,
then windows on the same pair are merged, so the output is a short list of times
to stand aside rather than a wall of releases. Times print in the local zone
with the offset shown: a calendar that is ambiguous about the hour is worse than
no calendar.

| Option | Default | Meaning |
|---|---|---|
| `--hours`, `-H` | `24` | Horizon from now. |
| `--currency`, `-C` | all | Repeatable currency filter. |
| `--pair`, `-p` | all | Repeatable, matches either leg. The check to run before pressing the button. |
| `--impact` | `high` | Minimum published impact. |
| `--blackouts` / `--events` | events | Merged windows per pair instead of the event list. |
| `--format` | `table` | `table`, `json` or `csv`. |

```console
$ fbe calendar --hours 24
Local time SAST (UTC+02:00), now Wed 09 Sep 08:12

When            CCY  Event                       Impact  Blackout
Wed 09 14:30    USD  CPI y/y                     high    14:00 - 15:30
Wed 09 16:00    CAD  BoC rate decision           high    15:30 - 17:00
Thu 10 03:30    AUD  Employment change           high    03:00 - 04:30

Pairs affected today: every USD pair 14:00-15:30, USDCAD also 15:30-17:00.
Clear window for USDJPY: 08:12 - 14:00 and after 15:30.
```

```console
$ fbe calendar --pair EURUSD --hours 4
EURUSD clear for the next 4 hours. Next event on either leg is USD CPI at
14:30 (in 6h 18m), blackout from 14:00.
```

### `fbe size`

Entry and stop are named options rather than positional arguments on purpose:
two bare numbers on a command line are easy to transpose, and a transposed stop
silently doubles the risk.

| Option | Default | Meaning |
|---|---|---|
| `PAIR` | required | Positional, market convention, for example `EURUSD`. |
| `--entry`, `-e` | required | Planned entry price. |
| `--stop`, `-s` | required | Stop price, placed beyond the trendline or channel. |
| `--conviction` | from the engine | Override when the technical setup is better or worse than the bias. |
| `--direction` | from the engine | Override. Sizing against the engine's bias is allowed and warned about. |
| `--balance`, `-b` | configured balance | Account balance. |
| `--risk`, `-r` | conviction-scaled | Risk fraction, clamped to the plan's 1-2% band. |
| `--force` | off | Size anyway inside a blackout. Exits 3 without it. |
| `--format` | `table` | `table`, `json` or `csv`. |

```console
$ fbe size EURUSD --entry 1.0850 --stop 1.0888
EURUSD short, medium conviction (engine agrees)

Balance          ZAR 2,000.00
Intended risk    1.5%  =  ZAR 30.00
Stop distance    38.0 pips
Pip value        ZAR 0.00176 per unit  (USDZAR 17.60)
Unrounded        448.6 units
Lots             0.004       (400 units, rounded down to the 0.001 step)
At risk          ZAR 26.75   =  1.34% of balance
Notional         ZAR 7,638

Limits           1 open position from data/journal/trades.jsonl, written 2026-09-09 18:42 UTC
  max_concurrent_positions  clear          1 open, limit 3
  max_correlated_exposure   clear          USD 2.84% with this trade, limit 4.00%
  max_daily_loss            not performed  today's realised profit and loss was not supplied
  max_drawdown_pause        not performed  no equity peak is recorded anywhere

Warnings
  Lot rounding cuts the risk by 11%. Size and R-multiples are measured on
  ZAR 26.75, not ZAR 30.00.
  If your broker's step is 0.01 lots, the smallest size it accepts is 1,000
  units, which risks ZAR 66.88 on this stop, 3.3% of balance and outside the
  plan's 1-2% band. Skip the trade rather than break the band.
```

The limits block is read from the journal on every run. There is no flag to skip
it: `check_limits` can only compare against a book it is given, and a limit check
run with nothing to check against used to come back clear. The command reads
`data/journal/trades.jsonl`, keeps the records whose `closed_at` is empty, and
prints the count, the path and the file's last write time, because a clear
concurrent limit means nothing without them. The plan's routine allows the
journal entry to be written in the evening, so a position opened this morning and
not yet written is invisible to the count, and the basis is printed so the reader
can see what the check could see.

Each limit reads **clear**, **breached** or **not performed**, and not performed
is never printed as clear. The last two limits read not performed on every run
today, because nothing supplies the day's realised profit and loss and nothing
records an equity peak. They are checked by hand, per section 4 of
`docs/risk-and-execution.md`. A breach prints here as a warning and does not
change the exit code; issue #46 decides whether it should.

An absent journal and an unreadable one are different facts and print
differently. No file is a fresh install with no trades. A file that will not
parse means the book is not known, which is not the same as empty:

```console
$ fbe size EURUSD --entry 1.0850 --stop 1.0888
...
Limits           open positions not known: data/journal/trades.jsonl could not be parsed
  max_concurrent_positions  not performed  the open book could not be read
  max_correlated_exposure   not performed  the open book could not be read
  max_daily_loss            not performed  today's realised profit and loss was not supplied
  max_drawdown_pause        not performed  no equity peak is recorded anywhere
```

Two risk figures, and the second one is the real one. `risk_amount` is what the
1-2% rule intends; `realised_risk_amount` is what the position exposes once the
lot size is rounded down to a step the broker accepts. On an R2,000 account that
gap is routinely 10% or more, and an R-multiple measured against the intended
figure is overstated by exactly that ratio. Every money field on the ticket,
`Notional` included, is in the account currency.

```console
$ fbe size USDJPY --entry 147.20 --stop 147.90
Refusing to size: USD CPI at 14:30 puts USDJPY in a blackout from 14:00 to
15:30, and the plan says to stand aside around high-impact news.
Re-run after 15:30, or pass --force if you are deliberately trading the event.
(exit 3)
```

### `fbe report`

| Option | Default | Meaning |
|---|---|---|
| `--asof` | today | Point-in-time cutoff. |
| `--out`, `-o` | `data/reports` | Output directory for the dated file. |
| `--compare` | `last` | Diff baseline: `last`, `none`, or a path. |
| `--stdout` | off | Print the Markdown instead of writing a file. |

```console
$ fbe report
Wrote data/reports/bias-2026-09-09.md (14.2 KB)
Compared against bias-2026-09-08.md: 1 direction flip, 2 shortlist changes.
```

### `fbe dashboard`

| Option | Default | Meaning |
|---|---|---|
| `--asof` | today | Point-in-time cutoff. |
| `--out`, `-o` | `data/reports/dashboard-YYYY-MM-DD.html` | Output file. |
| `--compare` | `last` | Diff baseline, same values as `report --compare`. |
| `--open` / `--no-open` | no-open | Open the result in a browser. |

```console
$ fbe dashboard --open
Wrote data/reports/dashboard-2026-09-09.html (38 KB)
Constraint check: 0 external assets, 38 KB of 16 MB, theme tokens ok.
```

### `fbe journal add`

The engine's bias for that pair on that date, its conviction and the config
digest are attached automatically. That is the point: months later the journal
should be able to say whether the losing trades were the ones taken against the
engine, the ones taken against the plan, or neither.

| Option | Default | Meaning |
|---|---|---|
| `PAIR` | required | Positional, market convention. |
| `--direction`, `-d` | required | `long`, `short` or `neutral`, on the base currency. |
| `--entry`, `-e` | required | Fill price. |
| `--stop`, `-s` | required | Stop price at entry. |
| `--exit`, `-x` | open | Exit price. Omit while the trade is still open. |
| `--lots` | none | Size traded. |
| `--opened` | now | `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`. |
| `--setup` | none | Technical setup label, for example `channel-low-bounce`. |
| `--followed-plan` / `--broke-plan` | followed | Whether the trade obeyed the plan, independent of whether it made money. |
| `--note`, `-m` | none | Free text for the lesson or the context. |
| `--tag`, `-t` | none | Repeatable label for grouping in review. |

```console
$ fbe journal add EURUSD -d short -e 1.0850 -s 1.0888 -x 1.0791 \
    --lots 0.004 --setup trendline-break-retest --followed-plan \
    -m "Waited for the retest instead of chasing the break."
Recorded EURUSD short, +59.0 pips, ZAR +41.54, +1.55R.
R measured against the realised risk of ZAR 26.75, not the intended ZAR 30.00.
Engine that day: short, medium conviction, spread -2.31. Aligned.
```

### `fbe journal review`

Reports the plain performance numbers, then the two splits that actually change
behaviour. A losing week where every trade followed the plan and the bias is a
model problem. A winning week full of trades that broke the plan is a discipline
problem waiting to become a loss.

| Option | Default | Meaning |
|---|---|---|
| `--days`, `-d` | `7` | Window back from today. |
| `--pair`, `-p` | all | Repeatable pair filter. |
| `--tag`, `-t` | all | Repeatable tag filter. |
| `--open-only` | off | Only trades with no exit recorded. |
| `--format` | `table` | `table`, `json` or `csv`. |

```console
$ fbe journal review --days 7
7 days to 2026-09-09: 6 trades, 5 closed, 1 open

Closed P&L        ZAR +38.20      Win rate  60%      Average  +0.41R
Largest loss      ZAR -23.20      Largest win  ZAR +41.54
R-multiples are measured against realised risk, after lot rounding.

Followed the plan   4 trades   ZAR +61.40   avg +0.92R
Broke the plan      1 trade    ZAR -23.20   avg -1.16R
With engine bias    4 trades   ZAR +52.00   avg +0.78R
Against the bias    1 trade    ZAR -13.80   avg -0.69R

Open: GBPUSD long from 1.3120, stop 1.3068, 2 days held.
The single plan break was the single worst trade of the week.
```

## The report

`fbe report` writes two files per run: `data/reports/bias-YYYY-MM-DD.md` and
`data/reports/bias-YYYY-MM-DD.json`. The Markdown is for reading. The JSON is
the serialised `BiasReport` and is the only thing `--compare` reads back, so the
layout of the Markdown never becomes load-bearing.

Both files are written or neither is. Writing the Markdown alone fails silently:
every run succeeds, `--compare last` still finds a report, and the what-changed
section is empty forever with nothing raising to say why.

Sections, in order:

1. **Header.** As-of date, generation time, config digest, pillar weights.
2. **Currency ranking.** Composite, rank, dispersion, coverage, pillar columns.
3. **Pair matrix.** The 8x8 grid, then the same pairs as a ranked list.
4. **Tradeable shortlist.** Each idea with its reasoning, the pillars carrying
   it, the size if one is attached, and any blackout.
5. **Calendar and blackouts.**
6. **Data coverage and warnings.**
7. **What changed since the last run.**

### Why reports go to disk

A bias call you cannot audit a week later is worthless. When a trade goes wrong
the only useful question is which part was wrong: the fundamental call, the
conviction on it, the size, or the entry. That cannot be answered from memory,
and it cannot be answered from a terminal window that was closed on Tuesday.
Markdown because it stays readable in ten years, diffs cleanly, and opens
anywhere.

The files are meant to be kept alongside the code, and a report is only
meaningful together with the config digest that produced it. Re-weighting the
pillars changes every future call, and without the historical files there is no
way to tell whether the model improved or simply started agreeing with a
different set of trades.

`data/reports/` is committed for this reason, as `CLAUDE.md` sets out. A report
is precisely what cannot be reproduced later: macro series get revised,
cross-sectional scores depend on the rest of the universe on the day, and the
weights may have changed since. Re-running last week's date does not recover
last week's call.

### Why the diff is its own section

The diff between two runs is often more informative than either run alone.
A JPY composite of -1.18 says little on its own. The same -1.18 after -0.15
yesterday says something repriced overnight, which is the difference between a
number to note and a chart to open. Fundamentals move slowly, so any large
one-day move in a score is either news worth trading or a data error worth
fixing, and both deserve to be pointed at rather than left to be spotted.

A worked example of a useful diff:

```markdown
## 6. What changed since the last run

Against 2026-09-08:

| CCY | Then  | Now   | Delta | Rank   |
|-----|------:|------:|------:|--------|
| JPY | -0.15 | -1.18 | -1.03 | 4 to 8 |
| USD | +1.20 | +1.42 | +0.22 | 1 to 1 |
| GBP | +0.68 | +0.31 | -0.37 | 2 to 3 |

Direction and conviction moves:

- USDJPY: long to long, medium to high
- AUDJPY: short to long (flip), low to medium
- GBPUSD: short to short, high to medium

Entered the shortlist: USDJPY, NZDUSD

Left the shortlist:
- GBPJPY: conviction fell to low after GBP coverage dropped to 86%

New warnings:
- forexfactory unreachable, calendar is 19 hours stale
```

Three things fall out of that block that no single run shows. JPY moving -1.03
in a day is not a fundamental move, it is the monetary pillar repricing on a BoJ
signal, so the first thing to check is whether the input is real. The AUDJPY
flip is a change in the story rather than in a number, which is why flips are
called out explicitly. And GBPJPY left the shortlist for a data reason, not a
market reason, which means nothing about GBPJPY actually changed.

When the config digest differs between the two runs, the diff says so and
reports the deltas as not comparable. Re-weighting moves every currency at once,
and reading that as a market move is the fastest way to talk yourself into a
trade that is not there.

## The dashboard

One HTML file, built by `fbe dashboard`, holding the same run as the report and
laid out for a phone. It exists because most of the trading day happens away
from the terminal: the pre-market work produces a bias, and four hours later the
only question is what the bias was and whether the window is clear.

### Layout

Single column, in the order the trading day needs it:

1. **Header.** As-of date, generation time, config digest, theme toggle.
2. **Currency ranking** as a horizontal bar ranking, strongest at the top, bars
   growing left and right from a centre zero line. Diverging by sign, because
   the sign is the whole message. Rows with incomplete coverage are faded.
3. **The 28-pair matrix** as a heatmap, base down the rows and quote across the
   columns, cell colour by spread. Diverging scale centred on zero with a
   neutral grey midpoint, so a near-zero cell reads as "no view" rather than as
   a weak signal. The ranked table underneath carries the same numbers, so no
   value is available only through colour.
4. **Shortlist as cards**, one per idea: pair, direction, conviction, reasoning,
   size if attached, blackout if any. Cards rather than a table because this is
   the part read on a phone at arm's length.
5. **Calendar strip.** A 24 hour axis with blackout windows shaded, events
   ticked and the current time marked, so "is the window clear" is answered by
   looking rather than by reading.
6. **Coverage, warnings and the run-to-run diff** in the footer. Both matter and
   neither should be the first thing on the screen.

Colour and layout follow the repository's data visualisation conventions: a
diverging pair with a neutral grey midpoint for signed values, never a rainbow
and never a hue at the midpoint; categorical hues in fixed order, never cycled;
text in text tokens rather than in a series colour; dark mode chosen against the
dark surface rather than flipped automatically; and a legend plus a table view
so nothing is carried by colour alone.

### Publishing constraints

The file is built to be published as a hosted artifact page and opened on a
phone. That sandbox enforces the following, and a violation fails silently at
view time, leaving a blank panel rather than an error.
`fbe.dashboard.build.check_constraints` checks the rendered output and
`build_dashboard` refuses to write a page that breaks one.

- **One file, no external assets.** Scripts only from `cdnjs.cloudflare.com` or
  `cdn.jsdelivr.net/npm/`; stylesheets only from `fonts.googleapis.com` with
  their font files from `fonts.gstatic.com`. Everything else is blocked:
  external images, other stylesheets, `fetch`, `XHR`, WebSocket. Inline all CSS
  and JS, embed any image as a `data:` URI. In practice the page needs no
  external script at all: the charts are inline SVG and CSS built from the
  report.
- **Real fallback stacks on any web font**, because the font request can fail
  while the page still renders.
- **Theme-aware in three states.** The full light palette is defined as CSS
  custom properties on bare `:root`. The dark values are redefined inside
  `@media (prefers-color-scheme: dark)` guarded as
  `:root:not([data-theme="light"])`, and again under `:root[data-theme="dark"]`
  so the toggle wins in both directions. No colour gets its only definition
  inside a media or `[data-theme]` block, and `body` sets an explicit background
  token: the host paints its own ground behind a transparent body, which then
  inherits the wrong theme.
- **Responsive to 400px** with at least a 16px side gutter and no horizontal
  page scroll. The gutter is set once as `padding-inline` on one wrapper, with
  vertical padding as `padding-block` so a shorthand can never zero the sides.
  The pair matrix is wider than a phone, so it lives in its own
  `overflow-x: auto` container and scrolls on its own.
- **Under 16MB rendered**, `data:` URIs included. A G10 run is a few tens of
  kilobytes of numbers, so the only way to approach that ceiling is by embedding
  raster images. Do not embed raster images.

## Where things live

| Path | Contents |
|---|---|
| `src/fbe/cli.py` | The `fbe` command surface. Entry point `fbe = "fbe.cli:app"`. |
| `src/fbe/report.py` | Markdown rendering, `ReportDiff` and `diff_reports`. |
| `src/fbe/templates/report.md.j2` | The report template. |
| `src/fbe/dashboard/build.py` | Dashboard rendering and the constraint check. |
| `src/fbe/dashboard/templates/dashboard.html.j2` | The dashboard template. |
| `data/reports/` | Dated Markdown reports, their JSON sidecars and built dashboards. Committed. |

Templates ship inside the package rather than at the repo root, so a report
renders identically from a checkout and from an installed wheel.
`fbe.report.build_context` builds the variables both templates render against,
which is what keeps the two views from drifting apart.
