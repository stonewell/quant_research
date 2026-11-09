"""common/live_capacity.py

ADVISORY live-side turnover-inflation and volume-participation (capacity)
observation. Task `AT4` of `docs/development_plan.md` (line 410).

Why this module exists
----------------------
The backtest engine already measures both quantities:

* `total_turnover` -- `common/allocation_backtester.py:1839`
  (`total_turnover += notional / exec_equity`), same rule for the day-0 book
  (`:2028` -> `:2057-2058`), and again in the second execution engine
  (`rebal_notional += trade_val` at `:2311` SELL and `:2399` BUY, then
  `turnover = rebal_notional / pre_rebal_equity` at `:2441-2442`);
* `max_volume_participation` -- `common/allocation_backtester.py:371` (input)
  and `_participation_cap` (`:1286`), which budgets against **today's** volume
  only and treats `Volume <= 0` as *unobservable* rather than zero.

The **live** side had zero implementation of either. `AT4` adds observation
only: nothing here is imported by the order path, nothing here can block,
resize, delay, or re-price an order. See "Advisory by construction" below.

Thresholds: the `L2.36` pre-registered values, and ONLY those
-------------------------------------------------------------
`AT4` acceptance clause 2: "换手基线只许复用 `L2.36` 预注册值（8.0x/年合格、
10.0x/年硬上限），不得另造第二个阈值". The authoritative wording lives in
`docs/development_plan_archive.md:261` (`L2.36` row, 交付与验收 item 4):

    预注册阈值（单边稳态）为合格 <= 8.0x/年、硬上限 <= 10.0x/年、
    下界冗余 >= 3.0x/年（摩擦 0.967% / 1.208%）

This module therefore carries exactly two turnover constants
(`TURNOVER_QUALIFIED_ANN_ONE_WAY = 8.0`,
`TURNOVER_CEILING_ANN_ONE_WAY = 10.0`), both **one-way** (`单边`) annualised,
and no other turnover-threshold literal exists anywhere in the file -- an AST
test pins that, so a future "just tweak it to 12" cannot land silently.

Two deliberate non-imports, both disclosed:

1. `L2.36` also pre-registers a `3.0x/年` *redundancy floor* (`下界冗余`).
   `AT4` names only the two lines above, and a "turnover too low" label would
   need a policy decision this task does not have, so the floor is **not**
   implemented here (documented, not silently skipped).
2. `L2.51` landed `run_audit.TURNOVER_CEILING_ANN = 10.0` as a **two-way**
   rating gate ("选的 C = 双边 10.0" => 单边 5.0). That is a *different axis*
   (walkforward grade A->B) with a different basis. Importing it here would
   make the same numeral mean two different things -- exactly the
   `L2.49` 判定 9 / §4.16.3 confusion. `AT4` uses `L2.36`'s numbers only.

Turnover convention -- stated once, then enforced
-------------------------------------------------
`annualized_turnover()` takes the SAME notional as the backtester accumulates:
`total_turnover` sums `trade_val` on **both** legs (`:1744` SELL, `:1814` BUY)
into `notional`, then divides by the equity at that rebalance, so it is a
**gross two-way (`双边`)** cumulative measure. `convention="two_way"` (the
default) declares that shape; `convention="one_way"` declares a one-side
series (buys only, or sells only). Because `L2.36`'s 8.0/10.0 lines are
**one-way**, every observation also carries `one_way_annualized`
(`two_way / 2`), and ONLY that field is compared against the constants.

The factor-of-two trap is not hypothetical: `L2.49` item 7 records that
`docs/version.md` §16.15.13's "稳态摩擦 85.26 bp/年" was a *one-way* figure
printed for a *two-way* ledger, understating the real charge by 2x (201.15
bp/年). This module refuses to repeat that: the basis is a required part of
the input contract, and a mismatch is visible in the returned object.

Annualisation form: `sum_t (notional_t / equity_t) * (periods_per_year / N)`,
which is term-for-term the backtester's per-rebalance accumulation, scaled by
the observation window (`L2.36` annualised 63-trading-day folds the same way,
x4.0). The alternative "annualise each sub-window then average" (`L2.51`
item 2) needs folds; a live run has one window and no folds.

Volume participation -- BOTH bases, tri-state when volume is missing
--------------------------------------------------------------------
`AT4` clause 3 asks for both "占当日成交量" and "占近 20 日均量". The
backtester's cap uses today's volume only (`_participation_cap`, `:1286`), so
the 20-day basis is genuinely new information here -- and it is the basis that
answers "could this order have been absorbed by a normal session?".

Clause 3 also forbids `0` / mean filling. Every unobservable input therefore
returns `available=False` with a machine-readable `reason`, and all derived
fields stay `None`. `daily_volume <= 0` is "suspended / unobservable", not
"zero capacity" -- the same distinction `_participation_cap` draws.

Advisory by construction
------------------------
`capacity_labels()` returns a plain dict with `advisory: True` and
`gates_orders: False`. It never raises on data (missing / NaN / `inf` /
absurd magnitudes all become labels, never exceptions) and it has no side
effects. Consuming code (e.g. `AT5`) may render it; the order path must not
branch on it. No participation threshold is defined here on purpose: any
percentage cap would be a *new* threshold, which `AT4` clause 2 forbids --
`AT5` must freeze its own mapping before it may act on these numbers.

Numbers are outputs, not prescriptions: a `BREACH` label is an observation
that the live book is turning over faster than the pre-registered ceiling,
nothing more.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "L2_36_SOURCE",
    "L2_36_ARCHIVE_LINE",
    "TURNOVER_QUALIFIED_ANN_ONE_WAY",
    "TURNOVER_CEILING_ANN_ONE_WAY",
    "DEFAULT_PERIODS_PER_YEAR",
    "DEFAULT_VOLUME_LOOKBACK",
    "TURNOVER_LABEL_OK",
    "TURNOVER_LABEL_WATCH",
    "TURNOVER_LABEL_BREACH",
    "TURNOVER_LABEL_MISSING",
    "PARTICIPATION_LABEL_OBSERVED",
    "PARTICIPATION_LABEL_MISSING",
    "TurnoverObservation",
    "VolumeParticipation",
    "annualized_turnover",
    "volume_participation",
    "turnover_label",
    "participation_label",
    "capacity_labels",
]

# ---------------------------------------------------------------------------
# Pre-registered thresholds. THE ONLY TURNOVER THRESHOLDS IN THIS MODULE.
# Source of truth (quoted verbatim in the module docstring):
#   docs/development_plan_archive.md:261 -- `L2.36`, 交付与验收 item 4:
#   "预注册阈值（单边稳态）为合格 <= 8.0x/年、硬上限 <= 10.0x/年、
#    下界冗余 >= 3.0x/年（摩擦 0.967% / 1.208%）"
# Both are ONE-WAY (`单边`) annualised, so they are compared against
# `TurnoverObservation.one_way_annualized`, never against the two-way total.
# Do NOT add a second threshold here (e.g. 12.0, 15.0, 1.5%): `AT4` clause 2.
# ---------------------------------------------------------------------------
L2_36_ARCHIVE_LINE = 261
L2_36_SOURCE = (
    "docs/development_plan_archive.md:261 (`L2.36` row, 交付与验收 item 4: "
    "预注册阈值（单边稳态）为合格 8.0x/年、硬上限 10.0x/年)"
)

TURNOVER_QUALIFIED_ANN_ONE_WAY = 8.0   # 合格线 (`L2.36`)
TURNOVER_CEILING_ANN_ONE_WAY = 10.0    # 硬上限 (`L2.36`)

DEFAULT_PERIODS_PER_YEAR = 252.0
DEFAULT_VOLUME_LOOKBACK = 20

TURNOVER_LABEL_OK = "OK"
TURNOVER_LABEL_WATCH = "WATCH"
TURNOVER_LABEL_BREACH = "BREACH"
TURNOVER_LABEL_MISSING = "MISSING"

PARTICIPATION_LABEL_OBSERVED = "OBSERVED"
PARTICIPATION_LABEL_MISSING = "MISSING"

_CONVENTION_TWO_WAY = "two_way"
_CONVENTION_ONE_WAY = "one_way"
_VALID_CONVENTIONS = (_CONVENTION_TWO_WAY, _CONVENTION_ONE_WAY)


# ---------------------------------------------------------------------------
# Small scalar / sequence plumbing
# ---------------------------------------------------------------------------
def _finite(value: Any) -> Optional[float]:
    """`float(value)` iff it is a finite real number, else `None`.

    `None` is returned (never `0.0`, never the mean) for: `None`, `bool`
    (`True` is not a price or a share count), NaN / +-inf, non-numeric
    strings, `pd.NA`, and anything else that refuses `float()`. A *numeric*
    string (`"12.5"`) is accepted, because a JSON-decoded ticket can carry one
    and refusing it would turn a real number into an unknown. This is the
    single place where "missing" is decided, so no caller can accidentally
    coerce it to zero.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _as_list(values: Any, *, name: str) -> Optional[list]:
    """Normalises a 1-D series to `list`; `None` for "not supplied".

    Accepts `list` / `tuple` / `numpy.ndarray` / `pandas.Series` (anything
    sized and iterable). A bare scalar is *not* a series -- callers that want
    broadcast semantics pass it explicitly (see `annualized_turnover`'s
    `equity`). A nested / 2-D object raises `TypeError`, because that is an
    API misuse rather than a data gap and silently flattening it would invent
    numbers.
    """
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a 1-D sequence of numbers, got {type(values).__name__}")
    try:
        out = list(values)
    except TypeError as exc:  # pragma: no cover - defensive
        raise TypeError(f"{name} must be a 1-D sequence of numbers: {exc}") from exc
    for item in out:
        if isinstance(item, (list, tuple, dict, set)) or hasattr(item, "__len__"):
            raise TypeError(
                f"{name} must be 1-D; element {item!r} is itself a container "
                "(a 2-D input would silently change the number of periods)"
            )
    return out


