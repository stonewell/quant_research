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
"""

from dataclasses import dataclass, field

import pandas as pd

from .indicators import rsi, sma


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
