"""Allocation templates for basket-level portfolio construction.

Unlike single-symbol timing templates that output boolean entry/exit signals,
these templates take a whole universe of price data and output a SPARSE
DataFrame of TARGET WEIGHTS (0.0 to 1.0) for each symbol: a row is NaN on
every day EXCEPT an actual rebalance date, where it holds the new target.

This sparseness is deliberate, not an artifact: the backtester (see
`allocation_backtester.py`) tells a real rebalance instruction apart from "no
rebalance today" by whether the row is present at all, not by whether its
VALUE differs from the previous day's -- a template like equal-weight
recomputes the identical 1/N target on every rebalance date, so a
value-equality check would (and, before this was fixed, silently did) treat
every rebalance after the first as a no-op. Templates must NOT forward-fill
their own output; the backtester forward-fills internally for simulation and
uses the pre-ffill sparsity to find the real rebalance dates.
"""

from dataclasses import dataclass, field
from typing import Dict

import numpy as np
import pandas as pd

from common.indicators import realized_vol, roc


@dataclass
class AllocationTemplate:
    name: str
    param_grid: dict

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        """Returns a DataFrame indexed by date, with columns for each symbol.
        A row is NaN except on an actual rebalance date, where it holds the
        target weight (0.0 to 1.0) for that symbol; weights across such a row
        should sum to <= 1.0. Do NOT forward-fill before returning -- see the
        module docstring for why the backtester depends on the sparsity."""
        raise NotImplementedError

    def explain_weights(self, params: dict) -> str:
        """Returns a human-readable explanation of how weights are calculated
        and when rebalancing occurs."""
        raise NotImplementedError

    def warmup_bars(self, params: dict) -> int:
        """How many bars of price history BEFORE a target evaluation window
        this template's indicators need before they stop returning NaN.
        Callers that slice a universe into a sub-window (e.g. a walk-forward
        fold) must include this many extra bars ahead of the window so the
        indicator isn't cold at the window's own start -- see
        `backtester/run_backtest.py`'s `run_walkforward`. Default: no
        indicator, no warmup needed."""
        return 0


def _get_rebalance_dates(index: pd.DatetimeIndex, freq_days: int) -> pd.DatetimeIndex:
    """Simple rebalance schedule: every N trading days."""
    return index[::freq_days]


@dataclass
class EqualWeightAllocation(AllocationTemplate):
    name: str = "equal_weight"
    param_grid: dict = field(default_factory=lambda: {
        "rebalance_freq_days": [5, 21, 63]  # Weekly, Monthly, Quarterly
    })

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()
        
        # Use the first symbol's index as the master calendar (assumes aligned universe)
        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])
        
        n_symbols = len(symbols)
        weight = 1.0 / n_symbols if n_symbols > 0 else 0.0
        
        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates, :] = weight

        # Sparse: only the actual rebalance-date rows are set. The backtester
        # forward-fills for simulation and uses this sparsity, not value
        # equality, to find real rebalance dates (see module docstring).
        return weights_df

    def explain_weights(self, params: dict) -> str:
        return (
            f"Equal Weight (1/N): Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: Capital is distributed evenly across all assets in the basket, "
            f"calculated simply as 1 / (number of assets). This prevents concentration risk "
            f"but ignores relative volatility or performance."
        )


@dataclass
class InverseVolatilityAllocation(AllocationTemplate):
    name: str = "inverse_volatility"
    param_grid: dict = field(default_factory=lambda: {
        "vol_lookback": [20, 60, 120],
        "rebalance_freq_days": [5, 21, 63]
    })

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()
            
        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])
        
        # Calculate daily inverse volatility for all symbols
        inv_vols = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            vol = realized_vol(df["Close"], window=params["vol_lookback"])
            # Avoid division by zero
            inv_vols[sym] = 1.0 / vol.replace(0, np.nan)
            
        # Only keep values on rebalance dates
        inv_vols_rebal = inv_vols.loc[rebalance_dates]
        
        # Normalize so weights sum to 1.0 across the row
        weights_rebal = inv_vols_rebal.div(inv_vols_rebal.sum(axis=1), axis=0)
        
        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal

        return weights_df

    def explain_weights(self, params: dict) -> str:
        return (
            f"Inverse Volatility: Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: Allocates more capital to lower-risk assets to achieve risk parity. "
            f"Calculated by taking the {params['vol_lookback']}-day realized volatility of each asset, "
            f"computing its inverse (1/vol), and normalizing across the basket so the total weights sum to 100%."
        )

    def warmup_bars(self, params: dict) -> int:
        return params["vol_lookback"]


@dataclass
class CrossSectionalMomentumAllocation(AllocationTemplate):
    name: str = "cross_sectional_momentum"
    param_grid: dict = field(default_factory=lambda: {
        "mom_lookback": [63, 126, 252],  # 3m, 6m, 12m
        "top_n_fraction": [0.25, 0.5],   # Top 25% or Top 50% of basket
        "rebalance_freq_days": [21, 63]  # Monthly, Quarterly
    })

    def generate_weights(self, universe: Dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()
            
        master_index = universe[symbols[0]].index
        rebalance_dates = _get_rebalance_dates(master_index, params["rebalance_freq_days"])
        
        # Calculate momentum (Rate of Change) for all symbols
        moms = pd.DataFrame(index=master_index, columns=symbols)
        for sym, df in universe.items():
            moms[sym] = roc(df["Close"], period=params["mom_lookback"])
            
        moms_rebal = moms.loc[rebalance_dates]
        
        n_symbols = len(symbols)
        top_n = max(1, int(n_symbols * params["top_n_fraction"]))
        
        weights_rebal = pd.DataFrame(index=rebalance_dates, columns=symbols, data=0.0)
        
        for date, row in moms_rebal.iterrows():
            if row.isna().all():
                continue
            # Get the top N symbols by momentum
            top_symbols = row.nlargest(top_n).index
            # Equal weight among the top N
            weights_rebal.loc[date, top_symbols] = 1.0 / top_n
            
        weights_df = pd.DataFrame(index=master_index, columns=symbols, data=np.nan)
        weights_df.loc[rebalance_dates] = weights_rebal

        return weights_df

    def explain_weights(self, params: dict) -> str:
        frac_pct = int(params['top_n_fraction'] * 100)
        return (
            f"Cross-Sectional Momentum: Rebalances every {params['rebalance_freq_days']} trading days. "
            f"Reasoning: Capital flows to the strongest recent performers, cutting losers entirely. "
            f"Calculated by ranking all assets by their {params['mom_lookback']}-day trailing return, "
            f"selecting the top {frac_pct}% of the basket, and equally weighting those winners."
        )

    def warmup_bars(self, params: dict) -> int:
        return params["mom_lookback"]

ALLOCATION_TEMPLATES = [
    EqualWeightAllocation,
    InverseVolatilityAllocation,
    CrossSectionalMomentumAllocation
]
