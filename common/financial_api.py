"""Tonghuashun (HiThink) Financial-API and MarketDB data providers for the quant pipeline.

Provides BaseDataProvider implementations:
- MarketDBDataProvider ('marketdb'): Ultra-fast, 100% offline querying of local DuckDB
  (10M+ rows of 10-year daily K-lines for 5,551 A-shares with forward-adjusted v_daily_qfq).
- FuyaoDataProvider ('fuyao', 'financial_api', 'hithink'): Online REST API client via fuyao_client
  supporting equities, indices (.TI, standard indices), ETFs, valuation snapshots, and financial
  fundamentals, with optional smart fallback to local DuckDB.
"""

from __future__ import annotations

import os
import re
import sys
import warnings
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd

from .data import BaseDataProvider, _drop_invalid_ohlcv_rows, _validate_symbol_for_path

# Standard A-share / Fuyao ticker normalization regexes
_CODE_WITH_SUFFIX_RE = re.compile(r"^([0-9]{6})\.(SH|SZ|BJ|TI|OF|SS)$", re.IGNORECASE)
_INDEX_CODES = {"000001.SH", "000300.SH", "000905.SH", "000852.SH", "399001.SZ", "399006.SZ"}


def _resolve_financial_api_path() -> Optional[str]:
    """Locates the third-party Financial-API repository directory."""
    env_path = os.environ.get("FINANCIAL_API_PATH")
    if env_path and os.path.isdir(env_path):
        return os.path.abspath(env_path)

    # common/ -> quant/ -> repo_root
    common_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(common_dir)

    candidates = [
        os.path.abspath(os.path.join(repo_root, "../../third-party/Financial-API")),
        os.path.abspath(os.path.join(repo_root, "../third-party/Financial-API")),
        os.path.abspath(os.path.join(repo_root, "third-party/Financial-API")),
        "/home/stone/Work/third-party/Financial-API",
    ]
    for cand in candidates:
        if os.path.isdir(cand):
            return cand
    return None


def _load_env_file(env_path: str) -> None:
    """Parses simple KEY=VAL definitions from a .env file into os.environ if not already set."""
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ and v and v != "replace-me":
                    os.environ[k] = v
    except Exception:
        pass


def _bootstrap_financial_api() -> Optional[str]:
    """Adds the Financial-API python directories to sys.path and loads environment variables."""
    api_root = _resolve_financial_api_path()
    if not api_root:
        return None

    py_dir = os.path.join(api_root, "python")
    fuyao_scripts = os.path.join(py_dir, "toolkit", "fuyao", "scripts")

    for p in (py_dir, fuyao_scripts):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)

    # Load potential .env files
    _load_env_file(os.path.join(api_root, ".env"))
    _load_env_file(os.path.join(api_root, "python", ".env"))

    return api_root


def normalize_thscode(symbol: str) -> str:
    """Normalizes various A-share ticker formats to canonical thscode.

    Examples:
        '600519.SS' -> '600519.SH'
        '600519'    -> '600519.SH'
        '000001'    -> '000001.SZ'
        '886042.TI' -> '886042.TI'
        '510300'    -> '510300.SH'
    """
    sym = symbol.strip().upper()
    m = _CODE_WITH_SUFFIX_RE.match(sym)
    if m:
        ticker, suffix = m.group(1), m.group(2)
        if suffix == "SS":
            return f"{ticker}.SH"
        return f"{ticker}.{suffix}"

    # Bare 6-digit code inference
    if len(sym) == 6 and sym.isdigit():
        if sym.startswith(("600", "601", "603", "605", "688", "689")):
            return f"{sym}.SH"
        if sym.startswith(("000", "001", "002", "003", "300", "301")):
            return f"{sym}.SZ"
        if sym.startswith(("43", "83", "87", "92")):
            return f"{sym}.BJ"
        if sym.startswith(("51", "56", "58")):
            return f"{sym}.SH"  # ETF
        if sym.startswith(("15", "16")):
            return f"{sym}.SZ"  # ETF/LOF
        if sym.startswith("88"):
            return f"{sym}.TI"  # THS Index/Sector
        if sym.startswith("6") or sym.startswith("9"):
            return f"{sym}.SH"
        return f"{sym}.SZ"

    return sym


