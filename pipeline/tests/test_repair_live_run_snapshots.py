"""Offline tests for `scripts/repair_live_run_snapshots.py` (pure functions only).

FORENSIC CONTEXT (what this suite guards)
-----------------------------------------
The live archive under
``web/livetrading/data/runs/chan_four_state_blend/custom_astock_202609_20_new``
contains three runs whose NAV book was silently wrong:

* ``10-27/holdings.json`` was byte-identical to ``10-22/post_holdings.json``
  (md5 ``3517d8b523c17c50fbc1c7e7aa432e44``) while ``10-26/post_holdings.json``
  already described a different book -- i.e. the input lagged one whole round.
* because ``current_weight`` was computed from those stale share counts, three
  names whose true drift was POSITIVE (trimmed yesterday, so now underweight)
  were reported with a NEGATIVE drift past ``inertia_threshold=0.05`` and were
  ordered sold a second time.
* two names already exited on 10-26 came back as ``HOLD_FILTERED`` with
  ``target_shares=100``, and ``storage.post_holdings_from_ticket`` only keeps
  ``target_shares > 0`` keys -- so they could never leave the book.

This file pins ONLY the pure decision layer of the repair script (share
correction / weights / inertia / row restatement / CSV + ticket shape / .orig
idempotency). It never touches the real ``data/`` tree, never opens
``market.duckdb`` and never goes online: every fixture is built in ``tmp_path``
and the NAV path is exercised through a duck-typed fake reader.

WHY IT LIVES IN ``pipeline/tests/``
-----------------------------------
The shared ``pipeline`` venv has everything the script needs (pandas + duckdb
are optional at import time), and ``web/livetrading/tests`` requires ``fastapi``
which this venv does not have. Run it on its own::

    pipeline/.venv/bin/python -m pytest pipeline/tests/test_repair_live_run_snapshots.py -q
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "repair_live_run_snapshots.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("repair_live_run_snapshots", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


rep = _load_script()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
SELLABLE = "600900.SH"
TRIMMED = "000999.SZ"
ORPHAN_A = "600584.SH"
ORPHAN_B = "600362.SH"


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def make_runs(root: Path, *, stale_10_27: bool = True) -> dict:
    """Build a minimal three-run archive mirroring the real defect's shape."""
    for run in ("2026-10-22", "2026-10-26", "2026-10-27"):
        (root / run).mkdir(parents=True, exist_ok=True)

    pre_22 = {SELLABLE: 1300, TRIMMED: 1100, ORPHAN_A: 100, ORPHAN_B: 100}
    post_22 = dict(pre_22)
    post_26 = {SELLABLE: 800, TRIMMED: 600}

    _write_json(root / "2026-10-22" / "holdings.json", pre_22)
    _write_json(root / "2026-10-22" / "post_holdings.json", post_22)
    _write_json(root / "2026-10-26" / "holdings.json", post_22)
    _write_json(root / "2026-10-26" / "post_holdings.json", post_26)
    # 这里就是真实缺陷：10-27 的输入仍然是 10-22 的票后态
    _write_json(
        root / "2026-10-27" / "holdings.json",
        post_22 if stale_10_27 else post_26,
    )
    _write_json(root / "2026-10-27" / "post_holdings.json", post_26)

    for run in ("2026-10-22", "2026-10-26", "2026-10-27"):
        _write_json(root / run / "summary.json", {"portfolio_value": 100000.0, "date": run})
    return {r: root / r for r in ("2026-10-22", "2026-10-26", "2026-10-27")}


class FakeReader:
    """Duck-typed stand-in for ``MarketDbReader`` (no duckdb, no market.duckdb)."""

    available = False

    def __init__(self, factors=None, closes=None, ex_events=None):
        self._factors = factors if factors is not None else {}
        self._closes = closes if closes is not None else {}
        self._ex = ex_events if ex_events is not None else []
        self.errors = []

    def factor_series(self, symbol, end=None):
        s = self._factors.get(symbol)
        if s is None:
            return None
        return s if end is None else s[s.index <= pd.Timestamp(end)]

    def closes_between(self, symbol, start, end):
        return self._closes.get(symbol)

    def ex_dividend_events_after(self, as_of):
        return list(self._ex)


