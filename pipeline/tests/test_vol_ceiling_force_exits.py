"""(PLAN C) Vol-ceiling enforcement: forcibly flatten ONLY already-exiting positions.

FORENSIC CONTEXT (replay of `2026-11-09`)
-----------------------------------------
`chan_four_state_blend` shipped a book that was 11.72pp OVER the Stage-1 Barroso ceiling:

    capped_equity_sum        = 0.3995   (binding = true)
    post_lifecycle_equity_sum = 0.5661   (`000157.SZ` parked in `EXIT_CONFIRMING`, 16.66%)
    delivered_equity_sum      = 0.5166
    ceiling_exceeded_by       = 0.1172

i.e. `binding=true` meant "the TARGET VECTOR was scaled", not "the book was reduced". The
lifecycle machine keeps holding a position that is already in an exit state until its
confirmation counters / tenure run out, and nothing re-applied the ceiling afterwards.

WHAT IS PINNED HERE (100% offline, pure row dicts, no data/network/filesystem writes)
-------------------------------------------------------------------------------------
1. binding + post=0.5661 + capped=0.3995 + `000157.SZ` in `EXIT_CONFIRMING` -> a forced SELL
   is emitted and the book ends inside the ceiling;
2. the same overage coming from `HELD_ACTIVE` names -> NOTHING is sold, `overage` survives and
   `force_exit_skipped` records `no_exit_intent` (a silent de-risk of a wanted position is not
   risk management, it is an override);
3. `000157.SZ` bought TODAY (China, `holding_bars == 0`), even in `EXIT_CONFIRMING` -> skipped
   with `t_plus_one` (broker settlement, not a risk preference);
4. an overage under 1pp -> no trade at all (dead-band against churn);
5. switch OFF -> behaviour identical to 1.3.0, no forced rows / no new ticket keys;
6. the forced row is a well-formed SELL (parity-safe: one printed line, one order).
"""

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.run_live_strategy import (  # noqa: E402
    TICKET_SELL_STDOUT_MARKER,
    VOL_CEILING_FORCE_EXIT_MIN_OVERAGE,
    VOL_CEILING_FORCE_EXIT_REASON,
    apply_vol_ceiling_force_exits,
    format_vol_ceiling_force_exit_line,
    format_vol_ceiling_force_exit_skip_line,
    resolve_vol_ceiling_force_exits_enabled,
    verify_ticket_stdout_order_parity,
)

CASH = "BIL"
NAV = 1_000_000.0
PRICE = 10.0

#: The archived 2026-11-09 numbers.
CAPPED = 0.3995
POST_LIFECYCLE = 0.5661


def _row(
    symbol,
    *,
    weight,
    state="HELD_ACTIVE",
    holding_bars=20,
    action="HOLD",
    price=PRICE,
    price_stale=False,
    shares=None,
    blocking_reason="NONE",
    exit_confirming=False,
):
    """One ticket-shaped row; `shares * price / NAV == weight` by construction.

    `exit_confirming=True` reproduces what the live machine actually emits for a position
    whose exit is still being confirmed: `state=PENDING_EXIT`, `action=HOLD_FILTERED`,
    `blocking_reason=EXIT_CONFIRMING` (there is no `EXIT_CONFIRMING` lifecycle STATE).
    """
    if exit_confirming:
        state = "PENDING_EXIT"
        action = "HOLD_FILTERED"
        blocking_reason = "EXIT_CONFIRMING"
    if shares is None:
        shares = int(round(weight * NAV / price))
    return {
        "symbol": symbol,
        "name": symbol,
        "action": action,
        "price": price,
        "current_weight": weight,
        "target_weight": weight,
        "target_weight_model": weight,
        "target_weight_effective": weight,
        "delta_weight": 0.0,
        "current_shares": shares,
        "target_shares": shares,
        "delta_shares": 0,
        "shares": 0,
        "trade_value": 0.0,
        "trade_value_rmb": 0.0,
        "holding_bars": holding_bars,
        "reason": "最优权重微调",
        "summary_reason": "最优目标权重维持",
        "detail_reason": {
            "lifecycle_state": {"state": state},
            "is_liquidation": False,
            "inertia_check": {"delta_weight": 0.0, "threshold": 0.05, "triggered": False},
            "lot_rounding": {"target_shares_raw": float(shares), "shares": 0, "lot_size": 100},
        },
        "strategy_version": "1.3.1",
        "base_version": "2.0.0",
        "execution_warning": None,
        "price_stale": bool(price_stale),
        "price_bar_date": "2026-11-09",
        "intraday_vwap": None,
        "vwap_premium_pct": None,
        "chan_5m_state": "N/A",
        "suggested_limit_price": price,
        "passive_support_limit_price": price,
        "blocking_reason": blocking_reason,
    }


