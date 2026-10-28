"""Shared historical OHLCV data loading with an extensible data provider interface and
DuckDB disk caching, used across every project in this workspace.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Type, Union
import warnings

import numpy as np
import pandas as pd
import yfinance as yf

from .universe import _parse_module_specifier
from .duckdb_cache import (
    AdjustBasisTransitionRefused,
    DuckDBCache,
    HistoryRewriteRefused,
    MissingAdjustFactorsError,
    UnknownAdjustBasisError,
    canonicalize_adjust_mode,
    is_factor_neutral_symbol,
    is_synthetic_provider,
    is_intraday_interval,
    parse_stored_basis,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FundamentalMetrics:
    symbol: str
    report_year: int
    stat_date: str
    roe: float
    net_profit: float
    net_profit_5yr_cagr: float
    cfo: float
    cfo_to_net_profit: float
    total_assets: float
    total_liabilities: float
    debt_to_assets: float
    debt_to_equity: float
    interest_bearing_debt: float
    dividend_yield: float
    dividend_payout_ratio: float
    pe_ttm: float
    pb: float
    total_shares: float
    market_cap: float
    close_price: float = float("nan")
    industry_name: str = "Unknown"
    industry_tier: str = "tier3_manufacturing"
    terminal_pe: float = 10.0
    is_blacklisted: bool = False
    blacklist_reason: Optional[str] = None


# Real ticker symbols (equities, ETFs, indices, FX pairs) are drawn from a
# small, well-known alphabet. CSVFolderDataProvider interpolates `symbol`
# directly into a filesystem path (e.g. f"{symbol}.csv") -- without this
# allow-list, a `symbol` containing path separators/traversal (e.g.
# "../../evil") could escape the intended folder entirely. CachedDataProvider
# applies the same check as a defense-in-depth boundary on the symbols it
# stores and returns.
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

    #: The adjustment basis of the series `fetch_ohlcv` returns: `"raw"`, `"qfq"`, `"hfq"`, or
    #: `None` when the provider cannot say. `CachedDataProvider` writes this label into the
    #: cache's `adjustflag` metadata, and it is the only thing that decides whether a requested
    #: qfq/hfq read is a pass-through or a derivation -- an unlabelled row cannot be either.
    #:
    #: Concrete providers override it when their own `fetch_ohlcv` applies (or does not apply) an
    #: adjustment: `YFinanceDataProvider` downloads with `auto_adjust=True`, i.e. `qfq`.
    native_adjust_basis: Optional[str] = None

    #: Documented fallback for a provider that declares nothing. `CachedDataProvider`'s legacy
    #: direct path (no `fetch_raw_ohlcv`, no `fetch_adjust_factors`) has to label what it stores,
    #: and "no basis information at all" cannot be stored as an undeclared row any more (P1: such
    #: a row fails closed on every read). `qfq` is the pre-M3.6 read default every consumer of this
    #: cache already assumes, and it is FAIL-CLOSED for `raw`/`hfq` requests (a `ValueError`, never
    #: a silent pass-through of possibly-wrong bytes). A provider whose series is not qfq must say
    #: so -- either via `native_adjust_basis`, or by an operator declaration through
    #: `scripts/migrate_adjust_basis.py --set-basis ... --force`.
    ASSUMED_NATIVE_BASIS_WHEN_UNKNOWN = "qfq"

    #: Whether this provider, **right now**, actually has a usable adjustment-factor source --
    #: i.e. whether calling `fetch_adjust_factors` can produce factors at all. This is a separate
    #: question from "does the method exist": `FuyaoDataProvider` always *has* the method (it
    #: delegates to MarketDB), but the factor table is only reachable when a local market.duckdb
    #: opened, so the flag is DYNAMIC and recomputed in `__init__` there.
    #:
    #: `CachedDataProvider.get_bars` uses it to tell a CAPABILITY GAP (no factor source -> take the
    #: legacy direct path, store the provider's native basis, derive nothing, warn once) apart from
    #: a DATA FAILURE (source exists, returned nothing -> `MissingAdjustFactorsError`, fail-closed).
    #: Default `True`: a provider that has `fetch_adjust_factors` at all is authoritative
    #: (`MarketDBDataProvider` / `BaoStockDataProvider` read real factor tables / endpoints), so an
    #: empty result from them stays a loud failure.
    #:
    #: Setting this False is a capability statement, never a way to hide an error: it must describe
    #: "this instance has nowhere to read factors from", and the fallback it triggers is the same
    #: documented one a provider without the method gets.
    adjust_factor_source_available: bool = True

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        """Fetch OHLCV data for a single symbol. Must return DataFrame with DatetimeIndex
        and columns ['Open', 'High', 'Low', 'Close', 'Volume'].
        """
        pass

    def fetch_universe(self, symbols: List[str], start: str, end: str, interval: str = "1d",
                       columns: Optional[Any] = None) -> Dict[str, pd.DataFrame]:
        """Fetch OHLCV for each symbol in symbols list. Skips failing symbols with warnings.

        `columns` exists for interface parity with `CachedDataProvider.fetch_universe` (which
        uses it to select the opt-in `Amount` column out of DuckDB). A raw provider has no column
        projection layer -- its `fetch_ohlcv` returns whatever it returns -- so the argument is
        accepted and ignored rather than raising, so that one caller can drive both.
        """
        data = {}
        for symbol in symbols:
            try:
                data[symbol] = self.fetch_ohlcv(symbol, start, end, interval)
            except Exception as exc:
                warnings.warn(f"Skipping {symbol}: {exc}")
        return data

    def fetch_metadata(self, symbol: str) -> dict:
        """Best-effort metadata lookup. Every field defaults to NaN -- a
        provider that can't/doesn't support a given field (e.g. a synthetic
        provider has no real company behind a symbol at all) should never
        raise for it, just report it as unavailable. `roe`/`dividend_yield`/
        `earnings_growth`/`debt_to_equity` are stock-fundamental fields
        (currently only populated by `YFinanceDataProvider`, see its own
        `fetch_metadata` docstring for the exact yfinance source fields and
        their unit caveats); `expense_ratio`/`total_assets` are fund-level
        fields for ETFs."""
        return {
            "expense_ratio": float("nan"), "total_assets": float("nan"),
            "roe": float("nan"), "dividend_yield": float("nan"),
            "earnings_growth": float("nan"), "debt_to_equity": float("nan"),
        }

    def fetch_financial_indicators(
        self, symbol: str, as_of_date: Optional[str] = None, lookback_years: int = 5
    ) -> FundamentalMetrics:
        """Fetch fundamental financial indicators for a symbol as of a date.
        Returns a default NaN-filled FundamentalMetrics instance.
        """
        year = pd.to_datetime(as_of_date).year if as_of_date else pd.Timestamp.now().year
        stat_date = as_of_date or f"{year}-12-31"
        return FundamentalMetrics(
            symbol=symbol,
            report_year=int(year),
            stat_date=stat_date,
            roe=float("nan"),
            net_profit=float("nan"),
            net_profit_5yr_cagr=float("nan"),
            cfo=float("nan"),
            cfo_to_net_profit=float("nan"),
            total_assets=float("nan"),
            total_liabilities=float("nan"),
            debt_to_assets=float("nan"),
            debt_to_equity=float("nan"),
            interest_bearing_debt=float("nan"),
            dividend_yield=float("nan"),
            dividend_payout_ratio=float("nan"),
            pe_ttm=float("nan"),
            pb=float("nan"),
            total_shares=float("nan"),
            market_cap=float("nan"),
        )


class YFinanceDataProvider(BaseDataProvider):
    """Data provider sourcing OHLCV and fund metadata from Yahoo Finance via yfinance."""

    #: `yf.download(..., auto_adjust=True)` below applies both splits and dividends, i.e. the
    #: series is back-adjusted: `qfq` in this codebase's vocabulary.
    native_adjust_basis = "qfq"

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        df = yf.download(symbol, start=start, end=end, interval=interval, auto_adjust=True, progress=False)
        if df.empty:
            raise ValueError(f"No data returned for {symbol} between {start} and {end}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        df.index = pd.to_datetime(df.index)
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        df = _drop_invalid_ohlcv_rows(df, symbol)
        if df.empty:
            raise ValueError(
                f"Data for {symbol} between {start} and {end} had rows, but every one was dropped as "
                f"logically-impossible OHLC (see the warning above) -- no valid data available."
            )
        return df

    def fetch_metadata(self, symbol: str) -> dict:
        """Best-effort fund/company metadata from yfinance's `.info` -- every
        field defaults to NaN and is only ever overwritten on a successful,
        present, non-None lookup (see `BaseDataProvider.fetch_metadata`'s own
        docstring for why: this must never raise for a symbol missing one
        field, since callers use partial metadata all the time, e.g. an ETF
        has no `roe`/`earnings_growth` and an individual stock usually has no
        `expense_ratio`).

        `roe`/`dividend_yield`/`earnings_growth`/`debt_to_equity` are read
        as-is from yfinance's own `.info` fields with NO unit normalization:
        yfinance has historically been inconsistent about whether
        `dividendYield`/`debtToEquity` are already-decimal fractions or
        percentage-scaled numbers (has varied across yfinance versions) --
        callers must treat these as "yfinance's own convention, whatever that
        happens to be for this install," not a guaranteed-decimal fraction.
        """
        result = {
            "expense_ratio": float("nan"), "total_assets": float("nan"),
            "roe": float("nan"), "dividend_yield": float("nan"),
            "earnings_growth": float("nan"), "debt_to_equity": float("nan"),
        }
        try:
            info = yf.Ticker(symbol).info
        except Exception:
            return result

        # A real yfinance failure mode for delisted/invalid tickers: `.info`
        # returns None (or something else non-dict-like) instead of raising.
        # Without this check, the `.get()` calls below would raise an
        # uncaught AttributeError instead of falling back to the same
        # NaN-filled dict the exception path above already returns.
        if not isinstance(info, dict):
            return result

        for key in ("netExpenseRatio", "annualReportExpenseRatio", "expenseRatio"):
            value = info.get(key)
            if value is not None:
                result["expense_ratio"] = float(value)
                break

        for key in ("totalAssets", "netAssets"):
            value = info.get(key)
            if value is not None:
                result["total_assets"] = float(value)
                break

        for field, keys in (
            ("roe", ("returnOnEquity",)),
            ("dividend_yield", ("dividendYield",)),
            ("earnings_growth", ("earningsGrowth", "earningsQuarterlyGrowth")),
            ("debt_to_equity", ("debtToEquity",)),
        ):
            for key in keys:
                value = info.get(key)
                if value is not None:
                    result[field] = float(value)
                    break

        return result

    def fetch_financial_indicators(
        self, symbol: str, as_of_date: Optional[str] = None, lookback_years: int = 5
    ) -> FundamentalMetrics:
        """Best-effort fundamental indicators from yfinance via `.info` and `.financials` / `.cashflow`."""
        base_metrics = super().fetch_financial_indicators(symbol, as_of_date, lookback_years)
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info if isinstance(ticker.info, dict) else {}
        except Exception:
            return base_metrics

        def _get_float(d: dict, *keys: str) -> float:
            for k in keys:
                v = d.get(k)
                if v is not None:
                    try:
                        val = float(v)
                        if not np.isnan(val):
                            return val
                    except (ValueError, TypeError):
                        pass
            return float("nan")

        roe = _get_float(info, "returnOnEquity")
        pe_ttm = _get_float(info, "trailingPE")
        pb = _get_float(info, "priceToBook")
        dividend_yield = _get_float(info, "dividendYield")
        dividend_payout_ratio = _get_float(info, "payoutRatio")
        total_shares = _get_float(info, "sharesOutstanding")
        market_cap = _get_float(info, "marketCap")
        interest_bearing_debt = _get_float(info, "totalDebt")
        debt_to_equity = _get_float(info, "debtToEquity")
        if not np.isnan(debt_to_equity) and debt_to_equity > 10.0:
            debt_to_equity = debt_to_equity / 100.0

        industry_name = info.get("industry") or info.get("sector") or "Unknown"

        net_profit = float("nan")
        cagr_5yr = float("nan")
        cfo = float("nan")
        total_assets = _get_float(info, "totalAssets", "netAssets")
        total_liabilities = float("nan")
        stat_date = base_metrics.stat_date
        report_year = base_metrics.report_year

        try:
            fin = getattr(ticker, "financials", None)
            if fin is not None and not fin.empty:
                ni_rows = [idx for idx in fin.index if "net income" in str(idx).lower()]
                if ni_rows:
                    ni_series = fin.loc[ni_rows[0]].dropna()
                    if not ni_series.empty:
                        net_profit = float(ni_series.iloc[0])
                        col_date = ni_series.index[0]
                        if hasattr(col_date, "year"):
                            report_year = int(col_date.year)
                            stat_date = str(col_date.strftime("%Y-%m-%d"))
                        if len(ni_series) >= 2:
                            k = min(len(ni_series) - 1, lookback_years)
                            val_start = float(ni_series.iloc[k])
                            val_end = float(ni_series.iloc[0])
                            if val_start > 0 and val_end > 0 and k > 0:
                                cagr_5yr = float((val_end / val_start) ** (1.0 / k) - 1.0)
        except Exception:
            pass

        try:
            cf = getattr(ticker, "cashflow", None)
            if cf is not None and not cf.empty:
                cfo_rows = [
                    idx for idx in cf.index
                    if any(term in str(idx).lower() for term in ["operating cash flow", "cash flow from continuing operating activities", "total cash from operating activities"])
                ]
                if cfo_rows:
                    cfo_series = cf.loc[cfo_rows[0]].dropna()
                    if not cfo_series.empty:
                        cfo = float(cfo_series.iloc[0])
        except Exception:
            pass

        try:
            bs = getattr(ticker, "balance_sheet", None)
            if bs is not None and not bs.empty:
                if np.isnan(total_assets):
                    ta_rows = [idx for idx in bs.index if "total assets" in str(idx).lower()]
                    if ta_rows:
                        ta_series = bs.loc[ta_rows[0]].dropna()
                        if not ta_series.empty:
                            total_assets = float(ta_series.iloc[0])

                tl_rows = [
                    idx for idx in bs.index
                    if any(term in str(idx).lower() for term in ["total liabilities net minority interest", "total liabilities"])
                ]
                if tl_rows:
                    tl_series = bs.loc[tl_rows[0]].dropna()
                    if not tl_series.empty:
                        total_liabilities = float(tl_series.iloc[0])
        except Exception:
            pass

        cfo_to_net_profit = (
            float(cfo / net_profit) if (not np.isnan(cfo) and not np.isnan(net_profit) and net_profit != 0) else float("nan")
        )
        debt_to_assets = (
            float(total_liabilities / total_assets)
            if (not np.isnan(total_liabilities) and not np.isnan(total_assets) and total_assets > 0)
            else float("nan")
        )
        if np.isnan(debt_to_equity) and not np.isnan(total_liabilities) and not np.isnan(total_assets):
            equity = total_assets - total_liabilities
            if equity > 0:
                debt_to_equity = float(total_liabilities / equity)

        return FundamentalMetrics(
            symbol=symbol,
            report_year=report_year,
            stat_date=stat_date,
            roe=roe,
            net_profit=net_profit,
            net_profit_5yr_cagr=cagr_5yr,
            cfo=cfo,
            cfo_to_net_profit=cfo_to_net_profit,
            total_assets=total_assets,
            total_liabilities=total_liabilities,
            debt_to_assets=debt_to_assets,
            debt_to_equity=debt_to_equity,
            interest_bearing_debt=interest_bearing_debt,
            dividend_yield=dividend_yield,
            dividend_payout_ratio=dividend_payout_ratio,
            pe_ttm=pe_ttm,
            pb=pb,
            total_shares=total_shares,
            market_cap=market_cap,
            industry_name=industry_name,
        )


class CSVFolderDataProvider(BaseDataProvider):
    """Data provider reading historical CSV files from a specified folder."""

    #: The legacy CSV disk cache this reads was populated from `fetch_ohlcv` results, which every
    #: consumer read with the default `adjust="qfq"`. Declaring it makes the label explicit and
    #: keeps `raw`/`hfq` requests fail-closed rather than silently answered with qfq bytes.
    native_adjust_basis = "qfq"

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
        df = _drop_invalid_ohlcv_rows(df, symbol)
        if df.empty:
            raise ValueError(
                f"CSV data for {symbol} between {start} and {end} had rows, but every one was dropped "
                f"as logically-impossible OHLC (see the warning above) -- no valid data available."
            )
        return df


def _generate_bridge_segment(
    t_a: int,
    t_b: int,
    p_a: float,
    p_b: float,
    low_d: float,
    high_d: float,
    intra_rng: np.random.Generator,
) -> np.ndarray:
    """Generates a pinned 1D Brownian bridge on discrete steps [t_a, t_b] connecting p_a to p_b."""
    n = t_b - t_a
    if n <= 0:
        return np.array([p_a])
    if n == 1:
        return np.array([p_a, p_b])

    t = np.arange(n + 1)
    dt = 1.0 / n
    dW = intra_rng.normal(0.0, np.sqrt(dt), size=n)
    W = np.concatenate([[0.0], np.cumsum(dW)])
    B = W - (t / n) * W[-1]

    span = high_d - low_d
    sigma = span * 0.15
    X = p_a + (t / n) * (p_b - p_a) + B * sigma
    X = np.clip(X, low_d, high_d)
    X[0] = p_a
    X[-1] = p_b
    return X


class SyntheticDataProvider(BaseDataProvider):
    """Data provider generating synthetic geometric Brownian motion OHLCV data."""

    def __init__(self, seed: int = 42, drift: float = 0.0003, vol: float = 0.01):
        self.seed = seed
        self.drift = drift
        self.vol = vol

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        if not is_intraday_interval(interval):
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

        # Intraday generation via Piecewise Pinned Brownian Bridge
        start_dt = pd.to_datetime(start) if start else pd.to_datetime("2020-01-01")
        end_dt = pd.to_datetime(end) if end else start_dt + pd.Timedelta(days=1200)

        # Generate base daily data using the exact same daily logic
        daily_start_str = start_dt.strftime("%Y-%m-%d")
        daily_end_str = end_dt.strftime("%Y-%m-%d")
        daily_df = self.fetch_ohlcv(symbol, start=daily_start_str, end=daily_end_str, interval="1d")

        symbol_seed = self.seed + (int(hashlib.md5(symbol.encode("utf-8")).hexdigest(), 16) % 10000)
        intra_rng = np.random.default_rng(symbol_seed + 1_000_000)

        intraday_days: List[pd.DataFrame] = []

        for date_idx, daily_row in daily_df.iterrows():
            d = date_idx.date()
            date_str = d.strftime("%Y-%m-%d")

            # 48 A-share 5m timestamps: 24 morning + 24 afternoon
            m_times = pd.date_range(f"{date_str} 09:35:00", f"{date_str} 11:30:00", freq="5min")
            a_times = pd.date_range(f"{date_str} 13:05:00", f"{date_str} 15:00:00", freq="5min")
            day_times = m_times.append(a_times)

            O_d = float(daily_row["Open"])
            H_d = float(daily_row["High"])
            L_d = float(daily_row["Low"])
            C_d = float(daily_row["Close"])
            V_d = float(daily_row["Volume"])

            # R2 Piecewise Pinned Brownian Bridge
            j_H, j_L = intra_rng.choice(np.arange(1, 47), size=2, replace=False)

            if j_H < j_L:
                j_1, P_1 = int(j_H), H_d
                j_2, P_2 = int(j_L), L_d
            else:
                j_1, P_1 = int(j_L), L_d
                j_2, P_2 = int(j_H), H_d

            # Allocate 48 close prices
            C = np.zeros(48, dtype=float)
            seg1 = _generate_bridge_segment(0, j_1, O_d, P_1, L_d, H_d, intra_rng)
            C[0 : j_1 + 1] = seg1
            seg2 = _generate_bridge_segment(j_1, j_2, P_1, P_2, L_d, H_d, intra_rng)
            C[j_1 : j_2 + 1] = seg2
            seg3 = _generate_bridge_segment(j_2, 47, P_2, C_d, L_d, H_d, intra_rng)
            C[j_2 : 48] = seg3

            C = np.clip(C, L_d, H_d)
            C[0] = O_d
            C[47] = C_d

            # Bar opens
            O = np.empty(48, dtype=float)
            O[0] = O_d
            O[1:] = C[:-1]

            # Bar highs and lows
            upper = np.maximum(O, C)
            lower = np.minimum(O, C)
            span = H_d - L_d
            if span > 1e-8:
                h_noise = np.abs(intra_rng.normal(0.0, span * 0.05, size=48))
                l_noise = np.abs(intra_rng.normal(0.0, span * 0.05, size=48))
                H = np.clip(upper + h_noise, upper, H_d)
                L = np.clip(lower - l_noise, L_d, lower)
            else:
                H = upper.copy()
                L = lower.copy()

            # Explicitly pin extrema
            H[j_H] = H_d
            L[j_L] = L_d

            # U-shaped intraday volume profile
            j_idx = np.arange(48)
            u = 1.0 + 1.8 * (((j_idx - 23.5) / 23.5) ** 2)
            noise_v = intra_rng.normal(0.0, 0.15, size=48)
            u_tilde = u * np.exp(noise_v)
            weights = u_tilde / np.sum(u_tilde)
            V = V_d * weights
            # Exact sum conservation to machine precision
            V[-1] += V_d - V.sum()

            day_df = pd.DataFrame(
                {
                    "Open": O,
                    "High": H,
                    "Low": L,
                    "Close": C,
                    "Volume": V,
                },
                index=day_times,
            )
            intraday_days.append(day_df)

        if not intraday_days:
            return pd.DataFrame(columns=_REQUIRED_OHLCV_COLUMNS)

        full_intra_df = pd.concat(intraday_days, axis=0)
        full_intra_df.index.name = "Date"

        # Filter by requested start and end timestamps if needed
        if start:
            start_ts = pd.to_datetime(start)
            full_intra_df = full_intra_df[full_intra_df.index >= start_ts]
        if end:
            end_str = str(end).strip()
            if len(end_str) <= 10 or ":" not in end_str:
                end_ts = pd.to_datetime(end).replace(hour=23, minute=59, second=59, microsecond=999999)
            else:
                end_ts = pd.to_datetime(end)
            full_intra_df = full_intra_df[full_intra_df.index <= end_ts]

        if interval == "5m" or interval == "1m":
            return full_intra_df

        rule_map = {
            "15m": "15min",
            "30m": "30min",
            "60m": "60min",
            "1h": "1h",
        }
        rule = rule_map.get(interval.lower(), None)
        if rule:
            agg_spec = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
            resampled = full_intra_df.resample(rule, closed="right", label="right").agg(agg_spec).dropna(subset=["Open", "High", "Low", "Close"])
            return resampled

        return full_intra_df


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


#: Providers already reported as "has `fetch_raw_ohlcv` but cannot supply adjustment factors"
#: (either no `fetch_adjust_factors` at all, or `adjust_factor_source_available=False`).
#: One warning per provider type, not per symbol: a 22-symbol universe would otherwise emit 22
#: copies of the same operational fact.
_FACTOR_CAPABILITY_GAP_WARNED: set = set()

#: Providers already reported as "declares no adjustment basis, so `qfq` was assumed".
#: Same once-per-provider policy as `_FACTOR_CAPABILITY_GAP_WARNED`.
_ASSUMED_BASIS_WARNED: set = set()


def _provider_native_basis(provider: BaseDataProvider) -> Optional[str]:
    """Best declared answer to "which adjustment basis does `fetch_ohlcv` return?".

    Order: the explicit `native_adjust_basis` attribute, then the legacy per-provider knobs this
    repo already used (`adjustflag` on BaoStock, `adjust` on EastMoney/Fuyao/MarketDB, `fqt` on
    Tushare-style providers). Returns `None` when the provider declares nothing -- callers must
    then decide (the legacy cache path assumes `qfq`, see
    `BaseDataProvider.ASSUMED_NATIVE_BASIS_WHEN_UNKNOWN`; `rebase_corporate_action` does the same
    and says so).
    """
    for attr in ("native_adjust_basis", "adjustflag", "adjust", "fqt"):
        raw = getattr(provider, attr, None)
        if raw is None:
            continue
        try:
            return canonicalize_adjust_mode(raw)
        except ValueError:
            # An unrecognised knob value tells us nothing -- keep looking rather than guess.
            continue
    return None


def _warn_factor_capability_gap(provider_name: str, symbol: str) -> None:
    """Explain the legacy fallback taken for a provider that cannot supply adjustment factors.

    Two causes reach here, and the wording has to cover both:
      * the provider has no `fetch_adjust_factors` method at all (e.g. `EastMoneyDataProvider`);
      * the provider has the method but declares
        `adjust_factor_source_available=False`, meaning its factor source is unreachable right now
        -- `FuyaoDataProvider` does exactly that when no local market.duckdb could be opened.

    M3.6 review F3: this used to be a `MissingAdjustFactorsError` raised on every cache miss for
    providers with `fetch_raw_ohlcv` but no factor source, which `fetch_universe`'s generic handler
    downgraded to `warnings.warn` + skip -- i.e. a silently SMALLER universe, not a visible
    failure. The fallback below is legitimate (their own `fetch_ohlcv` already applies their native
    basis), so it is taken silently-except-once instead of raised. The capability flag must NOT be
    abused to swallow a genuine data failure: a provider that says it HAS a source but returns an
    empty factor table still raises (`MissingAdjustFactorsError`).
    """
    if provider_name in _FACTOR_CAPABILITY_GAP_WARNED:
        return
    _FACTOR_CAPABILITY_GAP_WARNED.add(provider_name)
    warnings.warn(
        f"{provider_name} exposes fetch_raw_ohlcv but cannot supply adjustment factors "
        f"(first seen for {symbol!r}): either it has no fetch_adjust_factors() method, or it reports "
        f"adjust_factor_source_available=False because its factor source is not reachable right now "
        f"(FuyaoDataProvider does that when it has no local market.duckdb). Falling back to its own "
        f"fetch_ohlcv and recording the provider's native adjustment basis. No raw bars and no "
        f"adjustment factors are stored, so NO read-time derivation happens for this provider -- the "
        f"cached series keeps whatever basis the provider natively returns, and a request for "
        f"another basis fails loudly instead of being silently rescaled."
    )


class CachedDataProvider(BaseDataProvider):
    """Wrapper provider that adds DuckDB disk caching around any inner BaseDataProvider.

    Bars and metadata are keyed strictly by (provider, symbol) in DuckDB (cache.duckdb).
    Base daily bars are cached once and can serve any requested date range and interval.
    Tracks each asset's earliest available date from the upstream provider to avoid
    re-querying pre-inception date spans. Cached historical data never expires.
    Synthetic data generators (SyntheticDataProvider) always bypass the cache.
    """

    def __init__(self, inner_provider: BaseDataProvider, cache_dir: str, derive_mode: str = "auto"):
        """`derive_mode` (M3.6 review F4) is the runtime switch for in-cache derivation:

          * `"auto"` (default) -- store raw bars + factors on a cache miss and derive the
            requested `raw`/`qfq`/`hfq` on read. This is the normal path;
          * `"direct"` -- never use `fetch_raw_ohlcv` and never write the `adjust_factors` table:
            every miss falls through to the provider's own `fetch_ohlcv`, whose result is stored
            with the provider's native basis. This is the emergency fallback for a suspected
            factor-calibration defect: the cache keeps serving, but NOTHING is derived -- neither
            on write nor on read. That second half used to be missing (the read path ignored
            `derive_mode`, so a cache already holding raw bars + factors kept deriving and could
            be rescaled by the suspected-wrong factor anyway); it is now enforced by
            `_guard_direct_mode_derivation`, which raises `ValueError` for a `qfq`/`hfq` read of
            stored raw bars instead of deriving them. Switching back to `"auto"` is safe only
            after `AdjustBasisTransitionRefused` has been resolved for the affected symbols
            (a `direct` write never stamps a basis onto already-stored undeclared rows).

        The `derive_mode` is validated eagerly: a typo must not silently mean "auto".
        """
        mode = str(derive_mode if derive_mode is not None else "auto").strip().lower()
        if mode not in ("auto", "direct"):
            raise ValueError(
                f"CachedDataProvider(derive_mode={derive_mode!r}) is not supported: expected "
                f"'auto' (store raw + derive on read) or 'direct' (legacy pass-through, no factor "
                f"derivation)."
            )
        self.derive_mode = mode
        self.inner_provider = inner_provider
        self.cache_dir = cache_dir
        self.is_synthetic = is_synthetic_provider(inner_provider)
        if not self.is_synthetic and self.cache_dir:
            self.duckdb_cache = DuckDBCache(db_path=self.cache_dir)
        else:
            self.duckdb_cache = None

    def _guard_direct_mode_derivation(
        self, provider_name: str, symbol: str, interval: str, adjust: str
    ) -> None:
        """`derive_mode="direct"` must be honest on the READ path (M3.6 review P1/P2, item 2).

        `direct` promises "nothing is derived in this cache". The first version only changed the
        WRITE path (`is_raw_inner` was forced False), so a cache that already held raw bars plus
        factors -- written while the provider was in `auto` -- kept deriving qfq/hfq on read. The
        docstring's claim ("nothing new is derived, so no read can be rescaled by a wrong factor")
        was therefore false for exactly the case it was written to cover, which is the whole point
        of the emergency switch.

        The guard refuses to serve a DERIVED series out of a stored `raw` row in `direct` mode:
          * stored basis `raw` + requested `qfq`/`hfq` -> `ValueError` (the caller asked for
            something this mode is telling it it cannot produce honestly);
          * requested `raw`/`none` -> pass-through, always safe (no derivation was ever needed);
          * stored basis declared `qfq`/`hfq` -> pass-through (already the requested series, and
            `_resolve_read_basis` still refuses a declared-but-different basis);
          * no declaration (`NULL` / sentinel) -> not this guard's business; `_resolve_read_basis`
            keeps its own fail-closed `UnknownAdjustBasisError` / `allow_legacy_basis` contract;
          * factor-neutral symbols -> exempt, `_apply_factors_to_df` is the identity for them.

        Note `allow_legacy_basis` is deliberately NOT an escape hatch here: it exists for rows
        whose basis was never recorded, and would return raw bytes to a qfq request -- i.e. the
        silent wrong-basis read this blocker exists to prevent. Switch back to `derive_mode="auto"`
        (after resolving whatever factor calibration triggered `direct`) to get adjusted series.
        """
        if self.derive_mode != "direct":
            return
        if is_factor_neutral_symbol(symbol):
            return
        req_mode = canonicalize_adjust_mode(adjust)
        if req_mode not in ("qfq", "hfq"):
            return
        if is_intraday_interval(interval):
            meta = self.duckdb_cache.get_intraday_metadata(provider_name, symbol, interval)
        else:
            meta = self.duckdb_cache.get_asset_metadata(provider_name, symbol)
        stored_basis = parse_stored_basis(meta.get("adjustflag") if meta else None)
        if stored_basis != "raw":
            return
        raise ValueError(
            f"CachedDataProvider(derive_mode='direct') refuses to serve adjust='{req_mode}' for "
            f"{symbol} ({provider_name}, interval={interval!r}): the cache holds RAW bars and "
            f"direct mode has derivation switched off, so the only alternative would be to return "
            f"unadjusted prices under an adjusted label (a percent-level error that looks like a "
            f"valid series). Read it with `adjust='raw'`, or re-open the provider with "
            f"derive_mode='auto' to derive the requested basis from the stored raw bars + factors."
        )

    def get_bars(
        self,
        symbol: str,
        start: str,
        end: str,
        interval: str = "1d",
        adjust: str = "qfq",
        expected_last_session=None,
        *,
        columns: Optional[Union[str, Sequence[str]]] = None,
        allow_legacy_basis: bool = False,
    ) -> pd.DataFrame:
        """Fetch bars, serving from DuckDB when covered and deriving adjust dynamically.

        When caching raw bars, stores raw bars and factors, deriving requested adjust on read.

        `columns` / `allow_legacy_basis` are forwarded to `DuckDBCache.query_bars` unchanged, so
        the cache's `columns=` contract (default: required OHLCV only, no `Amount`) and its
        legacy-basis opt-in apply verbatim to every cached read here.

        `derive_mode="direct"` (M3.6 review F4, corrected in the P1 pass) is enforced on the READ
        path too: see `_guard_direct_mode_derivation`. Before the correction it only changed the
        write path, so a cache that already held raw bars + factors kept deriving -- i.e. the
        "no read can be rescaled by a wrong factor" claim was false for exactly the case it was
        meant to cover.
        """
        _validate_symbol_for_path(symbol)
        if self.is_synthetic or not self.cache_dir or self.duckdb_cache is None:
            return self.inner_provider.fetch_ohlcv(symbol, start, end, interval)
        provider_name = type(self.inner_provider).__name__

        # 1. Fast cache hit check: is [start, end] covered in DuckDB?
        # (2026-10-22 incident) This gate is the single choke point through which every
        # freshness defect in `DuckDBCache.is_range_covered` reaches ALL consumers -- Stage-1
        # (`load_universe`) and Stage-2 both read through `CachedDataProvider`, which is why
        # they showed identical stale prices (one poisoned cache read twice, not two
        # independent errors). `is_range_covered` requires the stored bars to actually
        # reach `end` (bar layer), and it is now the ONLY freshness gate: the CSV disk cache
        # this provider once kept (and its lenient 7-day-tolerance helper) is gone, so there
        # is no second lenient path left to keep in sync.
        if self.duckdb_cache.is_range_covered(
            provider_name,
            symbol,
            start=start,
            end=end,
            interval=interval,
            expected_last_session=expected_last_session,
        ):
            self._guard_direct_mode_derivation(provider_name, symbol, interval, adjust)
            df = self.duckdb_cache.query_bars(
                provider_name,
                symbol,
                start=start,
                end=end,
                interval=interval,
                adjust=adjust,
                columns=columns,
                allow_legacy_basis=allow_legacy_basis,
            )
            if not df.empty:
                return df

        # 2. Re-query provider for missing range.
        # Check if inner provider supports fetching raw unadjusted bars.
        #
        # M3.6 review F3: "has `fetch_raw_ohlcv`" is NOT the same capability as "can supply the
        # factors that raw bars need to be re-based". A provider can lack the factor source in two
        # ways -- no `fetch_adjust_factors` method at all (`EastMoneyDataProvider`), or the method
        # exists but declares `adjust_factor_source_available=False` because its factor table is
        # unreachable right now (`FuyaoDataProvider` without a local market.duckdb). The old code
        # raised `MissingAdjustFactorsError` for both on EVERY miss -- an exception `fetch_universe`
        # downgraded to a warn+skip, i.e. a silently SHRUNKEN universe (and, for Fuyao, it turned a
        # working provider into a hard failure the moment it had no local DB). The capability gap is
        # handled where it belongs: fall back to the provider's own `fetch_ohlcv` (legacy direct
        # path), store the provider's native basis, write no raw bars and no factors, and say so
        # once. A genuine upstream factor failure from a provider that DOES have a source still
        # raises (fail-closed) -- see `_EmptyFactorProviderL236`'s regression test.
        is_raw_inner = self.derive_mode == "auto" and hasattr(self.inner_provider, "fetch_raw_ohlcv")
        # Capability, not introspection (M3.8): `adjust_factor_source_available` defaults to True, so
        # providers that really do read a factor table keep the fail-closed behaviour above.
        has_factor_source = hasattr(
            self.inner_provider, "fetch_adjust_factors"
        ) and bool(getattr(self.inner_provider, "adjust_factor_source_available", True))
        req_mode = canonicalize_adjust_mode(adjust)

        if is_raw_inner:
            stored_basis = "raw"
            # If storing raw bars, fetch and store factors BEFORE storing bars.
            # Factor fetch errors are fail-closed: do NOT swallow exceptions.
            if not is_factor_neutral_symbol(symbol):
                if has_factor_source:
                    factors = self.inner_provider.fetch_adjust_factors(symbol, start=start, end=end)
                    if factors is None or factors.empty:
                        raise MissingAdjustFactorsError(symbol)
                    self.duckdb_cache.store_adjust_factors(symbol, factors, source=provider_name)
                else:
                    is_raw_inner = False
                    _warn_factor_capability_gap(provider_name, symbol)

        if is_raw_inner:
            if is_intraday_interval(interval):
                df = self.inner_provider.fetch_raw_ohlcv(symbol, start, end, interval=interval)
            else:
                try:
                    df = self.inner_provider.fetch_raw_ohlcv(symbol, start, end, interval="1d")
                except Exception:
                    df = self.inner_provider.fetch_raw_ohlcv(symbol, start, end, interval=interval)
        else:
            # Inner provider cannot be re-based from raw (no `fetch_raw_ohlcv`, no usable
            # `fetch_adjust_factors` source (`adjust_factor_source_available=False`), or
            # `derive_mode="direct"`): fall back to legacy `fetch_ohlcv`
            # and record the provider's native basis without deriving/storing adjustment factors.
            #
            # M3.6 review P1: `store_bars` now REFUSES an omitted basis, so this path must always
            # declare one. `_provider_native_basis` answers for every provider that exposes a knob
            # (`native_adjust_basis` / `adjustflag` / `adjust` / `fqt`); a provider that declares
            # nothing gets the documented assumption (`ASSUMED_NATIVE_BASIS_WHEN_UNKNOWN`, `qfq`
            # -- the pre-M3.6 read default), logged so it is not silent, and it stays FAIL-CLOSED
            # for `raw`/`hfq` requests rather than answering them with possibly-wrong bytes.
            native_basis = _provider_native_basis(self.inner_provider)
            if native_basis is None:
                native_basis = BaseDataProvider.ASSUMED_NATIVE_BASIS_WHEN_UNKNOWN
                key = f"{provider_name}:assumed-basis"
                if key not in _ASSUMED_BASIS_WARNED:
                    _ASSUMED_BASIS_WARNED.add(key)
                    logger.warning(
                        "%s declares no adjustment basis (no native_adjust_basis/adjustflag/"
                        "adjust/fqt) and supplies no raw bars+factors: caching its series as "
                        "'%s'. Requests for another basis will fail closed. If the series is not "
                        "%s, set `native_adjust_basis` on the provider or declare the truth with "
                        "scripts/migrate_adjust_basis.py --set-basis ... --force.",
                        provider_name,
                        native_basis,
                        native_basis,
                    )
            stored_basis = native_basis

            if is_intraday_interval(interval):
                df = self.inner_provider.fetch_ohlcv(symbol, start, end, interval=interval)
            else:
                try:
                    df = self.inner_provider.fetch_ohlcv(symbol, start, end, interval="1d")
                except Exception:
                    df = self.inner_provider.fetch_ohlcv(symbol, start, end, interval=interval)

        if df.empty:
            raise ValueError(
                f"{provider_name} returned no data for {symbol} between {start} and {end}."
            )

        df = _drop_invalid_ohlcv_rows(df, symbol)
        if df.empty:
            raise ValueError(
                f"Data for {symbol} between {start} and {end} had rows, but every one was dropped as invalid OHLC."
            )

        # 3. Store into DuckDB cache (tracks earliest available date/ts and basis marker)
        self.duckdb_cache.store_bars(
            provider_name,
            symbol,
            df,
            requested_start=start,
            requested_end=end,
            interval=interval,
            adjustflag=stored_basis,
        )

        # 4. Return queried and properly resampled bars for requested range and interval.
        # The direct-mode guard runs here too: the branch above may have re-fetched through
        # `fetch_ohlcv`, but the symbol's metadata can still say `raw` (e.g. only part of the
        # requested range was missing), and a derived read would then be dishonest.
        self._guard_direct_mode_derivation(provider_name, symbol, interval, adjust)
        return self.duckdb_cache.query_bars(
            provider_name,
            symbol,
            start=start,
            end=end,
            interval=interval,
            adjust=adjust,
            columns=columns,
            allow_legacy_basis=allow_legacy_basis,
        )

    def get_intraday_bars(
        self,
        symbol: str,
        start: str,
        end: str,
        interval: str = "5m",
        adjustflag: str = "qfq",
        expected_last_session=None,
        *,
        columns: Optional[Union[str, Sequence[str]]] = None,
        allow_legacy_basis: bool = False,
    ) -> pd.DataFrame:
        """Retrieves intraday OHLCV bars with dynamic adjustflag derivation."""
        return self.get_bars(
            symbol,
            start=start,
            end=end,
            interval=interval,
            adjust=adjustflag,
            expected_last_session=expected_last_session,
            columns=columns,
            allow_legacy_basis=allow_legacy_basis,
        )

    def fetch_ohlcv(
        self,
        symbol: str,
        start: str,
        end: str,
        interval: str = "1d",
        expected_last_session=None,
        adjust: str = "qfq",
        *,
        columns: Optional[Union[str, Sequence[str]]] = None,
        allow_legacy_basis: bool = False,
    ) -> pd.DataFrame:
        """Fetch bars, serving from DuckDB when `is_range_covered` says the range is on disk.

        `expected_last_session` (L2.2) is forwarded to `is_range_covered` unchanged. It is
        `None` for EVERY backtest/research caller (`backtester/*`, `pipeline/*`,
        `common/order_netting.py`, `run_live_multi_strategy.py`) -- that is the guard which keeps
        backtest results independent of the wall clock (design §11). Only live paths that already
        know the trading session the cache must hold (via
        `common/market_clock.py::expected_last_session`) may pass a date here.
        """
        return self.get_bars(
            symbol,
            start=start,
            end=end,
            interval=interval,
            adjust=adjust,
            expected_last_session=expected_last_session,
            columns=columns,
            allow_legacy_basis=allow_legacy_basis,
        )

    def fetch_universe(
        self,
        symbols: List[str],
        start: str,
        end: str,
        interval: str = "1d",
        expected_last_session=None,
        adjust: str = "qfq",
        *,
        columns: Optional[Union[str, Sequence[str]]] = None,
        allow_legacy_basis: bool = False,
    ) -> Dict[str, pd.DataFrame]:
        """Cache-backed multi-symbol fetch; a refused history rewrite is a HARD failure.

        The per-symbol `try/except Exception` below is intentional: one broken symbol (bad
        name, upstream outage, empty result) must not sink a whole universe, so those are
        warned about and skipped -- that behaviour is unchanged.

        `HistoryRewriteRefused` (L2.32, `common/duckdb_cache.py::store_bars`) must NOT take that
        path. It does not mean "this symbol is unavailable"; it means the cache already holds
        MATERIALLY DIFFERENT values for dates this fetch is about to overwrite -- a QFQ/basis
        change, not a missing bar. Dropping such a symbol leaves the caller with a *smaller*
        universe and downstream results that still look normal, which is the exact failure class
        of the 2026-10-23 incident (a refetch `INSERT OR REPLACE`d 13,303 already-repaired rows)
        wearing a different costume: "silent history rewrite" replaced by "silent symbol drop".
        L2.32 review finding: because the refusal subclasses `RuntimeError` it landed in that
        generic branch, so the only audit trail was a `warnings.warn` and a walk-forward batch
        happily continued on a shrunken universe.

        Therefore refusals are collected and re-raised as `RuntimeError` AFTER the loop, naming
        every refused symbol and chaining the first refusal as `__cause__`. Fixing the basis is
        a deliberate, auditable act (`scripts/repair_qfq_alignment.py`, or an explicitly declared
        `store_bars(..., allow_history_rewrite=True, rewrite_reason=...)`), never a side effect
        of a routine batch run that silently continued.

        M3.6 review F3/P2: the three OTHER basis-data exceptions are never warned-and-skipped,
        and they are collected rather than raised on the first one --
          * `MissingAdjustFactorsError` -- the provider cannot supply the factors its raw bars
            need, so nothing about this symbol can be derived. Skipping it shrank the universe
            silently (this is how `FuyaoDataProvider` / `EastMoneyDataProvider` used to disappear
            from a run). The capability gap itself is now handled inside `get_bars` by falling
            back to the legacy direct path, so what reaches here is a genuine upstream failure;
          * `UnknownAdjustBasisError` -- stored bars whose basis was never recorded
            (`scripts/migrate_adjust_basis.py`);
          * `AdjustBasisTransitionRefused` -- a write tried to stamp a basis onto such rows.
        All three mean "this cache cannot be trusted for this symbol", which is emphatically not
        "this symbol is unavailable". Fail-closed, but AGGREGATED (M3.6 review P2): every affected
        symbol is tried and reported in ONE `RuntimeError`, so a 22-symbol book with 6 bad rows
        needs one run (and one fix list), not six sequential runs that each stop at the next bad
        symbol. The first failure is chained as `__cause__`.
        """
        if self.is_synthetic or not self.cache_dir or self.duckdb_cache is None:
            return super().fetch_universe(symbols, start, end, interval, columns=columns)

        provider_name = type(self.inner_provider).__name__
        covered_symbols = []
        #: Refused-rewrite reports, kept apart from `covered_symbols` so they can hard-fail
        #: instead of being skipped (see docstring).
        refused: List[str] = []
        refusal_cause: Optional[HistoryRewriteRefused] = None
        #: Basis-integrity failures: NEVER skipped (F3), but COLLECTED (P2) so one run reports the
        #: whole fix list instead of just the first symbol.
        basis_reports: List[str] = []
        basis_cause: Optional[BaseException] = None
        basis_failures = (
            MissingAdjustFactorsError,
            UnknownAdjustBasisError,
            AdjustBasisTransitionRefused,
        )

        for symbol in symbols:
            try:
                _validate_symbol_for_path(symbol)
                # `expected_last_session` is threaded through BOTH the coverage probe and the
                # re-fetch so a per-symbol decision cannot disagree with itself.
                if self.duckdb_cache.is_range_covered(
                    provider_name,
                    symbol,
                    start=start,
                    end=end,
                    interval=interval,
                    expected_last_session=expected_last_session,
                ):
                    covered_symbols.append(symbol)
                else:
                    self.fetch_ohlcv(
                        symbol, start, end, interval=interval,
                        expected_last_session=expected_last_session,
                        adjust=adjust,
                    )
                    covered_symbols.append(symbol)
            except basis_failures as exc:
                # ORDER MATTERS: checked BEFORE the generic branch. These used to be downgraded to
                # a `warnings.warn` + skipped symbol, i.e. a smaller universe that downstream
                # stages could not tell apart from a smaller list. Collect instead of raising on
                # the first one (P2): the operator gets the full fix list from a single run.
                basis_reports.append(f"{symbol}: {type(exc).__name__}: {exc}")
                if basis_cause is None:
                    basis_cause = exc
                continue
            except Exception as exc:
                # ORDER MATTERS: `HistoryRewriteRefused` subclasses `RuntimeError` -> `Exception`,
                # so without the isinstance test first it would be downgraded to a skipped symbol
                # and the run would continue on a shrunken universe (L2.32 review finding).
                if isinstance(exc, HistoryRewriteRefused):
                    refused.append(f"{symbol}: {exc}")
                    if refusal_cause is None:
                        refusal_cause = exc
                    continue
                # Unchanged legacy behaviour: an unrelated provider/symbol failure only warns and
                # skips that symbol -- do not turn ordinary upstream flakiness into a hard error.
                warnings.warn(f"Skipping {symbol}: {exc}")

        if basis_reports:
            raise RuntimeError(
                f"fetch_universe aborted: {len(basis_reports)} of {len(symbols)} symbol(s) have "
                "untrustworthy adjustment-basis state, so returning the symbols that succeeded "
                "would silently shrink the universe (one run reports ALL of them, M3.6 review P2). "
                "Failing -- " + " | ".join(basis_reports)
                + ". Each one is either (a) a provider that cannot supply the factors its raw "
                "bars need, (b) a cache row whose basis was never declared "
                "(`python scripts/migrate_adjust_basis.py --dry-run` lists them; then "
                "--set-basis raw|qfq|hfq), or (c) a write that tried to stamp a basis onto such "
                "a row (declare the transition, or delete and re-fetch the symbol)."
            ) from basis_cause

        if refused:
            raise RuntimeError(
                f"fetch_universe aborted: {len(refused)} of {len(symbols)} symbol(s) were refused "
                "by store_bars' history-rewrite gate, so returning the symbols that succeeded "
                "would silently shrink the universe (2026-10-23 incident class). Refused -- "
                + " | ".join(refused)
                + ". Do not continue past this: either the stored history is correct and the "
                "upstream basis changed (route the re-derivation through "
                "scripts/repair_qfq_alignment.py), or the rewrite is genuinely intended and must "
                "be declared explicitly via allow_history_rewrite=True + rewrite_reason before "
                "re-running."
            ) from refusal_cause

        # `derive_mode="direct"` honesty on the read path (item 2): a universe read must not
        # derive qfq/hfq out of stored raw bars behind the caller's back. Checked for every
        # covered symbol BEFORE the single `query_universe` call (which would raise the same
        # error anyway, but only for the first symbol it happens to visit).
        for symbol in covered_symbols:
            self._guard_direct_mode_derivation(provider_name, symbol, interval, adjust)

        return self.duckdb_cache.query_universe(
            provider_name,
            covered_symbols,
            start=start,
            end=end,
            interval=interval,
            adjust=adjust,
            columns=columns,
            allow_legacy_basis=allow_legacy_basis,
        )

    def rebase_corporate_action(
        self,
        symbol: str,
        start: str,
        end: str,
        reason: str,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Administrative method to rebase cached bars following a corporate action (L2.33).

        Validates that a descriptive audit reason is provided (at least 10 non-whitespace characters),
        fetches fresh data from the inner provider, and stores it into the DuckDB cache
        with `allow_history_rewrite=True` and an audit reason prefix.
        Routine `fetch_ohlcv` and `fetch_universe` strictly enforce `allow_history_rewrite=False`
        so unannounced historical overwrites fail loud.

        M3.6 review P1: the rebase must DECLARE the basis of the bytes it writes. It fetches
        through the inner provider's own `fetch_ohlcv`, so the truthful label is exactly that
        provider's native basis (`_provider_native_basis`: `native_adjust_basis` / `adjustflag` /
        `adjust` / `fqt`). A provider that declares nothing gets the documented assumption
        `ASSUMED_NATIVE_BASIS_WHEN_UNKNOWN` (`qfq`, the pre-M3.6 read default) -- logged, and
        still fail-closed for `raw`/`hfq` requests. `allow_undeclared_basis=True` is deliberately
        NOT used here: this method exists to repair a known corporate action, so it always knows
        enough to claim a basis, and an undeclared row would make its own return value unreadable.
        """
        if not reason or not isinstance(reason, str) or len(reason.strip()) < 10:
            raise ValueError(
                f"rebase_corporate_action requires a descriptive reason (at least 10 non-whitespace characters), got {reason!r}"
            )
        _validate_symbol_for_path(symbol)
        if self.is_synthetic or not self.cache_dir or self.duckdb_cache is None:
            raise RuntimeError(
                f"Cannot rebase corporate action for {symbol}: DuckDB cache is not enabled or provider is synthetic."
            )

        provider_name = type(self.inner_provider).__name__
        if is_intraday_interval(interval):
            df = self.inner_provider.fetch_ohlcv(symbol, start, end, interval=interval)
        else:
            try:
                df = self.inner_provider.fetch_ohlcv(symbol, start, end, interval="1d")
            except Exception:
                df = self.inner_provider.fetch_ohlcv(symbol, start, end, interval=interval)

        if df is None or df.empty:
            raise ValueError(
                f"{provider_name} returned no data for {symbol} between {start} and {end}."
            )

        df = _drop_invalid_ohlcv_rows(df, symbol)
        if df.empty:
            raise ValueError(
                f"Data for {symbol} between {start} and {end} had rows, but every one was dropped as invalid OHLC."
            )

        declared_basis = _provider_native_basis(self.inner_provider)
        if declared_basis is None:
            declared_basis = BaseDataProvider.ASSUMED_NATIVE_BASIS_WHEN_UNKNOWN
            logger.warning(
                "rebase_corporate_action(%s) for %s: %s declares no adjustment basis, so the "
                "rebased bytes are recorded as '%s'. If the provider's series is not %s, correct "
                "the label with scripts/migrate_adjust_basis.py --set-basis ... --force.",
                symbol,
                provider_name,
                provider_name,
                declared_basis,
                declared_basis,
            )

        self.duckdb_cache.store_bars(
            provider_name,
            symbol,
            df,
            requested_start=start,
            requested_end=end,
            interval=interval,
            adjustflag=declared_basis,
            allow_history_rewrite=True,
            rewrite_reason=f"[CORPORATE_ACTION_REBASE] {reason.strip()}",
        )

        # Read back in the DECLARED basis, not in the default `qfq`: for a provider whose native
        # series is `raw`, a `qfq` read would need factors that this path deliberately does not
        # store (it never writes the `adjust_factors` table), so asking for qfq here would fail
        # closed on a write that actually succeeded. Reading the basis we just declared is also
        # what makes the returned frame equal to the bytes on disk.
        return self.duckdb_cache.query_bars(
            provider_name,
            symbol,
            start=start,
            end=end,
            interval=interval,
            adjust=declared_basis,
        )

    def fetch_metadata(self, symbol: str) -> dict:
        return self.inner_provider.fetch_metadata(symbol)

    def fetch_financial_indicators(
        self, symbol: str, as_of_date: Optional[str] = None, lookback_years: int = 5
    ) -> FundamentalMetrics:
        """Forward fundamental indicators query to inner provider (with DuckDB cache support)."""
        _validate_symbol_for_path(symbol)
        if self.duckdb_cache is not None and not self.is_synthetic:
            provider_name = type(self.inner_provider).__name__
            req_year = pd.to_datetime(as_of_date).year if as_of_date else None
            cached = self.duckdb_cache.query_fundamentals(provider_name, symbol, report_year=req_year)
            if cached is not None:
                return cached
            metrics = self.inner_provider.fetch_financial_indicators(symbol, as_of_date=as_of_date, lookback_years=lookback_years)
            if metrics is not None and not np.isnan(metrics.roe) and not np.isnan(metrics.net_profit):
                self.duckdb_cache.store_fundamentals(provider_name, metrics)
            return metrics
        return self.inner_provider.fetch_financial_indicators(symbol, as_of_date=as_of_date, lookback_years=lookback_years)


