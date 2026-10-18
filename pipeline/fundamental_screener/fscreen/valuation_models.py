"""Snowball Valuation Engine (F2.1, F2.2, F2.3).

Grounding: docs/snowball_strategy.txt (conservative value-investing framework).
Features:
- Three-tier conservative industry terminal P/E classification.
- Strict blacklist filtering (financials, tech iteration, cyclicals, deteriorating landscape, ST/delisting).
- Model 1: Closed-form analytical terminal PE discount model (finite geometric series).
- Model 2: Growth-dividend capitalization model with singularity guard (max(r - g, 0.015)) and terminal P/E cap.
- Margin of safety (MoS) discount and buy target price evaluation.
- Dual-trigger sell decision engine (fundamental quality failure + benchmark return edge decay).
"""

from typing import Optional, Tuple, Dict, Any
import numpy as np
import pandas as pd

from common.data import FundamentalMetrics
from .config import ScreenerConfig


_BLACKLIST_FINANCIALS = [
    "银行", "保险", "证券", "多元金融", "金融", "信托",
    "Bank", "Banking", "Insurance", "Financial", "Brokerage",
]

_BLACKLIST_TECH_ITERATION = [
    "半导体", "芯片", "人工智能", "AI", "创新药", "生物科技", "软件", "计算机", "IT服务",
    "Semiconductor", "Biotech", "Software", "Information Technology", "Chip",
]

_BLACKLIST_CYCLICALS = [
    "钢铁", "基础化工", "化工", "有色金属", "采矿", "煤炭", "石油石化", "石油", "普通有色",
    "Steel", "Commodity Chemicals", "Mining", "Metals & Mining", "Oil & Gas",
]

_BLACKLIST_DETERIORATING = [
    "房地产", "地产", "建筑装饰", "装修", "光伏", "太阳能", "储能", "锂电池", "电池",
    "Real Estate", "Residential Construction", "Photovoltaic", "Solar",
]

_TIER1_CONSUMER = [
    "食品饮料", "白酒", "调味品", "乳制品", "家用电器", "医药商业", "品牌中药",
    "消费零售", "纺织服装", "商贸零售", "美容护理", "社会服务", "旅游",
    "Consumer Staples", "Beverages", "Food Products", "Household Products",
    "Personal Products", "Consumer", "Retail",
]

_TIER2_CONCESSION = [
    "水电", "水利发电", "铁路", "公路", "高速公路", "港口", "机场", "燃气", "供水",
    "污水处理", "特许经营", "公用事业",
    "Utilities", "Hydropower", "Water Utilities", "Toll Roads", "Airports",
    "Ports", "Infrastructure", "Concession",
]

_TIER3_MANUFACTURING = [
    "机械设备", "优质制造", "制造", "电力设备", "汽车零部件", "轻工制造", "包装印刷",
    "综合", "资源龙头",
    "Capital Goods", "Machinery", "Industrial", "Manufacturing", "Integrated Resources",
]