# ---------------------------------------------------------------------------
# 1) holdings correction
# ---------------------------------------------------------------------------
def test_correct_holdings_replaces_one_round_stale_input(tmp_path):
    run_dirs = make_runs(tmp_path / "runs")
    holdings, ev = rep.correct_holdings("2026-10-27", run_dirs)

    # 修正后 = 上一轮票后态，孤儿持仓随之消失
    assert holdings == {SELLABLE: 800, TRIMMED: 600}
    assert ORPHAN_A not in holdings and ORPHAN_B not in holdings
    assert ev["modified"] is True
    assert ev["expected_source"] == "2026-10-26/post_holdings.json"
    # 输入 md5 就是那个"滞后一整轮"的账本
    assert ev["actual_input_md5"] != ev["expected_source_md5"]
    assert ev["actual_input"][SELLABLE] == 1300


def test_correct_holdings_keeps_anchor_run_untouched(tmp_path):
    run_dirs = make_runs(tmp_path / "runs")
    holdings, ev = rep.correct_holdings("2026-10-22", run_dirs)
    assert holdings == json.loads((run_dirs["2026-10-22"] / "holdings.json").read_text("utf-8"))
    assert ev["modified"] is False
    assert ev["actual_input_md5"] == ev["expected_source_md5"]


def test_correct_holdings_is_a_noop_when_input_already_correct(tmp_path):
    run_dirs = make_runs(tmp_path / "runs", stale_10_27=False)
    holdings, ev = rep.correct_holdings("2026-10-27", run_dirs)
    assert holdings == {SELLABLE: 800, TRIMMED: 600}
    assert ev["modified"] is False


# ---------------------------------------------------------------------------
# 2) .orig backup idempotency
# ---------------------------------------------------------------------------
def test_ensure_orig_backup_is_byte_identical_and_never_overwritten(tmp_path):
    path = tmp_path / "summary.json"
    original = json.dumps({"portfolio_value": 197351.17}, indent=2).encode("utf-8")
    path.write_bytes(original)

    info = rep.ensure_orig_backup(path)
    backup = rep.orig_path(path)
    assert info["created"] is True and info["verified"] is True
    assert backup.read_bytes() == original
    assert info["md5"] == rep.md5_bytes(original)

    # 原件被"修复"改写后，再次运行不得覆盖 .orig
    path.write_bytes(b'{"portfolio_value": 198882.17}')
    info2 = rep.ensure_orig_backup(path)
    assert info2["created"] is False
    assert backup.read_bytes() == original
    assert info2["md5"] == rep.md5_bytes(original)


def test_read_source_prefers_orig_so_repairs_are_idempotent(tmp_path):
    path = tmp_path / "summary.json"
    pristine = {"portfolio_value": 197351.17, "stage": "stage2"}
    path.write_text(json.dumps(pristine, indent=2), encoding="utf-8")
    rep.ensure_orig_backup(path)
    path.write_text(json.dumps({"portfolio_value": 198882.17}, indent=2), encoding="utf-8")

    obj, kind = rep.read_source_json(path)
    assert kind == "orig"
    assert obj == pristine  # 第二遍运行从同一份原件出发 -> 输出恒定


# ---------------------------------------------------------------------------
# 3) weights / inertia (the arithmetic that produced the duplicate SELL)
# ---------------------------------------------------------------------------
def test_inertia_triggered_matches_the_5pct_rule():
    t = 0.05
    assert rep.inertia_triggered(0.0500, t) is True
    assert rep.inertia_triggered(-0.0550, t) is True
    assert rep.inertia_triggered(0.0312, t) is False
    assert rep.inertia_triggered(0.0, t) is False
    assert rep.inertia_triggered(None, t) is False
    assert rep.inertia_triggered(float("nan"), t) is False


def test_shares_to_weights_uses_the_recomputed_nav():
    w = rep.shares_to_weights({SELLABLE: 800}, {SELLABLE: 29.69}, 198882.17)
    assert w[SELLABLE] == pytest.approx(800 * 29.69 / 198882.17)
    # NAV 非正时不产生 inf/nan
    assert rep.shares_to_weights({SELLABLE: 800}, {SELLABLE: 29.69}, 0.0) == {}


def test_stale_shares_flip_the_drift_sign_the_way_the_incident_did():
    """真实漂移为正（应持有），过期股数却算出负漂移越过阈值 -> 多下一笔卖单。"""
    price, nav, tgt_w, t = 29.69, 198882.17, 0.130746, 0.05
    real = rep.shares_to_weights({SELLABLE: 800}, {SELLABLE: price}, nav)[SELLABLE]
    stale = rep.shares_to_weights({SELLABLE: 1300}, {SELLABLE: price}, nav)[SELLABLE]

    real_drift = tgt_w - real
    stale_drift = tgt_w - stale
    assert real_drift > 0 and rep.inertia_triggered(real_drift, t) is False
    assert stale_drift < 0 and rep.inertia_triggered(stale_drift, t) is True


