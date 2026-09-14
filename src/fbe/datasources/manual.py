"""Operator-entered data: the escape hatch for what free APIs will not supply.

Nineteen of the registry's refs have no free machine-readable source. PMIs are
licensed, for all eight currencies. Two-year yields for CHF and NZD are on no
free API, the other six having landed on their issuing institutions' own feeds.
Euro-area employment change, industrial production for three currencies and the
New Zealand dairy link are the rest. Central bank guidance tone has never been a
number at all.

Inflation is no longer on that list. FRED's OECD complex did stop updating in
early 2025, but the OECD's own API carries headline and core CPI for all eight,
so an ``inflation.yaml`` left over from before that is not a gap being filled,
it is live data being shadowed by a stale hand-typed copy.

This source reads those values from YAML files the operator maintains by hand.
It is not a workaround to be replaced later; it is a permanent part of the
design, because some of these numbers will never be free.

Layout
------
``data/manual/*.yaml``, one file per topic. `DataConfig.manual_dir` points at
the directory. Files are read in sorted filename order, and a later file
overrides an earlier one for the same ``(indicator, currency, period)``, which
gives an operator a clean way to patch one value without editing a large file.

Schema
------
Every file carries a ``meta`` block and an ``observations`` list.

    meta:
      source: "S&P Global via investing.com"
      updated: 2026-09-08
      updated_by: "operator"
      notes: >
        Manufacturing PMI headline prints entered under pmi_composite as a
        stated approximation, pending a services print to blend in. Flash
        where marked, otherwise final.

    observations:
      - indicator: pmi_composite
        currency: EUR
        value: 49.8
        period: 2026-08-01
        unit: index
        frequency: monthly
        released_at: 2026-09-01T08:00:00Z
        meta:
          release: flash
          source_url: "https://..."

Field rules:

* ``indicator`` must be a key in `fbe.datasources.registry.INDICATORS`. An
  unknown key is an error, not a warning. A typo that silently creates a new
  indicator is invisible until a pillar quietly reports missing data.
* ``currency`` must be in `fbe.universe.G10`, or ``"GLOBAL"``.
* ``period`` is the period the number describes, not the day it was typed. For
  a monthly series use the first of the month, matching what FRED does, so that
  manual and fetched observations sort together.
* ``unit`` and ``frequency`` should match the registry's `SeriesRef` for that
  pair. A mismatch is worth refusing rather than coercing: a PMI entered as a
  percentage instead of an index will score, and score wrongly.
* ``released_at`` is optional but strongly wanted, and not for the reason this
  file used to give. The staleness penalty measures from ``period`` either way,
  as `fbe.pillars.base.BasePillar.staleness_days` says outright. What
  ``released_at`` drives is visibility: absent, the pillar falls back to
  `DEFAULT_PUBLICATION_LAG_DAYS` to decide whether a figure had been published
  by a given date, which in Phase 6 is the difference between a backtest that
  saw a number and one that could not have.
* ``meta`` is free-form and is the right place for the URL the number came
  from. That provenance is the only audit trail a hand-typed number has.

Guidance tone
-------------
``cb_guidance_tone`` encodes what a central bank signalled at its last meeting,
on ``-1..+1``, where negative is dovish and positive hawkish. It is a judgement,
and recording it as one, with the meeting date and the reasoning in ``meta``, is
better than pretending it falls out of the data. It is not in the registry,
because the registry maps indicators to external series and this has none.

**`load_file` refuses it today**, and that is a gap rather than a decision. Every
row is validated against `fbe.datasources.registry.INDICATORS`, which is what
makes a typo an error instead of a silent new indicator, and a key that is
deliberately outside the registry fails the same check. Nothing here carves it
out, because doing so needs a unit and a frequency for a series the registry
does not describe, and inventing those is the thing this module exists to stop.
So no ``guidance.yaml`` is suggested below, and one written anyway refuses the
whole directory rather than that file alone. See #60.

Operating discipline
--------------------
Hand-entered data is the most likely thing in this repo to be wrong, and the
least likely to announce it. Three rules follow.

Refuse silently-wrong input: validate against the registry and raise, rather
than skipping bad rows. Age it honestly: a stale manual value must decay like
any other, and a PMI last updated in March should stop counting toward coverage
in April. And keep it visible: the report should name every manual value it
used, so an operator can see at a glance how much of a currency's score they
typed themselves.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from fbe.datasources.base import BaseDataSource, RateLimit, RetryPolicy, SourceError
from fbe.datasources.registry import INDICATORS, SeriesRef
from fbe.types import Observation
from fbe.universe import G10

__all__ = [
    "FILE_GLOB",
    "GLOBAL_CURRENCY",
    "GUIDANCE_TONE_KEY",
    "NEAR_MISS_SUFFIXES",
    "LEVEL_TRANSFORM",
    "REQUIRED_FIELDS",
    "SUGGESTED_FILES",
    "ManualSource",
]


FILE_GLOB = "*.yaml"
"""Read in sorted filename order; later files override earlier ones."""

NEAR_MISS_SUFFIXES: frozenset[str] = frozenset({".yml", ".yaml"})
"""Suffixes that mean "this was meant to be read" and that `FILE_GLOB` misses.