def classify_industry_and_terminal_pe(
    industry_name: str, symbol: str = ""
) -> Tuple[str, float, bool, Optional[str]]:
    """Classifies an asset into a Snowball terminal PE tier or hard blacklist.

    Returns:
        (tier_name, terminal_pe, is_blacklisted, blacklist_reason)
    """
    ind = industry_name or ""
    sym = symbol or ""

    # 1. Delisting / Special treatment check
    if "*ST" in sym or "ST" in sym or "退" in sym:
        return "blacklisted", 0.0, True, f"Special treatment / delisting risk ({sym})"

    # 2. Hard Rejection Categories
    for kw in _BLACKLIST_FINANCIALS:
        if kw.lower() in ind.lower():
            return (
                "tier_financials",
                0.0,
                True,
                "Financial sector rejected (opaque asset quality / reliance on accounting estimates)",
            )

    for kw in _BLACKLIST_TECH_ITERATION:
        if kw.lower() in ind.lower():
            return (
                "tier_tech_iteration",
                0.0,
                True,
                "Tech iteration sector rejected (rapid obsolescence / unpredictable 5-year earnings)",
            )

    for kw in _BLACKLIST_CYCLICALS:
        if kw.lower() in ind.lower():
            return (
                "tier_cyclicals",
                0.0,
                True,
                "Strong cyclical sector rejected (earnings peak at cycle top / erratic 5-year cash flow)",
            )

    for kw in _BLACKLIST_DETERIORATING:
        if kw.lower() in ind.lower():
            return (
                "tier_deteriorating",
                0.0,
                True,
                "Deteriorating landscape rejected (severe overcapacity / structural margin decline)",
            )

    # 3. Valid Snowball Tiers
    for kw in _TIER1_CONSUMER:
        if kw.lower() in ind.lower():
            return "tier1_consumer", 15.0, False, None

    for kw in _TIER2_CONCESSION:
        if kw.lower() in ind.lower():
            return "tier2_concession", 13.5, False, None

    for kw in _TIER3_MANUFACTURING:
        if kw.lower() in ind.lower():
            return "tier3_manufacturing", 10.0, False, None

    # Default conservative baseline: Tier 3 quality manufacturing
    return "tier3_manufacturing", 10.0, False, None


def calc_model1_terminal_discount(
    net_profit_0: float,
    growth_5yr: float,
    payout_ratio: float,
    terminal_pe: float,
    discount_rate: float,
) -> float:
    """Closed-form analytical terminal PE discount model (docs/snowball_strategy.txt Model 1).

    Formula:
        rho = (1 + g) / (1 + r)
        If rho != 1:
            MV_1 = E_0 * [ rho^5 * PE_term + d * (rho * (1 - rho^5) / (1 - rho)) ]
        If rho == 1:
            MV_1 = E_0 * [ PE_term + 5 * d ]
    """
    if net_profit_0 <= 0 or terminal_pe <= 0 or discount_rate <= -1.0:
        return 0.0

    payout = float(np.clip(payout_ratio, 0.0, 1.0))
    rho = (1.0 + growth_5yr) / (1.0 + discount_rate)

    if abs(rho - 1.0) < 1e-9:
        mv1 = net_profit_0 * (terminal_pe + 5.0 * payout)
    else:
        rho_5 = rho ** 5
        discounted_dividends = payout * (rho * (1.0 - rho_5)) / (1.0 - rho)
        mv1 = net_profit_0 * (rho_5 * terminal_pe + discounted_dividends)

    return float(max(mv1, 0.0))


def calc_model2_growth_dividend_cap(
    net_profit_0: float,
    growth_5yr: float,
    payout_ratio: float,
    discount_rate: float,
    terminal_pe: float,
) -> Tuple[float, float]:
    """Growth-dividend yield capitalization model with singularity guard & cap (docs/snowball_strategy.txt Model 2).

    Formula:
        Div_0 = net_profit_0 * payout_ratio
        Spread = max(discount_rate - growth_5yr, 0.015)  # Singularity guard
        Raw MV = Div_0 / Spread
        Cap = net_profit_0 * (1 + growth_5yr) * terminal_pe
        MV_2 = min(Raw MV, Cap)
        Expected return = growth_5yr + (Div_0 / MV_2)
    """
    if net_profit_0 <= 0 or terminal_pe <= 0:
        return 0.0, 0.0

    payout = float(np.clip(payout_ratio, 0.0, 1.0))
    div_0 = net_profit_0 * payout
    spread = max(discount_rate - growth_5yr, 0.015)
    raw_mv = div_0 / spread
    cap = net_profit_0 * (1.0 + growth_5yr) * terminal_pe
    mv_2 = float(max(min(raw_mv, cap), 0.0))

    if mv_2 > 0:
        expected_return = float(growth_5yr + (div_0 / mv_2))
    else:
        expected_return = 0.0

    return mv_2, expected_return


