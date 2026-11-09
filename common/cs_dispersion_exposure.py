"""`R4` -- cross-sectional return **dispersion collapse** => market-layer exposure downshift.

FROZEN JUDGEMENT: `docs/cs_dispersion_exposure.md` (`cs_dispersion/v1`). Read that document, not
this docstring, if the two ever disagree; this module implements it and may not re-interpret it.
`DECISION_SPEC_VERSION = "r4.1"` is a **judgement spec tag**, NOT a strategy version -- no
`BASE_VERSION` / per-class `version` obligation attaches to a pure module (AGENTS §7), and none
may be invented "for symmetry".

WHAT THIS IS (positioning before arithmetic)
--------------------------------------------
One question: *is the market layer's return structure collapsing -- every name moving together --
so that today's market exposure should be carried at a discount?* The answer is a **market-level
multiplier** on the market layer's target weights (`1.0` unchanged, `0.75` / `0.50` de-risked)
plus the evidence that produced it.

It is explicitly NOT: per-symbol selection (no ranking, no picking -- the operator is a
cross-sectional *summary*, a disclosed proxy for "how much independent information the tape is
carrying"); intraday timing (no path forecast, no flipping inside a session); or a
volatility-targeting overlay (the reading is a dispersion of **returns across names**, not a
forecast of one series' volatility, and it is never merged with a strategy's own vol-target
parameters).

THE OPERATOR (frozen; `R4` item 1 mandates writing this down *before* code)
--------------------------------------------------------------------------
On the frozen 5m session grid (`common.intraday_synthetic`, 48 END-labelled slots -- END means a
label `t` covers `(t-5m, t]`):

    r_i(t) = close_i(t) / close_i(t - 20 bars) - 1      # a **20-bar** return (~100 minutes)
    D(t)   = std_{ddof=1}( r_i(t) )                     # over the eligible cross-section, eq-wt

`20` is `RETURN_LOOKBACK_BARS`, a **bar** count, not a session count. `D` therefore measures how
*spread out* the recent 100-minute returns are *across names*: when every name has moved by
roughly the same amount, `D` is small; when some names ran and others fell, `D` is large.

`std`, NOT `MAD` -- and the row (`docs/development_plan.md:334` item 1) rejects MAD explicitly.
MAD degenerates to `0.0` whenever **more than half** of the sample ties (small cross-sections and
highly co-moving days are exactly the cases this rule must judge), and a `0.0` reading is
indistinguishable from "the deepest possible collapse" while also being the arithmetic artefact of
a tie. `std` stays strictly positive for any non-degenerate cross-section and *rises* on a single
outlier, which is the discriminative property `R4` acceptance item 3 requires ("a single extreme
name must not masquerade as a collapse"). The known cost of `std` -- sensitivity to one extreme
name -- is a *false negative* (inflated `D` hides a real collapse), which is the safe direction for
an advisory overlay and is disclosed rather than papered over.

BASELINE (relative, never absolute)
-----------------------------------
    B(t) = median{ D(s) : the 20 most recent *prior* sessions' D, same bar-of-day as t }

Relative rather than absolute because a raw `D` is not comparable across regimes (a quiet market's
`D` and a stressed market's `D` are different objects); "collapse" is by definition a statement
about a name's own recent history. **median, not mean**: one wild session must not lift the yardstick
above the collapse it is supposed to detect. `B` is computed from the **same panel** and from
**strictly prior sessions**, so a collapsed day can never lower its own baseline (self-contamination
would make the rule hardest to trip exactly when it matters). Fewer than `baseline_window` usable
prior `D` values => `INSUFFICIENT_DATA`, **never a shorter window and never "not triggered"** (the
same discipline as `common/overnight_derisk.py:60-61`).

COLLAPSE BANDS (frozen; `<=` de-risks, and equality is pinned by tests)
----------------------------------------------------------------------
    D >  0.75 * B                        => 1.0             (bit-identical no-op)
    0.5 * B <  D <= 0.75 * B             => 0.75            "light collapse"
    D <= 0.5 * B                         => 0.50            "collapse"

`1.0` is returned as the literal `MULTIPLIER_UNCHANGED`, never a computed `min(1.0, ...)` float:
"no collapse" must be *bit-identical* to not calling this module at all, which is `R4` acceptance
item 1. There is **no hysteresis and no cooldown**: `D` back above `0.75 * B` restores `1.0`
immediately. The rule is stateless by construction (round 1 is advisory), so a cooldown would have
to live in a mutable cache -- i.e. it would smuggle state into a pure function and make the
judgement depend on how often it was called.

ELIGIBILITY (drop and count, never interpolate)
-----------------------------------------------
A bar `t` is judged over symbols that have **every one of the 21 bars** `t-20 .. t` present on the
grid. A symbol missing any bar inside the window is **dropped and counted** (`symbols_dropped`),
never forward-filled / interpolated: a 20-bar return stitched across a hole is a return over an
unknown horizon, not a return over 100 minutes. Fewer than `MIN_CROSS_SECTION_SYMBOLS = 8`
eligible symbols => `INSUFFICIENT_DATA`, multiplier `1.0`, and `dispersion is None` -- **never
`0.0`**, because a zero would read as "measured, the flattest possible tape", which is a far
stronger (and false) claim than "not measurable".

Cash proxies (`BIL` / `511880` / `GC001`, mirroring `common/overnight_derisk.py:10-13`) are
**removed unconditionally** from the cross-section, even if a caller passes them in `universe`:
they are the residual sleeve, not the market layer, and their near-zero return dispersion would
mechanically depress `D`.

BOUNDARIES (do not "helpfully" fold these in)
--------------------------------------------
* **Not `R1`.** `R1` judges a *single portfolio's* intraday drawdown (`hwm_intra` / `dd_trough`) and
  its own thresholds; this rule judges *cross-sectional structure*. The two are independent
  judgements: never share a threshold, never call one from the other, never merge them into a
  combined "risk score" (`docs/development_plan.md:334` item 5 says this verbatim).
* **Not `R7`.** Missing-5m-bar freezing of new entries is `R7`'s ownership; this module only
  *counts* incomplete symbols and de-risks the market layer. It never freezes orders.
* **Round 1 is advisory only.** `advisory=True` / `gates_orders=False` travel on every result, and
  this round wires nothing: no `scripts/run_live_strategy.py` change, no weight rewrite, no
  `blend_base.py` touch, no strategy-class change, no version-axis movement. "Never a gate" is the
  fence idiom of `common/specific_alpha.py:536-544`: the only thing a caller may do with a result
  this round is print it.
* **Pure**: no I/O, no clock reads, no network, no randomness, no global state, and **no warnings**
  -- the closed `status` vocabulary is the only signal, so a caller sweeping hundreds of sessions is
  not spammed and "insufficient" can never be lost in a warning filter. Same inputs => equal outputs.
"""