def _archived_rows():
    """The 2026-11-09 book: 000157.SZ exiting at 16.66% + a 39.95% wanted book."""
    return [
        _row("000157.SZ", weight=0.1666, exit_confirming=True, holding_bars=6),
        _row("600000.SH", weight=0.1995),
        _row("600036.SH", weight=0.20),
        _row(CASH, weight=0.4339, state="", shares=0),
    ]


def _equity_sum(rows):
    return sum(
        max(0.0, float(r["target_weight"]))
        for r in rows
        if str(r["symbol"]) != CASH
    )


def _run(rows, *, capped=CAPPED, binding=True, enabled=True):
    return apply_vol_ceiling_force_exits(
        rows,
        capped_equity_sum=capped,
        binding=binding,
        portfolio_value=NAV,
        cash_proxy=CASH,
        enabled=enabled,
        default_market="china",
    )


# ---------------------------------------------------------------------------
# 1. the archived round: the pending exit is flattened, the book ends inside
# ---------------------------------------------------------------------------


def test_archived_overage_forces_out_the_exit_confirming_position():
    rows = _archived_rows()
    assert _equity_sum(rows) == pytest.approx(POST_LIFECYCLE, abs=1e-9)

    report = _run(rows)

    assert report["applied"] is True
    assert [f["symbol"] for f in report["forced_exits"]] == ["000157.SZ"]
    forced = report["forced_exits"][0]
    assert forced["lifecycle_state"] == "EXIT_CONFIRMING"
    assert forced["reason"] == VOL_CEILING_FORCE_EXIT_REASON
    assert forced["shares"] == 16660
    assert forced["weight"] == pytest.approx(0.1666, abs=1e-9)
    assert report["forced_exit_equity_sum"] == pytest.approx(0.1666, abs=1e-9)

    # the whole point: the delivered book is back inside the ceiling
    assert _equity_sum(rows) == pytest.approx(CAPPED, abs=1e-9)
    assert report["overage_before"] == pytest.approx(0.1666, abs=1e-9)
    assert report["overage_after"] == pytest.approx(0.0, abs=1e-9)
    assert report["post_lifecycle_equity_sum_before_force"] == pytest.approx(
        POST_LIFECYCLE, abs=1e-9
    )
    assert report["post_lifecycle_equity_sum_after_force"] == pytest.approx(
        CAPPED, abs=1e-9
    )


def test_forced_row_becomes_a_clean_full_exit_sell():
    rows = _archived_rows()
    _run(rows)
    row = rows[0]

    assert row["action"] == "SELL"
    assert row["target_weight"] == 0.0
    assert row["target_shares"] == 0
    assert row["delta_shares"] == -16660
    assert row["shares"] == 16660
    assert row["trade_value"] == pytest.approx(-166600.0)
    assert row["blocking_reason"] == "NONE"
    assert row["detail_reason"]["is_liquidation"] is True
    assert row["detail_reason"]["vol_ceiling_force_exit"]["reason"] == (
        VOL_CEILING_FORCE_EXIT_REASON
    )
    # the ticket reason carries the greppable token so "strategy sold" and "risk sold"
    # stay distinguishable after the fact
    assert VOL_CEILING_FORCE_EXIT_REASON in row["reason"]
    assert "原信号: 最优权重微调" in row["reason"]


# ---------------------------------------------------------------------------
# 2. an overage made of WANTED positions is never traded away
# ---------------------------------------------------------------------------


def test_held_active_overage_is_never_force_sold():
    rows = [
        _row("600000.SH", weight=0.30, state="HELD_ACTIVE"),
        _row("600036.SH", weight=0.2661, state="HELD_ACTIVE"),
        _row(CASH, weight=0.4339, state="", shares=0),
    ]
    report = _run(rows)

    assert report["applied"] is False
    assert report["forced_exits"] == []
    assert report["forced_exit_equity_sum"] == 0.0
    # the drift is PRESERVED, not silently "fixed"
    assert report["overage_after"] == pytest.approx(0.1666, abs=1e-9)
    assert report["overage_before"] == pytest.approx(0.1666, abs=1e-9)
    assert _equity_sum(rows) == pytest.approx(POST_LIFECYCLE, abs=1e-9)
    skipped = {s["symbol"]: s["reason"] for s in report["force_exit_skipped"]}
    assert skipped["600000.SH"] == "no_exit_intent"
    assert skipped["600036.SH"] == "no_exit_intent"
    for r in rows:
        if r["symbol"] != CASH:
            assert r["action"] == "HOLD"


# ---------------------------------------------------------------------------
# 3. China T+1 beats the exit intent (broker settlement, not a preference)
# ---------------------------------------------------------------------------


