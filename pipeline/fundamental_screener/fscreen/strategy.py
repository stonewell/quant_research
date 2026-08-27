"""FundamentalMarginOfSafetyStrategy: the backtester-facing counterpart to
this project's screening report (`run_fundamental_screener.py`) -- an
`AllocationTemplate` so `backtester/run_backtest.py` can run a real
backtest against real symbols using the same buy/sell rules the report
applies (see `fscreen/rules.py`).

DISCLOSED LIMITATION: each candidate's fundamentals are fetched ONCE (at
`generate_weights()` call time) and treated as a CONSTANT signal across the
whole backtest window -- yfinance's free API has no historical
point-in-time fundamentals, so this evaluates "would this symbol pass
today's screen, applied retroactively across the whole window," not "would
it have passed the screen AT EACH historical date." The benchmark
comparator is genuine historical price data (no such limitation there),
since an index's own expected return is inherently a price/total-return
concept -- so the sell trigger still varies day to day even though the
candidate side's own signal doesn't.

Structured like `research_strategy.rs.strategy`'s per-symbol stateful
timing strategies (`RSIMeanReversionStrategy`, `TurtleBreakoutStrategy`) --
a genuine buy-high/sell-low hysteresis needs per-symbol position state.
`_fill_out_columns`/`_sparse_from_daily` are duplicated here (not imported
cross-project) to keep this project decoupled from `research_strategy`,
matching this workspace's existing convention of each project owning its
own small private helpers rather than sharing them.
"""

from dataclasses import fields, replace
from typing import Dict

import numpy as np
import pandas as pd

from common.allocation_templates import AllocationTemplate

from .config import ScreenerConfig
from .fundamentals import fetch_fundamentals_frame
from .rules import expected_return, quality_ok

_SCREENER_CONFIG_FIELDS = {f.name for f in fields(ScreenerConfig)}


def _fill_out_columns(daily: pd.DataFrame, symbols: list) -> pd.DataFrame:
    for s in symbols:
        if s not in daily.columns:
            daily[s] = 0.0
    return daily[symbols]


def _sparse_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
    changed = (daily != daily.shift(1)).any(axis=1)
    changed.iloc[0] = True
    return daily.where(changed)


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

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict = None) -> pd.DataFrame:
        p = params or {}
        # Merge params onto self.config so a backtester-reconstructed
        # instance (zero-arg constructed by _get_template, see
        # backtester/run_backtest.py's fundamental_spec branch) still honors
        # the actual screener-tuned thresholds saved in strategy.json's
        # params -- self.config alone would just be ScreenerConfig()'s
        # defaults in that path, silently discarding required_return/
        # min_roe/etc. overrides. quality_ok()/expected_return() need an
        # actual ScreenerConfig object (attribute access), not a raw dict.
        cfg = replace(self.config, **{k: v for k, v in p.items() if k in _SCREENER_CONFIG_FIELDS})
        cash_proxy = p.get("cash_proxy", "BIL")

        symbols = list(universe.keys())
        candidate_symbols = [s for s in cfg.universe if s in universe and s != cfg.benchmark_symbol]
        if not candidate_symbols or cfg.benchmark_symbol not in universe:
            return pd.DataFrame()

        master_index = universe[candidate_symbols[0]].index
        n_bars = len(master_index)

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

        raw_weights = {}
        for sym in candidate_symbols:
            row = fundamentals_df.loc[sym]
            sym_quality_ok = quality_ok(row, cfg)
            sym_expected_return = expected_return(row)

            raw = np.zeros(n_bars)
            # A constant signal that fails the quality gate or the buy
            # hurdle up front never enters at all -- matches the report's
            # own buy_flag semantics (see fscreen/rules.py).
            if sym_quality_ok and sym_expected_return >= cfg.required_return:
                # The only thing that varies day to day is the benchmark's
                # own trailing return: exit once it catches up to (or the
                # comparator is still warming up, hence NaN-safe True
                # default) this symbol's constant expected return -- the
                # doc's own sell-trigger rule, translated directly.
                exit_arr = (
                    (benchmark_trailing_return > sym_expected_return).fillna(True).to_numpy()
                )
                in_position = False
                for i in range(n_bars):
                    if in_position:
                        if exit_arr[i]:
                            in_position = False
                            raw[i] = 0.0
                        else:
                            raw[i] = 1.0
                    elif not exit_arr[i]:
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
        benchmark_symbol = p.get("benchmark_symbol", cfg.benchmark_symbol)
        return (
            f"Fundamental Margin-of-Safety (real-data adaptation of docs/snowball_strategy.txt's "
            f"conservative valuation framework): holds a candidate only while its real ROE/dividend/"
            f"leverage/earnings-growth (from yfinance) clear the quality gate AND its expected return "
            f"(earnings_growth + dividend_yield, the doc's own Model 2 formula) is >= "
            f"{required_return * 100:.0f}%. Exits once {benchmark_symbol}'s own trailing return "
            f"catches up to that expected return -- the doc's own sell-trigger rule. DISCLOSED "
            f"LIMITATION: fundamentals are fetched once and held CONSTANT across the whole backtest "
            f"window (yfinance's free API has no historical point-in-time fundamentals) -- this "
            f"evaluates 'would this symbol pass today's screen, applied retroactively,' not a true "
            f"historical simulation. See research_strategy.rs.strategy.CompounderMarginOfSafetyStrategy "
            f"for the price-only-proxy sibling used elsewhere in this workspace's offline testing."
        )

    def warmup_bars(self, params: dict = None) -> int:
        cfg = self.config
        p = params or {}
        return p.get("lookback_days", cfg.lookback_days)