from __future__ import annotations

import datetime as _dt
import enum
import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from common.intraday_synthetic import GRID_LABELS, wall_clock

#: Judgement spec tag (`cs_dispersion/v1`, rule `r4.1`). NOT a strategy version.
DECISION_SPEC_VERSION = "r4.1"

#: Frozen parameters (see `docs/cs_dispersion_exposure.md` for the parameter table and each value's
#: precedent). Re-register in the freeze document (append a `## Revision`) before changing any.
RETURN_LOOKBACK_BARS = 20          # bars, ~100 minutes -- mirror of `overnight_derisk.gap_window=20`
DEFAULT_BASELINE_WINDOW = 20       # prior sessions of D feeding the median
MIN_CROSS_SECTION_SYMBOLS = 8      # fewer eligible symbols => INSUFFICIENT_DATA (never 0.0)
LIGHT_COLLAPSE_RATIO = 0.75        # `D <= 0.75 * B` => light collapse
FULL_COLLAPSE_RATIO = 0.50         # `D <= 0.50 * B` => collapse
MULTIPLIER_UNCHANGED = 1.0         # the literal 1.0 (bit-identical no-op), never a computed 1.0
MULTIPLIER_LIGHT = 0.75
MULTIPLIER_FULL = 0.50

#: Cash proxies -- the residual sleeve, not the market layer (`common/overnight_derisk.py:10-13`).
CASH_PROXY_SYMBOLS: frozenset = frozenset({"BIL", "511880", "GC001"})