def test_todays_buy_in_an_exit_state_is_still_unsellable():
    rows = _archived_rows()
    rows[0]["holding_bars"] = 0  # bought today

    report = _run(rows)

    assert report["applied"] is False
    assert report["forced_exits"] == []
    assert {"symbol": "000157.SZ", "reason": "t_plus_one"} in report["force_exit_skipped"]
    assert rows[0]["action"] == "HOLD_FILTERED"  # untouched: still just confirming
    assert rows[0]["target_shares"] == rows[0]["current_shares"]
    assert report["overage_after"] == pytest.approx(0.1666, abs=1e-9)


def test_non_china_symbols_have_no_t_plus_one_gate():
    """The same holding_bars=0 row on a US symbol IS sellable (no China settlement rule)."""
    rows = [
        _row("AAPL", weight=0.1666, exit_confirming=True, holding_bars=0),
        _row("MSFT", weight=0.3995, state="HELD_ACTIVE"),
        _row(CASH, weight=0.4339, state="", shares=0),
    ]
    report = apply_vol_ceiling_force_exits(
        rows,
        capped_equity_sum=CAPPED,
        binding=True,
        portfolio_value=NAV,
        cash_proxy=CASH,
        default_market="us",
    )
    assert report["applied"] is True
    assert [f["symbol"] for f in report["forced_exits"]] == ["AAPL"]


def test_a_stale_or_missing_price_is_never_force_sold():
    rows = _archived_rows()
    rows[0]["price_stale"] = True
    report = _run(rows)
    assert report["applied"] is False
    assert {"symbol": "000157.SZ", "reason": "no_price"} in report["force_exit_skipped"]

    rows = _archived_rows()
    rows[0]["price"] = 0.0
    report = _run(rows)
    assert report["applied"] is False
    assert {"symbol": "000157.SZ", "reason": "no_price"} in report["force_exit_skipped"]


# ---------------------------------------------------------------------------
# 4. sub-1pp overage is a dead-band
# ---------------------------------------------------------------------------


def test_sub_one_percent_overage_is_left_alone():
    rows = [
        _row("000157.SZ", weight=0.1666, exit_confirming=True),
        _row("600000.SH", weight=0.2379, state="HELD_ACTIVE"),
        _row(CASH, weight=0.5955, state="", shares=0),
    ]
    assert _equity_sum(rows) - CAPPED == pytest.approx(0.005, abs=1e-9)
    report = _run(rows)

    assert report["skipped_reason"] == "below_threshold"
    assert report["applied"] is False
    assert report["forced_exits"] == []
    assert report["overage_after"] == pytest.approx(0.005, abs=1e-9)
    assert rows[0]["action"] == "HOLD_FILTERED"  # untouched


def test_threshold_is_configurable():
    rows = [
        _row("000157.SZ", weight=0.1666, exit_confirming=True),
        _row("600000.SH", weight=0.2379, state="HELD_ACTIVE"),
        _row(CASH, weight=0.5955, state="", shares=0),
    ]
    report = apply_vol_ceiling_force_exits(
        rows,
        capped_equity_sum=CAPPED,
        binding=True,
        portfolio_value=NAV,
        cash_proxy=CASH,
        default_market="china",
        min_overage=0.001,
    )
    assert report["applied"] is True
    assert VOL_CEILING_FORCE_EXIT_MIN_OVERAGE == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# 5. ordering, no double-selling, and the OFF switch
# ---------------------------------------------------------------------------


def test_largest_contribution_goes_first_and_stops_inside_the_ceiling():
    rows = [
        _row("000157.SZ", weight=0.10, exit_confirming=True),
        _row("600000.SH", weight=0.20, exit_confirming=True),
        _row("600036.SH", weight=0.2661, state="HELD_ACTIVE"),
        _row(CASH, weight=0.4339, state="", shares=0),
    ]
    report = _run(rows)  # overage 0.1666 -> only the 0.20 leg is needed
    assert [f["symbol"] for f in report["forced_exits"]] == ["600000.SH"]
    assert _equity_sum(rows) == pytest.approx(0.3661, abs=1e-9)
    assert report["overage_after"] == pytest.approx(0.0)


def test_an_already_planned_liquidation_is_not_double_counted():
    rows = _archived_rows()
    rows[0]["action"] = "SELL"
    rows[0]["target_weight"] = 0.0
    rows[0]["target_shares"] = 0
    report = _run(rows)  # post_lifecycle is now 0.3995 -> nothing to enforce
    assert report["applied"] is False
    assert report["skipped_reason"] == "below_threshold"


