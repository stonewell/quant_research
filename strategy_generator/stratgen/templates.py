"""A small, fixed set of parameterized strategy templates -- deliberately
NOT a genetic-programming symbolic search over an open-ended function set.

Research grounding: Allen & Karjalainen's classic genetic-algorithm study
found even a careful, validated search over trading rules largely failed to
beat buy-and-hold net of costs, and the field's own documented mitigation for
data-snooping risk is to restrict the primitive/parameter set to a small
number of long-established, simple constructs. Each template here exposes
only 2 free parameters for search (see `param_grid`) -- everything else
(which template, and the ATR-based stop-loss multiple) is fixed by the
regime classification and template design, not searched, to keep the
effective number of trials small and auditable (see `metrics.deflated_sharpe_ratio`,
which needs to know that count).

Every template exposes the same interface: `signals(df, params)` returns a
DataFrame with `entry_signal`/`exit_signal` boolean columns, computed from
each bar's CLOSE (the backtester acts on them at the NEXT bar's open, to
avoid lookahead -- consistent with every other backtester in this workspace).

`TurnOfMonthTemplate`, `VolGatedMomentumTemplate`, and
`AbsoluteMomentumTemplate` below were added after deep-research passes
specifically looking for additional sub-3-month-holding templates with
sourced evidence of beating buy-and-hold on drawdown as well as return (see
../README.md for the full research grounding, confidence levels, disclosed
simplifications, and adversarial counter-evidence for each). None is wired
into `TEMPLATES_BY_REGIME`: that dict routes by a trending/mean-reverting/
random-walk three-way split. For `TurnOfMonth`/`VolGatedMomentum` that's
because their edge isn't conditioned on that axis; for `AbsoluteMomentum` the
edge IS a trending-regime one, but its per-asset significance is contested
(Zakamulin 2014; Huang et al. 2020) and the trending slot is already held by
`MomentumTemplate`, so rather than silently change routing it's offered as an
alternative trending-regime construct for direct use via `run_backtest(df,
template, params)` (and through the generator's ERS/trust gate), not the
automatic Hurst-regime router.
"""

from dataclasses import dataclass, field

import pandas as pd

from .indicators import realized_vol, roc, rsi, sma


