"""Specific-alpha residual returns -- **SIMPLIFIED** -- plus a report-only
health label (Task `AT2`, `docs/development_plan.md:408`).

WHAT THIS MODULE COMPUTES
=========================
Two pure stages (no IO, no network, no global state):

* **Stage M (market leg, time series).** Per symbol, a rolling univariate
  market-model residual return ``r_m[t] = r[t] - beta[t-1] * bench[t]``, with
  ``beta`` estimated over a trailing ``beta_lookback`` window. This is the
  "market residual" half of the plan row.
* **Stage S (style leg, cross-section).** On each date, Stage-M residuals are
  regressed **cross-sectionally** on the caller's lagged style exposures plus an
  intercept (plain unweighted OLS). The leftover ``r_m - X b`` is this module's
  specific-alpha residual. With ``factor_exposures=None`` Stage S is
  intercept-only -- a pure cross-sectional centering of the market residual, and
  that is *not* a factor decomposition (see the warning below).

SIMPLIFIED -- WHAT IS *NOT* MODELLED (read before quoting any number)
=====================================================================
This repo has **no commercial Barra / Axioma / Northfield factor-return table**
(see `docs/development_plan_archive.md` §4.51.2 `C2` / §4.51.3: the data source
does not exist; `C3` records "§4.44:1995 已明定不可顶替"). The best offline
approximation is the two-stage construction above. It is **SIMPLIFIED**, and it
differs from a full Barra specific-return decomposition in every one of these
ways -- these are real differences, not cosmetic caveats:

1. **No industry factor block.** Barra regresses on country + industry dummy
   factors from a point-in-time industry mapping, so industry-common return is
   removed *before* the specific return is formed. This repo only has coarse
   sector labels (`common/sector_metadata.py` `load_sector_map` /
   `resolve_sector`), not industry factor returns, so Stage S cannot strip
   industry co-movement at all. Whatever is sector-common stays inside the
   "specific" residual here.
2. **No complete style block.** Barra CNE5/EUE4 estimate size, beta, momentum,
   residual volatility, nonlinear size, book-to-price, liquidity, earnings
   yield, growth and leverage. Offline this module can only approximate two of
   them (`style_exposures_from_close`: 20d/5d price momentum and 63d residual
   volatility). **SIZE / VALUE / LIQUIDITY / GROWTH / LEVERAGE / EARNINGS-YIELD
   are absent** -- no point-in-time fundamentals and no free-float share data
   exist in the repo, so building them would mean fabricating them.
3. **No risk-model estimation machinery.** Barra estimates factor returns in a
   weighted (sqrt-cap) cross-sectional regression with industry constraints that
   sum to zero, plus Bayesian factor-return smoothing and a factor covariance /
   specific-risk model. Here each date is an unweighted, unconstrained OLS with
   no shrinkage, no factor covariance matrix, no Newey-West correction.
4. **No nonlinear / descriptor refinement.** No size^3, no volatility-regime
   interaction, no descriptor orthogonalization (Barra orthogonalizes styles
   against each other and against industries); collinear exposures here are
   merely dropped when their cross-sectional variance is zero.
5. **No estimation-universe discipline.** A real risk model winsorizes
   descriptors and excludes recent IPOs, ST/suspended names and illiquid stocks.
   This module uses the caller's panel exactly as given.
6. **No attribution beyond the residual.** Barra can attribute the specific
   return back to omitted-factor buckets; here the residual is simply "whatever
   the included factors did not explain", and the omitted factors in item 1-4
   are inside it.
7. **No guarantee of "pure alpha".** Because the model is deliberately
   under-specified, a positive specific-alpha residual may be a size, value or
   industry bet in disguise. It is a *diagnostic leftover*, not a clean alpha
   estimate, and it is not comparable in level to a commercial Barra specific
   return.

THE UNIVARIATE MARKET RESIDUAL IS NOT A MULTI-FACTOR DECOMPOSITION
=================================================================
The single-variable market residual (``specific_alpha_residual(..., factor_exposures=None)``
or ``market_model_residual_returns``) is **单变量市场残差 ≠ 多因子特质收益分解** --
it is *not* a multi-factor idiosyncratic return decomposition, and it is
**不等于 Barra/多因子分解**. Removing one benchmark exposure only strips the
market leg; it leaves every style and industry bet untouched. Plan §4.44:1995
already ruled that the univariate residual cannot be substituted for a
multi-factor specific return, and this module keeps the two call shapes
distinguishable instead of silently relabeling one as the other.

RELATIONSHIP TO `common/daily_factors.py:192` `ivol_residual` (not interchangeable)
==================================================================================
`ivol_residual` is the rolling **univariate** market-model residual
**volatility**: annualized std of ``close.pct_change()`` residuals against a
benchmark (default: the panel's own equal-weight mean), built from the in-sample
window beta algebra at `common/daily_factors.py:241-250`.
This module **reuses that input chain** (a returns panel / benchmark; the same
cross-sectional equal-weight benchmark default) and the same rolling
covariance/variance algebra, but the outputs are different objects:

* `ivol_residual` returns a **non-negative, annualized volatility** level.
* This module returns a **signed daily return** series; then Stage S regresses
  it cross-sectionally and returns a second-stage leftover.
* The betas differ on purpose: Stage M uses ``beta.shift(1)`` so the residual at
  ``t`` is not fitted with the same-day return (an in-sample fit would
  mechanically shrink the very residual it is meant to isolate). `ivol_residual`
  keeps the in-sample convention, which is harmless for a std statistic.

Consequently the two are **not interchangeable and not reconstructible from each
other**: you cannot recover Stage-S residual returns by taking the sign of, or
rescaling, `ivol_residual`, and `ivol_residual` cannot be re-derived from this
module's output (the lagged beta plus the cross-sectional demeaning break the
identity). Use `ivol_residual` when you need a volatility *level* to rank on;
use this module when you need a signed residual *return*.

NaN / DEGRADATION CONTRACT (deterministic, never zero-filled)
=============================================================
A value that cannot be estimated is ``NaN`` and **never** ``0.0``: a fabricated
zero is a real number to a later cross-sectional rank or mean and would silently
pollute it (the same argument as `common/daily_factors.py`'s NaN contract).
Specifically:

* fewer than two symbols, or fewer than two dates -> all-NaN panel;
* a date with fewer than ``min_obs`` symbols having both a market residual and
  every exposure -> that row is all NaN (no partial-basis regression);
* constant cross-section (regression degenerates to a constant) -> NaN row
  instead of an all-zero "residual";
* all-NaN exposure column or all-NaN benchmark -> NaN rows, never back-filled;
* a singular / non-finite least-squares solve -> NaN row, never an exception.

ADVISORY-ONLY CONTRACT (report label, not a gate)
=================================================
The health criteria in `docs/量化策略性能归因与生命周期自愈体系.md` §二.3
("特质残差收益（Specific Alpha）健康度标准": specific-return share >= 60%,
style exposures in [-0.1, 0.1], specific drawdown ratio) are used here **as
report labels only**. `specific_alpha_health` returns a plain dict of
descriptive labels and never raises, never blocks a downstream caller, never
changes a weight, a target position, an order, or a pipeline exit code.
Nothing in this module may be wired into a gate; the docstring says so because
the precedent in this repo is that a "health" number eventually gets used to
block trades unless it is fenced off explicitly.

Scope note, so the labels are not over-read: only the *coverage* and *dispersion*
labels are computed here. The §二.3 "specific-return share >= 60%" test needs the
strategy's own portfolio vs. benchmark return series, and the "style exposure in
[-0.1, 0.1]" test needs the full style block -- neither exists at this layer, so
neither is reproduced (or approximated) here. The two thresholds below are this
module's own descriptive cut-offs, not the document's numbers.

ACADEMIC GROUNDING
==================
Specific (idiosyncratic) return as the residual of a factor model: Ross (1976,
APT); Fama & French (1993, three-factor model); Blitz, Huij & Martens (2011),
"Residual momentum", JEF -- residual *returns* (not raw returns) carry a
distinct cross-sectional signal, which is exactly the object built here (and the
reason a residual-return module is not a re-parameterization of IVOL).
Barra methodology reference: Grinold & Kahn (2000), "Active Portfolio
Management", ch. 3 -- cited for what a complete factor model contains, i.e. the
gap enumerated above.
"""

