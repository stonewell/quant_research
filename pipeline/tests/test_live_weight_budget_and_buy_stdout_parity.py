"""`D4` + `D6` -- delivered weights must be recorded, and a BUY must be printed once.

FORENSIC CONTEXT (archive `runs/2026-11-09/204956`, reproduced offline here)
---------------------------------------------------------------------------
**D4 (lost economic intent).**  `orders[0]` (600660.SH, BUY) said `target_weight = 0.2`
with `shares = 200` and `detail_reason.lot_rounding = {"target_shares_raw": 299.04,
"shares": 200, "lot_size": 100}`.  With NAV 197,903.35 and price 56.62 the 600 delivered
shares are worth 17.16629% of NAV (`mv_cap.market_value_weight = 0.171663`) -- the row
stated 20%.  Nothing in the ticket or in
`account_state.lifecycle_states["600660.SH"]` (`current_weight = 0.2`,
`broker_weight = 0.11444`) recorded the 17.17% that the book would actually hold, so the
next round's inertia / min-entry / single-name-cap gates all compared against a weight the
book can never have.

The whole ticket summed to `Σ target_weight = 1.028337` (BIL placeholder row included)
against `Σ current_weight = 1.000000` -- i.e. it claimed 102.8% of the account.  And
601919.SH delivered 21.1865% against a 20% cap with the breach recorded ONLY inside
`detail_reason.mv_cap` (`enforced: false`).

**D6 (duplicated order line).**  `summary.json::deploy_stdout` printed
`🟢 BUY 600660.SH ... 200 股 (~¥11,324.00)` TWICE while `orders` carried one BUY: the
PHASE 2 loop had a second `print()` that existed only to attach the sub-lot warning.  An
operator copying the stdout would have doubled the position.

WHAT IS PINNED HERE
-------------------
* `normalize_model_target_weights` scales a >100% model book proportionally (factor
  recorded) and re-quantizes the BUY legs in whole lots;
* `reconcile_delivered_weight_budget` stamps `target_weight_effective`
  (`target_shares * price / NAV`), makes `Σ effective <= 1 + 1e-9` by shaving whole lots
  off the SMALLEST delivered positions, and never invents a SELL;
* a cash-proxy row's effective weight is the NAV residual (a placeholder row carries no
  share quantity, so that is the only honest number it can state);
* `collect_mv_cap_breaches` lifts every breach to the ticket top level;
* `verify_ticket_stdout_order_parity` catches the archived double-print shape and the
  PHASE 2 renderer emits exactly one `🟢 BUY` line per order.

100% offline: plain dicts, no market data, no network, no filesystem.
"""

import ast
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.run_live_strategy import (  # noqa: E402
    TICKET_BUY_STDOUT_MARKER,
    TICKET_SELL_STDOUT_MARKER,
    apply_market_value_cap,
    collect_mv_cap_breaches,
    merge_weight_budget_summaries,
    normalize_model_target_weights,
    reconcile_delivered_weight_budget,
    verify_ticket_stdout_order_parity,
)

_LIVE_RUNNER = os.path.join(_REPO_ROOT, "scripts", "run_live_strategy.py")

#: The archived round's real numbers (`2026-11-09/204956`), reconstructed exactly:
#: 400 held shares at 56.62 are 11.444% of NAV -> NAV = 400 * 56.62 / 0.11444.
NAV = 400 * 56.62 / 0.11444          # 197,902.20...
PRICE_600660 = 56.62
CASH = "BIL"


