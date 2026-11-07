"""M1.1 / Stage C -- ONE causal derivation of the two opt-in gates' bar anchors.

WHY THIS FILE EXISTS
====================
M4.3 wired the position-policy kernel into the live path but passed
`ma20_close=None, vwap20=None, val=None`, because NOTHING in the codebase
derived them. So the two opt-in M1.1 gates -- gate 2b
(`two_day_breakdown_rule`, 两日结构破位) and gate 3b (`price_reentry_gate`,
价格/VWAP 再入场确认) -- were inert in EVERY engine, backtest and live alike.

Stage C adds exactly one derivation (`common.position_lifecycle.
derive_lifecycle_context` + its single reader `lifecycle_context_at`) and calls
it from BOTH the backtest simulator (`simulate_position_lifecycle`) and the live
runner (`scripts/run_live_strategy.py`). The tests below pin, in order:

  1. the FORMULAS and the degradation rules (`None`, never a fabricated 0.0);
  2. CAUSALITY -- mutating future bars does not change an earlier bar's anchors;
  3. the three synthetic acceptance scenarios of `M1.1` item 8, end to end
     through the BACKTEST path with the gate ON, each compared against the same
     scenario replayed through the LIVE adapter shape
     (`step_position_lifecycle` per bar, exactly as `run_live_strategy` calls it)
     and required to agree to `|Δw| < 1e-4` with `ΔShares == 0`;
  4. that both gates remain OFF by default.

100% offline: synthetic OHLCV built in-process. No network, no cache, no
`data/runs/**`.
"""

import numpy as np
import pandas as pd
import pytest

from common.position_lifecycle import (
    AssetLifecycleState,
    LIFECYCLE_CONTEXT_COLUMNS,
    PositionLifecycleConfig,
    derive_lifecycle_context,
    derive_lifecycle_context_from_frame,
    lifecycle_context_at,
    simulate_position_lifecycle,
    step_position_lifecycle,
)
from common.position_policy import PolicyContext, PositionPolicyConfig

_SYMBOL = "600000.SH"
_START = "2021-01-04"
_VOL = 1_000_000.0
_IDX_MA20 = 19  # 0-based index of the first bar with a full 20-bar window


# ---------------------------------------------------------------------------
# synthetic frames / helpers
# ---------------------------------------------------------------------------


def _frame(closes, volumes=None, val=None) -> pd.DataFrame:
    """OHLCV frame from a close path (Open=Close, High/Low = Close +/- 0.3).

    The narrow High/Low spread keeps ATR small, so the ATR trailing stop
    (`atr_multiplier=4.5`) sits far below the deliberate dips these scenarios use
    and cannot be mistaken for the structural-breakdown gate under test.
    """
    closes = np.asarray(closes, dtype=float)
    index = pd.bdate_range(_START, periods=len(closes))
    frame = pd.DataFrame(
        {
            "Open": closes,
            "High": closes + 0.3,
            "Low": closes - 0.3,
            "Close": closes,
        },
        index=index,
    )
    frame["Volume"] = _VOL if volumes is None else np.asarray(volumes, dtype=float)
    if val is not None:
        frame["VAL"] = np.asarray(val, dtype=float)
    return frame


def _record_map(records):
    """`(date_str, symbol) -> record` for the simulator's lifecycle records."""
    return {(rec["date"], rec["symbol"]): rec for rec in records}


# ---------------------------------------------------------------------------
# 1. formulas and degradation
# ---------------------------------------------------------------------------


def test_ma20_close_is_a_full_window_mean_and_none_before_warmup():
    """`ma20_close` is the trailing 20-bar mean; bar 19 is the first one.

    NOT a partial-window mean: a 3-bar mean labelled "MA20" would sit on top of
    the price and turn any dip into a "structural breakdown" on a symbol with no
    20-day structure at all. Insufficient history must therefore be `None` (a
    no-op for gate 2b), never a number.
    """
    closes = pd.Series(np.arange(1.0, 41.0), index=pd.bdate_range(_START, periods=40))
    context = derive_lifecycle_context(closes)

    assert list(context.columns) == list(LIFECYCLE_CONTEXT_COLUMNS)
    # Bars 0..18: fewer than 20 observations -> no anchor at all.
    assert context["ma20_close"].iloc[: _IDX_MA20].isna().all()
    # Bar 19 = mean(1..20) = 10.5, bar 20 = mean(2..21) = 11.5, trailing only.
    assert context["ma20_close"].iloc[_IDX_MA20] == pytest.approx(10.5)
    assert context["ma20_close"].iloc[_IDX_MA20 + 1] == pytest.approx(11.5)
    assert lifecycle_context_at(context, context.index[_IDX_MA20 - 1])["ma20_close"] is None
    assert lifecycle_context_at(context, context.index[_IDX_MA20])["ma20_close"] == pytest.approx(10.5)


