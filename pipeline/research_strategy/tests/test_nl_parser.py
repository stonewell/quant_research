"""Unit tests for Natural Language Strategy Parser (rs/nl_parser.py).

Verifies deterministic extraction of plain English strategy descriptions into ParsedStrategySpec.
100% offline and network-free.
"""

import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from research_strategy.rs.nl_parser import parse_plain_english_strategy
from research_strategy.rs.strategy import (
    CANONICAL_DUAL_MOMENTUM_TEXT,
    CANONICAL_VOLATILITY_MANAGED_TEXT,
)


def test_parse_rebalance_frequencies():
    spec_monthly = parse_plain_english_strategy("Rebalance monthly. Risky assets: SPY, QQQ.")
    assert spec_monthly.rebalance_freq_days == 21

    spec_weekly = parse_plain_english_strategy("Rebalance weekly. Risky assets: SPY, QQQ.")
    assert spec_weekly.rebalance_freq_days == 5

    spec_daily = parse_plain_english_strategy("Rebalance daily. Risky assets: SPY, QQQ.")
    assert spec_daily.rebalance_freq_days == 1

    spec_custom = parse_plain_english_strategy("Rebalance every 10 days. Risky assets: SPY, QQQ.")
    assert spec_custom.rebalance_freq_days == 10


def test_parse_universes_and_canary():
    text = (
        "Rebalance monthly. Canary assets: SPY, EEM, EFA. "
        "Offensive assets: SPY, QQQ, GLD. Defensive assets: TLT, AGG. Cash proxy BIL."
    )
    spec = parse_plain_english_strategy(text)
    assert spec.use_canary_logic is True
    assert set(spec.canary_universe) == {"SPY", "EEM", "EFA"}
    assert set(spec.offensive_universe) == {"SPY", "QQQ", "GLD"}
    assert set(spec.defensive_universe) == {"TLT", "AGG"}
    assert spec.cash_proxy == "BIL"


def test_parse_trend_gates_and_lookbacks():
    text = "Select top 3 assets from SPY, QQQ, GLD with Close > 100d SMA and 63d ROC > 0. Rebalance monthly."
    spec = parse_plain_english_strategy(text)
    assert spec.trend_sma_period == 100
    assert spec.trend_roc_lookback == 63
    assert spec.top_k == 3
    assert set(spec.risky_universe) == {"SPY", "QQQ", "GLD"}


def test_parse_allocation_schemes():
    spec_equal = parse_plain_english_strategy("Rebalance monthly. Risky assets: SPY, QQQ. Allocate equally.")
    assert spec_equal.allocation_scheme == "equal_weight"

    spec_inv_vol = parse_plain_english_strategy("Rebalance monthly. Risky assets: SPY, QQQ. Weight using 60d inverse volatility.")
    assert spec_inv_vol.allocation_scheme == "inverse_volatility"

    spec_vol_man = parse_plain_english_strategy("Rebalance monthly. Risky assets: SPY, QQQ. Apply volatility-managed scaling targeting 15% vol.")
    assert spec_vol_man.allocation_scheme == "volatility_managed"


def test_format_summary_output():
    text = "Rebalance monthly. Risky assets: SPY, QQQ. Select top 2 assets with Close > 200d SMA. Allocate equally."
    spec = parse_plain_english_strategy(text, name="Test Summary Strategy")
    summary = spec.format_summary()

    assert "TEST SUMMARY STRATEGY" in summary
    assert "Every 21 trading days" in summary
    assert "EQUAL_WEIGHT" in summary
    assert "Top 2 assets" in summary
    assert "Close > 200d SMA" in summary


def test_canonical_dual_momentum_text_parses_to_documented_lookbacks():
    # Regression test: the generic momentum-lookback sweep used to grab
    # EVERY "Xd" mention in the text and take min()/max() across all of
    # them, conflating the volatility-sizing lookback ("60d inverse
    # volatility") with the momentum lookbacks ("63d and 126d momentum") --
    # since 60 < 63, it silently became the "short momentum lookback",
    # discarding the real 63d entirely.
    spec = parse_plain_english_strategy(CANONICAL_DUAL_MOMENTUM_TEXT)
    assert spec.mom_short_lookback == 63
    assert spec.mom_long_lookback == 126
    assert spec.vol_lookback == 60


def test_explicit_defensive_universe_includes_cash_proxy_ticker():
    # Regression test: an explicit defensive-universe clause can legitimately list the
    # cash-proxy ticker as a rankable candidate (it can win a top-3 slot on its own momentum,
    # not just serve as the passive fallback for unallocated capital), but the ticker extractor
    # used to unconditionally strip the cash-proxy symbol out of every extracted universe,
    # including this one.
    text = (
        "Rebalance monthly. Canary assets: SPY, EEM. Offensive assets: SPY, QQQ. "
        "Defensive assets: TIP, IEF, TLT, BIL, AGG, DBC."
    )
    spec = parse_plain_english_strategy(text)
    assert set(spec.defensive_universe) == {"TIP", "IEF", "TLT", "BIL", "AGG", "DBC"}


def test_canonical_volatility_managed_text_parses_to_documented_values():
    # Regression test: var_lookback and target_vol used to never be parsed
    # from text at all (silently staying at their dataclass defaults, which
    # only coincidentally matched this canonical text's own numbers).
    spec = parse_plain_english_strategy(CANONICAL_VOLATILITY_MANAGED_TEXT)
    assert spec.var_lookback == 20
    assert spec.target_vol == 0.15
    assert spec.allocation_scheme == "volatility_managed"


