"""The A-share price band has TWO anchors, and they are not interchangeable.

THE DEFECT UNDER TEST (2026-11-17)
==================================
`scripts/run_live_strategy.py` builds two board prices per symbol and used ONE of them
for TWO different questions:

    prev_close = Close[-2]                      # the `T-1` close
    p_limit_up   = prev_close * (1 + limit_pct)  # the `T`   board
    p_limit_down = prev_close * (1 - limit_pct)

* "was `T` limit-locked?" (`LIMIT_UP_LOCKED` / `LIMIT_DOWN_ILLIQUID_RISK`, ticket
  `execution_warning` + `blocking_reason`) IS a statement about session `T`, so the
  `T-1`-anchored board is the CORRECT anchor there. Unchanged.
* the SUGGESTED limit price is not a `T` fact at all: this ticket declares
  `execution_session=as_of+1` / `price_reference=next_open`, i.e. **it fills at the
  `T+1` OPEN**, and the exchange band for `T+1` is anchored on the **`T` close**
  (`price`, the as-of close `latest_close()` returned -- the frame's last bar IS `T`).
  Clamping with the previous session's board is therefore a stale constraint. It can
  only ever TIGHTEN (never emit an out-of-band price), which is exactly why nothing
  alarmed:
    * buy side -- `min(p_limit_up, close x 1.002)`: after a limit-sized up move the
      stale ceiling sits a tick or two ABOVE the close, so the intended 0.2%
      marketable buffer is shaved off and the order is pinned to the previous
      session's board;
    * sell side -- `max(p_limit_down, close x 0.998)`: after a limit-sized down move
      the stale floor sits at/above the close, so the sell limit is LIFTED to a price
      the market may never print again.

WHAT THIS FILE PINS (all 100% offline: synthetic frames, frozen clock, `tmp_path`)
=================================================================================
1. `test_buy_limit_uses_the_next_session_board` -- +9.85% `T`: the buy limit is the
   as-of close x 1.002 (`11.007`), NOT the stale `T-1` board (`11.000`), and it stays
   inside the true `T+1` band.
2. `test_sell_limit_uses_the_next_session_board` -- -9.9% `T`: the sell limit is the
   as-of close x 0.998 (`8.992`), not the lifted stale floor (`9.000`), and it stays
   inside the true `T+1` band.
3. `test_risk_vetoes_still_run_on_the_session_t_board` -- the two risk vetoes are
   bit-identical to before the change: they still fire off the `T-1`-anchored board.
   Both cases are constructed so that re-pointing the veto at the `T+1` board would
   make it STOP firing (11.000 vs 12.100, 9.000 vs 8.100), so the guard cannot rot.
4. `test_detail_reason_carries_the_next_session_board_not_the_session_t_board` --
   the ticket's diagnostic `detail_reason["limit_board"]` publishes the `T+1` board
   (`*_next`, anchored on the `T` close) NEXT TO the `T` board (`*_session_t`), and the
   same two divergent cases prove the `_next` key is not a relabelled `_session_t`
   price (12.100 != 11.000, 8.100 != 9.000).

WORDING (2026-11-18, cosmetic + diagnostic only -- no decision moved)
=====================================================================
The `LIMIT_DOWN_ILLIQUID_RISK` reason used to say "跌停封死/流动性枯竭" ("sealed
limit-down / liquidity exhausted"), which reads as a `T+1` market verdict although the
label is set from a `T`-session fact and never blocks a sell. An earlier revision of
this file also called the up-side veto "涨停封板无法买入". Both texts now state only the
`T`-session observation plus what the label does; the enums, actions, share counts and
the veto condition are untouched.

The harness is one REAL `run_live_strategy.main()` per case; only the daily-bar loader
is replaced, so every field asserted here is the field the ticket actually ships.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common import cli_utils  # noqa: E402

#: Beijing 15:30 on the as-of session's own close+grace window -> `as_of` = the pinned
#: date and `run_mode=live` (no `REPLAY MODE` path, no intraday refusal).
_BEIJING_1530 = dt.datetime(2026, 10, 23, 7, 30, tzinfo=dt.timezone.utc)

_AS_OF = "2026-10-23"
#: The suspension ended on `_AS_OF`: the previous DAILY BAR is five weeks old. That is
#: the honest shape of `Close[-2]` whenever a name resumes after a halt -- and it is
#: also why the `T-1` board and the `T+1` board can be far apart.
_PRE_SUSPENSION_END = "2026-09-18"

#: 51xxxx => ETF: `decimals=3`, `tick_tol=0.001`, `limit_pct=0.10`. The three decimals
#: matter -- on a 2-decimal equity the one-tick lock tolerance swallows the whole window
#: in which the stale clamp could bind, so the defect is only observable here.
_ETF = "510300.SH"
#: A second equity so `equal_weight` gives 50/50 and a held 72% is genuinely a SELL.
_SECOND = "601857.SH"
_CASH = "BIL"
_PORTFOLIO_VALUE = 100_000.0


@pytest.fixture(autouse=True)
def _frozen_market_clock(monkeypatch):
    """Pin the one wall-clock read; `main()` otherwise refuses an out-of-window run."""
    real_market_now = cli_utils.market_now

    def _frozen(market, now=None, holidays=None, publication_grace_minutes=None):
        return real_market_now(
            market,
            now=now if now is not None else _BEIJING_1530,
            holidays=holidays,
            publication_grace_minutes=publication_grace_minutes,
        )

    monkeypatch.setattr(cli_utils, "market_now", _frozen)


def _frame(prev_close: float, as_of_close: float, bars: int = 39) -> pd.DataFrame:
    """Daily bars where `Close[-2] == prev_close` (`T-1`) and `Close[-1] == as_of_close`."""
    index = list(pd.bdate_range(end=_PRE_SUSPENSION_END, periods=bars)) + [pd.Timestamp(_AS_OF)]
    closes = [float(prev_close)] * bars + [float(as_of_close)]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [1_000_000.0] * len(closes),
        },
        index=pd.DatetimeIndex(index),
    )


def _ticket(
    tmp_path,
    monkeypatch,
    *,
    prev_close: float,
    as_of_close: float,
    holdings: dict,
) -> dict:
    """One REAL `main()` run on synthetic frames; returns the written ticket."""
    from scripts import run_live_strategy as rls

    frames = {_ETF: _frame(prev_close, as_of_close), _SECOND: _frame(10.0, 10.0)}

    def _fake_load_universe(args, symbols, start, end, **kwargs):
        return ({s: frames[s] for s in symbols if s in frames}, 1)

    monkeypatch.setattr(rls, "load_live_universe", _fake_load_universe)

    universe_file = os.path.join(str(tmp_path), "universe.txt")
    with open(universe_file, "w", encoding="utf-8") as f:
        f.write("\n".join([_ETF, _SECOND, _CASH]) + "\n")
    holdings_file = os.path.join(str(tmp_path), "holdings.json")
    with open(holdings_file, "w", encoding="utf-8") as f:
        json.dump(holdings, f)

    out_dir = os.path.join(str(tmp_path), "out")
    os.makedirs(out_dir, exist_ok=True)
    rls.main(
        [
            "--strategy", "equal_weight",
            "--universe-file", universe_file,
            "--current-holdings-file", holdings_file,
            "--output-dir", out_dir,
            "--start-date", "2026-01-01",
            "--data-provider", "synthetic",
            "--portfolio-value", str(int(_PORTFOLIO_VALUE)),
            "--no-cache",
            "--as-of-date", _AS_OF,
        ]
    )
    with open(os.path.join(out_dir, "live_trading_ticket.json"), encoding="utf-8") as f:
        return json.load(f)


def _order(ticket: dict, symbol: str) -> dict:
    for row in ticket["orders"]:
        if row["symbol"] == symbol:
            return row
    raise AssertionError(f"{symbol} missing from ticket: {[r['symbol'] for r in ticket['orders']]}")


# ---------------------------------------------------------------------------
# 1. +9.85% session: the buy limit must be anchored on the `T` close
# ---------------------------------------------------------------------------
def test_buy_limit_uses_the_next_session_board(tmp_path, monkeypatch):
    """`T` = 10.985 vs `T-1` = 10.000. The buy fills at the `T+1` open.

    Old (stale) clamp: `min(round(10.000*1.10, 3), round(10.985*1.002, 3))` =
    `min(11.000, 11.007)` = **11.000** -- the previous session's ceiling, i.e. the
    marketable 0.2% buffer silently shaved off an entry that has not happened yet.
    New (correct) anchor: `min(round(10.985*1.10, 3), 11.007)` = **11.007**.
    """
    ticket = _ticket(
        tmp_path,
        monkeypatch,
        prev_close=10.000,
        as_of_close=10.985,
        holdings={_ETF: 100},
    )
    row = _order(ticket, _ETF)

    price = row["price"]
    assert price == pytest.approx(10.985)
    # A real BUY: the board veto is NOT what this case exercises (see the lock tests).
    assert row["action"] == "BUY"
    assert row["execution_warning"] is None

    suggested = row["suggested_limit_price"]
    # The anchor moved: the ticket is the as-of close x 1.002, not the `T-1` board.
    assert suggested == pytest.approx(11.007), "limit price is not `close x 1.002`"
    assert suggested > round(10.000 * 1.10, 3), (
        "the stale `T-1`-anchored ceiling (11.000) is still clamping the buy limit"
    )
    # Fillable and never above the true `T+1` band (anchored on the `T` close).
    assert suggested >= price
    assert suggested <= price * (1.0 + 0.10)
    assert row["passive_support_limit_price"] == pytest.approx(11.007)


# ---------------------------------------------------------------------------
# 2. -9.9% session: the sell limit must be anchored on the `T` close
# ---------------------------------------------------------------------------
def test_sell_limit_uses_the_next_session_board(tmp_path, monkeypatch):
    """`T` = 9.010 vs `T-1` = 10.000, holding 72% against a 50% target -> SELL.

    Old (stale) clamp: `max(round(10.000*0.90, 3), round(9.010*0.998, 3))` =
    `max(9.000, 8.992)` = **9.000** -- a sell limit lifted to the previous session's
    floor, i.e. a price the market may never print again.
    New (correct) anchor: `max(round(9.010*0.90, 3), 8.992)` = **8.992**.
    """
    ticket = _ticket(
        tmp_path,
        monkeypatch,
        prev_close=10.000,
        as_of_close=9.010,
        holdings={_ETF: 8000},
    )
    row = _order(ticket, _ETF)

    price = row["price"]
    assert price == pytest.approx(9.010)
    assert row["action"] == "SELL"
    assert row["execution_warning"] is None

    suggested = row["suggested_limit_price"]
    assert suggested == pytest.approx(8.992), "limit price is not `close x 0.998`"
    assert suggested < round(10.000 * 0.90, 3), (
        "the stale `T-1`-anchored floor (9.000) is still lifting the sell limit"
    )
    # Never above the close, and never below the true `T+1` band.
    assert suggested <= price
    assert suggested >= price * (1.0 - 0.10)
    assert row["passive_support_limit_price"] == pytest.approx(8.992)


# ---------------------------------------------------------------------------
# 3. The two session-`T` risk vetoes are BIT-IDENTICAL
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "side, prev_close, as_of_close, holdings, expected_warning, expected_action, expected_blocking",
    [
        # `price == prev x 1.10` exactly: the `T-1`-anchored board (11.000) fires.
        # Re-pointed at the `T+1` board it would need `price >= 12.100 - 0.001`: no fire.
        ("limit_up", 10.000, 11.000, {_ETF: 100}, "LIMIT_UP_LOCKED", "HOLD_FILTERED", "LIMIT_UP_LOCKED"),
        # `price == prev x 0.90` exactly: the `T-1`-anchored floor (9.000 + 1 tick).
        # Re-pointed at the `T+1` board it would need `price <= 8.100 + 0.001`: no fire.
        ("limit_down", 10.000, 9.000, {_ETF: 8000}, "LIMIT_DOWN_ILLIQUID_RISK", "SELL", "NONE"),
    ],
)
def test_risk_vetoes_still_run_on_the_session_t_board(
    tmp_path,
    monkeypatch,
    side,
    prev_close,
    as_of_close,
    holdings,
    expected_warning,
    expected_action,
    expected_blocking,
):
    """`LIMIT_UP_LOCKED` / `LIMIT_DOWN_ILLIQUID_RISK` answer "was `T` locked?".

    Both decisions were produced by the `T-1`-anchored board before the band refactor
    and must still be: a `T+1` order may not be emitted into a board that `T` itself
    closed on. The assertion set is the entire published verdict of the veto (action,
    warning, blocking reason, share counts), so any drift is visible.
    """
    ticket = _ticket(
        tmp_path,
        monkeypatch,
        prev_close=prev_close,
        as_of_close=as_of_close,
        holdings=holdings,
    )
    row = _order(ticket, _ETF)

    assert row["action"] == expected_action
    assert row["execution_warning"] == expected_warning
    assert row["detail_reason"]["blocking_reason"] == expected_blocking

    # (2026-11-18 wording change) The old texts asserted a `T+1` market fact from a
    # `T`-session observation:
    #   * down side: `" [跌停封死/流动性枯竭警示 LIMIT_DOWN_ILLIQUID_RISK]"` -- the word
    #     "封死" ("sealed") claimed the sell cannot execute, which this branch never
    #     measured and which is false for 18/19 of the measured cases.
    #   * up side:   `"涨停封板无法买入 (LIMIT_UP_LOCKED)"` -- same defect class.
    # Only the prose moved; the enum token stays a substring (other suites assert it).
    if side == "limit_up":
        assert row["reason"] == (
            "T日收盘触及涨停板，本行按规则放弃本次买入（不追板，非断言市场无法成交）"
            " (LIMIT_UP_LOCKED)"
        )
        assert "无法买入" not in row["reason"]
    else:
        assert "T日收盘触及跌停板" in row["reason"]
        assert "不是禁止卖出" in row["reason"]
        assert "LIMIT_DOWN_ILLIQUID_RISK" in row["reason"]
        assert "跌停封死" not in row["reason"]

    if side == "limit_up":
        # The veto froze the entry: nothing bought, weight kept.
        assert row["delta_shares"] == 0
        assert row["shares"] == 0
        assert row["target_shares"] == row["current_shares"] == holdings[_ETF]
        assert row["trade_value_rmb"] == pytest.approx(0.0)
        # An unfilled row carries the as-of close, never a stale board.
        assert row["suggested_limit_price"] == pytest.approx(row["price"])
    else:
        # The veto is a WARNING on this path, not a block: the sell still ships.
        assert row["delta_shares"] < 0
        # ...and its limit price is still the `T`-close anchor, not the stale floor.
        assert row["suggested_limit_price"] == pytest.approx(8.982)
        assert row["suggested_limit_price"] < round(prev_close * 0.90, 3)


# ---------------------------------------------------------------------------
# 4. The diagnostic field carries the `T+1` board, next to the `T` board
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "side, prev_close, as_of_close, holdings, next_key, expected_next, session_key, expected_session_t",
    [
        # Up seal: `T` board = round(10.000*1.10) = 11.000 (anchored on `T-1`);
        # `T+1` board = round(11.000*1.10) = 12.100 (anchored on the `T` close).
        ("limit_up", 10.000, 11.000, {_ETF: 100}, "limit_up_next", 12.100, "limit_up_session_t", 11.000),
        # Down seal: `T` board = 9.000; `T+1` board = round(9.000*0.90) = 8.100.
        ("limit_down", 10.000, 9.000, {_ETF: 8000}, "limit_down_next", 8.100, "limit_down_session_t", 9.000),
    ],
)
def test_detail_reason_carries_the_next_session_board_not_the_session_t_board(
    tmp_path,
    monkeypatch,
    side,
    prev_close,
    as_of_close,
    holdings,
    next_key,
    expected_next,
    session_key,
    expected_session_t,
):
    """`detail_reason["limit_board"]` publishes BOTH boards, and `_next` is the `T+1` one.

    (2026-11-18) Motivation: the veto reason used to be the ONLY place a reader could
    learn the board, and it named the `T` session in prose that sounded like a `T+1`
    verdict. The diagnostic now carries the price an operator actually needs when
    judging whether a frozen sell will clear at tomorrow's open.

    The two boards are deliberately one limit step apart in both cases, so a
    "relabelled `T` price" regression -- i.e. writing `p_limit_*_session_t` into the
    `*_next` key -- turns this test red instead of silently agreeing with itself.
    """
    ticket = _ticket(
        tmp_path,
        monkeypatch,
        prev_close=prev_close,
        as_of_close=as_of_close,
        holdings=holdings,
    )
    row = _order(ticket, _ETF)
    board = row["detail_reason"]["limit_board"]

    # The veto itself did NOT move (this row still carries the same verdict).
    expected_warning = "LIMIT_UP_LOCKED" if side == "limit_up" else "LIMIT_DOWN_ILLIQUID_RISK"
    assert row["execution_warning"] == expected_warning

    # `_next` is anchored on the `T` close (`price`), not on the `T-1` close.
    assert board[next_key] == pytest.approx(expected_next)
    assert board[next_key] == pytest.approx(
        round(row["price"] * (1.0 + 0.10 if side == "limit_up" else 1.0 - 0.10), 3)
    )
    # The `T`-session board is published too, and it is the OTHER number.
    assert board[session_key] == pytest.approx(expected_session_t)
    assert board[session_key] == pytest.approx(round(prev_close * (1.0 + 0.10 if side == "limit_up" else 1.0 - 0.10), 3))
    assert board[next_key] != pytest.approx(board[session_key]), (
        f"{next_key} equals {session_key}: the diagnostic is a relabelled `T` price"
    )
    # 51xxxx ETF => `limit_pct == 0.10` (i.e. a real board, not the new-listing sentinel).
    assert board["limit_pct"] == pytest.approx(0.10)