@dataclass
class Template:
    name: str
    param_grid: dict
    stop_loss_atr_mult: float

    def signals(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        raise NotImplementedError


@dataclass
class MomentumTemplate(Template):
    name: str = "momentum"
    param_grid: dict = field(default_factory=lambda: {"fast_ma": [10, 20, 30], "slow_ma": [50, 100, 150]})
    stop_loss_atr_mult: float = 3.0

    def signals(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        fast = sma(df["Close"], params["fast_ma"])
        slow = sma(df["Close"], params["slow_ma"])
        state = fast > slow
        out = pd.DataFrame(index=df.index)
        out["entry_signal"] = state.fillna(False)
        out["exit_signal"] = (~state).fillna(False)
        return out


@dataclass
class MeanReversionTemplate(Template):
    name: str = "mean_reversion"
    param_grid: dict = field(default_factory=lambda: {"entry_threshold": [10, 20, 30], "exit_threshold": [60, 70, 80]})
    stop_loss_atr_mult: float = 2.0
    rsi_period: int = 2

    def signals(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        r = rsi(df["Close"], self.rsi_period)
        out = pd.DataFrame(index=df.index)
        out["entry_signal"] = (r < params["entry_threshold"]).fillna(False)
        out["exit_signal"] = (r > params["exit_threshold"]).fillna(False)
        return out


@dataclass
class TurnOfMonthTemplate(Template):
    """Turn-of-month calendar effect: buy near month-end, sell a few trading
    days into the next month. Multiply-corroborated across independent
    academic sources (Lakonishok & Smidt 1988; McConnell & Xu; Carchano &
    Tornero) covering 30+ country equity indexes -- not a single-source
    claim like most calendar-anomaly folklore. A cited illustrative backtest
    (1926-2005, unspecified single index) reported a 7.2% annualized return
    at only 6.9% volatility (Sharpe 1.04, max drawdown -20.79%) while
    invested only ~4 of ~20 trading days per month -- i.e. it is designed to
    capture most of equity drift while carrying market risk on a small
    fraction of the calendar, not by predicting direction. No accepted
    risk-based explanation exists, only unproven cash-flow/rebalancing
    hypotheses, and calendar effects are documented to weaken or drift to
    different days over time -- treat the exact day-count defaults as a
    starting point to re-tune, not a permanent edge.

    `entry_days_before_month_end`: entry_signal fires on the trading day(s)
    within this many trading days of month-end (1 = last trading day only).
    `exit_trading_day_of_month`: exit_signal fires once the new month's
    trading-day count exceeds this value. Both search this workspace's
    convention of entry/exit acted on at the NEXT bar's open, so a value of
    (1, 3) approximates "buy the last trading day, hold ~4 trading days."
    """

    name: str = "turn_of_month"
    param_grid: dict = field(default_factory=lambda: {
        "entry_days_before_month_end": [1, 2, 3], "exit_trading_day_of_month": [2, 3, 4],
    })
    stop_loss_atr_mult: float = 3.0  # loose: a tight stop would just add noise-driven exits to a scheduled, brief hold

    def signals(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        month_key = df.index.to_period("M")
        month_key_series = pd.Series(month_key, index=df.index)
        day_of_month_rank = month_key_series.groupby(month_key_series).cumcount() + 1
        days_in_month = month_key_series.groupby(month_key_series).transform("size")
        days_until_month_end = days_in_month - day_of_month_rank

        out = pd.DataFrame(index=df.index)
        out["entry_signal"] = days_until_month_end < params["entry_days_before_month_end"]
        out["exit_signal"] = day_of_month_rank > params["exit_trading_day_of_month"]
        return out


@dataclass
class VolGatedMomentumTemplate(Template):
    """Trend-following, de-risked out of the market when realized volatility
    spikes into its own extreme-high regime -- a binary simplification of
    "conditional volatility targeting" (Bongaerts, Kang & van Dijk 2020,
    Financial Analysts Journal, peer-reviewed): the paper's finding is that
    scaling EXPOSURE only during extreme vol states (not continuously) cuts
    max drawdown by ~6.6 percentage points on average across equity markets
    tested, and by far more (54.1% to 20.1%) for momentum-factor portfolios
    specifically -- while continuous (always-on) vol-scaling underperformed
    in 4 of 10 markets tested.

    Disclosed simplification: this workspace's backtester is single-position
    binary exposure (0%/100%), not continuous position sizing, so the
    overlay is implemented as a hard entry-block/forced-exit gate at the
    extreme-high-vol threshold rather than the paper's continuous scaling --
    it captures the drawdown-reducing mechanism (de-risk during vol spikes)
    but not the paper's more nuanced sizing. The paper's low-vol-state
    exposure increase has no long-only-unlevered analogue here and is not
    attempted. Documented failure mode carried over from the source: the
    benefit concentrates in equities and is weak/negative for other asset
    classes -- irrelevant here since this workspace is equities/ETFs only,
    but the source also notes vol-scaling can lag in fast V-shaped
    recoveries (de-risking right before the bounce), which applies directly.

    `vol_lookback`: realized-vol window (trading days). `vol_percentile`:
    the trailing-252-day percentile of that vol series above which the
    regime counts as "extreme" (fixed 252-day reference window, not
    searched, matching this template's philosophy of exposing only 2 free
    parameters). Trend filter is a fixed 100-day SMA, not searched.
    """

    name: str = "vol_gated_momentum"
    param_grid: dict = field(default_factory=lambda: {
        "vol_lookback": [10, 20, 30], "vol_percentile": [80, 90, 95],
    })
    stop_loss_atr_mult: float = 2.5
    trend_ma_period: int = 100
    vol_percentile_window: int = 252

    def signals(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        close = df["Close"]
        trend_ma = sma(close, self.trend_ma_period)
        trend_ok = (close > trend_ma).fillna(False)

        vol = realized_vol(close, params["vol_lookback"])
        vol_threshold = vol.rolling(self.vol_percentile_window, min_periods=self.vol_percentile_window).quantile(
            params["vol_percentile"] / 100.0
        )
        vol_extreme_high = (vol > vol_threshold).fillna(False)

        out = pd.DataFrame(index=df.index)
        out["entry_signal"] = trend_ok & ~vol_extreme_high
        out["exit_signal"] = (~trend_ok) | vol_extreme_high
        return out


@dataclass
class AbsoluteMomentumTemplate(Template):
    """Time-series (a.k.a. absolute) momentum: hold the instrument only while
    its OWN trailing return is positive, and step to cash when it isn't -- the
    single most-replicated tradable momentum construct, and the one in the
    momentum family with the strongest DRAWDOWN-reduction evidence (which is
    exactly the axis this project's follow-up template search cared about).

    This is a genuinely different signal from `MomentumTemplate`, not a
    reskin: `MomentumTemplate` is a fast/slow SMA CROSSOVER (a rule on two
    smoothed PRICE levels), whereas this is a rule on the SIGN of the
    instrument's own trailing RETURN -- the precise construct the academic
    time-series-momentum literature studies. The overlap (both are
    trend-following and long the same broad up-moves) is real and disclosed;
    the estimators differ and can disagree at turning points, which is why
    it's offered as a distinct option rather than folded into the crossover.

    Research grounding (adversarially verified, both directions):
    - **FOR (the core anomaly):** Moskowitz, Ooi & Pedersen (2012, *Journal of
      Financial Economics* 104(2):228-250) documented time-series momentum in
      every one of 58 liquid futures across equities, currencies, commodities
      and bonds: a 12-month-lookback / 1-month-hold rule was positive for all
      58 and significant for 52, with the diversified portfolio delivering an
      annualized Sharpe near 1.1 (1985-2009); returns persist ~1-12 months
      then partially reverse. This is the `lookback`/exit design below.
    - **FOR (the drawdown mechanism specifically):** Faber (2007, *Journal of
      Wealth Management*) tested a 10-month-SMA / absolute-momentum timing
      rule (economically the same "own trend is up -> hold, else cash" signal)
      across 20+ markets and a 5-asset-class allocation: it delivered
      equity-like returns with bond-like volatility, cut the max drawdown from
      ~46% (buy-and-hold) to single digits, and improved the risk-adjusted
      return / drawdown in >90% of the out-of-sample markets while invested
      only ~70% of the time. The de-risk-to-cash leg is what buys the
      drawdown reduction, and it's why the exit uses a (typically shorter)
      lookback than the entry -- cut losers faster than you commit to winners.
    - **AGAINST (why it's still only a directly-usable option, not trusted
      outright, and not auto-routed):** Zakamulin (2014, *Journal of Asset
      Management* 15(4)) showed the headline moving-average / time-series-
      momentum timing results are substantially overstated -- data-mining
      bias, ignored transaction costs, and extreme sensitivity to the lookback
      AND to the in-/out-of-sample split point; his robust out-of-sample tests
      found the "edge" is mostly RISK reduction confined to a few historical
      episodes, not persistent return outperformance. Huang, Li, Wang & Zhou
      (2020, *JFE* 135(3)) similarly found per-asset TSM weak once the pooled
      t-stat is bootstrap-corrected. Consistent with this, a long-horizon
      study finds long-only TSM beats buy-and-hold over the very long run but
      with <60% probability of doing so over any given 5-10-year window. So
      like `TurnOfMonth`/`VolGatedMomentum`, this template is exposed for
      direct use and put through the generator's ERS / trust gate rather than
      wired into `TEMPLATES_BY_REGIME` -- it is NOT trusted just for having a
      famous name.

    `entry_lookback`: trailing-return window (trading days) whose sign gates
    entry -- ~126/189/252 days spans the 6-12-month horizon the evidence is
    about. `exit_lookback`: a (typically shorter) trailing-return window whose
    turning negative forces the exit-to-cash; a shorter exit than entry is the
    asymmetry that produces the drawdown reduction (Faber). Both are ROC
    windows, keeping this to exactly 2 free search parameters like every other
    template here. Computed on each bar's CLOSE; acted on at the NEXT bar's
    open by the backtester, so there is no lookahead.
    """

    name: str = "absolute_momentum"
    param_grid: dict = field(default_factory=lambda: {
        "entry_lookback": [126, 189, 252], "exit_lookback": [21, 42, 63],
    })
    stop_loss_atr_mult: float = 3.0  # loose: the trailing-return sign flip IS the primary risk control, per the trend-following evidence

    def signals(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        close = df["Close"]
        entry_momentum = roc(close, params["entry_lookback"])
        exit_momentum = roc(close, params["exit_lookback"])
        out = pd.DataFrame(index=df.index)
        out["entry_signal"] = (entry_momentum > 0).fillna(False)
        out["exit_signal"] = (exit_momentum < 0).fillna(False)
        return out


@dataclass
class NoTradeTemplate(Template):
    name: str = "no_trade"
    param_grid: dict = field(default_factory=dict)
    stop_loss_atr_mult: float = 0.0

    def signals(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        out = pd.DataFrame(index=df.index)
        out["entry_signal"] = False
        out["exit_signal"] = False
        return out


TEMPLATES_BY_REGIME = {
    "trending": MomentumTemplate,
    "mean_reverting": MeanReversionTemplate,
    "random_walk_like": NoTradeTemplate,
}