def test_lot_rounded_sell_shares_rounds_up_to_a_whole_lot():
    delta, raw = rep.lot_rounded_sell_shares(-0.0648, 197351.17, 29.69, 1300)
    assert raw == pytest.approx(abs(-0.0648 * 197351.17) / 29.69)
    assert delta == -500  # ceil(430.92/100)*100
    # 不能卖出超过持仓
    assert rep.lot_rounded_sell_shares(-0.9, 100000.0, 10.0, 300)[0] == -300
    # 价格非正时不产生交易
    assert rep.lot_rounded_sell_shares(-0.1, 100000.0, 0.0, 100) == (0, 0.0)


# ---------------------------------------------------------------------------
# 4) single-row restatement
# ---------------------------------------------------------------------------
def _sell_row(sym=SELLABLE, cur_sh=1300, tgt_sh=800, cur_w=0.195575, tgt_w=0.130746):
    return {
        "symbol": sym,
        "action": "SELL",
        "price": 29.69,
        "current_weight": cur_w,
        "target_weight": tgt_w,
        "delta_weight": tgt_w - cur_w,
        "current_shares": cur_sh,
        "target_shares": tgt_sh,
        "delta_shares": tgt_sh - cur_sh,
        "shares": abs(tgt_sh - cur_sh),
        "trade_value": (tgt_sh - cur_sh) * 29.69,
        "trade_value_rmb": (tgt_sh - cur_sh) * 29.69,
        "name": "长江电力",
        "holding_bars": 3,
        "reason": "最优权重微调",
        "summary_reason": "最优权重微调",
        "strategy_version": "1.2.1",
        "base_version": "1.6.0",
        "execution_warning": None,
        "price_stale": False,
        "price_bar_date": "2026-10-27",
        "intraday_vwap": 29.5706,
        "vwap_premium_pct": 0.0040,
        "chan_5m_state": "5m_NEUTRAL",
        "suggested_limit_price": 29.63,
        "passive_support_limit_price": 29.63,
        "blocking_reason": "NONE",
        "detail_reason": {
            "summary_reason": "最优权重微调",
            "blocking_reason": "NONE",
            "is_liquidation": True,
            "dimension_signals": {"chan_four_state": 0.0},
            "lifecycle_state": {"symbol": sym, "current_weight": tgt_w, "broker_weight": cur_w},
            "lifecycle_decision": {
                "delta_w": tgt_w - cur_w, "inertia_threshold": 0.05,
                "raw_target_weight": tgt_w, "current_weight": cur_w,
            },
            "inertia_check": {"delta_weight": round(tgt_w - cur_w, 4), "threshold": 0.05, "triggered": True},
            "lot_rounding": {"target_shares_raw": -430.92, "shares": 500, "lot_size": 100},
        },
    }


def test_recompute_row_flips_sell_to_hold_and_never_trades():
    nav, thr = 198882.17, 0.05
    row = _sell_row()
    new = rep.recompute_equity_row(row, current_shares=800, nav=nav, threshold=thr)

    assert new["action"] == "HOLD"
    assert new["current_shares"] == 800
    assert new["target_shares"] == 800          # 不再"减到 800"，而是"本就 800"
    assert new["delta_shares"] == 0
    assert new["shares"] == 0
    assert new["trade_value"] == 0.0
    assert new["delta_weight"] > 0
    assert new["delta_weight"] == pytest.approx(0.130746 - 800 * 29.69 / nav)
    assert new["detail_reason"]["inertia_check"]["triggered"] is False
    assert new["detail_reason"]["is_liquidation"] is False
    assert new["detail_reason"]["lot_rounding"]["shares"] == 0
    assert new["detail_reason"]["original_action"] == "SELL"
    # 模型目标一律不重算
    assert new["target_weight"] == row["target_weight"]
    assert new["detail_reason"]["dimension_signals"] == {"chan_four_state": 0.0}


def test_recompute_row_is_pure_and_does_not_mutate_the_input_row():
    row = _sell_row()
    snapshot = json.dumps(row, sort_keys=True)
    rep.recompute_equity_row(row, current_shares=800, nav=198882.17, threshold=0.05)
    assert json.dumps(row, sort_keys=True) == snapshot


