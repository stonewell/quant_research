"""Unified Position Lifecycle State Machine (UPLSM).

Formalizes the lifecycle of every portfolio asset into a deterministic,
observable state machine preventing whipsaws, locking T+1 gestation tenures,
enforcing asymmetric re-entry cooldowns, and filtering micro-rebalance churn
across both backtesting and live execution engines.
"""

from __future__ import annotations

import copy
import logging
import warnings
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd


_LOGGER = logging.getLogger(__name__)


class PositionState(str, Enum):
    """Lifecycle states for a portfolio asset."""
    FLAT = "FLAT"
    PENDING_ENTRY = "PENDING_ENTRY"
    HELD_GESTATION = "HELD_GESTATION"
    HELD_ACTIVE = "HELD_ACTIVE"
    PENDING_EXIT = "PENDING_EXIT"
    COOLDOWN = "COOLDOWN"


class ExitType(str, Enum):
    """Categorization of liquidation triggers."""
    NORMAL = "NORMAL"
    HARD_STOP = "HARD_STOP"
    TRAILING_STOP = "TRAILING_STOP"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"


class BlockingReason(str, Enum):
    """Structured "why was no trade emitted" label -- `docs/development_plan.md` L2.24.

    Root cause this enum exists for (disclosed): `HOLD_FILTERED` was reused by
    SIX branches of `step_position_lifecycle` plus FOUR producer sites in
    `scripts/run_live_strategy.py`, so `action` alone could not distinguish
    "a signal was evaluated and rejected" from "nothing was evaluated at all"
    (market-closed freeze) or from "the order was blocked by a risk gate"
    (tenure lock / limit-up lock / cooldown). Downstream consumers then had no
    honest way to ask "did this row liquidate the position?" other than
    inferring it from `target_weight == 0.0` -- an inference that is WRONG for
    every branch below except a real `SELL` (see `docs/version.md` §11 and the
    `L2.21` phantom-anchor post-mortem: 34 never-traded rows across 25 archived
    tickets were read as liquidations).

    Contract:
      - `NONE` means "no blocking gate fired"; it is the default for BUY / SELL
        / plain HOLD / SWEEP_CASH / RELEASE_CASH / CANCEL_PENDING rows.
      - A non-`NONE` value is *only* ever an explanation of why a row traded
        nothing. It can never promote a non-`SELL` row into a liquidation.
      - Persisted/archived rows may predate this field; readers MUST go through
        `coerce_blocking_reason()` (missing -> `NONE`) and MUST NOT derive
        "liquidated or not" from `target_weight` (see `is_liquidation_event`).
    """

    # Nothing was evaluated: not a signal filter, not a risk veto.
    MARKET_CLOSED_FREEZE = "MARKET_CLOSED_FREEZE"
    # A real signal was evaluated and rejected by a gate.
    MIN_ENTRY_THRESHOLD = "MIN_ENTRY_THRESHOLD"
    PENDING_ENTRY_CONFIRM = "PENDING_ENTRY_CONFIRM"
    EXIT_CONFIRMING = "EXIT_CONFIRMING"
    COOLDOWN_BLOCKED = "COOLDOWN_BLOCKED"
    INERTIA = "INERTIA"
    TENURE_LOCKED = "TENURE_LOCKED"
    LIMIT_UP_LOCKED = "LIMIT_UP_LOCKED"
    # Missing / non-positive price on evaluation bar.
    NO_PRICE = "NO_PRICE"
    # No blocking gate fired.
    NONE = "NONE"


# Chinese display labels. Kept next to the enum so the live ticket, the Phase 3
# console rendering and any future web badge cannot drift apart in wording.
BLOCKING_REASON_LABELS: Dict[str, str] = {
    BlockingReason.MARKET_CLOSED_FREEZE.value: "休市未评估",
    BlockingReason.MIN_ENTRY_THRESHOLD.value: "未达最低建仓门槛",
    BlockingReason.PENDING_ENTRY_CONFIRM.value: "开仓信号确认中",
    BlockingReason.EXIT_CONFIRMING.value: "平仓信号确认中",
    BlockingReason.COOLDOWN_BLOCKED.value: "再入场冷却中",
    BlockingReason.INERTIA.value: "未超惰性阈值",
    BlockingReason.TENURE_LOCKED.value: "未满最低持仓周期",
    BlockingReason.LIMIT_UP_LOCKED.value: "涨停封板",
    BlockingReason.NO_PRICE.value: "价格缺失",
    BlockingReason.NONE.value: "无阻断",
}


def coerce_blocking_reason(value: Any) -> BlockingReason:
    """Legacy-tolerant reader for a persisted `blocking_reason`.

    Archived `live_trading_ticket.csv` files written before `L2.24` have no such
    column at all (`pandas.Series.get` -> None), older JSON rows have no such
    key, and a CSV round-trip yields the literal string. All three must read as
    `NONE` -- never raise, never be mistaken for a gate that actually fired.
    Anything unrecognised is also `NONE` (fail open: an unknown label may not
    claim a block, and it certainly may not claim a liquidation).
    """
    if isinstance(value, BlockingReason):
        return value
    if value is None:
        return BlockingReason.NONE
    text = str(value).strip().upper()
    if text in ("", "NAN", "NONE", "NULL"):
        return BlockingReason.NONE
    try:
        return BlockingReason(text)
    except ValueError:
        return BlockingReason.NONE


def blocking_reason_label(value: Any) -> str:
    """Chinese display label for a `blocking_reason` (legacy values tolerated)."""
    return BLOCKING_REASON_LABELS.get(coerce_blocking_reason(value).value, "无阻断")


def is_liquidation_event(action: Any, blocking_reason: Any = None) -> bool:
    """Whether a ticket row represents an ACTUAL liquidation (`docs/development_plan.md` L2.24).

    The single predicate every consumer must use instead of inferring "did we
    exit?" from `target_weight == 0.0`. That inference is the phantom-anchor bug
    class (`docs/version.md` §11): `MIN_ENTRY_THRESHOLD`, `PENDING_ENTRY_CONFIRM`
    and `MARKET_CLOSED_FREEZE` all write a 0.0 target while having traded
    nothing, and `CANCEL_PENDING` is a never-held record.

    `blocking_reason` is accepted for signature stability / caller honesty but is
    deliberately NOT part of the verdict: only `action == "SELL"` can mean "the
    position was liquidated". A blocking label can never promote a non-`SELL`
    row, and a `SELL` is never demoted by a stale/contradictory label (every
    `SELL` path in `position_lifecycle` and in the live runner emits `NONE`).
    """
    return str(action or "").strip().upper() == "SELL"


