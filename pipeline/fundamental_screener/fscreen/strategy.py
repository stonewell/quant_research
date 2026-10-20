"""FundamentalMarginOfSafetyStrategy: the backtester-facing counterpart to
this project's screening report (`run_fundamental_screener.py`) -- an
`AllocationTemplate` so `backtester/run_backtest.py` can run a real
backtest against real symbols using the same buy/sell rules the report
applies (see `fscreen/rules.py` and `fscreen/valuation_models.py`).

Grounding: `docs/snowball_strategy.txt` conservative value-investing framework.
Features:
- Three-tier conservative industry terminal P/E classification and blacklist filtering.
- Dual valuation models: Model 1 (closed-form terminal PE discounting) and
  Model 2 (growth-dividend capitalization with singularity guard).
- Conservative Margin of Safety (MoS) discount and buy target price evaluation.
- Dual-trigger sell decision engine (fundamental quality failure or benchmark return edge decay).
- Strict sparse weights contract: target weights generated only on rebalance dates
  (`common.scheduling.get_rebalance_dates`), non-rebalance rows strictly NaN,
  explicit 0.0 written for de-risked/sold/excluded assets.
"""

from dataclasses import fields, replace
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from common.allocation_templates import AllocationTemplate
from common.scheduling import get_rebalance_dates

from .config import ScreenerConfig
from .fundamentals import fetch_fundamentals_frame
from .rules import _row_to_fundamental_metrics, expected_return, quality_ok
from .valuation_models import evaluate_sell_trigger, evaluate_snowball_valuation

_SCREENER_CONFIG_FIELDS = {f.name for f in fields(ScreenerConfig)}