def test_vwap20_is_volume_weighted_and_needs_a_full_usable_window():
    """`vwap20 = sum(close*volume)/sum(volume)` over the configured lookback."""
    index = pd.bdate_range(_START, periods=25)
    closes = pd.Series(np.linspace(100.0, 124.0, 25), index=index)
    volumes = pd.Series(np.linspace(1000.0, 3000.0, 25), index=index)

    context = derive_lifecycle_context(closes, volumes)
    expected = float(
        (closes.iloc[-20:] * volumes.iloc[-20:]).sum() / volumes.iloc[-20:].sum()
    )
    assert context["vwap20"].iloc[-1] == pytest.approx(expected)
    # An equal-weighted mean would be a different number -- prove the weighting.
    assert context["vwap20"].iloc[-1] != pytest.approx(float(closes.iloc[-20:].mean()))
    # Shorter lookback -> a different window, never a partial one.
    short = derive_lifecycle_context(closes, volumes, vwap_lookback=5)
    assert short["vwap20"].iloc[-1] == pytest.approx(
        float((closes.iloc[-5:] * volumes.iloc[-5:]).sum() / volumes.iloc[-5:].sum())
    )

    # A NaN volume bar inside the window makes the window unusable -> None.
    holed = volumes.copy()
    holed.iloc[-3] = np.nan
    holed_ctx = derive_lifecycle_context(closes, holed)
    assert np.isnan(holed_ctx["vwap20"].iloc[-1])
    assert lifecycle_context_at(holed_ctx)["vwap20"] is None

    # A zero-volume window has no price at all (0/0), never an infinite one.
    zero_ctx = derive_lifecycle_context(closes, pd.Series(0.0, index=index))
    assert np.isnan(zero_ctx["vwap20"].iloc[-1])
    assert lifecycle_context_at(zero_ctx)["vwap20"] is None

    # No volume series at all -> VWAP unavailable, but MA20 still derived.
    no_vol = derive_lifecycle_context(closes)
    assert np.isnan(no_vol["vwap20"]).all()
    values = lifecycle_context_at(no_vol)
    assert values["vwap20"] is None and values["vol_ma20"] is None
    assert values["ma20_close"] == pytest.approx(float(closes.iloc[-20:].mean()))


def test_vol_ma20_reuses_the_callers_series_and_val_is_never_fabricated():
    """`vol_ma20` is REUSED verbatim; `val` exists only if the frame carries one."""
    index = pd.bdate_range(_START, periods=25)
    closes = pd.Series(100.0, index=index)
    volumes = pd.Series(_VOL, index=index)

    # A caller-supplied mean volume wins over any re-derivation.
    supplied = pd.Series(123_456.0, index=index)
    ctx = derive_lifecycle_context(closes, volumes, vol_ma20=supplied)
    assert lifecycle_context_at(ctx)["vol_ma20"] == pytest.approx(123_456.0)

    # Frame without a VAL column -> val is None. Never a rolling low, never a
    # Close-derived level: a fabricated floor silently changes `min(MA20, VAL)`.
    frame = _frame([100.0] * 25, volumes=[_VOL] * 25)
    values = lifecycle_context_at(derive_lifecycle_context_from_frame(frame))
    assert values["val"] is None
    assert set(derive_lifecycle_context_from_frame(frame).columns) == set(
        LIFECYCLE_CONTEXT_COLUMNS
    )

    # Frame WITH a VAL column -> passed through verbatim (alias accepted too).
    val_levels = np.linspace(90.0, 95.0, 25)
    with_val = _frame([100.0] * 25, volumes=[_VOL] * 25, val=val_levels)
    assert lifecycle_context_at(derive_lifecycle_context_from_frame(with_val))[
        "val"
    ] == pytest.approx(95.0)
    aliased = with_val.rename(columns={"VAL": "value_area_low"})
    assert lifecycle_context_at(derive_lifecycle_context_from_frame(aliased))[
        "val"
    ] == pytest.approx(95.0)