@dataclass
class PositionLifecycleConfig:
    """Configuration parameters for asset position lifecycles.

    The DEFAULTS below are the ones `scripts/run_live_strategy.py:1555-1573`
    actually deploys (`min_holding_bars=2`, `normal_cooldown_bars=2`,
    `inertia_relative_ratio=0.0`, ...). They were once inconsistent here
    (`min_holding_bars=1`, `normal_cooldown_bars=1`), which meant a bare
    `PositionLifecycleConfig()` modelled a DIFFERENT, looser state machine than
    the live ticket -- a 1-bar tenure lock and a 1-bar re-entry cooldown instead
    of the deployed 2/2. `min_holding_bars` was the odd one out (the field
    default said 1 while both resolvers said 2); F3 aligned it.
    """
    guard_latency_bars: int = 2
    min_holding_bars: int = 2
    normal_cooldown_bars: int = 2
    stop_cooldown_bars: int = 5
    inertia_threshold: float = 0.05
    # (2026-10-22 incident) Relative trim-side inertia ratio. When > 0.0, a
    # SELL-direction partial trim must clear BOTH the absolute
    # `inertia_threshold` AND a proportional fraction of the current weight
    # (`ratio * current_weight`) before it is allowed. Prevents micro-trims
    # (e.g. a -0.0082pp trim on a 17-bar incumbent) from being emitted as real
    # SELL orders that merely round to a handful of shares. 0.0 disables the
    # proportional hurdle and preserves the legacy absolute-only gate.
    inertia_relative_ratio: float = 0.0
    min_entry_threshold: Optional[float] = None
    hard_stop_loss_pct: float = 0.08
    atr_multiplier: float = 4.5
    volume_surge_multiplier: float = 1.5
    breadth_thrust_threshold: float = 0.60
    enable_entry_fast_path: bool = True
    enable_exit_fast_path: bool = True

    def to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


