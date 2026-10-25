"""Offline tests for the EXPERIMENTAL `common/allocation_backtester_fast.py` (task B4.1).

覆盖三件事：
1. **精度等价** -- 快内核的逐日 `equity` / `actual_w` / 成交股数与生产
   `run_allocation_backtest`（close 模式）逐位相等（容差 `rtol=1e-12`，实测
   0.0）；
2. **语义边界** -- 整手取整、现金不足的部分成交、停牌双向拒成交、
   `cash >= 0`、`sum(actual_w) <= 1+1e-9`、`min_weight_change` 抑制、
   A 股 T+1 / 涨跌停、分数字股、空头腿；
3. **"未接入生产"的静态护栏** -- 生产模块不得 import 快模块；快模块只允许
   借用两处纯规则查表；`numba` 不可用时整份套件必须 skip 或正常跑通，绝不
   error。

全部离线：只用合成 OHLCV，零网络、零真实行情。
"""

import ast
import copy
import os
import sys

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common import allocation_backtester as prod_backtester
from common.allocation_backtester import run_allocation_backtest
from common.allocation_backtester_fast import (
    FastBacktestResult,
    JIT_KERNEL,
    KERNEL_STATUS,
    KERNEL_VERSION,
    NUMBA_AVAILABLE,
    UNPORTED_PRODUCTION_KEYS,
    _lot_round,
    _tradable,
    close_mode_kernel,
    run_allocation_backtest_fast,
)

# 精度契约：`rtol=1e-12`。理由 -- 两条路径的浮点运算顺序被逐条复刻（同一个
# `cash -= (new_s - prior_s)*price + total_cost` 结合顺序、同一次左折叠求和、
# 同一批 pandas 表达式产出的 `mark` / `ret`），所以理论上应当**逐位相等**；
# 容差只为兜住 `np.dot` vs. 逐项乘加这类 BLAS 求和顺序差异留的余量（本内核与
# 生产用同一个 `np.dot`，实测偏差 0.0）。`atol=0.0` 是有意的：equity 量级在
# 1e5~1e6，`rtol=1e-12` 已经是 1e-6 分钱，任何真实账本偏差都会被抓到。
EQUIV_RTOL = 1e-12


# --------------------------------------------------------------------------- #
# 合成数据
# --------------------------------------------------------------------------- #
def _make_ohlcv(n_days, seed, base=50.0, with_volume=True):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-06", periods=n_days)
    rets = rng.normal(0.0004, 0.011, n_days)
    close = base * np.exp(np.cumsum(rets))
    high = close * (1.0 + np.abs(rng.normal(0.0, 0.004, n_days)))
    low = close * (1.0 - np.abs(rng.normal(0.0, 0.004, n_days)))
    open_ = close * (1.0 + rng.normal(0.0, 0.002, n_days))
    df = pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close},
        index=idx,
    )
    if with_volume:
        df["Volume"] = rng.integers(200_000, 2_000_000, n_days).astype(float)
    return df


def _make_universe(n_days=160, n_sym=5, seed=11, symbols=None, with_volume=True):
    symbols = symbols or [f"S{k}" for k in range(n_sym)]
    return {
        sym: _make_ohlcv(n_days, seed + 17 * k, base=40.0 + 25.0 * k, with_volume=with_volume)
        for k, sym in enumerate(symbols)
    }


def _sparse_weights(universe, rows, symbols=None):
    """`rows` = {row_pos: {symbol: weight}}. 未列出的格子是 NaN（不指令）。"""
    any_df = next(iter(universe.values()))
    symbols = symbols or list(universe.keys())
    tw = pd.DataFrame(np.nan, index=any_df.index, columns=symbols, dtype=float)
    for pos, weights in rows.items():
        for sym, w in weights.items():
            tw.loc[tw.index[pos], sym] = w
    return tw


