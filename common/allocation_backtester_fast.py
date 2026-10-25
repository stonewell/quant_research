"""EXPERIMENTAL / 未接入生产 -- allocation-backtester state-machine kernel.

任务 `B4.1`（Tier P4）：把 `common/allocation_backtester.run_allocation_backtest`
的核心**逐日账本状态机**抽取成一个纯数组函数 `close_mode_kernel`，用于压测
"原生 Python 循环 vs. 数组化内核"的加速比与精度误差，供后续极端大规模寻优
（`--optimize` 网格 / ERS 上千次抽样）评估是否值得接入。

=====================================================================
EXPERIMENTAL -- 未接入生产，且默认不被任何生产路径 import
=====================================================================
本模块是**试验品**，请先读完这一节再使用：

* `common/allocation_backtester.py` 里**没有任何一行**引用本模块（单测
  `test_fast_kernel_is_not_imported_by_production_backtester` 做静态断言，
  防止将来有人图省事把它悄悄接进生产路径）。
* 唯一的一处反向依赖是**纯规则查表**：`_get_china_price_limit` /
  `_get_hk_board_lot` / `HK_COMMON_BOARD_LOTS` 从生产模块 import 进来。它们是
  确定性的、无状态的涨跌停板宽 / 每手股数查表，复制一份只会引入"两套板宽
  逐渐漂移"的维护风险；且**不在热循环里**（每个 symbol 只调用一次，用于
  预计算数组）。账本状态机本身没有从生产模块借任何代码。
* 本模块**只实现 `execution_price="close"` 模式**（B3.1 之前的经典引擎，也是
  所有模板/策略的默认路径）。`next_open` / `next_vwap` / `intraday_twap` 的
  挂单队列、`market_impact`、`max_volume_participation` 一律**显式
  `raise ValueError`**，不静默降级 -- 一个"看起来跑通了但其实换了撮合价格"
  的回测正是这些模式存在的理由。
* 覆盖的语义（与生产 B1.1 / B1.3 账本逐条对齐）：现金账本 `cash` +
  `held_shares`、`equity[t] = cash + held@mark[t]`、`actual_w` 由持仓反算、
  整手取整（`_lot_round`）、现金不足的部分成交（买侧按 `cash/(1+rate)` 截断
  且**不重试**）、停牌（`Volume<=0` / NaN）双向拒成交、`min_weight_change`
  被动漂移抑制、A 股 T+1 与涨跌停、美股 SEC 费、港股双边印花税、
  `cash >= 0` 与 `sum(actual_w) <= 1`。

=====================================================================
KERNEL LAYOUT -- 为什么"数组化"就能快
=====================================================================
生产循环在每个 rebalance 日、每个 symbol 上的开销几乎都不是算术，而是簿记：
`held_shares[sym]` / `settled_shares[sym]` 两次 dict 查找、一个 candidate
dict 字面量、`sorted(..., key=lambda c: (-abs(c["delta_w"]), c["symbol"]))`
的一次闭包排序、每个成交再一个 15 键 dict 外加一次 `strftime` 日期字符串。
本内核把这些全部换成：

* `held` / `settled` 变成两个 float64 向量（下标即列号），无 dict；
* 候选只存进 3 个 preallocated 数组（列号 / delta_w / trade_s），插入排序；
  `lex_rank` 把 `c["symbol"]` 的字符串 tie-break 换成整数 tie-break，结果与
  CPython 的 `(-abs(delta_w), symbol)` 排序**完全一致**（见下方注释）；
* 成交写进 preallocated `(max_fills, 16)` 矩阵，日期字符串在 wrapper 里统一
  `strftime`（账本不碰字符串）；
* `actual_w` 直接写进输出视图，不建临时 DataFrame。

`close_mode_kernel` 被刻意写成 **"numba 可 lift"** 的形态：入参只有
`np.ndarray` + Python 标量，内部只用 `float()` / `np.dot` / `math.floor` /
`math.isfinite` / 下标循环，没有 dict、没有 pandas、没有 `sorted(key=)`。
（逐 symbol 的标量判断一律用 `math.*` 而非 `np.*`：语义相同，但前者是 C 标量
调用 ~30ns、后者是 ufunc 包装 ~300ns，在 200k 次/回测的路径上是实测 2~3 倍的
差别 -- 见压测报告的 dense-rebalance 一栏。）
若 `numba` 可用，`JIT_KERNEL` 就是 `numba.njit` 版本；本仓库当前的 `pipeline`
离线环境**没有 numba**（`import numba` -> ModuleNotFoundError），因此实测数字
全部来自**纯 NumPy/CPython 标量数组内核**（`JIT_KERNEL is None`）。这里不
假装做过 JIT：压测报告里 `n_jit = 0`。

=====================================================================
PRECISION CONTRACT
=====================================================================
目标是与 `run_allocation_backtest` 的 `equity` / `actual_w` / 成交股数
**逐位相等**（断言用 `rtol=1e-12`，见测试里的 `EQUIV_RTOL`），而不是"大致
相等"。做法是**逐条复刻浮点运算顺序**：

* 每笔现金流都是 `cash -= (new_s - prior_s) * price + total_cost`，与生产
  完全相同的结合顺序（先乘后加；`total_cost = comm + slip + stamp` 从左到右）；
* `_entry_need` 的 8 轮 day-0 缩量循环按**列表顺序**左折叠累加（`s = s + cf`
  再取负），与 CPython `sum()` 的求和树逐项一致；
* `drifted_w` / `actual_w` 用 `held * mark[t] / eq` 的同一表达式、同一顺序；
* `ret_arr` / `mark_arr` 直接由**同一批 pandas 表达式**产出
  （`closes.pct_change().fillna(0.0)`、`closes.ffill().fillna(0.0)`），不去
  重新推导 pandas 的 NaN 语义。

理论上唯一可能分叉的是 `int(round(raw, 6))`：CPython 的 `round(float, 6)`
是十进制正确舍入，而 `np.round` 是 `rint(x*1e6)/1e6` 的缩放式，二者只在
`raw` 距 6 位小数的 `.5` 边界 1 ulp 内才有分歧。本内核**显式 `float(raw)` +
CPython `round`**（不是 `np.round`），所以这一点也不分叉。实测最大偏差
0.0（逐位相等），见 `test_kernel_matches_production_bitwise`。
"""

