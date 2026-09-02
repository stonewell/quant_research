"""BnnForecastStrategy: the backtester-facing counterpart to this project's
screening report (`run_bnn_forecaster.py`) -- an `AllocationTemplate` so
`backtester/run_backtest.py` can run a real backtest using AutoBNN's own
probabilistic forecast as the entry/exit signal.

KEY EFFICIENCY INSIGHT: AutoBNN's `fit()` is expensive (MAP default: 1000
iterations per symbol, see `ForecasterConfig`) but `predict_quantiles()` is
cheap once fit. Unlike `fundamental_screener`'s strategy (where the signal
itself is a fetched constant), here the model is fit ONCE per symbol and then
queried at every date needed -- `fscreen.forecasting.fit_forecast` already
returns a full-history `forecast_return`/`ci_width` series from a single
training pass, so this strategy produces a genuinely time-varying daily
signal without per-fold refitting. See that module's docstring for the
disclosed look-ahead trade-off this implies.

Structured like `research_strategy.rs.strategy`'s per-symbol stateful timing
strategies (`RSIMeanReversionStrategy`, `TurtleBreakoutStrategy`) and this
workspace's other two "beat the benchmark" siblings
(`CompounderMarginOfSafetyStrategy`, `FundamentalMarginOfSafetyStrategy`) --
a genuine buy-high/sell-low hysteresis needs per-symbol position state.
`_fill_out_columns`/`_sparse_from_daily` were duplicated here in three
projects independently before being centralized into
`common.allocation_templates` -- imported from there now, not redefined.
"""

from dataclasses import fields, replace
from typing import Dict

import numpy as np
import pandas as pd

from common.allocation_templates import AllocationTemplate, _fill_out_columns, _sparse_from_daily

from .config import ForecasterConfig
from .forecasting import fit_forecast

_FORECASTER_CONFIG_FIELDS = {f.name for f in fields(ForecasterConfig)}