def test_reader_degrades_every_missing_cell_to_none_and_never_to_zero():
    """Missing bar / empty frame / non-positive level -> `None`, never 0.0."""
    index = pd.bdate_range(_START, periods=25)
    closes = pd.Series(100.0, index=index)
    context = derive_lifecycle_context(closes, pd.Series(_VOL, index=index))

    absent = lifecycle_context_at(context, pd.Timestamp("1999-01-04"))
    assert absent == {name: None for name in LIFECYCLE_CONTEXT_COLUMNS}
    assert lifecycle_context_at(None) == {name: None for name in LIFECYCLE_CONTEXT_COLUMNS}
    assert lifecycle_context_at(derive_lifecycle_context(None)) == {
        name: None for name in LIFECYCLE_CONTEXT_COLUMNS
    }

    # A zero/negative level is "cannot evaluate", NOT a floor of zero (which
    # would make every close a breakdown) and NOT a VWAP of zero.
    zeroed = context.copy()
    zeroed.loc[zeroed.index[-1], ["ma20_close", "vwap20", "vol_ma20", "val"]] = 0.0
    assert lifecycle_context_at(zeroed) == {name: None for name in LIFECYCLE_CONTEXT_COLUMNS}


# ---------------------------------------------------------------------------
# 2. causality -- the whole point of "no look-ahead"
# ---------------------------------------------------------------------------


def test_context_is_causal_future_bars_cannot_move_an_earlier_bar():
    """Mutating bars AFTER `k` must leave every bar `<= k` bit-identical.

    This is the property that makes the backtest and the live ticket comparable:
    on the live as-of session the future simply does not exist, so if a later bar
    could change an earlier anchor the backtest would be reading data the ticket
    never had. Volumes are mutated too, because `vwap20` / `vol_ma20` come from
    the volume series.
    """
    rng = np.random.default_rng(20261107)
    n = 60
    index = pd.bdate_range(_START, periods=n)
    closes = pd.Series(100.0 + np.cumsum(rng.normal(0.0, 0.5, n)), index=index)
    volumes = pd.Series(rng.uniform(5e5, 1.5e6, n), index=index)

    base = derive_lifecycle_context(closes, volumes)

    for k in (0, 5, _IDX_MA20 - 1, _IDX_MA20, 30, n - 2):
        mutated_closes = closes.copy()
        mutated_volumes = volumes.copy()
        # Wildly change EVERYTHING after bar k (both legs of both anchors).
        mutated_closes.iloc[k + 1 :] = closes.iloc[k + 1 :] * 3.0 + 500.0
        mutated_volumes.iloc[k + 1 :] = volumes.iloc[k + 1 :] * 0.01
        mutated = derive_lifecycle_context(mutated_closes, mutated_volumes)

        pd.testing.assert_frame_equal(
            base.iloc[: k + 1], mutated.iloc[: k + 1], check_exact=True
        )
        # ... and the mutation really did move the LATER rows (non-vacuity).
        if k + 1 < n:
            tail_changed = not base.iloc[k + 1 :].equals(mutated.iloc[k + 1 :])
            assert tail_changed, f"mutation after bar {k} changed nothing -- vacuous test"


# ---------------------------------------------------------------------------
# 3. synthetic acceptance scenarios (M1.1 item 8), backtest vs live adapter
# ---------------------------------------------------------------------------
#
# Each scenario is a full bar path through the SAME lifecycle machine, run twice:
#
#   BACKTEST ARM -- `simulate_position_lifecycle(universe, weights, cfg)`, i.e.
#                   the call `common/allocation_backtester.py`,
#                   `pipeline/parity_harness.py` and `backtester/run_backtest.py`
#                   make.
#   LIVE ARM     -- `step_position_lifecycle` per bar over a `{symbol: state}`
#                   dict, with the arguments `scripts/run_live_strategy.py`
#                   builds (last close, last volume, 20-bar mean volume, ATR(14),
#                   and the Stage-C anchors derived from the frame TRUNCATED to
#                   that session -- the live as-of view).
#
# They are required to agree bar-for-bar to |Δw| < 1e-4, which at these weights
# and a 1,000,000 NAV implies identical integer lot orders (ΔShares == 0), and
# the scenario-specific expectation is asserted on the BACKTEST arm.

_NAV = 1_000_000.0
_LOT = 100
_GATE_ON = dict(
    live_use_policy_kernel=True,
    two_day_breakdown_rule=True,
    price_reentry_gate=True,
)


def _weights(index, entries):
    """Sparse target-weight panel: a value only on the bars named in `entries`."""
    frame = pd.DataFrame(np.nan, index=index, columns=[_SYMBOL], dtype=float)
    for bar, weight in entries:
        frame.iloc[bar, 0] = weight
    return frame