from __future__ import annotations

from typing import Mapping, Optional

import numpy as np
import pandas as pd

from common.daily_factors import (
    ANNUALIZATION_DAYS,
    DEFAULT_MOM_LOOKBACK,
    DEFAULT_MOM_SKIP,
    ivol_residual,
    momentum_1m_1w,
)

__all__ = [
    "DEFAULT_BETA_LAG",
    "DEFAULT_BETA_LOOKBACK",
    "DEFAULT_COVERAGE_LABEL_THRESHOLD",
    "DEFAULT_DISPERSION_LABEL_THRESHOLD",
    "DEFAULT_EXPOSURE_LAG",
    "DEFAULT_MIN_OBS",
    "DEFAULT_MIN_LABEL_DATES",
    "DEFAULT_VOL_LOOKBACK",
    "market_model_residual_returns",
    "specific_alpha_health",
    "specific_alpha_residual",
    "style_exposures_from_close",
]

# Rolling window for the market-model beta. Mirrors `DEFAULT_IVOL_LOOKBACK`'s
# 63-day convention so a caller comparing the two modules is comparing like
# windows -- but NOT like statistics (see module docstring).
DEFAULT_BETA_LOOKBACK = 63
# The beta used at date t is estimated through t-1. See "RELATIONSHIP TO
# ivol_residual" above: this is a deliberate, load-bearing difference.
DEFAULT_BETA_LAG = 1
# Style exposures are lagged one day: the exposure measured with data through
# t-1 explains the return of t. Lag 0 would be a look-ahead.
DEFAULT_EXPOSURE_LAG = 1
# Minimum number of symbols that must have a market residual AND every exposure
# on a date before that date's cross-section is estimated at all.
DEFAULT_MIN_OBS = 30
DEFAULT_VOL_LOOKBACK = 63
# Report-label thresholds (advisory). Defaults are deliberately loose so the
# "OK" label means "the panel is usable for a report", not "the alpha is good".
DEFAULT_COVERAGE_LABEL_THRESHOLD = 0.60
DEFAULT_DISPERSION_LABEL_THRESHOLD = 1.20  # annualized residual vol
DEFAULT_MIN_LABEL_DATES = 20