def test_recompute_row_zeroes_an_orphan_position():
    """600584/600362：已经离场，输入却还挂着 100 股。"""
    row = _sell_row(sym=ORPHAN_A, cur_sh=100, tgt_sh=100, cur_w=0.027996, tgt_w=0.0)
    row["action"] = "HOLD_FILTERED"
    row["delta_shares"] = 0
    new = rep.recompute_equity_row(row, current_shares=0, nav=198882.17, threshold=0.05)
    assert new["current_shares"] == 0
    assert new["current_weight"] == 0.0
    # 关键：target_shares 不再是 100，否则 storage.post_holdings_from_ticket 会永远保留它
    assert new["target_shares"] == 0
    assert new["trade_value"] == 0.0
    # 缺陷 2(a)：顶层归零后，detail_reason 里的同义字段必须一起归零，否则同一行自相矛盾
    assert new["detail_reason"]["lifecycle_state"]["current_weight"] == 0.0
    assert new["detail_reason"]["lifecycle_state"]["broker_weight"] == 0.0


def test_recompute_row_syncs_detail_reason_fields_with_the_top_level():
    """HOLD 行：detail_reason.summary_reason 不能停在旧结论（"最优权重微调"）。"""
    row = _sell_row()
    assert row["detail_reason"]["summary_reason"] == "最优权重微调"
    new = rep.recompute_equity_row(row, current_shares=800, nav=198882.17, threshold=0.05)
    assert new["action"] == "HOLD"
    assert new["summary_reason"] == "调仓未超惰性阈值 (< 5%)"
    assert new["detail_reason"]["summary_reason"] == new["summary_reason"]
    # current_weight / broker_weight 同源（股数 x 价格 / NAV），不许留旧值
    assert new["detail_reason"]["lifecycle_state"]["current_weight"] == pytest.approx(
        new["current_weight"]
    )
    assert new["detail_reason"]["lifecycle_state"]["broker_weight"] == pytest.approx(
        new["current_weight"]
    )


def test_recompute_row_still_sells_when_drift_really_exceeds_the_threshold():
    row = _sell_row()
    new = rep.recompute_equity_row(row, current_shares=1300, nav=198882.17, threshold=0.05)
    assert new["action"] == "SELL"
    assert new["target_shares"] < 1300
    assert new["delta_shares"] < 0
    assert new["trade_value"] < 0


# ---------------------------------------------------------------------------
# 5) whole-ticket restatement
# ---------------------------------------------------------------------------
def test_recompute_ticket_zeroes_the_cash_sweep_and_drops_orphan_targets():
    ticket = [
        _sell_row(),
        {
            "symbol": "BIL", "name": "流动性现金", "action": "SWEEP_CASH", "price": 1.0,
            "current_weight": 0.226845, "target_weight": 0.445881, "delta_weight": 0.219036,
            "current_shares": 0, "target_shares": 0, "delta_shares": 0, "shares": 0,
            "trade_value": 43227.0, "trade_value_rmb": 43227.0,
            "detail_reason": {"summary_reason": "流动性闲置资金归集/释放"},
        },
        dict(_sell_row(sym=ORPHAN_A, cur_sh=100, tgt_sh=100, cur_w=0.028, tgt_w=0.0),
             action="HOLD_FILTERED"),
    ]
    new_ticket, changes = rep.recompute_ticket(
        ticket,
        corrected_holdings={SELLABLE: 800},
        nav=198882.17,
        cash=98698.17,
        threshold=0.05,
    )
    by_sym = {r["symbol"]: r for r in new_ticket}

    # 卖出重复下单 -> HOLD，归集金额 0
    assert by_sym[SELLABLE]["action"] == "HOLD"
    assert by_sym[SELLABLE]["trade_value"] == 0.0
    assert by_sym["BIL"]["action"] == "HOLD"
    assert by_sym["BIL"]["trade_value"] == 0.0
    assert by_sym["BIL"]["current_weight"] == pytest.approx(98698.17 / 198882.17)
    assert by_sym["BIL"]["delta_weight"] == 0.0
    # 孤儿持仓不再带 target_shares > 0
    assert by_sym[ORPHAN_A]["target_shares"] == 0
    assert by_sym[ORPHAN_A]["current_shares"] == 0

    assert len(changes) == len(ticket)
    assert {c["symbol"] for c in changes} == {SELLABLE, "BIL", ORPHAN_A}


def test_recomputed_ticket_shape_keeps_every_original_column():
    row = _sell_row()
    ticket, _ = rep.recompute_ticket(
        [row], corrected_holdings={SELLABLE: 800}, nav=198882.17, cash=0.0, threshold=0.05
    )
    assert set(row.keys()) - set(ticket[0].keys()) == set()
    assert all(col in ticket[0] for col in rep.CSV_COLUMNS)

    csv_text = rep.ticket_to_csv(ticket).decode("utf-8")
    header = csv_text.splitlines()[0]
    assert header == ",".join(rep.CSV_COLUMNS)
    assert len(csv_text.splitlines()) == 2