The glob is ``*.yaml`` and is case-sensitive, so ``pmi.yml`` and ``pmi.YAML``
are both read by nobody and noticed by nobody: the observations in them would
simply not exist, and the gap would surface as low coverage weeks later with
nothing pointing at the cause. Both are refused on sight, which costs an
operator one rename.

The test is the suffix lower-cased, so it catches the case variants and the
other spelling and nothing else. ``pmi.yaml.bak`` is deliberately outside it: a
file somebody kept a copy of is not one they expected to be loaded, and
refusing it would make the directory unusable as a working area."""

GLOBAL_CURRENCY = "GLOBAL"
"""The one accepted code outside `fbe.universe.G10`, for cross-market series."""

LEVEL_TRANSFORM = "level"
"""The transform value meaning the source publishes the canonical number itself.

It matters here only as a consistency check. A transformed ref carries the
unit the source publishes, and `IndicatorSpec.unit` carries the canonical unit
after the transform, so the two legitimately differ. A ``level`` ref whose unit
differs from its indicator's is the registry contradicting itself, and a row
under it is refused rather than labelled with a guess. One pair does that
today, ``commodity_price`` for CAD, which is a FRED ref in dollars per barrel
under an indicator declared as an index.

This module does not read `SeriesRef.transform` for anything else, because a
manual row never passes through a transform stage: only `fred.py` and
`curves.py` read that field, both at fetch time, and a typed number is already
the canonical value. An earlier draft refused every transformed ref and said
`FredSource` does the same. That was wrong. `FredSource` maps ``yoy`` to
``pc1`` and ``diff`` to ``chg`` and fetches them; it refuses ``chg_1m`` and
``chg_3m`` alone, and for its own reason, which is that FRED's ``chg`` is a
one-day change on a daily series. None of that bears on a figure an operator
types."""

REQUIRED_FIELDS: tuple[str, ...] = (
    "indicator",
    "currency",
    "value",
    "period",
)
"""Fields every observation row must carry. ``unit``, ``frequency``,
``released_at`` and ``meta`` are optional, with ``unit`` and ``frequency``
defaulting to the registry's `SeriesRef` for that indicator and currency."""

OPTIONAL_FIELDS: tuple[str, ...] = (
    "unit",
    "frequency",
    "released_at",
    "revision",
    "meta",
)

META_FIELDS: tuple[str, ...] = (
    "source",
    "updated",
    "updated_by",
    "notes",
)
"""Keys in each file's ``meta`` block. ``source`` and ``updated`` carry the
provenance and the age, which are the two things a reviewer needs to decide
whether to trust a hand-typed number."""

SUGGESTED_FILES: Mapping[str, str] = {
    "pmi.yaml": (
        "Manufacturing and services PMIs for all eight currencies. Licensed by "
        "S&P Global and ISM, so on no free API. The largest single manual "
        "burden and the one worth automating first if a licence is ever bought."
    ),
    "yields.yaml": (
        "Two-year government bond yields for CHF and NZD only. The other six "
        "are fetched: the ECB, the Bank of Canada, the RBA, the Bank of "
        "England and the Japanese Ministry of Finance each publish their own. "
        "These two carry the front end of the monetary pillar for their "
        "currencies, which is the heaviest pillar in `ScoringConfig`, so they "
        "matter more than their count suggests."
    ),
    "zz-overrides.yaml": (
        "Ad-hoc corrections. Named to sort last, so it wins over everything "
        "above. Use it to patch one bad value without editing a whole topic "
        "file."
    ),
}
"""Suggested file split. Nothing enforces these names; the loader globs the
directory. They are a starting layout that keeps each file small enough to
review at a glance.

The override file is named ``zz-overrides.yaml`` rather than
``overrides.yaml`` because precedence is filename order and nothing else.
``overrides.yaml`` sorts ahead of both ``pmi.yaml`` and ``yields.yaml``, so
under the name it used to carry it would have been overridden by the two files
it exists to override, and the mistake would have shown as the original value
quietly surviving the correction."""