#: The bar-of-day at which a session's `D` is read: the frozen grid's last END label (15:00 for the
#: A-share session). A basis point of comparison must hold the phase fixed, otherwise `D(t)` and
#: `B(t)` are measured over different parts of the day.
DEFAULT_BAR_LABEL = "15:00"

_LABEL_INDEX: dict = {label: i for i, label in enumerate(GRID_LABELS)}
BARS_PER_SESSION = len(GRID_LABELS)


class CsdStatus(str, enum.Enum):
    """Closed four-state vocabulary (the `.value` strings are the observable contract).

    `INSUFFICIENT_DATA` is deliberately its own member and is **neither** `NORMAL` **nor** `0.0`:
    folding "we cannot measure the cross-section" into "the tape is not collapsing" is the
    missing-data-reads-as-a-risk-conclusion bug class this repo exists to avoid (`common/intraday_features.py`
    / `common/overnight_derisk.py:65-70` set the precedent).
    """

    NORMAL = "NORMAL"
    LIGHT_COLLAPSE = "LIGHT_COLLAPSE"
    COLLAPSE = "COLLAPSE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


STATUSES: Tuple[str, ...] = tuple(s.value for s in CsdStatus)


@dataclass(frozen=True)
class CsdConfig:
    """Frozen judgement parameters (validated at construction, so a bad budget cannot run)."""

    return_lookback_bars: int = RETURN_LOOKBACK_BARS
    baseline_window: int = DEFAULT_BASELINE_WINDOW
    min_cross_section: int = MIN_CROSS_SECTION_SYMBOLS
    light_collapse_ratio: float = LIGHT_COLLAPSE_RATIO
    full_collapse_ratio: float = FULL_COLLAPSE_RATIO
    light_multiplier: float = MULTIPLIER_LIGHT
    full_multiplier: float = MULTIPLIER_FULL

    def __post_init__(self) -> None:
        for name, minimum in (("return_lookback_bars", 1), ("baseline_window", 1),
                              ("min_cross_section", 2)):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an int, got {type(value).__name__}: {value!r}")
            # `min_cross_section >= 2` because `D` is a *sample* std (ddof=1): one symbol cannot
            # produce a dispersion, and a floor of 1 would silently return NaN instead of refusing.
            if value < minimum:
                raise ValueError(f"{name} must be >= {minimum}, got {value!r}")
        for name in ("light_collapse_ratio", "full_collapse_ratio",
                     "light_multiplier", "full_multiplier"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} must be a number, got {type(value).__name__}")
            if not math.isfinite(float(value)) or not (0.0 < float(value) < 1.0):
                # 0.0 is liquidation (not a de-risk) and >= 1.0 is leverage (not a downshift);
                # both are outside this rule's semantics (`overnight_derisk` makes the same check).
                raise ValueError(f"{name} must satisfy 0 < {name} < 1, got {value!r}")
        # Strict ordering: equal ratios would make the `<=` bands non-disjoint (the second branch
        # would be unreachable), which is a silently dead threshold rather than a tighter one.
        if not float(self.full_collapse_ratio) < float(self.light_collapse_ratio):
            raise ValueError(
                "full_collapse_ratio must be strictly below light_collapse_ratio "
                f"(got {self.full_collapse_ratio!r} vs {self.light_collapse_ratio!r}); equal "
                "ratios make the collapse band unreachable"
            )
        if not float(self.full_multiplier) < float(self.light_multiplier):
            raise ValueError(
                "full_multiplier must be strictly below light_multiplier "
                f"(got {self.full_multiplier!r} vs {self.light_multiplier!r})"
            )


