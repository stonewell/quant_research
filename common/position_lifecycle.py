"""Unified Position Lifecycle State Machine (UPLSM).

Formalizes the lifecycle of every portfolio asset into a deterministic,
observable state machine preventing whipsaws, locking T+1 gestation tenures,
enforcing asymmetric re-entry cooldowns, and filtering micro-rebalance churn
across both backtesting and live execution engines.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


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
    """Configuration parameters for asset position lifecycles."""
    guard_latency_bars: int = 2
    min_holding_bars: int = 1
    normal_cooldown_bars: int = 1
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
            if abs(delta_w) < effective_threshold:
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
                if action == "SELL" and not can_sell:
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
                return LifecycleDecision(
                    action=action,
                    target_weight=raw_target_weight,
                    reason="最优权重微调",
                    detail_reason={
                        "delta_w": delta_w,
                        "inertia_threshold": cfg.inertia_threshold,
                        "raw_target_weight": raw_target_weight,
                        "current_weight": prev_weight,
                    },
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
