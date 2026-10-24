#!/usr/bin/env python
"""Benchmark: 原生 `run_allocation_backtest` vs. `allocation_backtester_fast`

任务 `B4.1`（Tier P4）的配套压测脚本。**100% 离线**：行情全部来自本脚本内的
确定性合成随机游走，不联网、不读 DuckDB、不碰真实行情。

测什么
------
1. `native_full`  -- 生产 `run_allocation_backtest`（close 模式）整次调用：
   pandas 输入整理 + 逐日 Python 状态机 + DataFrame 输出。这是策略寻优
   真实支付的代价。
2. `fast_full`    -- `run_allocation_backtest_fast` 整次调用：同样的 pandas
   输入整理 + 数组内核 + DataFrame 输出。
3. `fast_kernel`  -- **只**跑 `close_mode_kernel`（入参数组预置、输出
   preallocated），即"纯状态机"的每日本征成本。这一栏才是 numba 值得不值得
   接入的判据；前两栏含 pandas 开销。

每个配置跑 3 次取**中位数**（`REPS = 3`）。报 `μs/日 = 耗时 / n_days`。
精度：把两个实现的 `equity` / `actual_w` 对拍，报最大绝对/相对偏差与成交
笔数、成交股数是否完全一致 —— 加速不值得以任何账面偏差换来。

用法
----
    uv run --offline --project pipeline python scripts/bench_allocation_backtester_fast.py
    uv run --offline --project pipeline python scripts/bench_allocation_backtester_fast.py --quick
"""

import argparse
import os
import platform
import statistics
import sys
import time

import numpy as np
import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.allocation_backtester import run_allocation_backtest
from common.allocation_backtester_fast import (
    JIT_KERNEL,
    KERNEL_VERSION,
    NUMBA_AVAILABLE,
    close_mode_kernel,
    run_allocation_backtest_fast,
)

REPS = 3


