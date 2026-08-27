"""Fetches real per-symbol fundamentals -- always via yfinance, regardless
of whatever `--data-provider` a caller's OHLCV loading uses for the
benchmark's own price history (see `run_fundamental_screener.py`). This is
the one place this project necessarily touches the network by design: no
synthetic/offline equivalent exists for ROE/dividend yield/earnings growth/
debt-to-equity, since no real company backs a synthetic symbol.
"""

import pandas as pd

from common.data import fetch_fund_metadata

FUNDAMENTAL_FIELDS = ("roe", "dividend_yield", "earnings_growth", "debt_to_equity")


def fetch_fundamentals_frame(symbols: list) -> pd.DataFrame:
    """One `common.data.fetch_fund_metadata(symbol, provider="yfinance")`
    call per symbol (no caching layer -- see this project's README for why
    that's an accepted limitation for a first version), assembled into a
    DataFrame indexed by symbol with `FUNDAMENTAL_FIELDS` columns. A symbol
    yfinance can't resolve at all comes back as an all-NaN row (matching
    `fetch_fund_metadata`'s own best-effort, never-raises contract) rather
    than being silently dropped -- `rules.quality_ok` already treats any NaN
    field as a failed quality gate, so an unresolved symbol correctly ends
    up sell-flagged, not just missing from the report with no explanation.
    """
    rows = {}
    for symbol in symbols:
        metadata = fetch_fund_metadata(symbol, provider="yfinance")
        rows[symbol] = {field: metadata.get(field, float("nan")) for field in FUNDAMENTAL_FIELDS}
    return pd.DataFrame(rows).T
