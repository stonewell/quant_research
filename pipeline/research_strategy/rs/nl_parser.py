"""Natural Language Strategy Parser for Researched Quantitative Trading Strategies.

Parses plain English strategy descriptions into a structured execution specification (ParsedStrategySpec).
Design: 100% offline, deterministic keyword and pattern-matching extraction pipeline with sensible fallback defaults.
"""

from dataclasses import dataclass, field
import re
from typing import List, Optional

DEFAULT_RISKY_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "TLT", "VNQ"]
DEFAULT_CASH_PROXY = "BIL"

STOP_WORDS = {
    "THE", "AND", "FOR", "TOP", "SMA", "ROC", "BAA", "GTAA", "ALL",
    "USE", "FROM", "WITH", "INTO", "WHEN", "THAT", "THIS", "EACH",
    # Common all-caps technical-analysis / finance acronyms that otherwise
    # get captured as spurious phantom tickers by _extract_tickers() when a
    # description merely mentions them (e.g. "...with RSI confirmation...").
    # A later `if s in symbols` filter in strategy.py stops them from ever
    # being traded, but they still pollute explain_weights()/parsed_summary
    # output as if they were part of the universe.
    "RSI", "ATR", "ADX", "ETF", "CAGR", "MACD",
}


@dataclass
class ParsedStrategySpec:
    """Structured execution specification extracted from plain English text."""
    strategy_name: str
    raw_description: str

    rebalance_freq_days: int = 21

    # Universes
    risky_universe: List[str] = field(default_factory=list)
    # canary/offensive/defensive default to None (not []) -- None means "the description
    # named no tickers for this role, derive it from the runtime universe at generate_weights
    # time"; an explicitly-set empty list means "deliberately narrowed to nothing" and must be
    # respected as empty, not expanded (see generate_weights's canary-mode branch).
    canary_universe: Optional[List[str]] = None
    offensive_universe: Optional[List[str]] = None
    defensive_universe: Optional[List[str]] = None
    cash_proxy: str = DEFAULT_CASH_PROXY

    # Trend / Gate Filters
    trend_sma_period: int = 0         # e.g., 200 (0 means disabled)
    trend_roc_lookback: int = 0       # e.g., 126 (0 means disabled)

    # Canary Turbulence Logic
    use_canary_logic: bool = False

    # Selection & Momentum Ranking
    mom_short_lookback: int = 63
    mom_long_lookback: int = 126
    top_k: int = 3

    # Allocation & Sizing Scheme
    allocation_scheme: str = "inverse_volatility"  # "equal_weight", "inverse_volatility", "volatility_managed"
    vol_lookback: int = 60
    target_vol: float = 0.15
    var_lookback: int = 20
    max_leverage: float = 1.0

    # Populated by parse_plain_english_strategy() whenever a value below
    # could not be confidently extracted from the description text and fell
    # back to a default instead -- a low-confidence signal for the caller,
    # not itself a parse failure (the returned spec is always usable).
    warnings: List[str] = field(default_factory=list)

    def format_summary(self) -> str:
        """Returns a formatted ASCII summary table of the parsed strategy rules."""
        lines = [
            "=" * 70,
            f"STRATEGY SPECIFICATION: {self.strategy_name.upper()}",
            "=" * 70,
            f"Raw Description:          {self.raw_description.strip()}",
            "-" * 70,
            f"Rebalance Frequency:     Every {self.rebalance_freq_days} trading days",
            f"Allocation Scheme:       {self.allocation_scheme.upper()}",
            f"Top K Selection:         Top {self.top_k} assets",
            f"Cash Proxy Asset:        {self.cash_proxy}",
        ]

        if self.use_canary_logic:
            lines.extend([
                f"Canary Turbulence State: ENABLED",
                f"Canary Universe:         {', '.join(self.canary_universe) if self.canary_universe else '(derives from the runtime universe)'}",
                f"Offensive Universe:      {', '.join(self.offensive_universe) if self.offensive_universe else '(derives from the runtime universe)'}",
                f"Defensive Universe:      {', '.join(self.defensive_universe) if self.defensive_universe else '(derives from the runtime universe)'}",
            ])
        else:
            lines.append(f"Risky Universe:          {', '.join(self.risky_universe) if self.risky_universe else 'None'}")

        if self.trend_sma_period > 0 or self.trend_roc_lookback > 0:
            gates = []
            if self.trend_sma_period > 0:
                gates.append(f"Close > {self.trend_sma_period}d SMA")
            if self.trend_roc_lookback > 0:
                gates.append(f"{self.trend_roc_lookback}d ROC > 0")
            lines.append(f"Absolute Trend Gate:     {' AND '.join(gates)}")
        else:
            lines.append(f"Absolute Trend Gate:     None (No trend filter)")

        lines.extend([
            f"Momentum Lookbacks:      {self.mom_short_lookback}d (short), {self.mom_long_lookback}d (long)",
        ])

        if self.warnings:
            lines.append("-" * 70)
            lines.append("Parser Warnings:")
            for w in self.warnings:
                lines.append(f"  - {w}")

        lines.append("=" * 70)
        return "\n".join(lines)