def _row(
    symbol,
    *,
    action="BUY",
    price=10.0,
    current_shares=0,
    target_shares=0,
    delta_shares=0,
    target_weight=0.0,
    current_weight=0.0,
):
    """One ticket row in the shape `run_live_strategy.main` builds before the budget pass."""
    return {
        "symbol": symbol,
        "name": symbol,
        "action": action,
        "price": price,
        "current_shares": current_shares,
        "target_shares": target_shares,
        "delta_shares": delta_shares,
        "shares": abs(delta_shares),
        "trade_value": delta_shares * price,
        "trade_value_rmb": delta_shares * price,
        "current_weight": current_weight,
        "target_weight": target_weight,
        "target_weight_model": target_weight,
        "target_weight_effective": None,
        "detail_reason": {"lot_rounding": {"shares": abs(delta_shares), "lot_size": 100}},
    }


def _cash_row(target_weight=0.0):
    row = _row(CASH, action="SWEEP_CASH", price=1.0)
    row["target_weight"] = target_weight
    row["target_weight_model"] = target_weight
    return row


# ---------------------------------------------------------------------------
# 1. `target_weight_effective` -- the number the ticket actually delivers
# ---------------------------------------------------------------------------


def test_lot_rounding_drift_is_recorded_as_target_weight_effective():
    """600660.SH: stated 20%, delivered 17.1663% (600 shares x 56.62 / NAV)."""
    row = _row(
        "600660.SH",
        price=PRICE_600660,
        current_shares=400,
        target_shares=600,
        delta_shares=200,
        target_weight=0.20,
        current_weight=0.11444,
    )
    rows = [row, _cash_row()]

    summary = reconcile_delivered_weight_budget(rows, portfolio_value=NAV, cash_proxy=CASH)

    assert row["target_weight_effective"] == pytest.approx(0.17166, abs=1e-5)
    # The pre-D4 field keeps its meaning (downstream readers depend on it).
    assert row["target_weight"] == pytest.approx(0.20)
    assert row["target_weight_model"] == pytest.approx(0.20)
    # The gap is now machine-readable instead of something the operator must compute.
    assert row["target_weight_effective"] < row["target_weight"] - 0.02
    assert summary["rounding_rebalanced"] is False
    assert summary["target_weight_sum"] <= 1.0 + 1e-9
    assert summary["budget_ok"] is True


def test_cash_proxy_row_carries_the_nav_residual():
    """A cash placeholder has `target_shares == 0`, so its delivered weight is the residual."""
    equity = [
        _row("AAA", price=10.0, target_shares=1000, delta_shares=1000, target_weight=0.10),
        _row("BBB", price=20.0, target_shares=2000, delta_shares=2000, target_weight=0.30),
    ]
    cash = _cash_row(target_weight=0.60)
    summary = reconcile_delivered_weight_budget(
        equity + [cash], portfolio_value=100_000.0, cash_proxy=CASH
    )

    assert summary["equity_weight_sum"] == pytest.approx(0.50)
    assert cash["target_weight_effective"] == pytest.approx(0.50)
    assert summary["target_weight_sum"] == pytest.approx(1.0)
    # The legacy cash weight is NOT rewritten: only the new effective field is.
    assert cash["target_weight"] == pytest.approx(0.60)


# ---------------------------------------------------------------------------
# 2. Σ model weights > 1 -> proportional normalization, never a 102.8% ticket
# ---------------------------------------------------------------------------


def test_model_weights_above_one_are_normalized_proportionally():
    rows = [
        _row("600660.SH", price=PRICE_600660, current_shares=400, target_shares=1000,
             delta_shares=600, target_weight=0.35, current_weight=0.11444),
        _row("601919.SH", price=14.0, target_shares=3000, delta_shares=3000, target_weight=0.35),
        _row("600900.SH", price=25.0, target_shares=2000, delta_shares=2000, target_weight=0.328337),
        _cash_row(target_weight=0.0),
    ]
    model_sum = sum(r["target_weight"] for r in rows)
    assert model_sum == pytest.approx(1.028337)

    summary = normalize_model_target_weights(rows, portfolio_value=NAV, cash_proxy=CASH)

    assert summary["weights_normalized"] is True
    assert summary["weight_normalization_factor"] == pytest.approx(1 / 1.028337, rel=1e-9)
    assert summary["target_weight_sum_model"] == pytest.approx(1.028337)
    # Every recorded target was scaled, and the BUY quantities re-quantized in whole lots.
    assert sum(r["target_weight"] for r in rows) == pytest.approx(1.0, abs=1e-9)
    for r in rows:
        if r["symbol"] == CASH:
            continue
        assert r["target_shares"] % 100 == 0
        assert r["delta_shares"] == r["target_shares"] - r["current_shares"]
        assert r["shares"] == abs(r["delta_shares"])

    delivered = reconcile_delivered_weight_budget(rows, portfolio_value=NAV, cash_proxy=CASH)
    assert delivered["target_weight_sum"] <= 1.0 + 1e-9
    assert delivered["budget_ok"] is True