# ---------------------------------------------------------------------------
# input validation / normalisation
# ---------------------------------------------------------------------------
def _as_panel(panel: pd.DataFrame, name: str) -> pd.DataFrame:
    """Validate a date x symbol panel and return a float copy.

    Same contract as `common.daily_factors._as_float_panel`: a Series is
    rejected rather than guessed at, because every routine here assumes the
    column axis is the symbol axis and silently promoting a Series would make
    "one symbol" look like "one date" on the wrong axis.
    """
    if not isinstance(panel, pd.DataFrame):
        raise TypeError(
            f"{name} must be a pandas DataFrame (date x symbol) panel, got {type(panel).__name__}"
        )
    return panel.astype(float)


def _benchmark_series(
    benchmark_returns: Optional[pd.Series],
    returns: pd.DataFrame,
) -> Optional[pd.Series]:
    """Return the market series, or ``None`` when the panel has no cross-section.

    ``None`` -> cross-sectional equal-weight mean, exactly the offline-friendly
    default `ivol_residual` uses (`common/daily_factors.py:229-232`), so the two
    modules are fed by the same market proxy. A single-symbol panel has no
    cross-section, and averaging it would produce a trivially-zero "market
    residual"; that path returns ``None`` (all-NaN output) instead.
    """
    if benchmark_returns is None:
        if returns.shape[1] < 2:
            return None
        return returns.mean(axis=1, skipna=True)
    if not isinstance(benchmark_returns, pd.Series):
        raise TypeError(
            "benchmark_returns must be a pandas Series indexed by date, got "
            f"{type(benchmark_returns).__name__}"
        )
    return benchmark_returns.reindex(returns.index).astype(float)


