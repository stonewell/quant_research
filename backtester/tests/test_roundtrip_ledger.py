"""`L2.52` 阶段一：往返台账配对函数的离线单测（合成数据，无网络、无真实行情、无 DuckDB）。

只覆盖**阶段一**可证部分（规格「新增 `backtester/tests/test_roundtrip_ledger.py`」列的
①配对正确 ②`gap return` 数值断言 ③正 gap 占比/加权 gap/往返频率的分桶断言）。
④「阶段二落地后 `test_blend_strategy_versioning.py` 的 `version` 断言同步」**不在本文件**：
阶段二受用户裁决门控，本轮**零代码改动**，故没有可同步的版本断言。

被测算的实现是 `backtests_research/20261025_125300/roundtrip_ledger/roundtrip_ledger.py`
里的纯 pandas 函数（`pair_round_trips` / `summarise` / `grade`）。该目录名以数字开头、
且不是 Python 包，故按**文件路径**加载，不走 `import`——与仓库里
`backtester/tests/test_run_backtest.py` 的 `sys.path` 注入约定分开处理，避免给
`backtests_research/` 引入包语义。
"""

import importlib.util
import os
import sys

import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MODULE_PATH = os.path.join(
    _PROJECT_ROOT, "backtests_research", "20261025_125300", "roundtrip_ledger", "roundtrip_ledger.py"
)