# ---------------------------------------------------------------------------
# 6) summary patch keeps everything else byte-for-byte
# ---------------------------------------------------------------------------
def test_patch_summary_splits_nav_semantics_by_canonical_contract():
    before = {
        "clock": {"as_of_trading_date": "2026-10-27"},
        "date": "2026-10-27",
        "portfolio_value": 197351.17,
        "deploy_stdout": "账户总资产估值 : ¥197,351.17",
        "trades_count": 3,
    }
    after = rep.patch_summary(
        before, nav=198882.17, cash=98698.17, cash_source="bil_fallback", warnings=["w"]
    )
    for key in before:
        if key in ("portfolio_value",):
            continue
        assert after[key] == before[key]
    # portfolio_value = 重算含息 MTM；原输入另存
    assert after["portfolio_value"] == 198882.17
    assert after["portfolio_value_original"] == 197351.17
    # nav_effective 是历史事实（子进程当轮实收）——不得改写成重算值
    assert after["nav_effective"] == 197351.17
    assert after["nav_computed"] == 198882.17
    assert after["nav_source"] == "restated"
    assert after["nav_restatement"] == {
        "before": 197351.17,
        "after": 198882.17,
        "delta": 1531.0,
        "reason": after["nav_restatement"]["reason"],
    }
    assert after["cash"] == 98698.17
    assert after["cash_source"] == "bil_fallback"
    assert after["nav_warnings"] == ["w"]
    # 原有键的相对顺序不变，新键追加在尾部
    assert list(after.keys())[: len(before)] == list(before.keys())


def test_patch_summary_never_double_shifts_on_a_second_pass():
    """第二次运行时 portfolio_value_original 已在快照里，不能再被当成"原值"。"""
    first = rep.patch_summary(
        {"portfolio_value": 197351.17}, nav=198882.17, cash=98698.17,
        cash_source="bil_fallback", warnings=[],
    )
    second = rep.patch_summary(
        first, nav=198882.17, cash=98698.17, cash_source="bil_fallback", warnings=[],
    )
    assert second == first
    assert second["portfolio_value_original"] == 197351.17
    assert second["nav_effective"] == 197351.17


def test_before_fix_decomposition_recovers_the_frozen_split():
    ticket = [{"symbol": "BIL", "current_weight": 0.226845, "action": "SWEEP_CASH"}]
    split = rep._before_fix_decomposition(ticket, {"portfolio_value": 197351.17})
    # 金额一律取到分（引擎内部的浮点尾数不该写进快照）
    assert split["cash"] == pytest.approx(round(0.226845 * 197351.17, 2), abs=1e-9)
    assert split["equity"] + split["cash"] == pytest.approx(197351.17, abs=0.01)


# ---------------------------------------------------------------------------
# 7) NAV engine wiring (fake reader, no duckdb, no network)
# ---------------------------------------------------------------------------
def test_compute_run_nav_uses_snapshot_price_and_flat_factor():
    # 因子表覆盖入场日与 as_of 之间：常数序列 -> 比值恰好 1.0
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-21"), pd.Timestamp("2026-10-22")])
    flat = pd.Series([1.4, 1.4], index=idx, name="factor_hfq")
    reader = FakeReader(
        factors={SELLABLE: flat},
        closes={
            SELLABLE: pd.Series(
                [29.57], index=pd.DatetimeIndex([pd.Timestamp("2026-10-21")])
            )
        },
    )
    breakdown = rep.compute_run_nav(
        run_label="2026-10-27",
        holdings={SELLABLE: 800},
        prices={SELLABLE: ("2026-10-27", 29.69)},
        reader=reader,
        entry_prices={SELLABLE: 29.57},
        cash=98698.17,
        cash_source="bil_fallback",
        fallback_anchor="2026-10-22",
    )
    # 因子是常数 -> 比值 1.0 -> 市值就是 股数 x 快照价
    assert breakdown["equity"] == pytest.approx(800 * 29.69)
    assert breakdown["cash"] == pytest.approx(98698.17)
    assert breakdown["nav_effective"] == pytest.approx(800 * 29.69 + 98698.17)
    assert breakdown["nav_computed"] == breakdown["nav_effective"]
    assert breakdown["nav_source"] == "computed"
    assert breakdown["raw_valuation_total"] == pytest.approx(800 * 29.69)
    assert breakdown["anchors"] == {SELLABLE: "2026-10-21"}
    row = breakdown["per_symbol"][SELLABLE]
    assert row["price"] == 29.69
    assert row["raw_close"] == 29.69          # canonical 键：快照价即裸收盘价
    assert row["value_raw"] == pytest.approx(800 * 29.69)
    assert row["dividend_basis"] == "total_return"
    assert row["counted_as"] == "equity"
    assert row["price_bar_date"] == "2026-10-27"
    assert row["shares"] == 800
    assert row["stale"] is False
    assert row["factor_missing"] is False
    assert row["anchor_date"] == "2026-10-21"  # entry_price 反查命中
    assert row["anchor_source"] == "entry_price_db_match"