def _assert_equivalent(universe, tw, **kwargs):
    """对拍生产 close 模式：equity / actual_w / 成交股数 逐项比较。"""
    ref = run_allocation_backtest(universe, tw, **kwargs)
    fast = run_allocation_backtest_fast(universe, tw, **kwargs)

    ref_eq = ref["equity_curve"]["equity"].to_numpy(dtype=float)
    fast_eq = fast["equity_curve"]["equity"].to_numpy(dtype=float)
    assert ref_eq.shape == fast_eq.shape
    np.testing.assert_allclose(fast_eq, ref_eq, rtol=EQUIV_RTOL, atol=0.0)

    ref_w = ref["actual_weights"].to_numpy(dtype=float)
    fast_w = fast["actual_weights"].to_numpy(dtype=float)
    np.testing.assert_allclose(fast_w, ref_w, rtol=EQUIV_RTOL, atol=1e-15)

    ref_r = ref["rebalance_report"]
    fast_r = fast["rebalance_report"]
    assert len(ref_r) == len(fast_r), (len(ref_r), len(fast_r))
    if len(ref_r):
        for col in ("symbol", "action", "date"):
            assert list(ref_r[col]) == list(fast_r[col]), col
        for col in ("shares", "prior_shares", "target_shares", "trade_value",
                    "commission", "slippage", "total_cost", "price",
                    "portfolio_equity"):
            np.testing.assert_allclose(
                fast_r[col].to_numpy(dtype=float), ref_r[col].to_numpy(dtype=float),
                rtol=EQUIV_RTOL, atol=1e-15,
            )
        assert list(ref_r["rebalance_id"]) == list(fast_r["rebalance_id"])

    ref_s = ref["suspension_report"]
    fast_s = fast["suspension_report"]
    assert len(ref_s) == len(fast_s)
    if len(ref_s):
        assert list(ref_s["symbol"]) == list(fast_s["symbol"])
        assert list(ref_s["reason"]) == list(fast_s["reason"])
        assert list(ref_s["action"]) == list(fast_s["action"])

    return ref, fast


# --------------------------------------------------------------------------- #
# 1. 精度等价
# --------------------------------------------------------------------------- #
def test_kernel_matches_production_bitwise_generic_market():
    """默认（无地区规则）市场：equity / actual_w / 成交股数**逐位**相等。"""
    universe = _make_universe(n_days=180, n_sym=5, seed=3)
    tw = _sparse_weights(universe, {
        0: {"S0": 0.30, "S1": 0.30, "S2": 0.20, "S3": 0.10},
        20: {"S0": 0.20, "S1": 0.25, "S2": 0.25, "S3": 0.20, "S4": 0.10},
        55: {"S0": 0.40, "S1": 0.10, "S2": 0.10, "S3": 0.20, "S4": 0.20},
        90: {"S0": 0.0, "S1": 0.50, "S2": 0.0},
        130: {"S0": 0.20, "S1": 0.20, "S2": 0.20, "S3": 0.20, "S4": 0.20},
    })
    ref, fast = _assert_equivalent(universe, tw)

    # 逐位（不是"容差内"）：这是本任务精度主张的最强形式。
    np.testing.assert_array_equal(
        fast["equity_curve"]["equity"].to_numpy(), ref["equity_curve"]["equity"].to_numpy()
    )
    np.testing.assert_array_equal(
        fast["actual_weights"].to_numpy(), ref["actual_weights"].to_numpy()
    )
    np.testing.assert_array_equal(
        fast["rebalance_report"]["shares"].to_numpy(dtype=float),
        ref["rebalance_report"]["shares"].to_numpy(dtype=float),
    )


def test_kernel_matches_production_on_integer_lot_market():
    """美股式 1 股整手 + `min_weight_change`（抑制被动漂移）。"""
    universe = _make_universe(n_days=150, n_sym=4, seed=5)
    tw = _sparse_weights(universe, {
        0: {"S0": 0.30, "S1": 0.30, "S2": 0.30},
        30: {"S0": 0.31, "S1": 0.29, "S2": 0.30},   # 微调，可能被 min_weight_change 抑制
        70: {"S0": 0.10, "S1": 0.45, "S2": 0.35},
        110: {"S0": 0.33, "S1": 0.33, "S2": 0.34},
    })
    _assert_equivalent(universe, tw, us_trading=True, min_weight_change=0.05)


def test_kernel_matches_production_china_a_share_rules():
    """A 股：100 股整手 + 涨跌停 + T+1 + 卖出印花税。"""
    symbols = ["600000.SH", "600519.SH", "300750.SZ", "000001.SZ"]
    universe = _make_universe(n_days=170, n_sym=4, seed=7, symbols=symbols)
    tw = _sparse_weights(universe, {
        0: {symbols[0]: 0.30, symbols[1]: 0.30, symbols[2]: 0.20, symbols[3]: 0.10},
        40: {symbols[0]: 0.10, symbols[1]: 0.40, symbols[2]: 0.30, symbols[3]: 0.10},
        80: {symbols[0]: 0.0, symbols[1]: 0.0, symbols[2]: 0.0, symbols[3]: 0.0},
        120: {symbols[0]: 0.25, symbols[1]: 0.25, symbols[2]: 0.25, symbols[3]: 0.25},
    })
    _assert_equivalent(universe, tw, china_trading=True)


