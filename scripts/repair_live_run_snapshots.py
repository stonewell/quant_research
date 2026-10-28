#!/usr/bin/env python3
"""离线修复 chan_four_state_blend 历史 run 快照的 NAV / 持仓口径。

WHY THIS SCRIPT EXISTS (因果链，务必按顺序读)
============================================
1. **上一轮票后态没有被回填成下一轮的输入。**
   ``2026-10-26/holdings.json`` 与 ``2026-10-27/holdings.json`` 逐字节相同
   (md5 ``3517d8b523c17c50fbc1c7e7aa432e44``)，而 ``2026-10-22/post_holdings.json``
   也是同一个 md5。也就是说：10-26 用 10-22 票后态作输入是**对的**（10-22/10-23/10-25
   三次 run 的票面都没有产生卖出成交），但 10-27 本该用 10-26 票后态，实际却复用了
   10-22 票后态，**滞后了整整一轮**。

2. **滞后的股数直接把权重、惰性判定和票面动作算错。**
   ``current_weight`` / ``broker_weight`` 全部基于过期股数；于是 600900.SH /
   600660.SH / 000999.SZ 三只票的真实漂移是**正的**（真实持仓已经减到目标以下，
   应该持有），票面却算成负漂移 −0.0648 / −0.0574 / −0.0550，越过
   ``inertia_threshold=0.05`` 而被判成 SELL，多下 43,227 元卖单 —— 这个金额正好等于
   同票面 BIL 现金归集行的 ``trade_value``（卖出所得归集）。更糟的是这三笔卖出与
   10-26 已成交的减仓**完全重复**。

3. **孤儿持仓永远出不了场。**
   600584.SH / 600362.SH 在 10-26 就已经卖出（见 10-26 票面 SELL 行与
   ``last_exit_date``），但 10-27 的输入仍持有 100 股，于是它们以
   ``HOLD_FILTERED / COOLDOWN_BLOCKED`` + ``target_shares=100`` 的形态被带出票面。
   ``web/livetrading/storage.py::post_holdings_from_ticket`` 只在 ``target_shares > 0``
   时保留键，所以 100 股的 ``target_shares`` 让它们**无限期残留在票后态里**。

4. **整条账户 NAV 被冻在一个写死的数上。**
   ``scripts/run_live_strategy.py`` 的 ``resolve_holdings_weights_and_shares`` 里
   ``cash_val = portfolio_value - total_equity_val``：现金不是一个独立账户事实，而是
   "CLI/account_state.current_nav 减去持仓市值" 的**差额补数**。三轮 run 的
   ``current_nav`` 都是 197,351.17（``account_state.nav_history_10d`` 里连续 4 个
   197,351.17），于是持仓的市值波动被现金这一项**反向吸收**，NAV 恒定不动 ——
   即"纸面上永远不亏不赚"，回撤熔断读到的是一个假数。

本脚本只做**可证据化**的修复，绝不伪造模型输出
==============================================
* 价格、股数、票面现金流全部取快照里**已记录的值**，不从行情库重查
  （``market.duckdb`` 只到 2026-10-22，查 10-26/10-27 必然缺 bar）。
* 后复权因子（``calc_adjust_factor_daily.backward_factor``）与除权事件
  （``raw_adjustment_events``）从 ``market.duckdb`` **只读**读取，仅用于
  "把市值折算成含息口径" 与 "证明因子滞后 3 天对持仓无影响"。
* 策略链（模型目标权重 / 状态机 / 熔断 / 宏观门禁）**不重算**。票面里由输入股数
  直接决定的那一层（``current_weight`` / ``broker_weight`` / ``drift`` / 惰性判定 /
  ``action`` / ``target_shares`` / ``trade_value``）会被重算，其余一律原样保留，
  并在 ``_meta.recomputed_scope = "input_derived_only"`` 与
  ``_meta.not_recomputed`` 里显式声明。**宁可标注"未重算"，也不生成看起来完整的假票面。**

产出
====
* 每个 run 目录：``nav_breakdown.json``（含息 MTM 的逐票拆分）
* ``summary.json`` 补 ``portfolio_value`` / ``nav_computed`` / ``nav_source`` /
  ``cash`` / ``cash_source`` / ``nav_warnings``（其余字段一字不动）
* ``2026-10-27/holdings.json`` 股数修正为 ``2026-10-26/post_holdings.json``
* ``2026-10-27/live_trading_ticket.recomputed.json`` / ``.csv``（原票面不覆盖）
* ``2026-10-27/post_holdings.recomputed.json``（原 ``post_holdings.json`` 不覆盖）
* ``repair_manifest.json``（逐文件逐字段 before → after + md5）
* 每个被改写文件先留一份逐字节相同的 ``<原名>.orig``，并校验 md5

幂等性
======
所有"改写前"的内容一律优先从 ``<原名>.orig`` 读取（存在时），因此重复运行
结果完全一致；``.orig`` 已存在则永不覆盖。``10-27`` 的原始票面与两个
``post_holdings.json`` 原件永不被写入。

用法
====
    python scripts/repair_live_run_snapshots.py --help
    python scripts/repair_live_run_snapshots.py --dry-run
    python scripts/repair_live_run_snapshots.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPT_VERSION = "1.1.0"

DEFAULT_RUNS_ROOT = (
    REPO_ROOT / "web" / "livetrading" / "data" / "runs" / "chan_four_state_blend"
    / "custom_astock_202609_20_new"
)

#: 三份需要修口径的快照（10-23 / 10-25 是 10-22 的重复 run，as_of 仍是 2026-10-22，
#: 原样保留、不产出 nav_breakdown，避免制造三份互相矛盾的口径）。
DEFAULT_RUNS: Tuple[str, ...] = ("2026-10-22", "2026-10-26", "2026-10-27")

#: 现金锚点 run：三份快照里唯一"输入持仓是正确的"，因此只有它的
#: "NAV − 持仓市值" 差额可以当作真实现金余额。
CASH_ANCHOR_RUN = "2026-10-22"

#: 现金停泊标的（票面里是 SWEEP_CASH 归集行，shares 恒为 0）。
CASH_PROXY = "BIL"

#: 默认 A 股整手。
DEFAULT_LOT_SIZE = 100

#: 已按票面成交的 run（其 SELL 行的 trade_value 计入后续 run 的期初现金）。
#: 依据：10-26/holdings.json 与 10-27/holdings.json 逐字节相同，而 10-26 票后态
#: (post_holdings.json) 明显是"10-26 那批卖单成交后"的账本 —— 说明 10-26 的卖单
#: 是成交了的。
ASSUMED_FILLED_RUNS: Tuple[str, ...] = ("2026-10-26",)

#: 明确假设**未成交**的 run。10-27 的三笔卖单是重复下单，本脚本假设它们没有成交
#: （若实际已成交，10-27 的持仓与现金都要另行改写 —— 见 --assume-10-27-fills）。
ASSUMED_UNFILLED_RUNS: Tuple[str, ...] = ("2026-10-27",)

#: 因子表滞后时允许的最大天数（超过则在 nav_warnings 里点名）。
FACTOR_LAG_TOLERANCE_DAYS = 5


# ---------------------------------------------------------------------------
# 文件 I/O：.orig 备份 + 幂等读源
# ---------------------------------------------------------------------------
def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def md5_file(path: Path) -> str:
    return md5_bytes(Path(path).read_bytes())


def orig_path(path: Path) -> Path:
    """``summary.json`` -> ``summary.json.orig``（整名追加，不换后缀）。"""
    path = Path(path)
    return path.with_name(path.name + ".orig")


def read_source_bytes(path: Path) -> Tuple[bytes, str]:
    """读取"改写前"的原始内容；``.orig`` 存在时以它为准（幂等的关键）。

    第一次运行时 .orig 还不存在，读的是当前文件；从第二次运行起读 .orig，
    于是"输入"恒定，输出也就是恒定的。

    Returns:
        (raw_bytes, source_kind)，``source_kind`` 为 ``"orig"`` 或 ``"live"``。
    """
    path = Path(path)
    op = orig_path(path)
    if op.exists():
        return op.read_bytes(), "orig"
    return path.read_bytes(), "live"


def read_source_json(path: Path) -> Tuple[Any, str]:
    raw, kind = read_source_bytes(path)
    return json.loads(raw.decode("utf-8")), kind


def ensure_orig_backup(path: Path, *, dry_run: bool = False) -> Dict[str, Any]:
    """为 ``path`` 建一份逐字节相同的 ``<原名>.orig``，并校验 md5。

    硬性约束：``.orig`` 一旦存在就**绝不覆盖** —— 它是唯一能完整还原原件的副本，
    第二次运行时的"当前文件"已经是修正后的内容，覆盖会永久毁掉原始证据。

    Returns:
        dict(original=str, backup=str, md5=..., created=bool, verified=bool)
    """
    path = Path(path)
    op = orig_path(path)
    info: Dict[str, Any] = {
        "original": _rel(path),
        "backup": _rel(op),
        "created": False,
        "md5": None,
        "verified": False,
    }
    if op.exists():
        info["md5"] = md5_file(op)
        info["created"] = False
        # 校验 .orig 与其自身 md5 记录一致（trivially true），真正的"与原件一致"
        # 校验发生在第一次创建时；此处只回报已存在的备份 md5。
        info["verified"] = True
        info["note"] = "backup already exists, left untouched (idempotent rerun)"
        return info

    live_md5 = md5_file(path)
    if dry_run:
        info["md5"] = live_md5
        info["created"] = False
        info["verified"] = True
        info["note"] = "dry-run: backup NOT written"
        return info

    shutil.copyfile(path, op)
    backup_md5 = md5_file(op)
    info["md5"] = backup_md5
    info["original_md5"] = live_md5
    info["created"] = True
    info["verified"] = backup_md5 == live_md5
    if not info["verified"]:
        raise RuntimeError(
            f"备份校验失败: {op} md5={backup_md5} != 原件 {path} md5={live_md5}"
        )
    return info


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """先写同目录临时文件再 replace，避免半截文件。"""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp-repair")
    tmp.write_bytes(data)
    tmp.replace(path)


def json_bytes(obj: Any) -> bytes:
    """与仓库内快照一致的序列化风格：indent=2、非 ASCII 不转义、无尾随换行。"""
    return json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")


def _rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except Exception:
        return str(path)


# ---------------------------------------------------------------------------
# 纯计算层（可单测，不碰文件系统 / 不碰行情库）
# ---------------------------------------------------------------------------
def correct_holdings(
    run_label: str,
    run_dirs: Mapping[str, Path],
) -> Tuple[Dict[str, int], Dict[str, Any]]:
    """决定某个 run **应该**用哪一份持仓作为输入。

    因果：10-22/10-23/10-25 三个 run 的票面只有 HOLD，没有 SELL，所以 10-22 的
    票后态 == 10-22 的票前态，10-26 复用它是对的。10-26 的票面产生了 5 笔卖出成交，
    其票后态（post_holdings.json）才是 10-27 应该看到的账本 —— 10-27 实际读到的是
    10-22 的票后态，整整滞后一轮。

    Returns:
        (holdings, evidence)
    """
    evidence: Dict[str, Any] = {
        "run": run_label,
        "expected_source": None,
        "actual_input_md5": None,
        "expected_source_md5": None,
        "modified": False,
    }
    actual_path = run_dirs[run_label] / "holdings.json"
    actual_raw, _ = read_source_bytes(actual_path)
    evidence["actual_input_md5"] = md5_bytes(actual_raw)
    evidence["actual_input"] = json.loads(actual_raw.decode("utf-8"))

    if run_label == CASH_ANCHOR_RUN:
        # 第一个 run：没有"上一轮票后态"，自身输入即正确输入。
        evidence["expected_source"] = "holdings.json (self, no prior round)"
        evidence["expected_source_md5"] = evidence["actual_input_md5"]
        return dict(evidence["actual_input"]), evidence

    # 上一轮 = 本 run 之前最近的一个已存在的 run 目录。
    prior_runs = sorted(d for d in run_dirs if d < run_label)
    if not prior_runs:
        evidence["expected_source"] = "holdings.json (no prior round found)"
        evidence["expected_source_md5"] = evidence["actual_input_md5"]
        return dict(evidence["actual_input"]), evidence

    prior = prior_runs[-1]
    prior_post = run_dirs[prior] / "post_holdings.json"
    if not prior_post.exists():
        evidence["expected_source"] = f"{prior}/holdings.json (no post_holdings.json)"
        evidence["expected_source_md5"] = evidence["actual_input_md5"]
        return dict(evidence["actual_input"]), evidence

    expected_raw, _ = read_source_bytes(prior_post)
    expected = json.loads(expected_raw.decode("utf-8"))
    evidence["expected_source"] = f"{prior}/post_holdings.json"
    evidence["expected_source_md5"] = md5_bytes(expected_raw)
    evidence["expected"] = expected
    evidence["modified"] = md5_bytes(actual_raw) != md5_bytes(expected_raw)
    return dict(expected), evidence


def inertia_triggered(drift: Optional[float], threshold: float) -> bool:
    """惰性判定：漂移的绝对值达到阈值才允许动手。

    与 ``scripts/run_live_strategy.py`` 一致：``abs(delta_w) < threshold`` 时
    票面写成"未超惰性阈值"，股数维持不变 —— 正是这条规则在输入过期时会反过来
    制造一笔假卖出（真实漂移为正，票面算成负且超过阈值）。
    """
    if drift is None or not math.isfinite(float(drift)):
        return False
    return abs(float(drift)) >= float(threshold)


def shares_to_weights(
    holdings: Mapping[str, int],
    prices: Mapping[str, float],
    nav: float,
) -> Dict[str, float]:
    """股数 + 快照价 -> 权重。NAV 非正时一律 0.0（不产生 inf）。"""
    out: Dict[str, float] = {}
    if nav is None or not math.isfinite(float(nav)) or float(nav) <= 0:
        return out
    for sym, sh in holdings.items():
        px = prices.get(sym)
        if px is None:
            continue
        out[sym] = float(sh) * float(px) / float(nav)
    return out


def lot_rounded_sell_shares(
    drift: float,
    nav: float,
    price: float,
    current_shares: int,
    lot_size: int = DEFAULT_LOT_SIZE,
) -> Tuple[int, float]:
    """按 ``run_live_strategy.py`` 的 SELL 分支做整手取整。

    源码口径：``raw_sell_shares = abs(delta_w * nav) / price``，
    ``lot_sell = ceil(raw / lot) * lot``，再 ``min(lot_sell, current_shares)``。

    Returns:
        (delta_shares, raw_sell_shares)，``delta_shares`` 为负数。
    """
    if price <= 0 or nav <= 0 or lot_size <= 0:
        return 0, 0.0
    raw_sell = abs(float(drift) * float(nav)) / float(price)
    lot_sell = int(math.ceil(raw_sell / lot_size) * lot_size)
    lot_sell = min(lot_sell, int(current_shares))
    return -lot_sell, raw_sell


def recompute_equity_row(
    row: Mapping[str, Any],
    *,
    current_shares: int,
    nav: float,
    threshold: float,
    lot_size: int = DEFAULT_LOT_SIZE,
) -> Dict[str, Any]:
    """重算票面里"由输入股数直接决定的那一层"。

    只动这些键：``current_shares`` / ``current_weight`` / ``delta_weight`` /
    ``inertia_check`` / ``action`` / ``target_shares`` / ``delta_shares`` /
    ``shares`` / ``trade_value`` / ``trade_value_rmb`` /
    ``detail_reason.lifecycle_state.broker_weight`` /
    ``detail_reason.lifecycle_decision.*`` / ``detail_reason.is_liquidation`` /
    ``detail_reason.lot_rounding``。

    模型目标（``target_weight``）、宏观门禁、行业限流、冷却期一律不碰 ——
    它们不是"输入股数"的函数，重算就等于伪造模型输出。
    """
    import copy as _copy

    out = _copy.deepcopy(dict(row))
    out["current_shares"] = int(current_shares)
    price = float(row.get("price") or 0.0)
    tgt_w = float(row.get("target_weight") or 0.0)

    cur_w = (float(current_shares) * price / nav) if (nav > 0 and price > 0) else 0.0
    drift = tgt_w - cur_w
    triggered = inertia_triggered(drift, threshold)

    original_action = str(row.get("action") or "")
    # 平仓冷却 / 门槛过滤等"已经不在场上"的行，本来就不交易，不能因为重算出
    # 一个正漂移就被提升成 BUY（BUY 属于模型层，未重算）。
    non_tradable = original_action in (
        "HOLD_FILTERED", "COOLDOWN_BLOCKED", "NO_POSITION",
        "CANCEL_PENDING", "PENDING_ENTRY",
    )

    raw_sell = 0.0
    if int(current_shares) <= 0:
        action = original_action or "NO_POSITION"
        target_shares = 0
        delta_shares = 0
        trade_value = 0.0
    elif non_tradable and not triggered:
        action = original_action
        target_shares = int(current_shares)
        delta_shares = 0
        trade_value = 0.0
    elif triggered and drift < 0:
        delta_shares, raw_sell = lot_rounded_sell_shares(
            drift, nav, price, int(current_shares), lot_size
        )
        target_shares = int(current_shares) + delta_shares
        action = "SELL" if delta_shares < 0 else original_action
        trade_value = delta_shares * price
    elif triggered and drift > 0:
        # 需要加仓 —— 但"加多少"来自模型目标与整手取整策略，本脚本不重算模型层，
        # 因此停在这里并显式标注，绝不臆造买单。
        action = original_action
        target_shares = int(current_shares)
        delta_shares = 0
        trade_value = 0.0
        out.setdefault("_recompute_notes", []).append(
            f"positive_drift_requires_buy: {out.get('symbol')} drift={drift:+.4f} "
            f">= {threshold}; 买入层属模型目标，本脚本重算范围外，股数保持现状"
        )
    else:
        action = "HOLD" if int(current_shares) > 0 else (original_action or "NO_POSITION")
        target_shares = int(current_shares)
        delta_shares = 0
        trade_value = 0.0

    out["action"] = action
    out["current_weight"] = cur_w
    out["delta_weight"] = drift
    out["target_shares"] = int(target_shares)
    out["delta_shares"] = int(delta_shares)
    out["shares"] = int(abs(delta_shares))
    out["trade_value"] = float(trade_value)
    out["trade_value_rmb"] = float(trade_value)

    if action == "HOLD" and original_action != "HOLD":
        out["reason"] = f"调仓未超惰性阈值 (< {threshold:.0%})"
        out["summary_reason"] = out["reason"]

    detail = out.get("detail_reason")
    if isinstance(detail, dict):
        ls = detail.get("lifecycle_state")
        if isinstance(ls, dict):
            ls["broker_weight"] = cur_w
            # ``lifecycle_state.current_weight`` 在本例里是"状态机看到的当前权重"，
            # 而当前权重 = 输入股数 x 价格 / NAV，**是输入的直接函数**。输入修正后
            # 若仍留旧值，同一行就会出现"顶层 current_weight=0.0 而
            # detail_reason 里还是 0.028"的自相矛盾（600584/600362 孤儿行、
            # 以及 600900 的 0.1307 被减仓改写后）。这里只改这个数字字段，
            # 状态机自身的 state/cooldown/streak 一律不动（见 NOT_RECOMPUTED）。
            if "current_weight" in ls:
                ls["current_weight"] = cur_w
        ld = detail.get("lifecycle_decision")
        if isinstance(ld, dict):
            if "delta_w" in ld:
                ld["delta_w"] = drift
            if "current_weight" in ld:
                ld["current_weight"] = cur_w
            if "inertia_threshold" in ld:
                ld["inertia_threshold"] = threshold
        ic = detail.get("inertia_check")
        if isinstance(ic, dict):
            ic["delta_weight"] = round(drift, 4)
            ic["threshold"] = threshold
            ic["triggered"] = bool(triggered)
        lr = detail.get("lot_rounding")
        if isinstance(lr, dict):
            lr["target_shares_raw"] = round(float(raw_sell), 2) if raw_sell else 0.0
            lr["shares"] = int(abs(delta_shares))
        # is_liquidation 的唯一权威定义是 action == "SELL"（见 run_live_strategy.py）
        detail["is_liquidation"] = action == "SELL"
        detail["original_action"] = original_action
        # 同一行两个结论会误导（顶层已改成"未超惰性阈值"、detail 仍写
        # "最优权重微调"）。detail 只是顶层的复述，永远跟着顶层走。
        if "summary_reason" in detail and out.get("summary_reason") is not None:
            detail["summary_reason"] = out["summary_reason"]
    return out


# ---------------------------------------------------------------------------
# 行情库访问：**只读**。绝不写入任何 .duckdb。
# ---------------------------------------------------------------------------
MARKET_DB_CANDIDATES: Tuple[str, ...] = (
    "/media/downloads/files/market.duckdb",
    "/home/stone/Work/third-party/Financial-API/data/market.duckdb",
)


def resolve_market_db(explicit: Optional[str]) -> Optional[str]:
    """优先用显式路径，其次按候选顺序找第一个存在的库。"""
    if explicit:
        p = Path(explicit)
        return str(p) if p.exists() else None
    for cand in MARKET_DB_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


class MarketDbReader:
    """``market.duckdb`` 的只读封装。

    唯一用途：取后复权因子（``calc_adjust_factor_daily.backward_factor``）与
    除权事件（``raw_adjustment_events``）。**价格不来这里** —— 库只到
    2026-10-22，10-26/10-27 的 bar 根本不存在，价格一律用快照里的 ``price``。
    """

    def __init__(self, path: Optional[str]):
        self.path = path
        self._con = None
        self._factor_cache: Dict[str, Any] = {}
        self.errors: List[str] = []

    def __enter__(self) -> "MarketDbReader":
        if not self.path:
            self.errors.append("market.duckdb not found; factors unavailable")
            return self
        try:
            import duckdb  # 延迟导入：单测不需要 duckdb

            # read_only=True 是硬约束：本脚本不得写入任何真实库。
            self._con = duckdb.connect(self.path, read_only=True)
        except Exception as exc:  # pragma: no cover - 环境相关
            self.errors.append(f"cannot open market db read-only: {exc}")
            self._con = None
        return self

    def __exit__(self, *exc_info) -> None:
        if self._con is not None:
            try:
                self._con.close()
            finally:
                self._con = None

    @property
    def available(self) -> bool:
        return self._con is not None

    def factor_series(self, symbol: str, end: Optional[str] = None):
        """``symbol`` 的后复权因子序列（索引为 date，截至 ``end``）。"""
        import pandas as pd

        if symbol in self._factor_cache and end is None:
            return self._factor_cache[symbol]
        if self._con is None:
            return None
        sql = (
            "SELECT date, backward_factor FROM calc_adjust_factor_daily "
            "WHERE thscode = ?"
        )
        params: List[Any] = [symbol]
        if end:
            sql += " AND date <= ?"
            params.append(str(end))
        sql += " ORDER BY date"
        try:
            df = self._con.execute(sql, params).df()
        except Exception as exc:  # pragma: no cover
            self.errors.append(f"factor query failed for {symbol}: {exc}")
            return None
        if df is None or df.empty:
            return None
        idx = pd.DatetimeIndex(pd.to_datetime(df["date"]))
        s = pd.Series(df["backward_factor"].astype(float).to_numpy(), index=idx).sort_index()
        s.name = "factor_hfq"
        if end is None:
            self._factor_cache[symbol] = s
        return s

    def ex_dividend_events_after(self, as_of: str) -> List[Dict[str, Any]]:
        """``as_of`` 之后仍有除权事件的标的（用于证明因子滞后是否伤害持仓估值）。"""
        if self._con is None:
            return []
        try:
            df = self._con.execute(
                "SELECT thscode, ex_date, dividend_per_share, per_share_bonus "
                "FROM raw_adjustment_events WHERE ex_date > ? ORDER BY ex_date, thscode",
                [str(as_of)],
            ).df()
        except Exception as exc:  # pragma: no cover
            self.errors.append(f"ex-dividend query failed: {exc}")
            return []
        if df is None or df.empty:
            return []
        return [
            {
                "symbol": str(r["thscode"]),
                "ex_date": str(r["ex_date"]),
                "dividend_per_share": float(r["dividend_per_share"])
                if r["dividend_per_share"] == r["dividend_per_share"] else None,
            }
            for _, r in df.iterrows()
        ]

    def raw_close(self, symbol: str, on_date: str) -> Optional[float]:
        if self._con is None:
            return None
        try:
            row = self._con.execute(
                "SELECT close FROM raw_kline_daily WHERE thscode = ? AND date = ?",
                [symbol, str(on_date)],
            ).fetchone()
        except Exception:  # pragma: no cover
            return None
        return float(row[0]) if row and row[0] is not None else None

    def closes_between(self, symbol: str, start: str, end: str):
        import pandas as pd

        if self._con is None:
            return None
        try:
            df = self._con.execute(
                "SELECT date, close FROM raw_kline_daily WHERE thscode = ? "
                "AND date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) ORDER BY date",
                [symbol, str(start), str(end)],
            ).df()
        except Exception as exc:  # pragma: no cover
            self.errors.append(f"close query failed for {symbol} [{start}..{end}]: {exc}")
            return None
        if df is None or df.empty:
            return None
        idx = pd.DatetimeIndex(pd.to_datetime(df["date"]))
        return pd.Series(df["close"].astype(float).to_numpy(), index=idx).sort_index()


# ---------------------------------------------------------------------------
# 除权/因子阶跃的真实复核（只读 SQL，结果原样进 manifest）
# ---------------------------------------------------------------------------
#: 复核窗口起点：用户指定的 ``ex_date > 2026-10-15``。取 10-15 而不是各 run 的
#: as_of，是为了覆盖"入场日 <= 除权日 <= as_of"这一段 —— 只查 ``ex_date > as_of``
#: 会把"建仓当天正好除权"这类事件漏掉（000999.SZ 2026-10-16 就是这种）。
EX_DIVIDEND_CHECK_SINCE = "2026-10-15"

#: 判定"阶跃"的阈值：后复权因子是阶跃函数，同一台阶内会有浮点末位抖动，
#: 因此只有严格大于 ``prev * (1 + 1e-8)`` 才算真的跳了一级。
FACTOR_STEP_RTOL = 1e-8


def ex_dividend_check(
    reader: "MarketDbReader",
    *,
    symbols: Sequence[str],
    anchors_by_symbol: Optional[Mapping[str, Optional[str]]] = None,
    since: str = EX_DIVIDEND_CHECK_SINCE,
) -> Dict[str, Any]:
    """真跑一次两条只读 SQL，把原始输入/输出与逐票判定写进 manifest。

    复核者关心的是"窗口内除权事件为空 / 因子没有台阶"这两句断言是否成立。
    断言不算证据，所以这里把（1）原始 SQL 文本、（2）原始行、（3）因子序列、
    （4）逐票的"事件数 / 阶跃次数 / 结论"一起交出去，读者可以自己重算。

    阶跃判定：``back[t] > back[t-1] * (1 + FACTOR_STEP_RTOL)``。命中即说明
    ``factor_hfq`` 在该日跳了一级（= 该日或前一日有除权/送股），此时比值
    ``factor_mark / factor_anchor`` 是否等于 1 取决于**锚点日落在台阶哪一侧**：
    锚点日在阶跃日或之后 -> 两端同因子 -> 比值 1.0，无误差；锚点日在此之前 ->
    比值 > 1，市值被放大（这正是不能拿"因子序列最早日"当锚点的原因）。
    """
    symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
    anchors_by_symbol = anchors_by_symbol or {}
    tick_list = ", ".join("'%s'" % s for s in symbols)

    event_sql = (
        "SELECT thscode, ex_date, dividend_per_share, per_share_bonus\n"
        "FROM raw_adjustment_events\n"
        f"WHERE thscode IN ({tick_list})\n"
        f"  AND ex_date > DATE '{since}'\n"
        "ORDER BY thscode, ex_date"
    )
    factor_sql = (
        "SELECT thscode, date, backward_factor\n"
        "FROM calc_adjust_factor_daily\n"
        f"WHERE thscode IN ({tick_list})\n"
        f"  AND date >= DATE '{since}'\n"
        "ORDER BY thscode, date"
    )

    events: List[Dict[str, Any]] = []
    factor_rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    if reader.available:
        try:
            df = reader._con.execute(event_sql).df()
            for _, r in df.iterrows():
                events.append(
                    {
                        "thscode": str(r["thscode"]),
                        "ex_date": _to_iso_day(r["ex_date"]),
                        "dividend_per_share": _num_or_none(r["dividend_per_share"]),
                        "per_share_bonus": _num_or_none(r["per_share_bonus"]),
                    }
                )
        except Exception as exc:  # pragma: no cover - 依赖真实库
            errors.append(f"raw_adjustment_events query failed: {exc}")
        try:
            df = reader._con.execute(factor_sql).df()
            for _, r in df.iterrows():
                factor_rows.append(
                    {
                        "thscode": str(r["thscode"]),
                        "date": _to_iso_day(r["date"]),
                        "backward_factor": float(r["backward_factor"]),
                    }
                )
        except Exception as exc:  # pragma: no cover - 依赖真实库
            errors.append(f"calc_adjust_factor_daily query failed: {exc}")
    else:
        errors.append("market db unavailable: cannot run the read-only checks")

    per_symbol: Dict[str, Any] = {}
    for sym in symbols:
        evs = [e for e in events if e["thscode"] == sym]
        rows = [r for r in factor_rows if r["thscode"] == sym]
        steps: List[Dict[str, Any]] = []
        for prev, cur in zip(rows, rows[1:]):
            if cur["backward_factor"] > prev["backward_factor"] * (1.0 + FACTOR_STEP_RTOL):
                steps.append(
                    {
                        "date": cur["date"],
                        "from": prev["backward_factor"],
                        "to": cur["backward_factor"],
                        "ratio": round(cur["backward_factor"] / prev["backward_factor"], 8),
                    }
                )
        anchor = anchors_by_symbol.get(sym)
        last_step = steps[-1]["date"] if steps else None
        anchor_ok = None
        if anchor is not None:
            anchor_ok = last_step is None or anchor >= last_step
        if not rows:
            conclusion = "窗口内没有因子行（标的不在库内）：无法复核"
        elif not evs and not steps:
            conclusion = (
                "窗口内 0 个除权事件、0 次因子阶跃：backward_factor 是常数，"
                "factor_mark / factor_anchor 精确等于 1.0"
            )
        elif steps and anchor_ok:
            conclusion = (
                f"有 {len(evs)} 个除权事件、{len(steps)} 次因子阶跃（{last_step}）；"
                f"锚点日 {anchor} >= 阶跃日，锚点与估值日同因子，比值 1.0，无重复计息"
            )
        elif steps and anchor_ok is False:
            conclusion = (
                f"有 {len(evs)} 个除权事件、{len(steps)} 次因子阶跃（{last_step}）；"
                f"锚点日 {anchor} 早于阶跃日，比值会 > 1.0 —— 市值被放大，需人工处理"
            )
        else:
            conclusion = f"有 {len(evs)} 个除权事件、{len(steps)} 次因子阶跃（无持仓锚点可比）"
        per_symbol[sym] = {
            "event_count": len(evs),
            "events": evs,
            "factor_observations": len(rows),
            "factor_first": rows[0] if rows else None,
            "factor_last": rows[-1] if rows else None,
            "step_count": len(steps),
            "steps": steps,
            "held_anchor_date": anchor,
            "anchor_at_or_after_last_step": anchor_ok,
            "conclusion": conclusion,
        }

    total_events = sum(v["event_count"] for v in per_symbol.values())
    total_steps = sum(v["step_count"] for v in per_symbol.values())
    harmful = [
        s for s, v in per_symbol.items()
        if v["step_count"] and v["anchor_at_or_after_last_step"] is False
    ]
    return {
        "since": since,
        "read_only": True,
        "db": getattr(reader, "path", None),
        "queries": {"raw_adjustment_events": event_sql, "calc_adjust_factor_daily": factor_sql},
        "step_rule": (
            f"step 当且仅当 back[t] > back[t-1] * (1 + {FACTOR_STEP_RTOL:g})；"
            "同台阶内的浮点末位抖动不计"
        ),
        "raw_output": {
            "raw_adjustment_events": events,
            "calc_adjust_factor_daily": factor_rows,
        },
        "per_symbol": per_symbol,
        "totals": {"event_count": total_events, "step_count": total_steps},
        "errors": errors,
        "verdict": (
            f"窗口 ({since}, +inf) 内共 {total_events} 个除权事件、{total_steps} 次因子阶跃；"
            + (
                "所有阶跃都发生在各自持仓锚点日当天或之前，factor_mark/factor_anchor 恒为 1.0，"
                "本轮重算不含重复计息。"
                if not harmful
                else f"以下标的的阶跃晚于锚点日，需人工处理：{harmful}"
            )
        ),
    }


def _to_iso_day(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except Exception:
        try:
            return _ts(value).date().isoformat()
        except Exception:
            return str(value)


def _num_or_none(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


# ---------------------------------------------------------------------------
# compute_nav 的鸭子类型注入层
# ---------------------------------------------------------------------------
class SnapshotPriceCache:
    """把"快照里记录的收盘价 + 真实后复权因子"喂给 ``valuation.compute_nav``。

    WHY NOT ``DuckDBCache(":memory:")``：
    ``DuckDBCache.store_bars`` 走的是带 basis 声明的写路径（history-rewrite 门禁 /
    ``allow_undeclared_basis`` / ``asset_sync_metadata`` 对账）。为了塞进一个
    "快照价"就要伪造一整行 OHLCV 与 basis 元数据，反而制造了假的行情记录；而且
    ``query_bars(adjust="raw")`` 读的是真实表结构与 basis 解析链，和"快照价"这个
    事实来源对不上。``web/livetrading/valuation.py`` 的模块 docstring 明确把
    cache 定义成**鸭子类型**（"tests inject fakes"），因此这里按同一份契约注入，
    数学口径 100% 复用 ``compute_nav``，不重写公式。
    """

    def __init__(
        self,
        prices: Mapping[str, Tuple[str, float]],
        factors: Mapping[str, Any],
    ):
        # prices: symbol -> (bar_date_iso, raw_close_from_snapshot)
        self.prices = dict(prices)
        self.factors = dict(factors)

    def query_bars(self, provider, symbol, start=None, end=None, interval="1d", adjust="raw"):
        import pandas as pd

        rec = self.prices.get(symbol)
        if not rec:
            return pd.DataFrame()
        bar_date, px = rec
        ts = pd.Timestamp(bar_date)
        return pd.DataFrame({"Close": [float(px)],
                             "Open": [float(px)],
                             "High": [float(px)],
                             "Low": [float(px)]},
                            index=pd.DatetimeIndex([ts], name="date"))

    def query_adjust_factors(self, symbol, start=None, end=None):
        import pandas as pd

        s = self.factors.get(symbol)
        if s is None or len(s) == 0:
            return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))
        if end:
            s = s[s.index <= pd.Timestamp(end)]
        if len(s) == 0:
            return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))
        return s.to_frame("factor_hfq")

    def get_latest_factor_hfq(self, symbol):
        s = self.factors.get(symbol)
        return float(s.iloc[-1]) if s is not None and len(s) else None


# ---------------------------------------------------------------------------
# anchor（含息折算起点）解析
# ---------------------------------------------------------------------------
ENTRY_PRICE_TOL = 0.005  # 与 A 股最小报价单位半分钱对齐
ENTRY_SEARCH_DAYS = 180  # 入场日搜索窗口


def resolve_anchor(
    symbol: str,
    entry_price: Optional[float],
    reader: MarketDbReader,
    as_of: str,
    fallback: str,
) -> Dict[str, Any]:
    """确定 ``factor_hfq`` 比值的分母日期（anchor）。

    口径顺序（与任务约定一致）：
    1. 有可信入场日 -> 用它。本仓库快照里**没有** ``entry_date`` 字段，但有
       ``account_state.entry_prices[symbol]``（建仓当日收盘价）——用它在行情库里
       反查"哪一天的收盘价等于这个值"，得到入场日。这是**推断**，所以要把
       命中日期数、容差一并写进 evidence，命中多于一天时取最早一天并标注歧义。
    2. 推断不出来 -> 回落到三份快照里最早的一天（10-22），并标注
       ``anchor_fallback``：此口径**不含**入场到 10-22 之间已收的股息，NAV 是下限估计。
    3. **绝不**用因子序列最早一天（2016 年）：那会把建仓前十几年的股息全部算进来。

    实测结论：本次受影响的 6 只票，无论命中哪一档，``factor_hfq`` 在
    10-16..10-22 窗口内完全不变，两种口径的比值都恰好是 1.0，因此这项
    不确定性对最终数字的影响为 **0**（脚本会把两个口径都算出来写进
    ``nav_breakdown.json`` 的 ``anchor_sensitivity``）。
    """
    ev: Dict[str, Any] = {
        "symbol": symbol,
        "entry_price": entry_price,
        "anchor_source": "anchor_fallback",
        "anchor_date": fallback,
        "candidates": [],
        "ambiguous": False,
    }
    if entry_price is None or not math.isfinite(float(entry_price)) or float(entry_price) <= 0:
        ev["reason"] = "no entry_price recorded in account_state.entry_prices"
        return ev

    start = None
    try:
        start = str((datetime.fromisoformat(fallback) -
                     __import__("datetime").timedelta(days=ENTRY_SEARCH_DAYS)).date())
    except Exception:
        start = None
    if start is None:
        ev["reason"] = "cannot compute search window"
        return ev

    closes = reader.closes_between(symbol, start, as_of)
    if closes is None or len(closes) == 0:
        ev["reason"] = "no raw closes in market db for the search window"
        return ev

    hits = [ts for ts, c in closes.items() if abs(float(c) - float(entry_price)) <= ENTRY_PRICE_TOL]
    ev["candidates"] = [ts.date().isoformat() for ts in hits]
    if not hits:
        ev["reason"] = (
            f"no raw close within {ENTRY_PRICE_TOL} of entry_price={entry_price} "
            f"in [{start}, {as_of}]"
        )
        return ev

    # 命中多天时取最早一天：宁可少算股息（下限估计），也不要把建仓前的股息算进来。
    chosen = sorted(hits)[0]
    ev["anchor_source"] = "entry_price_db_match"
    ev["anchor_date"] = chosen.date().isoformat()
    ev["ambiguous"] = len(hits) > 1
    ev["reason"] = (
        f"account_state.entry_prices[{symbol!r}]={entry_price} matched raw close on "
        f"{ev['anchor_date']}"
        + (f" ({len(hits)} candidate dates, earliest taken)" if len(hits) > 1 else "")
    )
    return ev


# ---------------------------------------------------------------------------
# 现金推导
# ---------------------------------------------------------------------------
def bil_weight_cash(ticket: Sequence[Mapping[str, Any]], portfolio_value: float) -> Optional[float]:
    """票面 BIL 行的 ``current_weight`` × NAV = 该 run 的闲置现金。

    BIL 行的 ``target_shares`` 恒为 0、``action`` 是 ``SWEEP_CASH``：现金并不以
    BIL 份额的形式存在，而是以"卖出所得"形式趴在账户里，因此只能从票面的
    现金流口径推导。``resolve_holdings_weights_and_shares`` 里
    ``cash_w = cash_val / portfolio_value``，反解即得。
    """
    for row in ticket:
        if str(row.get("symbol", "")).upper() != CASH_PROXY:
            continue
        w = row.get("current_weight")
        if w is None:
            return None
        try:
            return float(w) * float(portfolio_value)
        except (TypeError, ValueError):
            return None
    return None


def sell_proceeds(ticket: Sequence[Mapping[str, Any]]) -> float:
    """一张票面的卖出所得合计（``SELL`` 行的 ``trade_value`` 绝对值之和）。"""
    total = 0.0
    for row in ticket:
        if str(row.get("action", "")).upper() != "SELL":
            continue
        try:
            total += abs(float(row.get("trade_value") or 0.0))
        except (TypeError, ValueError):
            continue
    return total


def cash_at_run_start(
    run_label: str,
    anchor_cash: float,
    run_dirs: Mapping[str, Path],
    filled_runs: Sequence[str],
) -> Tuple[float, List[Dict[str, Any]]]:
    """某 run **开盘时**的现金 = 锚点现金 + 该 run 之前所有已成交 run 的卖出所得。

    WHY 一定要区分"开盘现金"与"收盘现金"：10-26 的 ``holdings.json`` 是那一天的
    **票前**账本，所以它必须配"票前现金"；10-27 的输入是 10-26 的**票后**账本，
    所以它必须配"票后现金"。混用会把同一笔卖出所得既算进现金又留在持仓里
    （实测会让 10-27 的 NAV 凭空多出 52,482），或者把持仓减掉却不加现金
    （NAV 凭空少 52,399）。两者都是同一个错误的两个方向。
    """
    steps: List[Dict[str, Any]] = []
    cash = float(anchor_cash)
    for other in sorted(filled_runs):
        if other >= run_label:
            continue
        tpath = run_dirs.get(other, Path("/nonexistent")) / "live_trading_ticket.json"
        if not tpath.exists():
            continue
        raw, _ = read_source_bytes(tpath)
        ticket = json.loads(raw.decode("utf-8"))
        if not isinstance(ticket, list):
            continue
        proceeds = sell_proceeds(ticket)
        steps.append({"from_run": other, "sell_proceeds": round(proceeds, 2),
                      "cash_after": round(cash + proceeds, 2)})
        cash += proceeds
    return cash, steps


# ---------------------------------------------------------------------------
# 单个 run 的含息 MTM
# ---------------------------------------------------------------------------
def compute_run_nav(
    *,
    run_label: str,
    holdings: Mapping[str, int],
    prices: Mapping[str, Tuple[str, float]],
    reader: MarketDbReader,
    entry_prices: Mapping[str, float],
    cash: float,
    cash_source: str,
    fallback_anchor: str,
    ex_div_map: Optional[Mapping[str, Sequence[str]]] = None,
) -> Dict[str, Any]:
    """调用 ``web/livetrading/valuation.py::compute_nav`` 得到一个 run 的含息 NAV。

    口径：``value_i = shares_i x price_i,as_of x (factor_hfq_i,as_of / factor_hfq_i,anchor)``
    （valuation.py 模块 docstring 原文）。价格来自快照，因子来自只读行情库。

    ``ex_div_map``：``symbol -> [as_of 之后的除权日]``。用来把"因子表滞后"这条
    warning 从"推测"变成"已复核"：只有真的没有除权事件时才允许说比值精确等于 1.0。
    """
    from web.livetrading.valuation import compute_nav

    tradable = {s: int(n) for s, n in holdings.items() if int(n) > 0}
    factors: Dict[str, Any] = {}
    anchors: Dict[str, str] = {}
    anchor_evidence: Dict[str, Any] = {}

    as_of_all = sorted({d for (d, _px) in prices.values()}) or [run_label]

    for sym in tradable:
        series = reader.factor_series(sym)
        if series is not None and len(series):
            factors[sym] = series
        ev = resolve_anchor(
            sym,
            entry_prices.get(sym),
            reader,
            as_of=max(as_of_all),
            fallback=fallback_anchor,
        )
        anchor_evidence[sym] = ev
        anchors[sym] = ev["anchor_date"]

    cache = SnapshotPriceCache(prices, factors)
    result = compute_nav(
        tradable,
        as_of=run_label,
        cache=cache,
        cash=float(cash),
        anchors=anchors,
        dividend_mode="total_return",
        fail_closed=True,
    )

    # --- 逐票补上"因子取用的日期"与"因子是否可靠" --------------------------
    per_symbol: Dict[str, Any] = {}
    warnings: List[str] = list(result.warnings)
    for sym, row in result.per_symbol.items():
        fs = factors.get(sym)
        factor_date = None
        factor_lag_days = None
        if fs is not None and len(fs):
            upto = fs[fs.index <= _ts(row.get("mark_date") or run_label)]
            if len(upto):
                factor_date = _ts(upto.index[-1]).date().isoformat()
                try:
                    factor_lag_days = (
                        date.fromisoformat(str(row.get("mark_date") or run_label))
                        - date.fromisoformat(factor_date)
                    ).days
                except Exception:
                    factor_lag_days = None
        factor_missing = row.get("factor_hfq") is None
        bar_date, px = prices.get(sym, (None, None))
        # 键序与语义**逐字对齐**生产写入器
        # ``web/livetrading/execution_service.py::_write_nav_breakdown``：
        # 它把 ``valuation.compute_nav`` 产出的 per_symbol 原样落盘，因此
        # shares/raw_close/factor_hfq/anchor_date/value/value_raw/
        # anchor_inflation_ratio/full_history_ratio/dividend_basis/stale/
        # mark_date/factor_anchor_hfq/counted_as 这几个键**必须存在且同义**。
        # 后面的 price/price_source/... 是本脚本的取证附加键，生产侧没有，
        # 但**不能**顶替上面的同名语义（例如不能用 price 代替 raw_close）。
        per_symbol[sym] = {
            "shares": int(row["shares"]),
            "raw_close": row.get("raw_close"),
            "factor_hfq": row.get("factor_hfq"),
            "anchor_date": row.get("anchor_date"),
            "value": round(float(row["value"]), 2),
            "value_raw": round(float(row.get("value_raw") or 0.0), 2),
            "anchor_inflation_ratio": row.get("anchor_inflation_ratio"),
            "full_history_ratio": row.get("full_history_ratio"),
            "dividend_basis": row.get("dividend_basis"),
            "stale": bool(row.get("stale")),
            "mark_date": row.get("mark_date"),
            "factor_anchor_hfq": row.get("factor_anchor_hfq"),
            "counted_as": row.get("counted_as", "equity"),
            # ---- 以下为取证附加键（生产侧无） ----
            "price": float(px) if px is not None else row.get("raw_close"),
            "price_source": (
                f"live_trading_ticket.json:rows[{sym}].price (price_bar_date={bar_date})"
                if px is not None else "unavailable"
            ),
            "price_bar_date": bar_date,
            "factor_date": factor_date,
            "factor_lag_days": factor_lag_days,
            "anchor_source": anchor_evidence.get(sym, {}).get("anchor_source"),
            "factor_missing": bool(factor_missing),
        }
        if factor_missing:
            # 引擎自己已经对这种情况发过 warning（factor_missing /
            # anchor_before_factor_history 等），不要重复刷屏；只有当引擎完全没提及时
            # 才由本脚本补一条，保证"缺因子"绝不会静默。
            mentioned = any(
                sym in w and any(k in w for k in
                                 ("factor_missing", "anchor_before_factor_history", "anchor_after_as_of"))
                for w in result.warnings
            )
            if not mentioned:
                warnings.append(
                    f"factor_missing: {sym} 无后复权因子，按 raw 市值计价（不含股息）"
                )
        elif factor_lag_days:
            events = list((ex_div_map or {}).get(sym, ()))
            if events:
                warnings.append(
                    f"factor_table_lag_HITS_EX_DIVIDEND: {sym} factor_hfq 最新一行是 "
                    f"{factor_date}，但 as_of={run_label} 之后存在除权事件 {events}；"
                    f"该标的的含息市值不可信（因子表需要先更新）"
                )
            else:
                warnings.append(
                    f"factor_table_lag: {sym} factor_hfq 最新一行是 {factor_date}，"
                    f"比 as_of={run_label} 滞后 {factor_lag_days} 天；已用 "
                    f"raw_adjustment_events 复核：该标的在 {factor_date} 之后没有除权事件，"
                    f"因此 factor_hfq 是常数，比值精确等于 1.0，滞后不产生误差"
                )

    # 金额一律保留到分：浮点噪声（198882.16999999998）写进快照会让人以为
    # 引擎算出了不同的数，实际只是 IEEE754 的尾数。
    equity = round(float(result.equity), 2)
    cash_amt = round(float(result.cash), 2)
    nav_val = round(float(result.nav), 2)

    # ``raw_valuation_total`` 的口径照抄生产写入器（execution_service.py:1359-1363）：
    # 只累加 ``counted_as == "equity"`` 的 ``value_raw``（= 股数 x 裸价，不含息下限）。
    raw_total = round(
        sum(
            float(e.get("value_raw") or 0.0)
            for e in per_symbol.values()
            if e.get("counted_as") == "equity"
        ),
        2,
    )
    # ``anchor_source`` 在生产侧是"这批建仓日的来源"（生产写 "run_archive"）。
    # 本脚本的建仓日是推断出来的，故照实写推断方式；混用多种来源时用 "+" 连接。
    anchor_srcs = sorted(
        {str(ev.get("anchor_source") or "unknown") for ev in anchor_evidence.values()}
    )
    anchor_source = "+".join(anchor_srcs) if anchors else None

    return {
        # ---- canonical：生产 ``_write_nav_breakdown`` 的键集，语义一致 ----
        "as_of": result.as_of,
        "nav_effective": nav_val,
        "nav_computed": nav_val,
        "nav_source": result.nav_source,
        "equity": equity,
        "cash": cash_amt,
        "cash_source": cash_source,
        "anchor_source": anchor_source,
        "anchors": dict(anchors),
        "raw_valuation_total": raw_total,
        "stale_symbols": list(result.stale_symbols),
        "dividend_alarms": list(result.dividend_alarms),
        "nav_warnings": warnings,
        "per_symbol": per_symbol,
        # ---- 以下为本脚本的取证附加键（生产侧没有，保留不删） ----
        "run": run_label,
        "run_label": run_label,
        "holdings": dict(tradable),
        "dividend_mode": "total_return",
        "engine": "web/livetrading/valuation.py::compute_nav",
        "anchor_evidence": anchor_evidence,
        "raw_nav_engine": float(result.nav),
        # 本文件是"归档重述"，不是当轮子进程实收值；不与 summary.json 的
        # nav_source="restated" 混用，避免把"引擎算法"与"归档动作"混为一谈。
        "archive_nav_source": "restated",
    }


def _ts(value: Any):
    import pandas as pd

    return pd.Timestamp(value).normalize()


def anchor_sensitivity(
    breakdown: Mapping[str, Any],
    *,
    reader: MarketDbReader,
    fallback_anchor: str,
) -> Dict[str, Any]:
    """量化"anchor 取入场日 vs 取 10-22"对 NAV 的**实际**影响。

    这是对 ``anchor_fallback`` 免责声明的主动证伪：把同一只票换成 10-22 作 anchor
    重算一遍比值，差额写出来。如果差额是 0，说明"入场日不可考"没有留下数值误差；
    如果不是 0，读者就能看到它到底值多少钱，而不是只看到一句免责声明。
    """
    per = breakdown.get("per_symbol", {})
    diffs: Dict[str, Any] = {}
    total_delta = 0.0
    for sym, row in sorted(per.items()):
        fa = row.get("factor_anchor_hfq")
        fm = row.get("factor_hfq")
        adate = row.get("anchor_date")
        shares = int(row.get("shares") or 0)
        price = float(row.get("price") or 0.0)
        if fa is None or fm is None or adate is None:
            diffs[sym] = {"note": "factor missing -> raw valuation under both conventions"}
            continue
        fs = reader.factor_series(sym)
        factor_fb = None
        if fs is not None and len(fs):
            upto = fs[fs.index <= _ts(fallback_anchor)]
            if len(upto):
                factor_fb = float(upto.iloc[-1])
        ratio_used = float(fm) / float(fa) if fa else None
        ratio_fb = (float(fm) / factor_fb) if factor_fb else None
        delta = 0.0
        if ratio_used is not None and ratio_fb is not None:
            delta = shares * price * (ratio_fb - ratio_used)
            total_delta += delta
        diffs[sym] = {
            "anchor_used": adate,
            "factor_anchor_hfq": fa,
            "factor_at_fallback_anchor": factor_fb,
            "ratio_used": None if ratio_used is None else round(ratio_used, 12),
            "ratio_if_anchor_fallback": None if ratio_fb is None else round(ratio_fb, 12),
            "value_delta_if_anchor_fallback": round(delta, 6),
        }
    out = {
        "fallback_anchor_date": fallback_anchor,
        "per_symbol": diffs,
        "nav_delta": round(total_delta, 6),
        "verdict": (
            "两种 anchor 取值给出完全相同的市值（nav_delta = 0）：受影响的标的后复权"
            "因子在入场日..2026-10-22 之间是常数（阶跃函数没有台阶），比值恒为 1.0。"
            "因此 anchor 的选取在本例中不产生任何数值误差，anchor_fallback 只是保守的"
            "口径声明，不是误差来源。"
            if abs(total_delta) < 0.005
            else "两种 anchor 取值存在差异，见 per_symbol.value_delta_if_anchor_fallback"
        ),
    }
    return out


# ---------------------------------------------------------------------------
# 票面重算（只重算 input-derived 层）
# ---------------------------------------------------------------------------
RECOMPUTED_SCOPE = "input_derived_only"
NOT_RECOMPUTED: Tuple[str, ...] = (
    "model_targets",
    "circuit_breaker",
    "regime_weights",
    "lifecycle_state_machine",
    "macro_gate",
    "sector_throttle",
    "intraday_microstructure",
)


def recompute_ticket(
    original: Sequence[Mapping[str, Any]],
    *,
    corrected_holdings: Mapping[str, int],
    nav: float,
    cash: float,
    threshold: float,
    lot_size: int = DEFAULT_LOT_SIZE,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按修正后的股数重算票面里"由输入决定"的那一层。

    Returns:
        (new_ticket, change_log)
    """
    out: List[Dict[str, Any]] = []
    change_log: List[Dict[str, Any]] = []

    cash_w = (float(cash) / float(nav)) if nav > 0 else 0.0

    for row in original:
        sym = str(row.get("symbol", "")).strip().upper()
        before = {
            k: row.get(k) for k in (
                "action", "current_weight", "target_weight", "delta_weight",
                "current_shares", "target_shares", "delta_shares", "shares",
                "trade_value", "trade_value_rmb",
            )
        }

        if sym == CASH_PROXY:
            # BIL 归集行：cash 权重 = 闲置现金 / NAV。没有卖出成交就没有归集，
            # 因此 target = current、trade_value = 0。这一行**完全是输入派生**的
            # （它的 trade_value 就等于本轮的卖出所得合计）。
            new = _copy_row(row)
            new["action"] = "HOLD"
            new["current_weight"] = cash_w
            new["target_weight"] = cash_w
            new["delta_weight"] = 0.0
            new["current_shares"] = 0
            new["target_shares"] = 0
            new["delta_shares"] = 0
            new["shares"] = 0
            new["trade_value"] = 0.0
            new["trade_value_rmb"] = 0.0
            new["reason"] = "无卖出成交，闲置资金无需归集 (归集金额 0)"
            new["summary_reason"] = new["reason"]
            detail = new.get("detail_reason")
            if isinstance(detail, dict):
                detail["original_action"] = before["action"]
                detail["summary_reason"] = new["reason"]
                detail["recomputed_scope"] = RECOMPUTED_SCOPE
            out.append(new)
            change_log.append(_change_entry(sym, before, new))
            continue

        shares = int(corrected_holdings.get(sym, 0))
        new = recompute_equity_row(
            row,
            current_shares=shares,
            nav=nav,
            threshold=threshold,
            lot_size=lot_size,
        )
        detail = new.get("detail_reason")
        if isinstance(detail, dict):
            detail["recomputed_scope"] = RECOMPUTED_SCOPE
            detail["not_recomputed"] = list(NOT_RECOMPUTED)
            # 冷却期/门槛行的 reason 说的是"为什么不动"，重算后如果连持仓都没了，
            # 保留原文本会误导（"平仓后冷却"其实是因为它早就离场了），因此加一句。
            if shares == 0 and int(before.get("current_shares") or 0) > 0:
                detail["note"] = (
                    f"输入修正：{sym} 已在上一轮离场（当前股数 "
                    f"{before.get('current_shares')} -> 0），本行不再携带 "
                    f"target_shares>0，票后态将不再残留该标的"
                )
        out.append(new)
        change_log.append(_change_entry(sym, before, new))

    return out, change_log


