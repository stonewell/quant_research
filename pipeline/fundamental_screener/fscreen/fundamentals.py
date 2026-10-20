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
    call per symbol assembled into a DataFrame indexed by symbol.
    Extracts all populated fundamental and valuation metadata, ensuring core fields
    ('roe', 'dividend_yield', 'earnings_growth', 'debt_to_equity') exist.
    """
    rows = {}
    for symbol in symbols:
        metadata = fetch_fund_metadata(symbol, provider="yfinance")
        row_data = {k: v for k, v in metadata.items() if not pd.isna(v)}
        for field in FUNDAMENTAL_FIELDS:
            if field not in row_data:
                row_data[field] = float("nan")
        rows[symbol] = row_data
    return pd.DataFrame(rows).T