def test_normalization_is_a_no_op_at_or_below_one():
    rows = [
        _row("AAA", price=10.0, target_shares=5000, delta_shares=5000, target_weight=0.50),
        _row("BBB", price=10.0, target_shares=5000, delta_shares=5000, target_weight=0.50),
    ]
    summary = normalize_model_target_weights(rows, portfolio_value=100_000.0, cash_proxy=CASH)
    assert summary["weights_normalized"] is False
    assert summary["weight_normalization_factor"] == 1.0
    assert [r["target_shares"] for r in rows] == [5000, 5000]


def test_normalization_never_turns_a_buy_into_a_sell():
    """A scaled target that falls under what is already held becomes a zero-quantity record."""
    rows = [
        _row("AAA", price=10.0, current_shares=9_800, target_shares=10_000, delta_shares=200,
             target_weight=0.50, current_weight=0.49),
        _row("BBB", price=10.0, target_shares=5_500, delta_shares=5_500, target_weight=0.55),
    ]
    summary = normalize_model_target_weights(rows, portfolio_value=100_000.0, cash_proxy=CASH)
    assert summary["weights_normalized"] is True
    assert summary["weight_normalization_partial"] is True
    assert rows[0]["delta_shares"] == 0
    assert rows[0]["trade_value"] == 0.0
    assert rows[0]["target_shares"] == 9_800


# ---------------------------------------------------------------------------
# 3. Rounding amplification -> shave whole lots off the SMALLEST position
# ---------------------------------------------------------------------------


def test_over_budget_book_is_rebalanced_by_shaving_the_smallest_position():
    """`Σ effective > 1` after rounding: shave the smallest BUY, never a HOLD."""
    rows = [
        # The largest position is a HOLD (its drift is already in the book) -- it must not
        # be trimmed here, this guard is not an exit rule.
        _row("BIG", action="HOLD", price=10.0, current_shares=5_000, target_shares=5_000,
             delta_shares=0, target_weight=0.50, current_weight=0.50),
        _row("SMALL", price=10.0, target_shares=5_200, delta_shares=5_200, target_weight=0.52),
        _cash_row(),
    ]
    summary = reconcile_delivered_weight_budget(
        rows, portfolio_value=100_000.0, cash_proxy=CASH, lot_size_fn=lambda _s: 100
    )

    assert summary["rounding_rebalanced"] is True
    assert summary["shaved_rows"] == ["SMALL"]
    assert summary["target_weight_sum"] <= 1.0 + 1e-9
    assert summary["budget_ok"] is True
    # 0.50 held + 0.50 delivered = 100%; the shave is a whole lot off the BUY only.
    assert rows[1]["target_shares"] == 5_000
    assert rows[1]["delta_shares"] == 5_000
    assert rows[1]["target_weight_effective"] == pytest.approx(0.50)
    assert rows[0]["delta_shares"] == 0
    assert rows[0]["detail_reason"].get("weight_budget", {}).get("shaved_shares") is None
    assert rows[1]["detail_reason"]["weight_budget"]["shaved_shares"] == 200