def test_compute_run_nav_marks_missing_factor_instead_of_silently_faking_it():
    reader = FakeReader(factors={}, closes=None)
    breakdown = rep.compute_run_nav(
        run_label="2026-10-27",
        holdings={"BIL_LIKE": 10},
        prices={"BIL_LIKE": ("2026-10-27", 100.0)},
        reader=reader,
        entry_prices={},
        cash=500.0,
        cash_source="explicit",
        fallback_anchor="2026-10-22",
    )
    row = breakdown["per_symbol"]["BIL_LIKE"]
    assert row["factor_missing"] is True
    assert row["value"] == pytest.approx(1000.0)  # raw 估值
    assert row["value_raw"] == pytest.approx(1000.0)
    assert row["dividend_basis"] == "raw"
    assert row["anchor_source"] == "anchor_fallback"
    assert any("factor_missing" in w for w in breakdown["nav_warnings"])


# ---------------------------------------------------------------------------
# 8) schema compatibility with the production writer  (defect 1)
# ---------------------------------------------------------------------------
#: canonical 键集，逐字取自 ``web/livetrading/execution_service.py``
#: ``_write_nav_breakdown`` 的 payload 字面量（:1346-1368）。生产端读
#: ``["nav_effective"]`` / ``["nav_warnings"]`` / ``["anchors"]`` /
#: ``per_symbol[*]["raw_close"]`` / ``["value_raw"]``，缺一个就 KeyError。
CANONICAL_TOP_LEVEL_KEYS = {
    "as_of",            # :1347
    "nav_effective",    # :1348
    "nav_computed",     # :1349
    "nav_source",       # :1350
    "equity",           # :1351
    "cash",             # :1352
    "cash_source",      # :1353
    "anchor_source",    # :1357
    "anchors",          # :1358
    "raw_valuation_total",  # :1359-1363（sum of value_raw, counted_as == "equity"）
    "stale_symbols",    # :1364
    "dividend_alarms",  # :1365
    "nav_warnings",     # :1366
    "per_symbol",       # :1367
}

#: per_symbol 的 canonical 键集 = ``valuation.compute_nav`` 原样落盘的条目
#: （``web/livetrading/valuation.py`` :604-625、:456-469；生产 writer 直接
#: 把 nav_info["per_symbol"] 写去，不改键）。
CANONICAL_PER_SYMBOL_KEYS = {
    "shares",
    "raw_close",
    "factor_hfq",
    "anchor_date",
    "value",
    "value_raw",
    "anchor_inflation_ratio",
    "full_history_ratio",
    "dividend_basis",
    "stale",
    "mark_date",
    "factor_anchor_hfq",
    "counted_as",
}


def _assert_breakdown_schema(breakdown):
    missing = CANONICAL_TOP_LEVEL_KEYS - set(breakdown)
    assert not missing, f"nav_breakdown 缺少生产端会读的 canonical 键: {sorted(missing)}"
    # 同名异义是禁止的：旧的 nav / warnings 必须已经改名而不是并存
    assert "nav" not in breakdown
    assert "warnings" not in breakdown
    for sym, row in breakdown["per_symbol"].items():
        miss = CANONICAL_PER_SYMBOL_KEYS - set(row)
        assert not miss, f"per_symbol[{sym}] 缺 canonical 键: {sorted(miss)}"
        assert "price" in row  # 取证附加键保留
        assert row["dividend_basis"] in ("total_return", "raw")
        # raw_valuation_total 的求和口径必须能对上
        assert row["value_raw"] == pytest.approx(row["shares"] * row["raw_close"], abs=0.01)


