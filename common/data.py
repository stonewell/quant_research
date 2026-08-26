"""Shared historical OHLCV data loading with extensible data provider interface and
local CSV caching, used across every project in this workspace.
"""

from abc import ABC, abstractmethod
import hashlib
import importlib
import importlib.util
import os
import re
import sys
import time
from typing import Dict, List, Optional, Type, Union
import warnings

import numpy as np
import pandas as pd
import yfinance as yf

from .universe import _parse_module_specifier


# Real ticker symbols (equities, ETFs, indices, FX pairs) are drawn from a
# small, well-known alphabet. Both CSVFolderDataProvider and
# CachedDataProvider interpolate `symbol` directly into a filesystem path
# (e.g. f"{symbol}.csv") -- without this allow-list, a `symbol` containing
# path separators/traversal (e.g. "../../evil") could escape the intended
# folder entirely.
_SAFE_SYMBOL_RE = re.compile(r"^[A-Za-z0-9_.\-^=]+$")


def _validate_symbol_for_path(symbol: str) -> None:
    """Raises ValueError if `symbol` isn't safe to interpolate into a
    filesystem path (see `_SAFE_SYMBOL_RE`)."""
    if not symbol or not _SAFE_SYMBOL_RE.match(symbol):
        raise ValueError(
            f"Invalid symbol '{symbol}': must match {_SAFE_SYMBOL_RE.pattern} "
            "to be used as a filename component."
        )


class BaseDataProvider(ABC):
    """Abstract base class for all market data providers."""

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        """Fetch OHLCV data for a single symbol. Must return DataFrame with DatetimeIndex
        and columns ['Open', 'High', 'Low', 'Close', 'Volume'].
        """
        pass

    def fetch_universe(self, symbols: List[str], start: str, end: str, interval: str = "1d") -> Dict[str, pd.DataFrame]:
        """Fetch OHLCV for each symbol in symbols list. Skips failing symbols with warnings."""
        data = {}
        for symbol in symbols:
            try:
                data[symbol] = self.fetch_ohlcv(symbol, start, end, interval)
            except Exception as exc:
                warnings.warn(f"Skipping {symbol}: {exc}")
        return data

    def fetch_metadata(self, symbol: str) -> dict:
        """Best-effort metadata lookup returning {'expense_ratio': float, 'total_assets': float}."""
        return {"expense_ratio": float("nan"), "total_assets": float("nan")}