def make_universe(n_days: int, n_sym: int, seed: int = 7):
    """确定性合成 OHLCV 宇宙（零网络）。价格量级贴近 A 股/美股常见区间。"""
    idx = pd.bdate_range("2018-01-02", periods=n_days)
    universe = {}
    for k in range(n_sym):
        rng = np.random.default_rng(seed + 131 * k)
        rets = rng.normal(0.0003, 0.013, n_days)
        close = (20.0 + 15.0 * k) * np.exp(np.cumsum(rets))
        high = close * (1.0 + np.abs(rng.normal(0.0, 0.005, n_days)))
        low = close * (1.0 - np.abs(rng.normal(0.0, 0.005, n_days)))
        open_ = close * (1.0 + rng.normal(0.0, 0.002, n_days))
        vol = rng.integers(300_000, 3_000_000, n_days).astype(float)
        # 少量停牌柱，确保压测路径包含 B1.3 的拒成交分支。
        vol[rng.integers(0, n_days, max(1, n_days // 50))] = 0.0
        universe[f"SYM{k:02d}"] = pd.DataFrame(
            {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
            index=idx,
        )
    return universe


def make_targets(universe, every: int, seed: int = 3):
    """稀疏目标权重：每 `every` 个交易日一行指令，把资金摊到全宇宙。

    `every` 决定状态机的负载：24~21 日（月度）时逐日成本几乎全是
    mark-to-market；`every=5`（周度）或 1（每日）时每个 symbol 每天都要走一遍
    候选构建 / 排序 / 成交簿记，这才是 Python 循环真正的开销所在。
    """
    idx = next(iter(universe.values())).index
    syms = list(universe.keys())
    tw = pd.DataFrame(np.nan, index=idx, columns=syms, dtype=float)
    rng = np.random.default_rng(seed)
    for pos in range(0, len(idx), max(int(every), 1)):
        w = rng.dirichlet(np.ones(len(syms))) * 0.98
        tw.loc[idx[pos], :] = w
    return tw


def _time(fn):
    """3 次取中位数（秒）。先热身一次，避免把首次分配算进来。"""
    fn()
    samples = []
    for _ in range(REPS):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def _prepared_args(universe, tw, n_sym):
    """把 wrapper 的入参整理一次性做好，供 `fast_kernel` 单测状态机。"""
    syms = list(universe.keys())
    closes = pd.DataFrame({s: universe[s]["Close"] for s in syms})
    highs = pd.DataFrame({s: universe[s]["High"] for s in syms})
    lows = pd.DataFrame({s: universe[s]["Low"] for s in syms})
    common_idx = closes.index.intersection(tw.index)
    closes = closes.loc[common_idx]
    sparse = tw.reindex(columns=syms).loc[common_idx]
    is_reb = sparse.notna().any(axis=1).to_numpy()
    tgt = sparse.ffill().fillna(0.0).to_numpy(dtype=float)
    vol = pd.DataFrame({s: universe[s]["Volume"] for s in syms}).reindex(common_idx).to_numpy(dtype=float)
    n_days = len(common_idx)
    return dict(
        tgt_w=tgt,
        is_reb=is_reb,
        closes=closes.to_numpy(dtype=float),
        highs=highs.loc[common_idx].to_numpy(dtype=float),
        lows=lows.loc[common_idx].to_numpy(dtype=float),
        mark=closes.ffill().fillna(0.0).to_numpy(dtype=float),
        ret_arr=closes.pct_change().fillna(0.0).to_numpy(dtype=float),
        volume=vol,
        volume_known=np.ones(n_sym, dtype=bool),
        lot_arr=np.full(n_sym, 100, dtype=np.int64),
        lex_rank=np.arange(n_sym, dtype=np.int64),
        china_limit=np.ones(n_sym),
        out_equity=np.zeros(n_days),
        out_actual_w=np.zeros((n_days, n_sym)),
        out_fills=np.zeros((n_sym * (int(is_reb.sum()) + 1) + 1, 16)),
        out_susp=np.zeros((n_sym * (int(is_reb.sum()) + 1) + 1, 6)),
    )


def run_config(n_days: int, n_sym: int, every: int):
    universe = make_universe(n_days, n_sym)
    tw = make_targets(universe, every=every)
    kwargs = dict(china_trading=True)  # 100 股整手 + 涨跌停 + T+1 + 印花税全开

    t_native = _time(lambda: run_allocation_backtest(universe, tw, **kwargs))
    t_fast = _time(lambda: run_allocation_backtest_fast(universe, tw, **kwargs))

    p = _prepared_args(universe, tw, n_sym)
    kernel_fn = JIT_KERNEL if JIT_KERNEL is not None else close_mode_kernel

    def _kernel_only():
        p["out_equity"][:] = 0.0
        p["out_actual_w"][:] = 0.0
        kernel_fn(
            p["tgt_w"], p["is_reb"], p["closes"], p["highs"], p["lows"],
            p["mark"], p["ret_arr"], p["volume"], p["volume_known"],
            p["lot_arr"], p["lex_rank"], p["china_limit"],
            100_000.0, 0.0005, 0.0005, 0.0,
            True, False, False, 0.0005, 0.001085, 0.0000278,
            p["out_equity"], p["out_actual_w"], p["out_fills"], p["out_susp"],
        )

    t_kernel = _time(_kernel_only)

    # ---- 精度对拍 ---- #
    ref = run_allocation_backtest(universe, tw, **kwargs)
    fast = run_allocation_backtest_fast(universe, tw, **kwargs)
    ref_eq = ref["equity_curve"]["equity"].to_numpy(dtype=float)
    fast_eq = fast["equity_curve"]["equity"].to_numpy(dtype=float)
    abs_dev = float(np.max(np.abs(fast_eq - ref_eq)))
    with np.errstate(divide="ignore", invalid="ignore"):
        rel_dev = float(np.max(np.abs(fast_eq - ref_eq) / np.maximum(np.abs(ref_eq), 1e-12)))
    w_dev = float(np.max(np.abs(
        fast["actual_weights"].to_numpy(dtype=float) - ref["actual_weights"].to_numpy(dtype=float)
    )))
    shares_equal = (
        len(ref["rebalance_report"]) == len(fast["rebalance_report"])
        and (
            len(ref["rebalance_report"]) == 0
            or np.array_equal(
                ref["rebalance_report"]["shares"].to_numpy(dtype=float),
                fast["rebalance_report"]["shares"].to_numpy(dtype=float),
            )
        )
    )
    return {
        "every": every,
        "n_days": n_days,
        "n_sym": n_sym,
        "cells": n_days * n_sym,
        "native_s": t_native,
        "fast_s": t_fast,
        "kernel_s": t_kernel,
        "native_us_day": t_native / n_days * 1e6,
        "fast_us_day": t_fast / n_days * 1e6,
        "kernel_us_day": t_kernel / n_days * 1e6,
        "speedup_full": t_native / t_fast,
        "speedup_kernel": t_native / t_kernel,
        "abs_dev": abs_dev,
        "rel_dev": rel_dev,
        "w_dev": w_dev,
        "n_fills": len(ref["rebalance_report"]),
        "shares_equal": shares_equal,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="只跑两个小配置")
    args = ap.parse_args()

    if args.quick:
        configs = [(250, 5), (1000, 20)]
    else:
        configs = [(250, 5), (1000, 10), (2000, 30), (4000, 50)]

    print("=" * 104)
    print("B4.1 benchmark: native run_allocation_backtest vs. allocation_backtester_fast (close mode)")
    print("=" * 104)
    print(f"python     : {platform.python_version()} ({platform.machine()})")
    print(f"numpy      : {np.__version__}   pandas: {pd.__version__}")
    print(f"numba      : {'AVAILABLE ' + __import__('numba').__version__ if NUMBA_AVAILABLE else 'NOT INSTALLED -> NumPy kernel (no JIT)'}")
    print(f"kernel     : {'numba.njit' if JIT_KERNEL is not None else 'plain array kernel'}  v{KERNEL_VERSION}")
    print(f"n_jit      : {1 if JIT_KERNEL is not None else 0}   (first-call compile cost: "
          f"{'billed by numba on first invocation, excluded via warmup' if JIT_KERNEL is not None else 'n/a, no JIT'})")
    print(f"cpu cores  : {os.cpu_count()}   reps: {REPS} (median)")
    print("trading    : china_trading=True (100-share lots, limits, T+1, stamp duty)")
    print("regimes    : 'every' = rebalance instruction every N sessions (21 = ~monthly, 1 = daily)")
    print()
    header = (f"{'every':>5} {'n_days':>7} {'n_sym':>6} {'cells':>8} "
              f"{'native s':>9} {'fast s':>8} {'kernel s':>9} "
              f"{'native us/d':>12} {'fast us/d':>10} {'kernel us/d':>12} "
              f"{'x full':>7} {'x kern':>7} {'max|dev|':>10} {'fills':>6} {'shares==':>9}")
    print(header)
    print("-" * len(header))
    rows = []
    for n_days, n_sym in configs:
        for every in (21, 1):
            r = run_config(n_days, n_sym, every)
            rows.append(r)
            print(f"{r['every']:>5} {r['n_days']:>7} {r['n_sym']:>6} {r['cells']:>8} "
                  f"{r['native_s']:>9.4f} {r['fast_s']:>8.4f} {r['kernel_s']:>9.4f} "
                  f"{r['native_us_day']:>12.2f} {r['fast_us_day']:>10.2f} {r['kernel_us_day']:>12.2f} "
                  f"{r['speedup_full']:>7.2f} {r['speedup_kernel']:>7.2f} "
                  f"{r['abs_dev']:>10.2e} {r['n_fills']:>6} {str(r['shares_equal']):>9}")
    print()
    worst_dev = max(r["abs_dev"] for r in rows)
    print(f"precision: max |equity dev| = {worst_dev:.3e} over {len(rows)} configs; "
          f"all share counts identical = {all(r['shares_equal'] for r in rows)}")
    print(f"  (rtol=1e-12 contract from the test suite; 0.0 means bit-identical ledger)")
    print()
    def _med(key, every):
        vals = [r[key] for r in rows if r["every"] == every]
        return statistics.median(vals) if vals else float("nan")

    for every, label in ((21, "sparse ~monthly rebalance"), (1, "dense daily rebalance")):
        print(f"median speedup [{label}]: full-call x{_med('speedup_full', every):.2f}, "
              f"kernel-only x{_med('speedup_kernel', every):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