def test_fully_specified_description_produces_no_parser_warnings():
    text = (
        "Rebalance monthly. Risky assets: SPY, QQQ, GLD. "
        "Apply trend gate: Close > 100d SMA and 63d ROC > 0. "
        "Select top 3 assets. Weight using 60d inverse volatility."
    )
    spec = parse_plain_english_strategy(text, name="Explicit Test Strategy")
    assert spec.warnings == []
    assert "Parser Warnings:" not in spec.format_summary()


def test_vague_description_produces_parser_warnings():
    spec = parse_plain_english_strategy("Just trade some stocks somehow.")
    assert any("classify strategy type" in w for w in spec.warnings)
    assert any("risky-universe" in w for w in spec.warnings)
    assert any("allocation-scheme" in w for w in spec.warnings)
    assert "Parser Warnings:" in spec.format_summary()


def test_sma_mentioned_without_period_warns_and_defaults():
    text = "Rebalance monthly. Risky assets: SPY, QQQ. Use SMA trend filter."
    spec = parse_plain_english_strategy(text, name="SMA Test")
    assert spec.trend_sma_period == 200
    assert any("trend_sma_period" in w for w in spec.warnings)


def test_roc_mentioned_without_lookback_warns_and_defaults():
    text = "Rebalance monthly. Risky assets: SPY, QQQ. Apply positive return trend gate."
    spec = parse_plain_english_strategy(text, name="ROC Test")
    assert spec.trend_roc_lookback == 126
    assert any("trend_roc_lookback" in w for w in spec.warnings)


def test_canary_universe_fallback_warns():
    text = "Rebalance monthly. Canary logic enabled with no assets specified."
    spec = parse_plain_english_strategy(text, name="Canary Fallback Test")
    assert spec.use_canary_logic is True
    assert any("canary-universe" in w for w in spec.warnings)
    assert any("offensive-universe" in w for w in spec.warnings)
    assert any("defensive-universe" in w for w in spec.warnings)


def test_canary_universe_fallback_leaves_lists_as_none_not_hardcoded_default():
    # Regression: a canary-logic description with no explicit tickers used to substitute
    # DEFAULT_BAA_CANARY/OFFENSIVE/DEFENSIVE (hardcoded lists) -- it must now leave these as
    # None instead, so generate_weights derives them from the runtime universe.
    text = "Rebalance monthly. Canary logic enabled with no assets specified."
    spec = parse_plain_english_strategy(text)
    assert spec.canary_universe is None
    assert spec.offensive_universe is None
    assert spec.defensive_universe is None


def test_baa_keller_json_description_parses_to_universe_derived_roles():
    # The actual strategies_config.json baa_keller entry's description must no longer name any
    # canary/offensive/defensive tickers -- confirms the JSON text itself achieves the
    # universe-derived default, not just a synthetic example string.
    import json
    import os

    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies_config.json"
    )
    with open(config_path) as f:
        config = json.load(f)
    spec = parse_plain_english_strategy(config["baa_keller"]["plain_english_description"])
    assert spec.use_canary_logic is True
    assert spec.canary_universe is None
    assert spec.offensive_universe is None
    assert spec.defensive_universe is None


def test_roc_explicit_day_unit_is_not_treated_as_months():
    # Regression test: "10d ROC > 0" has an EXPLICIT day unit, but the old
    # heuristic (`val if val > 12 else val * 21`) assumed any number <= 12
    # meant months and multiplied by 21, turning 10 into 210.
    spec = parse_plain_english_strategy("Trend gate: 10d ROC > 0. Risky assets: SPY, QQQ.")
    assert spec.trend_roc_lookback == 10


def test_roc_month_phrasing_still_applies_month_heuristic():
    # Confirms the legitimate month-phrased heuristic case wasn't broken by
    # the day-unit fix above: "3 month ROC" should still become 63 (3 * 21).
    spec = parse_plain_english_strategy("Trend gate: 3 month ROC > 0. Risky assets: SPY, QQQ.")
    assert spec.trend_roc_lookback == 63


def test_roc_bare_small_number_without_unit_still_uses_month_heuristic():
    # No explicit unit at all -- legacy heuristic (<=12 assumed months)
    # should still apply, matching pre-fix behavior for this phrasing.
    spec = parse_plain_english_strategy("Trend gate: 6 ROC > 0. Risky assets: SPY, QQQ.")
    assert spec.trend_roc_lookback == 126


def test_rsi_mentioned_in_description_is_not_captured_as_phantom_ticker():
    # Regression test: STOP_WORDS omitted common TA acronyms like RSI/ATR/
    # ADX/ETF/CAGR/MACD, so a description mentioning "RSI confirmation"
    # captured "RSI" itself as a spurious phantom ticker in risky_universe.
    text = "Rebalance monthly. Risky assets: SPY, QQQ with Close > 200d SMA and RSI confirmation."
    spec = parse_plain_english_strategy(text)
    assert "RSI" not in spec.risky_universe
    assert set(spec.risky_universe) == {"SPY", "QQQ"}


def test_target_vol_and_var_lookback_are_actually_parsed_not_just_defaults():
    # Same regression, isolated from the canonical text's coincidental
    # defaults: a DIFFERENT target/lookback than the dataclass default must
    # still be picked up correctly.
    text = "Rebalance monthly. Risky assets: SPY, QQQ. Apply 45-day volatility-managed inverse variance scaling targeting 8% annual volatility."
    spec = parse_plain_english_strategy(text)
    assert spec.var_lookback == 45
    assert spec.target_vol == pytest.approx(0.08)
