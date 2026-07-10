"""Historical OHLCV data loading with local CSV caching.

Not exercised in this project's test suite by request -- tests use
synthetic data only. This module exists so `run_strategygen.py` is usable
against real market data when you're ready to run it yourself.
"""

import os

import pandas as pd
import yfinance as yf

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def load_ohlcv(symbol: str, start: str, end: str, interval: str = "1d", use_cache: bool = True) -> pd.DataFrame:
    os.makedirs(DATA_DIR, exist_ok=True)
    cache_path = os.path.join(DATA_DIR, f"{symbol}_{interval}_{start}_{end}.csv")

    if use_cache and os.path.exists(cache_path):
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
    else:
        df = yf.download(symbol, start=start, end=end, interval=interval, auto_adjust=True, progress=False)
        if df.empty:
            raise ValueError(f"No data returned for {symbol} between {start} and {end}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        df.to_csv(cache_path)

    df.index = pd.to_datetime(df.index)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df
