"""Multi-Strategy Order Netting & Virtual Ledger Engine.

Implements institutional internal crossing (内部对冲), order netting (撮合净额化),
friction savings auditing (税费磨损节约审计), and sub-strategy virtual ledger tracking.

Academic & Operational Grounding:
  When multiple quantitative sub-strategies (e.g. Tri-Core Blend: 40% Crisis Shield,
  35% Risk-Managed, 25% Four-State Shield) operate in a single master brokerage account,
  independent execution generates redundant contra-trades (one sub-strategy buying while
  another reduces the same symbol). In China A-share markets, executing contra-trades
  incurs 0.05% sell stamp duty, 0.03% broker commission (both sides), and 0.05% execution
  slippage (both sides) — totaling ~0.21% in pure churn friction bleed with zero net position change.

  This engine:
  1. Aggregates multi-strategy target weights into an optimal global master portfolio target:
     W_global[i] = sum(alpha_k * w_k[i])
  2. Synthesizes net share adjustments against master brokerage holdings:
     Delta_S_net[i] = S_target[i] - S_current[i]
  3. Detects and audits internal crossing:
     Crossed_Shares[i] = min(Gross_Buy_Shares[i], Gross_Sell_Shares[i])
     Quantifying exact savings in stamp duty, commission, and market impact slippage.
  4. Enforces global A-share execution hierarchy (Phase 1: NET SELLS -> Phase 2: NET BUYS ->
     Phase 3: NET HOLDS -> Phase 4: GLOBAL CASH SWEEP) with 100-share round lots.
  5. Maintains sub-strategy virtual ledgers so each sub-strategy's independent PnL and
     weight dynamics remain strictly auditable without brokerage account fragmentation.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from common.universe import get_stock_name, is_placeholder_stock_name


@dataclass
class SubStrategyIntent:
    """Representation of an individual sub-strategy's rebalance intent."""
    strategy_key: str
    strategy_name: str
    capital_weight: float
    allocated_capital: float
    target_weights: Dict[str, float]
    current_weights: Optional[Dict[str, float]] = None
    raw_orders: Optional[List[Dict[str, Any]]] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ContributingStrategyDetail:
    """Action of a specific sub-strategy on a symbol."""
    strategy_key: str
    strategy_name: str
    capital_weight: float
    target_weight: float
    current_weight: float
    delta_weight: float
    desired_shares: int
    action: str  # BUY, SELL, HOLD, HOLD_FILTERED


@dataclass
class CrossedSymbolDetail:
    """Internal crossing audit details for a single asset."""
    symbol: str
    name: str
    price: float
    gross_buy_shares: int
    gross_sell_shares: int
    net_shares: int
    crossed_shares: int  # min(gross_buy, gross_sell)
    gross_trade_value: float
    net_trade_value: float
    crossed_trade_value: float
    action: str  # NET_BUY, NET_SELL, NETTED_ZERO, HOLD
    contributing_strategies: List[Dict[str, Any]]


@dataclass
class FrictionSavings:
    """Quantified transaction cost savings achieved via internal netting."""
    total_gross_shares: int
    total_net_shares: int
    total_crossed_shares: int
    churn_reduction_ratio: float  # Percentage of gross volume eliminated via crossing
    total_gross_turnover: float
    total_net_turnover: float
    saved_turnover: float
    saved_stamp_duty: float    # 0.05% of crossed sells
    saved_commission: float    # 2 * 0.03% of crossed volume
    saved_slippage: float      # 2 * 0.05% of crossed volume
    total_friction_saved: float


@dataclass
class NettingResult:
    """Complete output of the order netting engine."""
    as_of_date: str
    portfolio_value: float
    cash_proxy: str
    net_orders: List[Dict[str, Any]]
    net_orders_df: pd.DataFrame
    crossed_details: List[CrossedSymbolDetail]
    friction_savings: FrictionSavings
    sub_ledgers: Dict[str, Any]
    summary: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "as_of_date": self.as_of_date,
            "portfolio_value": self.portfolio_value,
            "cash_proxy": self.cash_proxy,
            "net_orders": self.net_orders,
            "crossed_details": [asdict(c) for c in self.crossed_details],
            "friction_savings": asdict(self.friction_savings),
            "sub_ledgers": self.sub_ledgers,
            "summary": self.summary,
        }


