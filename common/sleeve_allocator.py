"""Core-satellite sleeve allocator -- a static basket `AllocationTemplate`.

M2.2 of `docs/development_plan.md`: a fixed-budget, two-risky-sleeve composite
that lives ALONGSIDE the institutional blend family
(`pipeline/research/blend/`), never inside it. It is deliberately a plain
`AllocationTemplate` subclass -- zero-arg constructible, no
`BaseBlendAllocationStrategy` dependency, so it changes no existing template's
code path and no `BASE_VERSION` (blend base) bump is due.

Construction
------------
    w_total = sum_s budget_s * w_sleeve_s

* `sleeve_beta_budget`   (default 0.25) -- the "core" sleeve (broad-market
  beta exposure).
* `sleeve_alpha_budget`  (default 0.55) -- the "satellite" sleeve.
* `sleeve_cash_budget`   (default 0.20) -- the deliberate cash reserve.

The three budgets MUST sum to 1.0 (`_BUDGET_TOLERANCE`); a violation raises
`ValueError` immediately because it is a CONFIGURATION error, not a runtime
condition a template should silently degrade around. The cash budget is not a
fourth "sleeve" whose members are weighted: it is the residual
`max(0, 1 - sum(risky))`, written explicitly into `sleeve_cash_ticker` when the
caller designates one (via the shared `_cap_and_deroute_to_cash` helper), and
otherwise left implicit under this workspace's "risky weights sum to <= 1"
convention.

Crisis overlay (beta only)
--------------------------
`sleeve_crisis_scale` (default 0.5) multiplies ONLY the beta sleeve's weights;
the alpha sleeve is bit-identical with and without the overlay. The crisis
signal itself is CALLER-SUPPLIED (`sleeve_crisis_active`, a bool or a
date-keyed bool Series/dict) -- a DISCLOSED SIMPLIFICATION, not an oversight:
this repo has no crisis/vol-regime detector that a zero-arg static template can
reuse. The one breadth/thrust detector that exists lives in
`scripts/check_live_portfolio_health.py`, a live-ops module; importing it from
`common/` would invert the dependency direction and drag live-trading concerns
into the research pipeline. `BreadthGatedMomentumAllocation`'s own breadth
de-risking is inlined in its `generate_weights`, not a reusable primitive. So
the flag is a hook: the caller (a live runner, or a research harness that has
already classified the regime) decides "crisis", and this template only applies
the documented, deterministic scale to the core sleeve.

Within-sleeve sizing
--------------------
`sleeve_within_weighting` is `"equal"` (default) or `"inverse_vol"`; the latter
delegates to the shared `_inverse_vol_weights` primitive with `scale=budget`,
so the two sleeves' weights are directly comparable. Equal weight is the
default building block on purpose: it is the minimal, estimator-free
allocation, and inverse-vol only earns its place when the caller has enough
history for a stable realized-vol estimate (`sleeve_vol_lookback`, default 60).
The tradeoff is the standard one -- equal weight ignores risk contribution and
therefore over-weights the volatile sleeve member, while inverse-vol is
sensitive to a single noisy vol estimate (and is undefined for a flat series).

Ticker sets, exclusion, and migration
-------------------------------------
`sleeve_beta_tickers` / `sleeve_alpha_tickers` are flat ticker lists, or
date-keyed schedules (dict / Series mapping a rebalance date to that day's
membership) for a caller that changes sleeve membership between dates. A
schedule is a step function: the last entry at or before a rebalance date
applies until the next stated change, and before the first stated date the
sleeve holds nothing. A symbol may not belong to both sleeves at the same time
(`ValueError`), and a symbol in NEITHER set is excluded -- written as an
explicit `0.0` on every rebalance row.

If only one sleeve is configured (flat), the other falls back to the universe
complement of it. If neither is configured at all, every universe symbol
participates in BOTH sleeves, i.e. each receives `budget_beta/N +
budget_alpha/N` -- a documented, non-degenerate default (total risky exposure
`budget_beta + budget_alpha`) rather than an empty portfolio.

Sparse-weights contract (CRITICAL)
----------------------------------
Rows off the rebalance schedule are `NaN`; the template never `ffill()`s its
own output. On a rebalance row EVERY universe symbol carries a real number --
excluded symbols and symbols that left a sleeve get explicit `0.0`, never
`NaN`, because the backtester's column-wise `ffill()` would otherwise carry a
stale position forward forever. Because of that, this module builds the sparse
frame by writing the full row vector on each scheduled rebalance date (the
convention every existing static template uses) instead of routing the frame
through `_sparse_from_daily`: that helper's row selection is
value-difference-based, and it would DROP a scheduled rebalance row whose
target happens to repeat the previous one -- exactly the "recompute the
identical target" case the module docstring of
`common/allocation_templates.py` warns about. `_sparse_from_daily`'s own
negligible-weight convention (`1e-4`, from `_step_asset_inertia`) IS reused:
micro-weights are pruned to explicit `0.0`.

Registry note: `common/allocation_templates.py` appends `SleeveAllocator` to
`ALLOCATION_TEMPLATES` at the bottom of that module, guarded, because this
module imports the base class/helpers from it. Either import order works --
see the comment at that guard.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple, Union

import numpy as np
import pandas as pd

from common.allocation_templates import (
    AllocationTemplate,
    _cap_and_deroute_to_cash,
    _get_master_index,
    _inverse_vol_weights,
)
from common.indicators import realized_vol
from common.scheduling import get_rebalance_dates as _get_rebalance_dates

__all__ = ["SleeveAllocator"]

_logger = logging.getLogger(__name__)

# The existing negligible-weight floor: `_step_asset_inertia` (see
# common/allocation_templates.py) treats a target weight at or below `1e-4` as
# "not held", so anything under it is pruned to an EXPLICIT 0.0 here rather
# than shipped as a tiny position. Reused rather than re-derived.
_NEGLIGIBLE_WEIGHT = 1e-4

# Budgets are a configuration contract (`sum == 1`), so they are compared with
# a float tolerance and a violation is a hard error, never a degrade.
_BUDGET_TOLERANCE = 1e-9

_DEFAULT_BETA_BUDGET = 0.25
_DEFAULT_ALPHA_BUDGET = 0.55
_DEFAULT_CASH_BUDGET = 0.20
_DEFAULT_CRISIS_SCALE = 0.5
_DEFAULT_VOL_LOOKBACK = 60

# Sentinel cash-proxy name that is never a real symbol, used when the caller
# did not designate `sleeve_cash_ticker`: `_cap_and_deroute_to_cash` then only
# enforces the `cap` (a no-op for a budget-constrained composite) and the cash
# leg stays implicit under the sum-to-<=1 convention.
_NO_CASH_PROXY = "\x00sleeve_allocator_no_cash"

SleeveSpec = Union[FrozenSet[str], Dict[str, FrozenSet[str]]]


def _resolve_budgets(params: Mapping) -> Tuple[float, float, float]:
    """Returns `(beta, alpha, cash)` after asserting the budget contract.

    Raises `ValueError` (never degrades) when the budgets do not sum to 1.0 or
    any of them is negative: a portfolio whose sleeve budgets do not partition
    capital is a bug in the caller's configuration, and silently renormalising
    it would hide a sizing mistake behind an apparently valid weight vector.
    """
    beta = float(params.get("sleeve_beta_budget", _DEFAULT_BETA_BUDGET))
    alpha = float(params.get("sleeve_alpha_budget", _DEFAULT_ALPHA_BUDGET))
    cash = float(params.get("sleeve_cash_budget", _DEFAULT_CASH_BUDGET))

    for label, value in (("beta", beta), ("alpha", alpha), ("cash", cash)):
        if not np.isfinite(value) or value < 0.0:
            raise ValueError(
                f"SleeveAllocator: sleeve_{label}_budget must be a finite non-negative "
                f"fraction, got {value!r}."
            )

    total = beta + alpha + cash
    if abs(total - 1.0) > _BUDGET_TOLERANCE:
        raise ValueError(
            "SleeveAllocator: sleeve budgets must sum to exactly 1.0 "
            f"(sleeve_beta_budget + sleeve_alpha_budget + sleeve_cash_budget), got "
            f"{beta} + {alpha} + {cash} = {total}. This is a configuration error, not a "
            "runtime condition -- fix the budgets instead of relying on the template to "
            "renormalise them."
        )
    return beta, alpha, cash


def _is_schedule(raw) -> bool:
    return isinstance(raw, (Mapping, pd.Series))


def _normalize_sleeve_spec(raw, symbols: Sequence[str], cash_ticker: Optional[str]) -> SleeveSpec:
    """Normalizes a sleeve spec into either a frozenset (constant membership) or
    a `{Timestamp: frozenset}` schedule (date-varying membership).

    Symbols outside the universe, and the designated cash ticker, are dropped:
    a sleeve can only ever hold instruments the caller actually supplied.
    """
    allowed = {s for s in symbols if s != cash_ticker}

    def _clean(values) -> FrozenSet[str]:
        if values is None:
            return frozenset()
        if isinstance(values, str):
            values = [values]
        return frozenset(s for s in values if s in allowed)

    if _is_schedule(raw):
        normalized: Dict[Union[pd.Timestamp, str], FrozenSet[str]] = {}
        for key, value in raw.items():
            try:
                parsed: Union[pd.Timestamp, str] = pd.Timestamp(key)
            except (TypeError, ValueError):
                parsed = str(key)
            normalized[parsed] = _clean(value)
        return normalized
    return _clean(raw)


def _membership_at(spec: SleeveSpec, date: pd.Timestamp) -> Optional[FrozenSet[str]]:
    """Membership for one rebalance date.

    A schedule is a STEP FUNCTION: the last entry at or before `date` applies
    until the caller states the next change, and before the first stated date
    the sleeve holds nothing. This is the caller's own last stated policy, not
    an accidental carry-forward -- a missing entry never means "keep whatever
    the previous rebalance happened to hold", which is exactly the silent-hold
    hazard the sparse contract exists to prevent (the weights themselves are
    always rewritten in full on each rebalance row).
    """
    if not isinstance(spec, dict):
        return spec
    if date in spec:
        return spec[date]
    if str(date) in spec:
        return spec[str(date)]
    prior = [key for key in spec if isinstance(key, pd.Timestamp) and key <= date]
    if prior:
        return spec[max(prior)]
    return frozenset()


def _resolve_crisis_flags(raw, rebalance_dates: pd.DatetimeIndex) -> Dict[pd.Timestamp, bool]:
    """Expands the caller-supplied crisis hook into one bool per rebalance date.

    Accepts a plain bool (constant regime), a date-keyed `dict`/`pd.Series`
    (already-classified regime), or `None`/anything falsy (no crisis). See the
    module docstring for why this is caller-supplied rather than detected here.
    """
    if raw is None or raw is False:
        return {d: False for d in rebalance_dates}
    if isinstance(raw, (bool, np.bool_)):
        flag = bool(raw)
        return {d: flag for d in rebalance_dates}
    if isinstance(raw, pd.Series):
        if raw.empty:
            return {d: False for d in rebalance_dates}
        indexed = raw.copy()
        try:
            indexed.index = pd.to_datetime(indexed.index)
        except (TypeError, ValueError):
            _logger.warning(
                "SleeveAllocator: sleeve_crisis_active Series index is not datetime-like; "
                "treating the crisis overlay as inactive."
            )
            return {d: False for d in rebalance_dates}
        reindexed = indexed.reindex(indexed.index.union(rebalance_dates)).ffill()
        return {d: bool(reindexed.get(d, False)) for d in rebalance_dates}
    if isinstance(raw, (dict, Mapping)):
        out: Dict[pd.Timestamp, bool] = {}
        for d in rebalance_dates:
            value = raw.get(d, raw.get(str(d), False))
            out[d] = bool(value)
        return out
    raise ValueError(
        "SleeveAllocator: sleeve_crisis_active must be a bool, a date-keyed dict/Series, "
        f"or None, got {type(raw).__name__}."
    )


@dataclass
class SleeveAllocator(AllocationTemplate):
    name: str = "sleeve_allocator"
    param_grid: dict = field(default_factory=lambda: {
        "rebalance_freq_days": [5, 21, 63],  # Weekly, Monthly, Quarterly
        "sleeve_within_weighting": ["equal", "inverse_vol"],
        "sleeve_vol_lookback": [_DEFAULT_VOL_LOOKBACK],
    })
    factor_tags: list = field(default_factory=lambda: ["static_fixed_weight"])
    version: str = "1.0.0"

    # ------------------------------------------------------------------ #
    # public AllocationTemplate API
    # ------------------------------------------------------------------ #
    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        params = params or {}
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = _get_master_index(universe)
        if len(master_index) == 0:
            return pd.DataFrame()

        beta_budget, alpha_budget, _cash_budget = _resolve_budgets(params)

        within = str(params.get("sleeve_within_weighting", "equal")).strip().lower()
        if within not in ("equal", "inverse_vol"):
            raise ValueError(
                "SleeveAllocator: sleeve_within_weighting must be 'equal' or 'inverse_vol', "
                f"got {params.get('sleeve_within_weighting')!r}."
            )

        crisis_scale = float(params.get("sleeve_crisis_scale", _DEFAULT_CRISIS_SCALE))
        if not (0.0 <= crisis_scale <= 1.0):
            raise ValueError(
                "SleeveAllocator: sleeve_crisis_scale must be a fraction in [0, 1] -- it "
                f"multiplies the beta sleeve's weights, got {crisis_scale!r}."
            )

        vol_lookback = int(params.get("sleeve_vol_lookback", _DEFAULT_VOL_LOOKBACK))
        freq = int(params.get("rebalance_freq_days", 21))
        rebalance_dates = _get_rebalance_dates(master_index, freq)

        weights_df = pd.DataFrame(index=master_index, columns=symbols, dtype=float, data=np.nan)
        if len(rebalance_dates) == 0:
            return weights_df

        cash_ticker = params.get("sleeve_cash_ticker")
        if cash_ticker not in symbols:
            cash_ticker = None

        beta_spec = _normalize_sleeve_spec(params.get("sleeve_beta_tickers"), symbols, cash_ticker)
        alpha_spec = _normalize_sleeve_spec(params.get("sleeve_alpha_tickers"), symbols, cash_ticker)
        beta_scheduled, alpha_scheduled = _is_schedule(beta_spec), _is_schedule(alpha_spec)
        risky_universe = [s for s in symbols if s != cash_ticker]

        crisis_flags = _resolve_crisis_flags(params.get("sleeve_crisis_active"), rebalance_dates)

        vols = None
        if within == "inverse_vol":
            vols = pd.DataFrame(index=master_index, columns=symbols, dtype=float)
            for sym, df in universe.items():
                if not df.empty and "Close" in df.columns:
                    vols[sym] = realized_vol(df["Close"], window=vol_lookback).reindex(master_index)

        # One dense row per scheduled rebalance date, ALWAYS carrying a real
        # number for every universe symbol (0.0 where not held). The dense
        # frame is scattered onto `master_index` only at the end, so
        # non-rebalance rows stay NaN -- no ffill anywhere.
        rows = pd.DataFrame(0.0, index=rebalance_dates, columns=symbols, dtype=float)

        for date in rebalance_dates:
            beta_members, alpha_members, both_defaulted = self._resolve_members_for_date(
                beta_spec, alpha_spec, beta_scheduled, alpha_scheduled,
                risky_universe, date, universe,
            )
            overlap = beta_members & alpha_members
            # The documented "no sleeve configuration at all" default puts every
            # symbol in both sleeves on purpose; any OTHER overlap is a config error.
            if overlap and not both_defaulted:
                raise ValueError(
                    "SleeveAllocator: a symbol cannot sit in both the beta and the alpha "
                    f"sleeve at the same rebalance date; {sorted(overlap)} claimed on {date}."
                )

            row = pd.Series(0.0, index=symbols, dtype=float)
            for members, budget, is_beta in (
                (beta_members, beta_budget, True),
                (alpha_members, alpha_budget, False),
            ):
                sleeve = self._sleeve_weights(
                    members, date, universe, budget, within, vols,
                )
                if is_beta and crisis_flags.get(date, False):
                    sleeve = {s: w * crisis_scale for s, w in sleeve.items()}
                for sym, weight in sleeve.items():
                    row[sym] = weight
            rows.loc[date] = row.to_numpy(dtype=float)

        # Explicit cash leg (and belt-and-braces cap) via the shared helper.
        rows = _cap_and_deroute_to_cash(
            rows, symbols, cash_ticker if cash_ticker is not None else _NO_CASH_PROXY, cap=1.0,
        )

        # Prune micro-weights using the existing 1e-4 negligible-weight floor,
        # writing an explicit 0.0 (never NaN, never a sub-threshold dust leg).
        rows = rows.where(rows.abs() >= _NEGLIGIBLE_WEIGHT, 0.0)

        weights_df.loc[rebalance_dates, symbols] = rows.to_numpy(dtype=float)
        return weights_df

    def explain_weights(self, params: dict) -> str:
        params = params or {}
        beta = float(params.get("sleeve_beta_budget", _DEFAULT_BETA_BUDGET))
        alpha = float(params.get("sleeve_alpha_budget", _DEFAULT_ALPHA_BUDGET))
        cash = float(params.get("sleeve_cash_budget", _DEFAULT_CASH_BUDGET))
        within = str(params.get("sleeve_within_weighting", "equal")).strip().lower()
        crisis_scale = float(params.get("sleeve_crisis_scale", _DEFAULT_CRISIS_SCALE))
        freq = params.get("rebalance_freq_days", 21)
        beta_tickers = params.get("sleeve_beta_tickers") or "(universe)"
        alpha_tickers = params.get("sleeve_alpha_tickers") or "(universe)"
        return (
            f"Core-Satellite Sleeve Allocator: rebalances every {freq} trading days. "
            f"Reasoning: capital is partitioned into fixed budgets -- beta/core {beta:.0%}, "
            f"alpha/satellite {alpha:.0%}, cash reserve {cash:.0%} -- and each sleeve's "
            f"budget is sized "
            + ("equally across its members." if within == "equal" else
               "by inverse realized volatility across its members.")
            + f" Beta sleeve: {beta_tickers}; alpha sleeve: {alpha_tickers}; symbols in neither "
            f"sleeve are excluded (explicit 0.0). A caller-supplied crisis flag multiplies ONLY "
            f"the beta sleeve's weights by {crisis_scale} (the alpha sleeve is untouched). "
            f"The cash budget plus any un-invested sleeve remainder is the residual "
            f"max(0, 1 - sum(risky)), written into the designated cash ticker when one exists."
        )

    def warmup_bars(self, params: dict) -> int:
        params = params or {}
        within = str(params.get("sleeve_within_weighting", "equal")).strip().lower()
        if within == "inverse_vol":
            return int(params.get("sleeve_vol_lookback", _DEFAULT_VOL_LOOKBACK))
        return 0

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_members_for_date(
        beta_spec: SleeveSpec,
        alpha_spec: SleeveSpec,
        beta_scheduled: bool,
        alpha_scheduled: bool,
        risky_universe: List[str],
        date: pd.Timestamp,
        universe: Dict[str, pd.DataFrame],
    ) -> Tuple[Set[str], Set[str], bool]:
        """Membership of both sleeves on one rebalance date, including the
        documented single-sleeve / no-sleeve fallbacks (see module docstring).
        The third element is True only for the "no sleeve configuration at all"
        fallback, where deliberate duplicate membership is not an error."""
        beta_at = _membership_at(beta_spec, date)
        alpha_at = _membership_at(alpha_spec, date)
        beta_members: Set[str] = set(beta_at or ())
        alpha_members: Set[str] = set(alpha_at or ())
        both_defaulted = False

        if not beta_members and not alpha_members and not (beta_scheduled or alpha_scheduled):
            # No sleeve configuration at all: every symbol participates in both
            # sleeves at their own budgets -- a non-degenerate default rather
            # than a cash-only portfolio.
            beta_members = set(risky_universe)
            alpha_members = set(risky_universe)
            both_defaulted = True
        elif not beta_members and not beta_scheduled and alpha_members:
            beta_members = set(risky_universe) - alpha_members
        elif not alpha_members and not alpha_scheduled and beta_members:
            alpha_members = set(risky_universe) - beta_members

        return beta_members, alpha_members, both_defaulted

    @staticmethod
    def _sleeve_weights(
        members: Set[str],
        date: pd.Timestamp,
        universe: Dict[str, pd.DataFrame],
        budget: float,
        within: str,
        vols: Optional[pd.DataFrame],
    ) -> Dict[str, float]:
        """Weights WITHIN one sleeve, summing to `budget` across its active
        members. A member without a usable price on `date` is inactive and gets
        no allocation (it is still written as explicit 0.0 by the caller)."""
        active = sorted(
            s for s in members
            if s in universe
            and not universe[s].empty
            and date in universe[s].index
            and pd.notna(universe[s].loc[date, "Close"])
        )
        if not active or budget <= 0.0:
            return {}

        if within == "equal":
            weight = budget / len(active)
            return {s: weight for s in active}

        row = (
            vols.loc[date, active].astype(float)
            if vols is not None and date in vols.index
            else pd.Series(np.nan, index=active, dtype=float)
        )
        weighted = _inverse_vol_weights(row, scale=budget, on_invalid="zero").fillna(0.0)
        return {s: float(max(0.0, weighted.get(s, 0.0))) for s in active}


# Self-registration for the reversed import order: if this module was imported
# first, `common/allocation_templates.py`'s own bottom-of-module import saw this
# module mid-initialization and could not pick the class up (see that guard's
# comment). Appending here -- after the class exists -- makes the registry
# complete regardless of which module the process imported first.
def _register_in_static_registry() -> None:
    from common import allocation_templates as _templates

    if SleeveAllocator not in _templates.ALLOCATION_TEMPLATES:
        _templates.ALLOCATION_TEMPLATES.append(SleeveAllocator)


_register_in_static_registry()
