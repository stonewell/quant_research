import pytest

from gridbot.grid_engine import GridEngine, compute_spacing_pct


def test_compute_spacing_pct_clips_to_floor_and_ceiling():
    # tiny ATR -> floor
    assert compute_spacing_pct(atr_value=0.01, price=100, atr_multiplier=1.0,
                                min_spacing_pct=0.01, max_spacing_pct=0.06) == 0.01
    # huge ATR -> ceiling
    assert compute_spacing_pct(atr_value=50, price=100, atr_multiplier=1.0,
                                min_spacing_pct=0.01, max_spacing_pct=0.06) == 0.06
    # mid-range passes through multiplier
    val = compute_spacing_pct(atr_value=2.0, price=100, atr_multiplier=1.5,
                               min_spacing_pct=0.01, max_spacing_pct=0.06)
    assert val == pytest.approx(0.03)


def test_build_grid_levels_and_slots():
    engine = GridEngine(levels_per_side=3)
    engine.build_grid(center_price=100.0, spacing_abs=2.0)
    assert engine.levels == [94.0, 96.0, 98.0, 100.0, 102.0, 104.0, 106.0]
    assert len(engine.slots) == 6
    assert engine.lower_bound == 94.0
    assert engine.upper_bound == 106.0
    assert all(s.state == "flat" for s in engine.slots)


def test_buy_trigger_fires_on_touch_and_respects_cap():
    engine = GridEngine(levels_per_side=2)
    engine.build_grid(center_price=100.0, spacing_abs=2.0)  # levels: 96,98,100,102,104
    # price dips to 97: should trigger the 98-line slot (slot index 1: 98-100)
    triggers = engine.buy_triggers(bar_low=97.0, allow_new_entries=True, open_slot_cap=5)
    assert 1 in triggers

    # a sharp drop touching both buy slots, but capped to 1 open slot
    triggers = engine.buy_triggers(bar_low=95.0, allow_new_entries=True, open_slot_cap=1)
    assert len(triggers) == 1


def test_buy_trigger_blocked_when_entries_disallowed():
    engine = GridEngine(levels_per_side=2)
    engine.build_grid(center_price=100.0, spacing_abs=2.0)
    triggers = engine.buy_triggers(bar_low=95.0, allow_new_entries=False, open_slot_cap=5)
    assert triggers == []


def test_fill_buy_then_sell_round_trip():
    engine = GridEngine(levels_per_side=2)
    engine.build_grid(center_price=100.0, spacing_abs=2.0)  # slot 1 = (98, 100)
    fill = engine.fill_buy(slot_id=1, price=98.0, qty=10)
    assert fill.side == "buy"
    assert engine.slots[1].state == "long"
    assert engine.open_slots == [engine.slots[1]]

    sell_triggers = engine.sell_triggers(bar_high=100.5)
    assert 1 in sell_triggers
    sell_fill = engine.fill_sell(slot_id=1, price=100.0)
    assert sell_fill.qty == 10
    assert sell_fill.entry_price == 98.0
    assert engine.slots[1].state == "flat"
    assert engine.open_slots == []


def test_is_breakout():
    engine = GridEngine(levels_per_side=2)
    engine.build_grid(center_price=100.0, spacing_abs=2.0)  # bounds 96..104
    assert not engine.is_breakout(102.0)
    assert engine.is_breakout(105.0)
    assert engine.is_breakout(95.0)