def test_kernel_matches_production_china_limit_locked_bar():
    """A 股一字涨停：买被拒、卖放行 -- 与生产逐位一致。"""
    symbols = ["600001.SH"]
    universe = _make_universe(n_days=12, n_sym=1, seed=9, symbols=symbols)
    df = universe[symbols[0]].copy()
    prev_close = df["Close"].iloc[2]
    df.iloc[3, df.columns.get_loc("Close")] = prev_close * 1.10
    df.iloc[3, df.columns.get_loc("High")] = prev_close * 1.10
    df.iloc[3, df.columns.get_loc("Low")] = prev_close * 1.06
    universe[symbols[0]] = df

    tw = _sparse_weights(universe, {0: {symbols[0]: 0.5}, 3: {symbols[0]: 1.0}}, symbols=symbols)
    ref, fast = _assert_equivalent(universe, tw, china_trading=True)
    # 一字板上的加仓必须被拒：3 号行不该出现成交。
    d3 = ref["rebalance_report"][
        ref["rebalance_report"]["date"] == universe[symbols[0]].index[3].strftime("%Y-%m-%d")
    ]
    assert d3.empty
    assert len(fast["rebalance_report"]) == len(ref["rebalance_report"])


def test_kernel_matches_production_fractional_shares():
    """`min_shares=0` 的分数股路径（lot==0）。"""
    universe = _make_universe(n_days=120, n_sym=3, seed=13)
    tw = _sparse_weights(universe, {
        0: {"S0": 0.4, "S1": 0.4},
        45: {"S0": 0.2, "S1": 0.2, "S2": 0.5},
        95: {"S0": 0.0, "S1": 0.0, "S2": 0.0},
    })
    _assert_equivalent(universe, tw, min_shares=0)


def test_kernel_matches_production_with_short_leg():
    """多空混合目标权重（负权重 = 开空），走的是卖侧对称路径。"""
    universe = _make_universe(n_days=120, n_sym=3, seed=21)
    tw = _sparse_weights(universe, {
        0: {"S0": 0.6, "S1": -0.3},
        40: {"S0": 0.3, "S1": -0.6, "S2": 0.4},
        85: {"S0": 0.0, "S1": 0.0, "S2": 0.0},
    })
    _assert_equivalent(universe, tw, min_shares=1)


# 空头的四条独立路径（B3.5 对齐回归）。旧内核的
# `prior_s + trade_s if prior_s <= 0` 会在 (a) 从平仓开空、(b) 空头加仓 这两条
# 路径上把符号翻成正的多头，所以这两个场景**不能**只靠"两边都一致"断言 -- 必须
# 额外断言账本里该腿确实是负的。
_SHORT_LEG_CASES = {
    "open_short_from_flat": {
        # day-0 不建仓（全 NaN），只在第 30 行从 0 开空。
        "rows": {30: {"S1": -0.50}, 60: {"S1": 0.0}},
        "short_sym": "S1",
    },
    "add_to_short": {
        # -0.20 -> -0.55：delta_w < 0 且 prior_s < 0（"加空"），旧码会把它变成回补。
        "rows": {10: {"S0": 0.30, "S1": -0.20}, 40: {"S0": 0.30, "S1": -0.55}},
        "short_sym": "S1",
    },
    "partial_cover_short": {
        # -0.60 -> -0.25：回补一部分（buy 侧），应保留剩余空头而非平掉或翻多。
        "rows": {10: {"S0": 0.30, "S1": -0.60}, 40: {"S0": 0.30, "S1": -0.25}},
        "short_sym": "S1",
    },
    "long_short_mixed": {
        # 多空同时存在 + 空头先加仓后全平（target 0 走 `abs(prior_s)` 的 BUY 分支）。
        "rows": {
            0: {"S0": 0.50, "S1": -0.30},
            35: {"S0": 0.35, "S1": -0.60, "S2": 0.30},
            70: {"S0": 0.40, "S1": 0.0, "S2": 0.30},
            95: {"S0": 0.0, "S1": 0.0, "S2": 0.0},
        },
        "short_sym": "S1",
    },
}


