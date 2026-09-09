"""Operator-entered data: the escape hatch for what free APIs will not supply.

Roughly a fifth of the registry has no free machine-readable source. PMIs are
licensed. Two-year yields outside the United States are on no free API. FRED's
OECD inflation complex stopped updating in early 2025, taking headline and core
CPI for six of the eight currencies with it. Central bank guidance tone has
never been a number at all.

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
        Manufacturing PMI headline prints. Flash where marked, otherwise final.

    observations:
      - indicator: pmi_manufacturing
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
* ``released_at`` is optional but strongly wanted. Without it the staleness
  penalty falls back to ``period``, which for a quarterly series overstates the
  age of the data by up to three months.
* ``meta`` is free-form and is the right place for the URL the number came
  from. That provenance is the only audit trail a hand-typed number has.

Guidance tone
-------------
``cb_guidance_tone`` is the one indicator here with no external number at all.
It encodes what a central bank signalled at its last meeting, on ``-1..+1``,
where negative is dovish and positive hawkish. It is a judgement, and recording
it as one, with the meeting date and a sentence of reasoning in ``meta``, is
better than pretending it falls out of the data. It is not in the registry
because the registry maps indicators to external series and this has none; the
monetary pillar reads it directly if it wants it.

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

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path

from fbe.config import DataConfig
from fbe.datasources.base import BaseDataSource, RateLimit, RetryPolicy
from fbe.datasources.registry import SeriesRef
from fbe.types import Observation

__all__ = [
    "FILE_GLOB",
    "GUIDANCE_TONE_KEY",
    "REQUIRED_FIELDS",
    "SUGGESTED_FILES",
    "ManualSource",
]


FILE_GLOB = "*.yaml"
"""Read in sorted filename order; later files override earlier ones."""

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
        "Two-year government bond yields for the seven non-US currencies. No "
        "free FRED series exists for any of them, verified by search. These "
        "carry the front end of the monetary pillar, which is where G10 FX is "
        "actually driven, so they matter more than their count suggests."
    ),
    "inflation.yaml": (
        "Headline and core CPI year-on-year for GBP, JPY, CHF, CAD, AUD and "
        "NZD. FRED's OECD CPI complex stopped updating in 2025-03/04, so the "
        "inflation pillar has no free current input outside USD and EUR."
    ),
    "guidance.yaml": (
        "Central bank guidance tone per currency, on -1..+1. A judgement, "
        "recorded with the meeting date and the reasoning."
    ),
    "overrides.yaml": (
        "Ad-hoc corrections. Read last, so it wins over everything above. Use "
        "it to patch one bad value without editing a whole topic file."
    ),
}
"""Suggested file split. Nothing enforces these names; the loader globs the
directory. They are a starting layout that keeps each file small enough to
review at a glance."""

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

    def __init__(self, config: DataConfig) -> None:
        """Store config and resolve the manual directory.

        Args:
            config: Effective `DataConfig`. ``manual_dir`` names the directory,
                defaulting to ``data/manual`` under the repository root.

        """
        super().__init__(config)

    def available(self) -> bool:
        """Report whether the manual directory exists and holds at least one file.

        An empty directory is available but contributes nothing, which is the
        correct state for a fresh checkout: the engine runs, and the report
        shows the gaps rather than refusing to start.

        Returns:
            Whether this source can be used on this run.

        """
        raise NotImplementedError

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

        """
        raise NotImplementedError

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is ``"manual"``.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`.

        """
        raise NotImplementedError

    def load_file(self, path: Path) -> Sequence[Observation]:
        """Parse and validate one YAML file.

        Args:
            path: Path to the file.

        Returns:
            Observations from that file's ``observations`` list.

        Raises:
            SourceError: On malformed YAML, a missing required field, an
                indicator absent from the registry, a currency outside the
                universe, or a unit that contradicts the registry.

        """
        raise NotImplementedError

    def missing(self, asof: date) -> Mapping[str, tuple[str, ...]]:
        """Report which manual refs have no usable value as of a date.

        The operator's to-do list. Run it before a session and it names exactly
        which numbers need typing in, rather than leaving the gaps to show up
        as a low coverage figure after the fact.

        Args:
            asof: The date to evaluate against, applying the same staleness
                rule the scoring layer uses.

        Returns:
            Indicator key to the currencies still missing a fresh value.

        """
        raise NotImplementedError

    def template(self, indicator: str, currency: str) -> str:
        """Return a ready-to-paste YAML stub for one indicator and currency.

        Pre-fills ``unit`` and ``frequency`` from the registry so the operator
        cannot get those wrong, and leaves ``value``, ``period`` and
        ``released_at`` blank.

        Args:
            indicator: Canonical indicator key.
            currency: ISO 4217 code, or ``"GLOBAL"``.

        Returns:
            A YAML fragment.

        Raises:
            KeyError: If the indicator is not in the registry.

        """
        raise NotImplementedError