def _resolve_duckdb_path(folder_path: Optional[str] = None, db_path: Optional[str] = None) -> Optional[str]:
    """Resolves the path to the DuckDB market database."""
    if db_path:
        abs_p = os.path.abspath(db_path)
        return abs_p if os.path.isfile(abs_p) else None

    if folder_path:
        abs_fp = os.path.abspath(folder_path)
        if os.path.isfile(abs_fp):
            return abs_fp
        cand = os.path.join(abs_fp, "market.duckdb")
        if os.path.isfile(cand):
            return cand
        return None

    env_path = os.environ.get("MARKETDB_DB_PATH")
    if env_path and os.path.isfile(env_path):
        return os.path.abspath(env_path)

    api_root = _resolve_financial_api_path()
    if api_root:
        for sub in ("data/market.duckdb", "python/data/market.duckdb"):
            cand = os.path.join(api_root, sub)
            if os.path.isfile(cand) and os.path.getsize(cand) > 50000:
                return os.path.abspath(cand)

    common_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(common_dir)
    workspace_cand = os.path.join(repo_root, "data", "market.duckdb")
    if os.path.isfile(workspace_cand):
        return os.path.abspath(workspace_cand)

    return None


class MarketDBDataProvider(BaseDataProvider):
    """Market data provider reading from local DuckDB database (market.duckdb).

    Provides sub-millisecond, 100% offline access to 10 years of daily K-lines
    across 5,551 China A-share stocks with forward-adjusted prices (v_daily_qfq).
    """

    def __init__(
        self,
        folder_path: Optional[str] = None,
        db_path: Optional[str] = None,
        adjust: str = "forward",
        con: Optional[Any] = None,
    ):
        try:
            import duckdb
        except ImportError as exc:
            raise ImportError(
                "duckdb is required for MarketDBDataProvider. "
                "Run 'uv add duckdb' or 'pip install duckdb'."
            ) from exc

        self.adjust = adjust.lower()
        if self.adjust not in ("forward", "backward", "none"):
            raise ValueError(f"adjust must be 'forward', 'backward', or 'none'; got '{adjust}'")

        self.view = {
            "forward": "v_daily_qfq",
            "backward": "v_daily_hfq",
            "none": "v_daily",
        }[self.adjust]

        self._con = con
        if con is not None:
            self.db_path = ":memory:"
        else:
            resolved = _resolve_duckdb_path(folder_path=folder_path, db_path=db_path)
            if not resolved or not os.path.isfile(resolved):
                raise FileNotFoundError(
                    f"MarketDB DuckDB database file not found. Checked folder_path={folder_path}, "
                    f"db_path={db_path}, and default Financial-API paths."
                )
            self.db_path = resolved

    def _get_connection(self):
        if self._con is not None:
            return self._con
        import duckdb
        return duckdb.connect(self.db_path, read_only=True)

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        if interval != "1d":
            raise ValueError(f"MarketDBDataProvider only supports interval='1d', got '{interval}'")

        thscode = normalize_thscode(symbol)
        _validate_symbol_for_path(thscode)

        clauses = ["thscode = ?"]
        params: List[Any] = [thscode]

        if start:
            clauses.append("date >= ?")
            params.append(pd.to_datetime(start).strftime("%Y-%m-%d"))
        if end:
            clauses.append("date <= ?")
            params.append(pd.to_datetime(end).strftime("%Y-%m-%d"))

        sql = (
            f"SELECT date, open, high, low, close, volume FROM {self.view} "
            f"WHERE {' AND '.join(clauses)} ORDER BY date"
        )

        con = self._get_connection()
        try:
            df = con.execute(sql, params).df()
        finally:
            if self._con is None:
                con.close()

        if df.empty:
            raise ValueError(f"No data returned for {symbol} ({thscode}) between {start} and {end} in {self.view}")

        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        df = df.rename(
            columns={
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
            }
        )
        df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
        df = _drop_invalid_ohlcv_rows(df, symbol)

        if df.empty:
            raise ValueError(f"All data rows for {symbol} between {start} and {end} were invalid OHLC")

        return df

    def fetch_universe(
        self, symbols: List[str], start: str, end: str, interval: str = "1d"
    ) -> Dict[str, pd.DataFrame]:
        """High-performance batch loading via a single SQL query."""
        if interval != "1d":
            raise ValueError(f"MarketDBDataProvider only supports interval='1d', got '{interval}'")

        if not symbols:
            return {}

        norm_map = {s: normalize_thscode(s) for s in symbols}
        unique_thscodes = list(set(norm_map.values()))
        placeholders = ",".join(["?"] * len(unique_thscodes))

        clauses = [f"thscode IN ({placeholders})"]
        params: List[Any] = [*unique_thscodes]

        if start:
            clauses.append("date >= ?")
            params.append(pd.to_datetime(start).strftime("%Y-%m-%d"))
        if end:
            clauses.append("date <= ?")
            params.append(pd.to_datetime(end).strftime("%Y-%m-%d"))

        sql = (
            f"SELECT thscode, date, open, high, low, close, volume FROM {self.view} "
            f"WHERE {' AND '.join(clauses)} ORDER BY thscode, date"
        )

        con = self._get_connection()
        try:
            full_df = con.execute(sql, params).df()
        finally:
            if self._con is None:
                con.close()

        result: Dict[str, pd.DataFrame] = {}
        if full_df.empty:
            for s in symbols:
                warnings.warn(f"Skipping {s}: no data returned in batch query")
            return result

        full_df["date"] = pd.to_datetime(full_df["date"])
        grouped = full_df.groupby("thscode")

        for orig_sym, code in norm_map.items():
            if code not in grouped.groups:
                warnings.warn(f"Skipping {orig_sym} ({code}): symbol not present in database query result")
                continue
            group = grouped.get_group(code)
            df = group.set_index("date").sort_index()
            df = df.rename(
                columns={
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume",
                }
            )
            df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
            df = _drop_invalid_ohlcv_rows(df, orig_sym)
            if df.empty:
                warnings.warn(f"Skipping {orig_sym}: all rows dropped as invalid OHLC")
                continue
            result[orig_sym] = df

        return result

    def fetch_metadata(self, symbol: str) -> dict:
        result = {
            "expense_ratio": float("nan"),
            "total_assets": float("nan"),
            "roe": float("nan"),
            "dividend_yield": float("nan"),
            "earnings_growth": float("nan"),
            "debt_to_equity": float("nan"),
        }
        thscode = normalize_thscode(symbol)
        con = self._get_connection()
        try:
            tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
            if "dim_symbol" in tables:
                row = con.execute("SELECT asset_type FROM dim_symbol WHERE thscode = ?", [thscode]).fetchone()
                if row and row[0] in ("fund-etf", "fund-lof"):
                    pass
        except Exception:
            pass
        finally:
            if self._con is None:
                con.close()
        return result