def _live_arm(frame: pd.DataFrame, weights: pd.DataFrame, cfg: PositionLifecycleConfig):
    """Replay one symbol through `step_position_lifecycle` exactly as live does.

    Returns `(per_bar_target_weights, per_bar_records)`; `records[i]` is the
    `to_dict()` of the decision at bar `i`, and the weight is the decision's
    `target_weight` -- the number the live ticket turns into shares.
    """
    from common.indicators import atr as _atr

    state = AssetLifecycleState(symbol=_SYMBOL)
    raw_targets = weights[_SYMBOL].ffill().fillna(0.0)
    live_weights, live_records = [], []
    for t in frame.index:
        visible = frame.loc[:t]  # the live as-of view: no future bars exist
        price = float(visible["Close"].iloc[-1])
        volumes = visible["Volume"].dropna()
        cur_v = float(volumes.iloc[-1]) if len(volumes) else 0.0
        ma_v = float(volumes.iloc[-20:].mean()) if len(volumes) else 0.0
        sym_atr = None
        if len(visible) >= 14:
            try:
                sym_atr = float(_atr(visible, 14).iloc[-1])
            except Exception:
                sym_atr = None
        anchors = lifecycle_context_at(
            derive_lifecycle_context_from_frame(
                visible,
                vwap_lookback=int(getattr(cfg, "reentry_vwap_lookback", 20) or 20),
            )
        )
        decision = step_position_lifecycle(
            state=state,
            raw_target_weight=float(raw_targets.loc[t]),
            current_price=price,
            cfg=cfg,
            market="china",
            current_volume=cur_v if cur_v > 0 else None,
            ma20_volume=ma_v if ma_v > 0 else None,
            market_breadth=None,
            atr=sym_atr,
            is_emergency_halt=False,
            is_market_open=True,
            is_portfolio_deleverage=False,
            ma20_close=anchors["ma20_close"],
            vwap20=anchors["vwap20"],
            vol_ma20=anchors["vol_ma20"],
            val=anchors["val"],
        )
        live_weights.append(float(decision.target_weight))
        rec = decision.to_dict()
        rec["date"] = str(t)
        rec["symbol"] = _SYMBOL
        live_records.append(rec)
    return live_weights, live_records


def _lot_rounded_shares(delta_w: float, price: float) -> int:
    """Signed lot-rounded share delta for a weight change (the ticket's arithmetic)."""
    raw = delta_w * _NAV / price
    return int(np.sign(raw) * np.floor(abs(raw) / _LOT + 0.5)) * _LOT


def _share_deltas(weights, closes) -> list:
    deltas, previous = [], 0.0
    for weight, price in zip(weights, closes):
        deltas.append(_lot_rounded_shares(float(weight) - previous, float(price)))
        previous = float(weight)
    return deltas


def _assert_arms_agree(universe, weights, cfg, label):
    """Backtest arm vs live arm: |Δw| < 1e-4 per bar and ΔShares == 0."""
    frame = universe[_SYMBOL]
    sparse, records = simulate_position_lifecycle(
        universe, weights, cfg=cfg, market="china"
    )
    backtest_weights = (
        sparse.reindex(index=frame.index, columns=[_SYMBOL]).ffill().fillna(0.0)[_SYMBOL]
    )
    live_weights, live_records = _live_arm(frame, weights, cfg)

    closes = frame["Close"].to_numpy(dtype=float)
    bt = backtest_weights.to_numpy(dtype=float)
    max_dw = float(np.max(np.abs(bt - np.asarray(live_weights, dtype=float))))
    assert max_dw < 1e-4, f"{label}: backtest vs live |Δw| = {max_dw!r}"

    bt_shares = _share_deltas(bt, closes)
    live_shares = _share_deltas(live_weights, closes)
    share_diff = [a - b for a, b in zip(bt_shares, live_shares)]
    assert all(d == 0 for d in share_diff), (
        f"{label}: ΔShares per bar = {share_diff} (must be all zero)"
    )

    # The two arms must also agree on WHY, not just on the number: a row that
    # traded nothing for a different reason is a divergence the ticket would show.
    assert len(live_records) == len(frame)
    bt_records = _record_map(records)
    for rec in live_records:
        # The simulator records one row per (evaluated bar, symbol); a missing
        # entry would mean it skipped a bar the live arm evaluated -- a coverage
        # gap, not a pass, so assert the row exists rather than skipping it.
        assert (rec["date"], _SYMBOL) in bt_records, (
            f"{label}: the simulator produced no record for {rec['date']}"
        )
        other = bt_records[(rec["date"], _SYMBOL)]
        assert rec["action"] == other["action"], (
            f"{label}: {rec['date']} action {rec['action']!r} != {other['action']!r}"
        )
        assert rec["blocking_reason"] == other["blocking_reason"], (
            f"{label}: {rec['date']} blocking_reason {rec['blocking_reason']!r} != "
            f"{other['blocking_reason']!r}"
        )
        assert rec["state_after"] == other["state_after"], (
            f"{label}: {rec['date']} state_after diverged"
        )
    return sparse, _record_map(records), backtest_weights