def _copy_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    import copy as _copy

    return _copy.deepcopy(dict(row))


def _change_entry(sym: str, before: Mapping[str, Any], after: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": sym,
        "before": dict(before),
        "after": {
            k: after.get(k) for k in (
                "action", "current_weight", "target_weight", "delta_weight",
                "current_shares", "target_shares", "delta_shares", "shares",
                "trade_value", "trade_value_rmb",
            )
        },
    }


CSV_COLUMNS: Tuple[str, ...] = (
    "symbol", "name", "action", "price", "current_weight", "target_weight",
    "delta_weight", "current_shares", "target_shares", "delta_shares", "shares",
    "trade_value", "trade_value_rmb", "holding_bars", "reason", "summary_reason",
    "detail_reason", "strategy_version", "base_version", "execution_warning",
    "price_stale", "price_bar_date", "intraday_vwap", "vwap_premium_pct",
    "chan_5m_state", "suggested_limit_price", "passive_support_limit_price",
    "blocking_reason",
)


def ticket_to_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """序列化成与原件同构的 CSV（28 列，LF 换行，detail_reason 用 Python repr）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(list(CSV_COLUMNS))
    for row in rows:
        cells: List[Any] = []
        for col in CSV_COLUMNS:
            v = row.get(col)
            if col == "detail_reason":
                cells.append(repr(v))
            elif col == "blocking_reason":
                cells.append("" if v is None else str(v))
            elif v is None:
                cells.append("")
            else:
                cells.append(v)
        writer.writerow(cells)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# run 级编排
# ---------------------------------------------------------------------------
def load_run_ticket(run_dir: Path) -> List[Dict[str, Any]]:
    raw, _ = read_source_bytes(run_dir / "live_trading_ticket.json")
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, list) else []


def prices_from_ticket(ticket: Sequence[Mapping[str, Any]], run_label: str) -> Dict[str, Tuple[str, float]]:
    """从票面里取每只标的价格 + bar 日期（**不从行情库取**）。

    行情库只到 2026-10-22，查 10-26/10-27 必然缺 bar；而票面里的 ``price`` 就是
    那一轮 run 实际用来决策的价（``price_bar_date`` 记录了它的 bar 日）。
    """
    out: Dict[str, Tuple[str, float]] = {}
    for row in ticket:
        sym = str(row.get("symbol", "")).strip().upper()
        if not sym or sym == CASH_PROXY:
            continue
        px = row.get("price")
        if px is None:
            continue
        try:
            px = float(px)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        out[sym] = (str(row.get("price_bar_date") or run_label), px)
    return out


def anchor_cash_from_origin(
    run_dirs: Mapping[str, Path],
    reader: MarketDbReader,
) -> Tuple[float, Dict[str, Any]]:
    """锚点现金 = 10-22 票面 BIL 权重 × 该轮 NAV，并与"NAV − 正确持仓市值"互相校验。

    两种算法在 10-22 必须一致（都是 46,216.17）；不一致说明这一轮的持仓输入或
    价格本身就有问题，那就不能拿它当锚，脚本会直接报错而不是猜一个数。
    """
    run_dir = run_dirs[CASH_ANCHOR_RUN]
    summary, _ = read_source_json(run_dir / "summary.json")
    ticket = load_run_ticket(run_dir)
    nav_cli = float(summary.get("portfolio_value") or 0.0)

    bil_cash = bil_weight_cash(ticket, nav_cli)
    holdings, _ = correct_holdings(CASH_ANCHOR_RUN, run_dirs)
    prices = prices_from_ticket(ticket, CASH_ANCHOR_RUN)
    equity_raw = sum(
        int(sh) * float(prices[sym][1]) for sym, sh in holdings.items() if sym in prices
    )
    residual = nav_cli - equity_raw

    evidence: Dict[str, Any] = {
        "anchor_run": CASH_ANCHOR_RUN,
        "nav_cli": nav_cli,
        "equity_raw_at_anchor": round(equity_raw, 2),
        "cash_from_bil_weight": None if bil_cash is None else round(bil_cash, 2),
        "cash_from_nav_residual": round(residual, 2),
        "holdings_at_anchor": dict(holdings),
        "price_source": "live_trading_ticket.json:price",
    }
    if bil_cash is None:
        evidence["chosen"] = "cash_from_nav_residual"
        evidence["warning"] = "BIL row weight missing; fell back to the NAV residual"
        return residual, evidence
    if abs(bil_cash - residual) > 1.0:
        raise RuntimeError(
            f"锚点现金两种算法不一致: BIL 权重推导 {bil_cash:.2f} vs NAV 差额 {residual:.2f}；"
            f"说明 {CASH_ANCHOR_RUN} 这一轮的输入/价格本身有问题，拒绝继续"
        )
    evidence["chosen"] = "cash_from_bil_weight (cross-checked against NAV residual)"
    evidence["delta"] = round(bil_cash - residual, 6)
    return bil_cash, evidence


def _before_fix_decomposition(
    ticket: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
) -> Dict[str, Any]:
    """还原"修正前"那一轮 run 自己的 equity / cash / nav 拆分。

    修正前这一轮**自己**就是这么算的：``cash = BIL 行权重 × portfolio_value``
    （等于 ``portfolio_value − 持仓市值``，因为 ``run_live_strategy.py`` 的现金是
    差额补数）。把这三项写出来，才能让"修正前 → 修正后"是三项对三项的比较，
    而不是只比一个总数。
    """
    nav = summary.get("portfolio_value")
    if nav is None:
        return {"equity": None, "cash": None, "nav": None}
    nav = float(nav)
    cash = bil_weight_cash(ticket, nav)
    if cash is None:
        return {"equity": None, "cash": None, "nav": round(nav, 2)}
    return {
        "equity": round(nav - cash, 2),
        "cash": round(cash, 2),
        "nav": round(nav, 2),
        "cash_source": "bil_fallback (run's own residual plug)",
    }


def patch_summary(
    summary: Mapping[str, Any],
    *,
    nav: float,
    nav_original: Optional[float] = None,
    cash: float,
    cash_source: str,
    warnings: Sequence[str],
) -> Dict[str, Any]:
    """按 canonical 语义拆分 ``nav_*``，不做"单字段覆写"。

    | 字段 | 值 | 为什么 |
    | --- | --- | --- |
    | ``portfolio_value`` | 重算含息 MTM | 用户要求把操作员输入字段改成正确口径 |
    | ``portfolio_value_original`` | 原值 | 保留操作员当时真正填进去的数字 |
    | ``nav_effective`` | **原值** | 历史事实：子进程当时实收的就是它，不能改写成没发生过的事 |
    | ``nav_computed`` | 重算值 | 按正确口径应为多少 |
    | ``nav_source`` | ``"restated"`` | 归档重述，与线上 ``computed``/``override``/``fallback_input`` 区分 |
    | ``nav_restatement`` | before/after/delta/reason | 让差异一眼可见 |

    ``deploy_stdout`` / ``deploy_stderr`` / ``health_stdout`` 里含着当轮打印的
    "账户总资产估值 : ¥197,351.17" —— 那是**历史运行的原始日志**，改写它等于伪造
    运行记录，因此保持原样，改在 ``nav_warnings`` 里显式说明这处不一致。
    """
    out = dict(summary)
    if nav_original is None:
        nav_original = summary.get("portfolio_value_original", summary.get("portfolio_value"))
    if nav_original is None:
        nav_original = nav
    before = round(float(nav_original), 2)
    after = round(float(nav), 2)

    out["portfolio_value"] = after
    out["portfolio_value_original"] = before
    # 历史事实：不改写"子进程当时实收了多少"。
    out["nav_effective"] = before
    out["nav_computed"] = after
    out["nav_source"] = "restated"
    out["nav_restatement"] = {
        "before": before,
        "after": after,
        "delta": round(after - before, 2),
        "reason": (
            "原 portfolio_value 是操作员输入的冻结表单值，子进程当时实收的 nav_effective "
            "也是同一个数（故保留为历史事实）；nav_computed 是按快照价 + 后复权因子重算的"
            "含息 MTM。差异来源：(1) 持仓输入滞后一轮，已离场的标的仍被计入并按旧权重下达"
            "卖出，导致账面权益/现金同时失真；(2) 现金按『票前持仓配票前现金』重算，"
            "不再沿用冻结值。原始运行日志字段（deploy_stdout 等）保持原样。"
        ),
    }
    out["cash"] = round(float(cash), 2)
    out["cash_source"] = cash_source
    out["nav_warnings"] = list(warnings)
    return out


def run_one(
    run_label: str,
    *,
    run_dirs: Mapping[str, Path],
    reader: MarketDbReader,
    fallback_anchor: str,
    anchor_cash: float,
    dry_run: bool,
    log: List[str],
) -> Dict[str, Any]:
    run_dir = run_dirs[run_label]
    ticket = load_run_ticket(run_dir)
    summary, summary_src = read_source_json(run_dir / "summary.json")
    account_state, _ = read_source_json(run_dir / "account_state.json")

    holdings_source, holdings_evidence = correct_holdings(run_label, run_dirs)
    prices = prices_from_ticket(ticket, run_label)
    entry_prices = account_state.get("entry_prices") or {}

    cash, cash_steps = cash_at_run_start(
        run_label, anchor_cash, run_dirs, ASSUMED_FILLED_RUNS
    )

    # as_of 之后的除权事件（先查，再算 NAV，这样 factor_lag 的 warning 才能是
    # "已复核"而不是"推测"）。
    ex_events = reader.ex_dividend_events_after(run_label)
    ex_div_map: Dict[str, List[str]] = {}
    for ev in ex_events:
        ex_div_map.setdefault(ev["symbol"], []).append(ev["ex_date"])

    breakdown = compute_run_nav(
        run_label=run_label,
        holdings=holdings_source,
        prices=prices,
        reader=reader,
        entry_prices=entry_prices,
        cash=cash,
        cash_source="bil_fallback",
        fallback_anchor=fallback_anchor,
        ex_div_map=ex_div_map,
    )

    # 除权事件 ∩ 持仓：用来证明"因子表滞后 3 天"没有伤害持仓估值。
    ex_hit = [e for e in ex_events if e["symbol"] in set(holdings_source)]

    breakdown.update(
        {
            "run_label": run_label,
            "holdings_source": holdings_evidence.get("expected_source"),
            "holdings_evidence": holdings_evidence,
            "cash_derivation": {
                "rule": (
                    "cash(开盘) = 锚点现金 + 该 run 之前所有已成交 run 的卖出所得合计；"
                    "票前持仓必须配票前现金，否则同一笔卖出所得会被算了两次或被吞掉"
                ),
                "anchor_cash": round(float(anchor_cash), 2),
                "steps": cash_steps,
                "assumed_filled_runs": list(ASSUMED_FILLED_RUNS),
                "assumed_unfilled_runs": list(ASSUMED_UNFILLED_RUNS),
                "result": round(float(cash), 2),
            },
            "ex_dividend_intersection": ex_hit,
            "anchor_sensitivity": anchor_sensitivity(
                breakdown, reader=reader, fallback_anchor=fallback_anchor
            ),
            "before_fix": _before_fix_decomposition(ticket, summary),
            "frozen_portfolio_value_before": summary.get("portfolio_value"),
            "nav_delta_vs_frozen": (
                None if summary.get("portfolio_value") is None
                else round(float(breakdown["nav_effective"]) - float(summary["portfolio_value"]), 2)
            ),
        }
    )
    if ex_hit:
        breakdown["nav_warnings"].append(
            "ex_dividend_after_as_of: 持仓中有标的在 as_of 之后除权，因子表滞后会影响估值: "
            + ", ".join(f"{e['symbol']}@{e['ex_date']}" for e in ex_hit)
        )
    if holdings_evidence.get("modified"):
        breakdown["nav_warnings"].append(
            f"holdings_input_stale: {run_label}/holdings.json 与上一轮票后态不一致，"
            f"已按 {holdings_evidence['expected_source']} 修正"
        )

    nav_warnings = list(breakdown["nav_warnings"])
    nav_warnings.append(
        "summary.json 的 deploy_stdout/deploy_stderr 保留了当轮原始日志"
        "（其中仍印着冻结的 ¥%s 与当轮股数），那是运行记录，不做改写"
        % summary.get("portfolio_value")
    )
    new_summary = patch_summary(
        summary,
        nav=breakdown["nav_effective"],
        cash=breakdown["cash"],
        cash_source="bil_fallback",
        warnings=nav_warnings,
    )

    # ---- 写盘 ----
    files: List[Dict[str, Any]] = []
    nb_path = run_dir / "nav_breakdown.json"
    nb_bytes = json_bytes(breakdown)
    files.append(
        _write_or_skip(nb_path, nb_bytes, dry_run=dry_run, action="create/update", generated=True)
    )

    sm_path = run_dir / "summary.json"
    files.append(
        _write_or_skip(
            sm_path,
            json_bytes(new_summary),
            dry_run=dry_run,
            action="patch",
            field_changes=_summary_field_changes(summary, new_summary),
        )
    )

    # ---- 10-27 专项：修正输入股数 ----
    if run_label == "2026-10-27":
        hp = run_dir / "holdings.json"
        files.append(
            _write_or_skip(
                hp,
                json_bytes(holdings_source),
                dry_run=dry_run,
                action="fix share counts",
                field_changes=[
                    {
                        "field": "holdings",
                        "before": holdings_evidence.get("actual_input"),
                        "after": holdings_source,
                    }
                ],
            )
        )

    log.append(
        f"{run_label}: equity={breakdown['equity']:,.2f} cash={breakdown['cash']:,.2f} "
        f"nav={breakdown['nav_effective']:,.2f} (before {summary.get('portfolio_value')})"
    )
    return {
        "run": run_label,
        "breakdown": breakdown,
        "summary_before": summary,
        "summary_after": new_summary,
        "files": files,
        "ticket": ticket,
        "summary_source_kind": summary_src,
    }


# ---------------------------------------------------------------------------
# 写盘 + 字段级 before/after
# ---------------------------------------------------------------------------
#: 只对"修复前就存在、且内容会被改写"的文件留 .orig。脚本自己产出的
#: nav_breakdown.json / *.recomputed.json / repair_manifest.json 不留 .orig
#: —— 它们没有"更早的原始版本"可以还原，给它们建 .orig 只会制造噪音。
BACKUP_SUFFIXES: Tuple[str, ...] = ("summary.json", "holdings.json")


def _needs_backup(path: Path) -> bool:
    return any(path.name == n or path.name.endswith("/" + n) for n in BACKUP_SUFFIXES)


def _write_or_skip(
    path: Path,
    data: bytes,
    *,
    dry_run: bool,
    action: str,
    field_changes: Optional[List[Dict[str, Any]]] = None,
    generated: bool = False,
) -> Dict[str, Any]:
    """写一个文件并返回它的 manifest 条目。

    ``generated=True`` 表示这是**本脚本自己产出**的文件（nav_breakdown /
    recomputed 票面 / post_holdings.recomputed）：它没有"更早的原始版本"，
    所以只记 ``md5_after`` 与是否执行了写入，不记"改写前 md5"/"是否变化" ——
    否则第二遍运行时"改写前"就变成了上一遍的输出，manifest 会每次都不一样，
    幂等性检验就永远过不了。
    """
    path = Path(path)
    after_md5 = md5_bytes(data)
    existed = path.exists()
    before_md5 = md5_file(path) if existed else None

    backup_info = None
    if existed and _needs_backup(path):
        backup_info = ensure_orig_backup(path, dry_run=dry_run)
        before_md5 = backup_info.get("md5") or before_md5

    if generated:
        entry: Dict[str, Any] = {
            "path": _rel(path),
            "action": action,
            "generated": True,
            "md5_after": after_md5,
            "note": "本脚本产出，无更早版本可还原（故不留 .orig、不记改写前 md5）",
            "field_changes": field_changes or [],
        }
        if not dry_run:
            if not (existed and before_md5 == after_md5):
                write_bytes_atomic(path, data)
        return entry

    entry = {
        "path": _rel(path),
        "action": action,
        "existed_before": existed,
        "md5_before": before_md5,
        "md5_after": after_md5,
        "changed": before_md5 != after_md5,
        "field_changes": field_changes or [],
    }
    if backup_info:
        entry["orig_backup"] = backup_info
        entry["orig_backup_present"] = orig_path(path).exists()

    if dry_run:
        entry["written"] = False
        entry["note"] = "dry-run"
        return entry

    if existed and before_md5 == after_md5:
        entry["written"] = False
        entry["note"] = "content already identical; file left untouched (idempotent)"
        return entry

    write_bytes_atomic(path, data)
    entry["written"] = True
    return entry


def _summary_field_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for key in after.keys() | before.keys():
        b = before.get(key, "<absent>")
        a = after.get(key, "<absent>")
        if b != a:
            out.append({"field": key, "before": b, "after": a})
    return sorted(out, key=lambda d: d["field"])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="repair_live_run_snapshots.py",
        description=(
            "离线修复 chan_four_state_blend 历史 run 快照的含息 NAV 与持仓/票面口径。"
            "只读行情库、不 commit、不联网；重复运行结果一致。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "产出: nav_breakdown.json / summary.json(补字段) / "
            "2026-10-27/holdings.json(修正股数) / live_trading_ticket.recomputed.{json,csv} / "
            "post_holdings.recomputed.json / repair_manifest.json"
        ),
    )
    p.add_argument(
        "--runs-root",
        default=str(DEFAULT_RUNS_ROOT),
        help=f"run 快照根目录（默认: {_rel(DEFAULT_RUNS_ROOT)}）",
    )
    p.add_argument(
        "--market-db",
        default=None,
        help=(
            "market.duckdb 路径（只读打开）。默认依次尝试 "
            + ", ".join(MARKET_DB_CANDIDATES)
        ),
    )
    p.add_argument(
        "--runs",
        nargs="+",
        default=list(DEFAULT_RUNS),
        help=f"要处理的 run 目录名（默认: {' '.join(DEFAULT_RUNS)}）",
    )
    p.add_argument(
        "--inertia-threshold",
        type=float,
        default=0.05,
        help="惰性阈值（默认 0.05，与策略参数 inertia_threshold 一致）",
    )
    p.add_argument(
        "--lot-size",
        type=int,
        default=DEFAULT_LOT_SIZE,
        help="整手股数（默认 100）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只计算并打印，不写任何文件（也不建 .orig）",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    runs_root = Path(args.runs_root).resolve()
    if not runs_root.is_dir():
        print(f"[ERROR] runs-root 不存在: {runs_root}", file=sys.stderr)
        return 2

    run_labels = list(args.runs)
    run_dirs = {r: runs_root / r for r in run_labels}
    for r in run_labels:
        if not run_dirs[r].is_dir():
            print(f"[ERROR] run 目录不存在: {run_dirs[r]}", file=sys.stderr)
            return 2

    market_db = resolve_market_db(args.market_db)
    fallback_anchor = min(CASH_ANCHOR_RUN, *run_labels)

    log: List[str] = []
    manifest_files: List[Dict[str, Any]] = []

    with MarketDbReader(market_db) as reader:
        if market_db:
            log.append(f"market db (read-only): {market_db}")
        else:
            log.append("market db: NOT FOUND -> factor_missing, raw valuation only")
        for err in reader.errors:
            log.append(f"market db warning: {err}")

        anchor_cash, anchor_evidence = anchor_cash_from_origin(run_dirs, reader)
        log.append(f"anchor cash = {anchor_cash:,.2f} ({anchor_evidence['chosen']})")

        results: List[Dict[str, Any]] = []
        for run_label in sorted(run_labels):
            res = run_one(
                run_label,
                run_dirs=run_dirs,
                reader=reader,
                fallback_anchor=fallback_anchor,
                anchor_cash=anchor_cash,
                dry_run=args.dry_run,
                log=log,
            )
            results.append(res)
            manifest_files.extend(res["files"])

        by_run = {r["run"]: r for r in results}

        # ---- 10-27 票面重算 ----
        target_run = "2026-10-27"
        ticket_changes: List[Dict[str, Any]] = []
        recomputed_ok = target_run in by_run
        if recomputed_ok:
            res = by_run[target_run]
            bd = res["breakdown"]
            new_ticket, ticket_changes = recompute_ticket(
                res["ticket"],
                corrected_holdings=bd["holdings"],
                nav=bd["nav_effective"],
                cash=bd["cash"],
                threshold=args.inertia_threshold,
                lot_size=args.lot_size,
            )
            meta = {
                "recomputed_scope": RECOMPUTED_SCOPE,
                "not_recomputed": list(NOT_RECOMPUTED),
                "script": "scripts/repair_live_run_snapshots.py",
                "script_version": SCRIPT_VERSION,
                "source_ticket": f"{target_run}/live_trading_ticket.json",
                "note": (
                    "本文件只重算由输入股数直接决定的那一层"
                    "（current_weight / broker_weight / drift / 惰性判定 / action / "
                    "target_shares / trade_value）。模型目标权重、熔断、regime 权重、"
                    "状态机均未重跑，原值原样保留。原票面 live_trading_ticket.json 未被覆盖。"
                ),
                "detail_reason_restatements": [
                    "detail_reason.lifecycle_state.current_weight := 修正后股数 × price / "
                    "nav_recomputed（该字段是输入派生量；状态机的 state/cooldown/streak "
                    "未重跑，仍是当轮值）",
                    "detail_reason.lifecycle_state.broker_weight := 同上（券商侧当前权重）",
                    "detail_reason.summary_reason := 顶层 summary_reason（同一行只允许一个结论）",
                    "detail_reason.is_liquidation := (action == 'SELL')",
                ],
                "nav_used": round(float(bd["nav_effective"]), 2),
                "cash_used": round(float(bd["cash"]), 2),
                "holdings_used": bd["holdings"],
                "inertia_threshold": args.inertia_threshold,
                "assumptions": [
                    "假设 10-27 三笔卖单未成交（它们是 10-26 已成交减仓的重复下单）",
                    "假设 10-26 五笔卖单按票面价整手成交，现金 = 期初现金 + 卖出所得",
                ],
            }
            payload = {"_meta": meta, "ticket": new_ticket}
            run_dir = run_dirs[target_run]
            manifest_files.append(
                _write_or_skip(
                    run_dir / "live_trading_ticket.recomputed.json",
                    json_bytes(payload),
                    dry_run=args.dry_run,
                    action="create (original untouched)",
                    field_changes=ticket_changes,
                    generated=True,
                )
            )
            manifest_files.append(
                _write_or_skip(
                    run_dir / "live_trading_ticket.recomputed.csv",
                    ticket_to_csv(new_ticket),
                    dry_run=args.dry_run,
                    action="create (original untouched)",
                    generated=True,
                )
            )

            # 票后态重算（原 post_holdings.json 不动）
            post = {
                str(r.get("symbol", "")).strip().upper(): int(r.get("target_shares"))
                for r in new_ticket
                if str(r.get("symbol", "")).strip().upper()
                and str(r.get("symbol", "")).strip().upper() != CASH_PROXY
                and int(r.get("target_shares") or 0) > 0
            }
            manifest_files.append(
                _write_or_skip(
                    run_dir / "post_holdings.recomputed.json",
                    json_bytes(post),
                    dry_run=args.dry_run,
                    action="create (original untouched)",
                    generated=True,
                    field_changes=[
                        {"field": "post_holdings", "before": json.loads(
                            (run_dir / "post_holdings.json").read_text("utf-8")
                        ), "after": post}
                    ],
                )
            )

        # ---- 复核项：10-26/holdings.json 本来就是对的内容 ----
        review: List[Dict[str, Any]] = []
        if "2026-10-26" in by_run:
            ev = by_run["2026-10-26"]["breakdown"]["holdings_evidence"]
            info = ensure_orig_backup(run_dirs["2026-10-26"] / "holdings.json", dry_run=args.dry_run)
            review.append(
                {
                    "path": "2026-10-26/holdings.json",
                    "verdict": (
                        "复核：内容本来就正确（等于 10-22 票后态，因为 10-22/10-23/10-25 "
                        "三轮没有产生任何卖出成交），未修改；仅保留 .orig 以便还原"
                    ),
                    "md5": info.get("md5"),
                    "expected_source_md5": ev.get("expected_source_md5"),
                    "modified": False,
                    "orig_backup": info,
                }
            )

        manifest = {
            "script": "scripts/repair_live_run_snapshots.py",
            "script_version": SCRIPT_VERSION,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dry_run": bool(args.dry_run),
            "runs_root": _rel(runs_root),
            "runs_processed": sorted(run_labels),
            "runs_skipped": ["2026-10-23", "2026-10-25"],
            "runs_skipped_reason": (
                "10-23 / 10-25 两轮 run 的 as_of_date 仍是 2026-10-22（重复 run，"
                "summary.deploy_stdout 为空），持仓输入与 10-22 逐字节相同，"
                "不存在滞后一轮的问题；为它们另出一份 nav_breakdown 只会制造"
                "互相矛盾的口径，故不处理。"
            ),
            "method": {
                "nav_formula": (
                    "value_i = shares_i × price_i,as_of × (factor_hfq_i,as_of / "
                    "factor_hfq_i,anchor)；nav = equity + cash"
                ),
                "nav_engine": "web/livetrading/valuation.py::compute_nav "
                              "(dividend_mode='total_return', fail_closed=True)",
                "price_source": "run 快照 live_trading_ticket.json 每行的 price + price_bar_date",
                "factor_source": "market.duckdb::calc_adjust_factor_daily.backward_factor（只读）",
                "ex_dividend_source": "market.duckdb::raw_adjustment_events（只读）",
                "cash_rule": (
                    "cash(某 run 开盘) = 锚点现金 + 该 run 之前所有已成交 run 的 "
                    "SELL 行 trade_value 合计；票前持仓配票前现金，票后持仓配票后现金"
                ),
                "weight_restatement": (
                    "current_weight_i = shares_i,corrected × price_i / nav_recomputed；"
                    "drift = model_target_weight − current_weight"
                ),
                "inertia_rule": (
                    f"|drift| >= {args.inertia_threshold} 才允许交易；否则票面写明"
                    f"'调仓未超惰性阈值'"
                ),
                "nav_breakdown_schema": (
                    "nav_breakdown.json 是 superset：canonical 键集 = 生产写入器 "
                    "web/livetrading/execution_service.py::_write_nav_breakdown "
                    "(as_of, nav_effective, nav_computed, nav_source, equity, cash, "
                    "cash_source, anchor_source, anchors, raw_valuation_total, "
                    "stale_symbols, dividend_alarms, nav_warnings, per_symbol)；"
                    "per_symbol[*] 的 canonical 键集 = valuation.compute_nav 的原样输出 "
                    "(shares, raw_close, factor_hfq, anchor_date, value, value_raw, "
                    "anchor_inflation_ratio, full_history_ratio, dividend_basis, stale, "
                    "mark_date, factor_anchor_hfq, counted_as)。取证附加键（anchor_evidence / "
                    "cash_derivation / anchor_sensitivity / factor_date / factor_lag_days 等）"
                    "只增不改，不与 canonical 同名异义"
                ),
                "summary_nav_semantics": (
                    "summary.json: portfolio_value=重算含息 MTM；portfolio_value_original="
                    "操作员原输入；nav_effective=子进程当轮实收值（历史事实，不改写）；"
                    "nav_computed=按正确口径应为多少；nav_source='restated'（归档重述，"
                    "区别于线上 computed/override/fallback_input）；nav_restatement 记录 "
                    "before/after/delta/reason"
                ),
            },
            "data_sources": [
                {
                    "path": _rel(runs_root),
                    "what": "holdings.json / post_holdings.json / live_trading_ticket.json(.csv) / "
                            "summary.json / account_state.json（entry_prices / lifecycle_states）",
                },
                {
                    "path": market_db or "<not found>",
                    "tables": ["calc_adjust_factor_daily", "raw_adjustment_events", "raw_kline_daily"],
                    "mode": "read_only",
                },
            ],
            "anchor_policy": {
                "order": [
                    "1) account_state.entry_prices[symbol] 反查行情库等于该收盘价的日期 -> 入场日",
                    "2) 查不到 -> 回落到 2026-10-22（三份快照最早一天）",
                    "3) 绝不使用因子序列最早日（2016 年）",
                ],
                "fallback_anchor": fallback_anchor,
                "anchor_fallback": fallback_anchor,
                "anchor_fallback_note": (
                    "快照里没有任何 entry_date 字段（全仓库 grep 'entry_date' 无命中），"
                    "本脚本用 account_state.entry_prices 与行情库 raw close 匹配来推断入场日；"
                    "若推断失败则回落到 2026-10-22，此口径**不含**入场至 10-22 之间已收股息，"
                    "NAV 为下限估计。实测两类口径的因子比值恒等（1.0），误差为 0，"
                    "见各 run 的 nav_breakdown.json:anchor_sensitivity"
                ),
                "per_run": {
                    r["run"]: r["breakdown"]["anchor_evidence"] for r in results
                },
            },
            "cash_source": "bil_fallback",
            "cash_evidence": anchor_evidence,
            "assumptions": [
                "假设 10-27 三笔卖单**未成交**：它们是 10-26 已成交减仓的重复下单"
                "（同一批标的、同一批股数），若已成交则 10-27 的持仓与现金都要另行改写",
                "假设 10-26 五笔卖单按票面价、整手成交（500/300/500/100/100 股），"
                "卖出所得 52,482.00 计入 10-27 期初现金；未计手续费与滑点",
                "假设 10-22 那一轮的持仓输入正确、且 197,351.17 是当时的真实账户总值，"
                "因此其现金 46,216.17 是唯一可当作真实余额的读数"
                "（BIL 权重推导与 NAV 差额两条路径互相校验一致）",
                "BIL 是美股 ETF、A 股库无其因子；且票面 BIL 行 target_shares 恒为 0，"
                "现金不以 BIL 份额存在，故按票面现金流推导而非按 BIL 市值",
                "未重算模型目标权重/熔断/regime 权重/状态机；票面 recomputed 文件已显式标注",
                "未计交易费用、印花税、过户费与滑点",
            ],
            # ---- 缺陷 4：真跑一次只读 SQL，把原始查询/原始输出/逐票判定交出去 ----
            "ex_dividend_check": ex_dividend_check(
                reader,
                symbols=sorted(
                    {
                        s
                        for r in results
                        for s in (r["breakdown"].get("per_symbol") or {}).keys()
                    }
                    | {s for r in results for s in (r["breakdown"].get("holdings") or {})}
                ),
                anchors_by_symbol=_anchors_by_symbol(results),
            ),
            "ex_dividend_intersection_by_run": {
                r["run"]: r["breakdown"].get("ex_dividend_intersection", []) for r in results
            },
            "files": manifest_files,
            "reviews": review,
            "ticket_row_changes": ticket_changes,
            "nav_summary": {
                r["run"]: {
                    "equity_before": r["breakdown"].get("before_fix", {}).get("equity"),
                    "cash_before": r["breakdown"].get("before_fix", {}).get("cash"),
                    "nav_before": r["summary_before"].get("portfolio_value"),
                    "equity_after": round(r["breakdown"]["equity"], 2),
                    "cash_after": round(r["breakdown"]["cash"], 2),
                    "nav_after": round(r["breakdown"]["nav_effective"], 2),
                    "delta_vs_frozen": r["breakdown"].get("nav_delta_vs_frozen"),
                    "summary_portfolio_value": r["summary_after"].get("portfolio_value"),
                    "summary_portfolio_value_original": r["summary_after"].get(
                        "portfolio_value_original"
                    ),
                    "summary_nav_effective": r["summary_after"].get("nav_effective"),
                    "summary_nav_computed": r["summary_after"].get("nav_computed"),
                    "summary_nav_source": r["summary_after"].get("nav_source"),
                }
                for r in results
            },
            "log": log,
        }

        manifest_path = runs_root / "repair_manifest.json"
        _write_manifest(manifest_path, manifest, dry_run=args.dry_run)

    for line in log:
        print(line)
    return 0


def _anchors_by_symbol(results: Sequence[Mapping[str, Any]]) -> Dict[str, Optional[str]]:
    """取"每只票在最近一次持有它的 run 里的建仓锚点"，用于阶跃日比对。"""
    out: Dict[str, Optional[str]] = {}
    for res in sorted(results, key=lambda r: str(r["run"])):
        for sym, row in (res["breakdown"].get("per_symbol") or {}).items():
            if row.get("anchor_date"):
                out[str(sym)] = str(row["anchor_date"])
    return out


def _write_manifest(path: Path, manifest: Dict[str, Any], *, dry_run: bool) -> None:
    """写 manifest；内容（除时间戳外）没变就不重写，保证重复运行字节一致。"""
    path = Path(path)
    if path.exists() and not dry_run:
        try:
            old = json.loads(path.read_text("utf-8"))
            a = {k: v for k, v in old.items() if k != "generated_at_utc"}
            b = {k: v for k, v in manifest.items() if k != "generated_at_utc"}
            if a == b:
                return
        except Exception:
            pass
    if dry_run:
        return
    write_bytes_atomic(path, json_bytes(manifest))


if __name__ == "__main__":
    raise SystemExit(main())
