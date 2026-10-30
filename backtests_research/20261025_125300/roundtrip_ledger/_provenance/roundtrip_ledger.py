"""`L2.52` 阶段一：换手引擎 = 「止损出局 → 冷却买回」往返的零回测取证。

本模块只做**测量**，不做任何策略改动、不跑任何回测、不联网。它把
`walkforward_rebalances.csv`（17 列成交台账）里同一个 `fold` 内同一个 `symbol` 的
「**出场（`target_weight` → 0）→ 下一次入场（`target_weight` > 0，且中间无持仓）**」
配成一条**往返**（round trip），并登记：

1. `bars_elapsed`  —— 两次成交之间的**交易日 bar 数**（需日线缓存提供交易日历）；
2. `gap_return = price_entry / price_exit − 1` —— 出局后到买回之间的价格漂移
   （正值 = 买回价比卖出价**高** ⇒ 这一趟往返在价格上是**亏**的）；
3. `mae` / `mfe` —— 放弃持仓期间的最大不利/有利偏移（需逐日复权 OHLC）；
4. 往返频率、正 gap 占比、按 `|trade_value|` 加权的 gap 均值；
5. 该往返**两腿合计摩擦**及其占全程毛成交额/总成本的份额。

--------------------------------------------------------------------------------
预注册判据（照抄 `docs/development_plan.md` 行 `L2.52` 第 2 条，逐字保留）

    H1 止损有效 = 往返后价更低、止损躲过损失、延长冷却会抬升风险；
    H2 洗盘往返 = 再入贴近且价 ≥ 出场价、止损双倍付摩擦且毁值。
    「若正 gap 占比高 且 加权 gap ≥ 0 且 中位 bars 短 ⇒ H2 主导 ⇒ 冷却延长的成本侧有案可查；
      若负 gap 占比高（止损确实躲损）⇒ H1 主导 ⇒ 见第 6 条。」

规格把「高」「短」写成形容词而没有给数字。为了让判据**机械可执行、且不做事后调参**，
本模块把定性规则固化为下面 4 个**常量**（`PREREG`），它们**不是**命令行参数、
**不允许**在看过结果后改动：

    H2_POS_SHARE_MIN      = 0.50  # 「正 gap 占比高」= 过半
    H2_WEIGHTED_GAP_MIN   = 0.0   # 「加权 gap ≥ 0」照抄（不加缓冲）
    H2_MEDIAN_BARS_MAX    = 6     # 「中位 bars 短」= 不长于机械冷却底
                                  #   （`asset_stop_cooldown_bars` = 5 + 1 根确认 bar = 6，
                                  #    归因见 docs/version.md §16.15.15）
    H1_NEG_SHARE_MIN      = 0.50  # 「负 gap 占比高」= 过半

三者全中 ⇒ `H2_dominant`；负 gap 过半（等价于加权 gap < 0 且中位数为负）⇒ `H1_dominant`；
其余 ⇒ `mixed`。**本模块不预设结论**：`grade()` 只是把上面这张真值表实现成函数。

--------------------------------------------------------------------------------
数据分层（诚实披露，见 STAGE1_REPORT.md §4）

- **第 1 层（CSV 单独可算，100% 覆盖）**：配对、`gap_return`（`price` 列已是复权价，
  见报告 §3.1 的验证）、日历日差近似（**近似，不是 bar 数**）。
- **第 2 层（`data/cache.duckdb`，只读）**：`bars_elapsed` / `mae` / `mfe` / 反事实。
  该库对 16 个宇宙标的只有 **2024-01-02 起**的 OHLC，故 2021-06..2023-12 的窗口
  **不可算**，本模块只用它算**覆盖得到**的那部分，并把覆盖率写进摘要。
- **第 3 层（`--aux-db`，可选、非预注册）**：`data/cache.backup-20261027-pre-m38raw.duckdb`
  （m38 raw 迁移**之前**的备份，其 `close` 已是复权价，且与回测 CSV 的 `price` 列
  逐值吻合）——**只读**打开，用来把第 2 层覆盖不到的 2021-2023 补全、并做交叉校验。
  用与不用都写进输出的 `provenance`。

用法（全程离线，无 `--data-provider`、无网络；2026-10-29 起本脚本归档在 `_provenance/`）：

    python backtests_research/20261025_125300/roundtrip_ledger/_provenance/roundtrip_ledger.py \
        --scope primary \
        --db data/cache.duckdb \
        --aux-db data/cache.backup-20261027-pre-m38raw.duckdb \
        --out backtests_research/20261025_125300/roundtrip_ledger/out

`--out` 的默认值由 `--run-root` 派生；`--run-root` 的默认值是**仓库相对**路径，若当前目录下
不存在（即不是从仓库根运行），则回退到由 `__file__` 推出的绝对路径 —— 默认输出**绝不会**
静默落到调用者的 CWD 里。复现命令与 sha256 见同目录 `README.md`。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# 预注册常量（写死，不接受 CLI 覆盖 —— 规格第 5 条「反事后加项」）
# --------------------------------------------------------------------------
PREREG = {
    "H2_POS_SHARE_MIN": 0.50,
    "H2_WEIGHTED_GAP_MIN": 0.0,
    "H2_MEDIAN_BARS_MAX": 6,
    "H1_NEG_SHARE_MIN": 0.50,
}

DEFAULT_TOL = 1e-12

# 主要范围 = 规格/协调者指定的 4 个主跑批；`all` 覆盖目录下全部 75 份 CSV。
PRIMARY_RUNS = (
    "01_r1_longwin",
    "03_r2b_oos",
    "07_r3_oos_bear",
    "08_ablate_wf/baseline",
)

REQUIRED_COLS = (
    "fold",
    "rebalance_id",
    "date",
    "symbol",
    "action",
    "price",
    "prior_weight",
    "target_weight",
    "trade_value",
    "total_cost",
)


# --------------------------------------------------------------------------
# 第 0 层：读取与配对（纯 pandas，可单测，不碰 DuckDB）
# --------------------------------------------------------------------------
def load_rebalances(paths, run_root=None):
    """读入并打上 `run` / `strategy` 标签。

    `run` 是相对 `<run_root>` 的父目录（如 `08_ablate_wf/baseline`），
    `strategy` 是 CSV 所在目录名。
    """
    frames = []
    for p in paths:
        df = pd.read_csv(p)
        missing = [c for c in REQUIRED_COLS if c not in df.columns]
        if missing:
            raise ValueError(f"{p}: missing columns {missing}")
        df["date"] = pd.to_datetime(df["date"])
        if run_root:
            rel = os.path.relpath(os.path.dirname(os.path.dirname(p)), run_root)
            run = rel.replace(os.sep, "/")
        else:
            run = os.path.basename(os.path.dirname(os.path.dirname(p)))
        df["run"] = run
        df["strategy"] = os.path.basename(os.path.dirname(p))
        df["csv_path"] = p
        frames.append(df)
    if not frames:
        raise ValueError("no rebalance CSV matched")
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(["run", "strategy", "fold", "symbol", "date", "rebalance_id"])
    return out.reset_index(drop=True)


def pair_round_trips(df, tol=DEFAULT_TOL):
    """把「出场 → 下一次入场」配成往返。

    配对键 = (`run`, `strategy`, `fold`, `symbol`)。要求：

    * 出场腿：`target_weight <= tol` 且 `prior_weight > tol`（真正清零）；
    * 入场腿：**该标的下一条**记录满足 `target_weight > tol` 且 `prior_weight <= tol`
      （从零建仓）。取「下一条」而非「后面任意一条」，就是为了落实
      「中间无持仓」——中间若真有持仓，它必然会留下一条 `prior_weight > tol` 的记录，
      此时判 `anomaly` 而不是硬配。

    返回的 DataFrame 每条既是一条往返；未配上的出场腿以 `status='no_reentry'`
    单独保留（**不丢弃**），作为「再入率」的分母。注意 `no_reentry` 同时覆盖两种
    无法从 CSV 区分的形状：折末截断、以及确实再也没买回；另有一条 CSV 契约违例
    （清零之后又出现 `prior_weight > tol` 的成交）也落在这里 —— 那种情况下
    我们**不**把它当成一次干净的往返，见 `test_reentry_after_intermediate_add_is_not_paired`。
    """
    cols = ["run", "strategy", "fold", "symbol"]
    rows = []
    for key, g in df.groupby(cols, sort=False):
        g = g.sort_values(["date", "rebalance_id"])
        recs = g.to_dict("records")
        pending = None  # 最近一次清零但还没买回的出场记录
        last_entry_price = None  # 当前持仓的建仓价（用于算该笔出场的实现收益）
        for r in recs:
            is_exit = (r["target_weight"] <= tol) and (r["prior_weight"] > tol)
            is_entry = (r["target_weight"] > tol) and (r["prior_weight"] <= tol)
            if is_exit:
                pending = r
                rows.append(_base(key, r, exit_row=r, status="pending", entry_ref_price=last_entry_price))
                last_entry_price = None
            elif is_entry and pending is not None:
                rows[-1].update(_entry_fields(r))
                rows[-1]["status"] = "paired"
                pending = None
                last_entry_price = r["price"]
            elif is_entry:
                last_entry_price = r["price"]
        if pending is not None:
            rows[-1]["status"] = "no_reentry"
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["gap_return"] = out["entry_price"] / out["exit_price"] - 1.0
    out["cal_days"] = (out["entry_date"] - out["exit_date"]).dt.days
    out["gap_positive"] = out["gap_return"] > 0
    out["exit_value"] = out["exit_trade_value"].abs()
    out["roundtrip_cost"] = out["exit_cost"] + out["entry_cost"].fillna(0.0)
    out["roundtrip_cost_bp"] = np.where(
        out["exit_value"] > 0, out["roundtrip_cost"] / out["exit_value"] * 1e4, np.nan
    )
    return out


def _base(key, r, exit_row, status, entry_ref_price=None):
    run, strategy, fold, symbol = key
    return {
        "run": run,
        "strategy": strategy,
        "fold": fold,
        "symbol": symbol,
        "status": status,
        "exit_date": exit_row["date"],
        "exit_price": exit_row["price"],
        "exit_prior_weight": exit_row["prior_weight"],
        "exit_trade_value": exit_row["trade_value"],
        "exit_cost": exit_row["total_cost"],
        "exit_rebalance_id": exit_row["rebalance_id"],
        "entry_date": pd.NaT,
        "entry_price": np.nan,
        "entry_target_weight": np.nan,
        "entry_trade_value": np.nan,
        "entry_cost": np.nan,
        "entry_rebalance_id": np.nan,
        "hold_entry_price": entry_ref_price if entry_ref_price is not None else np.nan,
        # 该笔出场相对其建仓价实现了多少收益：>0 说明是在赚钱时卖掉的
        "exit_realised_ret": (
            exit_row["price"] / entry_ref_price - 1.0
            if entry_ref_price not in (None, 0) and not pd.isna(entry_ref_price)
            else np.nan
        ),
    }


def _entry_fields(entry_row):
    return {
        "entry_date": entry_row["date"],
        "entry_price": entry_row["price"],
        "entry_target_weight": entry_row["target_weight"],
        "entry_trade_value": entry_row["trade_value"],
        "entry_cost": entry_row["total_cost"],
        "entry_rebalance_id": entry_row["rebalance_id"],
    }


def dedup_round_trips(rt):
    """折叠重叠造成的**同一经济事件重复登记**去重。

    `01_r1_longwin` 是 4 个 756 天、步长约 98 天的**重叠**窗口，同一次出场/买回会被
    记到最多 7-8 个 `fold` 里。池化会把 `n` 与 t 值一起放大，故分布统计另给一份
    以 (`symbol`, `exit_date`, `entry_date`) 为键、保留 `|exit_trade_value|` 最大的
    那条代表行的去重口径；**摩擦占比类统计不去重**（那是按 run 计的流量）。
    """
    d = rt[rt["status"] == "paired"].copy()
    if d.empty:
        return d
    d["dup_group_size"] = d.groupby(["symbol", "exit_date", "entry_date"])["exit_date"].transform("size")
    d = d.sort_values(["symbol", "exit_date", "entry_date", "exit_value"], ascending=[True, True, True, False])
    return d.drop_duplicates(["symbol", "exit_date", "entry_date"], keep="first").reset_index(drop=True)


# --------------------------------------------------------------------------
# 统计与判据
# --------------------------------------------------------------------------
def summarise(rt, bars_col=None, n_exits=None):
    """一份往返集合的描述性统计（含预注册判据要用的四个量）。

    `n_exits` 显式传入「全部出场事件数」，避免把传入子集的行数当分母
    （这会制造一个恒等于 1.0 的假再入率）。
    """
    d = rt[rt["status"] == "paired"].copy()
    if n_exits is None:
        n_exits = len(rt)
    if d.empty:
        return {
            "n_pairs": 0,
            "n_exits": int(n_exits),
            # 空集时再入率必须是 0.0，不能因为提前返回而丢掉这个键
            # （丢掉它会让下游 `grade()`/报告读到 None 并静默跳过判据）
            "reentry_rate": 0.0,
        }
    w = d["exit_value"].to_numpy(dtype=float)
    g = d["gap_return"].to_numpy(dtype=float)
    wsum = w.sum()
    out = {
        "n_pairs": int(len(d)),
        "n_exits": int(n_exits),
        "reentry_rate": float(len(d) / n_exits) if n_exits else np.nan,
        "pos_share": float((g > 0).mean()),
        "neg_share": float((g < 0).mean()),
        "zero_share": float((g == 0).mean()),
        "gap_mean": float(np.mean(g)),
        "gap_median": float(np.median(g)),
        "gap_std": float(np.std(g, ddof=1)) if len(g) > 1 else np.nan,
        "gap_weighted_mean": float((g * w).sum() / wsum) if wsum > 0 else np.nan,
        "gap_t_pooled": float(np.mean(g) / (np.std(g, ddof=1) / np.sqrt(len(g)))) if len(g) > 1 and np.std(g, ddof=1) > 0 else np.nan,
        "cal_days_median": float(d["cal_days"].median()),
        "cal_days_mean": float(d["cal_days"].mean()),
        "roundtrip_cost_bp_median": float(d["roundtrip_cost_bp"].median()),
        "roundtrip_cost_bp_mean": float(d["roundtrip_cost_bp"].mean()),
        "exit_value_sum": float(wsum),
    }
    if bars_col and bars_col in d.columns and d[bars_col].notna().any():
        b = d[bars_col].dropna()
        out.update(
            {
                f"{bars_col}_n": int(len(b)),
                f"{bars_col}_median": float(b.median()),
                f"{bars_col}_mean": float(b.mean()),
                f"{bars_col}_p25": float(b.quantile(0.25)),
                f"{bars_col}_p75": float(b.quantile(0.75)),
                f"{bars_col}_share_le_6": float((b <= 6).mean()),
                f"{bars_col}_share_le_3": float((b <= 3).mean()),
            }
        )
    return out


def grade(summary, prereg=None):
    """把规格第 2 条的定性真值表实现成函数。

    输入必须含 `pos_share` / `neg_share` / `gap_weighted_mean` / `<bars>_median`。
    返回 (verdict, checks) —— `checks` 逐条给出阈值与命中情况，便于报告里逐项对账。
    """
    p = dict(PREREG if prereg is None else prereg)
    bars_median = next((v for k, v in summary.items() if k.endswith("_median") and k.startswith("bars")), None)
    checks = {
        "H2_pos_share": {
            "value": summary.get("pos_share"),
            "threshold": f"> {p['H2_POS_SHARE_MIN']}",
            "hit": summary.get("pos_share") is not None and summary["pos_share"] > p["H2_POS_SHARE_MIN"],
        },
        "H2_weighted_gap_ge_0": {
            "value": summary.get("gap_weighted_mean"),
            "threshold": f">= {p['H2_WEIGHTED_GAP_MIN']}",
            "hit": summary.get("gap_weighted_mean") is not None and summary["gap_weighted_mean"] >= p["H2_WEIGHTED_GAP_MIN"],
        },
        "H2_median_bars_short": {
            "value": bars_median,
            "threshold": f"<= {p['H2_MEDIAN_BARS_MAX']}",
            "hit": bars_median is not None and not pd.isna(bars_median) and bars_median <= p["H2_MEDIAN_BARS_MAX"],
        },
        "H1_neg_share": {
            "value": summary.get("neg_share"),
            "threshold": f"> {p['H1_NEG_SHARE_MIN']}",
            "hit": summary.get("neg_share") is not None and summary["neg_share"] > p["H1_NEG_SHARE_MIN"],
        },
    }
    h2_all = all(checks[k]["hit"] for k in ("H2_pos_share", "H2_weighted_gap_ge_0", "H2_median_bars_short"))
    if h2_all:
        verdict = "H2_dominant"
    elif checks["H1_neg_share"]["hit"]:
        verdict = "H1_dominant"
    else:
        verdict = "mixed"
    checks["_h2_all_three"] = h2_all
    return verdict, checks


# --------------------------------------------------------------------------
# 第 2/3 层：日线缓存（只读）。缺 duckdb 时状态标 'blocked'，不静默降级。
# --------------------------------------------------------------------------
PROVIDER_PREF = ("FuyaoDataProvider", "MarketDBDataProvider")


def _sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def load_bars(db_path, symbols, mode="raw_factor", provider="FuyaoDataProvider"):
    """只读打开缓存，返回复权后的 `close` / `high` / `low` 逐日路径。

    `mode='raw_factor'`：`data/cache.duckdb` 形态 —— `ohlcv_bars.close` 是**原始价**，
        复权靠 `adjust_factors.factor_hfq`（按日 ffill）。`p_adj = close * factor`
        在除权日**不跳空**，正是总收益路径。因子源优先取覆盖更密的 Fuyao（日频），
        否则退到 BaoStock（除权日事件频）。
    `mode='preadj'`：迁移前备份形态 —— `close` 本身已是复权价，直接用。
    """
    import duckdb  # 延迟导入：配对/统计单测不需要它

    con = duckdb.connect(db_path, read_only=True)
    try:
        bars = con.execute(
            "SELECT symbol, date, high, low, close FROM ohlcv_bars "
            "WHERE provider = ? AND symbol IN (" + ",".join(["?"] * len(symbols)) + ")",
            [provider, *symbols],
        ).df()
        if mode == "preadj":
            bars = bars.rename(columns={"close": "p", "high": "p_high", "low": "p_low"})
        else:
            try:
                fac = con.execute(
                    "SELECT symbol, date, factor_hfq, source FROM adjust_factors "
                    "WHERE symbol IN (" + ",".join(["?"] * len(symbols)) + ")",
                    list(symbols),
                ).df()
            except Exception:
                fac = pd.DataFrame(columns=["symbol", "date", "factor_hfq", "source"])
            fac["date"] = pd.to_datetime(fac["date"])
            # 每个标的一条因子序列：Fuyao（日频）优先，否则 BaoStock（事件频）
            per_symbol = {}
            for sym, g in fac.groupby("symbol"):
                counts = g["source"].value_counts()
                src = "FuyaoDataProvider" if counts.get("FuyaoDataProvider", 0) >= 100 else counts.index[0]
                f = g[g["source"] == src][["date", "factor_hfq"]].sort_values("date")
                per_symbol[sym] = f
            parts = []
            for sym, g in bars.groupby("symbol"):
                g = g.sort_values("date").copy()
                f = per_symbol.get(sym)
                if f is None or f.empty:
                    g["fac"] = 1.0
                else:
                    g = pd.merge_asof(g, f.rename(columns={"date": "fdate"}), left_on="date", right_on="fdate")
                    g["fac"] = g["factor_hfq"].ffill().bfill().fillna(1.0)
                g["p"] = g["close"] * g["fac"]
                g["p_high"] = g["high"] * g["fac"]
                g["p_low"] = g["low"] * g["fac"]
                parts.append(g[["symbol", "date", "p", "p_high", "p_low"]])
            bars = pd.concat(parts, ignore_index=True)
    finally:
        con.close()
    bars["date"] = pd.to_datetime(bars["date"])
    return bars.sort_values(["symbol", "date"]).reset_index(drop=True)


def attach_path_stats(rt, bars, prefix="bars"):
    """给每条往返挂上 `{prefix}_elapsed` / `{prefix}_mae` / `{prefix}_mfe` / `{prefix}_cf_return`。

    * `bars_elapsed` = 该标的交易日历上的索引差（**bar 数**，不是日历日）；
    * `mae` = 放弃持仓区间 `(exit_date, entry_date]` 内 `low/p_exit − 1` 的最小值；
    * `mfe` 同理取 `high` 的最大值；
    * `cf_return` = `p_entry/p_exit − 1`，即「若继续持有到买回日」的价格结果
      （与 CSV 的 `gap_return` 互为交叉校验）。
    任何一端落在缓存覆盖之外 ⇒ 该条留 `NaN` 并置 `{prefix}_status='uncovered'`。
    """
    out = rt.copy()
    out[f"{prefix}_status"] = "uncovered"
    for c in (f"{prefix}_elapsed", f"{prefix}_mae", f"{prefix}_mfe", f"{prefix}_cf_return"):
        out[c] = np.nan
    idx = {sym: (g["date"].to_numpy(), g["p"].to_numpy(), g["p_high"].to_numpy(), g["p_low"].to_numpy())
           for sym, g in bars.groupby("symbol")}
    for i, row in out.iterrows():
        sym = row["symbol"]
        if sym not in idx or pd.isna(row["entry_date"]):
            continue
        dates, p, hi, lo = idx[sym]
        i0 = np.searchsorted(dates, np.datetime64(row["exit_date"]))
        i1 = np.searchsorted(dates, np.datetime64(row["entry_date"]))
        if i0 >= len(dates) or i1 >= len(dates):
            continue
        if dates[i0] != np.datetime64(row["exit_date"]) or dates[i1] != np.datetime64(row["entry_date"]):
            continue
        if i1 <= i0:
            continue
        window_lo = lo[i0 + 1: i1 + 1]
        window_hi = hi[i0 + 1: i1 + 1]
        out.at[i, f"{prefix}_elapsed"] = float(i1 - i0)
        out.at[i, f"{prefix}_mae"] = float(window_lo.min() / p[i0] - 1.0) if len(window_lo) else np.nan
        out.at[i, f"{prefix}_mfe"] = float(window_hi.max() / p[i0] - 1.0) if len(window_hi) else np.nan
        out.at[i, f"{prefix}_cf_return"] = float(p[i1] / p[i0] - 1.0)
        out.at[i, f"{prefix}_status"] = "covered"
    return out


def merge_path_layers(rt):
    """把主库层与辅助库层折叠成一套 canonical 列。

    规则（写死在代码里、报告里照抄）：**主库能算的用主库**；主库算不了的用辅助库并在
    `path_status` 里标 `covered_via_aux`。绝不用任何插值/合成价补缺。
    """
    out = rt.copy()
    pairs = {
        "bars_elapsed": ("bars_elapsed", "aux_elapsed"),
        "mae": ("bars_mae", "aux_mae"),
        "mfe": ("bars_mfe", "aux_mfe"),
        "cf_return": ("bars_cf_return", "aux_cf_return"),
    }
    out["path_status"] = "uncovered"
    for dst, (pri, aux) in pairs.items():
        if pri not in out.columns:
            out[pri] = np.nan
        if aux not in out.columns:
            out[aux] = np.nan
        v = out[pri].copy()
        need = v.isna() & out[aux].notna()
        v = v.where(~need, out[aux])
        out[dst] = v
    if "bars_status" in out.columns:
        out["path_status"] = np.where(out["bars_status"] == "covered", "covered_primary", out["path_status"])
    if "aux_status" in out.columns:
        out["path_status"] = np.where(
            (out["path_status"] == "uncovered") & (out["aux_status"] == "covered"), "covered_via_aux", out["path_status"]
        )
    return out


def attach_market_proxy(rt, bars, prefix="mkt"):
    """等权宇宙代理收益（**不是**指数）：同一对 `(exit_date, entry_date)` 上，
    对所有在两端都有价的标的取等权平均收益，用于把 `gap_return` 里的大盘 beta 剥掉。

    这是「**同一 20 只宇宙的等权组合**」而不是任何公开指数——本地缓存里 000300.SH 等
    指数只有 2026-09-22 起的 20 根 bar，**无法**做真正的指数调整（见报告 §6）。
    """
    out = rt.copy()
    out[f"{prefix}_proxy_return"] = np.nan
    out[f"gap_excess_{prefix}"] = np.nan
    px = bars.pivot_table(index="date", columns="symbol", values="p", aggfunc="first").sort_index()
    dates = px.index
    for i, row in out.iterrows():
        if pd.isna(row["entry_date"]):
            continue
        i0 = dates.searchsorted(pd.Timestamp(row["exit_date"]))
        i1 = dates.searchsorted(pd.Timestamp(row["entry_date"]))
        if i0 >= len(dates) or i1 >= len(dates) or dates[i0] != pd.Timestamp(row["exit_date"]) or dates[i1] != pd.Timestamp(row["entry_date"]) or i1 <= i0:
            continue
        seg = px.iloc[[i0, i1]].dropna(axis=1)
        if seg.shape[1] < 5:
            continue
        r = (seg.iloc[1] / seg.iloc[0] - 1.0).mean()
        out.at[i, f"{prefix}_proxy_return"] = float(r)
        out.at[i, f"gap_excess_{prefix}"] = float(row["gap_return"]) - float(r)
    return out


def classify_exit_depth(rt):
    """按该笔出场的**实现收益**（出场价 / 建仓价 − 1）分档。

    分档沿用 `docs/version.md` §16.15.15 的既有口径（≤ −8% / ≤ −5%），便于与本仓
    已发表的深亏组数字对照；`stop_like` 是**代理标签**，不是引擎内部的 `is_stop`
    标志（CSV 不落该字段，见报告 §6「不可验证项」）。
    """
    out = rt.copy()
    r = out["exit_realised_ret"]
    out["exit_depth_bucket"] = pd.cut(
        r,
        bins=[-np.inf, -0.08, -0.05, -0.02, 0.0, np.inf],
        labels=["<=-8%", "(-8%,-5%]", "(-5%,-2%]", "(-2%,0%]", ">0%"],
    ).astype(object)
    out.loc[r.isna(), "exit_depth_bucket"] = np.nan
    return out


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def by_group(rt, keys, bars_col=None):
    rows = []
    for key, g in rt.groupby(keys, sort=True):
        key = key if isinstance(key, tuple) else (key,)
        s = summarise(g, bars_col=bars_col)
        s.update(dict(zip(keys, key)))
        rows.append(s)
    return pd.DataFrame(rows)


def turnover_share(rt, df):
    """往返两腿摩擦 / 全部成交摩擦（**不去重**，按 run+strategy 计的流量）。"""
    rows = []
    paired = rt[rt["status"] == "paired"]
    for (run, strategy), g in df.groupby(["run", "strategy"]):
        p = paired[(paired["run"] == run) & (paired["strategy"] == strategy)]
        exit_ids = set(zip(p["fold"], p["exit_rebalance_id"], p["symbol"]))
        entry_ids = set(zip(p["fold"], p["entry_rebalance_id"], p["symbol"]))
        all_cost = g["total_cost"].sum()
        all_legs = len(g)
        rt_cost = 0.0
        rt_legs = 0
        for _, r in g.iterrows():
            k = (r["fold"], r["rebalance_id"], r["symbol"])
            if k in exit_ids or k in entry_ids:
                rt_cost += r["total_cost"]
                rt_legs += 1
        rows.append(
            {
                "run": run,
                "strategy": strategy,
                "n_legs": all_legs,
                "n_roundtrip_legs": rt_legs,
                "leg_share": rt_legs / all_legs if all_legs else np.nan,
                "total_cost": all_cost,
                "roundtrip_cost": rt_cost,
                "cost_share": rt_cost / all_cost if all_cost else np.nan,
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _resolve_scope(scope, run_root):
    pat = os.path.join(run_root, "**", "walkforward_rebalances.csv")
    all_paths = sorted(glob.glob(pat, recursive=True))
    if scope == "all":
        return all_paths
    keep = []
    for p in all_paths:
        rel = os.path.relpath(os.path.dirname(os.path.dirname(p)), run_root).replace(os.sep, "/")
        if rel in PRIMARY_RUNS:
            keep.append(p)
    return keep


def posthoc_slices(ded):
    """**事后切片（非预注册，严禁当作判据）**：把预注册规则再套到三个子群上，
    只为看清「H2 若成立，最可能成立在哪一层」。
    * `cooldown_timed`   —— `bars_elapsed <= 6`：机械冷却一解冻就买回的那批；
    * `stop_like`        —— 出场实现收益 `<= -5%`：深亏出场（§16.15.15 口径），
                            CSV 不落 `is_stop`，故这是**代理标签**；
    * `cooldown_and_deep`—— 两者交集。
    """
    out = {}
    slices = {
        "cooldown_timed": ded[ded["bars_elapsed"] <= 6],
        "stop_like": ded[ded["exit_realised_ret"] <= -0.05],
        "cooldown_and_deep": ded[(ded["bars_elapsed"] <= 6) & (ded["exit_realised_ret"] <= -0.05)],
        "long_gap_gt_6": ded[ded["bars_elapsed"] > 6],
    }
    for name, sl in slices.items():
        if sl.empty:
            out[name] = {"n_pairs": 0}
            continue
        s = summarise(sl, bars_col="bars_elapsed", n_exits=len(sl))
        v, c = grade(s)
        s["verdict"] = v
        s["checks"] = c
        out[name] = s
    return out


def cost_attribution(rt, df):
    """摩擦归属：把「全部成交成本」按「是否属于某条被配对往返的两腿」拆开，
    并给出 cooldown-timed 子群（`bars_elapsed <= 6`）在总成本里的份额。

    这是**阶段二唯一有意义的规模量**：任何再入干预能触到的成本上限就是这个份额。
    """
    paired = rt[rt["status"] == "paired"]
    rt_cost_by_key = {}
    for _, r in paired.iterrows():
        for fold, rid, sym in ((r["fold"], r["exit_rebalance_id"], r["symbol"]),
                               (r["fold"], r["entry_rebalance_id"], r["symbol"])):
            rt_cost_by_key[(r["run"], r["strategy"], fold, rid, sym)] = r.get("bars_elapsed", np.nan)
    total_cost = float(df["total_cost"].sum())
    rt_cost = 0.0
    cd_cost = 0.0
    for _, r in df.iterrows():
        k = (r["run"], r["strategy"], r["fold"], r["rebalance_id"], r["symbol"])
        if k in rt_cost_by_key:
            rt_cost += r["total_cost"]
            b = rt_cost_by_key[k]
            if isinstance(b, float) and not np.isnan(b) and b <= 6:
                cd_cost += r["total_cost"]
    # gap 的货币化影响：出场市值 × gap（正 = 买回更贵 = 相对持有更差）
    def money(sl):
        return float((sl["exit_value"] * sl["gap_return"]).sum())
    return {
        "total_cost_all_legs": total_cost,
        "roundtrip_cost": rt_cost,
        "roundtrip_cost_share": rt_cost / total_cost if total_cost else np.nan,
        "cooldown_timed_cost": cd_cost,
        "cooldown_timed_cost_share": cd_cost / total_cost if total_cost else np.nan,
        "gap_money_all_pairs": money(paired),
        "gap_money_cooldown_timed": money(paired[paired["bars_elapsed"] <= 6]),
        "gap_money_long": money(paired[paired["bars_elapsed"] > 6]),
        "n_pairs_cooldown_timed_pooled": int((paired["bars_elapsed"] <= 6).sum()),
        "note": "全部为**池化（未去重）**口径；与 turnover 表一致，按 run+strategy 的流量计。",
    }


def frequency(rt, df):
    """频次：**run 内去重后**的往返数 / 该 run 的日历覆盖年数。

    折叠重叠（`01_r1_longwin` 是 4 个 756 天、步长约 98 天的重叠窗口）会让池化计数
    虚高，所以分子先按 (`symbol`, `exit_date`, `entry_date`) **在 run 内**去重，
    分母用该 run 的 `min(date)..max(date)` 日历跨度。
    """
    rows = []
    for run in sorted(df["run"].unique()):
        g = df[df["run"] == run]
        years = (g["date"].max() - g["date"].min()).days / 365.25
        sub = rt[(rt["run"] == run) & (rt["status"] == "paired")]
        n = int(len(sub.drop_duplicates(["symbol", "exit_date", "entry_date"])))
        rows.append({
            "run": run,
            "n_strategies": int(g["strategy"].nunique()),
            "n_folds": int(g["fold"].nunique()),
            # 多重登记来源：01_r1_longwin 是 4 个重叠窗口 + 6 个策略同跑；
            # 其余 run 的重复只来自「同一批成交被多个策略共享」。
            "dedup_multiplicity": round(len(sub) / n, 2) if n else np.nan,
            "calendar_years": round(years, 2),
            "n_pairs_pooled": int(len(sub)),
            "n_pairs_dedup": n,
            "pairs_per_year_per_strategy": round(n / years / max(g["strategy"].nunique(), 1), 1) if years > 0 else np.nan,
            "rows_per_year_per_strategy": round(len(g) / years / max(g["strategy"].nunique(), 1), 0) if years > 0 else np.nan,
            "n_rows": int(len(g)),
        })
    return rows


def summarise_excess(d, col="gap_excess_mkt"):
    """**补充口径（非预注册）**：把 gap 减去等权宇宙代理收益后的超额。
    只用于回答「正 gap 是不是纯粹的大盘 beta」，不参与 `grade()` 的判定。"""
    s = d[col].dropna()
    if s.empty:
        return {}
    w = d.loc[s.index, "exit_value"].to_numpy(dtype=float)
    v = s.to_numpy(dtype=float)
    wsum = w.sum()
    return {
        "n": int(len(s)),
        "excess_mean": float(v.mean()),
        "excess_median": float(np.median(v)),
        "excess_weighted_mean": float((v * w).sum() / wsum) if wsum > 0 else np.nan,
        "excess_pos_share": float((v > 0).mean()),
        "excess_t_pooled": float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))) if len(v) > 1 and v.std(ddof=1) > 0 else np.nan,
        "mkt_proxy_mean": float(d.loc[s.index, "mkt_proxy_return"].mean()),
    }


def _default_run_root():
    """`--run-root` 的默认值：优先仓库相对路径（从仓库根运行时的既有行为，逐字节不变），
    否则回退到由 `__file__` 推出的绝对路径 —— 这样默认 `--out` **不会**静默依赖 CWD。
    本文件位于 `<run_root>/roundtrip_ledger/_provenance/`，故上溯三层即 run_root。"""
    repo_relative = os.path.join("backtests_research", "20261025_125300")
    if os.path.isdir(repo_relative):
        return repo_relative
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    ap = argparse.ArgumentParser(description="L2.52 阶段一：出场→买回往返台账（零回测、离线）")
    ap.add_argument("--run-root", default=None,
                    help="默认：仓库根的 backtests_research/20261025_125300（否则按本文件位置回退）")
    ap.add_argument("--scope", choices=("primary", "all"), default="primary",
                    help="primary = 4 个主跑批（默认）；all = 目录下全部 75 份 CSV")
    ap.add_argument("--db", default="data/cache.duckdb", help="只读取；用于 bars_elapsed / MAE")
    ap.add_argument("--db-mode", choices=("raw_factor", "preadj"), default="raw_factor")
    ap.add_argument("--aux-db", default=None, help="可选：迁移前备份（preadj），补 2021-2023 覆盖")
    ap.add_argument("--aux-db-mode", choices=("raw_factor", "preadj"), default="preadj")
    ap.add_argument("--out", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.run_root is None:
        args.run_root = _default_run_root()

    out_dir = args.out or os.path.join(args.run_root, "roundtrip_ledger", "out")
    os.makedirs(out_dir, exist_ok=True)

    paths = _resolve_scope(args.scope, args.run_root)
    if not paths:
        print("no CSV found", file=sys.stderr)
        return 2
    df = load_rebalances(paths, run_root=args.run_root)
    rt = pair_round_trips(df)
    symbols = sorted(df["symbol"].unique())

    provenance = {
        "scope": args.scope,
        "n_csv": len(paths),
        "runs": sorted(df["run"].unique().tolist()),
        "strategies": sorted(df["strategy"].unique().tolist()),
        "n_rows": int(len(df)),
        "folds": int(df[["run", "strategy", "fold"]].drop_duplicates().shape[0]),
        "db": None,
        "aux_db": None,
        "generated_by": "backtests_research/20261025_125300/roundtrip_ledger/_provenance/roundtrip_ledger.py",
    }

    # 第 2 层：主库
    bars = None
    if args.db and os.path.exists(args.db):
        try:
            bars = load_bars(args.db, symbols, mode=args.db_mode)
            provenance["db"] = {
                "path": args.db,
                "mode": args.db_mode,
                "sha256": _sha256(args.db),
                "size_bytes": os.path.getsize(args.db),
                "n_symbols": int(bars["symbol"].nunique()),
                "n_rows": int(len(bars)),
                "coverage": {
                    s: [str(g["date"].min().date()), str(g["date"].max().date()), int(len(g))]
                    for s, g in bars.groupby("symbol")
                },
            }
            rt = attach_path_stats(rt, bars, prefix="bars")
            rt = attach_market_proxy(rt, bars, prefix="mkt")
        except Exception as exc:  # 只读失败/库损坏 ⇒ 明确标 blocked，不静默降级
            provenance["db"] = {"path": args.db, "status": "blocked", "error": f"{type(exc).__name__}: {exc}"}

    # 第 3 层：可选补覆盖（非预注册）
    if args.aux_db and os.path.exists(args.aux_db):
        try:
            aux = load_bars(args.aux_db, symbols, mode=args.aux_db_mode)
            provenance["aux_db"] = {
                "path": args.aux_db,
                "mode": args.aux_db_mode,
                "sha256": _sha256(args.aux_db),
                "size_bytes": os.path.getsize(args.aux_db),
                "n_rows": int(len(aux)),
                "coverage": {
                    s: [str(g["date"].min().date()), str(g["date"].max().date()), int(len(g))]
                    for s, g in aux.groupby("symbol")
                },
            }
            rt = attach_path_stats(rt, aux, prefix="aux")
            # 代理收益只在**覆盖最宽**的那份源上算一次（aux 覆盖 2017+，是超集）
            rt = attach_market_proxy(rt, aux, prefix="mkt")
        except Exception as exc:
            provenance["aux_db"] = {"path": args.aux_db, "status": "blocked", "error": f"{type(exc).__name__}: {exc}"}

    rt = merge_path_layers(rt)
    rt = classify_exit_depth(rt)

    # 校准：复权路径能否复现 CSV 的 `price` 列（最强陈述：逐值核对）
    price_check = {}
    if bars is not None:
        chk = df.merge(bars.rename(columns={"p": "p_adj"})[["symbol", "date", "p_adj"]], on=["symbol", "date"], how="left")
        chk = chk[chk["p_adj"].notna()]
        if len(chk):
            rel = (chk["price"] / chk["p_adj"]).groupby(chk["symbol"]).transform("median")
            dev = (chk["price"] / chk["p_adj"] / rel - 1).abs()
            price_check["n_checked"] = int(len(chk))
            price_check["share_exact_1e_9"] = float((dev < 1e-9).mean())
            price_check["max_dev"] = float(dev.max())
            by_sym = chk.assign(dev=dev).groupby("symbol")["dev"].agg(["count", "max"])
            price_check["symbols_with_dev_gt_1e_9"] = {
                s: {"n": int(r["count"]), "max_dev": float(r["max"])} for s, r in by_sym[by_sym["max"] > 1e-9].iterrows()
            }
    gap_check = {}
    paired_all = rt[rt["status"] == "paired"]
    for col, label in (("bars_cf_return", "path_vs_csv_gap"), ("aux_cf_return", "auxpath_vs_csv_gap")):
        if col in paired_all.columns:
            v = (paired_all[col] - paired_all["gap_return"]).abs().dropna()
            if len(v):
                gap_check[f"{label}_n"] = int(len(v))
                gap_check[f"{label}_median_absdiff"] = float(v.median())
                gap_check[f"{label}_share_lt_1e_4"] = float((v < 1e-4).mean())
                gap_check[f"{label}_share_lt_1e_2"] = float((v < 1e-2).mean())

    # 汇总
    paired = rt[rt["status"] == "paired"].copy()
    ded = dedup_round_trips(rt)
    summary = {
        "provenance": provenance,
        "prereg": PREREG,
        "n_exit_events": int(len(rt)),
        "n_unpaired": int((rt["status"] != "paired").sum()),
        "n_pairs_pooled": int(len(paired)),
        "n_pairs_dedup": int(len(ded)),
        "pooled": summarise(paired, bars_col="bars_elapsed", n_exits=len(rt)),
        "dedup": summarise(ded, bars_col="bars_elapsed", n_exits=len(rt)),
        "price_calibration": price_check,
        "gap_calibration": gap_check,
        "turnover": turnover_share(rt, df).to_dict("records"),
        "cost_attribution": cost_attribution(rt, df),
        "frequency": frequency(rt, df),
        "path_status_counts": {str(k): int(v) for k, v in rt["path_status"].value_counts().items()},
    }
    if "gap_excess_mkt" in ded.columns and ded["gap_excess_mkt"].notna().any():
        summary["dedup_excess"] = summarise_excess(ded)
    if len(ded) and "bars_elapsed" in ded.columns:
        summary["posthoc_not_preregistered"] = posthoc_slices(ded)
        summary["posthoc_excess_by_slice"] = {
            k: summarise_excess(v) for k, v in {
                "cooldown_timed": ded[ded["bars_elapsed"] <= 6],
                "long_gap_gt_6": ded[ded["bars_elapsed"] > 6],
            }.items() if len(v)
        }
    # 去重口径的 `reentry_rate` 分母是「池化出场数」，比值无经济含义，显式置空
    summary["dedup"]["reentry_rate"] = None
    summary["dedup"]["reentry_rate_note"] = "去重口径无分母，再入率见 pooled"
    verdict, checks = grade(summary["dedup"])
    verdict_pooled, checks_pooled = grade(summary["pooled"])
    summary["verdict_dedup"] = {"verdict": verdict, "checks": checks}
    summary["verdict_pooled"] = {"verdict": verdict_pooled, "checks": checks_pooled}

    # bars 直方图（复现 §16.15.15 的 3/6 机械底，作为管线自校验）
    if "bars_elapsed" in paired and paired["bars_elapsed"].notna().any():
        hist = paired["bars_elapsed"].dropna().astype(int).value_counts().sort_index()
        summary["bars_histogram"] = {int(k): int(v) for k, v in hist.items() if v >= 5}

    rt.to_csv(os.path.join(out_dir, "roundtrips.csv"), index=False)
    by_group(paired, ["run", "strategy", "fold"], bars_col="bars_elapsed").to_csv(
        os.path.join(out_dir, "summary_by_fold.csv"), index=False)
    by_group(paired, ["run", "strategy"], bars_col="bars_elapsed").to_csv(
        os.path.join(out_dir, "summary_by_strategy.csv"), index=False)
    depth = ded.dropna(subset=["exit_depth_bucket"]) if "exit_depth_bucket" in ded else ded.iloc[0:0]
    if len(depth):
        by_group(depth, ["exit_depth_bucket"], bars_col="bars_elapsed").to_csv(
            os.path.join(out_dir, "summary_by_exit_depth.csv"), index=False)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, default=str)

    if not args.quiet:
        print(json.dumps({
            "n_csv": len(paths),
            "n_rows": len(df),
            "n_exit_events": summary["n_exit_events"],
            "n_pairs_pooled": summary["n_pairs_pooled"],
            "n_pairs_dedup": summary["n_pairs_dedup"],
            "verdict_dedup": verdict,
            "verdict_pooled": verdict_pooled,
            "dedup": {k: summary["dedup"].get(k) for k in (
                "pos_share", "gap_mean", "gap_median", "gap_weighted_mean",
                "bars_elapsed_median", "bars_elapsed_n", "cal_days_median")},
            "price_calibration": price_check.get("share_exact_1e_9"),
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