class BnnForecastStrategy(AllocationTemplate):
    """Real probabilistic-forecast sibling of
    `research_strategy.rs.strategy.CompounderMarginOfSafetyStrategy` (price
    proxy) and `fundamental_screener.fscreen.strategy.FundamentalMarginOfSafetyStrategy`
    (real fundamentals) -- see this module's own docstring for the shared
    "beat the benchmark" mechanism and the fit-once/predict-many design.
    """

    def __init__(self, config: ForecasterConfig = None):
        self.config = config or ForecasterConfig()
        # symbol -> fitted forecast_df, keyed by (symbol, cfg fields that
        # change what a fit means) -- see generate_weights' own comment for
        # why this matters (backtester.run_walkforward calls
        # generate_weights() once per fold on the SAME instance, and a BNN
        # fit is far too expensive to redo per fold for what's meant to be
        # one snapshot per run).
        self._forecast_cache: Dict[tuple, pd.DataFrame] = {}
        super().__init__(name="bnn_forecast", param_grid={})

    def _cache_key(self, symbol: str, cfg: ForecasterConfig) -> tuple:
        return (symbol, cfg.lookback_days, cfg.horizon_days, cfg.estimator, cfg.width, cfg.num_iters,
                cfg.num_particles, cfg.seed)

    def _cached_forecast(self, symbol: str, close: pd.Series, cfg: ForecasterConfig) -> pd.DataFrame:
        key = self._cache_key(symbol, cfg)
        if key not in self._forecast_cache:
            self._forecast_cache[key] = fit_forecast(close, cfg)
        return self._forecast_cache[key]

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        p = params or {}
        # Merge params onto self.config so a backtester-reconstructed
        # instance (zero-arg constructed by _get_template, see
        # backtester/run_backtest.py's bnn_spec branch) still honors the
        # actual screener-tuned thresholds saved in strategy.json's params --
        # self.config alone would just be ForecasterConfig()'s defaults in
        # that path. Same fix already applied to FundamentalMarginOfSafetyStrategy.
        cfg = replace(self.config, **{k: v for k, v in p.items() if k in _FORECASTER_CONFIG_FIELDS})
        cash_proxy = p.get("cash_proxy", "BIL")

        symbols = list(universe.keys())
        candidate_symbols = [s for s in cfg.universe if s in universe and s != cfg.benchmark_symbol]
        if not candidate_symbols or cfg.benchmark_symbol not in universe:
            return pd.DataFrame()

        master_index = universe[candidate_symbols[0]].index
        n_bars = len(master_index)

        # The benchmark's own BNN forecast -- same "beat the benchmark"
        # comparator convention as the sibling strategies, but here it's
        # ALSO a genuine time-varying forecast rather than a trailing
        # realized return, since AutoBNN applies equally well to the
        # benchmark's own price history.
        benchmark_close = universe[cfg.benchmark_symbol]["Close"]
        benchmark_forecast = self._cached_forecast(cfg.benchmark_symbol, benchmark_close, cfg)
        benchmark_return_series = benchmark_forecast["forecast_return"]

        raw_weights = {}
        for sym in candidate_symbols:
            close = universe[sym]["Close"]
            forecast_df = self._cached_forecast(sym, close, cfg)

            confident_series = forecast_df["ci_width"] <= cfg.max_ci_width
            entry_signal = (confident_series & (forecast_df["forecast_return"] >= cfg.required_return)).fillna(False)
            exit_signal = (
                (~confident_series) | (forecast_df["forecast_return"] < benchmark_return_series)
            ).fillna(True)

            entry_arr = entry_signal.to_numpy()
            exit_arr = exit_signal.to_numpy()
            raw = np.zeros(n_bars)
            in_position = False
            for i in range(n_bars):
                if in_position:
                    if exit_arr[i]:
                        in_position = False
                        raw[i] = 0.0
                    else:
                        raw[i] = 1.0
                elif entry_arr[i] and not exit_arr[i]:
                    in_position = True
                    raw[i] = 1.0
            raw_weights[sym] = raw

        position_size_pct = 1.0 / len(candidate_symbols)
        daily = pd.DataFrame(raw_weights, index=master_index) * position_size_pct
        total_risky_raw = daily.sum(axis=1)
        scale = np.where(total_risky_raw > 1.0, 1.0 / total_risky_raw, 1.0)
        daily = daily.mul(scale, axis=0)

        if cash_proxy in symbols:
            daily[cash_proxy] = np.maximum(0.0, 1.0 - daily.sum(axis=1))

        daily = _fill_out_columns(daily, symbols)
        return _sparse_from_daily(daily)

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        required_return = p.get("required_return", cfg.required_return)
        max_ci_width = p.get("max_ci_width", cfg.max_ci_width)
        horizon_days = p.get("horizon_days", cfg.horizon_days)
        benchmark_symbol = p.get("benchmark_symbol", cfg.benchmark_symbol)
        return (
            f"BNN Forecast (AutoBNN compositional Bayesian Neural Network, see "
            f"https://research.google/blog/autobnn-probabilistic-time-series-forecasting-with-compositional-bayesian-neural-networks/): "
            f"holds a candidate only while its {horizon_days}-day-ahead median forecast return is >= "
            f"{required_return * 100:.0f}% AND the forecast's own 97.5/2.5 confidence-interval width is "
            f"<= {max_ci_width * 100:.0f}% (annualized) -- a genuine probabilistic forecast, not a proxy. "
            f"Exits once {benchmark_symbol}'s own BNN forecast catches up, or confidence degrades. "
            f"DISCLOSED LIMITATION: the model is fit ONCE per symbol on the trailing "
            f"{p.get('lookback_days', cfg.lookback_days)} bars and its predictions are read off across "
            f"the whole backtest window (a look-ahead trade-off; a true walk-forward refit is "
            f"prohibitively expensive given AutoBNN's own compute profile). See "
            f"research_strategy.rs.strategy.CompounderMarginOfSafetyStrategy and "
            f"fundamental_screener.fscreen.strategy.FundamentalMarginOfSafetyStrategy for this "
            f"workspace's other two 'beat the benchmark' strategies (price-proxy and real-fundamentals, "
            f"respectively)."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        return p.get("lookback_days", cfg.lookback_days)