@pytest.mark.parametrize("case_name", sorted(_SHORT_LEG_CASES))
def test_kernel_matches_production_short_leg_paths(case_name):
    """空头建仓 / 加仓 / 部分回补 / 多空混合：与生产等价 + 符号独立可验。

    B3.5 之前卖侧是 `prior_s + trade_s if prior_s <= 0`，本用例的
    `open_short_from_flat` / `add_to_short` 会在快内核里被记成多头；现在两侧都
    统一为 `new_s = prior_s - trade_s`（`|prior_s|` 截断只对多头生效）。
    """
    spec = _SHORT_LEG_CASES[case_name]
    universe = _make_universe(n_days=120, n_sym=3, seed=70 + len(case_name))
    tw = _sparse_weights(universe, spec["rows"])
    short_sym = spec["short_sym"]

    ref, fast = _assert_equivalent(universe, tw, min_shares=1)

    # 独立于生产路径的符号断言：空头腿在持有期内必须为负权重，且其
    # `prior_shares -> target_shares` 的成交方向与 action 自洽。
    rep = fast["rebalance_report"]
    short_rows = rep[rep["symbol"] == short_sym]
    assert not short_rows.empty, f"{case_name}: 空头腿没有产生任何成交"
    for _, row in short_rows.iterrows():
        if row["action"] == "SELL":
            assert row["target_shares"] < row["prior_shares"], (
                f"{case_name}: SELL 必须让 signed shares 变小 "
                f"({row['prior_shares']} -> {row['target_shares']})"
            )
        else:
            assert row["target_shares"] > row["prior_shares"], (
                f"{case_name}: BUY 必须让 signed shares 变大 "
                f"({row['prior_shares']} -> {row['target_shares']})"
            )
    # 该腿至少有一次持仓为净空（负权重），否则本用例没覆盖到空头。
    assert float(fast["actual_weights"][short_sym].min()) < 0.0, (
        f"{case_name}: 空头腿从未变为负权重"
    )
    # 与生产逐位复算一次最大偏差，便于失败时定位（容差仍由 _assert_equivalent 管）。
    dev = np.max(np.abs(
        fast["equity_curve"]["equity"].to_numpy(dtype=float)
        - ref["equity_curve"]["equity"].to_numpy(dtype=float)
    ))
    assert dev <= 1e-9, f"{case_name}: equity 最大偏差 {dev}"


def test_kernel_matches_production_suspension_guard():
    """停牌（Volume == 0 / NaN）双向拒成交，且持仓继续按最后收盘价计价。"""
    symbols = ["600100.SH", "600101.SH"]
    universe = _make_universe(n_days=60, n_sym=2, seed=31, symbols=symbols)
    a = universe[symbols[0]].copy()
    a.iloc[10:16, a.columns.get_loc("Volume")] = 0.0          # 停牌若干日
    a.iloc[30, a.columns.get_loc("Volume")] = np.nan           # 缺量
    universe[symbols[0]] = a

    tw = _sparse_weights(universe, {
        0: {symbols[0]: 0.4, symbols[1]: 0.4},
        12: {symbols[0]: 0.0, symbols[1]: 0.8},   # 停牌期内想清仓 -> 必须被拒
        31: {symbols[0]: 0.6, symbols[1]: 0.2},   # 缺量日 -> 必须被拒
        45: {symbols[0]: 0.0, symbols[1]: 0.8},   # 复牌后可以清仓
    })
    ref, fast = _assert_equivalent(universe, tw, china_trading=True)
    assert not ref["suspension_report"].empty
    assert set(ref["suspension_report"]["reason"]) <= {"volume_zero", "volume_missing"}
    # 停牌期间持仓不得被"幽灵成交"清掉。
    idx = universe[symbols[0]].index
    held_12 = ref["actual_weights"].loc[idx[12], symbols[0]]
    assert held_12 > 0.0