@dataclass(frozen=True)
class CsdDecision:
    """The judgement for one bar: a market-layer multiplier plus the evidence behind it.

    Field semantics worth pinning:

    * ``multiplier`` -- `1.0` is the literal `MULTIPLIER_UNCHANGED`, so "no collapse" is
      bit-identical to not calling this module. Never `0.999...`.
    * ``dispersion`` / ``baseline`` -- `None` when **not measurable** (never `0.0`, which would read
      as "measured, the flattest possible tape"). A thin cross-section gives `dispersion is None`; an
      unusable baseline leaves a real `dispersion` with `baseline is None`.
    * ``ratio`` -- `dispersion / baseline` when both exist, else `None`.
    * ``status`` -- one of `CsdStatus`; `INSUFFICIENT_DATA` is neither "normal" nor "collapsed".
    * ``symbols_used`` / ``symbols_dropped`` -- the eligible cross-section and what was **dropped
      and counted** (missing a bar inside the window, or absent from the panel). Never imputed.
    * ``advisory`` / ``gates_orders`` -- round-1 fence: this result may be printed, never obeyed.
    """

    multiplier: float
    dispersion: Optional[float]
    baseline: Optional[float]
    status: str
    reason: str
    symbols_used: Tuple[str, ...]
    symbols_dropped: Tuple[str, ...]
    ratio: Optional[float] = None
    session_date: Optional[_dt.date] = None
    bar_label: str = DEFAULT_BAR_LABEL
    baseline_observations: int = 0
    advisory: bool = True
    gates_orders: bool = False


# --------------------------------------------------------------------------- #
# panel -> frozen-grid close matrix
# --------------------------------------------------------------------------- #
def _require_plain_date(value: object) -> _dt.date:
    """Reject a `datetime` (a `date` subclass that never equals a `date`) and non-dates."""
    if not isinstance(value, _dt.date) or isinstance(value, _dt.datetime):
        raise TypeError(
            "session_date must be a plain datetime.date (not datetime), got "
            f"{type(value).__name__}"
        )
    return value


def exclude_cash_proxies(symbols: Iterable[str]) -> Tuple[str, ...]:
    """The cross-section without the cash proxies (order preserved, duplicates dropped).

    Mirrors `common/overnight_derisk.py:10-13` / `r6.3`: the cross-section is the **market layer**,
    and the cash sleeve is by construction the residual -- including it would put a near-zero
    dispersion series inside `D` and mechanically depress the reading.
    """
    seen: dict = {}
    for symbol in symbols:
        name = str(symbol)
        if name in CASH_PROXY_SYMBOLS or name in seen:
            continue
        seen[name] = None
    return tuple(seen)