@dataclass(frozen=True)
class TurnoverObservation:
    """One advisory turnover observation, in a declared basis.

    `annualized` is the number in the basis the caller DECLARED
    (`convention`), `one_way_annualized` is that number normalised to the
    `L2.36` basis (`单边`). Only `one_way_annualized` is ever compared against
    `TURNOVER_QUALIFIED_ANN_ONE_WAY` / `TURNOVER_CEILING_ANN_ONE_WAY`.

    When `available` is `False`, every numeric field is `None` and `reason`
    explains why -- never `0.0`, because "unknown turnover" must not render as
    "zero turnover" (the `L2.51` item 4 rule: 未知不得被当成很好).
    """

    available: bool
    convention: str
    reason: Optional[str] = None
    annualized: Optional[float] = None
    one_way_annualized: Optional[float] = None
    periods_per_year: Optional[float] = None
    n_periods: int = 0
    window_periods: Optional[float] = None
    traded_notional_total: Optional[float] = None
    notional_over_equity_sum: Optional[float] = None
    thresholds: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON-safe snapshot (floats stay floats; `None` stays `None`)."""
        return {
            "available": self.available,
            "convention": self.convention,
            "reason": self.reason,
            "annualized": self.annualized,
            "one_way_annualized": self.one_way_annualized,
            "periods_per_year": self.periods_per_year,
            "n_periods": self.n_periods,
            "window_periods": self.window_periods,
            "traded_notional_total": self.traded_notional_total,
            "notional_over_equity_sum": self.notional_over_equity_sum,
            "thresholds": dict(self.thresholds),
        }


def _threshold_snapshot() -> dict:
    return {
        "qualified_one_way_ann": TURNOVER_QUALIFIED_ANN_ONE_WAY,
        "ceiling_one_way_ann": TURNOVER_CEILING_ANN_ONE_WAY,
        "convention": "one_way annualised",
        "source": L2_36_SOURCE,
    }


def annualized_turnover(
    traded_notional: Any,
    equity: Any,
    *,
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
    convention: str = _CONVENTION_TWO_WAY,
    window_periods: Optional[float] = None,
) -> TurnoverObservation:
    """Annualised turnover of a live observation window. ADVISORY.

    Formula (see the module docstring for why this shape)::

        annualized = sum_t ( traded_notional_t / equity_t )
                     * ( periods_per_year / window_periods )

    where `t` runs over observation periods, `traded_notional_t` is the traded
    notional **in the basis declared by `convention`**, and `equity_t` is the
    portfolio equity that notional was sized against. The inner sum is
    term-for-term what `common/allocation_backtester.py:1839` accumulates
    (`notional / exec_equity` per rebalance); the multiplier annualises the
    window (`L2.36` used x4.0 for a 63-trading-day fold).

    Parameters
    ----------
    traded_notional:
        1-D sequence of per-period traded notional MAGNITUDES (non-negative).
        `convention="two_way"` (default) means each element is
        `|buy| + |sell|` for that period -- i.e. the same `notional` the
        backtester's `total_turnover` sums (both legs, `:1744` SELL and
        `:1814` BUY). `convention="one_way"` means each element is a single
        side (buys only, or sells only).
    equity:
        Per-period equity (same length as `traded_notional`), or a scalar
        (broadcast to every period -- useful for a mean-capital base).
    periods_per_year:
        Annualisation base. Default `252.0` trading days.
    convention:
        `"two_way"` or `"one_way"`; anything else raises `ValueError`
        (a silent default would make the returned basis a guess).
    window_periods:
        Length of the window in periods, when it differs from
        `len(traded_notional)`. Use this when the caller supplies only the
        rebalance rows: e.g. 2 rebalance rows spanning 63 trading days ->
        `window_periods=63`. Defaults to `len(traded_notional)`.

    Returns
    -------
    `TurnoverObservation`. Unobservable input yields `available=False` with a
    `reason` naming the offending period -- never a zero-filled result.
    Negative notional is treated as a data defect (tri-state), not as a
    signed-flow feature: mixing directions into a gross measure would make
    the number un-interpretable.

    Raises
    ------
    ValueError
        Only for structural API misuse that cannot be represented as data:
        unknown `convention`, non-positive / non-finite `periods_per_year`,
        empty `traded_notional`, a mismatched `equity` length, or a
        non-positive `window_periods`.
    TypeError
        Only when the sequences are not 1-D.
    """
    if convention not in _VALID_CONVENTIONS:
        raise ValueError(
            f"convention must be one of {_VALID_CONVENTIONS}, got {convention!r} "
            "(the basis is never guessed: two-way vs one-way differ by 2x)"
        )
    ppy = _finite(periods_per_year)
    if ppy is None or ppy <= 0.0:
        raise ValueError(f"periods_per_year must be a finite positive number, got {periods_per_year!r}")

    notional = _as_list(traded_notional, name="traded_notional")
    if notional is None or not notional:
        return TurnoverObservation(
            available=False,
            convention=convention,
            reason="no_observations: traded_notional is empty",
            periods_per_year=ppy,
            thresholds=_threshold_snapshot(),
        )

    n_periods = len(notional)
    window = n_periods if window_periods is None else _finite(window_periods)
    if window is None or window <= 0.0:
        raise ValueError(f"window_periods must be a finite positive number, got {window_periods!r}")

    # Equity: per-period series when the input is sized (list / ndarray /
    # Series), else a scalar broadcast to every period. Deciding by "is it
    # sized" rather than by "does float() work" keeps a 1-element array from
    # being silently reinterpreted as a capital base.
    sized = equity is not None and (
        isinstance(equity, (list, tuple, str, bytes)) or hasattr(equity, "__len__")
    )
    if equity is None:
        return TurnoverObservation(
            available=False,
            convention=convention,
            reason="equity_unavailable: no equity base supplied",
            periods_per_year=ppy,
            n_periods=n_periods,
            window_periods=window,
            thresholds=_threshold_snapshot(),
        )
    if not sized:
        equity_scalar = _finite(equity)
        if equity_scalar is None:
            return TurnoverObservation(
                available=False,
                convention=convention,
                reason="equity_unavailable: scalar equity is not a finite number",
                periods_per_year=ppy,
                n_periods=n_periods,
                window_periods=window,
                thresholds=_threshold_snapshot(),
            )
        equity_series = [equity_scalar] * n_periods
    else:
        equity_series = _as_list(equity, name="equity")
        if len(equity_series) != n_periods:
            raise ValueError(
                f"equity length {len(equity_series)} != traded_notional length {n_periods}"
            )

    ratio_sum = 0.0
    notional_total = 0.0
    for idx, (raw_n, raw_e) in enumerate(zip(notional, equity_series)):
        n_t = _finite(raw_n)
        e_t = _finite(raw_e)
        if n_t is None:
            return TurnoverObservation(
                available=False,
                convention=convention,
                reason=f"traded_notional_unavailable_at_period_{idx}",
                periods_per_year=ppy,
                n_periods=n_periods,
                window_periods=window,
                thresholds=_threshold_snapshot(),
            )
        if n_t < 0.0:
            return TurnoverObservation(
                available=False,
                convention=convention,
                reason=f"negative_traded_notional_at_period_{idx}",
                periods_per_year=ppy,
                n_periods=n_periods,
                window_periods=window,
                thresholds=_threshold_snapshot(),
            )
        if e_t is None:
            return TurnoverObservation(
                available=False,
                convention=convention,
                reason=f"equity_unavailable_at_period_{idx}",
                periods_per_year=ppy,
                n_periods=n_periods,
                window_periods=window,
                thresholds=_threshold_snapshot(),
            )
        if e_t <= 0.0:
            return TurnoverObservation(
                available=False,
                convention=convention,
                reason=f"equity_nonpositive_at_period_{idx}",
                periods_per_year=ppy,
                n_periods=n_periods,
                window_periods=window,
                thresholds=_threshold_snapshot(),
            )
        ratio_sum += n_t / e_t
        notional_total += n_t

    annualized = ratio_sum * (ppy / window)
    one_way = annualized / 2.0 if convention == _CONVENTION_TWO_WAY else annualized

    return TurnoverObservation(
        available=True,
        convention=convention,
        reason=None,
        annualized=annualized,
        one_way_annualized=one_way,
        periods_per_year=ppy,
        n_periods=n_periods,
        window_periods=window,
        traded_notional_total=notional_total,
        notional_over_equity_sum=ratio_sum,
        thresholds=_threshold_snapshot(),
    )


@dataclass(frozen=True)
class VolumeParticipation:
    """One advisory volume-participation observation, BOTH bases at once.

    * `of_daily_volume` — 占当日成交量: `traded_shares / daily_volume`, the same
      basis the backtester's `_participation_cap`
      (`common/allocation_backtester.py:1286`) budgets against;
    * `of_average_volume` — 占近 N 日均量: `traded_shares / average_volume`,
      where `average_volume` is the mean of the trailing `lookback` sessions
      (strictly before today).

    `available=False` ⇒ every numeric field is `None` and `reason` says why.
    Nothing is zero-filled and nothing is mean-filled: a missing volume is
    *unobservable*, which is not the same statement as "there was no volume".
    """

    available: bool
    reason: Optional[str] = None
    traded_shares: Optional[float] = None
    daily_volume: Optional[float] = None
    average_volume: Optional[float] = None
    lookback: int = DEFAULT_VOLUME_LOOKBACK
    history_length: int = 0
    of_daily_volume: Optional[float] = None
    of_average_volume: Optional[float] = None
    exceeds_daily_volume: Optional[bool] = None
    exceeds_average_volume: Optional[bool] = None

    def to_dict(self) -> dict:
        """JSON-safe snapshot."""
        return {
            "available": self.available,
            "reason": self.reason,
            "traded_shares": self.traded_shares,
            "daily_volume": self.daily_volume,
            "average_volume": self.average_volume,
            "lookback": self.lookback,
            "history_length": self.history_length,
            "of_daily_volume": self.of_daily_volume,
            "of_average_volume": self.of_average_volume,
            "exceeds_daily_volume": self.exceeds_daily_volume,
            "exceeds_average_volume": self.exceeds_average_volume,
        }


def _participation_unavailable(reason: str, *, lookback: int, history_length: int,
                              traded_shares: Optional[float] = None,
                              daily_volume: Optional[float] = None) -> VolumeParticipation:
    """Tri-state failure object. Numeric fields stay `None` -- never `0.0`."""
    return VolumeParticipation(
        available=False,
        reason=reason,
        traded_shares=traded_shares,
        daily_volume=daily_volume,
        average_volume=None,
        lookback=lookback,
        history_length=history_length,
        of_daily_volume=None,
        of_average_volume=None,
        exceeds_daily_volume=None,
        exceeds_average_volume=None,
    )


def volume_participation(
    traded_shares: Any,
    daily_volume: Any,
    *,
    volume_history: Any = None,
    lookback: int = DEFAULT_VOLUME_LOOKBACK,
) -> VolumeParticipation:
    """Both participation bases for one order. ADVISORY.

    Parameters
    ----------
    traded_shares:
        Order size in shares (non-negative magnitude, float allowed).
    daily_volume:
        TODAY's session volume in shares. `<= 0` is a suspension / an
        unobservable session (the backtester's `_participation_cap` draws the
        same line at `Volume <= 0`), so it becomes a tri-state failure rather
        than a zero-share budget.
    volume_history:
        The trailing sessions used for the 20-day basis, **strictly BEFORE
        today** (today is supplied separately as `daily_volume`). At least
        `lookback` finite, strictly-positive values are required; only the
        last `lookback` are used. Excluding today is deliberate: including it
        would let an unusually thin or heavy session damp or inflate the very
        baseline it is being measured against.
    lookback:
        Window length for the average basis. Default 20 ("近 20 日均量").

    Returns
    -------
    `VolumeParticipation` with `of_daily_volume`, `of_average_volume`, and the
    factual flags `exceeds_daily_volume` / `exceeds_average_volume`
    (ratio `> 1.0`, i.e. the order is larger than the whole reference volume).
    Those flags are arithmetic, not policy: no participation threshold is
    defined here (`AT4` clause 2 forbids inventing one; `AT5` must freeze its
    own mapping).

    Raises
    ------
    ValueError
        Only when `lookback` is not a positive integer (structural misuse).
    TypeError
        Only when `volume_history` is not 1-D.
    """
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 1:
        raise ValueError(f"lookback must be a positive integer, got {lookback!r}")

    hist = _as_list(volume_history, name="volume_history")
    hist_len = 0 if hist is None else len(hist)

    shares = _finite(traded_shares)
    dv = _finite(daily_volume)

    if shares is None:
        return _participation_unavailable(
            "traded_shares_unavailable", lookback=lookback, history_length=hist_len,
            daily_volume=dv,
        )
    if shares < 0.0:
        return _participation_unavailable(
            "negative_traded_shares", lookback=lookback, history_length=hist_len,
            traded_shares=shares, daily_volume=dv,
        )
    if dv is None:
        return _participation_unavailable(
            "daily_volume_unavailable", lookback=lookback, history_length=hist_len,
            traded_shares=shares,
        )
    if dv <= 0.0:
        return _participation_unavailable(
            "daily_volume_zero_or_negative (suspended / unobservable)", lookback=lookback,
            history_length=hist_len, traded_shares=shares, daily_volume=dv,
        )
    if hist is None or not hist:
        return _participation_unavailable(
            "volume_history_unavailable", lookback=lookback, history_length=hist_len,
            traded_shares=shares, daily_volume=dv,
        )
    if hist_len < lookback:
        return _participation_unavailable(
            f"insufficient_volume_history: need {lookback} prior sessions, got {hist_len}",
            lookback=lookback, history_length=hist_len, traded_shares=shares, daily_volume=dv,
        )

    window = hist[-lookback:]
    cleaned = []
    for idx, raw in enumerate(window):
        v = _finite(raw)
        if v is None:
            return _participation_unavailable(
                f"volume_history_unavailable_at_index_{idx} (NaN / non-numeric)",
                lookback=lookback, history_length=hist_len, traded_shares=shares, daily_volume=dv,
            )
        # A zero-volume session inside the window would silently deflate the
        # mean; refuse instead of dropping or zero-filling it.
        if v <= 0.0:
            return _participation_unavailable(
                f"volume_history_nonpositive_at_index_{idx}", lookback=lookback,
                history_length=hist_len, traded_shares=shares, daily_volume=dv,
            )
        cleaned.append(v)

    average = math.fsum(cleaned) / float(lookback)
    if average <= 0.0:  # pragma: no cover - unreachable given the guards above
        return _participation_unavailable(
            "average_volume_nonpositive", lookback=lookback, history_length=hist_len,
            traded_shares=shares, daily_volume=dv,
        )

    of_daily = shares / dv
    of_average = shares / average
    return VolumeParticipation(
        available=True,
        reason=None,
        traded_shares=shares,
        daily_volume=dv,
        average_volume=average,
        lookback=lookback,
        history_length=hist_len,
        of_daily_volume=of_daily,
        of_average_volume=of_average,
        exceeds_daily_volume=bool(of_daily > 1.0),
        exceeds_average_volume=bool(of_average > 1.0),
    )


# ---------------------------------------------------------------------------
# Advisory labels. Pure formatting -- they never gate, never mutate, never raise.
# ---------------------------------------------------------------------------
def turnover_label(one_way_annualized: Any) -> str:
    """`OK` / `WATCH` / `BREACH` / `MISSING` for a one-way annualised turnover.

    Bands come from the two `L2.36` constants and nothing else:

    * `<= 8.0`  -> `OK`      (合格线)
    * `<= 10.0` -> `WATCH`   (above the qualify line, still inside the ceiling)
    * `> 10.0`  -> `BREACH`  (硬上限)

    Boundaries use the strict `>` convention already established for the
    turnover gate (`L2.51` item 1: 恰 10.0 通过), so exactly `8.0` is `OK` and
    exactly `10.0` is `WATCH`, not `BREACH`.

    `MISSING` for `None`, NaN, +-inf and non-numerics -- "unknown" is never
    allowed to fall through a comparison chain into `OK` (NaN compares `False`
    against everything, which is precisely how such a bug hides). Extreme but
    finite values (`1e9`) are labelled `BREACH`, never raised.
    """
    value = _finite(one_way_annualized)
    if value is None:
        return TURNOVER_LABEL_MISSING
    if value > TURNOVER_CEILING_ANN_ONE_WAY:
        return TURNOVER_LABEL_BREACH
    if value > TURNOVER_QUALIFIED_ANN_ONE_WAY:
        return TURNOVER_LABEL_WATCH
    return TURNOVER_LABEL_OK


def participation_label(participation: Any) -> str:
    """`OBSERVED` or `MISSING` -- deliberately band-free.

    No participation ratio maps to a warning level here: any such cut-off
    would be a newly invented threshold, which `AT4` clause 2 forbids. The
    numbers (and the factual `exceeds_*` flags) are returned to the caller;
    a level mapping must be frozen separately (see `AT5`).
    """
    available = getattr(participation, "available", None)
    of_daily = getattr(participation, "of_daily_volume", None)
    of_average = getattr(participation, "of_average_volume", None)
    if available is True and _finite(of_daily) is not None and _finite(of_average) is not None:
        return PARTICIPATION_LABEL_OBSERVED
    return PARTICIPATION_LABEL_MISSING


def capacity_labels(
    *,
    turnover: Any = None,
    participation: Any = None,
) -> dict:
    """One flat, JSON-safe, ADVISORY report for a live run. Never raises.

    Accepts the `TurnoverObservation` / `VolumeParticipation` objects (or
    `None`, or anything duck-typed with the same attribute names). Missing
    objects, missing data, NaN/inf and absurd magnitudes are all rendered as
    labels -- this function has no failure mode that could abort ticket
    generation, and no branch that could change an order.

    A bare float passed as `turnover` is deliberately NOT interpreted (its
    basis would be a guess): it degrades to `MISSING`. Use
    `turnover_label(one_way_value)` for a naked number, or build a
    `TurnoverObservation` so the two-way / one-way basis travels with it.

    The returned dict always carries `advisory: True` and `gates_orders:
    False` so the "observation only" contract is machine-readable, not just a
    comment. `turnover_label` is computed from `one_way_annualized` (the
    `L2.36` basis); the raw two-way total is echoed separately so a reader can
    see both bases and cannot mistake one for the other.
    """
    report: dict = {
        "advisory": True,
        "gates_orders": False,
        "turnover_label": TURNOVER_LABEL_MISSING,
        "turnover_available": False,
        "turnover_reason": None,
        "turnover_convention": None,
        "turnover_two_way_annualized": None,
        "turnover_one_way_annualized": None,
        "turnover_thresholds": _threshold_snapshot(),
        "participation_label": PARTICIPATION_LABEL_MISSING,
        "participation_available": False,
        "participation_reason": None,
        "participation_of_daily_volume": None,
        "participation_of_average_volume": None,
        "participation_exceeds_daily_volume": None,
        "participation_exceeds_average_volume": None,
        "participation_lookback": None,
        "notes": [
            "ADVISORY ONLY: these labels never gate, resize, delay or re-price an order.",
            "Turnover lines are L2.36 one-way annualised (8.0x qualified / 10.0x ceiling); "
            "no second threshold is defined here.",
            "Participation is reported on both bases (占当日成交量 / 占近20日均量) with no "
            "policy cut-off; missing data is tri-state and never zero- or mean-filled.",
        ],
    }

    # --- turnover -----------------------------------------------------------
    if turnover is not None:
        available = getattr(turnover, "available", None)
        one_way = getattr(turnover, "one_way_annualized", None)
        two_way = getattr(turnover, "annualized", None)
        convention = getattr(turnover, "convention", None)
        report["turnover_convention"] = convention
        report["turnover_reason"] = getattr(turnover, "reason", None)
        report["turnover_available"] = bool(available is True)
        done = _finite(one_way)
        dtow = _finite(two_way)
        if convention == _CONVENTION_ONE_WAY:
            report["turnover_one_way_annualized"] = done
            report["turnover_two_way_annualized"] = None if done is None else done * 2.0
        else:
            report["turnover_two_way_annualized"] = dtow
            report["turnover_one_way_annualized"] = done
        if available is True:
            report["turnover_label"] = turnover_label(report["turnover_one_way_annualized"])
        else:
            report["turnover_label"] = TURNOVER_LABEL_MISSING

    # --- participation ------------------------------------------------------
    if participation is not None:
        available = getattr(participation, "available", None)
        label = participation_label(participation)
        report["participation_label"] = label
        report["participation_available"] = bool(available is True)
        report["participation_reason"] = getattr(participation, "reason", None)
        lookback = getattr(participation, "lookback", None)
        report["participation_lookback"] = lookback if isinstance(lookback, int) else None
        if available is True:
            report["participation_of_daily_volume"] = _finite(
                getattr(participation, "of_daily_volume", None)
            )
            report["participation_of_average_volume"] = _finite(
                getattr(participation, "of_average_volume", None)
            )
            report["participation_exceeds_daily_volume"] = getattr(
                participation, "exceeds_daily_volume", None
            )
            report["participation_exceeds_average_volume"] = getattr(
                participation, "exceeds_average_volume", None
            )

    return report
