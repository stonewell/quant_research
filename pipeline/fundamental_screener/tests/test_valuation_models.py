"""Unit tests for fscreen/valuation_models.py (Snowball Valuation Engine F2.1, F2.2, F2.3).

All tests are 100% offline with zero network calls.
"""

from dataclasses import replace
import numpy as np
import pytest

from common.data import FundamentalMetrics
from fundamental_screener.fscreen.config import ScreenerConfig
from fundamental_screener.fscreen.valuation_models import (
    calc_model1_terminal_discount,
    calc_model2_growth_dividend_cap,
    classify_industry_and_terminal_pe,
    evaluate_snowball_valuation,
    evaluate_sell_trigger,
)


def test_model1_analytical_solution_vs_manual_calculation():
    """Verify Model 1 closed-form finite geometric series against manual year-by-year discounting."""
    e0 = 100.0
    g = 0.08
    r = 0.12
    d = 0.50
    pe_term = 15.0

    # Step-by-step manual discounting
    divs = [e0 * ((1.0 + g) ** t) * d / ((1.0 + r) ** t) for t in range(1, 6)]
    manual_discounted_divs = sum(divs)
    manual_terminal_mv = (e0 * ((1.0 + g) ** 5) * pe_term) / ((1.0 + r) ** 5)
    expected_manual_mv = manual_discounted_divs + manual_terminal_mv

    calc_mv = calc_model1_terminal_discount(
        net_profit_0=e0,
        growth_5yr=g,
        payout_ratio=d,
        terminal_pe=pe_term,
        discount_rate=r,
    )

    assert calc_mv == pytest.approx(expected_manual_mv, rel=1e-6)
    assert calc_mv == pytest.approx(1475.060456, rel=1e-5)

    # Singularity edge case: g == r (rho == 1.0)
    mv_equal = calc_model1_terminal_discount(
        net_profit_0=100.0,
        growth_5yr=0.10,
        payout_ratio=0.40,
        terminal_pe=12.0,
        discount_rate=0.10,
    )
    # E0 * (PE_term + 5 * d) = 100 * (12 + 2) = 1400.0
    assert mv_equal == pytest.approx(1400.0)

    # Edge cases: non-positive earnings
    assert calc_model1_terminal_discount(-50.0, 0.08, 0.50, 15.0, 0.12) == 0.0
    assert calc_model1_terminal_discount(0.0, 0.08, 0.50, 15.0, 0.12) == 0.0


def test_model2_singularity_guard_and_growth_cap():
    """Verify Model 2 singularity guard (max(r - g, 0.015)) and terminal PE capitalization cap."""
    e0 = 100.0
    d = 0.50
    pe_term = 15.0

    # 1. Normal regime: g < r
    mv_normal, ret_normal = calc_model2_growth_dividend_cap(
        net_profit_0=e0,
        growth_5yr=0.08,
        payout_ratio=d,
        discount_rate=0.12,
        terminal_pe=pe_term,
    )
    # Div_0 = 50, Spread = 0.04, Raw MV = 1250, Cap = 100 * 1.08 * 15 = 1620 -> MV = 1250
    assert mv_normal == pytest.approx(1250.0)
    assert ret_normal == pytest.approx(0.08 + (50.0 / 1250.0))  # 0.12

    # 2. Singularity regime: g >= r (e.g. g=0.14 >= r=0.12)
    mv_sing, ret_sing = calc_model2_growth_dividend_cap(
        net_profit_0=e0,
        growth_5yr=0.14,
        payout_ratio=d,
        discount_rate=0.12,
        terminal_pe=pe_term,
    )
    # Spread is clamped to 0.015, Raw MV = 50 / 0.015 = 3333.33
    # Cap = 100 * 1.14 * 15 = 1710.0 -> MV = 1710.0 (Cap binds safely)
    assert mv_sing == pytest.approx(1710.0)
    assert ret_sing == pytest.approx(0.14 + (50.0 / 1710.0))
    assert mv_sing > 0 and not np.isnan(mv_sing) and not np.isinf(mv_sing)

    # 3. Non-positive profit
    assert calc_model2_growth_dividend_cap(-20.0, 0.08, 0.5, 0.12, 15.0) == (0.0, 0.0)