from __future__ import annotations

import logging
from math import floor as _math_floor
from math import isfinite as _math_isfinite
from math import isnan as _math_isnan
from typing import Dict, Optional

import numpy as np
import pandas as pd

# 纯规则查表（涨跌停板宽 / 港股每手股数）复用生产实现，避免两套板宽漂移。
# 这两个函数是确定性的无状态查表，不在热循环内；账本状态机没有借生产的代码。
from common.allocation_backtester import (
    HK_COMMON_BOARD_LOTS,  # noqa: F401  (re-exported for callers building HK fixtures)
    _get_china_price_limit,
    _get_hk_board_lot,
)

_logger = logging.getLogger(__name__)

KERNEL_VERSION = "0.1.0"
KERNEL_STATUS = "experimental"

# ---- numba 可选加速 ---------------------------------------------------- #
# 缺失时**降级**为纯数组内核（不是"假装 JIT"）。绝不在这里安装依赖。
try:  # pragma: no cover - environment dependent
    import numba as _numba

    NUMBA_AVAILABLE = True
except Exception:  # pragma: no cover - environment dependent
    _numba = None
    NUMBA_AVAILABLE = False

# 现金浮点松弛，与生产模块同一个量级约定。
_CASH_FLOAT_TOL = 1e-9

# 成交记录矩阵的列序（纯 float64，便于 numba）。symbol 存列号，day 存行号。
FILL_COLUMNS = [
    "day",
    "symbol",
    "action",
    "price",
    "prior_weight",
    "target_weight",
    "weight_change",
    "trade_value",
    "shares",
    "prior_shares",
    "target_shares",
    "commission",
    "slippage",
    "total_cost",
    "portfolio_equity",
    "rebalance_id",
]
_N_FILL_COLS = len(FILL_COLUMNS)

# 停牌记录矩阵：day, sym_idx, action, target_w, reason_code, volume
_N_SUSP_COLS = 6
_SUSP_ZERO = 0.0
_SUSP_MISSING = 1.0


