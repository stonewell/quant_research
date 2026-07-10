"""Historical OHLCV data loading for a universe of tickers, with local CSV
caching. Tickers that fail to download (delisted, mistyped, no data for the
requested window) are skipped with a warning rather than aborting the whole
screen -- this itself is a small, disclosed instance of the survivorship-bias
risk discussed in the README: a ticker that no longer exists may be exactly
the one a real historical screen should have seen and rejected.
"""

import os
import warnings

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


def load_universe(symbols: list, start: str, end: str, interval: str = "1d", use_cache: bool = True) -> dict:
    """Load OHLCV for each symbol; skips (with a warning) any that fail."""
    data = {}
    for symbol in symbols:
        try:
            data[symbol] = load_ohlcv(symbol, start, end, interval, use_cache)
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