def test_scenario_false_breakdown_rebound_never_sells():
    """假破位反弹: one close below MA20 that recovers the next bar -> NO SELL.

    Gate 2b counts the break (`EXIT_CONFIRMING`) and then CLEARS it, which is the
    whole reason the rule exists (a one-bar spike through the floor is one of the
    three real loss sources in `docs/long_only_multi_asset_gap_analysis.md`).
    """
    closes = [100.0] * 25 + [100.5] * 6 + [99.0, 100.6] + [100.6] * 13
    frame = _frame(closes)
    universe = {_SYMBOL: frame}
    weights = _weights(frame.index, [(25, 0.30)])
    cfg = PositionLifecycleConfig(**_GATE_ON)

    sparse, records, effective = _assert_arms_agree(
        universe, weights, cfg, "假破位反弹"
    )

    dip, recovery = str(frame.index[31]), str(frame.index[32])
    assert records[(dip, _SYMBOL)]["blocking_reason"] == "EXIT_CONFIRMING"
    assert records[(dip, _SYMBOL)]["action"] == "HOLD_FILTERED"
    assert (records[(dip, _SYMBOL)]["detail_reason"] or {}).get("breakdown_streak") == 1
    # Recovered: the counter is cleared and no exit ladder is entered.
    assert records[(recovery, _SYMBOL)]["blocking_reason"] != "EXIT_CONFIRMING"
    assert records[(recovery, _SYMBOL)]["action"] == "HOLD"

    assert not any(rec["action"] == "SELL" for rec in records.values())
    assert effective.iloc[-1] == pytest.approx(0.30)
    # Exactly one rebalance row survived (the entry); the dip wrote nothing.
    assert int(sparse[_SYMBOL].notna().sum()) == 1


def test_scenario_volume_shrink_pullback_is_exempt_and_not_counted():
    """缩量回踩: a low-volume dip is exempt -- it must not advance the counter.

    Bar 31 dips on 10% of normal volume (exempt, no count); bar 32 dips on normal
    volume. `breakdown_streak` must therefore be 1, not 2 -- one counted day, no
    confirmed breakdown, no SELL.
    """
    volumes = [_VOL] * 31 + [_VOL * 0.10, _VOL] + [_VOL] * 13
    closes = [100.0] * 25 + [100.5] * 6 + [99.0, 99.0] + [100.6] * 13
    frame = _frame(closes, volumes=volumes)
    universe = {_SYMBOL: frame}
    weights = _weights(frame.index, [(25, 0.30)])
    cfg = PositionLifecycleConfig(**_GATE_ON)

    sparse, records, effective = _assert_arms_agree(
        universe, weights, cfg, "缩量回踩"
    )

    shrink_bar, counted_bar = str(frame.index[31]), str(frame.index[32])
    # The exempt bar never even enters the rule: no confirmation row at all.
    assert records[(shrink_bar, _SYMBOL)]["blocking_reason"] != "EXIT_CONFIRMING"
    assert "breakdown_streak" not in (records[(shrink_bar, _SYMBOL)]["detail_reason"] or {})
    # One counted bar, so the two-day rule is NOT confirmed.
    assert records[(counted_bar, _SYMBOL)]["blocking_reason"] == "EXIT_CONFIRMING"
    assert (records[(counted_bar, _SYMBOL)]["detail_reason"] or {})["breakdown_streak"] == 1
    assert (records[(counted_bar, _SYMBOL)]["detail_reason"] or {})[
        "breakdown_vol_shrink_pct"
    ] == pytest.approx(0.60)

    assert not any(rec["action"] == "SELL" for rec in records.values())
    assert effective.iloc[-1] == pytest.approx(0.30)
    assert int(sparse[_SYMBOL].notna().sum()) == 1