def test_unfixable_book_reports_budget_not_ok():
    """No BUY delta left to shave (pure drift): say so instead of pretending it fits."""
    rows = [
        _row("AAA", action="HOLD", price=10.0, current_shares=6_000, target_shares=6_000,
             target_weight=0.60, current_weight=0.60),
        _row("BBB", action="HOLD", price=10.0, current_shares=5_000, target_shares=5_000,
             target_weight=0.50, current_weight=0.50),
    ]
    summary = reconcile_delivered_weight_budget(rows, portfolio_value=100_000.0, cash_proxy=CASH)
    assert summary["rounding_rebalanced"] is False
    assert summary["budget_ok"] is False
    assert summary["target_weight_sum"] > 1.0


def test_merge_keeps_the_first_pass_verdicts():
    base = {
        "weights_normalized": True,
        "weight_normalization_factor": 0.9724,
        "rounding_rebalanced": True,
        "weight_normalization_partial": False,
        "shaved_rows": ["SMALL"],
        "target_weight_sum": 0.999,
    }
    refresh = {
        "weights_normalized": False,
        "weight_normalization_factor": 1.0,
        "rounding_rebalanced": False,
        "weight_normalization_partial": False,
        "shaved_rows": [],
        "target_weight_sum": 0.98,
        "budget_ok": True,
    }
    merged = merge_weight_budget_summaries(base, refresh)
    assert merged["weights_normalized"] is True
    assert merged["rounding_rebalanced"] is True
    assert merged["shaved_rows"] == ["SMALL"]
    assert merged["weight_normalization_factor"] == pytest.approx(0.9724)
    # The refresh pass owns the FINAL sum (the cap may have clamped a quantity).
    assert merged["target_weight_sum"] == pytest.approx(0.98)


# ---------------------------------------------------------------------------
# 4. Delivered single-name cap breach must leave `detail_reason`
# ---------------------------------------------------------------------------


def test_mv_cap_breach_is_lifted_to_the_ticket_top_level():
    """601919.SH: 21.1865% delivered against a 20% cap, `enforced: false` (warn-only)."""
    row = _row("601919.SH", price=14.0, target_shares=3000, delta_shares=3000, target_weight=0.20)
    apply_market_value_cap(
        row, portfolio_value=NAV, max_weight=0.20, lot_size=100, enforce=False
    )
    assert row["detail_reason"]["mv_cap"]["breached"] is True

    breaches = collect_mv_cap_breaches([row, _cash_row()])
    assert len(breaches) == 1
    assert breaches[0]["symbol"] == "601919.SH"
    assert breaches[0]["market_value_weight"] == pytest.approx(0.21, abs=0.01)
    assert breaches[0]["cap"] == pytest.approx(0.20)
    assert breaches[0]["enforced"] is False


def test_no_breach_yields_an_empty_list():
    row = _row("AAA", price=10.0, target_shares=1_000, delta_shares=1_000, target_weight=0.10)
    apply_market_value_cap(row, portfolio_value=100_000.0, max_weight=0.20, lot_size=100)
    assert collect_mv_cap_breaches([row]) == []


# ---------------------------------------------------------------------------
# 5. `D6` -- one printed order line per order
# ---------------------------------------------------------------------------

_ARCHIVED_DUPLICATED_STDOUT = """
[PHASE 2: EXECUTE BUYS SECOND (第二阶段: 买入建仓/加仓资金配置)]
  🟢 BUY  600660.SH (福耀玻璃)     | 现价:   56.62 | 仓位: 11.4% -> 目标: 20.0% (+8.6%) | 买入:    200 股 (~¥11,324.00) | 建议限价: 56.73
  🟢 BUY  600660.SH (福耀玻璃)     | 现价:   56.62 | 仓位: 11.4% -> 目标: 20.0% (+8.6%) | 买入:    200 股 (~¥11,324.00)
"""