def test_industry_tier_mapping():
    """Verify 3-tier Snowball industry classification and terminal PE mapping."""
    # Tier 1: Light-asset consumer leaders (15.0x, range 13-17x)
    for ind in ["食品饮料", "白酒", "调味品", "Consumer Staples", "Beverages"]:
        tier, pe, is_bl, reason = classify_industry_and_terminal_pe(ind)
        assert tier == "tier1_consumer"
        assert pe == 15.0
        assert not is_bl
        assert reason is None

    # Tier 2: Hydropower & concession assets (13.5x, range 12-15x)
    for ind in ["水电", "高速公路", "港口", "Utilities", "Hydropower"]:
        tier, pe, is_bl, reason = classify_industry_and_terminal_pe(ind)
        assert tier == "tier2_concession"
        assert pe == 13.5
        assert not is_bl
        assert reason is None

    # Tier 3: Quality manufacturing & integrated resources (10.0x, range 8-12x)
    for ind in ["机械设备", "优质制造", "Machinery", "Integrated Resources", "Unknown"]:
        tier, pe, is_bl, reason = classify_industry_and_terminal_pe(ind)
        assert tier == "tier3_manufacturing"
        assert pe == 10.0
        assert not is_bl
        assert reason is None


def test_blacklist_hard_rejection():
    """Verify hard rejection of blacklisted sectors (financials, tech iteration, cyclicals, deteriorating, ST)."""
    # Financials (banks, insurance)
    for ind in ["银行", "股份制银行", "保险", "证券", "Commercial Banking", "Insurance"]:
        tier, pe, is_bl, reason = classify_industry_and_terminal_pe(ind)
        assert is_bl is True
        assert "Financial sector" in reason

    # Tech iteration (AI, chips, biotech, software)
    for ind in ["半导体", "芯片设计", "人工智能", "创新药", "软件开发", "Semiconductor", "Biotech"]:
        tier, pe, is_bl, reason = classify_industry_and_terminal_pe(ind)
        assert is_bl is True
        assert "Tech iteration" in reason

    # Strong cyclical (steel, basic chemicals, mining)
    for ind in ["钢铁", "基础化工", "有色金属", "煤炭开采", "Steel", "Mining"]:
        tier, pe, is_bl, reason = classify_industry_and_terminal_pe(ind)
        assert is_bl is True
        assert "Strong cyclical" in reason

    # Deteriorating landscape (real estate, interior construction, overcapacity solar/battery)
    for ind in ["房地产开发", "建筑装饰", "光伏设备", "锂电池", "Real Estate"]:
        tier, pe, is_bl, reason = classify_industry_and_terminal_pe(ind)
        assert is_bl is True
        assert "Deteriorating landscape" in reason

    # Special treatment (ST / delisting risk)
    tier, pe, is_bl, reason = classify_industry_and_terminal_pe("食品饮料", symbol="*ST600000")
    assert is_bl is True
    assert "Special treatment" in reason


def _sample_metrics(
    symbol="600519",
    industry_name="食品饮料",
    net_profit=100.0,
    growth=0.08,
    payout=0.50,
    roe=0.30,
    cfo_ratio=1.10,
    debt_assets=0.20,
    market_cap=1200.0,
    total_shares=10.0,
    is_blacklisted=False,
) -> FundamentalMetrics:
    return FundamentalMetrics(
        symbol=symbol,
        report_year=2024,
        stat_date="2024-12-31",
        roe=roe,
        net_profit=net_profit,
        net_profit_5yr_cagr=growth,
        cfo=net_profit * cfo_ratio,
        cfo_to_net_profit=cfo_ratio,
        total_assets=500.0,
        total_liabilities=500.0 * debt_assets,
        debt_to_assets=debt_assets,
        debt_to_equity=25.0,
        interest_bearing_debt=0.0,
        dividend_yield=0.03,
        dividend_payout_ratio=payout,
        pe_ttm=market_cap / net_profit,
        pb=5.0,
        total_shares=total_shares,
        market_cap=market_cap,
        industry_name=industry_name,
        industry_tier="tier1_consumer",
        terminal_pe=15.0,
        is_blacklisted=is_blacklisted,
    )


