"""common/brinson_attribution.py

ADVISORY single-period Brinson-Fachler (a.k.a. BHB) sector attribution.
Task `AT1` of `docs/development_plan.md` (§2.24).

What this is
------------
A **pure function** that decomposes one period's active return into the classic
three-term Brinson-Fachler decomposition, per sector `s`:

    allocation_effect  = sum_s (w_p,s - w_b,s) * r_b,s
    selection_effect   = sum_s  w_b,s        * (r_p,s - r_b,s)
    interaction_effect = sum_s (w_p,s - w_b,s) * (r_p,s - r_b,s)

with sector weights/returns aggregated from the asset level:

    w_p,s = sum_{i in s} w_p,i      r_p,s = sum_{i in s} w_p,i * r_i / w_p,s
    w_b,s = sum_{i in s} w_b,i      r_b,s = sum_{i in s} w_b,i * r_i / w_b,s

The three effects sum to the active return `sum_i w_p,i r_i - sum_i w_b,i r_i`
to within float summation order; `BrinsonAttribution.residual` records the
actual gap so a caller can assert `abs(residual) <= 1e-12` in its own test
(the library itself deliberately does **not** assert -- see "No asserts" below).

Grounding: Brinson & Fachler (1986), "Measuring Non-US Equity Portfolio
Performance", Journal of Portfolio Management; Brinson, Hood & Beebower (1986),
"Determinants of Portfolio Performance", Financial Analysts Journal.

Scope and boundaries (disclosed simplifications)
------------------------------------------------
* **Advisory only.** This module is never imported by -- and must not be wired
  into -- any gate, preflight, order-ticket or execution path. It imports
  nothing from `scripts/`, `web/livetrading/`, or `pipeline/research/blend/`.
  Its purpose is strictly to *report* why a period differed from benchmark.
* **Single period.** No multi-period linking (no Carino/Menchero smoothing).
  Linking needs per-period returns this function does not receive, and a naive
  product link is not additive; callers that need multi-period numbers must
  call this once per period and link in their own reporting layer.
* **No trading costs, taxes, slippage, or cash timing.** `r` is the gross
  asset return handed in by the caller; `w` are the weights as handed in (they
  are NOT renormalised, so a partially invested portfolio keeps the residual
  cash as a zero-return, zero-weight-of-asset drag -- exactly what makes the
  allocation term meaningful).
* **No fancy attribution**: no currency, no Brinson-Hood-Beebower "active
  return vs. pure active return" variants, no risk attribution.
* **No asserts in library code.** Degenerate inputs (empty buckets, all-cash,
  NaN returns) are handled deterministically and disclosed in the result; the
  identity is verified by the test suite, not by a runtime assertion here.
* **Sector labels are NOT re-invented.** `common/sector_metadata.py` is the
  single source of truth: `load_sector_map` loads a JSON map and
  `resolve_sector` resolves each symbol. This module builds no second mapping
  table; the only extra logic is a *miss* test on the same dict so an unmapped
  symbol can be reported as `UNCLASSIFIED` instead of silently invented.

Missing-sector policy (development_plan §2.24 `AT1` item 3)
-----------------------------------------------------------
An asset whose sector cannot be established is put in the explicit
`UNCLASSIFIED` bucket, keeps its full weight and return contribution, and is
listed by name in `BrinsonAttribution.unclassified_symbols`. It is **never**
dropped (dropping would silently break the identity and flatter the report)
and **never** imputed with a sector mean.

Two documented modes decide what "cannot be established" means:

1. `sector_map` supplied (non-empty mapping, or a path to a JSON map): the map
   is the AUTHORITY. A symbol absent from it (or mapped to a blank value) goes
   to `UNCLASSIFIED` even though `resolve_sector`'s heuristic fallback would
   have produced a label. Rationale: when an operator hands over a map, an
   absent symbol means "unknown sector", and an invented `sec_60` pseudo-label
   would hide a data gap inside a fake bucket.
2. `sector_map` is None/empty: there is no map at all, so `resolve_sector`'s
   requested `fallback` ('auto' / 'prefix' / 'other') is honoured as-is. That
   is the explicit "no metadata, classify heuristically" mode; an empty label
   from that path still lands in `UNCLASSIFIED`.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

import pandas as pd

from common.sector_metadata import (
    VALID_FALLBACKS,
    load_sector_map,
    resolve_sector,
)

__version__ = "1.0.0"

__all__ = [
    "UNCLASSIFIED",
    "SectorContribution",
    "BrinsonAttribution",
    "brinson_attribution",
]

#: Explicit bucket for assets whose sector cannot be established. The literal
#: string is public API: downstream reports/tests key off it.
UNCLASSIFIED = "UNCLASSIFIED"

# Python-side sentinels for a *blank* resolution. `resolve_sector` never returns
# these for its own reasons (it always returns a non-empty label), but a caller
# may hand over a map whose values are literally "nan"/"none", and pandas NaN
# can leak through string coercion; both mean "no sector information".
_NULL_LABELS = frozenset({"", "nan", "none", "null"})

_SECTOR_REPORT_COLUMNS = (
    "sector",
    "portfolio_weight",
    "benchmark_weight",
    "portfolio_return",
    "benchmark_return",
    "allocation_effect",
    "selection_effect",
    "interaction_effect",
    "total_effect",
)


@dataclass(frozen=True)
class SectorContribution:
    """One sector's row of the attribution report.

    Attributes are already aggregated to the sector level, so the three effects
    on a row sum to `portfolio_weight * portfolio_return - benchmark_weight *
    benchmark_return` for that sector -- which is what makes the per-sector rows
    add back up to the three aggregate effects (see the test suite).

    Return convention for one-sided buckets: if a sector holds no portfolio
    weight (`portfolio_weight == 0.0`) its `portfolio_return` is reported as
    `0.0` rather than NaN/undefined, and symmetrically for an empty benchmark
    bucket. That is a *reporting* convention only -- it makes the interaction
    term carry the whole one-sided contribution, which is exactly what the
    algebra demands (`0 * x` contributes nothing to allocation/selection).
    """

    sector: str
    portfolio_weight: float
    benchmark_weight: float
    portfolio_return: float
    benchmark_return: float
    allocation_effect: float
    selection_effect: float
    interaction_effect: float

    @property
    def total_effect(self) -> float:
        """Sum of the three effects for this sector (its active contribution)."""
        return self.allocation_effect + self.selection_effect + self.interaction_effect

    def as_dict(self) -> Dict[str, float]:
        """Plain-dict view, keyed by the report column names."""
        return {
            "sector": self.sector,
            "portfolio_weight": self.portfolio_weight,
            "benchmark_weight": self.benchmark_weight,
            "portfolio_return": self.portfolio_return,
            "benchmark_return": self.benchmark_return,
            "allocation_effect": self.allocation_effect,
            "selection_effect": self.selection_effect,
            "interaction_effect": self.interaction_effect,
            "total_effect": self.total_effect,
        }


@dataclass(frozen=True)
class BrinsonAttribution:
    """Immutable result of one single-period attribution run.

    `sectors` is sorted by sector label so repeated calls on identical inputs
    produce byte-identical reports (determinism is part of the contract).

    `residual` is `active_return - (allocation + selection + interaction)`. It
    is exposed rather than raised on: for any non-degenerate input it is pure
    float summation noise (order 1e-16), and the caller's test asserts the
    1e-12 bound. A *large* residual means upstream weights/returns were mutated
    between the two summation passes, which cannot happen inside a pure call.

    `nan_return_symbols` / `nan_weight_symbols` are disclosure fields: those
    assets were treated as 0.0 (return or weight respectively) so the identity
    still holds over the full universe and nothing is silently dropped. They
    exist so a data gap is visible in the report instead of hiding inside a
    number that looks fine.
    """

    allocation_effect: float
    selection_effect: float
    interaction_effect: float
    portfolio_return: float
    benchmark_return: float
    active_return: float
    residual: float
    sectors: Tuple[SectorContribution, ...]
    unclassified_symbols: Tuple[str, ...] = ()
    nan_return_symbols: Tuple[str, ...] = ()
    nan_weight_symbols: Tuple[str, ...] = ()

    @property
    def total_effect(self) -> float:
        """Allocation + selection + interaction (the reported active return)."""
        return self.allocation_effect + self.selection_effect + self.interaction_effect

    def sector(self, name: str) -> SectorContribution:
        """Looks up one sector row by label; raises KeyError when absent."""
        for row in self.sectors:
            if row.sector == name:
                return row
        raise KeyError(f"sector {name!r} not present in attribution report")

    def as_dict(self) -> Dict[str, object]:
        """Flat dict of the aggregate terms plus the per-sector rows."""
        return {
            "allocation_effect": self.allocation_effect,
            "selection_effect": self.selection_effect,
            "interaction_effect": self.interaction_effect,
            "total_effect": self.total_effect,
            "portfolio_return": self.portfolio_return,
            "benchmark_return": self.benchmark_return,
            "active_return": self.active_return,
            "residual": self.residual,
            "unclassified_symbols": self.unclassified_symbols,
            "nan_return_symbols": self.nan_return_symbols,
            "nan_weight_symbols": self.nan_weight_symbols,
            "sectors": [row.as_dict() for row in self.sectors],
        }

    def to_dataframe(self) -> pd.DataFrame:
        """Per-sector report as a DataFrame (one row per sector, sorted)."""
        rows = [row.as_dict() for row in self.sectors]
        return pd.DataFrame(rows, columns=list(_SECTOR_REPORT_COLUMNS)).set_index("sector")

    def summary(self) -> str:
        """Human-readable multi-line report (advisory text, no side effects)."""
        lines = [
            "Brinson-Fachler single-period attribution (advisory)",
            "  portfolio return      : {:+.12f}".format(self.portfolio_return),
            "  benchmark return      : {:+.12f}".format(self.benchmark_return),
            "  active return         : {:+.12f}".format(self.active_return),
            "  allocation effect     : {:+.12f}".format(self.allocation_effect),
            "  selection effect      : {:+.12f}".format(self.selection_effect),
            "  interaction effect    : {:+.12f}".format(self.interaction_effect),
            "  residual (identity)   : {:+.3e}".format(self.residual),
        ]
        if self.unclassified_symbols:
            lines.append(
                "  UNCLASSIFIED symbols  : {}".format(", ".join(self.unclassified_symbols))
            )
        return "\n".join(lines)


def _as_series(values, arg_name: str) -> pd.Series:
    """Coerces a weight/return argument into a float Series (NaN = 'not given').

    Non-numeric entries become NaN rather than raising, because a report tool
    must survive a dirty input file and *disclose* the gap (`nan_*_symbols`)
    instead of dying -- and instead of silently treating a garbage string as a
    return of zero without saying so.
    """
    if isinstance(values, pd.Series):
        series = values
    elif isinstance(values, Mapping):
        series = pd.Series(dict(values))
    else:
        raise TypeError(
            f"{arg_name} must be a pandas Series or a mapping of symbol -> number, "
            f"got {type(values).__name__}"
        )
    if not series.index.is_unique:
        raise ValueError(f"{arg_name} index must be unique (duplicate symbols are ambiguous)")
    return pd.to_numeric(series, errors="coerce").astype(float)


def _normalise_sector_map(sector_map) -> Optional[Mapping[str, str]]:
    """Accepts a mapping, or a JSON path (delegated to `load_sector_map`), or None.

    Accepting a path is the *only* reason this module imports `load_sector_map`:
    the JSON parsing/upper-casing/`_meta`-skipping rules stay owned by
    `common/sector_metadata.py` and are never duplicated here.
    """
    if sector_map is None:
        return None
    if isinstance(sector_map, (str, os.PathLike)):
        return load_sector_map(os.fspath(sector_map))
    if isinstance(sector_map, Mapping):
        return dict(sector_map)
    raise TypeError(
        "sector_map must be a mapping, a path to a JSON sector map, or None; "
        f"got {type(sector_map).__name__}"
    )


def _mapped_label(symbol, sector_map: Optional[Mapping[str, str]]) -> Optional[str]:
    """Returns the map's non-blank label for `symbol`, else None.

    This mirrors EXACTLY the two probes `common/sector_metadata.resolve_sector`
    performs (`sector_metadata.py:139-149`): upper-cased key first, then the raw
    key, with blank/NaN/'nan' values treated as absent. It reads the SAME dict
    the caller supplied -- it is a miss test, not a second sector mapping, and
    it never assigns a label of its own.
    """
    if not sector_map:
        return None
    sym = str(symbol).strip()
    for key in (sym.upper(), sym):
        if key in sector_map:
            value = sector_map[key]
            if value is None:
                continue
            text = str(value).strip()
            if text and text.lower() not in _NULL_LABELS:
                return text
            return None  # mapped, but to a blank value -> no information
    return None


def _resolve_labels(
    symbols: Sequence,
    sector_map: Optional[Mapping[str, str]],
    fallback: str,
    unclassified_label: str,
) -> Tuple[Dict[object, str], Tuple[object, ...]]:
    """Resolves one sector label per symbol, mirroring the miss policy in the header.

    `resolve_sector` is called for EVERY symbol (it is the single source of truth
    for the label) and its answer is used verbatim whenever it is a real label.
    The only extra decision is the "map supplied but symbol absent" override
    described in the module docstring.
    """
    labels: Dict[object, str] = {}
    unclassified: List[object] = []
    for symbol in symbols:
        resolved = resolve_sector(symbol, sector_map, fallback=fallback)
        text = "" if resolved is None else str(resolved).strip()
        missing = text == "" or text.lower() in _NULL_LABELS
        if not missing and sector_map and _mapped_label(symbol, sector_map) is None:
            # A map was supplied and does not cover this symbol: the map is the
            # authority, so the heuristic fallback label is discarded and the
            # asset is surfaced as UNCLASSIFIED instead of hidden in a fake bucket.
            missing = True
        if missing:
            labels[symbol] = unclassified_label
            unclassified.append(symbol)
        else:
            labels[symbol] = text
    return labels, tuple(unclassified)


def brinson_attribution(
    portfolio_weights,
    benchmark_weights,
    asset_returns,
    sector_map: Union[Mapping[str, str], str, os.PathLike, None] = None,
    *,
    fallback: str = "other",
    unclassified_label: str = UNCLASSIFIED,
) -> BrinsonAttribution:
    """Single-period Brinson-Fachler decomposition of active return.

    Pure: no I/O, no global state, no mutation of the inputs, no RNG. Calling it
    twice with equal inputs returns equal results (the test suite asserts this).

    Parameters
    ----------
    portfolio_weights : Mapping[str, float] | pd.Series
        Portfolio weight per symbol at the START of the period. NOT renormalised
        -- the un-invested remainder is simply absent, which is what makes the
        allocation term measure the cash/allocation decision honestly. Negative
        weights (shorts) are allowed and handled by the same algebra.
    benchmark_weights : Mapping[str, float] | pd.Series
        Benchmark weight per symbol, same conventions.
    asset_returns : Mapping[str, float] | pd.Series
        Period return per symbol (fractional, e.g. 0.012 == +1.2%). The index is
        the union key: symbols may appear in only some of the three inputs.
    sector_map : Mapping[str, str] | str | os.PathLike | None, default None
        Symbol -> sector map, or a path to a JSON map (loaded through
        `common.sector_metadata.load_sector_map`). A supplied map is treated as
        authoritative and unmapped symbols go to `unclassified_label`; with no
        map, `resolve_sector`'s `fallback` rule classifies every symbol.
    fallback : str, default 'other'
        Forwarded to `resolve_sector` ('auto', 'prefix', 'other'). Validated
        here so an invalid value fails even on an empty universe.
    unclassified_label : str, default 'UNCLASSIFIED'
        Bucket name for assets with no established sector. Overriding it is
        allowed (e.g. to match a report's localisation) but the default is the
        contract value reports key off.

    Returns
    -------
    BrinsonAttribution
        Aggregate effects, portfolio/benchmark/active returns, residual, and the
        per-sector rows (each row's three effects sum to its own active
        contribution, so rows roll up to the aggregates).

    Deterministic edge-case behaviour (no silent special cases)
    ----------------------------------------------------------
    * **Empty sector on one side** (sector held only by the portfolio, or only
      by the benchmark): the missing side's weight is 0.0 and its return is
      reported as 0.0 (never NaN, never a sector-mean imputation). The algebra
      then puts the whole one-sided contribution into `interaction_effect`,
      which is the standard Brinson convention for an undefined base return.
    * **Empty universe / all-cash on both sides**: every effect is exactly 0.0.
    * **Single asset**: `allocation == interaction == 0` and
      `selection == w_p (r_p - r_b)`, since one asset means one sector.
    * **NaN return for a held asset**: counted as a 0.0 return (the weight still
      participates, so weights are conserved and the identity still holds) and
      the symbol is listed in `nan_return_symbols`. Never dropped.
    * **NaN weight** (in either book): counted as 0.0 and listed in
      `nan_weight_symbols`, for the same reason.
    * **Sector-map miss / blank value**: the asset keeps its full weight and
      return contribution inside the `UNCLASSIFIED` bucket and appears in
      `unclassified_symbols`; weights are conserved exactly.

    Raises
    ------
    ValueError
        If `fallback` is not in `common.sector_metadata.VALID_FALLBACKS`, if
        `unclassified_label` is blank, or if an input Series has a duplicate
        (ambiguous) index.
    TypeError
        If an input is neither a mapping nor a Series, or `sector_map` is neither
        a mapping, a path, nor None.
    """
    if fallback not in VALID_FALLBACKS:
        raise ValueError(f"unknown fallback: {fallback!r}")
    label = "" if unclassified_label is None else str(unclassified_label).strip()
    if not label:
        raise ValueError("unclassified_label must be a non-empty string")

    sector_map_norm = _normalise_sector_map(sector_map)

    pw = _as_series(portfolio_weights, "portfolio_weights")
    bw = _as_series(benchmark_weights, "benchmark_weights")
    ar = _as_series(asset_returns, "asset_returns")

    # Union of all three indexes, sorted by str() for a deterministic report order
    # even when a caller mixes str and non-str symbols.
    symbols = sorted({*pw.index, *bw.index, *ar.index}, key=str)

    p = pw.reindex(symbols)
    b = bw.reindex(symbols)
    r = ar.reindex(symbols)

    nan_weight_symbols = tuple(s for s in symbols if pd.isna(p[s]) or pd.isna(b[s]))
    nan_return_symbols = tuple(s for s in symbols if pd.isna(r[s]))

    p = p.fillna(0.0).astype(float)
    b = b.fillna(0.0).astype(float)
    r = r.fillna(0.0).astype(float)

    labels, unclassified = _resolve_labels(symbols, sector_map_norm, fallback, label)

    members: Dict[str, List] = {}
    for symbol in symbols:
        members.setdefault(labels[symbol], []).append(symbol)

    rows: List[SectorContribution] = []
    for sector in sorted(members):
        group = members[sector]
        w_p = math.fsum(float(p[s]) for s in group)
        w_b = math.fsum(float(b[s]) for s in group)
        contrib_p = math.fsum(float(p[s]) * float(r[s]) for s in group)
        contrib_b = math.fsum(float(b[s]) * float(r[s]) for s in group)
        # Exact-zero guard (not isclose): a bucket with no capital has no defined
        # return, and inventing one from a tiny denominator would inject noise.
        r_p = contrib_p / w_p if w_p != 0.0 else 0.0
        r_b = contrib_b / w_b if w_b != 0.0 else 0.0

        d_w = w_p - w_b
        d_r = r_p - r_b
        rows.append(
            SectorContribution(
                sector=sector,
                portfolio_weight=w_p,
                benchmark_weight=w_b,
                portfolio_return=r_p,
                benchmark_return=r_b,
                allocation_effect=d_w * r_b,
                selection_effect=w_b * d_r,
                interaction_effect=d_w * d_r,
            )
        )

    allocation_effect = math.fsum(row.allocation_effect for row in rows)
    selection_effect = math.fsum(row.selection_effect for row in rows)
    interaction_effect = math.fsum(row.interaction_effect for row in rows)

    # Computed independently from the asset level (NOT by summing the sector
    # rows), so `residual` is a genuine cross-check of the decomposition rather
    # than a tautology that is always zero.
    portfolio_return = math.fsum(float(p[s]) * float(r[s]) for s in symbols)
    benchmark_return = math.fsum(float(b[s]) * float(r[s]) for s in symbols)
    active_return = portfolio_return - benchmark_return
    residual = active_return - (allocation_effect + selection_effect + interaction_effect)

    return BrinsonAttribution(
        allocation_effect=allocation_effect,
        selection_effect=selection_effect,
        interaction_effect=interaction_effect,
        portfolio_return=portfolio_return,
        benchmark_return=benchmark_return,
        active_return=active_return,
        residual=residual,
        sectors=tuple(rows),
        unclassified_symbols=unclassified,
        nan_return_symbols=nan_return_symbols,
        nan_weight_symbols=nan_weight_symbols,
    )