class FundamentalMarginOfSafetyStrategy(AllocationTemplate):
    """Real-fundamentals sibling of
    `research_strategy.rs.strategy.CompounderMarginOfSafetyStrategy` -- see
    that class's own docstring for the shared grounding
    (`docs/snowball_strategy.txt`). This version applies the document's own
    Model 2 formula (`earnings_growth + dividend_yield`) and quality gate
    (ROE/dividend/leverage/growth) to REAL yfinance fundamentals instead of
    a price-only proxy -- see the module docstring above for the
    constant-signal limitation this implies.
    """

    def __init__(self, config: ScreenerConfig = None):
        self.config = config or ScreenerConfig()
        # Populated on first generate_weights() call and reused for the
        # lifetime of this instance -- see generate_weights' own docstring
        # note for why this matters: backtester.run_walkforward calls
        # generate_weights() once per fold on the SAME template instance,
        # and fundamentals are meant to be one constant per-run snapshot,
        # not independently re-fetched (and potentially inconsistent) once
        # per fold.
        self._fundamentals_cache: pd.DataFrame = None
        super().__init__(name="fundamental_margin_of_safety", param_grid={})

    def generate_weights(
        self, universe: Dict[str, pd.DataFrame], params: Optional[Dict[str, Any]] = None
    ) -> pd.DataFrame:
        p = params or {}
        # Merge params onto self.config so a backtester-reconstructed
        # instance (zero-arg constructed by _get_template, see
        # backtester/run_backtest.py's fundamental_spec branch) still honors
        # the actual screener-tuned thresholds saved in strategy.json's
        # params -- self.config alone would just be ScreenerConfig()'s
        # defaults in that path, silently discarding required_return/
        # min_roe/etc. overrides.
        cfg = replace(self.config, **{k: v for k, v in p.items() if k in _SCREENER_CONFIG_FIELDS})
        cash_proxy = p.get("cash_proxy", cfg.cash_proxy)
        rebalance_freq_days = p.get("rebalance_freq_days", cfg.rebalance_freq_days)
        weighting_mode = p.get("weighting_mode", cfg.weighting_mode)
        top_n = p.get("top_n", cfg.top_n)

        symbols = list(universe.keys())
        candidate_symbols = [s for s in cfg.universe if s in universe and s != cfg.benchmark_symbol]
        if not candidate_symbols or cfg.benchmark_symbol not in universe:
            return pd.DataFrame()

        master_index = universe[candidate_symbols[0]].index
        n_bars = len(master_index)
        if n_bars == 0:
            return pd.DataFrame()

        benchmark_close = universe[cfg.benchmark_symbol]["Close"]
        benchmark_trailing_return = (
            (benchmark_close / benchmark_close.shift(cfg.lookback_days)) ** (252.0 / cfg.lookback_days) - 1.0
        )

        # Fetch once per distinct candidate set for this instance's whole
        # lifetime (see __init__'s comment), not once per generate_weights()
        # call -- avoids O(n_folds) redundant live yfinance calls for what's
        # supposed to be a single constant snapshot per run.
        if self._fundamentals_cache is None or set(self._fundamentals_cache.index) != set(candidate_symbols):
            self._fundamentals_cache = fetch_fundamentals_frame(candidate_symbols)
        fundamentals_df = self._fundamentals_cache

        rebalance_dates = get_rebalance_dates(master_index, rebalance_freq_days)
        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan, dtype=float)

        for d in rebalance_dates:
            # Check benchmark return at date d (may be NaN during initial warmup)
            bench_ret = (
                float(benchmark_trailing_return.loc[d])
                if (d in benchmark_trailing_return.index and pd.notna(benchmark_trailing_return.loc[d]))
                else float("nan")
            )

            # Evaluate each candidate symbol on date d
            selected_symbols = []
            symbol_mos = {}
            for sym in candidate_symbols:
                if sym not in universe or d not in universe[sym].index:
                    continue
                row = fundamentals_df.loc[sym] if sym in fundamentals_df.index else pd.Series()
                sym_quality_ok = quality_ok(row, cfg)
                close_p = (
                    float(universe[sym].loc[d, "Close"])
                    if pd.notna(universe[sym].loc[d, "Close"])
                    else float("nan")
                )

                metrics = _row_to_fundamental_metrics(sym, row, close_price=close_p)
                val = evaluate_snowball_valuation(metrics, cfg)
                exp_ret = (
                    expected_return(row)
                    if ("expected_return" in row or "earnings_growth" in row)
                    else val["expected_return"]
                )

                should_sell, _ = evaluate_sell_trigger(metrics, exp_ret, bench_ret, cfg)

                # Qualification logic
                if not sym_quality_ok or should_sell:
                    continue

                if cfg.enable_snowball_valuation:
                    if val["mos_ratio"] > 0 and exp_ret >= cfg.required_return:
                        selected_symbols.append(sym)
                        symbol_mos[sym] = max(0.0, val["mos_ratio"])
                else:
                    if exp_ret >= cfg.required_return:
                        selected_symbols.append(sym)
                        symbol_mos[sym] = max(0.0, val.get("mos_ratio", 0.0))

            # Limit to top_n candidates
            if len(selected_symbols) > top_n:
                if weighting_mode == "mos":
                    selected_symbols.sort(key=lambda s: symbol_mos.get(s, 0.0), reverse=True)
                else:
                    selected_symbols.sort(
                        key=lambda s: (
                            expected_return(fundamentals_df.loc[s])
                            if s in fundamentals_df.index
                            else 0.0
                        ),
                        reverse=True,
                    )
                selected_symbols = selected_symbols[:top_n]

            # Compute target weights
            target_weights = {}
            if selected_symbols:
                if weighting_mode == "mos":
                    total_mos = sum(symbol_mos.get(s, 0.0) for s in selected_symbols)
                    if total_mos > 0:
                        target_weights = {s: symbol_mos[s] / total_mos for s in selected_symbols}
                    else:
                        target_weights = {s: 1.0 / len(selected_symbols) for s in selected_symbols}
                else:  # "equal"
                    cap_n = min(top_n, len(candidate_symbols)) if candidate_symbols else top_n
                    if cash_proxy in symbols:
                        unit_w = 1.0 / max(1, cap_n)
                    else:
                        unit_w = 1.0 / len(selected_symbols)
                    target_weights = {s: unit_w for s in selected_symbols}

            # Normalize risky weight if sum > 1.0
            total_risky = sum(target_weights.values())
            if total_risky > 1.0:
                scale = 1.0 / total_risky
                target_weights = {s: w * scale for s, w in target_weights.items()}
                total_risky = 1.0

            # Route remainder to cash_proxy
            if cash_proxy in symbols:
                target_weights[cash_proxy] = max(0.0, 1.0 - total_risky)

            # Explicit 0.0 for all symbols in universe that are not selected / sold / de-risked
            for s in symbols:
                weights_df.loc[d, s] = target_weights.get(s, 0.0)

        return weights_df

    def explain_weights(self, params: dict = None) -> str:
        cfg = self.config
        p = params or {}
        required_return = p.get("required_return", cfg.required_return)
        benchmark_symbol = p.get("benchmark_symbol", cfg.benchmark_symbol)
        margin_of_safety = p.get("margin_of_safety", cfg.margin_of_safety)
        weighting_mode = p.get("weighting_mode", cfg.weighting_mode)
        rebalance_freq_days = p.get("rebalance_freq_days", cfg.rebalance_freq_days)
        return (
            f"Fundamental Margin-of-Safety Strategy (docs/snowball_strategy.txt conservative valuation framework): "
            f"Rebalances every {rebalance_freq_days} days. Holds candidates clearing the 5-gate fundamental quality "
            f"filter (ROE >= {p.get('min_roe', cfg.min_roe):.0%}, sustainable earnings growth, cash flow quality, "
            f"debt limits, dividend payout) and offering positive Margin of Safety (target discount {margin_of_safety:.0%}) "
            f"derived from dual-model terminal PE and dividend capitalization discounting. Expected return must be >= "
            f"{required_return * 100:.0f}%. Weights are allocated via '{weighting_mode}' mode. Exits immediately if "
            f"fundamental quality deteriorates or benchmark ({benchmark_symbol}) trailing return catches up. "
            f"DISCLOSED LIMITATION: fundamentals are fetched once and held CONSTANT across the backtest window "
            f"due to API constraints; trailing price and benchmark returns update dynamically."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        return p.get("lookback_days", cfg.lookback_days)


# Alias per development_plan.md F3.1
FundamentalScreenerStrategy = FundamentalMarginOfSafetyStrategy