def test_buy_target_price_and_mos_ratio_calculation():
    """Verify conservative buy target price and Margin of Safety ratio calculation."""
    metrics = _sample_metrics(market_cap=1200.0, total_shares=10.0)
    cfg = ScreenerConfig(discount_rate=0.12, margin_of_safety=0.15)

    res = evaluate_snowball_valuation(metrics, cfg)

    # Model 1 MV ~= 1475.06, Model 2 MV = 1250.0 -> Fair MV = 1250.0
    assert res["model1_mv"] == pytest.approx(1475.06, rel=1e-3)
    assert res["model2_mv"] == pytest.approx(1250.0, rel=1e-3)
    assert res["fair_mv"] == pytest.approx(1250.0, rel=1e-3)

    # Buy MV = 1250.0 * (1 - 0.15) = 1062.5
    assert res["buy_mv"] == pytest.approx(1062.5, rel=1e-3)

    # Buy Price = 1062.5 / 10.0 = 106.25
    assert res["buy_price"] == pytest.approx(106.25, rel=1e-3)

    # MoS ratio = (1250.0 - 1200.0) / 1250.0 = 50 / 1250 = 0.04 (4%)
    assert res["mos_ratio"] == pytest.approx(0.04, rel=1e-3)


def test_sell_trigger_when_expected_return_below_benchmark_or_quality_fails():
    """Verify dual sell triggers: fundamental quality degradation or return edge decay."""
    cfg = ScreenerConfig(min_roe=0.15, min_cfo_to_profit=0.80, max_debt_to_assets=0.60)
    healthy_metrics = _sample_metrics()

    # Case 1: Healthy asset with superior return -> Do NOT sell
    should_sell, reason = evaluate_sell_trigger(
        healthy_metrics, exp_return_fwd=0.12, benchmark_return=0.07, cfg=cfg
    )
    assert should_sell is False
    assert reason == ""

    # Case 2: Return edge decay -> Sell
    should_sell, reason = evaluate_sell_trigger(
        healthy_metrics, exp_return_fwd=0.05, benchmark_return=0.07, cfg=cfg
    )
    assert should_sell is True
    assert "Return edge decay" in reason

    # Case 3: Quality failure - Low ROE -> Sell
    low_roe_metrics = replace(healthy_metrics, roe=0.08)
    should_sell, reason = evaluate_sell_trigger(
        low_roe_metrics, exp_return_fwd=0.12, benchmark_return=0.07, cfg=cfg
    )
    assert should_sell is True
    assert "ROE" in reason

    # Case 4: Quality failure - Cash flow collapse -> Sell
    bad_cfo_metrics = replace(healthy_metrics, cfo_to_net_profit=0.50, cfo=50.0)
    should_sell, reason = evaluate_sell_trigger(
        bad_cfo_metrics, exp_return_fwd=0.12, benchmark_return=0.07, cfg=cfg
    )
    assert should_sell is True
    assert "CFO" in reason

    # Case 5: Quality failure - Excessive leverage -> Sell
    high_debt_metrics = replace(healthy_metrics, debt_to_assets=0.85)
    should_sell, reason = evaluate_sell_trigger(
        high_debt_metrics, exp_return_fwd=0.12, benchmark_return=0.07, cfg=cfg
    )
    assert should_sell is True
    assert "Debt-to-assets" in reason

    # Case 6: Blacklist trigger -> Sell
    bl_metrics = replace(healthy_metrics, is_blacklisted=True, blacklist_reason="Semiconductor tech iteration")
    should_sell, reason = evaluate_sell_trigger(
        bl_metrics, exp_return_fwd=0.15, benchmark_return=0.07, cfg=cfg
    )
    assert should_sell is True
    assert "blacklisted" in reason