_PROVIDER_REGISTRY: Dict[str, Type[BaseDataProvider]] = {}


def register_provider(name: str, provider_cls: Type[BaseDataProvider]):
    """Registers a data provider class under a name."""
    _PROVIDER_REGISTRY[name.lower()] = provider_cls


register_provider("yfinance", YFinanceDataProvider)
register_provider("csv", CSVFolderDataProvider)
register_provider("synthetic", SyntheticDataProvider)

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

    if callable(target_cls):
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
    kwargs = dict(kwargs)
    if provider_name_or_instance is None and "provider" in kwargs:
        provider_name_or_instance = kwargs.pop("provider")
    else:
        kwargs.pop("provider", None)

    if isinstance(provider_name_or_instance, BaseDataProvider):
        return provider_name_or_instance

    if provider_name_or_instance is None:
        return YFinanceDataProvider(**kwargs)

    name = str(provider_name_or_instance).strip()
    _ensure_financial_api_registered()
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
               expected_last_session=None, **kwargs) -> pd.DataFrame:
    """Download (or load cached) OHLCV data for symbol between start and end.
    Maintains backward compatibility with original load_ohlcv function signature.
    Cached historical data in DuckDB never expires.

    `expected_last_session` (`L2.3`) is forwarded to `DuckDBCache.is_range_covered` when a
    `CachedDataProvider` is in play. LIVE callers MUST pass the session the cache has to
    hold (`common.cli_utils.resolve_live_clock_context(...).expected_last_session`);
    every backtest/research caller leaves it `None` (the bit-for-bit legacy path, design §11).
    """
    base_prov = get_data_provider(provider, **kwargs)
    cached = bool(use_cache and cache_dir)
    if cached:
        prov = CachedDataProvider(base_prov, cache_dir)
    else:
        prov = base_prov
    if cached:
        return prov.fetch_ohlcv(
            symbol, start, end, interval, expected_last_session=expected_last_session
        )
    if expected_last_session is not None and not is_synthetic_provider(base_prov):
        warnings.warn(
            f"load_ohlcv(expected_last_session={expected_last_session!r}) has no cache layer to "
            "apply it to (use_cache/cache_dir is off): every bar comes straight from the provider. "
            "The freshness gate (common/freshness.py) still runs at the call site."
        )
    return prov.fetch_ohlcv(symbol, start, end, interval)