def evaluate_snowball_valuation(
    metrics: FundamentalMetrics,
    cfg: ScreenerConfig,
) -> Dict[str, Any]:
    """Evaluates intrinsic fair market value, buy target price, and margin of safety.

    Returns dict with keys:
        model1_mv, model2_mv, fair_mv, buy_mv, buy_price, mos_ratio,
        expected_return, terminal_pe, industry_tier, is_blacklisted, blacklist_reason
    """
    tier, term_pe, is_bl, bl_reason = classify_industry_and_terminal_pe(
        metrics.industry_name, metrics.symbol
    )

    is_blacklisted = metrics.is_blacklisted or (cfg.enable_industry_blacklist and is_bl)
    blacklist_reason = metrics.blacklist_reason or (bl_reason if cfg.enable_industry_blacklist else None)

    # Use explicit terminal_pe if set and not default, else classified tier terminal_pe
    terminal_pe = term_pe if (metrics.terminal_pe <= 0 or metrics.terminal_pe == 10.0) else metrics.terminal_pe

    # Growth rate capped at conservative maximum
    raw_growth = metrics.net_profit_5yr_cagr if not np.isnan(metrics.net_profit_5yr_cagr) else 0.05
    growth = float(min(raw_growth, cfg.max_earnings_growth))

    net_profit = float(metrics.net_profit) if (not np.isnan(metrics.net_profit) and metrics.net_profit > 0) else 0.0

    # Payout ratio resolution
    if not np.isnan(metrics.dividend_payout_ratio) and metrics.dividend_payout_ratio > 0:
        payout = metrics.dividend_payout_ratio
    elif not np.isnan(metrics.dividend_yield) and not np.isnan(metrics.pe_ttm) and metrics.pe_ttm > 0:
        payout = metrics.dividend_yield * metrics.pe_ttm
    elif not np.isnan(metrics.dividend_yield) and metrics.market_cap > 0 and net_profit > 0:
        payout = (metrics.dividend_yield * metrics.market_cap) / net_profit
    else:
        payout = 0.30
    payout = float(np.clip(payout, 0.0, cfg.max_dividend_payout))

    if is_blacklisted or net_profit <= 0:
        return {
            "model1_mv": 0.0,
            "model2_mv": 0.0,
            "fair_mv": 0.0,
            "buy_mv": 0.0,
            "buy_price": 0.0,
            "mos_ratio": 0.0,
            "expected_return": 0.0,
            "terminal_pe": terminal_pe,
            "industry_tier": tier,
            "is_blacklisted": is_blacklisted,
            "blacklist_reason": blacklist_reason,
        }

    model1_mv = calc_model1_terminal_discount(
        net_profit_0=net_profit,
        growth_5yr=growth,
        payout_ratio=payout,
        terminal_pe=terminal_pe,
        discount_rate=cfg.discount_rate,
    )

    model2_mv, exp_ret = calc_model2_growth_dividend_cap(
        net_profit_0=net_profit,
        growth_5yr=growth,
        payout_ratio=payout,
        discount_rate=cfg.discount_rate,
        terminal_pe=terminal_pe,
    )

    fair_mv = float(min(model1_mv, model2_mv))
    buy_mv = float(fair_mv * (1.0 - cfg.margin_of_safety))

    # Buy target price derivation
    if metrics.total_shares > 0 and not np.isnan(metrics.total_shares):
        buy_price = float(buy_mv / metrics.total_shares)
    elif metrics.market_cap > 0 and not np.isnan(metrics.market_cap):
        buy_price = float(buy_mv / metrics.market_cap)
    else:
        buy_price = 0.0

    # Margin of Safety ratio: (Fair MV - Current Market Cap) / Fair MV
    if fair_mv > 0 and metrics.market_cap > 0 and not np.isnan(metrics.market_cap):
        mos_ratio = float((fair_mv - metrics.market_cap) / fair_mv)
    else:
        mos_ratio = 0.0

    return {
        "model1_mv": model1_mv,
        "model2_mv": model2_mv,
        "fair_mv": fair_mv,
        "buy_mv": buy_mv,
        "buy_price": buy_price,
        "mos_ratio": mos_ratio,
        "expected_return": exp_ret,
        "terminal_pe": terminal_pe,
        "industry_tier": tier,
        "is_blacklisted": is_blacklisted,
        "blacklist_reason": blacklist_reason,
    }