def session_close_matrix(bars: pd.DataFrame, *,
                         symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """Session closes on the **frozen 5m grid**, as rows `(session_date, bar_label)` x symbol.

    Why a matrix and not a per-symbol series: the operator needs `close_i(t - 20 bars)`, and "20
    bars back" is a position on the *session clock*, not on a symbol's own row order. On a
    reindexed grid a `shift(20)` is exactly 20 bars earlier, and a hole in the panel stays a hole
    (`NaN`) instead of silently shortening the lookback -- which is why nothing here fills or
    interpolates.

    Conventions (frozen, and load-bearing):

    * rows are the full Cartesian product of the session dates present in `bars` and
      `GRID_LABELS` (48 END labels), in grid order, so positional shifts are bar shifts;
    * the timestamp column is read through `common.intraday_synthetic.wall_clock`, i.e. the
      **labelled** wall clock -- a mis-zoned feed keeps its labels and is not re-pinned;
    * labels that are not on the frozen grid (a lunch-break bar, say) are **ignored**, never
      snapped to the nearest slot: snapping would invent a price at an instant the feed never
      printed;
    * duplicated rows for the same `(symbol, session, label)` collapse to the **last** one in input
      order (deterministic; never averaged -- an average of two prints is a price nobody traded).
    """
    required = ("symbol", "ts", "close")
    missing = [c for c in required if c not in bars.columns]
    if missing:
        raise ValueError(f"bars is missing required column(s) {missing}; got {list(bars.columns)}")

    wall = wall_clock(pd.Series(np.asarray(bars["ts"])))
    closes = pd.to_numeric(pd.Series(np.asarray(bars["close"])), errors="raise")
    work = pd.DataFrame({
        "symbol": [str(s) for s in np.asarray(bars["symbol"])],
        "close": closes.to_numpy(dtype=float),
        "_date": np.asarray(wall.dt.date),
        "_label": np.asarray(wall.dt.strftime("%H:%M")),
    })
    work["_pos"] = work["_label"].map(_LABEL_INDEX)
    work = work[work["_pos"].notna()]
    if symbols is not None:
        keep = {str(s) for s in symbols}
        work = work[work["symbol"].isin(keep)]
    if work.empty:
        empty_index = pd.MultiIndex.from_arrays([[], []], names=["session_date", "bar_label"])
        return pd.DataFrame(index=empty_index, dtype=float)

    sessions = sorted(work["_date"].unique())
    ordinal = {day: i for i, day in enumerate(sessions)}
    work["_g"] = work["_date"].map(ordinal).astype(int) * BARS_PER_SESSION + work["_pos"].astype(int)

    matrix = work.pivot_table(index="_g", columns="symbol", values="close", aggfunc="last")
    matrix = matrix.reindex(range(len(sessions) * BARS_PER_SESSION))
    matrix.index = pd.MultiIndex.from_product(
        [sessions, list(GRID_LABELS)], names=["session_date", "bar_label"]
    )
    matrix.columns.name = None
    return matrix[sorted(matrix.columns)]


def window_returns(close_matrix: pd.DataFrame,
                   *, lookback_bars: int = RETURN_LOOKBACK_BARS) -> pd.DataFrame:
    """`r_i(t) = close_i(t) / close_i(t - lookback_bars) - 1`, `NaN` unless the window is complete.

    A cell is kept **only** if all `lookback_bars + 1` bars `t-lookback_bars .. t` are present for
    that symbol (a rolling non-NaN count, not a fill): a return stitched across a hole covers an
    unknown horizon, so it is not the 20-bar return this rule is defined on.
    """
    if not isinstance(lookback_bars, int) or isinstance(lookback_bars, bool) or lookback_bars < 1:
        raise ValueError(f"lookback_bars must be an int >= 1, got {lookback_bars!r}")
    width = lookback_bars + 1
    complete = close_matrix.rolling(width, min_periods=width).count() == width
    returns = close_matrix / close_matrix.shift(lookback_bars) - 1.0
    return returns.where(complete)


def dispersion_frame(bars: pd.DataFrame, *, lookback_bars: int = RETURN_LOOKBACK_BARS,
                     symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """Per-bar cross-sectional dispersion of the 20-bar returns (`columns: dispersion, n_used`).

    `dispersion` is the **equal-weighted sample std (ddof=1)** across the eligible symbols, `NaN`
    when fewer than two are eligible (a dispersion of one number does not exist). `n_used` is the
    count of finite cells and is exposed so a caller can see the cross-section width rather than
    infer it from a NaN.
    """
    matrix = session_close_matrix(bars, symbols=symbols)
    returns = window_returns(matrix, lookback_bars=lookback_bars)
    n_used = returns.count(axis=1).astype(int)
    dispersion = returns.std(axis=1, ddof=1)
    # Explicit mask instead of relying on pandas' ddof=1 corner case: with one usable symbol the
    # sample std is undefined, and "undefined" must not surface as a number.
    dispersion = dispersion.where(n_used >= 2)
    return pd.DataFrame({"dispersion": dispersion, "n_used": n_used})


# --------------------------------------------------------------------------- #
# the judgement
# --------------------------------------------------------------------------- #
def classify_dispersion(dispersion: Optional[float], baseline: Optional[float],
                        config: Optional[CsdConfig] = None) -> Tuple[str, float, str]:
    """Map a `(D, B)` pair onto `(status, multiplier, reason)` -- the frozen bands, nothing else.

    Split out from the panel plumbing on purpose: the band boundaries are pinned by tests on
    **exact** floats here (`D == 0.75 * B` de-risks; `D` one ULP above does not), which is not
    achievable through compounded price arithmetic.

    `<=` de-risks on both bands (`0.5 * B < D <= 0.75 * B` is the light band, `D <= 0.5 * B` the
    full one), and the comparison is `<=` rather than `<` because the thresholds are a *risk
    budget*, not a filter: being exactly at the budget still means the budget was reached. `1.0`
    is returned as the literal `MULTIPLIER_UNCHANGED` so "no collapse" is bit-identical.
    """
    cfg = config or CsdConfig()
    if dispersion is None or not math.isfinite(float(dispersion)):
        return (CsdStatus.INSUFFICIENT_DATA.value, MULTIPLIER_UNCHANGED,
                "cross-sectional dispersion is not measurable (thin cross-section): "
                f"need >= {cfg.min_cross_section} symbols with a complete "
                f"{cfg.return_lookback_bars}-bar window")
    value = float(dispersion)
    if baseline is None or not math.isfinite(float(baseline)):
        return (CsdStatus.INSUFFICIENT_DATA.value, MULTIPLIER_UNCHANGED,
                f"baseline is not measurable: need {cfg.baseline_window} prior sessions of "
                "dispersion at the same bar-of-day, never a shorter window")
    base = float(baseline)
    if base <= 0.0:
        # A non-positive baseline makes the ratio meaningless (and `0.0` would read as "every prior
        # session collapsed"). Refusing beats dividing by a degenerate yardstick.
        return (CsdStatus.INSUFFICIENT_DATA.value, MULTIPLIER_UNCHANGED,
                f"baseline is non-positive ({base!r}); the ratio is undefined, not triggered")
    ratio = value / base
    if ratio <= float(cfg.full_collapse_ratio):
        return (CsdStatus.COLLAPSE.value, MULTIPLIER_FULL,
                f"dispersion collapsed: D={value:.6f} vs baseline B={base:.6f} "
                f"(D/B={ratio:.4f} <= {cfg.full_collapse_ratio:.2f}) -> market-layer exposure "
                f"x{MULTIPLIER_FULL:.2f}")
    if ratio <= float(cfg.light_collapse_ratio):
        return (CsdStatus.LIGHT_COLLAPSE.value, MULTIPLIER_LIGHT,
                f"dispersion thinning: D={value:.6f} vs baseline B={base:.6f} "
                f"(D/B={ratio:.4f} <= {cfg.light_collapse_ratio:.2f}) -> market-layer exposure "
                f"x{MULTIPLIER_LIGHT:.2f}")
    return (CsdStatus.NORMAL.value, MULTIPLIER_UNCHANGED,
            f"dispersion normal: D={value:.6f} vs baseline B={base:.6f} "
            f"(D/B={ratio:.4f} > {cfg.light_collapse_ratio:.2f}) -> exposure unchanged (x1.00)")


def _usable_dispersion(returns: pd.DataFrame, row: tuple, symbols: Sequence[str],
                       min_n: int) -> Tuple[Optional[float], Tuple[str, ...]]:
    """`D` at one grid row over `symbols`, plus the symbols that actually contributed.

    Returns `(None, used)` when fewer than `min_n` symbols are eligible: the caller turns that into
    `INSUFFICIENT_DATA`, and the count is preserved so the reason can say *how* thin the
    cross-section was instead of only that it was.
    """
    columns = [s for s in symbols if s in returns.columns]
    if not columns:
        return None, ()
    values = returns.loc[row, columns].to_numpy(dtype=float)
    finite = np.isfinite(values)
    used = tuple(columns[i] for i in np.flatnonzero(finite))
    if len(used) < min_n:
        return None, used
    return float(np.std(values[finite], ddof=1)), used


def evaluate_exposure(bars: pd.DataFrame, session_date: _dt.date, *,
                      config: Optional[CsdConfig] = None,
                      bar_label: str = DEFAULT_BAR_LABEL,
                      universe: Optional[Sequence[str]] = None) -> CsdDecision:
    """Judge one session's market-layer exposure multiplier from a 5m panel.

    Parameters
    ----------
    bars
        A 5m panel with at least `symbol` / `ts` / `close` (the `common.intraday_synthetic`
        schema). Extra columns are ignored.
    session_date
        The session being judged (plain `datetime.date`). Must exist in the panel -- a session the
        panel never saw is a caller error, so it raises instead of returning a quiet `1.0`.
    bar_label
        The frozen-grid bar at which `D(t)` and every baseline `D` are read. Default `15:00` (the
        last END label): the baseline must hold the phase fixed, or `D(t)` and `B(t)` describe
        different parts of the day.
    universe
        The market layer the caller loaded for that market. Cash proxies are removed
        **unconditionally**; symbols in `universe` that never appear (or lack a complete window)
        are counted in `symbols_dropped`, never imputed. Omit to use every non-cash-proxy symbol in
        the panel.
    """
    cfg = config or CsdConfig()
    day = _require_plain_date(session_date)
    if bar_label not in _LABEL_INDEX:
        raise ValueError(
            f"bar_label={bar_label!r} is not on the frozen 5m grid "
            f"(first/last labels: {GRID_LABELS[0]!r} / {GRID_LABELS[-1]!r})"
        )

    matrix = session_close_matrix(bars)
    present = [str(column) for column in matrix.columns]
    if universe is None:
        requested = exclude_cash_proxies(present)
    else:
        requested = exclude_cash_proxies(universe)

    if matrix.empty:
        return CsdDecision(
            multiplier=MULTIPLIER_UNCHANGED, dispersion=None, baseline=None,
            status=CsdStatus.INSUFFICIENT_DATA.value,
            reason="panel is empty: no bars, hence no cross-section",
            symbols_used=(), symbols_dropped=tuple(requested),
            session_date=day, bar_label=bar_label,
        )
    returns = window_returns(matrix, lookback_bars=cfg.return_lookback_bars)
    sessions = list(dict.fromkeys(matrix.index.get_level_values(0)))
    if day not in sessions:
        raise ValueError(
            f"session_date={day.isoformat()} is not in the panel "
            f"(panel sessions: {sessions[0].isoformat()} .. {sessions[-1].isoformat()})"
        )

    row = (day, bar_label)
    dispersion, used = _usable_dispersion(returns, row, requested, cfg.min_cross_section)
    dropped = tuple(s for s in requested if s not in used)

    index = sessions.index(day)
    prior = sessions[max(0, index - cfg.baseline_window):index]
    baseline_observations: list = []
    for session in prior:
        value, _ = _usable_dispersion(returns, (session, bar_label), requested,
                                      cfg.min_cross_section)
        if value is not None:
            baseline_observations.append(value)
    baseline = (float(np.median(baseline_observations))
                if len(baseline_observations) >= cfg.baseline_window else None)

    status, multiplier, reason = classify_dispersion(dispersion, baseline, cfg)
    if dispersion is None:
        reason = (
            f"cross-section too thin: {len(used)} eligible symbol(s) with a complete "
            f"{cfg.return_lookback_bars}-bar window at {bar_label}, need "
            f">= {cfg.min_cross_section}; dispersion is not measurable (never 0.0)"
        )
    elif baseline is None:
        reason = (
            f"baseline unavailable: only {len(baseline_observations)} of "
            f"{cfg.baseline_window} prior sessions had a measurable dispersion at {bar_label}; "
            "a shorter baseline is not used"
        )
    ratio = (float(dispersion) / float(baseline)
             if dispersion is not None and baseline not in (None, 0.0) else None)
    return CsdDecision(
        multiplier=multiplier,
        dispersion=dispersion,
        baseline=baseline,
        ratio=ratio,
        status=status,
        reason=reason,
        symbols_used=used,
        symbols_dropped=dropped,
        session_date=day,
        bar_label=bar_label,
        baseline_observations=len(baseline_observations),
    )


__all__ = [
    "DECISION_SPEC_VERSION", "RETURN_LOOKBACK_BARS", "DEFAULT_BASELINE_WINDOW",
    "MIN_CROSS_SECTION_SYMBOLS", "LIGHT_COLLAPSE_RATIO", "FULL_COLLAPSE_RATIO",
    "MULTIPLIER_UNCHANGED", "MULTIPLIER_LIGHT", "MULTIPLIER_FULL", "CASH_PROXY_SYMBOLS",
    "DEFAULT_BAR_LABEL", "BARS_PER_SESSION", "STATUSES",
    "CsdStatus", "CsdConfig", "CsdDecision", "exclude_cash_proxies", "session_close_matrix",
    "window_returns", "dispersion_frame", "classify_dispersion", "evaluate_exposure",
]