class OrderNettingEngine:
    """Institutional multi-strategy netting and internal crossing engine."""

    def __init__(
        self,
        lot_size_default: int = 100,
        stamp_duty: float = 0.0005,      # 0.05% A-share sell stamp duty
        commission: float = 0.0003,      # 0.03% broker commission
        slippage: float = 0.0005,        # 0.05% execution slippage
        cash_proxy: str = "BIL",
    ):
        self.lot_size_default = lot_size_default
        self.stamp_duty = stamp_duty
        self.commission = commission
        self.slippage = slippage
        self.cash_proxy = cash_proxy

    def resolve_lot_size(self, symbol: str) -> int:
        """Resolve round lot size for symbol."""
        sym_clean = symbol.strip().upper()
        if sym_clean in (self.cash_proxy, "BIL", "CASH", "511880.SH", "GC001"):
            return 1
        if sym_clean.endswith(".SH") or sym_clean.endswith(".SZ") or sym_clean.endswith(".BJ"):
            return 100
        if sym_clean.endswith(".HK"):
            return 100  # Default HK standard lot fallback
        return 1  # US equities default lot is 1

    def net_portfolio_orders(
        self,
        intents: List[SubStrategyIntent],
        current_holdings: Dict[str, Any],
        portfolio_value: float,
        latest_prices: Dict[str, float],
        as_of_date: Optional[str] = None,
        holding_bars: Optional[Dict[str, int]] = None,
        min_holding_bars: int = 2,
        inertia_threshold: float = 0.05,
        symbol_names: Optional[Dict[str, str]] = None,
        prior_sub_ledgers: Optional[Dict[str, Any]] = None,
    ) -> NettingResult:
        """Performs multi-strategy target aggregation, internal crossing, and netting.

        Args:
            intents: List of individual sub-strategy rebalance intents with capital weights.
            current_holdings: Master account actual holdings {'SYMBOL': shares_or_weight, ...}.
            portfolio_value: Master account total NAV in RMB.
            latest_prices: Dict of {SYMBOL: latest_close_price}.
            as_of_date: Evaluation valuation date string (YYYY-MM-DD).
            holding_bars: Dict of {SYMBOL: continuous_holding_days}.
            min_holding_bars: Minimum tenure before exits are permitted (T+1 defense).
            inertia_threshold: Global weight change threshold below which trades are filtered.
            symbol_names: Optional mapping from ticker symbol to human Chinese company name.
            prior_sub_ledgers: Previous day virtual sub-ledgers for continuous tracking.

        Returns:
            NettingResult with net tickets, crossed details, friction savings, and sub-ledgers.
        """
        val_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
        names_map = symbol_names or {}
        bars_map = holding_bars or {}

        # 1. Normalize sub-strategy capital weights sum to 1.0
        total_strat_weight = sum(intent.capital_weight for intent in intents)
        if total_strat_weight <= 0:
            raise ValueError("Total sub-strategy capital weight must be positive.")
        normalized_intents: List[SubStrategyIntent] = []
        for intent in intents:
            norm_w = intent.capital_weight / total_strat_weight
            norm_cap = portfolio_value * norm_w
            normalized_intents.append(SubStrategyIntent(
                strategy_key=intent.strategy_key,
                strategy_name=intent.strategy_name,
                capital_weight=norm_w,
                allocated_capital=norm_cap,
                target_weights=dict(intent.target_weights),
                current_weights=dict(intent.current_weights or {}),
                raw_orders=intent.raw_orders,
                meta=dict(intent.meta),
            ))

        # 2. Gather all universe symbols across all strategies and master holdings
        all_symbols = set()
        for it in normalized_intents:
            all_symbols.update(it.target_weights.keys())
            all_symbols.update(it.current_weights.keys())
        for s in current_holdings.keys():
            clean_s = s.strip().upper()
            if clean_s not in (self.cash_proxy, "BIL", "CASH"):
                all_symbols.add(clean_s)
        equity_symbols = sorted(list(s for s in all_symbols if s not in (self.cash_proxy, "BIL", "CASH")))

        # 3. Resolve Master Account Current Holdings (Shares and Weights)
        master_current_shares: Dict[str, int] = {}
        master_current_weights: Dict[str, float] = {}
        for sym in equity_symbols:
            val = current_holdings.get(sym, 0)
            price = float(latest_prices.get(sym, 1.0))
            if isinstance(val, (int, np.integer)) or (isinstance(val, float) and val > 1.0):
                shares = int(val)
                cur_w = (shares * price) / portfolio_value if portfolio_value > 0 else 0.0
            else:
                cur_w = float(val)
                shares = int(np.round((cur_w * portfolio_value) / price)) if price > 0 else 0
            master_current_shares[sym] = max(0, shares)
            master_current_weights[sym] = cur_w

        # 4. Synthesize Global Master Target Weights: W_global[i] = sum(alpha_k * w_k[i])
        global_target_weights: Dict[str, float] = {}
        for sym in equity_symbols:
            w_sum = 0.0
            for it in normalized_intents:
                w_sub = float(it.target_weights.get(sym, 0.0))
                w_sum += it.capital_weight * w_sub
            global_target_weights[sym] = float(w_sum)

        # 5. Calculate Sub-Strategy Desired Trades (Gross Intentions per strategy)
        # Each strategy has a virtual target shares: round_down( (w_k * allocated_capital) / price )
        per_strat_shares: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for it in normalized_intents:
            strat_orders = {}
            for sym in equity_symbols:
                price = float(latest_prices.get(sym, 1.0))
                lot = self.resolve_lot_size(sym)
                tgt_w = float(it.target_weights.get(sym, 0.0))
                sub_tgt_val = tgt_w * it.allocated_capital
                sub_tgt_shares = int(np.floor(sub_tgt_val / price / lot) * lot) if price > 0 else 0

                # Prior virtual shares (from prior_sub_ledgers if available, else proportional)
                prior_sub_shares = 0
                if prior_sub_ledgers and it.strategy_key in prior_sub_ledgers:
                    prior_sub_shares = int(prior_sub_ledgers[it.strategy_key].get("holdings", {}).get(sym, 0))
                else:
                    # Initial launch: infer proportional share of master holdings
                    prior_sub_shares = int(np.round(master_current_shares.get(sym, 0) * it.capital_weight))

                delta_shares = sub_tgt_shares - prior_sub_shares
                strat_action = "HOLD"
                if delta_shares > 0:
                    strat_action = "BUY"
                elif delta_shares < 0:
                    strat_action = "SELL"

                strat_orders[sym] = {
                    "strategy_key": it.strategy_key,
                    "strategy_name": it.strategy_name,
                    "capital_weight": it.capital_weight,
                    "target_weight": tgt_w,
                    "current_weight": float(it.current_weights.get(sym, 0.0)),
                    "delta_weight": tgt_w - float(it.current_weights.get(sym, 0.0)),
                    "target_shares": sub_tgt_shares,
                    "current_shares": prior_sub_shares,
                    "delta_shares": delta_shares,
                    "action": strat_action,
                }
            per_strat_shares[it.strategy_key] = strat_orders

        # 6. Perform Internal Crossing Detection & Friction Savings Audit per symbol
        crossed_details: List[CrossedSymbolDetail] = []
        global_rows: List[Dict[str, Any]] = []

        total_gross_buy_shares = 0
        total_gross_sell_shares = 0
        total_crossed_shares = 0
        total_gross_turnover = 0.0
        total_net_turnover = 0.0
        total_crossed_turnover = 0.0
        crossed_sells_value = 0.0

        for sym in equity_symbols:
            price = float(latest_prices.get(sym, 1.0))
            lot = self.resolve_lot_size(sym)
            cur_master_shares = master_current_shares[sym]
            cur_master_w = master_current_weights[sym]
            tgt_master_w = global_target_weights[sym]

            # Collect sub-strategy actions on this symbol
            contribs: List[Dict[str, Any]] = []
            gross_buy = 0
            gross_sell = 0
            for it in normalized_intents:
                order_info = per_strat_shares[it.strategy_key][sym]
                contribs.append(order_info)
                d_sh = order_info["delta_shares"]
                if d_sh > 0:
                    gross_buy += d_sh
                elif d_sh < 0:
                    gross_sell += abs(d_sh)

            total_gross_buy_shares += gross_buy
            total_gross_sell_shares += gross_sell

            # Internal crossing: volume of orders that offset each other internally
            crossed_sh = min(gross_buy, gross_sell)
            total_crossed_shares += crossed_sh

            # Calculate Global Target Shares with Lot Rounding
            tgt_master_val = tgt_master_w * portfolio_value
            raw_tgt_shares = tgt_master_val / price if price > 0 else 0
            rounded_tgt_shares = int(np.floor(raw_tgt_shares / lot) * lot)

            # Master Account Net Delta Shares
            raw_delta_shares = rounded_tgt_shares - cur_master_shares
            delta_w = tgt_master_w - cur_master_w
            abs_delta_w = abs(delta_w)

            # Tenure Lock Check (T+1 holding tenure constraint on master account)
            held_age = bars_map.get(sym, min_holding_bars)
            is_tenure_locked = (held_age < min_holding_bars) and (cur_master_shares > 0)

            # Action Determination with Inertia and Tenure Filter
            action = "HOLD"
            reason = ""
            final_tgt_shares = rounded_tgt_shares
            final_delta_shares = raw_delta_shares

            if cur_master_shares == 0 and rounded_tgt_shares > 0:
                action = "BUY"
                reason = "多策略协同新开仓"
            elif rounded_tgt_shares == 0 and cur_master_shares > 0:
                if is_tenure_locked:
                    action = "HOLD_FILTERED"
                    final_tgt_shares = cur_master_shares
                    final_delta_shares = 0
                    reason = f"未满最低持仓周期 (持仓 {held_age} 日 < {min_holding_bars} 日，锁仓防洗盘)"
                else:
                    action = "SELL"
                    reason = "多策略协同全额清仓 (已满持有期)"
            elif abs_delta_w < 1e-6 or raw_delta_shares == 0:
                action = "HOLD"
                final_tgt_shares = cur_master_shares
                final_delta_shares = 0
                reason = "最优权重维持"
            elif abs_delta_w < inertia_threshold:
                action = "HOLD_FILTERED"
                final_tgt_shares = cur_master_shares
                final_delta_shares = 0
                reason = f"全局调仓未超惰性阈值 (< {inertia_threshold:.0%})"
            elif raw_delta_shares < 0:
                if is_tenure_locked:
                    action = "HOLD_FILTERED"
                    final_tgt_shares = cur_master_shares
                    final_delta_shares = 0
                    reason = f"未满最低持仓周期 (持仓 {held_age} 日 < {min_holding_bars} 日，锁仓防洗盘)"
                else:
                    action = "SELL"
                    reason = "多策略净额减仓"
            elif raw_delta_shares > 0:
                action = "BUY"
                reason = "多策略净额加仓"
            else:
                action = "HOLD"
                reason = "最优权重维持"

            trade_val = abs(final_delta_shares) * price
            gross_val = (gross_buy + gross_sell) * price
            crossed_val = crossed_sh * 2 * price
            crossed_sells_val = crossed_sh * price

            total_gross_turnover += gross_val
            total_net_turnover += trade_val
            total_crossed_turnover += crossed_val
            crossed_sells_value += crossed_sells_val

            # Resolve readable Chinese name
            sym_name = names_map.get(sym, sym)
            if is_placeholder_stock_name(sym_name, sym):
                sym_name = get_stock_name(sym, sym)

            crossed_detail = CrossedSymbolDetail(
                symbol=sym,
                name=sym_name,
                price=price,
                gross_buy_shares=gross_buy,
                gross_sell_shares=gross_sell,
                net_shares=abs(final_delta_shares),
                crossed_shares=crossed_sh,
                gross_trade_value=gross_val,
                net_trade_value=trade_val,
                crossed_trade_value=crossed_val,
                action=action,
                contributing_strategies=contribs,
            )
            crossed_details.append(crossed_detail)

            # Only emit row if held or targeted
            if cur_master_shares > 0 or final_tgt_shares > 0 or abs(final_delta_shares) > 0:
                global_rows.append({
                    "symbol": sym,
                    "name": sym_name,
                    "action": action,
                    "price": price,
                    "current_weight": cur_master_w,
                    "target_weight": (final_tgt_shares * price) / portfolio_value if portfolio_value > 0 else tgt_master_w,
                    "delta_weight": ((final_tgt_shares - cur_master_shares) * price) / portfolio_value if portfolio_value > 0 else delta_w,
                    "current_shares": cur_master_shares,
                    "target_shares": final_tgt_shares,
                    "delta_shares": final_delta_shares,
                    "trade_value": trade_val,
                    "trade_value_rmb": trade_val,
                    "gross_buy_shares": gross_buy,
                    "gross_sell_shares": gross_sell,
                    "crossed_shares": crossed_sh,
                    "holding_bars": held_age if cur_master_shares > 0 else 0,
                    "reason": reason,
                })

        # 7. Compute Quantified Friction & Tax Savings
        saved_stamp_duty = crossed_sells_value * self.stamp_duty
        saved_comm = (total_crossed_shares * 2 * price if False else total_crossed_turnover) * self.commission
        saved_slip = total_crossed_turnover * self.slippage
        total_friction_saved = saved_stamp_duty + saved_comm + saved_slip

        total_gross_shares = total_gross_buy_shares + total_gross_sell_shares
        total_net_shares = sum(abs(r["delta_shares"]) for r in global_rows)
        churn_red_ratio = (total_crossed_shares * 2.0 / total_gross_shares) if total_gross_shares > 0 else 0.0

        friction_savings = FrictionSavings(
            total_gross_shares=total_gross_shares,
            total_net_shares=total_net_shares,
            total_crossed_shares=total_crossed_shares,
            churn_reduction_ratio=churn_red_ratio,
            total_gross_turnover=total_gross_turnover,
            total_net_turnover=total_net_turnover,
            saved_turnover=total_crossed_turnover,
            saved_stamp_duty=saved_stamp_duty,
            saved_commission=saved_comm,
            saved_slippage=saved_slip,
            total_friction_saved=total_friction_saved,
        )

        # 8. Apply Global Master Purchasing Power Constraint
        # Net buys cannot exceed master initial cash + master sales proceeds
        initial_cash_val = float(current_holdings.get(self.cash_proxy, 0.0))
        if initial_cash_val <= 1.0:  # Weight representation
            initial_cash_val = initial_cash_val * portfolio_value
        else:
            initial_cash_val = float(initial_cash_val)

        total_sales_proceeds = sum(r["trade_value"] for r in global_rows if r["action"] == "SELL")
        available_purchasing_power = max(0.0, initial_cash_val + total_sales_proceeds)
        total_desired_buys = sum(r["trade_value"] for r in global_rows if r["action"] == "BUY")

        if total_desired_buys > available_purchasing_power and total_desired_buys > 0:
            buy_scale = available_purchasing_power / total_desired_buys
            for r in global_rows:
                if r["action"] == "BUY" and r["price"] > 0:
                    lot = self.resolve_lot_size(r["symbol"])
                    scaled_val = r["trade_value"] * buy_scale
                    scaled_shares = int(np.floor(scaled_val / r["price"] / lot) * lot)
                    r["delta_shares"] = max(0, scaled_shares)
                    r["target_shares"] = r["current_shares"] + r["delta_shares"]
                    r["trade_value"] = r["delta_shares"] * r["price"]
                    r["trade_value_rmb"] = r["trade_value"]
                    r["target_weight"] = (r["target_shares"] * r["price"]) / portfolio_value
                    r["delta_weight"] = r["target_weight"] - r["current_weight"]

        # 9. Append Cash Proxy Sweep / Release Row
        actual_equity_trades_val = sum(
            r["trade_value"] if r["action"] == "BUY" else (-r["trade_value"] if r["action"] == "SELL" else 0.0)
            for r in global_rows
        )
        post_trade_cash_val = max(0.0, initial_cash_val - actual_equity_trades_val)
        post_trade_cash_weight = post_trade_cash_val / portfolio_value if portfolio_value > 0 else 0.0
        cur_cash_weight = initial_cash_val / portfolio_value if portfolio_value > 0 else 0.0
        cash_delta_w = post_trade_cash_weight - cur_cash_weight

        cash_row = {
            "symbol": self.cash_proxy,
            "name": "现金及现金管理工具",
            "action": "SWEEP_CASH" if cash_delta_w > 0 else ("RELEASE_CASH" if cash_delta_w < 0 else "HOLD"),
            "price": 1.0,
            "current_weight": cur_cash_weight,
            "target_weight": post_trade_cash_weight,
            "delta_weight": cash_delta_w,
            "current_shares": int(np.round(initial_cash_val)),
            "target_shares": int(np.round(post_trade_cash_val)),
            "delta_shares": int(np.round(post_trade_cash_val - initial_cash_val)),
            "trade_value": abs(post_trade_cash_val - initial_cash_val),
            "trade_value_rmb": abs(post_trade_cash_val - initial_cash_val),
            "gross_buy_shares": 0,
            "gross_sell_shares": 0,
            "crossed_shares": 0,
            "holding_bars": 999,
            "reason": "多策略净额化流动性资金归集/释放",
        }
        global_rows.append(cash_row)

        # 10. Sort into Institutional Four-Phase Execution Priority
        def sort_priority(act: str) -> int:
            if act == "SELL":
                return 1  # Phase 1: SELLS FIRST
            if act == "BUY":
                return 2  # Phase 2: BUYS SECOND
            if act.startswith("SWEEP") or act.startswith("RELEASE"):
                return 4  # Phase 4: CASH SWEEP
            return 3      # Phase 3: HOLDS

        order_df = pd.DataFrame(global_rows)
        if not order_df.empty:
            order_df["priority"] = order_df["action"].map(sort_priority)
            order_df = order_df.sort_values(by=["priority", "delta_weight"]).drop(columns=["priority"])
            net_orders_list = order_df.to_dict(orient="records")
        else:
            net_orders_list = []

        # 11. Update Sub-Strategy Virtual Ledgers
        sub_ledgers = self._update_virtual_ledgers(
            normalized_intents=normalized_intents,
            per_strat_shares=per_strat_shares,
            latest_prices=latest_prices,
            portfolio_value=portfolio_value,
            prior_sub_ledgers=prior_sub_ledgers,
            as_of_date=val_date,
        )

        summary = {
            "as_of_date": val_date,
            "portfolio_value": portfolio_value,
            "n_strategies": len(normalized_intents),
            "strategies": [
                {"key": it.strategy_key, "name": it.strategy_name, "capital_weight": it.capital_weight}
                for it in normalized_intents
            ],
            "total_gross_shares": total_gross_shares,
            "total_net_shares": total_net_shares,
            "total_crossed_shares": total_crossed_shares,
            "churn_reduction_ratio": churn_red_ratio,
            "total_friction_saved_rmb": total_friction_saved,
            "saved_stamp_duty_rmb": saved_stamp_duty,
            "saved_commission_rmb": saved_comm,
            "saved_slippage_rmb": saved_slip,
            "net_sells_count": len([r for r in net_orders_list if r.get("action") == "SELL"]),
            "net_buys_count": len([r for r in net_orders_list if r.get("action") == "BUY"]),
            "post_trade_cash_rmb": post_trade_cash_val,
            "post_trade_cash_weight": post_trade_cash_weight,
        }

        return NettingResult(
            as_of_date=val_date,
            portfolio_value=portfolio_value,
            cash_proxy=self.cash_proxy,
            net_orders=net_orders_list,
            net_orders_df=order_df,
            crossed_details=crossed_details,
            friction_savings=friction_savings,
            sub_ledgers=sub_ledgers,
            summary=summary,
        )

    def _update_virtual_ledgers(
        self,
        normalized_intents: List[SubStrategyIntent],
        per_strat_shares: Dict[str, Dict[str, Dict[str, Any]]],
        latest_prices: Dict[str, float],
        portfolio_value: float,
        prior_sub_ledgers: Optional[Dict[str, Any]],
        as_of_date: str,
    ) -> Dict[str, Any]:
        """Maintains independent virtual accounting ledgers for each sub-strategy."""
        ledgers = {}
        for it in normalized_intents:
            s_key = it.strategy_key
            strat_orders = per_strat_shares[s_key]

            # Calculate post-rebalance virtual stock holdings
            sub_holdings: Dict[str, int] = {}
            sub_equity_val = 0.0
            for sym, o in strat_orders.items():
                sh = int(o["target_shares"])
                if sh > 0:
                    sub_holdings[sym] = sh
                    sub_equity_val += sh * float(latest_prices.get(sym, 1.0))

            sub_nav = it.allocated_capital
            sub_cash = max(0.0, sub_nav - sub_equity_val)

            ledgers[s_key] = {
                "strategy_key": s_key,
                "strategy_name": it.strategy_name,
                "capital_weight": it.capital_weight,
                "allocated_nav": sub_nav,
                "virtual_equity_value": sub_equity_val,
                "virtual_cash": sub_cash,
                "equity_ratio": sub_equity_val / sub_nav if sub_nav > 0 else 0.0,
                "cash_ratio": sub_cash / sub_nav if sub_nav > 0 else 0.0,
                "holdings": sub_holdings,
                "last_updated": as_of_date,
            }
        return ledgers