def _lot_round(raw_shares: float, lot: int) -> float:
    """`raw_shares` 在 `lot` 手数下的股数（逐位复刻生产 `_lot_round`）。

    lot==0 -> 分数股原样；lot==1 -> 整数股；lot>1 -> 整手向下取整。
    `round` 走 CPython 的 float 重载（十进制正确舍入），不是 `np.round` 的
    缩放式 `rint`，否则在 6 位小数 `.5` 边界附近会与生产分叉。
    """
    if lot == 0:
        return float(raw_shares)
    rounded = int(round(float(raw_shares), 6))
    if lot > 1:
        return float((rounded // lot) * lot)
    return float(rounded)


def _tradable(shares: float, lot: int) -> bool:
    """与生产同一个可成交性谓词。"""
    return shares > 1e-9 if lot == 0 else shares >= lot


def _write_fill(out_fills, k, day, sym_i, action, price, prior_w, target_w,
                delta_w, trade_val, trade_s, prior_s, new_s, comm, slip,
                total_cost, equity_mark, rebalance_id):
    """把一笔成交写进 preallocated 矩阵，返回下一个空槽（numba 友好）。"""
    out_fills[k, 0] = float(day)
    out_fills[k, 1] = float(sym_i)
    out_fills[k, 2] = float(action)
    out_fills[k, 3] = price
    out_fills[k, 4] = prior_w
    out_fills[k, 5] = target_w
    out_fills[k, 6] = delta_w
    out_fills[k, 7] = trade_val
    out_fills[k, 8] = trade_s
    out_fills[k, 9] = prior_s
    out_fills[k, 10] = new_s
    out_fills[k, 11] = comm
    out_fills[k, 12] = slip
    out_fills[k, 13] = total_cost
    out_fills[k, 14] = equity_mark
    out_fills[k, 15] = float(rebalance_id)
    return k + 1


def close_mode_kernel(
    tgt_w,
    is_reb,
    closes,
    highs,
    lows,
    mark,
    ret_arr,
    volume,
    volume_known,
    lot_arr,
    lex_rank,
    china_limit_pcts,
    initial_capital,
    commission_pct,
    slippage_pct,
    min_weight_change,
    china_trading,
    us_trading,
    hk_trading,
    stamp_duty_pct,
    hk_stamp_duty_pct,
    sec_fee_pct,
    out_equity,
    out_actual_w,
    out_fills,
    out_susp,
):
    """`execution_price == "close"` 的逐日账本状态机（纯数组，无 pandas）。

    所有入参都是 `np.ndarray` / Python 标量；`out_*` 由调用方预分配。
    返回 `(n_fills, n_susp, total_turnover, rebalance_id)`。

    逐条对齐生产 `run_allocation_backtest` 的 close 分支：day-0 建仓（含 8 轮
    现金缩放 + 整手削减兜底）、每日收盘 mark-to-market、rebalance 日先卖后买、
    费用/印花税、停牌双向拒绝、A 股 T+1。**不做**任何它没做的事：不重试现金
    不足的买单、不在停牌日按最后一个价"幽灵成交"、不把 `actual_w` 改写成目标
    权重。
    """
    n_days = tgt_w.shape[0]
    n_sym = tgt_w.shape[1]

    held = np.zeros(n_sym)
    settled = np.zeros(n_sym)

    def lot_round(raw, lot):
        if lot == 0:
            return float(raw)
        rounded = int(round(float(raw), 6))
        if lot > 1:
            return float((rounded // lot) * lot)
        return float(rounded)

    def tradable(shares, lot):
        return shares > 1e-9 if lot == 0 else shares >= lot

    def susp_code(row, i):
        """0 = 可交易；1 = volume_missing；2 = volume_zero（与生产同一判据）。"""
        if not volume_known[i]:
            return 0
        v = float(row[i])
        if not _math_isfinite(v):
            return 1
        if v <= 0.0:
            return 2
        return 0

    def cost_rate_for(target_w):
        """与生产 `_cost_rate_for` 同序相加（commission+slip 后再加地区税）。"""
        rate = commission_pct + slippage_pct
        if hk_trading:
            rate += hk_stamp_duty_pct
        elif china_trading and target_w < 0:
            rate += stamp_duty_pct
        elif us_trading and target_w < 0:
            rate += sec_fee_pct
        return rate

    # ------------------------------------------------------------------ #
    # DAY 0 -- 初始建仓
    # ------------------------------------------------------------------ #
    cash = float(initial_capital)
    o_idx = []
    o_w = []
    o_price = []
    o_s = []
    o_lot = []
    n_susp = 0
    k_fill = 0

    for i in range(n_sym):
        target_w = float(tgt_w[0, i])
        if abs(target_w) <= 1e-7:
            continue
        code = susp_code(volume[0], i)
        if code != 0:
            out_susp[n_susp, 0] = 0.0
            out_susp[n_susp, 1] = float(i)
            out_susp[n_susp, 2] = 1.0 if target_w > 0 else -1.0
            out_susp[n_susp, 3] = target_w
            out_susp[n_susp, 4] = 1.0 if code == 1 else 0.0
            out_susp[n_susp, 5] = float(volume[0, i]) if volume_known[i] else float("nan")
            n_susp += 1
            continue
        price = float(closes[0][i])
        if _math_isnan(price) or not (price > 0):
            continue
        lot = int(lot_arr[i])
        target_s = lot_round((abs(target_w) * initial_capital) / price, lot)
        if not tradable(target_s, lot):
            continue
        o_idx.append(i)
        o_w.append(target_w)
        o_price.append(price)
        o_s.append(target_s)
        o_lot.append(lot)

    long_cost_rate = cost_rate_for(1.0)
    buy_cost_rate = long_cost_rate

    def entry_need():
        """净现金消耗（含费用）。与生产 `_entry_need` 同序：左折叠后取负。"""
        s = 0.0
        for k in range(len(o_idx)):
            value = float(o_s[k]) * float(o_price[k])
            rate = cost_rate_for(o_w[k])
            if o_w[k] > 0:
                s = s + (-value * (1.0 + rate))
            else:
                s = s + (value * (1.0 - rate))
        return -s

    cash_tolerance = _CASH_FLOAT_TOL * max(1.0, abs(cash))

    for _attempt in range(8):
        if entry_need() - cash <= cash_tolerance:
            break
        credit_net = 0.0
        long_notional = 0.0
        for k in range(len(o_idx)):
            value = float(o_s[k]) * float(o_price[k])
            if o_w[k] < 0:
                rate = cost_rate_for(o_w[k])
                credit_net = credit_net + value * (1.0 - rate)
            if o_w[k] > 0:
                long_notional = long_notional + value
        if long_notional <= 0:
            break
        budget = max(0.0, (cash + credit_net) / (1.0 + long_cost_rate))
        scale = budget / long_notional
        keep = ([], [], [], [], [])
        for k in range(len(o_idx)):
            if o_w[k] > 0:
                scaled_s = lot_round(o_s[k] * scale, o_lot[k])
                if not tradable(scaled_s, o_lot[k]):
                    continue
                keep[0].append(o_idx[k])
                keep[1].append(o_w[k])
                keep[2].append(o_price[k])
                keep[3].append(scaled_s)
                keep[4].append(o_lot[k])
            else:
                keep[0].append(o_idx[k])
                keep[1].append(o_w[k])
                keep[2].append(o_price[k])
                keep[3].append(o_s[k])
                keep[4].append(o_lot[k])
        o_idx, o_w, o_price, o_s, o_lot = keep

    # 整手粒度兜底：从最大的多头订单上逐手削减，直到买得起。严格大于 =>
    # 与 Python `max(..., key=...)` 一样在并列时取列表里第一个极大值。
    guard = 0
    while entry_need() - cash > cash_tolerance and guard < 10000:
        guard += 1
        best_k = -1
        best_val = -1.0
        for k in range(len(o_idx)):
            if o_w[k] > 0:
                v = float(o_s[k]) * float(o_price[k])
                if v > best_val:
                    best_val = v
                    best_k = k
        if best_k < 0:
            break
        step = max(int(o_lot[best_k]), 1)
        new_s = o_s[best_k] - step
        if not tradable(new_s, o_lot[best_k]):
            del o_idx[best_k]
            del o_w[best_k]
            del o_price[best_k]
            del o_s[best_k]
            del o_lot[best_k]
        else:
            o_s[best_k] = new_s

    day0_fills = 0
    day0_notional = 0.0
    for k in range(len(o_idx)):
        i = o_idx[k]
        target_w = o_w[k]
        price = o_price[k]
        trade_s = o_s[k]
        held_s = trade_s if target_w >= 0 else -trade_s
        held[i] = held_s
        trade_val = float(trade_s * price)
        comm = float(trade_val * commission_pct)
        slip = float(trade_val * slippage_pct)
        if hk_trading:
            stamp = float(trade_val * hk_stamp_duty_pct)
        elif china_trading:
            stamp = float(trade_val * stamp_duty_pct) if target_w < 0 else 0.0
        elif us_trading:
            stamp = float(trade_val * sec_fee_pct) if target_w < 0 else 0.0
        else:
            stamp = 0.0
        total_cost = comm + slip + stamp
        cash -= held_s * price
        cash -= total_cost
        day0_fills += 1
        day0_notional += trade_val
        k_fill = _write_fill(
            out_fills, k_fill, 0, i, 1.0 if target_w > 0 else -1.0, price,
            0.0, target_w, target_w, trade_val, trade_s, 0.0, held_s,
            comm, slip, total_cost, float(initial_capital), 1.0,
        )

    total_turnover = 0.0
    rebalance_id = 0
    if day0_fills:
        rebalance_id = 1
        total_turnover += day0_notional / initial_capital

    eq0 = cash + float(np.dot(held, mark[0]))
    out_equity[0] = eq0
    if eq0 > 0:
        out_actual_w[0] = held * mark[0] / eq0

    # 结算股数镜像：切片赋值走 C 循环，不用 Python 逐 symbol 拷贝。
    settled[:] = held

    # ------------------------------------------------------------------ #
    # DAY 1..T-1 -- 逐日 mark-to-market + rebalance 日先卖后买
    # ------------------------------------------------------------------ #
    buf = np.empty(n_sym)
    cand_i = np.empty(n_sym, dtype=np.int64)
    cand_dw = np.empty(n_sym)
    cand_s = np.empty(n_sym)
    order = np.empty(n_sym, dtype=np.int64)

    for t in range(1, n_days):
        # 行视图（view，不拷贝）：`arr[t, i]` 的二维下标在 200k 次符号-日上是
        # 可观测开销，取一次行视图再 `row[i]` 便宜得多，且逐位等价。
        tgt_t = tgt_w[t]
        close_t = closes[t]
        high_t = highs[t]
        low_t = lows[t]
        ret_t = ret_arr[t]
        vol_t = volume[t]
        mark_t = mark[t]

        pre = cash + float(np.dot(held, mark_t))
        out_equity[t] = pre

        has_pre = pre > 0.0
        if has_pre:
            np.multiply(held, mark_t, out=buf)
            np.divide(buf, pre, out=buf)
            drifted_w = buf
        else:
            drifted_w = np.zeros(n_sym)

        if is_reb[t] and has_pre:
            n_cand = 0
            for i in range(n_sym):
                prior_s = float(held[i])
                prior_w = float(drifted_w[i])
                target_w = float(tgt_t[i])
                delta_w = target_w - prior_w
                if abs(delta_w) <= 1e-7:
                    continue

                code = susp_code(vol_t, i)
                if code != 0:
                    out_susp[n_susp, 0] = float(t)
                    out_susp[n_susp, 1] = float(i)
                    out_susp[n_susp, 2] = 1.0 if delta_w > 0 else -1.0
                    out_susp[n_susp, 3] = target_w
                    out_susp[n_susp, 4] = 1.0 if code == 1 else 0.0
                    out_susp[n_susp, 5] = float(vol_t[i]) if volume_known[i] else float("nan")
                    n_susp += 1
                    continue

                price = float(close_t[i])
                if _math_isnan(price) or not (price > 0):
                    continue

                if china_trading:
                    limit_pct = float(china_limit_pcts[i])
                    if limit_pct < 1.0:
                        ret_i = float(ret_t[i])
                        p_high = float(high_t[i])
                        p_low = float(low_t[i])
                        if delta_w > 0 and (ret_i >= limit_pct - 0.005) and (p_high - price <= 1e-4 * price):
                            continue
                        if delta_w < 0 and (ret_i <= -limit_pct + 0.005) and (price - p_low <= 1e-4 * price):
                            continue

                lot = int(lot_arr[i])

                if abs(target_w) < 1e-7 and prior_s != 0:
                    # 全平永远允许（清掉零股）。
                    trade_s = abs(prior_s)
                    action = -1 if prior_s > 0 else 1
                elif abs(target_w) < 1e-7 and prior_s == 0:
                    continue
                else:
                    if min_weight_change > 0.0 and prior_s != 0 and abs(delta_w) < min_weight_change:
                        continue
                    raw_s = (abs(delta_w) * pre) / price
                    trade_s = lot_round(raw_s, lot)
                    if not tradable(trade_s, lot):
                        continue
                    action = 1 if delta_w > 0 else -1

                if china_trading and action == -1:
                    avail_to_sell = max(0.0, float(settled[i]))
                    trade_s = min(trade_s, avail_to_sell)
                    if trade_s <= 0:
                        continue

                cand_i[n_cand] = i
                cand_dw[n_cand] = delta_w
                cand_s[n_cand] = trade_s
                n_cand += 1

            n_fills = 0
            rebal_notional = 0.0

            # 排序键 = 生产 `(-abs(delta_w), symbol)`。`lex_rank` 是 symbol 在
            # `sorted(symbols)` 中的名次（唯一），故 (|delta_w| 升序, lex_rank
            # 升序) 是一个**严格全序**，任何正确排序给出的置换都相同 => 与
            # CPython 的 sorted 结果逐位一致，且不必在热循环里比较字符串。
            for a in range(n_cand):
                order[a] = a
            for a in range(1, n_cand):
                key = order[a]
                b = a - 1
                kd = abs(cand_dw[key])
                while b >= 0:
                    kb = order[b]
                    # 生产键是 `-|delta_w|` 升序 => `|delta_w|` **降序**为主键，
                    # symbol 字典序为次键。
                    if abs(cand_dw[kb]) < kd or (
                        abs(cand_dw[kb]) == kd
                        and int(lex_rank[cand_i[kb]]) > int(lex_rank[cand_i[key]])
                    ):
                        order[b + 1] = kb
                        b -= 1
                    else:
                        break
                order[b + 1] = key

            # ---- SELL 先执行（释放现金） ---- #
            for pos in range(n_cand):
                c = int(order[pos])
                if cand_dw[c] > 0:
                    continue
                i = int(cand_i[c])
                delta_w = float(cand_dw[c])
                prior_s = float(held[i])
                prior_w = float(drifted_w[i])
                target_w = float(tgt_t[i])
                price = float(close_t[i])
                trade_s = float(cand_s[c])
                # (B3.5 对齐) 卖侧符号与生产**逐字相同**：SELL 对**任何**先验符号
                # 都是负的股数增量，`new_s = prior_s - trade_s` 无条件成立。
                # `|prior_s|` 才是多头专用的守卫（卖不能靠没持有的股票融资到空翻
                # 多）；空头无需截断，加空只会释放现金。旧的
                # `prior_s + trade_s if prior_s <= 0` 会把"从 0 开空"变成
                # `+trade_s` 的多头、"空头加仓"变成回补，且现金流符号一并反转。
                trade_s = min(trade_s, prior_s) if prior_s > 0 else trade_s
                if trade_s <= 0:
                    continue
                new_s = prior_s - trade_s
                trade_val = float(trade_s * price)
                comm = float(trade_val * commission_pct)
                slip = float(trade_val * slippage_pct)
                if hk_trading:
                    stamp = float(trade_val * hk_stamp_duty_pct)
                elif china_trading:
                    stamp = float(trade_val * stamp_duty_pct)
                elif us_trading:
                    stamp = float(trade_val * sec_fee_pct)
                else:
                    stamp = 0.0
                total_cost = comm + slip + stamp
                cash -= (new_s - prior_s) * price + total_cost
                held[i] = new_s
                settled[i] = max(0.0, settled[i] - trade_s)
                n_fills += 1
                rebal_notional += trade_val
                k_fill = _write_fill(
                    out_fills, k_fill, t, i, -1.0, price, prior_w, target_w,
                    delta_w, trade_val, trade_s, prior_s, new_s, comm, slip,
                    total_cost, pre, rebalance_id + 1.0,
                )

            # ---- BUY 再执行，对着真实余额 ---- #
            for pos in range(n_cand):
                c = int(order[pos])
                if cand_dw[c] <= 0:
                    continue
                i = int(cand_i[c])
                delta_w = float(cand_dw[c])
                prior_s = float(held[i])
                prior_w = float(drifted_w[i])
                target_w = float(tgt_t[i])
                price = float(close_t[i])
                lot = int(lot_arr[i])
                trade_s = float(cand_s[c])

                # 现金不足即部分成交（B1.1：费用从同一笔余额里出，故买得起的
                # 名义金额是 cash/(1+rate) 而非 cash；floor 保证最后一股不越界）。
                avail_cash = max(0.0, cash)
                unit_cash = price * (1.0 + buy_cost_rate)
                if lot == 0:
                    max_lots_s = avail_cash / unit_cash
                else:
                    max_affordable_s = int(_math_floor(avail_cash / unit_cash))
                    max_lots_s = (max_affordable_s // lot) * lot if lot > 1 else max_affordable_s
                trade_s = min(trade_s, float(max_lots_s))
                if not tradable(trade_s, lot):
                    # 现金受限的买单是 FINAL（B3.1 规则），不重试。
                    continue
                new_s = prior_s + trade_s
                trade_val = float(trade_s * price)
                comm = float(trade_val * commission_pct)
                slip = float(trade_val * slippage_pct)
                stamp = float(trade_val * hk_stamp_duty_pct) if hk_trading else 0.0
                total_cost = comm + slip + stamp
                cash -= (new_s - prior_s) * price + total_cost
                held[i] = new_s
                n_fills += 1
                rebal_notional += trade_val
                k_fill = _write_fill(
                    out_fills, k_fill, t, i, 1.0, price, prior_w, target_w,
                    delta_w, trade_val, trade_s, prior_s, new_s, comm, slip,
                    total_cost, pre, rebalance_id + 1.0,
                )

            if n_fills:
                rebalance_id += 1
                eq_t = cash + float(np.dot(held, mark_t))
                out_equity[t] = eq_t
                total_turnover += rebal_notional / pre
                if eq_t > 0:
                    out_actual_w[t] = held * mark_t / eq_t
            else:
                out_actual_w[t] = drifted_w
        else:
            out_actual_w[t] = drifted_w

        settled[:] = held

    return k_fill, n_susp, total_turnover, rebalance_id


# numba 可用时把它真正 JIT 掉；不可用时**不伪造**，暴露 None。
if NUMBA_AVAILABLE:  # pragma: no cover - 当前离线环境没有 numba
    try:
        JIT_KERNEL = _numba.njit(cache=False)(close_mode_kernel)
    except Exception:  # pragma: no cover
        _logger.warning(
            "numba is importable but njit(close_mode_kernel) failed; falling back to the "
            "plain array kernel (no JIT)."
        )
        JIT_KERNEL = None
else:
    JIT_KERNEL = None

_ACTIVE_KERNEL = JIT_KERNEL if JIT_KERNEL is not None else close_mode_kernel

UNPORTED_PRODUCTION_KEYS = frozenset({
    "deferral_report",
    "cash_carry_report",
    "total_market_impact",
    "total_cash_interest",
    "total_borrow_fees",
    "total_borrow_fees_unpaid",
})


class FastBacktestResult(dict):
    """Result dictionary from `run_allocation_backtest_fast` (tasks B5.3, B6.2 & B6.4).

    Guards against schema drift by raising NotImplementedError when accessing,
    writing, or copying unported production keys that exist in
    `run_allocation_backtest` but are not supported by the fast array kernel.
    Every ordinary read on the instance (`res[key]`, `res.get(key)`, `key in res`)
    and every ordinary write operator/method on the instance (`res[key] = val`,
    `res.setdefault(...)`, `res.update(...)`, `res.pop(...)`, `res |= other`,
    `res | other`-into-a-copy, `res.copy()`/`copy.copy(res)`) is guarded.

    Documented RESIDUAL bypasses -- a `dict` subclass cannot intercept the builtin
    `dict` C descriptors, so these still reach the underlying storage; this is
    disclosure, NOT full closure:
      * explicit unbound-dict calls -- `dict.update(res, ...)`,
        `dict.__setitem__(res, ...)`, `dict.__ior__(res, ...)`,
        `dict.setdefault(res, ...)`;
      * `plain_dict | res`, which resolves on the LEFT operand (`dict.__or__`) and
        yields a plain `dict`, i.e. the guard type is dropped -- `__ror__` is
        deliberately not implemented for this reason (it would only cover the
        plain-dict-left case whilst still leaking a plain result);
      * the raw view after one of the above has already injected a key:
        `repr(res)`, `iter(res)`, `dict.keys/items/values(res)` are dict-level
        and do not consult the guards (the injection happens at the unbound
        call, so this bullet is a visibility caveat, not a second hole).
    (`FastBacktestResult.fromkeys(...)` was also suspected; measured on
    CPython 3.9-3.14 it routes through `PyObject_SetItem` for dict subclasses and
    DOES raise -- not a bypass -- and `test_fromkeys_still_rejects_unported_keys`
    now pins that so a future refactor cannot silently introduce one.)
    """

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.update(*args, **kwargs)

    def __getitem__(self, key):
        if key in UNPORTED_PRODUCTION_KEYS:
            raise NotImplementedError(
                f"run_allocation_backtest_fast does not port production key {key!r}; "
                f"use run_allocation_backtest for full production reports."
            )
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in UNPORTED_PRODUCTION_KEYS:
            raise NotImplementedError(
                f"run_allocation_backtest_fast does not port production key {key!r}; "
                f"use run_allocation_backtest for full production reports."
            )
        return super().get(key, default)

    def __contains__(self, key):
        # Without this override, `"deferral_report" in res` fell through to dict's
        # C-level membership test and answered False while `res["deferral_report"]`
        # and `res.get("deferral_report")` raised -- i.e. `in` DISAGREED with both,
        # so the exact drift guard B5.3 installed could be bypassed by a `has_key`
        # style probe (`if "deferral_report" in res: ...` then never touching []).
        # `dict.__contains__` (not `super().__contains__`) keeps this off the
        # `__getitem__` path and makes the recursion impossible in principle.
        if key in UNPORTED_PRODUCTION_KEYS:
            raise NotImplementedError(
                f"run_allocation_backtest_fast does not port production key {key!r}; "
                f"use run_allocation_backtest for full production reports."
            )
        return dict.__contains__(self, key)

    def __setitem__(self, key, value):
        if key in UNPORTED_PRODUCTION_KEYS:
            raise NotImplementedError(
                f"run_allocation_backtest_fast does not port production key {key!r}; "
                f"use run_allocation_backtest for full production reports."
            )
        super().__setitem__(key, value)

    def setdefault(self, key, default=None):
        if key in UNPORTED_PRODUCTION_KEYS:
            raise NotImplementedError(
                f"run_allocation_backtest_fast does not port production key {key!r}; "
                f"use run_allocation_backtest for full production reports."
            )
        return super().setdefault(key, default)

    def update(self, *args, **kwargs):
        keys_to_check = set(kwargs.keys())
        if args:
            if len(args) > 1:
                raise TypeError(f"update expected at most 1 argument, got {len(args)}")
            other = args[0]
            if hasattr(other, "keys"):
                keys_to_check.update(other.keys())
            else:
                other_list = list(other)
                for item in other_list:
                    try:
                        keys_to_check.add(item[0])
                    except (IndexError, TypeError):
                        try:
                            k, _ = item
                            keys_to_check.add(k)
                        except Exception:
                            pass
                args = (other_list,)
        for k in keys_to_check:
            if k in UNPORTED_PRODUCTION_KEYS:
                raise NotImplementedError(
                    f"run_allocation_backtest_fast does not port production key {k!r}; "
                    f"use run_allocation_backtest for full production reports."
                )
        super().update(*args, **kwargs)

    def pop(self, key, *args):
        if key in UNPORTED_PRODUCTION_KEYS:
            raise NotImplementedError(
                f"run_allocation_backtest_fast does not port production key {key!r}; "
                f"use run_allocation_backtest for full production reports."
            )
        return super().pop(key, *args)

    def copy(self):
        return FastBacktestResult(super().copy())

    def __copy__(self):
        return self.copy()

    def __ior__(self, other):
        """Guarded in-place merge for ``res |= other`` (task B6.4).

        ``dict.__ior__`` merges at the C level via ``PyDict_Merge`` and therefore
        skips this subclass's ``__setitem__``/``update``, letting an unported key
        be injected silently -- and, because ``__getitem__``/``__contains__`` stay
        guarded, such a key is "written but unreadable". Route through the already
        guarded ``update``, which pre-checks every incoming key atomically and
        raises ``NotImplementedError`` before mutating anything. Must return
        ``self``: the augmented-assignment statement rebinds ``res`` to the return
        value.
        """
        self.update(other)
        return self

    def __or__(self, other):
        """Guarded non-mutating merge for ``res | other`` (task B6.4).

        ``dict.__or__`` returns a plain ``dict``, silently dropping the guard type.
        Mirror ``dict.__or__``'s operand contract (the right operand must be a
        dict, unlike ``|=``/``update`` which also accept arbitrary mappings), but
        return a guarded ``FastBacktestResult`` instead of a plain dict.
        """
        if not isinstance(other, dict):
            return NotImplemented
        result = self.copy()
        result.update(other)
        return result


def run_allocation_backtest_fast(
    universe: Dict[str, pd.DataFrame],
    target_weights: pd.DataFrame,
    initial_capital: float = 100_000.0,
    commission_pct: float = 0.0005,
    slippage_pct: float = 0.0005,
    min_shares: int = 1,
    min_weight_change: float = 0.0,
    china_trading: bool = False,
    stamp_duty_pct: float = 0.0005,
    us_trading: bool = False,
    hk_trading: bool = False,
    hk_stamp_duty_pct: float = 0.001085,
    sec_fee_pct: float = 0.0000278,
    name_map: Optional[Dict[str, str]] = None,
    minimal_metrics: bool = False,
    execution_price: str = "close",
    intraday_data: Optional[Dict[str, pd.DataFrame]] = None,
    market_impact: bool = False,
    impact_gamma: float = 1.0,
    impact_vol_window: int = 20,
    impact_adv_window: int = 20,
    max_volume_participation: Optional[float] = None,
) -> dict:
    """`run_allocation_backtest(..., execution_price="close")` 的试验性数组内核。

    签名刻意与生产保持一致（便于 A/B 对拍），但**只支持**默认 close 撮合，
    且不含 B3.2 的冲击/流动性封顶。任何超出范围的参数都**显式报错**，绝不
    静默换成另一种撮合语义。

    返回 dict 与生产同构：`equity_curve` / `actual_weights` /
    `rebalance_report` / `suspension_report` / `total_turnover` /
    `total_rebalances` / `execution_price`，额外带 `kernel` 元信息
    （`"numba"` / `"numpy"`、`KERNEL_VERSION`、`KERNEL_STATUS`）。**不含**
    绩效指标（sharpe/cagr/...）：本内核的交付物是**账本本身**，指标由调用方
    从 `equity_curve` 自行计算，避免第二处定义漂移。
    """
    if minimal_metrics:
        raise NotImplementedError(
            "run_allocation_backtest_fast does not implement minimal_metrics=True; "
            "use run_allocation_backtest."
        )
    if execution_price != "close":
        raise ValueError(
            "run_allocation_backtest_fast only implements execution_price='close' "
            "(the B3.1 next_* queueing modes are not ported), got "
            f"{execution_price!r}."
        )
    if market_impact:
        raise ValueError(
            "run_allocation_backtest_fast does not port the B3.2 square-root impact model; "
            "use the production engine for market_impact=True."
        )
    if max_volume_participation is not None:
        raise ValueError(
            "run_allocation_backtest_fast does not port the B3.2 liquidity participation cap; "
            "use the production engine for max_volume_participation."
        )
    if intraday_data is not None:
        raise ValueError(
            "intraday_data only matters for execution_price='intraday_twap', which this "
            "kernel does not implement."
        )
    if sum([bool(china_trading), bool(us_trading), bool(hk_trading)]) > 1:
        raise ValueError("Only one of china_trading, us_trading, hk_trading may be enabled.")

    # 与生产完全相同的 min_shares 归一化（A 股/港股 100 股、美股 1 股）。
    if china_trading and min_shares == 1:
        min_shares = 100
    elif hk_trading and min_shares == 1:
        min_shares = 100
    elif us_trading and min_shares != 0:
        min_shares = 1
    if isinstance(min_shares, bool) or not isinstance(min_shares, (int, np.integer)) or min_shares < 0:
        raise ValueError(f"min_shares must be an integer >= 0, got {min_shares!r}")

    symbols = list(universe.keys())
    if not symbols or target_weights.empty:
        return FastBacktestResult({"equity_curve": pd.DataFrame(), "turnover": 0.0})

    closes = pd.DataFrame({sym: df["Close"] for sym, df in universe.items()})
    highs = pd.DataFrame({sym: df["High"] if "High" in df.columns else df["Close"] for sym, df in universe.items()})
    lows = pd.DataFrame({sym: df["Low"] if "Low" in df.columns else df["Close"] for sym, df in universe.items()})

    common_idx = closes.index.intersection(target_weights.index)
    if len(common_idx) == 0:
        return FastBacktestResult({"equity_curve": pd.DataFrame(), "turnover": 0.0})
    closes = closes.loc[common_idx]
    highs = highs.loc[common_idx]
    lows = lows.loc[common_idx]
    sparse_weights = target_weights.reindex(columns=symbols).loc[common_idx]

    volume_cols = {sym: df["Volume"] for sym, df in universe.items() if "Volume" in df.columns}
    volume_known = np.array([sym in volume_cols for sym in symbols], dtype=bool)
    if volume_cols:
        volume_arr = (
            pd.DataFrame({sym: pd.to_numeric(vol, errors="coerce") for sym, vol in volume_cols.items()})
            .reindex(common_idx)
            .reindex(columns=symbols)
            .to_numpy(dtype=float)
        )
    else:
        volume_arr = np.full((len(common_idx), len(symbols)), np.nan)

    is_reb = sparse_weights.notna().any(axis=1).to_numpy()
    tgt_w_arr = sparse_weights.ffill().fillna(0.0).to_numpy(dtype=float)

    closes_arr = closes.to_numpy(dtype=float)
    highs_arr = highs.to_numpy(dtype=float)
    lows_arr = lows.to_numpy(dtype=float)
    # 同一批 pandas 表达式，避免重新推导 NaN 语义造成第二套定义。
    ret_arr = closes.pct_change().fillna(0.0).to_numpy(dtype=float)
    mark_arr = closes.ffill().fillna(0.0).to_numpy(dtype=float)

    china_limit_pcts = (
        np.array([_get_china_price_limit(s, (name_map or {}).get(s)) for s in symbols], dtype=float)
        if china_trading else np.ones(len(symbols))
    )
    lot_arr = np.array(
        [_get_hk_board_lot(s, min_shares) for s in symbols] if (hk_trading and min_shares > 0)
        else [min_shares] * len(symbols),
        dtype=np.int64,
    )
    # `(-|delta_w|, symbol)` 的字符串 tie-break 换成整数 tie-break：lex_rank 唯一。
    lex_rank = np.empty(len(symbols), dtype=np.int64)
    for rank, sym in enumerate(sorted(symbols)):
        lex_rank[symbols.index(sym)] = rank

    n_days = len(common_idx)
    n_sym = len(symbols)
    # 成交/停牌的上界是紧的：每个 symbol 每个 rebalance 日最多产生一笔成交（外加
    # day-0 建仓），停牌记录同样只可能出现在 day-0 与 rebalance 日。按这个上界
    # 预分配，而不是 n_days*n_sym -- 后者在 (4000 日 x 50 标的) 上会白分配并清零
    # 25 MB，那笔开销足以吃掉整个内核的加速。
    n_reb = int(is_reb.sum())
    max_rows = n_sym * (n_reb + 1) + 1
    out_equity = np.zeros(n_days)
    out_actual_w = np.zeros((n_days, n_sym))
    out_fills = np.zeros((max_rows, _N_FILL_COLS))
    out_susp = np.zeros((max_rows, _N_SUSP_COLS))

    n_fill, n_susp, total_turnover, _rebalance_id = _ACTIVE_KERNEL(
        tgt_w_arr, is_reb, closes_arr, highs_arr, lows_arr, mark_arr, ret_arr,
        volume_arr, volume_known, lot_arr, lex_rank, china_limit_pcts,
        float(initial_capital), float(commission_pct), float(slippage_pct),
        float(min_weight_change),
        bool(china_trading), bool(us_trading), bool(hk_trading),
        float(stamp_duty_pct), float(hk_stamp_duty_pct), float(sec_fee_pct),
        out_equity, out_actual_w, out_fills, out_susp,
    )
    n_fill = int(n_fill)
    n_susp = int(n_susp)

    fills = out_fills[:n_fill]
    rebalance_report_df = pd.DataFrame({
        "rebalance_id": fills[:, 15].astype(int),
        "date": [common_idx[int(d)].strftime("%Y-%m-%d") for d in fills[:, 0]],
        "symbol": [symbols[int(s)] for s in fills[:, 1]],
        "action": ["BUY" if a > 0 else "SELL" for a in fills[:, 2]],
        "price": fills[:, 3],
        "prior_weight": fills[:, 4],
        "target_weight": fills[:, 5],
        "weight_change": fills[:, 6],
        "trade_value": fills[:, 7],
        "shares": fills[:, 8],
        "prior_shares": fills[:, 9],
        "target_shares": fills[:, 10],
        "commission": fills[:, 11],
        "slippage": fills[:, 12],
        "total_cost": fills[:, 13],
        "portfolio_equity": fills[:, 14],
    })

    susp = out_susp[:n_susp]
    suspension_report_df = pd.DataFrame({
        "date": [common_idx[int(d)].strftime("%Y-%m-%d") for d in susp[:, 0]],
        "symbol": [symbols[int(s)] for s in susp[:, 1]],
        "action": ["BUY" if a > 0 else "SELL" for a in susp[:, 2]],
        "reason": ["volume_missing" if r > 0.5 else "volume_zero" for r in susp[:, 4]],
        "volume": susp[:, 5],
        "target_weight": susp[:, 3],
    })

    equity_df = pd.DataFrame(index=common_idx)
    equity_df["equity"] = out_equity
    actual_weights_df = pd.DataFrame(out_actual_w, index=common_idx, columns=symbols)

    return FastBacktestResult({
        "equity_curve": equity_df,
        "actual_weights": actual_weights_df,
        "rebalance_report": rebalance_report_df,
        "suspension_report": suspension_report_df,
        "execution_price": "close",
        "total_turnover": float(total_turnover),
        "total_rebalances": int(is_reb.sum()),
        "kernel": "numba" if JIT_KERNEL is not None else "numpy",
        "kernel_version": KERNEL_VERSION,
        "kernel_status": KERNEL_STATUS,
    })