def evaluate_sell_trigger(
    metrics: FundamentalMetrics,
    exp_return_fwd: float,
    benchmark_return: float,
    cfg: Optional[ScreenerConfig] = None,
) -> Tuple[bool, str]:
    """Dual-trigger sell evaluation: fundamental quality failure or benchmark return edge decay.

    Returns:
        (should_sell, reason)
    """
    config = cfg or ScreenerConfig()

    # 1. Hard Blacklist Trigger
    if metrics.is_blacklisted:
        return True, f"Hard rejection: blacklisted asset ({metrics.blacklist_reason or 'Blacklisted'})"

    # 2. Fundamental Quality Gate Failures
    if np.isnan(metrics.roe) or metrics.roe < config.min_roe:
        roe_val = metrics.roe if not np.isnan(metrics.roe) else 0.0
        return True, f"Quality gate failure: ROE ({roe_val:.2%}) below minimum hurdle ({config.min_roe:.2%})"

    if not np.isnan(metrics.net_profit_5yr_cagr) and metrics.net_profit_5yr_cagr < config.min_earnings_growth:
        return (
            True,
            f"Quality gate failure: 5-year profit CAGR ({metrics.net_profit_5yr_cagr:.2%}) below {config.min_earnings_growth:.2%}",
        )

    # Cash flow quality: CFO / Net Profit >= 0.80
    if not np.isnan(metrics.cfo_to_net_profit) and metrics.cfo_to_net_profit < config.min_cfo_to_profit:
        return (
            True,
            f"Quality gate failure: CFO/NetProfit ({metrics.cfo_to_net_profit:.2f}) below minimum {config.min_cfo_to_profit:.2f}",
        )
    elif (
        not np.isnan(metrics.cfo)
        and not np.isnan(metrics.net_profit)
        and metrics.net_profit > 0
        and (metrics.cfo < config.min_cfo_to_profit * metrics.net_profit)
    ):
        return (
            True,
            f"Quality gate failure: CFO ({metrics.cfo:.1f}) below {config.min_cfo_to_profit:.0%} of net profit ({metrics.net_profit:.1f})",
        )

    # Solvency / Leverage
    if not np.isnan(metrics.debt_to_assets) and metrics.debt_to_assets > config.max_debt_to_assets:
        return (
            True,
            f"Quality gate failure: Debt-to-assets ratio ({metrics.debt_to_assets:.1%}) exceeds maximum {config.max_debt_to_assets:.1%}",
        )
    elif not np.isnan(metrics.debt_to_equity) and metrics.debt_to_equity > config.max_debt_to_equity:
        return (
            True,
            f"Quality gate failure: Debt-to-equity ratio ({metrics.debt_to_equity:.1f}) exceeds maximum {config.max_debt_to_equity:.1f}",
        )

    # Dividend yield / payout
    if not np.isnan(metrics.dividend_yield) and metrics.dividend_yield <= config.min_dividend_yield:
        return (
            True,
            f"Quality gate failure: Dividend yield ({metrics.dividend_yield:.2%}) <= {config.min_dividend_yield:.2%}",
        )

    # 3. Return Edge Decay Trigger
    if exp_return_fwd < benchmark_return:
        return (
            True,
            f"Return edge decay: forward expected return ({exp_return_fwd:.2%}) below benchmark ({benchmark_return:.2%})",
        )

    return False, ""
