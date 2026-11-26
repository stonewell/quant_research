"""Market-specific asset allocation profiles and resolution helpers.

Defines canonical VAA (Vigilant Asset Allocation) universe matrices for US,
China A-shares, and Hong Kong markets, along with authoritative market
resolution rules and VAA strategy detection.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from common.market_holidays import (
    MARKET_CHINA,
    MARKET_HK,
    MARKET_US,
    SUPPORTED_MARKETS,
    UnknownMarketError,
    normalize_market,
)
from common.market_clock import (
    MixedMarketUniverseError,
    UnknownSymbolMarketError,
    infer_market_from_symbols,
)
from common.allocation_backtester import CASH_PROXY_SYMBOLS

MARKET_VAA_PROFILES: Dict[str, Dict[str, Any]] = {
    MARKET_US: {
        "offensive_universe": ("SPY", "QQQ", "EFA", "EEM"),
        "external_canary_symbols": ("SPY", "QQQ", "EFA", "EEM"),
        "defensive_universe": ("IEF", "BIL"),
        "cash_proxy": "BIL",
    },
    MARKET_CHINA: {
        "offensive_universe": ("510300.SH", "510500.SH", "159915.SZ", "510050.SH"),
        "external_canary_symbols": ("000300.SH", "000905.SH", "399006.SZ"),
        "defensive_universe": ("511010.SH", "511880.SH"),
        "cash_proxy": "511880.SH",
    },
    MARKET_HK: {
        "offensive_universe": ("2800.HK", "2828.HK", "3033.HK"),
        "external_canary_symbols": ("2800.HK", "2828.HK", "3033.HK"),
        "defensive_universe": ("3199.HK", "3053.HK"),
        "cash_proxy": "3199.HK",
    },
}

_EXTENDED_CASH_PROXIES = frozenset(
    {s.upper() for s in CASH_PROXY_SYMBOLS} | {"3199", "3053", "CASH", "MONEY"}
)

_VAA_TEMPLATE_NAMES = frozenset({
    "chan_vaa_compound",
    "chan_four_state_blend",
    "chan_risk_managed_blend",
    "chan_crisis_shield_blend",
    "chan_four_state_shield_blend",
    "chan_dual_hybrid_blend",
    "generic_blend",
    "multi_strategy_alpha_book",
    "regime_factor_compound",
})

_VAA_CLASS_NAMES = frozenset({
    "ChanVaaCompoundStrategy",
    "ChanFourStateBlendStrategy",
    "ChanRiskManagedBlendStrategy",
    "ChanCrisisShieldBlendStrategy",
    "ChanFourStateShieldBlendStrategy",
    "ChanDualHybridBlendStrategy",
    "GenericBlendStrategy",
    "MultiStrategyAlphaBookStrategy",
    "RegimeFactorCompoundStrategy",
})


def resolve_strategy_market(
    params: Optional[dict] = None,
    cfg: Any = None,
    symbols: Optional[Union[Iterable[str], Mapping[str, Any]]] = None,
    cash_proxy: Optional[str] = None,
) -> Optional[str]:
    """Resolve the authoritative market identifier for a strategy.

    Resolution order:
    1. params.get("market") (if present and non-empty, normalized; invalid raises UnknownMarketError)
    2. getattr(cfg, "market", None) (if present and non-empty, normalized; invalid raises UnknownMarketError)
    3. Legacy trading boolean flags in params or cfg (china_trading -> "china", hk_trading -> "hk", us_trading -> "us")
    4. Auto-infer from equity symbols (stripping cash proxy and extended cash proxy symbols).
       If symbols cannot be inferred or cross-market, safely returns None (e.g. synthetic test symbols).
    """
    if isinstance(params, dict):
        p_market = params.get("market")
        if p_market is not None and str(p_market).strip() != "":
            return normalize_market(str(p_market))

    if cfg is not None:
        cfg_market = getattr(cfg, "market", None)
        if cfg_market is not None and str(cfg_market).strip() != "":
            return normalize_market(str(cfg_market))

    legacy_flags: List[str] = []
    if isinstance(params, dict):
        if params.get("china_trading"):
            legacy_flags.append(MARKET_CHINA)
        if params.get("hk_trading"):
            legacy_flags.append(MARKET_HK)
        if params.get("us_trading"):
            legacy_flags.append(MARKET_US)

    if not legacy_flags and cfg is not None:
        if getattr(cfg, "china_trading", False):
            legacy_flags.append(MARKET_CHINA)
        if getattr(cfg, "hk_trading", False):
            legacy_flags.append(MARKET_HK)
        if getattr(cfg, "us_trading", False):
            legacy_flags.append(MARKET_US)

    if len(legacy_flags) > 1:
        raise ValueError(f"Conflicting legacy trading flags detected: {legacy_flags}")
    if len(legacy_flags) == 1:
        return legacy_flags[0]

    if symbols is not None:
        cash_set = set(_EXTENDED_CASH_PROXIES)
        if cash_proxy:
            clean_cp = str(cash_proxy).strip().upper()
            cash_set.add(clean_cp)
            cash_set.add(clean_cp.split(".")[0])

        sym_list = list(symbols.keys()) if isinstance(symbols, dict) else list(symbols)
        equity_symbols = []
        for s in sym_list:
            s_str = str(s).strip().upper()
            bare = s_str.split(".")[0]
            if s_str in cash_set or bare in cash_set:
                continue
            equity_symbols.append(str(s))

        if equity_symbols:
            try:
                return infer_market_from_symbols(equity_symbols)
            except (UnknownSymbolMarketError, MixedMarketUniverseError):
                return None

    return None


def template_needs_vaa_canary(template: Any = None, template_name: str = "") -> bool:
    """Determine whether a template requires external VAA canary market data."""
    if template_name:
        t_clean = str(template_name).strip().lower()
        if t_clean in _VAA_TEMPLATE_NAMES:
            return True

    if template is not None:
        t_name = getattr(template, "name", "")
        if isinstance(t_name, str) and t_name.strip().lower() in _VAA_TEMPLATE_NAMES:
            return True

        cls = template if isinstance(template, type) else template.__class__
        if cls.__name__ in _VAA_CLASS_NAMES:
            return True

        if hasattr(template, "get_sub_strategies"):
            try:
                subs = template.get_sub_strategies({}, {})
                if isinstance(subs, dict):
                    if any(k in _VAA_TEMPLATE_NAMES or template_needs_vaa_canary(v) for k, v in subs.items()):
                        return True
                elif isinstance(subs, (list, tuple)):
                    if any(template_needs_vaa_canary(s) for s in subs):
                        return True
            except Exception:
                pass

    return False


__all__ = [
    "MARKET_VAA_PROFILES",
    "resolve_strategy_market",
    "template_needs_vaa_canary",
]