@dataclass
class AssetLifecycleState:
    """Persistent state representation for a single asset's lifecycle."""
    symbol: str
    state: PositionState = PositionState.FLAT
    held_bars: int = 0
    entry_streak: int = 0
    exit_streak: int = 0
    cooldown_remaining: int = 0
    entry_price: float = 0.0
    peak_price: float = 0.0
    current_weight: float = 0.0
    # (2026-10-22 incident) Pre-trade brokerage weight snapshot, hydrated from
    # the actual holdings (cur_w) by run_live_strategy.py before
    # step_position_lifecycle mutates `current_weight` to the post-rebalance
    # target. Persisting `broker_weight` alongside `current_weight` prevents
    # account_state.json from silently storing the TARGET as if it were the
    # broker weight (000999.SZ persisted 0.06731049 while the broker held
    # 0.13934551). The lifecycle machine itself never mutates this field.
    broker_weight: float = 0.0
    last_exit_type: Optional[str] = None

    def is_holding(self) -> bool:
        """Returns True if the asset is currently held in any position state."""
        return self.state in (
            PositionState.HELD_GESTATION,
            PositionState.HELD_ACTIVE,
            PositionState.PENDING_EXIT,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serializes lifecycle state to a JSON-safe dictionary."""
        return {
            "symbol": self.symbol,
            "state": self.state.value if isinstance(self.state, PositionState) else str(self.state),
            "held_bars": self.held_bars,
            "entry_streak": self.entry_streak,
            "exit_streak": self.exit_streak,
            "cooldown_remaining": self.cooldown_remaining,
            "entry_price": float(self.entry_price),
            "peak_price": float(self.peak_price),
            "current_weight": float(self.current_weight),
            "broker_weight": float(self.broker_weight),
            "last_exit_type": self.last_exit_type,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AssetLifecycleState":
        """Restores an AssetLifecycleState instance from a dictionary."""
        raw_state = d.get("state", PositionState.FLAT.value)
        state_enum = PositionState(raw_state) if isinstance(raw_state, str) else raw_state
        return cls(
            symbol=d["symbol"],
            state=state_enum,
            held_bars=int(d.get("held_bars", 0)),
            entry_streak=int(d.get("entry_streak", 0)),
            exit_streak=int(d.get("exit_streak", 0)),
            cooldown_remaining=int(d.get("cooldown_remaining", 0)),
            entry_price=float(d.get("entry_price", 0.0)),
            peak_price=float(d.get("peak_price", 0.0)),
            current_weight=float(d.get("current_weight", 0.0)),
            broker_weight=float(d.get("broker_weight", 0.0)),
            last_exit_type=d.get("last_exit_type"),
        )


@dataclass
class LifecycleDecision:
    """Decision output generated by a single lifecycle step transition."""
    action: str  # "BUY", "SELL", "HOLD", "HOLD_FILTERED", "COOLDOWN_BLOCKED", "CANCEL_PENDING"
    target_weight: float
    reason: str
    detail_reason: Dict[str, Any] = field(default_factory=dict)
    state_before: PositionState = PositionState.FLAT
    state_after: PositionState = PositionState.FLAT
    fast_path_triggered: Optional[str] = None
    # (2026-10-22 incident, Fix 3) Pre-reset entry-confirmation streak, populated
    # ONLY by the CANCEL_PENDING transition. A PENDING_ENTRY asset whose signal
    # disappears has its `entry_streak` zeroed (correct: the confirmation clock
    # must restart from scratch if the signal returns), but the VALUE it had
    # reached is the single most diagnostic number in the 601857.SH post-mortem
    # ("was it 1/2, or 2/2 and therefore a lost confirmed entry?"). Carrying it
    # on the decision object lets the ticket record the lost progress instead of
    # erasing it. Appended with a default so every existing constructor keeps
    # working unchanged.
    cancelled_entry_streak: int = 0
    # (L2.24) Structured "why was nothing traded" label. Appended with a default so
    # every existing constructor keeps working unchanged. See `BlockingReason`:
    # this is the ONLY honest way downstream code may distinguish "a signal was
    # evaluated and gated" from "nothing was evaluated at all", instead of
    # inferring a liquidation from `target_weight == 0.0`.
    blocking_reason: BlockingReason = BlockingReason.NONE

    def to_dict(self) -> Dict[str, Any]:
        """Serializes decision to a JSON-safe dictionary."""
        return {
            "action": self.action,
            "target_weight": float(self.target_weight),
            "reason": self.reason,
            "detail_reason": self.detail_reason,
            "state_before": self.state_before.value if isinstance(self.state_before, PositionState) else str(self.state_before),
            "state_after": self.state_after.value if isinstance(self.state_after, PositionState) else str(self.state_after),
            "fast_path_triggered": self.fast_path_triggered,
            "cancelled_entry_streak": int(self.cancelled_entry_streak),
            "blocking_reason": coerce_blocking_reason(self.blocking_reason).value,
        }


def step_position_lifecycle(
    state: AssetLifecycleState,
    raw_target_weight: float,
    current_price: float,
    cfg: PositionLifecycleConfig,
    market: str = "china",
    current_volume: Optional[float] = None,
    ma20_volume: Optional[float] = None,
    market_breadth: Optional[float] = None,
    atr: Optional[float] = None,
    is_emergency_halt: bool = False,
    is_market_open: bool = True,
    is_portfolio_deleverage: bool = False,
) -> LifecycleDecision:
    """Transitions an asset's position lifecycle state for a single bar.

    Parameters:
        state: The persistent asset lifecycle state (mutated in place).
        raw_target_weight: Unfiltered target weight from the strategy model.
        current_price: Latest asset market price.
        cfg: State machine configuration rules.
        market: Target exchange market ("china" enforces T+1 gestation locks).
        current_volume: Current bar volume (for volume breakout fast path).
        ma20_volume: 20-bar moving average volume.
        market_breadth: Macro breadth percentage [0.0, 1.0].
        atr: Average True Range for dynamic trailing stop checks.
        is_emergency_halt: Macro circuit breaker emergency halt flag.
        is_market_open: Whether current bar is an open trading day. If False,
            all lifecycle counters, gestation timers, and cooldowns remain frozen.
        is_portfolio_deleverage: Portfolio-level drawdown deleveraging active.
            When True and trimming an existing holding (0.0 <= raw_target < current_weight),
            bypasses asset-level inertia threshold and tenure lock to prevent risk-control
            dilution.

    Returns:
        LifecycleDecision containing actionable target weight, trade action,
        and forensic reasoning.
    """
    state_before = state.state

    # 0. Market Open / Trading Day Freeze Check
    if not is_market_open:
        # (L2.23) Freeze semantics must be explicit, not merely implied: this row
        # does NOT mean "a signal was evaluated and filtered out". Nothing was
        # evaluated at all -- the whole state machine (confirmation streaks,
        # tenure lock, cooldown counters) is frozen. It also is NOT a liquidation:
        # `target_weight` is just the carried-over current weight, which happens
        # to be 0.0 whenever the symbol is flat. Reading that 0.0 as "we exited"
        # is exactly the phantom-anchor bug class removed in `L2.21`.
        return LifecycleDecision(
            action="HOLD_FILTERED",
            target_weight=state.current_weight,
            reason=(
                "非交易日/休市日：休市冻结，未评估任何信号"
                "(状态机、确认期、锁定期与冷却期计数器全部冻结，无任何交易指令)"
            ),
            detail_reason={
                "is_market_open": False,
                "signals_evaluated": False,
                "blocking_reason": BlockingReason.MARKET_CLOSED_FREEZE.value,
                "note": "休市冻结，未评估任何信号 (NOT a signal filter, NOT a liquidation)",
            },
            state_before=state_before,
            state_after=state.state,
            fast_path_triggered=None,
            blocking_reason=BlockingReason.MARKET_CLOSED_FREEZE,
        )

    # 0.1 Valid Price Check
    if not (np.isfinite(current_price) and current_price > 0.0):
        return LifecycleDecision(
            action="HOLD_FILTERED",
            target_weight=float(state.current_weight),
            reason="缺失有效价格：跳过评估，保持当前持仓权重与状态",
            detail_reason={
                "current_price": current_price,
                "blocking_reason": BlockingReason.NO_PRICE.value,
                "note": "缺失有效价格 (NO_PRICE): 状态与当前持仓权重保持不变",
            },
            state_before=state_before,
            state_after=state.state,
            fast_path_triggered=None,
            blocking_reason=BlockingReason.NO_PRICE,
        )

    # 1. Cooldown management
    if state.cooldown_remaining > 0:
        state.cooldown_remaining -= 1
        if state.cooldown_remaining > 0:
            return LifecycleDecision(
                action="COOLDOWN_BLOCKED",
                target_weight=0.0,
                reason=f"平仓后处于再入场冷却期 (离场冷却中，剩余 {state.cooldown_remaining} 日，防追高洗盘)",
                detail_reason={
                    "cooldown_remaining": state.cooldown_remaining,
                    "last_exit_type": state.last_exit_type,
                },
                state_before=state_before,
                state_after=state.state,
                fast_path_triggered=None,
                blocking_reason=BlockingReason.COOLDOWN_BLOCKED,
            )
        else:
            state.state = PositionState.FLAT
            state.entry_streak = 0
            state.exit_streak = 0

    # 2. If holding (HELD_GESTATION, HELD_ACTIVE, or PENDING_EXIT)
    if state.state in (PositionState.HELD_GESTATION, PositionState.HELD_ACTIVE, PositionState.PENDING_EXIT):
        state.held_bars += 1
        state.peak_price = max(state.peak_price, current_price)
        can_sell = (market != "china") or (state.held_bars >= cfg.min_holding_bars)
        loss_pct = (current_price / state.entry_price - 1.0) if state.entry_price > 0 else 0.0
        trailing_stop = (state.peak_price - cfg.atr_multiplier * atr) if (atr is not None and atr > 0) else -float("inf")
        is_stop = (
            (loss_pct <= -cfg.hard_stop_loss_pct)
            or (atr is not None and atr > 0 and current_price < trailing_stop)
            or is_emergency_halt
        )

        # (2026-10-22 incident, follow-up safety fix) The min-holding tenure lock
        # (`can_sell`) is a 2-bar anti-whipsaw guard for SIGNAL-driven exits and
        # partial trims — it must NEVER block a risk exit. A China position
        # bought yesterday that trips its hard stop / trailing stop / Tier-3
        # emergency halt today must be liquidated today; otherwise the lock
        # converts a bounded stop into an unbounded tail loss. The lock is
        # therefore EXEMPTED here for the stop/emergency-halt fast path, and the
        # override is logged so a lock bypass is visible in live runs.
        if is_stop and cfg.enable_exit_fast_path:
            if not can_sell:
                _LOGGER.warning(
                    "tenure lock overridden by risk exit for %s: held_bars=%d < min_holding_bars=%d "
                    "(stop-loss / emergency-halt liquidation must never be blocked by the anti-whipsaw lock)",
                    state.symbol, state.held_bars, cfg.min_holding_bars,
                )
            state.state = PositionState.COOLDOWN
            state.cooldown_remaining = cfg.stop_cooldown_bars
            if is_emergency_halt:
                state.last_exit_type = ExitType.CIRCUIT_BREAKER.value
                stop_reason = "触发账户熔断保护强制平仓"
            elif loss_pct <= -cfg.hard_stop_loss_pct:
                state.last_exit_type = ExitType.HARD_STOP.value
                stop_reason = f"触发个股 -{cfg.hard_stop_loss_pct:.0%} 硬止损风控 (当前价格 {current_price:.2f} 较成本 {state.entry_price:.2f} 跌幅 {loss_pct:.1%} <= -{cfg.hard_stop_loss_pct:.1%})"
            elif atr is not None and atr > 0 and current_price < trailing_stop:
                state.last_exit_type = ExitType.TRAILING_STOP.value
                stop_reason = f"触发个股 ATR 跟踪止损风控 (当前价格 {current_price:.2f} < 跟踪止损价 {trailing_stop:.2f})"

            detail = {
                "loss_pct": loss_pct,
                "current_price": current_price,
                "entry_price": state.entry_price,
                "peak_price": state.peak_price,
                "atr": atr,
                "trailing_stop": trailing_stop,
                "is_emergency_halt": is_emergency_halt,
                "last_exit_type": state.last_exit_type,
            }
            state.held_bars = 0
            state.entry_streak = 0
            state.exit_streak = 0
            state.entry_price = 0.0
            state.peak_price = 0.0
            state.current_weight = 0.0
            return LifecycleDecision(
                action="SELL",
                target_weight=0.0,
                reason=stop_reason,
                detail_reason=detail,
                state_before=state_before,
                state_after=state.state,
                fast_path_triggered="STOP_LOSS",
            )

        if raw_target_weight == 0.0:
            if can_sell:
                state.exit_streak += 1
                if state.exit_streak >= cfg.guard_latency_bars:
                    streak_count = state.exit_streak
                    state.state = PositionState.COOLDOWN
                    state.cooldown_remaining = cfg.normal_cooldown_bars
                    state.last_exit_type = ExitType.NORMAL.value
                    detail = {
                        "exit_streak": streak_count,
                        "guard_latency_bars": cfg.guard_latency_bars,
                        "last_exit_type": state.last_exit_type,
                    }
                    state.held_bars = 0
                    state.entry_streak = 0
                    state.exit_streak = 0
                    state.entry_price = 0.0
                    state.peak_price = 0.0
                    state.current_weight = 0.0
                    return LifecycleDecision(
                        action="SELL",
                        target_weight=0.0,
                        reason=f"策略模型平仓信号持续 {streak_count} 日确认生效",
                        detail_reason=detail,
                        state_before=state_before,
                        state_after=state.state,
                        fast_path_triggered=None,
                    )
                else:
                    state.state = PositionState.PENDING_EXIT
                    return LifecycleDecision(
                        action="HOLD_FILTERED",
                        target_weight=state.current_weight,
                        reason=f"平仓信号进入确认期 (第 {state.exit_streak}/{cfg.guard_latency_bars} 日待确认，防单日脉冲假平仓)",
                        detail_reason={
                            "exit_streak": state.exit_streak,
                            "guard_latency_bars": cfg.guard_latency_bars,
                        },
                        state_before=state_before,
                        state_after=state.state,
                        fast_path_triggered=None,
                        blocking_reason=BlockingReason.EXIT_CONFIRMING,
                    )
            else:
                return LifecycleDecision(
                    action="HOLD_FILTERED",
                    target_weight=state.current_weight,
                    reason=f"未满最低持仓周期 (持仓 {state.held_bars} 日 < 最低 {cfg.min_holding_bars} 日，锁定防洗盘)",
                    detail_reason={
                        "held_bars": state.held_bars,
                        "min_holding_bars": cfg.min_holding_bars,
                        "blocked_action": "full_exit",
                    },
                    state_before=state_before,
                    state_after=state.state,
                    fast_path_triggered=None,
                    blocking_reason=BlockingReason.TENURE_LOCKED,
                )
        else:
            state.exit_streak = 0
            if state.held_bars >= cfg.min_holding_bars:
                state.state = PositionState.HELD_ACTIVE

            # Signed delta (raw_target - current), unified with the signed
            # `inertia_check.delta_weight` convention emitted by
            # run_live_strategy.py. The ABSOLUTE value still gates inertia.
            delta_w = raw_target_weight - state.current_weight

            # Defense-in-depth: if raw_target_weight > state.current_weight or < 0.0, reset is_portfolio_deleverage = False
            if is_portfolio_deleverage:
                if not (delta_w < 0.0 and 0.0 <= raw_target_weight <= state.current_weight):
                    is_portfolio_deleverage = False

            # (2026-10-22 incident) Relative trim-side inertia hurdle: SELL
            # trims must clear both the absolute threshold AND a proportional
            # fraction of the current weight. BUY-direction rebalances keep the
            # absolute-only gate so rebalancing UP is never over-restricted.
            effective_threshold = cfg.inertia_threshold
            if delta_w < 0.0 and cfg.inertia_relative_ratio > 0.0:
                effective_threshold = max(
                    cfg.inertia_threshold,
                    cfg.inertia_relative_ratio * state.current_weight,
                )
            if not is_portfolio_deleverage and abs(delta_w) < effective_threshold:
                return LifecycleDecision(
                    action="HOLD",
                    target_weight=state.current_weight,
                    reason=f"调仓未超惰性阈值 (< {effective_threshold:.0%})",
                    detail_reason={
                        "delta_w": delta_w,
                        "inertia_threshold": cfg.inertia_threshold,
                        "effective_threshold": effective_threshold,
                        "inertia_relative_ratio": cfg.inertia_relative_ratio,
                        "raw_target_weight": raw_target_weight,
                        "current_weight": state.current_weight,
                    },
                    state_before=state_before,
                    state_after=state.state,
                    fast_path_triggered=None,
                    blocking_reason=BlockingReason.INERTIA,
                )
            else:
                action = "BUY" if raw_target_weight > state.current_weight else "SELL"
                if action == "SELL" and not can_sell and not is_portfolio_deleverage:
                    # (2026-10-22 live incident) `can_sell` (the min-holding
                    # tenure lock) was previously enforced ONLY on the stop-loss
                    # fast-path (line ~212) and the full-exit path
                    # (raw_target_weight == 0.0), NEVER on partial trims.
                    # 000999.SZ / 600900.SH were sold at held_bars=1 <
                    # min_holding_bars=2 because this branch emitted SELL
                    # without consulting `can_sell`. Enforce it here, mirroring
                    # the full-exit HOLD_FILTERED reason. BUY-direction trims
                    # (raw_target_weight > current_weight) remain allowed so
                    # re-balancing UP during the lock is not delayed.
                    return LifecycleDecision(
                        action="HOLD_FILTERED",
                        target_weight=state.current_weight,
                        reason=f"未满最低持仓周期 (持仓 {state.held_bars} 日 < 最低 {cfg.min_holding_bars} 日，锁定防洗盘)",
                        detail_reason={
                            "held_bars": state.held_bars,
                            "min_holding_bars": cfg.min_holding_bars,
                            "raw_target_weight": raw_target_weight,
                            "current_weight": state.current_weight,
                            "delta_w": delta_w,
                            "blocked_action": "partial_trim",
                        },
                        state_before=state_before,
                        state_after=state.state,
                        fast_path_triggered=None,
                        blocking_reason=BlockingReason.TENURE_LOCKED,
                    )
                # (2026-10-22 live incident, ticket-level audit artifact) The
                # mutation below stages the post-rebalance TARGET into
                # `state.current_weight`, which is what the backtester and the
                # next bar need. But the forensic `detail_reason` must describe
                # the PRE-TRADE broker weight, not the target: reading
                # `state.current_weight` AFTER the mutation made the ticket
                # self-contradictory — it recorded
                # `current_weight == raw_target_weight` while `delta_w` (computed
                # one line above from the pre-mutation weight) was non-zero.
                # 600660.SH emitted a ticket whose `lifecycle_decision` block
                # showed current_weight == raw_target_weight == 0.131807 next to
                # a non-zero delta_w, making the trim un-auditable by hand.
                # Snapshot the broker/pre-trade weight BEFORE mutating, so the
                # record reads: current_weight = broker weight,
                # raw_target_weight = target, delta_w = target - broker.
                # (`AssetLifecycleState.broker_weight` was the persisted-state
                # half of the same fix; this is the remaining ticket half.)
                prev_weight = state.current_weight
                state.current_weight = raw_target_weight
                trim_reason = "组合级去杠杆风控减仓" if is_portfolio_deleverage else "最优权重微调"
                detail_dict = {
                    "delta_w": delta_w,
                    "inertia_threshold": cfg.inertia_threshold,
                    "raw_target_weight": raw_target_weight,
                    "current_weight": prev_weight,
                }
                if is_portfolio_deleverage:
                    detail_dict["is_portfolio_deleverage"] = True
                return LifecycleDecision(
                    action=action,
                    target_weight=raw_target_weight,
                    reason=trim_reason,
                    detail_reason=detail_dict,
                    state_before=state_before,
                    state_after=state.state,
                    fast_path_triggered=None,
                )

    # 3. If not holding (FLAT, PENDING_ENTRY)
    if state.state in (PositionState.FLAT, PositionState.PENDING_ENTRY):
        entry_gate = cfg.min_entry_threshold if cfg.min_entry_threshold is not None else cfg.inertia_threshold
        if raw_target_weight > 0.0 and raw_target_weight < entry_gate:
            state.entry_streak = 0
            state.exit_streak = 0
            state.state = PositionState.FLAT
            return LifecycleDecision(
                action="HOLD_FILTERED",
                target_weight=0.0,
                reason=f"目标仓位未达最低建仓/调仓门槛 (目标 {raw_target_weight:.1%} < 门槛 {entry_gate:.0%}，防微量试错损耗)",
                detail_reason={
                    "delta_w": raw_target_weight,
                    "inertia_threshold": entry_gate,
                    "raw_target_weight": raw_target_weight,
                    "current_weight": 0.0,
                },
                state_before=state_before,
                state_after=state.state,
                fast_path_triggered=None,
                blocking_reason=BlockingReason.MIN_ENTRY_THRESHOLD,
            )

        if raw_target_weight > 0.0:
            is_vol = bool(
                current_volume is not None
                and ma20_volume is not None
                and ma20_volume > 0
                and (current_volume / ma20_volume) >= cfg.volume_surge_multiplier
            )
            is_breadth = bool(
                market_breadth is not None
                and market_breadth >= cfg.breadth_thrust_threshold
            )
            # Strict confluence: requires BOTH macro thrust (breadth >= 60%) AND individual asset volume surge (>= 1.5x),
            # or an extreme standalone volume breakout (>= 2.5x MA20).
            is_thrust_confluence = is_breadth and is_vol
            is_super_volume_surge = bool(
                current_volume is not None
                and ma20_volume is not None
                and ma20_volume > 0
                and (current_volume / ma20_volume) >= 2.5
            )
            if cfg.enable_entry_fast_path and (is_thrust_confluence or is_super_volume_surge):
                state.state = PositionState.HELD_GESTATION
                state.held_bars = 0
                state.entry_streak = cfg.guard_latency_bars
                state.exit_streak = 0
                state.entry_price = current_price
                state.peak_price = current_price
                state.current_weight = raw_target_weight
                vol_ratio = (current_volume / ma20_volume) if (current_volume and ma20_volume and ma20_volume > 0) else None
                fast_reason = (
                    f"宏观冲力爆量突破 (冲力生效且成交量放大 {vol_ratio:.1f}x)，豁免确认期即刻买入"
                    if vol_ratio is not None
                    else "放量突破冲力闪电建仓"
                )
                return LifecycleDecision(
                    action="BUY",
                    target_weight=raw_target_weight,
                    reason=fast_reason,
                    detail_reason={
                        "vol_ratio": vol_ratio,
                        "volume_surge_multiplier": cfg.volume_surge_multiplier,
                        "market_breadth": market_breadth,
                        "breadth_thrust_threshold": cfg.breadth_thrust_threshold,
                    },
                    state_before=state_before,
                    state_after=state.state,
                    fast_path_triggered="VOLUME_BREAKOUT",
                )

            state.entry_streak += 1
            if state.entry_streak >= cfg.guard_latency_bars:
                state.state = PositionState.HELD_GESTATION
                state.held_bars = 0
                state.exit_streak = 0
                state.entry_price = current_price
                state.peak_price = current_price
                state.current_weight = raw_target_weight
                return LifecycleDecision(
                    action="BUY",
                    target_weight=raw_target_weight,
                    reason=f"开仓/再入场信号持续 {state.entry_streak} 日确认生效",
                    detail_reason={
                        "entry_streak": state.entry_streak,
                        "guard_latency_bars": cfg.guard_latency_bars,
                        "target_weight": raw_target_weight,
                    },
                    state_before=state_before,
                    state_after=state.state,
                    fast_path_triggered=None,
                )
            else:
                state.state = PositionState.PENDING_ENTRY
                return LifecycleDecision(
                    action="HOLD_FILTERED",
                    target_weight=0.0,
                    reason=f"开仓/再入场信号进入确认期 (第 {state.entry_streak}/{cfg.guard_latency_bars} 日待确认，防脉冲假突破)",
                    detail_reason={
                        "entry_streak": state.entry_streak,
                        "guard_latency_bars": cfg.guard_latency_bars,
                    },
                    state_before=state_before,
                    state_after=state.state,
                    fast_path_triggered=None,
                    blocking_reason=BlockingReason.PENDING_ENTRY_CONFIRM,
                )
        else:
            # (2026-10-22 incident, Fix 3) A PENDING_ENTRY asset whose entry
            # signal vanishes used to return a bare `action="HOLD"` with reason
            # "空仓维持" -- byte-for-byte the SAME decision a never-signalled,
            # permanently-flat asset gets. Three consequences downstream in
            # scripts/run_live_strategy.py, all observed in the 601857.SH case:
            #   1. `cur_w == 0 and tgt_w == 0 and action == "HOLD"` was
            #      reclassified NO_POSITION and then filtered out of the order
            #      ticket entirely (`order_df[order_df["action"] != "NO_POSITION"]`),
            #      so a symbol whose confirmation period was CANCELLED mid-flight
            #      simply disappeared from the ticket with no trace;
            #   2. `entry_signal_streak` is persisted sparsely (only entries with
            #      streak > 0), so the reset became an ABSENT KEY rather than an
            #      explicit 0 -- unobservable in account_state.json;
            #   3. the pre-reset streak value was destroyed before anything could
            #      log it.
            # The ECONOMICS of this branch are correct and are unchanged: target
            # weight stays 0.0, both streaks are zeroed, and the state returns to
            # FLAT (a re-appearing signal must re-earn its confirmation bars).
            # Only the visibility changes: the transition now carries a distinct
            # CANCEL_PENDING label plus the lost streak, so the ticket can show a
            # record row. Backtester parity: step_position_lifecycle is NOT used
            # by backtester/ at all (grep confirms only the live runner and unit
            # tests call it), and even a hypothetical `action == "HOLD"` consumer
            # sees the same target_weight / state_after values here.
            was_pending_entry = state_before == PositionState.PENDING_ENTRY
            cancelled_streak = int(state.entry_streak) if was_pending_entry else 0
            state.entry_streak = 0
            state.exit_streak = 0
            state.state = PositionState.FLAT
            if was_pending_entry:
                return LifecycleDecision(
                    action="CANCEL_PENDING",
                    target_weight=0.0,
                    reason="待确认开仓信号消失，取消入场",
                    detail_reason={
                        "raw_target_weight": raw_target_weight,
                        "cancelled_entry_streak": cancelled_streak,
                        "guard_latency_bars": cfg.guard_latency_bars,
                    },
                    state_before=state_before,
                    state_after=state.state,
                    fast_path_triggered=None,
                    cancelled_entry_streak=cancelled_streak,
                )
            return LifecycleDecision(
                action="HOLD",
                target_weight=0.0,
                reason="空仓维持",
                detail_reason={
                    "raw_target_weight": raw_target_weight,
                },
                state_before=state_before,
                state_after=state.state,
                fast_path_triggered=None,
            )

    raise ValueError(f"Unhandled position lifecycle state: {state.state}")


def _live_param_resolvers() -> Tuple[Any, Any, Any, Any]:
    """The LIVE runner's parameter resolvers, imported -- never re-implemented.

    Root cause this helper exists for (F2): `resolve_position_lifecycle_config`
    used to read ONLY `lifecycle_<field>` keys, while the live runner reads the
    STRATEGY's own spellings (`guard_latency_bars`, `min_entry_bars`,
    `reentry_cooldown_bars`, `min_hold_bars`, `cfsb_inertia_relative_ratio`, the
    `min_weight_change` family, ...). A strategy carrying any of those therefore
    resolved to the DEFAULTS in a backtest -- the backtest silently modelled a
    different state machine than the one actually deployed, with no warning.

    The alias resolution delegates to `scripts/run_live_strategy`'s own
    functions (`resolve_blend_param`, `resolve_min_entry_bars`,
    `resolve_min_holding_bars`, `resolve_trade_inertia_threshold`) instead of
    copying their precedence rules, so the two engines cannot drift: e.g. the
    `_min_holding_bars` / `_min_hold_bars` suffix compatibility (`L2.39`) and the
    `cfsb_/crb_/ccsb_/cfssb_/cdhb_/blend_` prefix scan are picked up here for
    free, and a future fix to the live precedence applies to both.

    Imported LAZILY on purpose: `scripts.run_live_strategy` imports this module
    at module scope (`PositionLifecycleConfig`, `step_position_lifecycle`,
    `BlockingReason`, ...), so a module-level import here would be circular.
    `scripts.run_live_strategy` also repairs `sys.path` (repo root + `pipeline/`)
    on import, so the call works from any cwd.
    """
    from scripts.run_live_strategy import (
        resolve_blend_param,
        resolve_min_entry_bars,
        resolve_min_holding_bars,
        resolve_trade_inertia_threshold,
    )

    return (
        resolve_blend_param,
        resolve_min_entry_bars,
        resolve_min_holding_bars,
        resolve_trade_inertia_threshold,
    )


def resolve_position_lifecycle_config(
    params: Optional[Dict[str, Any]] = None,
    args: Any = None,
) -> PositionLifecycleConfig:
    """Resolves PositionLifecycleConfig by merging strategy parameters and CLI arguments.

    Defaults (ALIGNED WITH THE LIVE DEPLOYMENT, `scripts/run_live_strategy.py:1538-1573`):
        guard_latency_bars: 2
        min_holding_bars: 2
        normal_cooldown_bars: 2
        stop_cooldown_bars: 5
        inertia_threshold: 0.05
        inertia_relative_ratio: 0.0
        min_entry_threshold: None
        hard_stop_loss_pct: 0.08
        atr_multiplier: 4.5
        volume_surge_multiplier: 1.5
        breadth_thrust_threshold: 0.60
        enable_entry_fast_path: True
        enable_exit_fast_path: True

    The two defaults that used to DISAGREE with live (and are fixed here, F1)
    were `normal_cooldown_bars=1` (live: 2 -- `--reentry-cooldown-bars` default)
    and `inertia_relative_ratio=0.50` (live: 0.0, i.e. DISABLED everywhere except
    `chan_four_state_blend`, which opts in through
    `cfsb_inertia_relative_ratio`). A 0.50 default silently halved every trim
    below half the incumbent weight in the backtest only.

    `min_entry_threshold` is the ONE field that deliberately stays `None` here
    while the live ticket sets `0.05`: live derives it as
    `args.min_entry_threshold or resolve_blend_param(params, "entry_threshold")
    or <inertia threshold>` (line 1539), i.e. the raw entry gate inherits the
    turnover-inertia threshold. Adopting that here would silently TIGHTEN entry
    gating for every backtest that never set the field (0.0 < w < 0.05 would
    stop entering), which is a separate behavioural decision, not a
    default-alignment fix. Pass `min_entry_threshold` explicitly (params
    `lifecycle_min_entry_threshold` / CLI `--lifecycle-min-entry-threshold`) to
    opt in.

    KEY RESOLUTION (F2) -- first hit wins, per field:
      1. `args.lifecycle_<field>` (backtester CLI, `run_backtest.py:214-258`)
      2. `params["lifecycle_<field>"]` (explicit, unambiguous)
      3. live-runner ALIAS on the CLI namespace (`args.reentry_cooldown_bars`,
         `args.guard_latency_bars`, `args.min_entry_bars`, ...)
      4. the same alias in `params` (`reentry_cooldown_bars`,
         `min_hold_bars`, `min_entry_bars`, `cfsb_inertia_relative_ratio`,
         `min_weight_change`, ...)
      5. the live default

    Alias table (at least; the live resolvers cover far more spellings):
        normal_cooldown_bars  <- reentry_cooldown_bars
        stop_cooldown_bars    <- stop_cooldown_bars (bare) / max(5, normal)
        min_holding_bars      <- min_hold_bars (+ *_min_holding_bars suffix)
        guard_latency_bars    <- min_entry_bars (+ *_min_entry_bars suffix)
        inertia_threshold     <- min_weight_change / *_min_weight_change
        inertia_relative_ratio<- cfsb_inertia_relative_ratio (only when present)
    """
    p = params or {}
    (
        resolve_blend_param,
        resolve_min_entry_bars,
        resolve_min_holding_bars,
        resolve_trade_inertia_threshold,
    ) = _live_param_resolvers()

    def _get(field_name: str, default: Any, *aliases: str) -> Any:
        key = f"lifecycle_{field_name}"
        if args is not None:
            val = getattr(args, key, None)
            if val is not None:
                return val
        if p:
            val = p.get(key)
            if val is not None:
                return val
        if args is not None:
            for alias in aliases:
                val = getattr(args, alias, None)
                if val is not None:
                    return val
        if p:
            for alias in aliases:
                val = p.get(alias)
                if val is not None:
                    return val
        return default

    def _live_guard_latency_bars() -> int:
        """`scripts/run_live_strategy.py:1541-1547`, verbatim.

        NOTE the live chain is gated on three literal keys
        (`min_entry_bars` / `cfsb_min_entry_bars` / `cfssb_min_entry_bars`)
        before `resolve_min_entry_bars` (which itself scans any
        `*_min_entry_bars` suffix) is invoked; the same three-key gate is kept
        here so a params dict resolves to the SAME guard in both engines.
        """
        guard = 2
        if args is not None and getattr(args, "guard_latency_bars", None) is not None:
            guard = int(getattr(args, "guard_latency_bars"))
        if args is not None and getattr(args, "min_entry_bars", None) is not None:
            return resolve_min_entry_bars(p, args.min_entry_bars, default=guard)
        if p.get("guard_latency_bars") is not None:
            return max(1, int(p["guard_latency_bars"]))
        if any(k in p for k in ("min_entry_bars", "cfsb_min_entry_bars", "cfssb_min_entry_bars")):
            return resolve_min_entry_bars(p, None, default=guard)
        return guard

    guard_latency_bars = max(1, int(_get("guard_latency_bars", _live_guard_latency_bars())))
    # `resolve_min_holding_bars` IS the live resolution (including the
    # `min_hold_bars` / `*_min_hold_bars` suffix compatibility, L2.39), so no
    # alias list is layered on top of it.
    min_holding_bars = max(
        0,
        int(
            _get(
                "min_holding_bars",
                resolve_min_holding_bars(
                    p,
                    None if args is None else getattr(args, "min_holding_bars", None),
                    default=2,
                ),
            )
        ),
    )
    normal_cooldown_bars = max(
        0, int(_get("normal_cooldown_bars", 2, "reentry_cooldown_bars"))
    )
    # Live: `stop_cooldown_bars=max(5, reentry_cooldown_bars)` (line 1559) -- the
    # stop-loss cooling period can never be shorter than the voluntary one.
    # An explicit `lifecycle_stop_cooldown_bars` / bare `stop_cooldown_bars`
    # still wins over that floor.
    stop_cooldown_bars = max(
        0,
        int(
            _get(
                "stop_cooldown_bars",
                max(5, normal_cooldown_bars),
                "stop_cooldown_bars",
            )
        ),
    )
    # Live: `resolve_trade_inertia_threshold(params, default=0.05)` (line 1538),
    # which owns the `min_weight_change` / `*_min_weight_change` /
    # `*_min_weight_pct` spellings.
    inertia_threshold = float(
        _get("inertia_threshold", resolve_trade_inertia_threshold(p, default=0.05))
    )
    # Live: `resolve_blend_param(params, "inertia_relative_ratio", 0.0, float)`
    # (line 1565) -- 0.0 unless the strategy explicitly carries the key under
    # one of the blend prefixes (`cfsb_inertia_relative_ratio` for
    # chan_four_state_blend) or bare.
    inertia_relative_ratio = float(
        _get(
            "inertia_relative_ratio",
            resolve_blend_param(p, "inertia_relative_ratio", 0.0, float),
        )
    )
    min_entry_thresh = _get("min_entry_threshold", None)
    min_entry_threshold = float(min_entry_thresh) if min_entry_thresh is not None else None
    hard_stop_loss_pct = float(_get("hard_stop_loss_pct", 0.08))
    atr_multiplier = float(_get("atr_multiplier", 4.5))
    volume_surge_multiplier = float(_get("volume_surge_multiplier", 1.5))
    breadth_thrust_threshold = float(_get("breadth_thrust_threshold", 0.60))
    enable_entry_fast_path = bool(_get("enable_entry_fast_path", True))
    enable_exit_fast_path = bool(_get("enable_exit_fast_path", True))

    return PositionLifecycleConfig(
        guard_latency_bars=guard_latency_bars,
        min_holding_bars=min_holding_bars,
        normal_cooldown_bars=normal_cooldown_bars,
        stop_cooldown_bars=stop_cooldown_bars,
        inertia_threshold=inertia_threshold,
        inertia_relative_ratio=inertia_relative_ratio,
        min_entry_threshold=min_entry_threshold,
        hard_stop_loss_pct=hard_stop_loss_pct,
        atr_multiplier=atr_multiplier,
        volume_surge_multiplier=volume_surge_multiplier,
        breadth_thrust_threshold=breadth_thrust_threshold,
        enable_entry_fast_path=enable_entry_fast_path,
        enable_exit_fast_path=enable_exit_fast_path,
    )


def simulate_position_lifecycle(
    universe: Dict[str, pd.DataFrame],
    target_weights: pd.DataFrame,
    cfg: Optional[PositionLifecycleConfig] = None,
    market: str = "china",
    cash_proxy: Optional[str] = None,
    initial_states: Optional[Dict[str, Union[AssetLifecycleState, Dict[str, Any]]]] = None,
    current_volumes: Optional[pd.DataFrame] = None,
    ma20_volumes: Optional[pd.DataFrame] = None,
    market_breadths: Optional[pd.Series] = None,
    atrs: Optional[pd.DataFrame] = None,
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    """Pure causal simulation of position lifecycle state machine across time.

    Enforces entry latency confirmation, T+1 tenure locks, asymmetric cooldowns,
    and trailing / hard stop-loss risk exits.

    Adheres strictly to the SPARSE WEIGHTS CONTRACT:
    - On active rebalance days (where at least one asset triggers BUY or SELL):
      target weights are written for all active assets. Closed/exited assets are
      explicitly written as 0.0 (never left as NaN, preventing backtester ffill leaks).
    - If NO asset triggers a trade on date t (all HOLD/HOLD_FILTERED/COOLDOWN_BLOCKED):
      the entire row is left as all-NaN so the backtester does not rebalance.
    - If cash_proxy is provided, residual cash weight `max(0.0, 1.0 - sum(risky_w))`
      is assigned to cash_proxy on active rebalance days.

    CASH-PROXY DISCLOSURE (F5, accounting geometry is deliberately unchanged):
    the cash leg is EXCLUDED from `risky_symbols`, so it never enters the state
    machine -- it has no PENDING_ENTRY confirmation, no tenure lock, no cooldown
    and no stop-loss. It is therefore also not a "position" that can be
    de-risked by this function. What it IS, is an ordinary symbol to the
    backtester that consumes these weights: its rebalances still pay commission,
    slippage and the stamp/levy legs exactly like a risky asset, and its
    in-and-out turnover counts toward the reported turnover figure. In other
    words the idle-cash sleeve is lifecycle-exempt but NOT cost-exempt.
    Auto cash parking (the `max(0.0, 1.0 - sum(risky_w))` residual) only happens
    when `cash_proxy` is a column of `target_weights`; a caller that passes a
    proxy outside the universe gets a warning (see below) and no parking.

    Returns:
        (filtered_sparse_weights, lifecycle_records)
    """
    if cfg is None:
        cfg = resolve_position_lifecycle_config()

    if target_weights.empty:
        return target_weights.copy(), []

    # Identify symbols
    symbols = list(target_weights.columns)
    if cash_proxy is not None and cash_proxy not in symbols:
        # (F5) This used to be silent: the residual-cash parking below appended
        # a brand-new all-NaN column, which a caller that indexes columns by
        # POSITION (rather than by name) would either drop or misalign -- the
        # weights would then look like an un-invested book while the state
        # machine reported it had run. Warn ONCE per call instead of guessing:
        # the proxy has to be a real member of the universe for the sleeve to
        # be lifecycle-exempt *and* cost-bearing in the same ledger.
        warnings.warn(
            f"simulate_position_lifecycle: cash_proxy={cash_proxy!r} is not a column of "
            "target_weights, so automatic cash parking into it is inert (the residual is "
            "left to the caller's own cash handling). Pass the proxy as a universe/target "
            "column to have the idle-cash sleeve lifecycle-exempt but cost-bearing.",
            RuntimeWarning,
            stacklevel=2,
        )
    risky_symbols = [s for s in symbols if cash_proxy is None or s != cash_proxy]

    # Pre-extract price, volume, and ATR series
    closes: Dict[str, pd.Series] = {}
    vols: Dict[str, pd.Series] = {}
    ma20_v: Dict[str, pd.Series] = {}
    atr_map: Dict[str, pd.Series] = {}

    for sym in risky_symbols:
        df = universe.get(sym)
        if df is not None and not df.empty:
            if "Close" in df.columns:
                closes[sym] = pd.to_numeric(df["Close"], errors="coerce")
            if current_volumes is not None and sym in current_volumes.columns:
                vols[sym] = current_volumes[sym]
            elif "Volume" in df.columns:
                vols[sym] = pd.to_numeric(df["Volume"], errors="coerce")
            if ma20_volumes is not None and sym in ma20_volumes.columns:
                ma20_v[sym] = ma20_volumes[sym]
            elif sym in vols:
                ma20_v[sym] = vols[sym].rolling(20, min_periods=1).mean()
            if atrs is not None and sym in atrs.columns:
                atr_map[sym] = atrs[sym]
            elif "High" in df.columns and "Low" in df.columns and "Close" in df.columns:
                try:
                    from common.indicators import atr
                    atr_map[sym] = atr(df, 14)
                except Exception:
                    pass

    # Initialize states
    states: Dict[str, AssetLifecycleState] = {}
    if initial_states:
        for sym, st in initial_states.items():
            if isinstance(st, AssetLifecycleState):
                states[sym] = AssetLifecycleState.from_dict(st.to_dict())
            elif isinstance(st, dict):
                states[sym] = AssetLifecycleState.from_dict(st)
    for sym in risky_symbols:
        if sym not in states:
            states[sym] = AssetLifecycleState(symbol=sym)

    # Raw model targets carried forward (ffill) across dates
    raw_targets = target_weights.ffill().fillna(0.0)

    # Initialize output sparse weights with NaNs
    filtered_weights = pd.DataFrame(
        np.nan,
        index=target_weights.index,
        columns=target_weights.columns,
        dtype=float,
    )
    if cash_proxy is not None and cash_proxy not in filtered_weights.columns:
        filtered_weights[cash_proxy] = np.nan

    lifecycle_records: List[Dict[str, Any]] = []

    for t in target_weights.index:
        decisions: Dict[str, LifecycleDecision] = {}
        for sym in risky_symbols:
            st = states[sym]
            tgt_w = float(raw_targets.loc[t, sym]) if (t in raw_targets.index and sym in raw_targets.columns) else 0.0

            # Resolve price
            p_series = closes.get(sym)
            if p_series is not None and t in p_series.index:
                p_val = p_series.loc[t]
                if isinstance(p_val, pd.Series):
                    p_val = p_val.iloc[-1]
                price = float(p_val) if pd.notna(p_val) and p_val > 0 else 0.0
            else:
                price = 0.0
            if not (np.isfinite(price) and price > 0.0):
                dec = LifecycleDecision(
                    action="HOLD_FILTERED",
                    target_weight=float(st.current_weight),
                    reason="缺失有效价格：跳过评估，保持当前持仓权重与状态",
                    detail_reason={
                        "current_price": price,
                        "blocking_reason": BlockingReason.NO_PRICE.value,
                        "note": "缺失有效价格 (NO_PRICE): 状态与当前持仓权重保持不变",
                    },
                    state_before=st.state,
                    state_after=st.state,
                    fast_path_triggered=None,
                    blocking_reason=BlockingReason.NO_PRICE,
                )
                decisions[sym] = dec
                rec = dec.to_dict()
                rec["date"] = str(t)
                rec["symbol"] = sym
                lifecycle_records.append(rec)
                continue

            cur_v = None
            if sym in vols and t in vols[sym].index:
                v_val = vols[sym].loc[t]
                if isinstance(v_val, pd.Series):
                    v_val = v_val.iloc[-1]
                cur_v = float(v_val) if pd.notna(v_val) and v_val > 0 else None

            ma_v = None
            if sym in ma20_v and t in ma20_v[sym].index:
                mv_val = ma20_v[sym].loc[t]
                if isinstance(mv_val, pd.Series):
                    mv_val = mv_val.iloc[-1]
                ma_v = float(mv_val) if pd.notna(mv_val) and mv_val > 0 else None

            sym_atr = None
            if sym in atr_map and t in atr_map[sym].index:
                atr_val = atr_map[sym].loc[t]
                if isinstance(atr_val, pd.Series):
                    atr_val = atr_val.iloc[-1]
                sym_atr = float(atr_val) if pd.notna(atr_val) and atr_val > 0 else None

            breadth_val = None
            if market_breadths is not None and t in market_breadths.index:
                b_val = market_breadths.loc[t]
                if isinstance(b_val, pd.Series):
                    b_val = b_val.iloc[-1]
                breadth_val = float(b_val) if pd.notna(b_val) else None

            # Resolve market per asset
            asset_market = market
            if market == "auto":
                if sym.endswith(".SH") or sym.endswith(".SZ") or (sym.isdigit() and len(sym) == 6):
                    asset_market = "china"
                elif sym.endswith(".HK") or (sym.isdigit() and len(sym) in (4, 5)):
                    asset_market = "hk"
                else:
                    asset_market = "us"

            dec = step_position_lifecycle(
                state=st,
                raw_target_weight=tgt_w,
                current_price=price,
                cfg=cfg,
                market=asset_market,
                current_volume=cur_v,
                ma20_volume=ma_v,
                market_breadth=breadth_val,
                atr=sym_atr,
            )
            decisions[sym] = dec

            rec = dec.to_dict()
            rec["date"] = str(t)
            rec["symbol"] = sym
            lifecycle_records.append(rec)

        # Check if any asset triggered an actionable trade (BUY or SELL)
        any_trade = any(d.action in ("BUY", "SELL") for d in decisions.values())

        if any_trade:
            # Active rebalance day: write target weights for all active assets
            sum_risky = 0.0
            for sym in risky_symbols:
                st = states[sym]
                if st.is_holding():
                    w = float(st.current_weight)
                else:
                    # Closed / flat assets MUST write explicit 0.0
                    w = 0.0
                filtered_weights.loc[t, sym] = w
                sum_risky += w

            # If cash proxy is provided, assign residual cash
            if cash_proxy is not None:
                cash_w = max(0.0, 1.0 - sum_risky)
                filtered_weights.loc[t, cash_proxy] = cash_w

    return filtered_weights, lifecycle_records
