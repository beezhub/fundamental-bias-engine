"""Eurostat's dissemination API: the euro-area employment level (#354).

Base: ``https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/``.
No key, no registration. A request is ``{dataset}?{dimension}={code}&...``, and
the answer is JSON-stat 2.0: a ``value`` object keyed by the position of each
cell in the flattened dimension grid, with any cell that holds no number simply
absent.

Why this source exists. EMPLOYMENT needs a change in employment and the level
it is a change of, and for the euro area FRED carries neither: its euro-area
and German employment levels stopped in 2022. Eurostat's quarterly national
accounts table ``namq_10_pe`` carries total employment for the bloc, current to
the latest quarter.

Why ``EA20`` and not ``EA``. ``EA`` is the euro area at its composition on each
date, so Bulgaria joining on 1 January 2026 adds about 3.6 million people
between 2025-Q4 and 2026-Q1, read live on 2026-10-07. Differenced, that step is
a hiring boom that never happened. ``EA20`` is the twenty members of 2023-2025
throughout, so its change is hiring. The registry's ref names ``EA20``, and the
two EMPLOYMENT keys read the same series, so the change and the level cannot
describe different populations.

What fails loudly. A request whose filters match nothing answers HTTP 200 with
an empty ``value`` and a zero in ``size``, not an error status. Read as no
rows, that is a live series reported as dead with no cause, so `fetch` raises
on it the way `fbe.datasources.oecd` does, naming the series. A body whose
non-time dimensions hold more than one code is refused too: the filters were
meant to pick one series, and reading a grid of several as one would mix them.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from urllib.parse import parse_qsl

from fbe.config import DataConfig
from fbe.datasources.base import (
    BaseDataSource,
    ProbeRequest,
    RateLimit,
    RetryPolicy,
    SourceError,
    WindowTooNarrow,
)
from fbe.datasources.registry import (
    CYCLE_DAYS,
    INDICATORS,
    SeriesRef,
    empty_is_judgeable,
    publication_lag,
)
from fbe.types import Frequency, Observation

__all__ = [
    "BASE_URL",
    "EMPLOYMENT_DATASET",
    "RATE_LIMIT",
    "EurostatSource",
]

BASE_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
"""Verified live on 2026-10-07. Datasets hang off it by their lower-case code."""

EMPLOYMENT_DATASET = "namq_10_pe"
"""Population and employment, quarterly national accounts. The probe asks for
it because it is the one dataset the registry routes here."""

TIME_DIMENSION = "time"
"""The JSON-stat dimension that carries the period. Every other dimension must
hold exactly one code."""

RATE_LIMIT = RateLimit(requests=10, per_seconds=60.0, min_interval_seconds=2.0)
"""Eurostat publishes no limit for the dissemination API. One series is routed
here and a run asks for it twice, so this is a courtesy, not a constraint."""


def _period_code(day: date, frequency: Frequency) -> str:
    """Write a date as the period code Eurostat's time filters take.

    Args:
        day: Any day in the period.
        frequency: The ref's cadence.

    Returns:
        ``"2026-Q2"`` for quarterly, ``"2026-04"`` for monthly.

    Raises:
        SourceError: For any other cadence. Nothing routed here has one, and a
            guessed code would filter on the wrong span.

    """
    if frequency is Frequency.QUARTERLY:
        return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
    if frequency is Frequency.MONTHLY:
        return f"{day.year}-{day.month:02d}"
    raise SourceError(f"eurostat has no period code for {frequency.value} series")


def _period_start(code: str) -> date:
    """Read ``"2026-Q2"`` or ``"2026-04"`` as the first day of that period.

    The first day, as the rest of the registry stamps periods, so a quarter
    from here sorts with the same quarter from FRED or the OECD.

    Raises:
        SourceError: On a code of neither shape.

    """
    year, _, rest = code.partition("-")
    try:
        if rest.startswith("Q") and rest[1:] in {"1", "2", "3", "4"}:
            return date(int(year), 3 * int(rest[1:]) - 2, 1)
        return date(int(year), int(rest), 1)
    except ValueError as error:
        raise SourceError(f"eurostat returned an unreadable period {code!r}") from error


class EurostatSource(BaseDataSource):
    """Eurostat's dissemination API, the euro-area employment level.

    Series IDs in the registry are written ``{dataset}?{filters}``, the
    request's own path and query, for example
    ``namq_10_pe?geo=EA20&unit=THS_PER&na_item=EMP_DC&s_adj=SCA``, so the ID
    is exactly what an operator can paste after `BASE_URL`.

    Attributes:
        name: ``"eurostat"``, matching ``SeriesRef.source``.

    """

    name = "eurostat"
    base_url = BASE_URL
    rate_limit = RATE_LIMIT
    retry = RetryPolicy(attempts=3, backoff_seconds=5.0)

    failure_scope = "series"
    """Each ref is its own request and none is derived from another's answer,
    so one failing costs that series alone (ADR 0015). The change and the
    level read the same published series, but each is computed from the
    answer to its own request."""

    def __init__(self, config: DataConfig) -> None:
        """Store the run's data configuration.

        Args:
            config: Effective `DataConfig`. No credential is needed.

        """
        super().__init__(config)

    def available(self) -> bool:
        """Report availability, which is always true: the API needs no key.

        Configuration rather than connectivity, as `BaseDataSource.available`
        requires: an unreachable API is reported by `fetch` raising.
        """
        return True

    def probe_request(self) -> ProbeRequest:
        """Ask for the newest quarter of the euro-area employment series."""
        dataset, filters = self.split_series_id(
            INDICATORS["employment_level"].series["EUR"].series_id
        )
        return ProbeRequest(
            path=dataset,
            params={**filters, "lastTimePeriod": "1"},
            verify=self._decode,
        )

    def refs(self) -> Mapping[tuple[str, str], SeriesRef]:
        """Return every registry entry whose source is ``"eurostat"``.

        Returns:
            Mapping from ``(indicator, currency)`` to its `SeriesRef`.

        """
        return {
            (spec.key, currency): ref
            for spec in INDICATORS.values()
            for currency, ref in spec.series.items()
            if ref.source == self.name
        }

    def serves(self, ref: SeriesRef) -> bool:
        """Accept the two transforms `fetch` applies, ``level`` and ``diff``."""
        return ref.transform in {"level", "diff"}

    def split_series_id(self, series_id: str) -> tuple[str, dict[str, str]]:
        """Split a registry series ID into its dataset and its filters.

        Args:
            series_id: e.g.
                ``"namq_10_pe?geo=EA20&unit=THS_PER&na_item=EMP_DC&s_adj=SCA"``.

        Returns:
            ``(dataset, filters)``.

        Raises:
            SourceError: When there is no dataset or no filter. A dataset with
                no filter asks for every country and unit at once, which this
                source refuses to read as one series.

        """
        dataset, _, query = series_id.partition("?")
        filters = dict(parse_qsl(query, strict_parsing=bool(query)))
        if not dataset or not filters:
            raise SourceError(
                f"{series_id!r} is not a {{dataset}}?{{filters}} identifier"
            )
        return dataset, filters

    def _decode(self, body: bytes) -> object:
        """Parse a JSON-stat body into ``(period_code, value)`` pairs.

        Returns:
            A tuple of pairs in period order. Empty when the filters matched
            nothing, which `fetch` turns into an error naming the series.

        Raises:
            SourceError: When the body is not JSON-stat, when a dimension other
                than time holds more than one code, or when a value is not a
                finite number.

        """
        payload = super()._decode(body)
        if not isinstance(payload, dict) or payload.get("class") != "dataset":
            raise SourceError(f"{self.name} returned a body that is not a dataset")
        ids = payload.get("id")
        sizes = payload.get("size")
        if (
            not isinstance(ids, list)
            or not isinstance(sizes, list)
            or TIME_DIMENSION not in ids
            or len(ids) != len(sizes)
        ):
            raise SourceError(f"{self.name} returned a dataset with no time dimension")
        wide = [
            str(name)
            for name, size in zip(ids, sizes, strict=True)
            if name != TIME_DIMENSION and size > 1
        ]
        if wide:
            raise SourceError(
                f"{self.name} returned more than one code for {', '.join(wide)}; "
                "the filters must pick one series"
            )
        if 0 in sizes:
            return ()
        try:
            index = payload["dimension"][TIME_DIMENSION]["category"]["index"]
        except (KeyError, TypeError) as error:
            raise SourceError(f"{self.name} returned no time index") from error
        values = payload.get("value")
        if not isinstance(index, dict) or not isinstance(values, dict):
            raise SourceError(f"{self.name} returned an unreadable time index")
        parsed: list[tuple[str, float]] = []
        for code, position in sorted(index.items(), key=lambda item: item[1]):
            raw = values.get(str(position))
            if raw is None:
                # An absent cell is a period with no figure, never a zero.
                continue
            if isinstance(raw, bool) or not isinstance(raw, int | float):
                raise SourceError(f"{self.name} returned {raw!r} for {code}")
            value = float(raw)
            if not math.isfinite(value):
                raise SourceError(f"{self.name} returned a non-finite value for {code}")
            parsed.append((str(code), value))
        return tuple(parsed)

    def fetch(
        self,
        indicators: Iterable[str],
        currencies: Iterable[str],
        start: date,
        end: date,
    ) -> Sequence[Observation]:
        """Fetch every Eurostat-backed series in the request.

        ``diff`` is the period-on-period change of the published level, so a
        ``diff`` request also asks for the period before ``start``: without it
        the first period in the window would have nothing to be a change from.

        Args:
            indicators: Canonical indicator keys. Pairs whose `SeriesRef` names
                another source are skipped.
            currencies: ISO 4217 codes.
            start: Earliest period wanted, inclusive.
            end: Latest period wanted, inclusive.

        Returns:
            Observations in the ref's unit: the level, or its change from the
            previous period. A period whose previous period is absent gets no
            change, rather than a change from further back.

        Raises:
            SourceError: On repeated request failure, an unreadable body, or a
                series that answers with nothing over a window wide enough to
                judge it, which means the series is dead or mis-keyed.
            WindowTooNarrow: When that same series answers with nothing over a
                window too narrow to judge, as `registry.empty_is_judgeable`
                decides.

        """
        wanted_indicators = set(indicators)
        wanted_currencies = set(currencies)
        emitted: list[Observation] = []
        for (indicator, currency), ref in self.refs().items():
            if indicator not in wanted_indicators or currency not in wanted_currencies:
                continue
            dataset, filters = self.split_series_id(ref.series_id)
            since = start
            if ref.transform == "diff":
                since = start - timedelta(days=CYCLE_DAYS[ref.frequency])
            payload = self._request(
                dataset,
                {
                    **filters,
                    "sinceTimePeriod": _period_code(since, ref.frequency),
                    "untilTimePeriod": _period_code(end, ref.frequency),
                },
            )
            if not isinstance(payload, tuple):
                raise SourceError(f"{self.name} decoded {ref.series_id} unusably")
            served = 0
            for period, value in self._transformed(ref, payload):
                if start <= period <= end:
                    emitted.append(
                        self._observation(indicator, currency, ref, period, value)
                    )
                    served += 1
            if served == 0:
                if not empty_is_judgeable(ref, ref.frequency, start, end):
                    raise WindowTooNarrow(
                        f"{self.name} served no observation for {indicator} "
                        f"{currency} ({ref.series_id}) between {start} and {end}; "
                        f"that window less this leg's "
                        f"{publication_lag(ref, ref.frequency)}-day lag does not "
                        f"span one {ref.frequency.value} cycle"
                    )
                raise SourceError(
                    f"{self.name} answered for {ref.series_id} but served no "
                    f"observation for {indicator} {currency} between {start} and "
                    f"{end}; a series with nothing in a window that long is dead "
                    "or mis-keyed, not empty"
                )
        return emitted

    def _transformed(
        self, ref: SeriesRef, pairs: Sequence[tuple[str, float]]
    ) -> list[tuple[date, float]]:
        """Apply ``ref.transform`` to the published level.

        Returns:
            ``(period_start, value)`` pairs. For ``diff``, a change is emitted
            only between consecutive periods, so a hole in the series leaves a
            hole in the change rather than a change over two periods.

        Raises:
            SourceError: On a transform this source does not apply.

        """
        levels = [(_period_start(code), value) for code, value in pairs]
        if ref.transform == "level":
            return levels
        if ref.transform != "diff":
            raise SourceError(f"{self.name} cannot apply transform {ref.transform!r}")
        step = CYCLE_DAYS[ref.frequency]
        return [
            (later, value - earlier_value)
            for (earlier, earlier_value), (later, value) in zip(
                levels, levels[1:], strict=False
            )
            if (later - earlier).days <= step
        ]
