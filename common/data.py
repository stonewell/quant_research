"""Shared historical OHLCV data loading with local CSV caching, used across
every project in this workspace. Each project's own `data.py` is a thin
wrapper that pins `cache_dir` to that project's own `data/` folder, so the
per-project cache layout and call signatures (`load_ohlcv(symbol, start, end,
interval, use_cache)`, no `cache_dir` argument) are unchanged for callers.
"""

import os
import warnings

import pandas as pd
import yfinance as yf


def load_ohlcv(symbol: str, start: str, end: str, interval: str = "1d", use_cache: bool = True,
               cache_dir: str = None) -> pd.DataFrame:
    """Download (or load cached) OHLCV data for symbol between start and end.

    Uses auto_adjust=True so Close is dividend/split-adjusted (a reasonable
    approximation of total return for a long-only backtest). If `cache_dir`
    is None, caching is skipped entirely (always downloads fresh).
    """
    cache_path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f"{symbol}_{interval}_{start}_{end}.csv")

    if use_cache and cache_path and os.path.exists(cache_path):
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
    else:
        df = yf.download(symbol, start=start, end=end, interval=interval, auto_adjust=True, progress=False)
        if df.empty:
            raise ValueError(f"No data returned for {symbol} between {start} and {end}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        if cache_path:
            df.to_csv(cache_path)

    df.index = pd.to_datetime(df.index)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df


def load_universe(symbols: list, start: str, end: str, interval: str = "1d", use_cache: bool = True,
                   cache_dir: str = None) -> dict:
    """Load OHLCV for each symbol; skips (with a warning) any that fail."""
    data = {}
    for symbol in symbols:
        try:
            data[symbol] = load_ohlcv(symbol, start, end, interval, use_cache, cache_dir)
        except Exception as exc:
            warnings.warn(f"Skipping {symbol}: {exc}")
    return data


def fetch_fund_metadata(symbol: str) -> dict:
    """Best-effort expense ratio / AUM lookup via yfinance metadata.

    This is a data-availability convenience, not a verified research claim --
    yfinance's `.info` field names are inconsistent across instrument types
    and frequently missing, especially for plain stocks (which have neither
    an expense ratio nor a fund AUM). Callers should treat NaN as "not
    available," not "zero" or "bad."
    """
    expense_ratio, total_assets = float("nan"), float("nan")
    try:
        info = yf.Ticker(symbol).info
    except Exception:
        return {"expense_ratio": expense_ratio, "total_assets": total_assets}

    for key in ("netExpenseRatio", "annualReportExpenseRatio", "expenseRatio"):
        value = info.get(key)
        if value is not None:
            expense_ratio = float(value)
            break

    for key in ("totalAssets", "netAssets"):
        value = info.get(key)
        if value is not None:
            total_assets = float(value)
            break

    return {"expense_ratio": expense_ratio, "total_assets": total_assets}
