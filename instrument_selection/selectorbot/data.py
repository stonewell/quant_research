"""Historical OHLCV data loading for a universe of tickers, with local CSV
caching -- thin wrapper around the shared quant-level loader
(`common/data.py`), pinned to this project's own `data/` cache directory.
Tickers that fail to download (delisted, mistyped, no data for the
requested window) are skipped with a warning rather than aborting the whole
screen -- this itself is a small, disclosed instance of the survivorship-bias
risk discussed in the README: a ticker that no longer exists may be exactly
the one a real historical screen should have seen and rejected.
"""

import os

from common.data import fetch_fund_metadata as _fetch_fund_metadata
from common.data import load_ohlcv as _load_ohlcv
from common.data import load_universe as _load_universe

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def load_ohlcv(symbol: str, start: str, end: str, interval: str = "1d", use_cache: bool = True, provider=None, **kwargs):
    return _load_ohlcv(symbol, start, end, interval, use_cache, cache_dir=DATA_DIR, provider=provider, **kwargs)


def load_universe(symbols: list, start: str, end: str, interval: str = "1d", use_cache: bool = True, provider=None, **kwargs) -> dict:
    return _load_universe(symbols, start, end, interval, use_cache, cache_dir=DATA_DIR, provider=provider, **kwargs)


def fetch_fund_metadata(symbol: str, provider=None, **kwargs) -> dict:
    return _fetch_fund_metadata(symbol, provider=provider, **kwargs)