GUIDANCE_TONE_KEY = "cb_guidance_tone"
"""Not in the registry, because it maps to no external series. Read directly by
the monetary pillar if that pillar wants it. Range ``-1..+1``, dovish to
hawkish."""

RATE_LIMIT = RateLimit(requests=1000, per_seconds=1.0)
"""Local disk. The limit exists only so the base class has one to apply."""


class ManualSource(BaseDataSource):
    """Reads operator-entered observations from ``data/manual/*.yaml``.

    Backs whatever the free APIs cannot: PMIs across the board, non-US 2y
    yields, CPI outside USD and EUR, and any one-off correction.

    Attributes:
        name: ``"manual"``, matching ``SeriesRef.source`` on every manual ref.

    """

    name = "manual"
    rate_limit = RATE_LIMIT
    retry = RetryPolicy(attempts=1)

    def available(self) -> bool:
        """Report whether the manual directory exists.

        An empty directory is available and contributes nothing, which is the
        correct state for a fresh checkout: the engine runs, and the report
        shows the gaps rather than refusing to start. A directory that is not
        there is a different fact, a misconfigured `DataConfig.manual_dir`, and
        answering that with an empty result would show up as a coverage gap
        with no cause attached.

        The summary line of this docstring used to read "exists and holds at
        least one file", which contradicted the paragraph under it. The
        paragraph is the one that is right, and #60 records the correction.

        Returns:
            True when `DataConfig.manual_dir` is an existing directory, empty
            or not. False when it does not exist, or exists and is not a
            directory.

        """
        return self.config.manual_dir.is_dir()

    def fetch(
        self,
        indicators: Iterable[str],
        currencies: Iterable[str],
        start: date,
        end: date,
    ) -> Sequence[Observation]:
        """Return matching operator-entered observations.

        Args:
            indicators: Canonical indicator keys to return.
            currencies: ISO 4217 codes, plus ``"GLOBAL"``.
            start: Earliest period wanted, inclusive.
            end: Latest period wanted, inclusive.

        Returns:
            Observations with ``source`` set to ``"manual"``, so a report can
            separate typed numbers from fetched ones.

        Raises:
            SourceError: If a file is unparseable or a row fails validation.
                Manual data fails loudly on purpose. A skipped bad row is a
                silent hole, and silent holes are what this source exists to
                fill.

                Also when `DataConfig.manual_dir` does not exist. An empty
                directory returns an empty sequence, because a topic nobody has
                typed in yet holds no observations and that is data; a missing
                directory is a misconfiguration and is not.

        """
        wanted_indicators = set(indicators)
        wanted_currencies = set(currencies)
        return [
            observation
            for (indicator, currency, period), observation in sorted(
                self._entries().items()
            )
            if indicator in wanted_indicators
            and currency in wanted_currencies
            and start <= period <= end
        ]

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is ``"manual"``.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`. Derived
            from the registry rather than listed here, so a ref moved onto or
            off this source moves this mapping with it in the same commit.

        """
        return {
            (spec.key, currency): ref
            for spec in INDICATORS.values()
            for currency, ref in spec.series.items()
            if ref.source == self.name
        }

    def load_file(self, path: Path) -> Sequence[Observation]:
        """Parse and validate one YAML file.

        Args:
            path: Path to the file.

        Returns:
            Observations from that file's ``observations`` list.

        Raises:
            SourceError: On malformed YAML, a missing required field, an
                unknown field, a value that is not a finite number, an
                unreadable date, an indicator absent from the registry, a
                currency outside the universe, a unit or frequency that
                contradicts the registry, or an indicator whose ref carries a
                transform other than `LEVEL_TRANSFORM`.

                Every one of these refuses the whole file rather than the one
                row. A file that loads with rows missing looks like a working
                file, and the rows that went are the ones nobody sees.

        """
        document = self._document(path)
        rows = document.get("observations")
        if rows is None:
            raise SourceError(
                f"{path.name} has no 'observations' list. An empty list is a "
                "topic nobody has filled in yet and is fine; the key missing "
                "altogether means the file is not in this schema."
            )
        if not isinstance(rows, list):
            raise SourceError(
                f"{path.name} has an 'observations' key that is not a list, "
                f"it is {type(rows).__name__}"
            )
        return [self._row(row, path, index) for index, row in enumerate(rows)]

    def missing(self, asof: date) -> Mapping[str, tuple[str, ...]]:
        """Report which manual refs have no usable value as of a date.

        The operator's to-do list. Run it before a session and it names exactly
        which numbers need typing in, rather than leaving the gaps to show up
        as a low coverage figure after the fact.

        Args:
            asof: The date to evaluate against. The rule here is a cliff at the
                indicator's allowance, not the ramp `fbe.scoring.freshness`
                applies: this answers "is there a usable value", while the ramp
                answers "how much weight does it still carry". A PMI sixty days
                old is not on this list and does carry a reduced weight in the
                pillar, and those are both correct answers to different
                questions.

        Returns:
            Indicator key to the currencies still missing a fresh value, each
            tuple sorted so the list does not reshuffle between runs. An
            indicator with nothing outstanding is absent from the mapping
            rather than present with an empty tuple, so the result reads as a
            to-do list and an empty mapping means there is nothing to do.

            "Fresh" is measured from ``period``, the span the figure describes,
            against that indicator's own `IndicatorSpec.max_staleness_days`.
            Per indicator rather than one number for the registry, because the
            cadences here differ by an order of magnitude: a 2-year yield a
            week old means the feed broke, while a quarterly figure five months
            old is routinely the most current one published.

            Every pair on this list can be filled in. An earlier draft
            refused four of them on entry while still reporting them here,
            which would have sent an operator to type something the loader
            then rejected.

        Raises:
            SourceError: When `DataConfig.manual_dir` does not exist, or a file
                in it fails to load. Reporting an empty directory as "nothing
                missing" would invert the answer.

        """
        newest: dict[tuple[str, str], date] = {}
        for (indicator, currency, period), _ in self._entries().items():
            key = (indicator, currency)
            if period > newest.get(key, date.min):
                newest[key] = period

        outstanding: dict[str, list[str]] = {}
        for (indicator, currency), _ in self.refs().items():
            entered = newest.get((indicator, currency))
            allowance = INDICATORS[indicator].max_staleness_days
            # A period after `asof` is outstanding too. Without the lower bound
            # a mistyped year reads as a negative age, so the pair drops off
            # this list while `fetch` still filters the row out of its window:
            # the pillar loses the currency, coverage falls, and the one tool
            # built to say why reports nothing to do.
            if entered is None or entered > asof or (asof - entered).days > allowance:
                outstanding.setdefault(indicator, []).append(currency)
        return {
            indicator: tuple(sorted(currencies))
            for indicator, currencies in outstanding.items()
        }

    def template(self, indicator: str, currency: str) -> str:
        """Return a ready-to-paste YAML stub for one indicator and currency.

        Pre-fills ``unit`` and ``frequency`` from the registry so the operator
        cannot get those wrong, and leaves ``value``, ``period`` and
        ``released_at`` blank.

        The row comes back **commented out**, under an ``observations`` list
        that is empty and therefore loads. That shape is what lets the two
        things asked of this method both hold at once: the blanks stay blank,
        and `load_file` accepts the file unedited. The alternative, a row with
        ``value`` filled in so the file parses, would put a number nobody typed
        into the one file whose whole purpose is to be copied, which is the
        first line of the prime directive. The operator's edit is uncommenting
        the block and filling two fields.

        Args:
            indicator: Canonical indicator key.
            currency: ISO 4217 code, or ``"GLOBAL"``.

        Returns:
            A whole file, not a fragment: ``meta`` block, an empty
            ``observations`` list, and the commented row. ``unit`` and
            ``frequency`` are the registry's for this pair, so an operator who
            uncomments the block cannot contradict them by accident.

        Raises:
            KeyError: If the indicator is not in the registry.
            ValueError: If the currency is outside the universe, or the
                registry contradicts itself for the pair in the way `load_file`
                refuses. Emitting a stanza that this source rejects would waste
                the operator's typing and teach them the format is wrong.

        """
        spec = INDICATORS[indicator]
        if currency not in G10 and currency != GLOBAL_CURRENCY:
            raise ValueError(
                f"{currency!r} is outside the universe: {', '.join(G10)}, "
                f"{GLOBAL_CURRENCY}"
            )
        ref = spec.series.get(currency)
        if (
            ref is not None
            and ref.transform == LEVEL_TRANSFORM
            and ref.unit != spec.unit
        ):
            raise ValueError(
                f"{indicator} / {currency} publishes {ref.unit!r} under an "
                f"indicator declared as {spec.unit!r} with no transform "
                "between them, which load_file refuses; see the note there"
            )
        unit = spec.unit
        frequency = ref.frequency if ref is not None else spec.frequency
        return (
            "meta:\n"
            '  source: ""\n'
            "  updated:\n"
            '  updated_by: ""\n'
            "\n"
            "observations: []\n"
            "\n"
            "# Uncomment and fill in value and period. released_at is optional\n"
            "# and strongly wanted: without it staleness falls back to period.\n"
            f"# - indicator: {indicator}\n"
            f"#   currency: {currency}\n"
            "#   value:\n"
            "#   period:\n"
            f"#   unit: {unit}\n"
            f"#   frequency: {frequency.value}\n"
            "#   released_at:\n"
            "#   meta:\n"
            '#     source_url: ""\n'
        )

    # --- internals ---------------------------------------------------------

    def _files(self) -> list[Path]:
        """Return the files to read, oldest precedence first.

        Returns:
            Paths matching `FILE_GLOB`, in sorted filename order. Later entries
            override earlier ones, so the sort is the precedence rule and not a
            tidiness one: an unsorted directory listing would make which value
            wins depend on the filesystem.

        Raises:
            SourceError: When `DataConfig.manual_dir` does not exist, or when a
                file matching `IGNORED_GLOB` sits in it. The second is not
                pedantry: that file would be read by nobody and its absence
                would surface weeks later as coverage that never arrived.

        """
        directory = self.config.manual_dir
        if not self.available():
            raise SourceError(
                f"the manual directory {directory} does not exist, so there is "
                "nothing to read. This is a configuration problem, not an "
                "empty one: create the directory, or point DataConfig."
                "manual_dir somewhere else."
            )
        readable = sorted(directory.glob(FILE_GLOB))
        near_misses = sorted(
            path.name
            for path in directory.iterdir()
            if path.is_file()
            and path not in readable
            and path.suffix.lower() in NEAR_MISS_SUFFIXES
        )
        if near_misses:
            raise SourceError(
                f"{', '.join(near_misses)} would be read by nobody: this "
                f"source globs {FILE_GLOB!r}, case-sensitively. Rename rather "
                "than leaving the observations in them silently out of every "
                "run."
            )
        return readable

    def _entries(self) -> dict[tuple[str, str, date], Observation]:
        """Return every entered observation, overrides already applied.

        Returns:
            Mapping from ``(indicator, currency, period)`` to the observation
            that wins for it. Files are applied in `_files` order, so the last
            file to name a key is the one kept, which is what lets one small
            file correct a large one.

        Raises:
            SourceError: As `load_file`, plus the `_files` cases, plus one key
                appearing twice inside a single file.

        """
        entries: dict[tuple[str, str, date], Observation] = {}
        for path in self._files():
            seen: set[tuple[str, str, date]] = set()
            for observation in self.load_file(path):
                key = (observation.indicator, observation.currency, observation.period)
                if key in seen:
                    indicator, currency, period = key
                    raise SourceError(
                        f"{path.name} carries {indicator} / {currency} for "
                        f"{period.isoformat()} twice. Across files that is the "
                        "override rule; inside one file it has no use and is a "
                        "value typed twice, so it is refused rather than "
                        "resolved by position."
                    )
                seen.add(key)
                entries[key] = observation
        return entries

    def _document(self, path: Path) -> Mapping[str, Any]:
        """Parse one file into a mapping.

        Args:
            path: File to read.

        Returns:
            The parsed document.

        Raises:
            SourceError: On unreadable bytes, malformed YAML, or a document
                that is not a mapping. The filename is in every message,
                because an operator with five files needs to know which one.

        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise SourceError(f"{path.name} could not be read: {error}") from error
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise SourceError(f"{path.name} is not valid YAML: {error}") from error
        if document is None:
            raise SourceError(
                f"{path.name} is empty. Delete it, or give it an "
                "'observations' list, which may itself be empty."
            )
        if not isinstance(document, dict):
            raise SourceError(
                f"{path.name} parsed as {type(document).__name__} rather than a "
                "mapping with 'meta' and 'observations' keys"
            )
        return document

    def _row(self, row: object, path: Path, index: int) -> Observation:
        """Validate one observation row and build its `Observation`.

        Args:
            row: The parsed row.
            path: File it came from, named in every error.
            index: Position in the ``observations`` list, zero based, so an
                error points at a row rather than at a file of forty.

        Returns:
            The observation. ``source`` is this source's name and ``series_id``
            is the indicator key the file named, because a hand-typed number
            has no vendor identifier and borrowing the fetched ref's would
            claim a provenance it does not have. ``unit`` and ``frequency``
            come from the registry, never from the file, so those two cannot
            drift from what a pillar assumes.

        Raises:
            SourceError: On any of the cases `load_file` documents.

        """
        where = f"{path.name} observation {index}"
        if not isinstance(row, dict):
            raise SourceError(f"{where} is {type(row).__name__} rather than a mapping")

        allowed = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
        unknown = sorted(set(row) - allowed)
        if unknown:
            raise SourceError(
                f"{where} carries unknown field(s) {', '.join(unknown)}. "
                f"Accepted: {', '.join(sorted(allowed))}. Refused rather than "
                "ignored, because a misspelt optional field would otherwise "
                "drop out of the observation with nothing said."
            )
        for field in REQUIRED_FIELDS:
            if row.get(field) is None:
                raise SourceError(
                    f"{where} has no {field}. Required fields are "
                    f"{', '.join(REQUIRED_FIELDS)}."
                )

        indicator = self._text(row["indicator"], "indicator", where)
        if indicator not in INDICATORS:
            raise SourceError(
                f"{where} names indicator {indicator!r}, which is not in the "
                "registry. Refused rather than skipped: a typo that silently "
                "creates a new indicator is invisible until a pillar quietly "
                "reports missing data."
            )
        currency = self._text(row["currency"], "currency", where)
        if currency not in G10 and currency != GLOBAL_CURRENCY:
            raise SourceError(
                f"{where} names currency {currency!r}, which is outside the "
                f"universe. Accepted: {', '.join(G10)}, {GLOBAL_CURRENCY}."
            )

        spec = INDICATORS[indicator]
        ref = spec.series.get(currency)
        if (
            ref is not None
            and ref.transform == LEVEL_TRANSFORM
            and ref.unit != spec.unit
        ):
            raise SourceError(
                f"{where} names {indicator} / {currency}, whose registry ref "
                f"publishes {ref.unit!r} under an indicator declared as "
                f"{spec.unit!r} with no transform between them. The registry "
                "contradicts itself for this pair, so there is no unit to put "
                "on a typed number. Fix the registry rather than this row."
            )
        # The indicator's unit, not the ref's. `IndicatorSpec.unit` is
        # documented as the canonical unit after any transform, which is what a
        # pillar assumes; `SeriesRef.unit` is what a source publishes before
        # one. A typed number never passes through a transform stage, so it is
        # already the canonical value. The frequency goes the other way: the
        # spec's is the typical cadence across the universe and the ref's is
        # what this currency actually publishes, quarterly for Swiss industrial
        # production against a monthly indicator.
        unit = spec.unit
        frequency = ref.frequency if ref is not None else spec.frequency
        self._agrees(row, "unit", unit, where)
        self._agrees(row, "frequency", frequency, where)

        return Observation(
            indicator=indicator,
            currency=currency,
            value=self._value(row["value"], where),
            period=self._period(row["period"], where),
            source=self.name,
            series_id=indicator,
            unit=unit,
            frequency=frequency,
            released_at=self._released_at(row.get("released_at"), where),
            revision=self._revision(row.get("revision"), where),
            meta=self._meta(row.get("meta"), where),
        )

    @staticmethod
    def _text(value: object, field: str, where: str) -> str:
        """Return a field that must be a string, or refuse it."""
        if not isinstance(value, str):
            raise SourceError(
                f"{where} has {field} {value!r}, which is "
                f"{type(value).__name__} rather than a string"
            )
        return value

    @staticmethod
    def _agrees(
        row: Mapping[str, Any], field: str, expected: object, where: str
    ) -> None:
        """Refuse a stated ``unit`` or ``frequency`` that contradicts the registry.

        Absent is fine and takes the registry's value. Present and different is
        refused rather than coerced: coercing would keep the operator's number
        and relabel it correctly, which is the worst of the three outcomes
        available, because a PMI typed as a percentage then scores and scores
        wrongly.

        Args:
            row: The parsed row.
            field: ``"unit"`` or ``"frequency"``.
            expected: What the registry says for this indicator and currency.
            where: File and row, for the message.

        Raises:
            SourceError: When the row states a different value.

        """
        stated = row.get(field)
        if stated is None:
            return
        if str(stated) != str(expected):
            raise SourceError(
                f"{where} states {field} {str(stated)!r}, and the registry says "
                f"{str(expected)!r}. Refused rather than coerced: the number "
                "was typed in one of those and only one of them is right."
            )

    @staticmethod
    def _value(value: object, where: str) -> float:
        """Return the published number, or refuse it.

        Args:
            value: The parsed ``value`` field.
            where: File and row, for the message.

        Returns:
            The number, in the unit the registry gives for its indicator and
            currency.

        Raises:
            SourceError: When it is not a number, is a bool, or is not finite.
                ``float()`` accepts ``"nan"`` and ``"inf"`` without complaint,
                and a nan reaching a cross-sectional pillar makes the mean and
                the standard deviation nan for all eight currencies at once, so
                every threshold comparison reads False and nothing shows as a
                gap.

        """
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise SourceError(
                f"{where} has value {value!r}, which is "
                f"{type(value).__name__} rather than a number"
            )
        number = float(value)
        if not math.isfinite(number):
            raise SourceError(f"{where} has value {value!r}, which is not finite")
        return number

    @staticmethod
    def _period(value: object, where: str) -> date:
        """Return the period the figure describes, or refuse it.

        YAML reads an unquoted ``2026-08-01`` as a `date` already, so the
        string branch covers a quoted one.

        Raises:
            SourceError: When it is not a date and cannot be read as one.

        """
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as error:
                raise SourceError(
                    f"{where} has period {value!r}, which is not an ISO date"
                ) from error
        raise SourceError(
            f"{where} has period {value!r}, which is {type(value).__name__} "
            "rather than a date"
        )

    @staticmethod
    def _released_at(value: object, where: str) -> datetime | None:
        """Return when the number hit the tape, or ``None``.

        Args:
            value: The parsed ``released_at`` field, absent as ``None``.
            where: File and row, for the message.

        Returns:
            The release timestamp, or ``None`` when the file omits it. Never
            derived from ``period``: the span a figure describes and the day it
            was published are different facts, and conflating them is the
            look-ahead bias Phase 6 has to avoid.

            A value written as a bare date becomes midnight, and a timestamp
            written without a zone is read as UTC. Both are conventions rather
            than facts, and they are stated here because the alternative is an
            operator's ``2026-09-01`` being refused for want of a time nobody
            publishes.

        Raises:
            SourceError: When it is present and cannot be read as a timestamp.

        """
        if value is None:
            return None
        if isinstance(value, datetime):
            stamped = value
        elif isinstance(value, date):
            stamped = datetime(value.year, value.month, value.day)
        elif isinstance(value, str):
            try:
                stamped = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise SourceError(
                    f"{where} has released_at {value!r}, which is not an ISO "
                    "date or timestamp"
                ) from error
        else:
            raise SourceError(
                f"{where} has released_at {value!r}, which is "
                f"{type(value).__name__} rather than a timestamp"
            )
        return stamped if stamped.tzinfo is not None else stamped.replace(tzinfo=UTC)

    @staticmethod
    def _revision(value: object, where: str) -> int:
        """Return the vintage marker, defaulting to 0.

        Raises:
            SourceError: When present and not an integer. A bool is refused
                because it subclasses ``int``.

        """
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, int):
            raise SourceError(
                f"{where} has revision {value!r}, which is "
                f"{type(value).__name__} rather than an integer"
            )
        return value

    @staticmethod
    def _meta(value: object, where: str) -> Mapping[str, Any]:
        """Return the row's free-form provenance block, defaulting to empty.

        Raises:
            SourceError: When present and not a mapping. It is free-form in its
                keys, not in its shape, and a string here would mean the
                operator wrote a note where the schema wants a block.

        """
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise SourceError(
                f"{where} has meta of {type(value).__name__} rather than a mapping"
            )
        return value