def test_switch_off_reproduces_1_3_0_behaviour():
    rows = _archived_rows()
    report = _run(rows, enabled=False)

    assert report["skipped_reason"] == "disabled"
    assert report["applied"] is False
    assert report["forced_exits"] == []
    assert report["force_exit_skipped"] == []
    assert report["overage_before"] is None
    assert _equity_sum(rows) == pytest.approx(POST_LIFECYCLE, abs=1e-9)
    assert rows[0]["action"] == "HOLD_FILTERED"  # 1.3.0 behaviour: nothing forced


def test_switch_resolution_is_prefixed_and_allowlisted():
    # default ON for the allowlisted strategy
    assert resolve_vol_ceiling_force_exits_enabled("chan_four_state_blend", {}) is True
    # prefixed key OFF
    assert (
        resolve_vol_ceiling_force_exits_enabled(
            "chan_four_state_blend",
            {"cfsb_portfolio_vol_ceiling_force_exits": "false"},
        )
        is False
    )
    # a BARE name must never enable/disable it (the `wq_alpha_id` collision class)
    assert (
        resolve_vol_ceiling_force_exits_enabled(
            "chan_four_state_blend",
            {"portfolio_vol_ceiling_force_exits": False},
        )
        is True
    )
    # every other strategy keeps the overlay off, whatever its params say
    assert (
        resolve_vol_ceiling_force_exits_enabled(
            "chan_risk_managed_blend",
            {"cfsb_portfolio_vol_ceiling_force_exits": True},
        )
        is False
    )


def test_non_binding_ceiling_never_sells_anything():
    rows = _archived_rows()
    report = _run(rows, binding=False)
    assert report["applied"] is False
    assert report["skipped_reason"] == "not_binding"
    assert _equity_sum(rows) == pytest.approx(POST_LIFECYCLE, abs=1e-9)
    # a zero/absent ceiling must never read as "sell everything"
    report = _run(_archived_rows(), capped=0.0)
    assert report["applied"] is False
    assert report["skipped_reason"] == "not_binding"  # never read as "sell everything"


# ---------------------------------------------------------------------------
# 6. stdout line + ticket parity
# ---------------------------------------------------------------------------


def test_stdout_lines_name_the_count_the_pp_and_the_skips():
    rows = _archived_rows()
    rows[0]["holding_bars"] = 0
    report = _run(rows)  # nothing forced, 000157 skipped T+1

    line = format_vol_ceiling_force_exit_line(report, CAPPED)
    assert line.startswith("[INFO/VOL CEILING]")
    assert "强制平掉 0 个退出期仓位" in line
    assert "39.95%" in line
    skip_line = format_vol_ceiling_force_exit_skip_line(report)
    assert skip_line is not None and "t_plus_one: 000157.SZ" in skip_line

    rows = _archived_rows()
    report = _run(rows)
    line = format_vol_ceiling_force_exit_line(report, CAPPED)
    assert "强制平掉 1 个退出期仓位" in line
    assert "16.66pp" in line
    assert "39.95%" in line


def test_forced_row_keeps_ticket_stdout_parity():
    rows = _archived_rows()
    _run(rows)
    forced_rows = [r for r in rows if r["action"] == "SELL"]
    assert len(forced_rows) == 1
    stdout = f"  {TICKET_SELL_STDOUT_MARKER}000157.SZ (测试) | 现价: 10.00 | 卖出: 16660 股\n"
    parity = verify_ticket_stdout_order_parity(
        forced_rows, stdout, action="SELL", marker=TICKET_SELL_STDOUT_MARKER
    )
    assert parity["parity"] is True
    assert parity["orders"] == 1 and parity["printed_lines"] == 1


# ---------------------------------------------------------------------------
# 7. source contract: where the pass runs and what it may not break
# ---------------------------------------------------------------------------


def _source():
    with open(
        os.path.join(_REPO_ROOT, "scripts", "run_live_strategy.py"), "r", encoding="utf-8"
    ) as f:
        return f.read()


def test_force_pass_runs_after_lifecycle_and_before_the_budget_passes():
    source = _source()
    force_call = source.index("vol_force_report = apply_vol_ceiling_force_exits(")
    normalize_call = source.index("weight_budget = normalize_model_target_weights(")
    assert force_call < normalize_call
    # `ceiling_exceeded_by` is measured AFTER the forced pass and keeps the pre-force value
    exceeded = source.index('vol_overlay["ceiling_exceeded_by"] = round(')
    assert exceeded > force_call
    assert 'vol_overlay["ceiling_exceeded_by_before_force"] = round(' in source
    # with the switch OFF no forced-exit key is written at all (1.3.0 byte-identical)
    assert 'if vol_force_report.get("skipped_reason") != "disabled":' in source
    # attribution: a forced de-risk gets its own bucket, never "the lifecycle ate it"
    assert '_code = "vol_ceiling_force_exit"' in source