# --------------------------------------------------------------------------- #
# 2. 语义边界（不依赖生产实现的独立断言）
# --------------------------------------------------------------------------- #
def test_kernel_matches_production_on_randomized_fixtures():
    """确定性随机对拍：多日历长度 / 标的数 / 地区规则 / 零成交量的组合。

    覆盖手工 fixture 容易漏掉的排列（停牌散落在随机位置、目标行随机稀疏、
    现金正好卡在整手边界）。任何一处不等价都会在这里暴露。
    """
    for trial in range(6):
        rng = np.random.default_rng(1000 + trial)
        n_days = int(rng.integers(30, 160))
        n_sym = int(rng.integers(1, 6))
        syms = [f"X{k}" for k in range(n_sym)]
        universe = {
            s: _make_ohlcv(n_days, 5000 + trial * 7 + k, base=30.0 + 20 * k)
            for k, s in enumerate(syms)
        }
        # 随机撒一些停牌柱（Volume=0 / NaN）-- 停牌必须双向拒成交。
        for s in syms:
            v = universe[s]["Volume"].to_numpy(dtype=float).copy()
            hits = rng.integers(0, n_days, max(1, n_days // 12))
            v[hits] = 0.0
            v[np.roll(hits, 3) % n_days] = np.nan
            universe[s]["Volume"] = v

        idx = next(iter(universe.values())).index
        tw = pd.DataFrame(np.nan, index=idx, columns=syms, dtype=float)
        for r in sorted(set(rng.integers(0, n_days, int(rng.integers(1, 6))).tolist())):
            w = rng.dirichlet(np.ones(n_sym)) * rng.uniform(0.3, 1.4)
            for k, s in enumerate(syms):
                if rng.integers(0, 10) != 0:
                    tw.loc[idx[r], s] = float(w[k])

        kwargs = {}
        mode = int(rng.integers(0, 4))
        if mode == 1:
            kwargs["china_trading"] = True
        elif mode == 2:
            kwargs["us_trading"] = True
        if rng.integers(0, 2):
            kwargs["min_shares"] = 0
        if rng.integers(0, 2):
            kwargs["min_weight_change"] = float(rng.choice([0.0, 0.01, 0.05]))
        _assert_equivalent(universe, tw, **kwargs)


def test_lot_round_semantics():
    assert _lot_round(1234.56, 0) == 1234.56          # 分数股原样
    # 生产是 `int(round(raw, 6))`，即 **截断** 而非四舍五入（1234.56 -> 1234）。
    assert _lot_round(1234.56, 1) == 1234.0
    assert _lot_round(1234.99, 1) == 1234.0
    assert _lot_round(1299.99, 100) == 1200.0         # 截断后整手向下
    assert _lot_round(99.9, 100) == 0.0
    assert _tradable(0.0, 100) is False
    assert _tradable(100.0, 100) is True
    assert _tradable(1e-12, 0) is False
    assert _tradable(1e-6, 0) is True


def test_cash_never_negative_and_weights_bounded():
    """硬不变量：`cash >= 0`（由 equity 恒等式反推）与 `sum(actual_w) <= 1+1e-9`。"""
    symbols = ["600200.SH", "600201.SH", "600202.SH"]
    universe = _make_universe(n_days=90, n_sym=3, seed=41, symbols=symbols)
    # 目标权重合计 1.5（超额指令）：只允许现金可负担的多头部分被买入。
    tw = _sparse_weights(universe, {
        0: {symbols[0]: 0.5, symbols[1]: 0.5, symbols[2]: 0.5},
        30: {symbols[0]: 0.33, symbols[1]: 0.33, symbols[2]: 0.34},
        60: {symbols[0]: 0.5, symbols[1]: 0.5, symbols[2]: 0.5},
    })
    fast = run_allocation_backtest_fast(universe, tw, china_trading=True)

    aw = fast["actual_weights"].to_numpy(dtype=float)
    assert np.all(np.nansum(aw, axis=1) <= 1.0 + 1e-9)
    assert np.all(aw >= -1e-12)

    # 用成交记录重放到账本，验证 equity[t] == cash + holdings@close 且 cash >= 0。
    closes = pd.DataFrame({s: universe[s]["Close"] for s in symbols})
    held = np.zeros(len(symbols))
    cash = 100_000.0
    rep = fast["rebalance_report"]
    equity = fast["equity_curve"]["equity"]
    by_day = {}
    for _, row in rep.iterrows():
        by_day.setdefault(row["date"], []).append(row)
    for i, ts in enumerate(closes.index):
        day = ts.strftime("%Y-%m-%d")
        for row in by_day.get(day, []):
            j = symbols.index(row["symbol"])
            trade_s = row["shares"] * (1.0 if row["action"] == "BUY" else -1.0)
            cash -= trade_s * row["price"]
            cash -= row["total_cost"]
            held[j] += trade_s
        mtm = cash + float(np.dot(held, closes.iloc[i].to_numpy(dtype=float)))
        assert cash >= -1e-9, f"cash went negative on {day}: {cash}"
        np.testing.assert_allclose(mtm, float(equity.iloc[i]), rtol=1e-12, atol=0.0)


def test_cash_limited_buy_is_a_partial_fill_and_final():
    """现金不足 -> 部分成交（不是拒绝、不是幽灵杠杆、也不次日重试）。"""
    symbols = ["600300.SH"]
    idx = pd.bdate_range("2020-01-06", periods=6)
    # 价格 1000 元/股、100 股整手 => 一手就是 10 万，正好吃掉全部本金。
    df = pd.DataFrame(
        {"Open": [1000.0] * 6, "High": [1010.0] * 6, "Low": [990.0] * 6,
         "Close": [1000.0] * 6, "Volume": [1e6] * 6},
        index=idx,
    )
    universe = {symbols[0]: df}
    # 目标 100% 但只有 10 万：整手 100 股买不起（含费用），只能买 0 股 -> 全现金。
    tw = _sparse_weights(universe, {0: {symbols[0]: 1.0}}, symbols=symbols)
    res = run_allocation_backtest_fast(universe, tw, china_trading=True)
    eq = res["equity_curve"]["equity"]
    # 一手 10 万 + 费用 > 10 万 => 买不起，day-0 兜底把手数削到 0。
    assert res["rebalance_report"].empty
    np.testing.assert_allclose(eq.to_numpy(), np.full(6, 100_000.0), rtol=1e-12, atol=0.0)

    # 价格降到 500 => 一手 5 万，可以买 100 股（部分建仓，剩余 5 万闲置现金）。
    df2 = df.copy()
    for col in ("Open", "High", "Low", "Close"):
        df2[col] = df2[col] / 2.0
    res2 = run_allocation_backtest_fast({symbols[0]: df2}, tw, china_trading=True)
    rep2 = res2["rebalance_report"]
    assert len(rep2) == 1
    assert rep2["shares"].iloc[0] == 100.0
    # 权重只有 ~50%，因为整手截断留下闲置现金（这正是 B1.1 要如实披露的）。
    held_w = res2["actual_weights"].iloc[0, 0]
    assert 0.49 < held_w < 0.51


def test_suspension_refuses_both_buy_and_sell():
    symbols = ["600400.SH"]
    idx = pd.bdate_range("2020-01-06", periods=5)
    df = pd.DataFrame(
        {"Open": [10.0] * 5, "High": [10.1] * 5, "Low": [9.9] * 5,
         "Close": [10.0] * 5, "Volume": [1e6] * 5},
        index=idx,
    )
    df.loc[idx[2], "Volume"] = 0.0  # 停牌
    universe = {symbols[0]: df}
    tw = _sparse_weights(universe, {0: {symbols[0]: 0.5}, 2: {symbols[0]: 0.0}}, symbols=symbols)
    res = run_allocation_backtest_fast(universe, tw, china_trading=True)
    susp = res["suspension_report"]
    assert len(susp) == 1
    assert susp["reason"].iloc[0] == "volume_zero"
    assert susp["action"].iloc[0] == "SELL"       # 卖出指令被拒
    assert res["actual_weights"].iloc[2, 0] > 0.0  # 持仓没被幽灵清掉


def test_unsupported_modes_raise_instead_of_degrading():
    universe = _make_universe(n_days=10, n_sym=1, seed=1)
    tw = _sparse_weights(universe, {0: {"S0": 0.5}})
    with pytest.raises(ValueError, match="execution_price='close'"):
        run_allocation_backtest_fast(universe, tw, execution_price="next_open")
    with pytest.raises(ValueError, match="market_impact"):
        run_allocation_backtest_fast(universe, tw, market_impact=True)
    with pytest.raises(ValueError, match="max_volume_participation"):
        run_allocation_backtest_fast(universe, tw, max_volume_participation=0.1)
    with pytest.raises(ValueError, match="intraday_data"):
        run_allocation_backtest_fast(universe, tw, intraday_data={"S0": universe["S0"]})


def test_empty_inputs_short_circuit_like_production():
    universe = _make_universe(n_days=10, n_sym=1, seed=1)
    empty = pd.DataFrame(np.nan, index=universe["S0"].index, columns=["S0"])
    res = run_allocation_backtest_fast(universe, empty)
    # 空的行内没有非 NaN => 全 0 目标 => 纯现金账本（不是空 DataFrame）。
    assert "equity_curve" in res
    disjoint = pd.DataFrame(np.nan, index=pd.bdate_range("2035-01-01", periods=3), columns=["S0"])
    res2 = run_allocation_backtest_fast(universe, disjoint)
    assert res2["equity_curve"].empty and res2["turnover"] == 0.0


# --------------------------------------------------------------------------- #
# 3. "未接入生产" 的静态护栏 + numba 诚实性
# --------------------------------------------------------------------------- #
def test_fast_kernel_is_not_imported_by_production_backtester():
    """生产模块的源码里不得出现本试验模块的任何引用（防止被悄悄接入）。"""
    src_path = prod_backtester.__file__
    with open(src_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    assert "allocation_backtester_fast" not in src
    assert "run_allocation_backtest_fast" not in src
    assert "close_mode_kernel" not in src


def test_fast_module_only_borrows_pure_rule_lookups_from_production():
    """反向依赖白名单：只允许借用涨跌停板宽 / 港股手数这类纯查表。"""
    import common.allocation_backtester_fast as fast_mod

    with open(fast_mod.__file__, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    allowed = {"HK_COMMON_BOARD_LOTS", "_get_china_price_limit", "_get_hk_board_lot"}
    borrowed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "common.allocation_backtester":
            borrowed |= {alias.name for alias in node.names}
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "common.allocation_backtester"
    assert borrowed <= allowed, f"unexpected production coupling: {borrowed - allowed}"
    # 账本状态机本身不得借生产的实现细节。
    assert "run_allocation_backtest" not in borrowed


def test_experimental_marker_is_declared():
    import common.allocation_backtester_fast as fast_mod

    assert KERNEL_STATUS == "experimental"
    assert KERNEL_VERSION
    doc = fast_mod.__doc__ or ""
    assert "EXPERIMENTAL" in doc
    assert "未接入生产" in doc


def test_numba_is_optional_and_reported_honestly():
    """`numba` 缺失时降级为纯 NumPy 内核，且**不谎报** JIT。

    numba 可用 -> `JIT_KERNEL` 是 njit 版本、结果仍与生产等价；
    不可用   -> `JIT_KERNEL is None` 且 `kernel == "numpy"`（本仓库当前环境）。
    两种情况都必须**跑通而不是 error**（缺 numba 时整份套件照常绿）。
    """
    universe = _make_universe(n_days=40, n_sym=2, seed=2)
    tw = _sparse_weights(universe, {0: {"S0": 0.4, "S1": 0.4}, 15: {"S0": 0.0, "S1": 0.8}})
    res = run_allocation_backtest_fast(universe, tw)
    if NUMBA_AVAILABLE and JIT_KERNEL is not None:
        assert res["kernel"] == "numba"
        _assert_equivalent(universe, tw)  # JIT 路径同样必须等价
    else:
        assert JIT_KERNEL is None
        assert res["kernel"] == "numpy"
        assert close_mode_kernel is not None


def test_kernel_accepts_arrays_only_and_returns_expected_arity():
    """`close_mode_kernel` 是纯数组函数（压测脚本直接用它，绕过 pandas）。"""
    n_days, n_sym = 20, 3
    closes = np.full((n_days, n_sym), 10.0)
    tgt = np.zeros((n_days, n_sym))
    tgt[0] = 0.3
    is_reb = np.zeros(n_days, dtype=bool)
    is_reb[0] = True
    out = close_mode_kernel(
        tgt, is_reb, closes, closes, closes, closes, np.zeros((n_days, n_sym)),
        np.full((n_days, n_sym), 1e6), np.ones(n_sym, dtype=bool),
        np.ones(n_sym, dtype=np.int64), np.arange(n_sym, dtype=np.int64),
        np.ones(n_sym),
        100_000.0, 0.0005, 0.0005, 0.0,
        False, False, False, 0.0005, 0.001085, 0.0000278,
        np.zeros(n_days), np.zeros((n_days, n_sym)),
        np.zeros((64, 16)), np.zeros((128, 6)),
    )
    assert len(out) == 4
    n_fill, n_susp, turnover, rebalance_id = out
    assert n_fill == 3 and n_susp == 0 and rebalance_id == 1
    assert turnover > 0.0


# --------------------------------------------------------------------------- #
# B5.3: Schema drift guardrails tests
# --------------------------------------------------------------------------- #
def test_minimal_metrics_raises_not_implemented_error():
    """Verify minimal_metrics=True raises NotImplementedError."""
    universe = _make_universe(n_days=20, n_sym=2, seed=1)
    tw = _sparse_weights(universe, {0: {"S0": 0.5, "S1": 0.5}})
    with pytest.raises(NotImplementedError, match="minimal_metrics=True"):
        run_allocation_backtest_fast(universe, tw, minimal_metrics=True)


def test_unported_production_keys_raise_not_implemented_error():
    """Every read path on `FastBacktestResult` must agree about unported keys.

    `[]`, `.get()` and `in` are the three ways a caller can probe the result dict.
    If `in` answered False while the other two raised, a `has_key`-style guard
    (`if "deferral_report" in res:`) would silently take the "not available" branch
    and the B5.3 drift guard would never fire -- exactly the hole this asserts shut.
    """
    universe = _make_universe(n_days=20, n_sym=2, seed=1)
    tw = _sparse_weights(universe, {0: {"S0": 0.5, "S1": 0.5}})
    res = run_allocation_backtest_fast(universe, tw)
    assert isinstance(res, FastBacktestResult)

    # Positive control: a PORTED key still answers True through the new override,
    # and an unknown key still answers False -- so `__contains__` is a guard, not a
    # blanket raise.
    assert "equity_curve" in res
    assert res["equity_curve"] is res.get("equity_curve")
    assert "no_such_key_at_all" not in res
    assert res.get("no_such_key_at_all") is None

    # Standard supported keys work as normal dict access
    assert not res["equity_curve"].empty
    assert res.get("execution_price") == "close"
    assert res["kernel_status"] == KERNEL_STATUS

    # Unported production keys raise NotImplementedError through ALL THREE paths
    for key in UNPORTED_PRODUCTION_KEYS:
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            _ = res[key]
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            res.get(key)
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            _ = key in res


# --------------------------------------------------------------------------- #
# B6.2: Write-path and copy() guardrail tests
# --------------------------------------------------------------------------- #
def test_unported_production_keys_write_paths_raise():
    """Every write and deletion path on `FastBacktestResult` must reject unported keys.

    Task B6.2: __setitem__, setdefault, update, and pop must all raise
    NotImplementedError when called with any key in UNPORTED_PRODUCTION_KEYS.
    Also verifies that initialization with unported keys is rejected.
    """
    universe = _make_universe(n_days=20, n_sym=2, seed=1)
    tw = _sparse_weights(universe, {0: {"S0": 0.5, "S1": 0.5}})
    res = run_allocation_backtest_fast(universe, tw)
    assert isinstance(res, FastBacktestResult)

    for key in UNPORTED_PRODUCTION_KEYS:
        # __setitem__
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            res[key] = 123

        # setdefault
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            res.setdefault(key, 123)

        # update via dict
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            res.update({key: 123})

        # update via list of pairs
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            res.update([(key, 123)])

        # update via kwargs
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            res.update(**{key: 123})

        # pop without default
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            res.pop(key)

        # pop with default
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            res.pop(key, None)

        # __init__ with dict
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            FastBacktestResult({key: 123})

        # __init__ with kwargs
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            FastBacktestResult(**{key: 123})

        # __init__ with pairs
        with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
            FastBacktestResult([(key, 123)])


def test_update_atomicity_on_unported_keys():
    """update() must be atomic: if ANY candidate key is unported, self is not mutated at all."""
    universe = _make_universe(n_days=20, n_sym=2, seed=1)
    tw = _sparse_weights(universe, {0: {"S0": 0.5, "S1": 0.5}})
    res = run_allocation_backtest_fast(universe, tw)

    # 1. Dict update with mixed keys
    with pytest.raises(NotImplementedError, match="deferral_report"):
        res.update({"valid_dict_key": 999, "deferral_report": []})
    assert "valid_dict_key" not in res

    # 2. Kwargs update with mixed keys
    with pytest.raises(NotImplementedError, match="cash_carry_report"):
        res.update(valid_kw_key=888, cash_carry_report=[])
    assert "valid_kw_key" not in res

    # 3. Iterable of pairs update with mixed keys
    with pytest.raises(NotImplementedError, match="total_market_impact"):
        res.update([("valid_pair_key", 777), ("total_market_impact", 0.0)])
    assert "valid_pair_key" not in res

    # 4. Generator update with mixed keys (ensuring generator replay / non-consumption)
    gen = ((k, v) for k, v in [("valid_gen_key", 666), ("total_cash_interest", 0.0)])
    with pytest.raises(NotImplementedError, match="total_cash_interest"):
        res.update(gen)
    assert "valid_gen_key" not in res

    # Positive control: update with all valid keys succeeds
    res.update({"valid_dict_key": 100}, valid_kw_key=200)
    assert res["valid_dict_key"] == 100
    assert res["valid_kw_key"] == 200

    # Generator with all valid keys succeeds
    valid_gen = ((k, v) for k, v in [("gen_k1", 300), ("gen_k2", 400)])
    res.update(valid_gen)
    assert res["gen_k1"] == 300
    assert res["gen_k2"] == 400


def test_copy_preserves_guard_and_type():
    """`copy()` and `copy.copy()` return FastBacktestResult and preserve all guards."""
    universe = _make_universe(n_days=20, n_sym=2, seed=1)
    tw = _sparse_weights(universe, {0: {"S0": 0.5, "S1": 0.5}})
    res = run_allocation_backtest_fast(universe, tw)

    c1 = res.copy()
    c2 = copy.copy(res)
    c3 = copy.deepcopy(res)

    for copy_obj in (c1, c2, c3):
        assert isinstance(copy_obj, FastBacktestResult)
        assert type(copy_obj) is FastBacktestResult

        # Reads on unported keys still raise
        for key in UNPORTED_PRODUCTION_KEYS:
            with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
                _ = copy_obj[key]
            with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
                copy_obj.get(key)
            with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
                _ = key in copy_obj

            # Writes on unported keys still raise
            with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
                copy_obj[key] = 123
            with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
                copy_obj.setdefault(key, 123)
            with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
                copy_obj.update({key: 123})
            with pytest.raises(NotImplementedError, match=f"does not port production key {key!r}"):
                copy_obj.pop(key, None)

    # Mutations on copies do not affect original
    c1["isolated_test_key"] = 999
    assert c1["isolated_test_key"] == 999
    assert "isolated_test_key" not in res


def test_allowed_keys_write_paths():
    """Valid (non-unported) keys can be written, updated, defaulted, and popped normally."""
    universe = _make_universe(n_days=20, n_sym=2, seed=1)
    tw = _sparse_weights(universe, {0: {"S0": 0.5, "S1": 0.5}})
    res = run_allocation_backtest_fast(universe, tw)

    # __setitem__
    res["custom_attr"] = 42.0
    assert res["custom_attr"] == 42.0

    # setdefault on existing
    val1 = res.setdefault("custom_attr", 99.0)
    assert val1 == 42.0

    # setdefault on new
    val2 = res.setdefault("new_default_attr", 123.0)
    assert val2 == 123.0
    assert res["new_default_attr"] == 123.0

    # pop existing
    popped = res.pop("custom_attr")
    assert popped == 42.0
    assert "custom_attr" not in res

    # pop missing with default
    popped_def = res.pop("custom_attr", "not_found")
    assert popped_def == "not_found"

    # pop missing without default
    with pytest.raises(KeyError):
        res.pop("custom_attr")