def _extract_tickers(segment: str, cash_proxy: Optional[str] = None) -> List[str]:
    """Extracts unique uppercase ticker symbols from a text segment.

    `cash_proxy`, when given, is excluded -- appropriate for a general
    "risky universe" extraction, where the cash proxy isn't itself a risky
    holding. Pass None (the default) for a canary/offensive/defensive
    universe extraction: BAA-G12's defensive pool legitimately lists the
    cash-proxy symbol (e.g. BIL) as a rankable candidate, not just the
    passive fallback for unallocated capital, so it must not be excluded
    there."""
    raw_tokens = re.findall(r"\b[A-Z]{2,5}\b", segment)
    valid = []
    for tok in raw_tokens:
        if tok not in STOP_WORDS and (cash_proxy is None or tok != cash_proxy) and tok not in valid:
            valid.append(tok)
    return valid


def parse_plain_english_strategy(description: str, name: Optional[str] = None) -> ParsedStrategySpec:
    """Parses a plain English strategy description into a structured ParsedStrategySpec."""
    text = description.strip()
    text_lower = text.lower()

    # Determine strategy name
    name_unrecognized = False
    if not name:
        if "turtle" in text_lower or "donchian" in text_lower or "channel breakout" in text_lower:
            name = "Turtle Channel Breakout (Parsed)"
        elif "baa" in text_lower or "bold asset" in text_lower or "keller" in text_lower or "canary" in text_lower:
            name = "Bold Asset Allocation (Parsed)"
        elif "volatility-managed" in text_lower or "volatility managed" in text_lower or "moreira" in text_lower:
            name = "Volatility-Managed Strategy (Parsed)"
        elif "dual momentum" in text_lower or "gtaa" in text_lower or "antonacci" in text_lower:
            name = "Active Dual Momentum GTAA (Parsed)"
        else:
            name = "Custom Plain English Strategy"
            name_unrecognized = True

    spec = ParsedStrategySpec(strategy_name=name, raw_description=text)
    if name_unrecognized:
        spec.warnings.append(
            "Could not classify strategy type from description; using generic name 'Custom Plain English Strategy'"
        )

    # 1. Extract Cash Proxy
    cash_match = re.search(r"cash\s*(?:proxy|asset)?\s*:?\s*([A-Z]{2,5})", text, re.IGNORECASE)
    if cash_match and cash_match.group(1).upper() not in STOP_WORDS:
        spec.cash_proxy = cash_match.group(1).upper()
    elif "BIL" in text:
        spec.cash_proxy = "BIL"

    # 2. Extract Rebalance Frequency
    if "daily" in text_lower or "every day" in text_lower:
        spec.rebalance_freq_days = 1
    elif "weekly" in text_lower or "every week" in text_lower:
        spec.rebalance_freq_days = 5
    elif "monthly" in text_lower or "every month" in text_lower or "rebalance monthly" in text_lower:
        spec.rebalance_freq_days = 21
    else:
        freq_match = re.search(r"every\s+(\d+)\s*d(?:ay)?s?", text_lower)
        if freq_match:
            spec.rebalance_freq_days = int(freq_match.group(1))

    # 3. Extract Universes by Sentence/Clause Context
    sentences = re.split(r"[.\n;]", text)

    if "canary" in text_lower:
        spec.use_canary_logic = True

        canary_tickers: List[str] = []
        offensive_tickers: List[str] = []
        defensive_tickers: List[str] = []
        for s in sentences:
            s_lower = s.lower()
            if "canary" in s_lower:
                tickers = _extract_tickers(s)
                canary_tickers.extend([t for t in tickers if t not in canary_tickers])
            if "offensive" in s_lower:
                tickers = _extract_tickers(s)
                offensive_tickers.extend([t for t in tickers if t not in offensive_tickers])
            if "defensive" in s_lower:
                tickers = _extract_tickers(s)
                defensive_tickers.extend([t for t in tickers if t not in defensive_tickers])

        # None (not []) means "no tickers named" -- generate_weights derives this role from
        # the runtime universe instead of expanding a deliberately-empty explicit list.
        spec.canary_universe = canary_tickers or None
        spec.offensive_universe = offensive_tickers or None
        spec.defensive_universe = defensive_tickers or None

        if spec.canary_universe is None:
            spec.warnings.append(
                "No canary-universe tickers found in description; will use every symbol in the "
                "runtime universe instead of a hardcoded default."
            )
        if spec.offensive_universe is None:
            spec.warnings.append(
                "No offensive-universe tickers found in description; will use every symbol in the "
                "runtime universe instead of a hardcoded default."
            )
        if spec.defensive_universe is None:
            spec.warnings.append(
                "No defensive-universe tickers found in description; will use every symbol in the "
                "runtime universe instead of a hardcoded default."
            )
    else:
        # General risky universe extraction
        tickers = _extract_tickers(text, spec.cash_proxy)
        if tickers:
            spec.risky_universe = tickers
        else:
            spec.risky_universe = list(DEFAULT_RISKY_UNIVERSE)
            spec.warnings.append(
                f"No risky-universe tickers found in description; defaulting to DEFAULT_RISKY_UNIVERSE "
                f"({', '.join(DEFAULT_RISKY_UNIVERSE)})"
            )

    # 4. Absolute Trend Gate
    if "sma" in text_lower or "moving average" in text_lower:
        sma_before = re.search(r"(\d+)\s*(?:d|day|m|month)?\s*(?:sma|moving average)", text_lower)
        sma_after = re.search(r"(?:sma|moving average)\(?\s*(\d+)", text_lower)
        if sma_before:
            spec.trend_sma_period = int(sma_before.group(1))
        elif sma_after:
            spec.trend_sma_period = int(sma_after.group(1))
        else:
            spec.trend_sma_period = 200
            spec.warnings.append("'sma'/'moving average' mentioned but no explicit period found; defaulting trend_sma_period=200")

    if "roc" in text_lower or "positive return" in text_lower or "trend gate" in text_lower:
        # Capture an adjacent day/month unit token (group 2) alongside the
        # number itself. Without this, e.g. "10d ROC > 0" -- an EXPLICIT day
        # unit -- fell through to the "small number means months" heuristic
        # below and was wrongly multiplied by 21 (10 -> 210) purely because
        # 10 <= 12, discarding the explicitly-stated day unit entirely.
        roc_match = re.search(r"(\d+)\s*(d|day|days|m|month|months)?\s*(?:roc|return)", text_lower)
        if roc_match:
            val = int(roc_match.group(1))
            unit = roc_match.group(2)
            if unit and unit.startswith("d"):
                # Explicit day unit -- use the number literally regardless
                # of its size.
                spec.trend_roc_lookback = val
            elif unit and unit.startswith("m"):
                # Explicit month unit -- convert to trading days.
                spec.trend_roc_lookback = val * 21
            else:
                # No explicit unit found -- keep the legacy heuristic for
                # bare numbers (e.g. "3 ROC" is assumed to mean 3 months).
                spec.trend_roc_lookback = val if val > 12 else val * 21
        else:
            spec.trend_roc_lookback = 126
            spec.warnings.append("'roc'/'positive return'/'trend gate' mentioned but no explicit lookback found; defaulting trend_roc_lookback=126")

    # 5. Selection & Top K
    topk_match = re.search(r"top\s*(\d+)", text_lower)
    if topk_match:
        spec.top_k = int(topk_match.group(1))

    # Volatility-sizing lookbacks -- extracted BEFORE the generic momentum
    # sweep below, and excluded from it by value. Without this, a phrase
    # like "...63d and 126d momentum... using 60d inverse volatility..."
    # would have its 60d picked up by min()/max() as a MOMENTUM lookback
    # (discarding the real 63d) purely because 60 < 63, conflating two
    # unrelated lookback windows that happen to share similar magnitudes.
    vol_lookback_match = re.search(r"(\d+)[\s-]*(?:d|day)?s?\s*inverse\s*vol(?:atility)?", text_lower)
    parsed_vol_lookback = None
    if vol_lookback_match:
        parsed_vol_lookback = int(vol_lookback_match.group(1))
        spec.vol_lookback = parsed_vol_lookback

    var_lookback_match = re.search(
        r"(\d+)[\s-]*(?:d|day)?s?\s*(?:volatility[- ]managed|inverse\s*variance)", text_lower)
    parsed_var_lookback = None
    if var_lookback_match:
        parsed_var_lookback = int(var_lookback_match.group(1))
        spec.var_lookback = parsed_var_lookback

    # Target volatility (e.g. "targeting 15% annual volatility") -- was
    # previously never parsed at all, silently staying at the dataclass
    # default regardless of what the text actually specified.
    target_vol_match = re.search(r"target(?:ing)?\s*(?:a\s*)?(\d+(?:\.\d+)?)\s*%\s*(?:annual\s*)?vol", text_lower)
    if target_vol_match:
        spec.target_vol = float(target_vol_match.group(1)) / 100.0

    # Lookback extraction for momentum
    lookbacks = [int(m) for m in re.findall(r"\b(\d+)\s*d(?:ay)?s?\b", text_lower)]
    exclude_lookbacks = {spec.trend_sma_period, spec.rebalance_freq_days, spec.top_k}
    if parsed_vol_lookback is not None:
        exclude_lookbacks.add(parsed_vol_lookback)
    if parsed_var_lookback is not None:
        exclude_lookbacks.add(parsed_var_lookback)
    lookbacks = [l for l in lookbacks if l not in exclude_lookbacks]
    if len(lookbacks) >= 2:
        spec.mom_short_lookback = min(lookbacks)
        spec.mom_long_lookback = max(lookbacks)
    elif len(lookbacks) == 1:
        spec.mom_long_lookback = lookbacks[0]
        spec.mom_short_lookback = max(21, lookbacks[0] // 2)

    # 6. Allocation Scheme
    if "volatility-managed" in text_lower or "volatility managed" in text_lower or "inverse variance" in text_lower or "moreira" in text_lower:
        spec.allocation_scheme = "volatility_managed"
    elif "equal" in text_lower or "equally" in text_lower:
        spec.allocation_scheme = "equal_weight"
    elif "inverse volatility" in text_lower or "risk parity" in text_lower or "inverse vol" in text_lower:
        spec.allocation_scheme = "inverse_volatility"
    else:
        if spec.use_canary_logic:
            spec.allocation_scheme = "equal_weight"
        else:
            spec.allocation_scheme = "inverse_volatility"
        spec.warnings.append(
            f"No explicit allocation-scheme keywords found in description; defaulting "
            f"allocation_scheme='{spec.allocation_scheme}' based on canary-logic presence alone"
        )

    return spec