def load_universe(symbols: list, start: str, end: str, interval: str = "1d", use_cache: bool = True,
                  cache_dir: str = None, provider: Union[str, BaseDataProvider, None] = None,
                  expected_last_session=None, columns: Optional[Union[str, Sequence[str]]] = "all",
                  **kwargs) -> dict:
    """Load OHLCV for each symbol; skips (with a warning) any that fail.
    Maintains backward compatibility with original load_universe function signature.
    Cached historical data in DuckDB never expires.

    `expected_last_session` (`L2.3`) selects the cache's strict freshness contract: with a
    session given, "covered" means the stored bars really reach that session (no 7-day
    tolerance, no "already synced today" short-circuit). It is `None` for every
    backtest/research caller and a real session for every live caller.

    `columns` defaults to `"all"` (required OHLCV + `Amount`) -- NOT to the cache's own
    `columns=None` default. Reason: this is the single loader every backtest goes through
    (`common/cli_utils.load_universe_with_banner` -> here), and a universe frame is consumed
    long after loading by code that cannot re-open the cache. `run_allocation_backtest`'s
    `execution_price="next_vwap"` mode needs `VWAP`, or `Amount` + `Volume`, on EVERY symbol
    (`common/allocation_backtester.py`, the `next_vwap` branch); with the narrower default that
    mode became an unconditional `ValueError` for every cache-backed run. Requesting the extra
    `Amount` here costs one nullable column and is all-NaN when the table has no money column,
    which the price paths ignore. Pass `columns=None` for the strict OHLCV-only shape.
    """
    base_prov = get_data_provider(provider, **kwargs)
    cached = bool(use_cache and cache_dir)
    if cached:
        prov = CachedDataProvider(base_prov, cache_dir)
    else:
        prov = base_prov
    if cached:
        return prov.fetch_universe(
            symbols, start, end, interval, expected_last_session=expected_last_session,
            columns=columns,
        )
    if expected_last_session is not None and not is_synthetic_provider(base_prov):
        warnings.warn(
            f"load_universe(expected_last_session={expected_last_session!r}) has no cache layer to "
            "apply it to (use_cache/cache_dir is off): every bar comes straight from the provider. "
            "The freshness gate (common/freshness.py) still runs at the call site."
        )
    # Raw providers have no column-projection layer (`BaseDataProvider.fetch_universe` accepts
    # `columns` only for parity) and several of them override `fetch_universe` with the old
    # 4-arg signature, so it is NOT forwarded on this branch.
    return prov.fetch_universe(symbols, start, end, interval)


def fetch_fund_metadata(symbol: str, provider: Union[str, BaseDataProvider, None] = None, **kwargs) -> dict:
    """Best-effort expense ratio / AUM lookup via metadata provider."""
    prov = get_data_provider(provider, **kwargs)
    return prov.fetch_metadata(symbol)


def _ensure_financial_api_registered() -> None:
    """Lazily registers financial_api, fuyao, baostock, and eastmoney data providers if available."""
    if "baostock" in _PROVIDER_REGISTRY and "eastmoney" in _PROVIDER_REGISTRY and "fuyao" in _PROVIDER_REGISTRY:
        return
    try:
        from .financial_api import (
            MarketDBDataProvider,
            FuyaoDataProvider,
            BaoStockDataProvider,
            EastMoneyDataProvider,
        )

        register_provider("marketdb", MarketDBDataProvider)
        register_provider("fuyao", FuyaoDataProvider)
        register_provider("financial_api", FuyaoDataProvider)
        register_provider("hithink", FuyaoDataProvider)
        register_provider("baostock", BaoStockDataProvider)
        register_provider("eastmoney", EastMoneyDataProvider)
    except Exception:
        pass


_ensure_financial_api_registered()