def test_compute_run_nav_output_is_a_superset_of_the_production_schema():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-21"), pd.Timestamp("2026-10-22")])
    flat = pd.Series([1.4, 1.4], index=idx, name="factor_hfq")
    reader = FakeReader(
        factors={SELLABLE: flat},
        closes={
            SELLABLE: pd.Series([29.57], index=pd.DatetimeIndex([pd.Timestamp("2026-10-21")]))
        },
    )
    breakdown = rep.compute_run_nav(
        run_label="2026-10-27",
        holdings={SELLABLE: 800},
        prices={SELLABLE: ("2026-10-27", 29.69)},
        reader=reader,
        entry_prices={SELLABLE: 29.57},
        cash=98698.17,
        cash_source="bil_fallback",
        fallback_anchor="2026-10-22",
    )
    _assert_breakdown_schema(breakdown)
    expected_raw = sum(
        e["value_raw"] for e in breakdown["per_symbol"].values()
        if e["counted_as"] == "equity"
    )
    assert breakdown["raw_valuation_total"] == pytest.approx(expected_raw, abs=0.01)
    # 取证键没被删
    for extra in ("anchor_evidence", "run_label", "archive_nav_source"):
        assert extra in breakdown


ARCHIVE_RUNS = (
    REPO_ROOT
    / "web/livetrading/data/runs/chan_four_state_blend/custom_astock_202609_20_new"
)


def test_archived_nav_breakdowns_are_a_superset_of_the_production_schema():
    """真实产出物（若本机存在归档）也必须满足同一套 canonical 契约。"""
    if not ARCHIVE_RUNS.is_dir():
        pytest.skip("归档 run 目录不存在（干净 checkout）")
    for run in ("2026-10-22", "2026-10-26", "2026-10-27"):
        path = ARCHIVE_RUNS / run / "nav_breakdown.json"
        if not path.is_file():
            pytest.skip(f"{path} 尚未生成")
        breakdown = json.loads(path.read_text("utf-8"))
        _assert_breakdown_schema(breakdown)
        # 生产端 raw_valuation_total 的求和在读侧复现一次
        expected_raw = sum(
            float(e.get("value_raw") or 0.0)
            for e in breakdown["per_symbol"].values()
            if e.get("counted_as") == "equity"
        )
        assert breakdown["raw_valuation_total"] == pytest.approx(expected_raw, abs=0.01), run
        assert breakdown["nav_effective"] == breakdown["nav_computed"], run
        assert isinstance(breakdown["nav_warnings"], list), run
        assert isinstance(breakdown["anchors"], dict) and breakdown["anchors"], run


# ---------------------------------------------------------------------------
# 9) summary.json field contract  (defect 3)
# ---------------------------------------------------------------------------
def test_patch_summary_satisfies_the_nav_field_contract():
    after = rep.patch_summary(
        {"portfolio_value": 197351.17},
        nav=198646.17,
        cash=46216.17,
        cash_source="bil_fallback",
        warnings=[],
    )
    assert after["portfolio_value"] != after["portfolio_value_original"]
    assert after["nav_effective"] == after["portfolio_value_original"]
    assert after["nav_source"] == "restated"
    assert after["nav_restatement"]["delta"] == pytest.approx(
        after["portfolio_value"] - after["portfolio_value_original"]
    )


def test_archived_summaries_satisfy_the_nav_field_contract():
    if not ARCHIVE_RUNS.is_dir():
        pytest.skip("归档 run 目录不存在（干净 checkout）")
    for run in ("2026-10-22", "2026-10-26", "2026-10-27"):
        path = ARCHIVE_RUNS / run / "summary.json"
        if not path.is_file():
            pytest.skip(f"{path} 尚未生成")
        summary = json.loads(path.read_text("utf-8"))
        assert summary["nav_source"] == "restated", run
        # 历史事实：三份的 nav_effective 都是当轮子进程实收的 197,351.17
        assert summary["nav_effective"] == pytest.approx(197351.17), run
        assert summary["portfolio_value_original"] == pytest.approx(197351.17), run
        assert summary["nav_computed"] == pytest.approx(summary["portfolio_value"]), run
        assert summary["nav_restatement"]["before"] == pytest.approx(197351.17), run
        assert summary["nav_restatement"]["after"] == pytest.approx(summary["portfolio_value"])
        assert summary["nav_restatement"]["delta"] == pytest.approx(
            summary["portfolio_value"] - summary["portfolio_value_original"]
        ), run
        if run == "2026-10-22":
            assert summary["portfolio_value"] == pytest.approx(197351.17), run
        else:
            assert summary["portfolio_value"] != summary["portfolio_value_original"], run
        # canonical UI 字段（app.js::renderNavProvenance 读这两个）
        assert "cash" in summary and "nav_warnings" in summary