_SINGLE_LINE_STDOUT = """
[PHASE 2: EXECUTE BUYS SECOND (第二阶段: 买入建仓/加仓资金配置)]
  🟢 BUY  600660.SH (福耀玻璃)     | 现价:   56.62 | 仓位: 11.4% -> 目标: 20.0% (+8.6%) | 买入:    200 股 (~¥11,324.00) | 建议限价: 56.73
"""


def test_parity_check_catches_the_archived_duplicate_buy_line(capsys):
    orders = [
        {"symbol": "600660.SH", "action": "BUY", "shares": 200, "target_shares": 600},
        _cash_row(),
    ]
    result = verify_ticket_stdout_order_parity(orders, _ARCHIVED_DUPLICATED_STDOUT, action="BUY")
    assert result["orders"] == 1
    assert result["printed_lines"] == 2
    assert result["parity"] is False
    assert "[WARN/TICKET STDOUT PARITY]" in capsys.readouterr().out


def test_parity_check_passes_on_one_line_per_order():
    orders = [
        {"symbol": "600660.SH", "action": "BUY", "shares": 200, "target_shares": 600},
        _cash_row(),
    ]
    result = verify_ticket_stdout_order_parity(orders, _SINGLE_LINE_STDOUT, action="BUY")
    assert (result["orders"], result["printed_lines"], result["parity"]) == (1, 1, True)


def test_parity_check_counts_each_action_with_its_own_marker():
    orders = [
        {"symbol": "AAA", "action": "SELL", "shares": 100},
        {"symbol": "BBB", "action": "BUY", "shares": 100},
        _cash_row(),
    ]
    stdout = (
        "  🔴 SELL AAA | 卖出: 100 股\n"
        "  🟢 BUY  BBB | 买入: 100 股\n"
    )
    buy = verify_ticket_stdout_order_parity(orders, stdout, action="BUY")
    sell = verify_ticket_stdout_order_parity(
        orders, stdout, action="SELL", marker=TICKET_SELL_STDOUT_MARKER
    )
    assert (buy["orders"], buy["printed_lines"], buy["parity"]) == (1, 1, True)
    assert (sell["orders"], sell["printed_lines"], sell["parity"]) == (1, 1, True)


def _ticket_line_printer_calls_for_buys(source: str) -> int:
    """How many statements render a `🟢 BUY` line (the D6 regression counter)."""
    tree = ast.parse(source)
    hits = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in {"print", "print_ticket_order_line"}:
            continue
        if TICKET_BUY_STDOUT_MARKER in (ast.get_source_segment(source, node) or ""):
            hits += 1
    return hits


def test_phase2_renders_exactly_one_buy_line_per_row():
    """The archived defect was a SECOND `print()` in the same loop; the AST must see one."""
    with open(_LIVE_RUNNER, "r", encoding="utf-8") as f:
        source = f.read()
    assert _ticket_line_printer_calls_for_buys(source) == 1


# ---------------------------------------------------------------------------
# 6. End-to-end (offline, synthetic): the fields actually land in the artifacts
# ---------------------------------------------------------------------------