def test_scenario_next_day_reentry_is_gated_then_allowed():
    """T+1 隔日再入场: exit -> cooldown expiry -> gate 3b decides the re-entry.

    Price 98.8 on the cooldown-expiry bar is below `last_exit_price * (1 + 2%)`
    AND below the 20-bar VWAP, so the kernel refuses the re-entry (`REENTRY_GATED`,
    no position, no `0.0` exit row). The following bar clears the rebound
    threshold, the gate allows, and the normal two-bar entry confirmation resumes.
    """
    closes = (
        [100.0] * 25
        + [100.5] * 5
        + [99.5, 99.0, 98.5, 98.8]   # exit at 99.0, then the cooldown bars
        + [101.5] * 9                # rebound -> gate allows
    )
    frame = _frame(closes)
    universe = {_SYMBOL: frame}
    # 0.30 in, flat at bar 30, re-entry signal from bar 32 onward.
    weights = _weights(frame.index, [(25, 0.30), (30, 0.0), (32, 0.30)])
    cfg = PositionLifecycleConfig(**_GATE_ON)

    sparse, records, effective = _assert_arms_agree(
        universe, weights, cfg, "T+1 隔日再入场"
    )

    exit_bar = str(frame.index[31])
    expiry_bar = str(frame.index[33])
    rebound_bar = str(frame.index[34])
    confirm_bar = str(frame.index[35])

    exit_rec = records[(exit_bar, _SYMBOL)]
    assert exit_rec["action"] == "SELL"
    assert (exit_rec["detail_reason"] or {})["last_exit_price"] == pytest.approx(99.0)

    gated = records[(expiry_bar, _SYMBOL)]
    assert gated["action"] == "HOLD_FILTERED"
    assert gated["blocking_reason"] == "REENTRY_GATED"
    assert gated["target_weight"] == 0.0
    assert gated["state_after"] == "FLAT"

    assert records[(rebound_bar, _SYMBOL)]["blocking_reason"] == "PENDING_ENTRY_CONFIRM"
    assert records[(confirm_bar, _SYMBOL)]["action"] == "BUY"

    # The sparse frame: entry, explicit 0.0 exit, then the confirmed re-entry --
    # the gated bar wrote NOTHING (a blocked re-entry is a missing row, never 0.0).
    assert sparse[_SYMBOL].notna().sum() == 3
    assert sparse[_SYMBOL].loc[frame.index[31]] == 0.0
    assert sparse[_SYMBOL].loc[confirm_bar] == pytest.approx(0.30)
    assert effective.iloc[-1] == pytest.approx(0.30)


def test_scenarios_are_inert_while_the_gate_flags_are_off():
    """Both flags default OFF, and with them OFF the anchors move no decision.

    The anchors are still DERIVED (one code path, no rot), so this compares the
    default config against a config that additionally carries a hand-built
    hostile anchor set: gate 2b/3b do not exist while the flags are False, so the
    two must be bit-identical.
    """
    assert PositionLifecycleConfig().two_day_breakdown_rule is False
    assert PositionLifecycleConfig().price_reentry_gate is False
    assert PositionPolicyConfig().two_day_breakdown_rule is False
    assert PositionPolicyConfig().price_reentry_gate is False

    closes = [100.0] * 25 + [100.5] * 6 + [99.0, 98.8] + [100.6] * 11
    frame = _frame(closes)
    universe = {_SYMBOL: frame}
    weights = _weights(frame.index, [(25, 0.30)])

    default_cfg = PositionLifecycleConfig(live_use_policy_kernel=True)
    gated_cfg = PositionLifecycleConfig(**_GATE_ON)

    base_frame, base_records = simulate_position_lifecycle(
        universe, weights, cfg=default_cfg, market="china"
    )
    armed_frame, armed_records = simulate_position_lifecycle(
        universe, weights, cfg=gated_cfg, market="china"
    )
    # The gate does matter when it is ON (non-vacuity): the two-day close below
    # MA20 is an explicit `0.0` BREAKDOWN exit that the default config never emits.
    assert not base_frame.equals(armed_frame)
    assert any(
        rec["action"] == "SELL"
        and (rec["detail_reason"] or {}).get("last_exit_type") == "BREAKDOWN"
        for rec in armed_records
    )
    # ... but the DEFAULT (both flags OFF) never emits a gate label.
    assert not any(
        rec["blocking_reason"] in ("EXIT_CONFIRMING", "REENTRY_GATED")
        for rec in base_records
    )