# ---------------------------------------------------------------------------
# 10) ex-dividend / factor-step check runs real SQL (defect 4)
# ---------------------------------------------------------------------------
class _FakeCon:
    def __init__(self, events, factors):
        self._events = events
        self._factors = factors
        self.seen = []

    def execute(self, sql, params=None):
        self.seen.append(sql)
        return _FakeRel(sql, self._events, self._factors)


class _FakeRel:
    def __init__(self, sql, events, factors):
        self._sql = sql
        self._events = events
        self._factors = factors

    def df(self):
        rows = self._events if "raw_adjustment_events" in self._sql else self._factors
        return pd.DataFrame(rows)


class _FakeDbReader:
    available = True

    def __init__(self, events, factors, path="<fake>"):
        self.path = path
        self._con = _FakeCon(events, factors)


def test_ex_dividend_check_detects_the_step_and_rules_out_double_counting():
    """000999.SZ 2026-10-16 真的除权了（0.415/股），因子在当天跳了一级。

    锚点日 2026-10-21 >= 阶跃日 -> 两端同因子 -> 比值 1.0，无重复计息。
    """
    events = [{
        "thscode": "000999.SZ", "ex_date": "2026-10-16",
        "dividend_per_share": 0.415, "per_share_bonus": 0.0,
    }]
    factors = [
        {"thscode": "000999.SZ", "date": "2026-10-15", "backward_factor": 2.068294},
        {"thscode": "000999.SZ", "date": "2026-10-16", "backward_factor": 2.101948},
        {"thscode": "000999.SZ", "date": "2026-10-22", "backward_factor": 2.101948},
        {"thscode": "600900.SH", "date": "2026-10-15", "backward_factor": 1.438852},
        {"thscode": "600900.SH", "date": "2026-10-22", "backward_factor": 1.438852},
    ]
    check = rep.ex_dividend_check(
        _FakeDbReader(events, factors),
        symbols=["000999.SZ", "600900.SH"],
        anchors_by_symbol={"000999.SZ": "2026-10-21", "600900.SH": "2026-10-21"},
    )
    assert check["read_only"] is True
    assert "ex_date > DATE '2026-10-15'" in check["queries"]["raw_adjustment_events"]
    assert "date >= DATE '2026-10-15'" in check["queries"]["calc_adjust_factor_daily"]
    assert check["raw_output"]["raw_adjustment_events"][0]["ex_date"] == "2026-10-16"

    nine = check["per_symbol"]["000999.SZ"]
    assert nine["event_count"] == 1
    assert nine["step_count"] == 1
    assert nine["steps"][0]["date"] == "2026-10-16"
    assert nine["steps"][0]["ratio"] == pytest.approx(2.101948 / 2.068294, rel=1e-6)
    assert nine["anchor_at_or_after_last_step"] is True
    assert "1.0" in nine["conclusion"]

    nine_hundred = check["per_symbol"]["600900.SH"]
    assert nine_hundred["event_count"] == 0
    assert nine_hundred["step_count"] == 0

    assert check["totals"] == {"event_count": 1, "step_count": 1}
    assert check["errors"] == []


def test_ex_dividend_check_flags_a_step_that_postdates_the_anchor():
    """锚点早于阶跃日 -> 比值 > 1 -> 必须被判为"需人工处理"，不能静默放过。"""
    factors = [
        {"thscode": "000999.SZ", "date": "2026-10-15", "backward_factor": 2.068294},
        {"thscode": "000999.SZ", "date": "2026-10-16", "backward_factor": 2.101948},
    ]
    check = rep.ex_dividend_check(
        _FakeDbReader([], factors),
        symbols=["000999.SZ"],
        anchors_by_symbol={"000999.SZ": "2026-10-15"},
    )
    nine = check["per_symbol"]["000999.SZ"]
    assert nine["step_count"] == 1
    assert nine["anchor_at_or_after_last_step"] is False
    assert "需人工处理" in check["verdict"]
    assert "000999.SZ" in check["verdict"]


def test_ex_dividend_check_reports_db_unavailable_instead_of_faking_an_empty_result():
    check = rep.ex_dividend_check(
        FakeReader(), symbols=["000999.SZ"], anchors_by_symbol={"000999.SZ": "2026-10-21"}
    )
    assert check["errors"], "库不可用时必须显式报错，不能假装查到了 0 个事件"
    assert check["per_symbol"]["000999.SZ"]["event_count"] == 0
