"""Historical OHLCV data loading with local CSV caching -- thin wrapper
around the shared quant-level loader (`common/data.py`), pinned to this
project's own `data/` cache directory."""

import os

from common.data import load_ohlcv as _load_ohlcv

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def load_ohlcv(symbol: str, start: str, end: str, interval: str = "1d", use_cache: bool = True):
    return _load_ohlcv(symbol, start, end, interval, use_cache, cache_dir=DATA_DIR)
