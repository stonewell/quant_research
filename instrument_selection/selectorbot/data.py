"""Historical OHLCV data loading for a universe of tickers, with local CSV
caching -- thin wrapper around the shared quant-level loader
(`common/data.py`), pinned to the workspace-wide shared `<repo_root>/data`
cache directory (see `common/README.md`'s "Shared OHLCV cache directory"
section). Tickers that fail to download (delisted, mistyped, no data for the
requested window) are skipped with a warning rather than aborting the whole
screen -- this itself is a small, disclosed instance of the survivorship-bias
risk discussed in the README: a ticker that no longer exists may be exactly
the one a real historical screen should have seen and rejected.
"""

from common.cli_utils import shared_data_dir
from common.data import fetch_fund_metadata as _fetch_fund_metadata
from common.data import load_ohlcv as _load_ohlcv
from common.data import load_universe as _load_universe

DATA_DIR = shared_data_dir()


def load_ohlcv(symbol: str, start: str, end: str, interval: str = "1d", use_cache: bool = True, provider=None, **kwargs):
    return _load_ohlcv(symbol, start, end, interval, use_cache, cache_dir=DATA_DIR, provider=provider, **kwargs)


def load_universe(symbols: list, start: str, end: str, interval: str = "1d", use_cache: bool = True, provider=None, **kwargs) -> dict:
    return _load_universe(symbols, start, end, interval, use_cache, cache_dir=DATA_DIR, provider=provider, **kwargs)


def fetch_fund_metadata(symbol: str, provider=None, **kwargs) -> dict:
    return _fetch_fund_metadata(symbol, provider=provider, **kwargs)
