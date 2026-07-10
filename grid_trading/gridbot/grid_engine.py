"""Core adaptive grid geometry and fill logic.

Long-only, buy-low/sell-high grid: a set of evenly spaced price levels is built
around a center price. Each pair of adjacent levels forms a "slot". A flat slot
buys when price touches its lower level; once long, it sells when price rises
to touch its upper level, realizing the spacing as profit. Spacing is rebuilt
periodically from ATR so wider/narrower grids track current volatility.

This module only manages grid geometry and slot state (flat/long) -- it has no
notion of cash, commissions, or equity. The Backtester owns money accounting
and calls fill_buy/fill_sell once it has decided a trade is affordable.
"""

from dataclasses import dataclass, field


def compute_spacing_pct(atr_value: float, price: float, atr_multiplier: float,
                         min_spacing_pct: float, max_spacing_pct: float) -> float:
    """spacing_pct = clip(ATR% * multiplier, floor, ceiling).

    This is the dynamic-spacing formula documented across the ATR-grid
    implementations reviewed in research: volatility expands the grid,
    the floor/ceiling keep it from becoming degenerate (too tight -> fee
    churn, too wide -> misses oscillations).
    """
    if price <= 0 or atr_value is None or atr_value != atr_value:  # NaN guard
        return min_spacing_pct
    atr_pct = atr_value / price
    return max(min_spacing_pct, min(max_spacing_pct, atr_pct * atr_multiplier))


@dataclass
class Slot:
    lower: float
    upper: float
    state: str = "flat"   # "flat" or "long"
    qty: float = 0.0
    entry_price: float = 0.0


@dataclass
class Fill:
    side: str        # "buy" or "sell"
    price: float
    slot_id: int
    qty: float = 0.0
    entry_price: float = 0.0  # only set for sells, to compute P&L


@dataclass
class GridEngine:
    levels_per_side: int
    levels: list = field(default_factory=list)
    slots: list = field(default_factory=list)
    center: float = 0.0
    spacing: float = 0.0

    def build_grid(self, center_price: float, spacing_abs: float) -> None:
        """(Re)build the grid around center_price, discarding any prior slot state.

        Caller is responsible for liquidating open slots (at the current market
        price) before calling this, so no position silently disappears.
        """
        n = self.levels_per_side
        self.center = center_price
        self.spacing = spacing_abs
        self.levels = [center_price + i * spacing_abs for i in range(-n, n + 1)]
        self.slots = [Slot(lower=self.levels[i], upper=self.levels[i + 1]) for i in range(len(self.levels) - 1)]

    @property
    def open_slots(self) -> list:
        return [s for s in self.slots if s.state == "long"]

    @property
    def lower_bound(self) -> float:
        return self.levels[0]

    @property
    def upper_bound(self) -> float:
        return self.levels[-1]

    def is_breakout(self, price: float, breakout_mult: float = 1.0) -> bool:
        """True if price has moved outside the grid's outer band."""
        if not self.levels:
            return True
        span = self.upper_bound - self.center
        return price > self.center + span * breakout_mult or price < self.center - span * breakout_mult

    def buy_triggers(self, bar_low: float, allow_new_entries: bool, open_slot_cap: int) -> list:
        """Return flat slot indices whose lower level was touched this bar.

        Ordered nearest-to-center first (the levels price would reach first
        on the way down), and truncated to respect the open-slot cap.
        """
        if not allow_new_entries:
            return []
        room = open_slot_cap - len(self.open_slots)
        if room <= 0:
            return []
        candidates = [
            i for i, s in enumerate(self.slots)
            if s.state == "flat" and bar_low <= s.lower
        ]
        candidates.sort(key=lambda i: abs(self.slots[i].lower - self.center))
        return candidates[:room]

    def sell_triggers(self, bar_high: float) -> list:
        """Return long slot indices whose upper level was touched this bar."""
        candidates = [
            i for i, s in enumerate(self.slots)
            if s.state == "long" and bar_high >= s.upper
        ]
        candidates.sort(key=lambda i: abs(self.slots[i].upper - self.center))
        return candidates

    def fill_buy(self, slot_id: int, price: float, qty: float) -> Fill:
        slot = self.slots[slot_id]
        slot.state = "long"
        slot.qty = qty
        slot.entry_price = price
        return Fill(side="buy", price=price, slot_id=slot_id, qty=qty)

    def fill_sell(self, slot_id: int, price: float) -> Fill:
        slot = self.slots[slot_id]
        fill = Fill(side="sell", price=price, slot_id=slot_id, qty=slot.qty, entry_price=slot.entry_price)
        slot.state = "flat"
        slot.qty = 0.0
        slot.entry_price = 0.0
        return fill