class FuyaoDataProvider(BaseDataProvider):
    """Market data provider integrating the Fuyao REST API with smart local DuckDB caching/fallback.

    Supports:
    - China A-share equities (prices_historical)
    - China A-share indices (index_prices_historical: .TI, 000300.SH, etc.)
    - Exchange-traded funds (fund_market_historical: 510300.SH, 159919.SZ, etc.)
    - Fundamental and valuation metrics (a_share_valuations_snapshot, financials_indicators)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        adjust: str = "forward",
        prefer_local: bool = True,
        folder_path: Optional[str] = None,
        db_path: Optional[str] = None,
    ):
        _bootstrap_financial_api()
        self.adjust = adjust.lower()
        self.prefer_local = prefer_local
        self.api_key = api_key

        if self.api_key:
            os.environ["HITHINK_FINANCE_API_KEY"] = self.api_key

        self._local_provider: Optional[MarketDBDataProvider] = None
        if self.prefer_local:
            try:
                self._local_provider = MarketDBDataProvider(
                    folder_path=folder_path,
                    db_path=db_path,
                    adjust=adjust,
                )
            except Exception:
                self._local_provider = None

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        if interval != "1d":
            raise ValueError(f"FuyaoDataProvider only supports interval='1d', got '{interval}'")

        thscode = normalize_thscode(symbol)
        _validate_symbol_for_path(thscode)

        # 1. Try local DuckDB if prefer_local is enabled
        if self._local_provider is not None:
            try:
                df = self._local_provider.fetch_ohlcv(thscode, start, end, interval)
                if not df.empty:
                    return df
            except Exception:
                # Fallback to REST API
                pass

        # 2. Remote REST API
        start_dt = pd.to_datetime(start) if start else pd.to_datetime("2015-01-01")
        end_dt = pd.to_datetime(end) if end else pd.Timestamp.now()
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        items: List[dict] = []
        is_index = thscode.endswith(".TI") or thscode in _INDEX_CODES
        is_etf = thscode.startswith(("51", "56", "58", "15", "16"))

        try:
            import fuyao_client
        except ImportError as exc:
            raise ImportError(
                "fuyao_client could not be imported. Ensure Financial-API repository is present."
            ) from exc

        if is_index:
            items = fuyao_client.index_prices_historical(
                thscode=thscode,
                start_ms=start_ms,
                end_ms=end_ms,
                interval="1d",
            )
        elif is_etf:
            try:
                res = fuyao_client.fund_market_historical(
                    thscode=thscode,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    interval="1d",
                )
                items = res.get("item", []) if isinstance(res, dict) else res
            except Exception:
                # Some symbols starting with 15/16/51 may be equities or need general endpoint
                items = fuyao_client.prices_historical(
                    thscode=thscode,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    interval="1d",
                    adjust=self.adjust,
                )
        else:
            items = fuyao_client.prices_historical(
                thscode=thscode,
                start_ms=start_ms,
                end_ms=end_ms,
                interval="1d",
                adjust=self.adjust,
            )

        if not items:
            raise ValueError(f"No price data returned from Fuyao API for {symbol} ({thscode}) between {start} and {end}")

        df = pd.DataFrame(items)
        if "date_ms" not in df.columns:
            raise ValueError(f"Unexpected response structure from Fuyao API: missing 'date_ms'")

        df["date"] = pd.to_datetime(df["date_ms"], unit="ms").dt.normalize()
        df = df.set_index("date").sort_index()

        rename_map = {
            "open_price": "Open",
            "high_price": "High",
            "low_price": "Low",
            "close_price": "Close",
            "volume": "Volume",
        }
        for src_col, target_col in rename_map.items():
            if src_col in df.columns:
                df[target_col] = df[src_col].astype(float)
            elif target_col not in df.columns:
                raise ValueError(f"Missing required price column '{src_col}' in Fuyao API response")

        df = df[["Open", "High", "Low", "Close", "Volume"]]
        if start:
            df = df[df.index >= pd.to_datetime(start)]
        if end:
            df = df[df.index <= pd.to_datetime(end)]

        df = _drop_invalid_ohlcv_rows(df, symbol)
        if df.empty:
            raise ValueError(f"All data rows for {symbol} between {start} and {end} were dropped as invalid OHLC")

        return df

    def fetch_universe(
        self, symbols: List[str], start: str, end: str, interval: str = "1d"
    ) -> Dict[str, pd.DataFrame]:
        # If local DuckDB covers all symbols, use fast batch fetch
        if self._local_provider is not None:
            try:
                local_results = self._local_provider.fetch_universe(symbols, start, end, interval)
                missing = [s for s in symbols if s not in local_results]
                if not missing:
                    return local_results
                # If only partial, fetch remainder via individual fetch_ohlcv
                for s in missing:
                    try:
                        local_results[s] = self.fetch_ohlcv(s, start, end, interval)
                    except Exception as exc:
                        warnings.warn(f"Skipping {s}: {exc}")
                return local_results
            except Exception:
                pass

        return super().fetch_universe(symbols, start, end, interval)

    def fetch_metadata(self, symbol: str) -> dict:
        result = {
            "expense_ratio": float("nan"),
            "total_assets": float("nan"),
            "roe": float("nan"),
            "dividend_yield": float("nan"),
            "earnings_growth": float("nan"),
            "debt_to_equity": float("nan"),
        }
        thscode = normalize_thscode(symbol)

        try:
            import fuyao_client
        except ImportError:
            return result

        is_etf = thscode.startswith(("51", "56", "58", "15", "16"))
        if is_etf:
            try:
                profile = fuyao_client.fund_profile_detail(thscode=thscode, fund_type="exchange")
                item = (profile.get("item") or [{}])[0] if isinstance(profile, dict) else {}
                if "fund_scale" in item and item["fund_scale"] is not None:
                    result["total_assets"] = float(item["fund_scale"])
                rate_info = item.get("rate_info", [])
                for rate in rate_info:
                    if "管理费" in rate.get("rate_type", ""):
                        val = rate.get("standard_rate") or rate.get("discounted_rate")
                        if val is not None:
                            result["expense_ratio"] = float(str(val).rstrip("%")) / 100.0
                            break
            except Exception:
                pass
            return result

        # Equity fundamentals
        try:
            val_data = fuyao_client.a_share_valuations_snapshot(thscodes=[thscode])
            val_item = (val_data.get("item") or [{}])[0] if isinstance(val_data, dict) else {}
            current_year = pd.Timestamp.now().year
            for year in (current_year, current_year - 1, current_year - 2):
                try:
                    ind_data = fuyao_client.financials_indicators(thscode=thscode, report=f"{year}-4")
                    abilities = ind_data.get("abilities", []) if isinstance(ind_data, dict) else []
                    found_any = False
                    for ab in abilities:
                        for ind in ab.get("indicators", []):
                            idx_id = ind.get("index_id")
                            val = ind.get("value")
                            if val is None:
                                continue
                            fval = float(val)
                            if idx_id == "calculate_roe":
                                result["roe"] = fval
                                found_any = True
                            elif idx_id in ("net_profit_yoy_growth_ratio", "operating_income_yoy_growth_ratio"):
                                if np.isnan(result["earnings_growth"]):
                                    result["earnings_growth"] = fval
                                    found_any = True
                            elif idx_id == "asset_liability_ratio":
                                if fval < 100.0:
                                    ratio = fval / 100.0
                                    if ratio < 1.0:
                                        result["debt_to_equity"] = ratio / (1.0 - ratio)
                                        found_any = True
                    if found_any:
                        break
                except Exception:
                    continue
        except Exception:
            pass

        return result