def test_main_writes_the_delivered_weights_and_the_post_holdings_sidecar(tmp_path, monkeypatch, capsys):
    """One real `main()` run against an archive-shaped output dir, 100% synthetic data.

    Pins what a reader of the archive sees: `target_weight_effective` on every order row,
    a book-level `Σ effective <= 1`, the cap breaches at the top level, ONE printed line per
    BUY, and `post_holdings.meta.json` next to the book.
    """
    import json
    import sys as _sys

    from scripts.run_live_strategy import main

    out_dir = os.path.join(str(tmp_path), "archive", "equal_weight", "univ")
    os.makedirs(out_dir, exist_ok=True)
    universe_file = os.path.join(str(tmp_path), "univ.txt")
    with open(universe_file, "w", encoding="utf-8") as f:
        f.write("600000.SH\n600036.SH\n")
    holdings_file = os.path.join(str(tmp_path), "holdings.json")
    with open(holdings_file, "w", encoding="utf-8") as f:
        json.dump({"600000.SH": 1000}, f)

    monkeypatch.setattr(
        _sys,
        "argv",
        [
            "run_live_strategy.py",
            "--strategy", "equal_weight",
            "--universe-file", universe_file,
            "--current-holdings-file", holdings_file,
            "--account-state-file", os.path.join(out_dir, "account_state.json"),
            "--output-dir", out_dir,
            "--as-of-date", "2024-05-10",
            "--start-date", "2024-01-01",
            "--data-provider", "synthetic",
            "--portfolio-value", "100000",
        ],
    )
    main()
    stdout = capsys.readouterr().out

    with open(os.path.join(out_dir, "live_trading_ticket.json"), "r", encoding="utf-8") as f:
        ticket = json.load(f)
    orders = ticket["orders"]

    assert "weights_normalized" in ticket
    assert isinstance(ticket["mv_cap_breaches"], list)
    # The synthetic book delivers ~47% of NAV in one name against a 20% cap: the breach is
    # listed at the TOP level and was NOT enforced (warn-only default).
    breached = {b["symbol"] for b in ticket["mv_cap_breaches"]}
    assert breached == {"600000.SH"}
    assert all(b["enforced"] is False for b in ticket["mv_cap_breaches"])
    assert "[WARN/MV CAP]" in stdout

    for row in orders:
        if row["symbol"] == "BIL":
            continue
        assert row["target_weight_effective"] is not None
        assert row["target_weight_model"] is not None
    assert ticket["weight_budget_ok"] is True
    assert 0.0 <= ticket["target_weight_sum"] <= 1.0 + 1e-9
    assert sum(
        r["target_weight_effective"] for r in orders
    ) == pytest.approx(ticket["target_weight_sum"], abs=1e-7)

    # One printed order line per order, verified by the archived machine check.  The D4
    # delivered weight of the trimmed row is ALSO visible on the rendered line, so the
    # operator never reads "目标 50.0%" as the delivered 47.19%.
    assert ticket["ticket_stdout_parity"]["BUY"]["parity"] is True
    assert ticket["ticket_stdout_parity"]["SELL"]["parity"] is True
    assert ticket["ticket_stdout_parity"]["SELL"]["printed_lines"] == 1
    n_buy_orders = len([o for o in orders if o["action"] == "BUY"])
    n_sell_orders = len([o for o in orders if o["action"] == "SELL"])
    assert stdout.count(TICKET_BUY_STDOUT_MARKER) == n_buy_orders
    assert stdout.count(TICKET_SELL_STDOUT_MARKER) == n_sell_orders
    assert "可交付" in stdout

    # (D4) `account_state` persists the DELIVERED weight (not the model's wish) next to the
    # model's original target, which is exactly what the next round's gates must compare.
    with open(os.path.join(out_dir, "account_state.json"), "r", encoding="utf-8") as f:
        state = json.load(f)
    trimmed = [o for o in orders if o["symbol"] == "600000.SH"][0]
    st = state["lifecycle_states"]["600000.SH"]
    assert st["current_weight"] == pytest.approx(trimmed["target_weight_effective"], abs=1e-8)
    assert st["current_weight"] != pytest.approx(trimmed["target_weight"], abs=1e-4)
    assert st["target_weight_model"] == pytest.approx(trimmed["target_weight_model"], abs=1e-8)

    # (D5) The sidecar makes "empty book" distinguishable from "no book".
    with open(os.path.join(out_dir, "post_holdings.meta.json"), "r", encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["complete"] is True
    assert meta["as_of"] == "2024-05-10"
    assert meta["source"] in {"ticket", "replay"}
    with open(os.path.join(out_dir, "post_holdings.json"), "r", encoding="utf-8") as f:
        book = json.load(f)
    assert meta["n_symbols"] == len([s for s, q in book.items() if int(q) > 0])