class YFinanceDataProvider(BaseDataProvider):
    """Data provider sourcing OHLCV and fund metadata from Yahoo Finance via yfinance."""

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        df = yf.download(symbol, start=start, end=end, interval=interval, auto_adjust=True, progress=False)
        if df.empty:
            raise ValueError(f"No data returned for {symbol} between {start} and {end}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        df.index = pd.to_datetime(df.index)
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        return _drop_invalid_ohlcv_rows(df, symbol)

    def fetch_metadata(self, symbol: str) -> dict:
        expense_ratio, total_assets = float("nan"), float("nan")
        try:
            info = yf.Ticker(symbol).info
        except Exception:
            return {"expense_ratio": expense_ratio, "total_assets": total_assets}

        # A real yfinance failure mode for delisted/invalid tickers: `.info`
        # returns None (or something else non-dict-like) instead of raising.
        # Without this check, the `.get()` calls below would raise an
        # uncaught AttributeError instead of falling back to the same
        # NaN-filled dict the exception path above already returns.
        if not isinstance(info, dict):
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


class CSVFolderDataProvider(BaseDataProvider):
    """Data provider reading historical CSV files from a specified folder."""

    def __init__(self, folder_path: str = "data"):
        self.folder_path = folder_path

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        _validate_symbol_for_path(symbol)
        candidates = [
            os.path.join(self.folder_path, f"{symbol}.csv"),
            os.path.join(self.folder_path, f"{symbol}_{interval}.csv"),
            os.path.join(self.folder_path, f"{symbol}_{interval}_{start}_{end}.csv"),
        ]
        found_path = None
        for path in candidates:
            if os.path.exists(path):
                found_path = path
                break

        if not found_path:
            raise FileNotFoundError(f"No CSV file found for symbol '{symbol}' in folder '{self.folder_path}'")

        df = pd.read_csv(found_path, index_col=0, parse_dates=True)
        req_cols = ["Open", "High", "Low", "Close", "Volume"]
        for col in req_cols:
            if col not in df.columns:
                raise ValueError(f"CSV file '{found_path}' missing required column '{col}'")

        df = df[req_cols]
        df.index = pd.to_datetime(df.index)
        if start:
            df = df[df.index >= pd.to_datetime(start)]
        if end:
            df = df[df.index <= pd.to_datetime(end)]

        if df.empty:
            raise ValueError(f"CSV data for {symbol} is empty between {start} and {end}")

        # Enforce the documented ascending-DatetimeIndex OHLCV contract
        # (common/README.md, section 1) -- a CSV on disk isn't guaranteed to
        # already be sorted by date.
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()
        return _drop_invalid_ohlcv_rows(df, symbol)


class SyntheticDataProvider(BaseDataProvider):
    """Data provider generating synthetic geometric Brownian motion OHLCV data."""

    def __init__(self, seed: int = 42, drift: float = 0.0003, vol: float = 0.01):
        self.seed = seed
        self.drift = drift
        self.vol = vol

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        start_dt = pd.to_datetime(start) if start else pd.to_datetime("2020-01-01")
        end_dt = pd.to_datetime(end) if end else start_dt + pd.Timedelta(days=1200)

        dates = pd.bdate_range(start_dt, end_dt)
        n_days = len(dates)
        if n_days <= 0:
            n_days = 252
            dates = pd.bdate_range("2020-01-01", periods=n_days)

        # Python's builtin hash() is randomized per-process for strings
        # (PYTHONHASHSEED), so it must NOT be used here -- it would silently
        # make `seed` non-reproducible across runs (the same seed producing
        # different data every process invocation). A stable digest gives
        # the same per-symbol offset regardless of process/interpreter.
        symbol_seed = self.seed + (int(hashlib.md5(symbol.encode("utf-8")).hexdigest(), 16) % 10000)
        rng = np.random.default_rng(symbol_seed)

        rets = rng.normal(self.drift, self.vol, n_days)
        closes = 100.0 * np.exp(np.cumsum(rets))

        df = pd.DataFrame(index=dates)
        df["Close"] = closes
        df["Open"] = closes * (1.0 + rng.normal(0, 0.002, n_days))
        df["High"] = np.maximum(df["Open"], df["Close"]) * (1.0 + np.abs(rng.normal(0, 0.004, n_days)))
        df["Low"] = np.minimum(df["Open"], df["Close"]) * (1.0 - np.abs(rng.normal(0, 0.004, n_days)))
        df["Volume"] = (rng.uniform(1e5, 1e7, n_days)).astype(float)
        return df[["Open", "High", "Low", "Close", "Volume"]]


_REQUIRED_OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _drop_invalid_ohlcv_rows(df: pd.DataFrame, symbol: str = None) -> pd.DataFrame:
    """Drops rows with LOGICALLY IMPOSSIBLE OHLC values -- High < Low,
    High less than the bar's own Open/Close, Low greater than the bar's own
    Open/Close, a non-positive price, or a NaN/inf anywhere in O/H/L/C -- no
    real market can ever produce these; they're a data provider bug, not a
    legitimate (if extreme) price move.

    Deliberately does NOT flag "is this move too big" -- a leveraged ETF, a
    gap, or an earnings move can legitimately jump 10-50%+ in a day, and
    judging that is a modeling choice this project keeps out of the shared
    data layer (see `common/README.md`'s provider contract).
    """
    ohlc = df[["Open", "High", "Low", "Close"]]
    bad = (
        (ohlc["High"] < ohlc["Low"])
        | (ohlc["High"] < ohlc[["Open", "Close"]].max(axis=1))
        | (ohlc["Low"] > ohlc[["Open", "Close"]].min(axis=1))
        | (ohlc <= 0).any(axis=1)
        | (~np.isfinite(ohlc)).any(axis=1)
    )
    if bad.any():
        label = symbol or "series"
        bad_dates = list(df.index[bad])
        warnings.warn(f"Dropping {int(bad.sum())} row(s) with impossible OHLC values for {label}: {bad_dates}")
        df = df[~bad]
    return df


def _is_valid_cached_ohlcv(df: pd.DataFrame) -> bool:
    """Sanity-checks a CSV read back from CachedDataProvider's disk cache:
    non-empty, has the expected OHLCV columns, and a real DatetimeIndex."""
    if df.empty:
        return False
    if not all(col in df.columns for col in _REQUIRED_OHLCV_COLUMNS):
        return False
    if not isinstance(df.index, pd.DatetimeIndex):
        return False
    return True


class CachedDataProvider(BaseDataProvider):
    """Wrapper provider that adds CSV disk caching around any inner BaseDataProvider."""

    def __init__(self, inner_provider: BaseDataProvider, cache_dir: str, cache_max_age_days: Optional[float] = None):
        self.inner_provider = inner_provider
        self.cache_dir = cache_dir
        # None (default) preserves the original unlimited-cache behavior --
        # a cached file is trusted forever regardless of age. Set this to
        # force a re-fetch once a cached file is older than N days (useful
        # for a rolling/live `end` date; irrelevant for a fixed historical
        # range, which never goes stale).
        self.cache_max_age_days = cache_max_age_days

    def _is_stale(self, cache_path: str) -> bool:
        if self.cache_max_age_days is None:
            return False
        age_days = (time.time() - os.path.getmtime(cache_path)) / 86400.0
        return age_days > self.cache_max_age_days

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        if not self.cache_dir:
            return self.inner_provider.fetch_ohlcv(symbol, start, end, interval)

        _validate_symbol_for_path(symbol)
        os.makedirs(self.cache_dir, exist_ok=True)
        # Prefixing with the inner provider's class name is load-bearing, not
        # cosmetic: this cache directory is shared workspace-wide across
        # projects with DIFFERENT default providers (e.g. research_strategy
        # defaults to synthetic, the other 3 default to yfinance) -- without
        # this, two projects fetching the same symbol/interval/date-range
        # from different providers would silently read back each other's
        # (wrong-provider) cached data.
        provider_name = type(self.inner_provider).__name__
        cache_path = os.path.join(self.cache_dir, f"{provider_name}_{symbol}_{interval}_{start}_{end}.csv")

        if os.path.exists(cache_path) and not self._is_stale(cache_path):
            try:
                df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
                df.index = pd.to_datetime(df.index)
                if not _is_valid_cached_ohlcv(df):
                    raise ValueError("cached file is missing expected OHLCV columns or is empty")
                df = _drop_invalid_ohlcv_rows(df, symbol)
                if df.empty:
                    raise ValueError("cached file contains no rows with valid OHLC values")
                return df
            except Exception as exc:
                warnings.warn(f"Cache file '{cache_path}' is corrupt or invalid ({exc}); re-fetching from source.")

        df = self.inner_provider.fetch_ohlcv(symbol, start, end, interval)
        if not df.empty:
            df.to_csv(cache_path)
        return df

    # fetch_universe is deliberately NOT overridden here: BaseDataProvider's
    # default implementation loops over symbols calling `self.fetch_ohlcv`
    # (the cached version) for each -- delegating straight to
    # `inner_provider.fetch_universe` instead, as a prior version of this
    # method did, would call the INNER provider's fetch_ohlcv directly for
    # every symbol, silently skipping this class's own cache entirely for
    # any caller using load_universe() rather than per-symbol load_ohlcv().

    def fetch_metadata(self, symbol: str) -> dict:
        return self.inner_provider.fetch_metadata(symbol)


_PROVIDER_REGISTRY: Dict[str, Type[BaseDataProvider]] = {}


def register_provider(name: str, provider_cls: Type[BaseDataProvider]):
    """Registers a data provider class under a name."""
    _PROVIDER_REGISTRY[name.lower()] = provider_cls


register_provider("yfinance", YFinanceDataProvider)
register_provider("csv", CSVFolderDataProvider)
register_provider("synthetic", SyntheticDataProvider)

_DEFAULT_PROVIDER: Optional[BaseDataProvider] = None


def set_default_data_provider(provider: BaseDataProvider):
    """Sets global default data provider instance."""
    global _DEFAULT_PROVIDER
    _DEFAULT_PROVIDER = provider


def _load_provider_from_specifier(specifier: str, **kwargs) -> BaseDataProvider:
    """Dynamically loads a data provider from a module specifier string
    (e.g., 'script.py:CustomProvider', 'module.path:CustomProvider', or 'script.py').
    """
    path_or_module, attr_name = _parse_module_specifier(specifier)

    if os.path.exists(path_or_module) or path_or_module.endswith(".py"):
        file_path = os.path.abspath(path_or_module)
        module_name = f"dynamic_provider_{abs(hash(file_path))}"
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load Python script from '{file_path}'")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
    else:
        try:
            mod = importlib.import_module(path_or_module)
        except Exception as exc:
            raise ValueError(
                f"Could not import module '{path_or_module}' for data provider '{specifier}': {exc}"
            ) from exc

    target_cls = None
    if attr_name:
        if not hasattr(mod, attr_name):
            raise AttributeError(f"Module or script '{path_or_module}' has no attribute '{attr_name}'")
        target_cls = getattr(mod, attr_name)
    else:
        for attr in dir(mod):
            val = getattr(mod, attr)
            if isinstance(val, type) and issubclass(val, BaseDataProvider) and val is not BaseDataProvider:
                target_cls = val
                break
        if target_cls is None:
            raise AttributeError(
                f"No BaseDataProvider subclass found in module '{path_or_module}'. Specify 'module:ClassName'."
            )

    if isinstance(target_cls, type):
        instance = target_cls(**kwargs)
    elif callable(target_cls):
        instance = target_cls(**kwargs)
    else:
        instance = target_cls

    if not hasattr(instance, "fetch_ohlcv") or not callable(getattr(instance, "fetch_ohlcv")):
        raise TypeError(f"Loaded provider '{specifier}' does not implement 'fetch_ohlcv'")

    return instance


def get_data_provider(provider_name_or_instance: Union[str, BaseDataProvider, None] = None, **kwargs) -> BaseDataProvider:
    """Factory to instantiate or retrieve a provider instance. Accepts registered provider names,
    instances, or module specifier strings (e.g. 'script.py:CustomProvider' or 'module.path:CustomProvider').
    """
    if isinstance(provider_name_or_instance, BaseDataProvider):
        return provider_name_or_instance

    if provider_name_or_instance is None:
        if _DEFAULT_PROVIDER is not None:
            return _DEFAULT_PROVIDER
        return YFinanceDataProvider(**kwargs)

    name = str(provider_name_or_instance).strip()
    if name.lower() in _PROVIDER_REGISTRY:
        provider_cls = _PROVIDER_REGISTRY[name.lower()]
        return provider_cls(**kwargs)

    if ":" in name or os.path.exists(name) or name.endswith(".py") or "." in name:
        return _load_provider_from_specifier(name, **kwargs)

    raise ValueError(
        f"Unknown data provider '{provider_name_or_instance}'. Available registered providers: {list(_PROVIDER_REGISTRY.keys())}"
    )


def load_ohlcv(symbol: str, start: str, end: str, interval: str = "1d", use_cache: bool = True,
               cache_dir: str = None, provider: Union[str, BaseDataProvider, None] = None,
               cache_max_age_days: Optional[float] = None, **kwargs) -> pd.DataFrame:
    """Download (or load cached) OHLCV data for symbol between start and end.
    Maintains backward compatibility with original load_ohlcv function signature.
    `cache_max_age_days` (None by default -- cached files never expire) is
    passed straight through to CachedDataProvider; it is NOT part of `**kwargs`
    since those flow into the underlying provider's own constructor.
    """
    base_prov = get_data_provider(provider, **kwargs)
    if use_cache and cache_dir:
        prov = CachedDataProvider(base_prov, cache_dir, cache_max_age_days=cache_max_age_days)
    else:
        prov = base_prov
    return prov.fetch_ohlcv(symbol, start, end, interval)


def load_universe(symbols: list, start: str, end: str, interval: str = "1d", use_cache: bool = True,
                  cache_dir: str = None, provider: Union[str, BaseDataProvider, None] = None,
                  cache_max_age_days: Optional[float] = None, **kwargs) -> dict:
    """Load OHLCV for each symbol; skips (with a warning) any that fail.
    Maintains backward compatibility with original load_universe function signature.
    See `load_ohlcv`'s docstring for `cache_max_age_days`.
    """
    base_prov = get_data_provider(provider, **kwargs)
    if use_cache and cache_dir:
        prov = CachedDataProvider(base_prov, cache_dir, cache_max_age_days=cache_max_age_days)
    else:
        prov = base_prov
    return prov.fetch_universe(symbols, start, end, interval)


def fetch_fund_metadata(symbol: str, provider: Union[str, BaseDataProvider, None] = None, **kwargs) -> dict:
    """Best-effort expense ratio / AUM lookup via metadata provider."""
    prov = get_data_provider(provider, **kwargs)
    return prov.fetch_metadata(symbol)


# Re-export Universe Provider components for convenience
from .universe import (
    BaseUniverseProvider,
    CodeUniverseProvider,
    FileUniverseProvider,
    StaticUniverseProvider,
    add_universe_cli_args,
    get_universe_provider,
    register_universe_provider,
    resolve_universe_from_args,
    resolve_universe_symbols,
)