def _load_ledger():
    if not os.path.exists(_MODULE_PATH):
        pytest.skip(f"ledger module missing: {_MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("l252_roundtrip_ledger", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("l252_roundtrip_ledger", mod)
    spec.loader.exec_module(mod)
    return mod


RL = _load_ledger()


def _row(fold, rid, date, symbol, price, prior_w, target_w, value=1000.0, cost=10.0):
    return {
        "run": "synthetic_run",
        "strategy": "synthetic_strategy",
        "fold": fold,
        "rebalance_id": rid,
        "date": pd.Timestamp(date),
        "symbol": symbol,
        "action": "BUY" if target_w > prior_w else "SELL",
        "price": float(price),
        "prior_weight": float(prior_w),
        "target_weight": float(target_w),
        "weight_change": float(target_w - prior_w),
        "trade_value": float(value),
        "total_cost": float(cost),
    }


def _frame(rows):
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# ① 配对正确性
# ---------------------------------------------------------------------------
def test_pairs_exit_with_next_reentry_in_same_fold():
    df = _frame([
        _row(1, 1, "2024-01-02", "AAA", 10.0, 0.0, 0.10),   # 建仓（不入账）
        _row(1, 2, "2024-01-10", "AAA", 9.0, 0.10, 0.00),   # 出场 @9.0
        _row(1, 3, "2024-01-20", "AAA", 9.9, 0.00, 0.08),   # 再入 @9.9
    ])
    rt = RL.pair_round_trips(df)
    assert len(rt) == 1
    r = rt.iloc[0]
    assert r["status"] == "paired"
    assert r["exit_date"] == pd.Timestamp("2024-01-10")
    assert r["entry_date"] == pd.Timestamp("2024-01-20")
    # 建仓价被记住，用来标注这笔出场是赚是亏
    assert r["hold_entry_price"] == pytest.approx(10.0)
    assert r["exit_realised_ret"] == pytest.approx(9.0 / 10.0 - 1.0)


def test_exit_never_reentered_is_flagged_not_dropped():
    df = _frame([
        _row(1, 1, "2024-01-02", "AAA", 10.0, 0.0, 0.10),
        _row(1, 2, "2024-01-10", "AAA", 9.0, 0.10, 0.00),
    ])
    rt = RL.pair_round_trips(df)
    assert len(rt) == 1
    assert rt.iloc[0]["status"] == "no_reentry"
    assert pd.isna(rt.iloc[0]["entry_date"])
    # 分母必须保留：再入率 = 0，而不是 1
    s = RL.summarise(rt, n_exits=1)
    assert s["n_pairs"] == 0
    assert s["reentry_rate"] == 0.0


def test_pairing_is_per_fold_and_per_symbol():
    """同一 symbol 在折 A 清零后，折 B 的建仓**不得**被当成折 A 的再入（`§16.15.15`
    明确「0/138 跨折」是匹配器键定义的产物）。"""
    df = _frame([
        _row(1, 1, "2024-01-02", "AAA", 10.0, 0.0, 0.10),
        _row(1, 2, "2024-01-10", "AAA", 9.0, 0.10, 0.00),
        _row(2, 1, "2024-02-01", "AAA", 9.5, 0.0, 0.10),   # 另一折的建仓
        _row(2, 2, "2024-02-10", "BBB", 20.0, 0.0, 0.05),
    ])
    rt = RL.pair_round_trips(df)
    assert (rt["status"] == "paired").sum() == 0
    assert set(rt["status"]) == {"no_reentry"}


def test_reentry_after_intermediate_add_is_not_paired():
    """出场后若下一条记录的 `prior_weight > 0`（说明其实没清零、CSV 契约违例），
    不得把那条硬配成往返；该出场保持 `no_reentry` 并留在分母里。"""
    df = _frame([
        _row(1, 1, "2024-01-02", "AAA", 10.0, 0.0, 0.10),
        _row(1, 2, "2024-01-10", "AAA", 9.0, 0.10, 0.00),
        _row(1, 3, "2024-01-15", "AAA", 9.2, 0.05, 0.08),  # 违反契约：prior>0 却有成交
    ])
    rt = RL.pair_round_trips(df)
    assert len(rt) == 1
    assert rt.iloc[0]["status"] == "no_reentry"


# ---------------------------------------------------------------------------
# ② gap return 数值断言
# ---------------------------------------------------------------------------
def test_gap_return_is_entry_over_exit_minus_one():
    df = _frame([
        _row(1, 1, "2024-01-02", "AAA", 10.0, 0.0, 0.10),
        _row(1, 2, "2024-01-10", "AAA", 8.0, 0.10, 0.00),    # 出场 @8.0
        _row(1, 3, "2024-01-20", "AAA", 12.0, 0.00, 0.06),   # 再入 @12.0
    ])
    rt = RL.pair_round_trips(df)
    r = rt.iloc[0]
    assert r["gap_return"] == pytest.approx(12.0 / 8.0 - 1.0)   # 恰好 +50%
    assert bool(r["gap_positive"]) is True
    assert r["cal_days"] == 10                                   # 日历日差（近似口径）
    assert r["roundtrip_cost"] == pytest.approx(20.0)


def test_gap_return_negative_when_rebuy_cheaper():
    df = _frame([
        _row(1, 1, "2024-01-02", "AAA", 10.0, 0.0, 0.10),
        _row(1, 2, "2024-01-10", "AAA", 10.0, 0.10, 0.00),
        _row(1, 3, "2024-01-20", "AAA", 9.0, 0.00, 0.06),
    ])
    rt = RL.pair_round_trips(df)
    assert rt.iloc[0]["gap_return"] == pytest.approx(-0.10)
    assert bool(rt.iloc[0]["gap_positive"]) is False


# ---------------------------------------------------------------------------
# ③ 正 gap 占比 / 加权 gap / 频率的分桶断言
# ---------------------------------------------------------------------------
def test_summary_bins_and_weighting():
    # 三笔往返：+10%（权重大）、+20%（权重小）、-10%（权重中等）
    df = _frame([
        _row(1, 1, "2024-01-02", "AAA", 100.0, 0.0, 0.10, value=1.0),
        _row(1, 2, "2024-01-05", "AAA", 100.0, 0.10, 0.00, value=1000.0),  # 出场市值 1000
        _row(1, 3, "2024-01-10", "AAA", 110.0, 0.00, 0.10, value=1000.0),  # +10%
        _row(1, 4, "2024-01-20", "AAA", 110.0, 0.10, 0.00, value=100.0),   # 出场市值 100
        _row(1, 5, "2024-01-25", "AAA", 132.0, 0.00, 0.10, value=100.0),   # +20%
        _row(1, 6, "2024-02-05", "AAA", 132.0, 0.10, 0.00, value=500.0),   # 出场市值 500
        _row(1, 7, "2024-02-10", "AAA", 118.8, 0.00, 0.10, value=500.0),   # -10%
    ])
    rt = RL.pair_round_trips(df)
    s = RL.summarise(rt, n_exits=3)
    assert s["n_pairs"] == 3
    assert s["reentry_rate"] == pytest.approx(1.0)
    assert s["pos_share"] == pytest.approx(2 / 3)
    assert s["neg_share"] == pytest.approx(1 / 3)
    # 简单均值 vs 按 |exit_trade_value| 加权：权重不同的两列**必须**不同
    assert s["gap_mean"] == pytest.approx((0.10 + 0.20 - 0.10) / 3)
    expected_w = (0.10 * 1000 + 0.20 * 100 - 0.10 * 500) / 1600
    assert s["gap_weighted_mean"] == pytest.approx(expected_w)
    assert s["gap_weighted_mean"] != pytest.approx(s["gap_mean"])


def test_summary_bars_buckets():
    rt = pd.DataFrame({
        "status": ["paired"] * 4,
        "gap_return": [0.01, -0.02, 0.03, 0.0],
        "exit_value": [100.0] * 4,
        "cal_days": [3, 6, 30, 60],
        "bars_elapsed": [3, 6, 20, 60],
        "roundtrip_cost_bp": [24.0] * 4,
    })
    s = RL.summarise(rt, bars_col="bars_elapsed", n_exits=4)
    assert s["bars_elapsed_median"] == pytest.approx((6 + 20) / 2)
    assert s["bars_elapsed_share_le_6"] == pytest.approx(0.5)
    assert s["bars_elapsed_share_le_3"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 预注册判据真值表（函数化，不预设结论）
# ---------------------------------------------------------------------------
def test_grade_requires_all_three_h2_conditions():
    base = {"pos_share": 0.60, "neg_share": 0.35, "gap_weighted_mean": 0.01, "bars_elapsed_median": 6.0}
    assert RL.grade(base)[0] == "H2_dominant"

    too_long = dict(base, bars_elapsed_median=22.0)
    assert RL.grade(too_long)[0] == "mixed"          # 只有「中位 bars 短」这一条不中

    negative = dict(base, gap_weighted_mean=-0.005)
    assert RL.grade(negative)[0] == "mixed"

    h1 = {"pos_share": 0.30, "neg_share": 0.65, "gap_weighted_mean": -0.02, "bars_elapsed_median": 10.0}
    assert RL.grade(h1)[0] == "H1_dominant"


def test_dedup_keeps_largest_exit_value_representative():
    rt = pd.DataFrame({
        "status": ["paired", "paired"],
        "symbol": ["AAA", "AAA"],
        "exit_date": pd.to_datetime(["2024-01-10", "2024-01-10"]),
        "entry_date": pd.to_datetime(["2024-01-20", "2024-01-20"]),
        "exit_value": [100.0, 900.0],
        "gap_return": [0.01, 0.02],
        "cal_days": [10, 10],
        "roundtrip_cost_bp": [24.0, 24.0],
        "run": ["r1", "r2"],
    })
    d = RL.dedup_round_trips(rt)
    assert len(d) == 1
    assert d.iloc[0]["run"] == "r2"          # 保留 |exit_value| 最大的那条作为代表
    assert d.iloc[0]["dup_group_size"] == 2