def _normalise_exposures(
    factor_exposures,
    template: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """Accept ``None`` / one DataFrame / a mapping of name -> DataFrame.

    Every frame is reindexed onto ``template``'s exact axes so a caller's
    column-order mismatch cannot regress symbol A's return on symbol B's
    exposure (the same reason `common/daily_factors.vwap` reindexes volume).
    Missing entries become NaN -- which the per-date completeness check then
    turns into an all-NaN row, never into an implicit 0.
    """
    if factor_exposures is None:
        return {}
    if isinstance(factor_exposures, Mapping):
        items = list(factor_exposures.items())
    elif isinstance(factor_exposures, pd.DataFrame):
        items = [("style_0", factor_exposures)]
    else:
        raise TypeError(
            "factor_exposures must be None, a pandas DataFrame, or a mapping of "
            f"name -> DataFrame, got {type(factor_exposures).__name__}"
        )

    normalised: dict[str, pd.DataFrame] = {}
    for name, frame in items:
        if not isinstance(frame, pd.DataFrame):
            raise TypeError(
                f"factor exposure {name!r} must be a pandas DataFrame (date x symbol), "
                f"got {type(frame).__name__}"
            )
        normalised[str(name)] = frame.reindex(
            index=template.index, columns=template.columns
        ).astype(float)
    return normalised


# ---------------------------------------------------------------------------
# stage M: univariate market-model residual RETURN
# ---------------------------------------------------------------------------
def market_model_residual_returns(
    returns_panel: pd.DataFrame,
    benchmark_returns: Optional[pd.Series] = None,
    *,
    beta_lookback: int = DEFAULT_BETA_LOOKBACK,
    beta_lag: int = DEFAULT_BETA_LAG,
) -> pd.DataFrame:
    """Signed univariate market-model residual returns (stage M only).

    ``r_m[t] = r[t] - beta[t-beta_lag] * bench[t]``, where ``beta`` is the
    trailing ``beta_lookback`` OLS slope of ``r`` on ``bench``.

    **This is a single-variable market residual, NOT a multi-factor
    idiosyncratic return.** It removes exactly one exposure (the market); every
    style and industry bet survives in the output. Plan §4.44:1995 already ruled
    that this object cannot substitute for a multi-factor specific return.
    Downstream code that needs the simplified multi-factor leftover must call
    `specific_alpha_residual` with actual exposures, not this function.

    The rolling covariance/variance algebra is the same one
    `common/daily_factors.ivol_residual` uses (`:241-250`); the statistic is a
    different object -- that module returns annualized residual **volatility**
    (non-negative, unit-free per year), this one returns a **signed daily
    return**. See the module docstring for why they are not interchangeable.

    ``benchmark_returns=None`` -> panel's cross-sectional equal-weight mean, and
    a panel with fewer than two symbols returns an all-NaN frame (no
    "residual vs itself" of zero).
    """
    lb = int(beta_lookback)
    if lb < 2:
        raise ValueError(f"beta_lookback must be >= 2, got {beta_lookback}")
    lag = int(beta_lag)
    if lag < 0:
        raise ValueError(f"beta_lag must be >= 0, got {beta_lag}")

    returns = _as_panel(returns_panel, "returns_panel")
    out = pd.DataFrame(np.nan, index=returns.index, columns=returns.columns)
    bench = _benchmark_series(benchmark_returns, returns)
    if bench is None:
        return out

    var_x = bench.rolling(lb, min_periods=lb).var(ddof=1)
    for symbol in returns.columns:
        y = returns[symbol]
        cov_xy = y.rolling(lb, min_periods=lb).cov(bench)
        # var_x.where(...) keeps a zero-variance benchmark from producing inf.
        beta = cov_xy / var_x.where(var_x != 0.0)
        if lag:
            beta = beta.shift(lag)
        # No intercept: the market-model alpha would be a *trailing average*
        # alpha and subtracting it would eat exactly the signal this module is
        # trying to expose. The estimate here is therefore "residual return",
        # not "residual return net of estimated alpha" (disclosed).
        out[symbol] = y - beta * bench
    return out


# ---------------------------------------------------------------------------
# default style exposures (reuses `common/daily_factors`)
# ---------------------------------------------------------------------------
def style_exposures_from_close(
    close: pd.DataFrame,
    benchmark_returns: Optional[pd.Series] = None,
    *,
    momentum_lookback: int = DEFAULT_MOM_LOOKBACK,
    momentum_skip: int = DEFAULT_MOM_SKIP,
    vol_lookback: int = DEFAULT_VOL_LOOKBACK,
) -> dict[str, pd.DataFrame]:
    """Build the two offline style exposures this repo can honestly approximate.

    Returns ``{"momentum_1m_1w": ..., "residual_volatility": ...}``, both
    date x symbol panels, by delegating to the existing daily-factor primitives
    (no new math is written here):

    * ``momentum_1m_1w`` -> `common.daily_factors.momentum_1m_1w` (20d return
      skipping the last 5d).
    * ``residual_volatility`` -> `common.daily_factors.ivol_residual` (63d
      annualized residual vol vs. the same benchmark).

    DISCLOSED APPROXIMATION -- these are *proxies for two Barra style
    dimensions*, not Barra descriptors:

    * Barra's momentum is 12-1 (long horizon, one-month skip); the repo's
      primitive is 20d/5d, i.e. short-horizon momentum contaminated by the
      short-term reversal factor. It is also used **raw**, not as a
      cross-sectional z-score -- `specific_alpha_residual` standardizes
      internally, per date.
    * Barra's residual-volatility descriptor is a *residual* vol from the full
      multi-factor model; here it is the univariate market-model residual vol
      from `ivol_residual`, so it inherits every omitted factor listed in the
      module docstring.
    * **SIZE, VALUE, LIQUIDITY, GROWTH, LEVERAGE and EARNINGS YIELD have no
      offline source and are deliberately not built** (fabricating them from
      proxies would recreate exactly the "名字像但对象不同" failure the plan
      warns about).

    Nothing here is annualized-adjusted or winsorized; the caller keeps the
    choice. Exposures are returned unlagged -- `specific_alpha_residual` applies
    its own ``exposure_lag`` so the lag is auditable in one place.
    """
    close = _as_panel(close, "close")
    return {
        "momentum_1m_1w": momentum_1m_1w(
            close, skip=momentum_skip, lookback=momentum_lookback
        ),
        "residual_volatility": ivol_residual(
            close, lookback=vol_lookback, benchmark_returns=benchmark_returns
        ),
    }


# ---------------------------------------------------------------------------
# stage S: cross-sectional style residual (the SIMPLIFIED specific-alpha return)
# ---------------------------------------------------------------------------
def specific_alpha_residual(
    returns_panel: pd.DataFrame,
    factor_exposures=None,
    benchmark_returns: Optional[pd.Series] = None,
    *,
    min_obs: int = DEFAULT_MIN_OBS,
    exposure_lag: int = DEFAULT_EXPOSURE_LAG,
    beta_lookback: int = DEFAULT_BETA_LOOKBACK,
    beta_lag: int = DEFAULT_BETA_LAG,
) -> pd.DataFrame:
    """**SIMPLIFIED** specific-alpha residual returns (stage M -> stage S).

    ``r_specific[t, i] = r_m[t, i] - (a[t] + sum_k b_k[t] * X_k[t-lag, i])``

    where ``r_m`` is `market_model_residual_returns` (stage M) and the
    coefficients come from one **cross-sectional OLS per date** of ``r_m`` on
    the lagged style exposures (stage S). The exposures are standardized
    cross-sectionally (z-scores over the same valid symbols) before the fit, so
    tabular scale does not decide the coefficient magnitude.

    READ THE MODULE DOCSTRING. This is an approximation of a Barra specific
    return, **not** one: there is no industry block, only two of Barra's ten
    style descriptors exist offline, the regression is unweighted/unconstrained
    and there is no risk-model estimation machinery. Missing industry, size,
    value and liquidity factors therefore remain inside the "specific" residual.

    ``factor_exposures=None`` runs stage S with the intercept only, i.e. the
    cross-sectionally demeaned market residual. That is a **single-variable
    market residual** and is explicitly NOT a multi-factor idiosyncratic return
    decomposition (§4.44:1995). Pass exposures (for example
    `style_exposures_from_close(close)`) to get the simplified multi-factor
    leftover. The two call shapes are deliberately distinguishable by output, so
    nobody can "upgrade" the univariate number by relabelling it.

    Degradation (never an exception, never a zero):
      * < 2 symbols or < 2 dates -> all-NaN panel;
      * a date needs at least ``max(min_obs, n_exposures + 2)`` symbols with a
        finite market residual *and* every exposure -> else that whole row is
        NaN (a partial-basis regression would silently change the model);
      * zero-variance exposure columns are dropped per date (they carry no
        cross-sectional information), never replaced by zeros;
      * a constant cross-section (all residuals equal) is a degenerate
        regression -> NaN row, not an all-zero "residual";
      * a singular/failed ``lstsq`` or non-finite output -> NaN row.

    Advisory only: the returned panel must not be used to gate, block, or
    reweight anything.
    """
    min_n = int(min_obs)
    if min_n < 3:
        # A cross-section of two symbols with an intercept has zero residual
        # degrees of freedom; that is a programming error in the caller, not a
        # data-degradation case, so it is rejected loudly (consistent with
        # `daily_factors.ivol_residual`'s lookback validation).
        raise ValueError(f"min_obs must be >= 3, got {min_obs}")
    lag = int(exposure_lag)
    if lag < 0:
        raise ValueError(f"exposure_lag must be >= 0, got {exposure_lag}")

    returns = _as_panel(returns_panel, "returns_panel")
    n_rows, n_cols = returns.shape
    empty = pd.DataFrame(np.nan, index=returns.index, columns=returns.columns)
    if n_rows < 2 or n_cols < 2:
        return empty

    bench = _benchmark_series(benchmark_returns, returns)
    if bench is None:
        return empty

    market = market_model_residual_returns(
        returns, bench, beta_lookback=beta_lookback, beta_lag=beta_lag
    )
    exposures = _normalise_exposures(factor_exposures, returns)
    exposure_arrays = [(name, frame.to_numpy(dtype=float)) for name, frame in exposures.items()]
    y_all = market.to_numpy(dtype=float)

    # Needed for a residual degree of freedom: intercept + one free symbol.
    min_required = max(min_n, len(exposure_arrays) + 2)

    residuals = np.full((n_rows, n_cols), np.nan, dtype=float)
    for t in range(n_rows):
        if t - lag < 0:
            continue
        y = y_all[t]
        valid = np.isfinite(y)
        columns = []
        for _name, matrix in exposure_arrays:
            x = matrix[t - lag]
            valid = valid & np.isfinite(x)
            columns.append(x)

        n_valid = int(valid.sum())
        if n_valid < min_required:
            continue

        y_valid = y[valid]
        # Constant cross-section => the regression explains nothing and its
        # residual is identically zero. Emitting 0.0 here would masquerade as a
        # real "no specific alpha" observation, so the row is NaN instead.
        if not np.isfinite(y_valid).all() or np.std(y_valid) == 0.0:
            continue

        standardised = []
        for x in columns:
            x_valid = x[valid]
            sd = float(np.std(x_valid))
            if not np.isfinite(sd) or sd <= 0.0:
                continue  # no cross-sectional variation -> not a regressor
            standardised.append((x_valid - np.mean(x_valid)) / sd)

        if standardised:
            design = np.column_stack([np.ones(n_valid)] + standardised)
        else:
            design = np.ones((n_valid, 1))

        if n_valid <= design.shape[1]:
            continue

        try:
            coef, *_ = np.linalg.lstsq(design, y_valid, rcond=None)
        except np.linalg.LinAlgError:
            continue

        resid = y_valid - design @ coef
        if not np.isfinite(resid).all():
            continue
        residuals[t, valid] = resid

    return pd.DataFrame(residuals, index=returns.index, columns=returns.columns)


# ---------------------------------------------------------------------------
# report-only health label
# ---------------------------------------------------------------------------
def specific_alpha_health(
    residuals: pd.DataFrame,
    *,
    coverage_label_threshold: float = DEFAULT_COVERAGE_LABEL_THRESHOLD,
    dispersion_label_threshold: float = DEFAULT_DISPERSION_LABEL_THRESHOLD,
    min_label_dates: int = DEFAULT_MIN_LABEL_DATES,
    annualization: int = ANNUALIZATION_DAYS,
) -> dict:
    """Descriptive health label for a specific-alpha residual panel.

    **Report-only, advisory, never a gate.** The thresholds exist to *label* a
    report: the criteria in `docs/量化策略性能归因与生命周期自愈体系.md` §二.3
    ("Specific Alpha 健康度标准") are report labels there and stay report labels
    here -- they are not admission gates anywhere in this repo. This function
    never raises, never returns a boolean "is healthy", never blocks a caller and
    never feeds a weight, an order or an exit code. Its return value is a plain
    dict; the only thing a caller may do with it is print it.

    Keys (always present, also on degenerate input)::

        simplified                  True -- the model is the simplified one above
        advisory                    True -- labels must not be used as gates
        status                      one of OK / LOW_COVERAGE /
                                    INSUFFICIENT_HISTORY / NO_RESIDUALS /
                                    INVALID_INPUT
        labels                      list of descriptive strings (status included)
        n_dates, n_symbols          panel shape
        n_observations              finite residual cells
        n_dates_with_residuals      rows holding at least one finite value
        coverage                    finite cells / all cells (0.0 if empty)
        mean_residual               mean over finite cells (should be ~0 because
                                    stage S fits an intercept; a visible
                                    non-zero mean means the panel is a raw
                                    market residual, not a stage-S one)
        residual_vol_daily          cross-sectional pooled daily std (ddof=1)
        residual_vol_annualized     residual_vol_daily * sqrt(annualization)
        thresholds                  the label thresholds actually applied
        notes                       tuple of advisory notes

    ``status`` is purely descriptive: ``LOW_COVERAGE`` means "the panel is sparse
    enough that the report is weak", not "reject the strategy".
    """
    thresholds = {
        "coverage_label_threshold": coverage_label_threshold,
        "dispersion_label_threshold": dispersion_label_threshold,
        "min_label_dates": min_label_dates,
        "annualization": annualization,
    }
    notes = (
        "SIMPLIFIED factor model: no industry block, two style proxies, no risk-model machinery.",
        "Labels are advisory report text; never gate, block, reweight or resize on them.",
    )

    def _invalid(detail: str) -> dict:
        return {
            "simplified": True,
            "advisory": True,
            "status": "INVALID_INPUT",
            "labels": ["SIMPLIFIED_FACTOR_MODEL", "INVALID_INPUT"],
            "n_dates": 0,
            "n_symbols": 0,
            "n_observations": 0,
            "n_dates_with_residuals": 0,
            "coverage": 0.0,
            "mean_residual": float("nan"),
            "residual_vol_daily": float("nan"),
            "residual_vol_annualized": float("nan"),
            "thresholds": thresholds,
            "notes": notes + (detail,),
        }

    try:
        panel = _as_panel(residuals, "residuals")
        values = panel.to_numpy(dtype=float)
        finite = np.isfinite(values)
        n_dates, n_symbols = int(panel.shape[0]), int(panel.shape[1])
        n_obs = int(finite.sum())
        n_dates_with = int(finite.any(axis=1).sum()) if n_symbols else 0
        coverage = float(n_obs / values.size) if values.size else 0.0

        if n_obs:
            finite_values = values[finite]
            mean_residual = float(np.mean(finite_values))
            vol_daily = float(np.std(finite_values, ddof=1)) if n_obs > 1 else float("nan")
        else:
            mean_residual = float("nan")
            vol_daily = float("nan")
        vol_annual = (
            vol_daily * float(np.sqrt(float(annualization))) if np.isfinite(vol_daily) else float("nan")
        )

        labels = ["SIMPLIFIED_FACTOR_MODEL"]
        if n_obs == 0:
            status = "NO_RESIDUALS"
        elif n_dates_with < int(min_label_dates):
            status = "INSUFFICIENT_HISTORY"
        elif coverage < float(coverage_label_threshold):
            status = "LOW_COVERAGE"
        else:
            status = "OK"

        if np.isfinite(vol_annual) and vol_annual > float(dispersion_label_threshold):
            labels.append("HIGH_DISPERSION")
        if np.isfinite(mean_residual) and abs(mean_residual) > 1e-6:
            # Not an error: a raw market-residual panel is expected to have a
            # non-zero pooled mean. The label just makes that visible.
            labels.append("NONZERO_POOLED_MEAN")
        labels.append(status)

        return {
            "simplified": True,
            "advisory": True,
            "status": status,
            "labels": labels,
            "n_dates": n_dates,
            "n_symbols": n_symbols,
            "n_observations": n_obs,
            "n_dates_with_residuals": n_dates_with,
            "coverage": coverage,
            "mean_residual": mean_residual,
            "residual_vol_daily": vol_daily,
            "residual_vol_annualized": vol_annual,
            "thresholds": thresholds,
            "notes": notes,
        }
    except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
        # A malformed panel or a non-comparable threshold type must not take a
        # reporting path down. Degrade to an explicit status instead.
        return _invalid(f"degraded: {type(exc).__name__}: {exc}")
