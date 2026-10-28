"""Tonghuashun (HiThink) Financial-API and MarketDB data providers for the quant pipeline.

Provides BaseDataProvider implementations:
- MarketDBDataProvider ('marketdb'): Ultra-fast, 100% offline querying of local DuckDB
  (10M+ rows of 10-year daily K-lines for 5,551 A-shares with forward-adjusted v_daily_qfq).
- FuyaoDataProvider ('fuyao', 'financial_api', 'hithink'): Online REST API client via fuyao_client
  supporting equities, indices (.TI, standard indices), ETFs, valuation snapshots, and financial
  fundamentals, with optional smart fallback to local DuckDB.
"""

from __future__ import annotations

import datetime
import os
import re
import sys
import threading
import time
import warnings
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import requests

from .data import BaseDataProvider, FundamentalMetrics, _drop_invalid_ohlcv_rows, _validate_symbol_for_path
from .duckdb_cache import DuckDBCache, is_intraday_interval

# Standard A-share / Fuyao ticker normalization regexes
_CODE_WITH_SUFFIX_RE = re.compile(r"^([0-9]{6})\.(SH|SZ|BJ|TI|OF|SS)$", re.IGNORECASE)
_INDEX_CODES = {
    "000001.SH", "000016.SH", "000300.SH", "000905.SH", "000852.SH", "000688.SH",
    "399001.SZ", "399005.SZ", "399006.SZ", "399300.SZ",
}
_BARE_SH_INDEX_CODES = {"000300", "000905", "000852", "000016", "000688"}


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
    ]
    for cand in candidates:
        if os.path.isdir(cand):
            return cand
    return None


def is_etf_code(thscode: str) -> bool:
    """True if thscode starts with known China A-share ETF/LOF prefixes."""
    return thscode.startswith(("51", "56", "58", "15", "16"))


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
        if sym in _BARE_SH_INDEX_CODES:
            return f"{sym}.SH"
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


def is_index_symbol(symbol: str) -> bool:
    """Returns True for known China A-share broad-market indices (e.g. 000001.SH, 000300.SH, 399001.SZ)."""
    norm_sym = normalize_thscode(symbol)
    if norm_sym in _INDEX_CODES:
        return True
    ticker, _, suffix = norm_sym.partition(".")
    if suffix == "SZ" and ticker.startswith("399"):
        return True
    if suffix == "SH" and (ticker.startswith("000") or ticker.startswith("001")):
        return True
    return False


def is_factor_neutral_symbol(symbol: str) -> bool:
    """Returns True for structurally factor-neutral symbols (indices, cash ETF 511880)."""
    if not symbol:
        return False
    s = str(symbol).strip().upper()
    if "511880" in s:
        return True
    if s.endswith(".TI"):
        return True
    return is_index_symbol(s)


def to_baostock_code(symbol: str) -> str:
    """Converts a symbol to BaoStock format (e.g. '600519.SH' -> 'sh.600519', '000001.SZ' -> 'sz.000001')."""
    norm = normalize_thscode(symbol)
    ticker, _, suffix = norm.partition(".")
    return f"{suffix.lower()}.{ticker}"


def to_eastmoney_secid(symbol: str) -> str:
    """Converts symbol to EastMoney secid format ('1.ticker' for SH, '0.ticker' for SZ and BJ)."""
    norm = normalize_thscode(symbol)
    ticker, _, suffix = norm.partition(".")
    if suffix == "SH" or ticker.startswith(("6", "5", "9")):
        return f"1.{ticker}"
    return f"0.{ticker}"


_TRADING_DAYS_CACHE: Optional[set[datetime.date]] = None


def get_trading_days_calendar() -> set[datetime.date]:
    """Retrieves and caches China A-share exchange trading days from fuyao_client."""
    global _TRADING_DAYS_CACHE
    if _TRADING_DAYS_CACHE is not None:
        return _TRADING_DAYS_CACHE

    try:
        import fuyao_client
        raw_items = fuyao_client.calendar_trading_days()
        days: set[datetime.date] = set()
        for item in raw_items:
            ms = item.get("date_ms")
            if ms:
                d = pd.to_datetime(ms, unit="ms", utc=True).tz_convert("Asia/Shanghai").date()
                days.add(d)
        if days:
            _TRADING_DAYS_CACHE = days
            return _TRADING_DAYS_CACHE
    except Exception:
        pass

    return set()


def check_missing_trading_days(
    latest_retrieved_date: datetime.date,
    target_end_date: datetime.date,
) -> list[datetime.date]:
    """Returns a list of missing trading days in the open-closed interval (latest_retrieved_date, target_end_date].

    If all days in the interval are non-trading days (weekends or official holidays), returns an empty list.
    """
    if target_end_date <= latest_retrieved_date:
        return []

    # Candidate calendar dates in (latest_retrieved_date, target_end_date]
    delta = (target_end_date - latest_retrieved_date).days
    candidates = [latest_retrieved_date + datetime.timedelta(days=i) for i in range(1, delta + 1)]

    # Fast path: if all candidate dates are Saturday (5) or Sunday (6), no trading days are missed
    if all(d.weekday() in (5, 6) for d in candidates):
        return []

    calendar = get_trading_days_calendar()
    # If official calendar is available, use it when candidates fall within its coverage window
    if calendar:
        min_cal = min(calendar)
        max_cal = max(calendar)
        result = []
        for d in candidates:
            if min_cal <= d <= max_cal:
                if d in calendar:
                    result.append(d)
            else:
                if d.weekday() not in (5, 6):
                    result.append(d)
        return result

    # Fallback when official calendar is unavailable:
    # consider non-weekend days (Monday..Friday) as candidate trading days
    return [d for d in candidates if d.weekday() not in (5, 6)]


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


def calibrate_adjust_factors_from_closes(
    raw_close: Union[pd.Series, pd.DataFrame],
    qfq_close: Union[pd.Series, pd.DataFrame],
    hfq_close: Optional[Union[pd.Series, pd.DataFrame]] = None,
) -> pd.DataFrame:
    """UTILITY-ONLY helper: no production call site (see `fetch_adjust_factors` for the real sources).

    Calibrates daily qfq and hfq adjustment factors from raw close and adjusted close series.

    The factors every production path returns come from the `fetch_adjust_factors` implementations
    (dense `calc_adjust_factor_daily` / BaoStock event rows / MarketDB's raw-vs-hfq construction).
    Kept because its math is independently unit-tested and it stays the documented way to turn a
    pair of GENUINE raw + adjusted close series into factors. Do not wire it into a data path that
    has no genuine qfq close series: handing it the hfq close as both `qfq_close` and `hfq_close`
    silently mislabels `factor_qfq` (exactly what MarketDB's old fallback did).

    Computes:
        factor_qfq = qfq_close / raw_close
        factor_hfq = hfq_close / raw_close (or derived from factor_qfq normalized by inception)

    Returns DataFrame indexed by date with columns ['factor_qfq', 'factor_hfq'].
    """
    if isinstance(raw_close, pd.DataFrame):
        rc = raw_close.iloc[:, 0]
    else:
        rc = raw_close
    if isinstance(qfq_close, pd.DataFrame):
        qc = qfq_close.iloc[:, 0]
    else:
        qc = qfq_close

    common_idx = rc.index.intersection(qc.index)
    if len(common_idx) == 0:
        return pd.DataFrame(columns=["factor_qfq", "factor_hfq"], index=pd.DatetimeIndex([], name="date"))

    rc_aligned = rc.loc[common_idx].astype(float)
    qc_aligned = qc.loc[common_idx].astype(float)

    # Avoid div by zero
    factor_qfq = np.where(rc_aligned != 0, qc_aligned / rc_aligned, 1.0)
    factor_qfq_s = pd.Series(factor_qfq, index=common_idx, name="factor_qfq")

    if hfq_close is not None:
        if isinstance(hfq_close, pd.DataFrame):
            hc = hfq_close.iloc[:, 0]
        else:
            hc = hfq_close
        hc_aligned = hc.loc[common_idx].astype(float)
        factor_hfq = np.where(rc_aligned != 0, hc_aligned / rc_aligned, 1.0)
        factor_hfq_s = pd.Series(factor_hfq, index=common_idx, name="factor_hfq")
    else:
        first_qfq = factor_qfq_s.iloc[0] if len(factor_qfq_s) > 0 and factor_qfq_s.iloc[0] != 0 else 1.0
        factor_hfq_s = factor_qfq_s / first_qfq

    df_res = pd.DataFrame({
        "factor_qfq": factor_qfq_s,
        "factor_hfq": factor_hfq_s,
    }, index=pd.to_datetime(common_idx))
    df_res.index.name = "date"
    return df_res


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

    def _fetch_ohlcv_internal(
        self, symbol: str, start: str, end: str, interval: str = "1d", view: Optional[str] = None
    ) -> pd.DataFrame:
        if interval != "1d":
            raise ValueError(f"MarketDBDataProvider only supports interval='1d', got '{interval}'")

        target_view = view or self.view
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
            f"SELECT date, open, high, low, close, volume FROM {target_view} "
            f"WHERE {' AND '.join(clauses)} ORDER BY date"
        )

        con = self._get_connection()
        try:
            df = con.execute(sql, params).df()
        finally:
            if self._con is None:
                con.close()

        if df.empty:
            if is_index_symbol(thscode):
                try:
                    bs_p = BaoStockDataProvider()
                    if target_view == "v_daily" and hasattr(bs_p, "fetch_raw_ohlcv"):
                        fallback_df = bs_p.fetch_raw_ohlcv(thscode, start, end, interval)
                    else:
                        fallback_df = bs_p.fetch_ohlcv(thscode, start, end, interval)
                    if not fallback_df.empty:
                        return fallback_df
                except Exception:
                    pass
                try:
                    em_p = EastMoneyDataProvider()
                    if target_view == "v_daily" and hasattr(em_p, "fetch_raw_ohlcv"):
                        fallback_df = em_p.fetch_raw_ohlcv(thscode, start, end, interval)
                    else:
                        fallback_df = em_p.fetch_ohlcv(thscode, start, end, interval)
                    if not fallback_df.empty:
                        return fallback_df
                except Exception:
                    pass
            raise ValueError(f"No data returned for {symbol} ({thscode}) between {start} and {end} in {target_view}")

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

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        return self._fetch_ohlcv_internal(symbol, start, end, interval=interval, view=self.view)

    def fetch_raw_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        """Fetch raw unadjusted OHLCV bars without mutating instance basis."""
        return self._fetch_ohlcv_internal(symbol, start, end, interval=interval, view="v_daily")

    def fetch_universe(
        self, symbols: List[str], start: str, end: str, interval: str = "1d"
    ) -> Dict[str, pd.DataFrame]:
        """High-performance batch loading via a single SQL query."""
        if interval != "1d":
            raise ValueError(f"MarketDBDataProvider only supports interval='1d', got '{interval}'")

        if not symbols:
            return {}

        norm_map = {s: normalize_thscode(s) for s in symbols}
        unique_thscodes = list(dict.fromkeys(norm_map.values()))
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

    def fetch_adjust_factors(
        self,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetch daily adjustment factors for a symbol from MarketDB.

        Reads from calc_adjust_factor_daily if present, or derives from v_daily / v_daily_qfq.
        For index symbols or cash ETFs (e.g. 511880.SH), returns factors = 1.0.

        When `calc_adjust_factor_daily` is missing (or has no rows for the window) the last-resort
        fallback reads raw `v_daily` and backward-adjusted `v_daily_hfq` closes and constructs the
        factors itself: ``factor_hfq = hfq_close / raw_close``, and ``factor_qfq = factor_hfq /
        factor_hfq[latest]`` (BaoStock's official `fore = back / back_last`). That fallback does
        NOT use `calibrate_adjust_factors_from_closes`.
        """
        thscode = normalize_thscode(symbol)
        _validate_symbol_for_path(thscode)

        # Index symbols and cash ETFs (511880) have no corporate actions -> factor 1.0
        if is_index_symbol(thscode) or "511880" in thscode:
            con = self._get_connection()
            try:
                clauses = ["thscode = ?"]
                params: List[Any] = [thscode]
                if start:
                    clauses.append("date >= ?")
                    params.append(pd.to_datetime(start).strftime("%Y-%m-%d"))
                if end:
                    clauses.append("date <= ?")
                    params.append(pd.to_datetime(end).strftime("%Y-%m-%d"))
                date_rows = con.execute(
                    f"SELECT DISTINCT date FROM {self.view} WHERE {' AND '.join(clauses)} ORDER BY date",
                    params,
                ).fetchall()
            except Exception:
                date_rows = []
            finally:
                if self._con is None:
                    con.close()

            if date_rows:
                dates = pd.to_datetime([r[0] for r in date_rows])
            elif start and end:
                dates = pd.bdate_range(start, end)
            else:
                dates = pd.DatetimeIndex([])

            return pd.DataFrame({
                "factor_qfq": np.ones(len(dates), dtype=float),
                "factor_hfq": np.ones(len(dates), dtype=float),
            }, index=pd.DatetimeIndex(dates, name="date"))

        con = self._get_connection()
        try:
            tables = {r[0].lower() for r in con.execute("SHOW TABLES").fetchall()}
            if "calc_adjust_factor_daily" in tables:
                clauses = ["thscode = ?"]
                params = [thscode]
                if start:
                    clauses.append("date >= ?")
                    params.append(pd.to_datetime(start).strftime("%Y-%m-%d"))
                if end:
                    clauses.append("date <= ?")
                    params.append(pd.to_datetime(end).strftime("%Y-%m-%d"))
                cols_info = {r[1].lower() for r in con.execute("PRAGMA table_info('calc_adjust_factor_daily')").fetchall()}
                qfq_col = "forward_factor" if "forward_factor" in cols_info else "factor_qfq"
                hfq_col = "backward_factor" if "backward_factor" in cols_info else "factor_hfq"
                sql = (
                    f"SELECT date, {qfq_col} AS factor_qfq, {hfq_col} AS factor_hfq "
                    f"FROM calc_adjust_factor_daily WHERE {' AND '.join(clauses)} ORDER BY date"
                )
                res_df = con.execute(sql, params).df()
                if not res_df.empty:
                    res_df["date"] = pd.to_datetime(res_df["date"])
                    res_df = res_df.set_index("date").sort_index()
                    res_df.index.name = "date"
                    return res_df[["factor_qfq", "factor_hfq"]].astype(float)
        except Exception:
            pass
        finally:
            if self._con is None:
                con.close()

        # Fallback: derive the factors directly from the raw vs backward-adjusted closes of
        # v_daily / v_daily_hfq. This path deliberately does NOT go through
        # `calibrate_adjust_factors_from_closes`: there is no qfq view to read here, so the helper
        # would have to be handed the hfq close as `qfq_close`, and its `factor_qfq` column would
        # come out equal to the hfq factor, i.e. mislabelled. Per BaoStock's official definition
        # `fore = back / back[latest]` -- and `_apply_factors_to_df` uses exactly that identity,
        # `qfq_price = raw * factor_hfq[t] / factor_hfq[latest]` -- the honest qfq factor is simply
        # the hfq factor rescaled by its own last value.
        try:
            raw_prov = MarketDBDataProvider(adjust="none", con=self._con, folder_path=getattr(self, "folder_path", None), db_path=getattr(self, "db_path", None) if getattr(self, "db_path", None) != ":memory:" else None)
            hfq_prov = MarketDBDataProvider(adjust="backward", con=self._con, folder_path=getattr(self, "folder_path", None), db_path=getattr(self, "db_path", None) if getattr(self, "db_path", None) != ":memory:" else None)
            raw_df = raw_prov.fetch_ohlcv(symbol, start=start or "2016-01-01", end=end or "2030-01-01", interval="1d")
            hfq_df = hfq_prov.fetch_ohlcv(symbol, start=start or "2016-01-01", end=end or "2030-01-01", interval="1d")

            raw_close = raw_df["Close"]
            hfq_close = hfq_df["Close"]
            common_idx = raw_close.index.intersection(hfq_close.index)
            rc = raw_close.loc[common_idx].astype(float)
            hc = hfq_close.loc[common_idx].astype(float)
            # Same div-by-zero guard the retired calibration call applied: a zero raw close yields
            # factor 1.0 instead of inf (numpy still evaluates both branches, so a non-finite raw
            # close propagates as-is -- exactly the pre-existing behaviour, not a new policy).
            factor_hfq = pd.Series(
                np.where(rc.to_numpy() != 0, hc.to_numpy() / rc.to_numpy(), 1.0),
                index=common_idx,
                name="factor_hfq",
            )
            fac = pd.DataFrame({"factor_hfq": factor_hfq}, index=pd.to_datetime(common_idx))
            fac.index.name = "date"
            if fac.empty:
                return pd.DataFrame(
                    columns=["factor_qfq", "factor_hfq"], index=pd.DatetimeIndex([], name="date")
                )

            last_hfq = float(fac["factor_hfq"].iloc[-1])
            if np.isfinite(last_hfq) and last_hfq > 0:
                fac["factor_qfq"] = fac["factor_hfq"] / last_hfq
            else:
                warnings.warn(
                    f"MarketDB adjust-factor fallback for {symbol!r} produced a non-positive "
                    f"terminal backward factor ({last_hfq!r}); labelling factor_qfq as 1.0. "
                    f"factor_hfq is unaffected."
                )
                fac["factor_qfq"] = 1.0
            return fac[["factor_qfq", "factor_hfq"]]
        except Exception:
            return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))

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


#: Providers already told "I have no local adjustment-factor table" (M3.8).
#: Once per provider rather than per symbol -- the same precedent as
#: `common/data.py::_FACTOR_CAPABILITY_GAP_WARNED`, which exists because a 22-symbol universe
#: otherwise emits 22 copies of a single operational fact.
_NO_LOCAL_FACTOR_TABLE_WARNED: set = set()


def _warn_no_local_factor_table(symbol: str) -> None:
    """Say once that `FuyaoDataProvider` has nowhere to read adjustment factors from."""
    if "FuyaoDataProvider" in _NO_LOCAL_FACTOR_TABLE_WARNED:
        return
    _NO_LOCAL_FACTOR_TABLE_WARNED.add("FuyaoDataProvider")
    warnings.warn(
        f"FuyaoDataProvider has no local adjustment-factor table reachable (first seen for "
        f"{symbol!r}): its only factor source is MarketDB's calc_adjust_factor_daily via "
        f"`self._local_provider`, which is absent here (prefer_local=False, or no market.duckdb). "
        f"`adjust_factor_source_available` is therefore False and the cache layer takes its legacy "
        f"direct path -- the provider's own fetch_ohlcv, its native basis recorded, no raw bars, no "
        f"factors, no read-time derivation, plus its own once-per-provider capability-gap warning. "
        f"Calling this method directly returns an empty frame. Run with prefer_local=True (the "
        f"default) or point db_path/MARKETDB_DB_PATH at a real market.duckdb to get factor-based "
        f"derivation. No factor is invented from Fuyao's own raw x qfq closes: that inference is "
        f"only defined up to a multiplicative constant."
    )


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

        if self.api_key and os.environ.get("HITHINK_FINANCE_API_KEY") != self.api_key:
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

        # M3.8 capability flag (`BaseDataProvider.adjust_factor_source_available`): Fuyao's ONLY
        # adjustment-factor source is the local MarketDB factor table, so this is decided by whether
        # that DB actually opened -- it is DYNAMIC, not a static class property. `derive_mode="auto"`
        # in `CachedDataProvider` reads it: True keeps the raw+derive+fail-closed path, False takes
        # the legacy direct path (native basis recorded, nothing derived, one warning) instead of
        # turning every cache miss into `MissingAdjustFactorsError`.
        self.adjust_factor_source_available = (
            self._local_provider is not None
            and hasattr(self._local_provider, "fetch_adjust_factors")
        )

    def _fetch_remote_ohlcv(
        self, symbol: str, start: str, end: str, interval: str = "1d", adjust: Optional[str] = None
    ) -> pd.DataFrame:
        """Fetches OHLCV directly from the Fuyao REST API via fuyao_client."""
        if interval != "1d":
            raise ValueError(f"FuyaoDataProvider only supports interval='1d', got '{interval}'")

        eff_adjust = adjust or self.adjust

        thscode = normalize_thscode(symbol)
        _validate_symbol_for_path(thscode)

        start_dt = pd.to_datetime(start) if start else pd.to_datetime("2015-01-01")
        end_dt = pd.to_datetime(end) if end else pd.Timestamp.now()
        start_dt_cst = start_dt.tz_localize("Asia/Shanghai") if start_dt.tzinfo is None else start_dt.tz_convert("Asia/Shanghai")
        end_dt_cst = end_dt.tz_localize("Asia/Shanghai") if end_dt.tzinfo is None else end_dt.tz_convert("Asia/Shanghai")
        start_ms = int(start_dt_cst.timestamp() * 1000)
        end_ms = int((end_dt_cst + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)).timestamp() * 1000)

        items: List[dict] = []
        is_index = is_index_symbol(thscode) or thscode.endswith(".TI")
        is_etf = is_etf_code(thscode)

        rename_map = {
            "open_price": "Open",
            "high_price": "High",
            "low_price": "Low",
            "close_price": "Close",
            "volume": "Volume",
        }

        try:
            import fuyao_client
        except ImportError as exc:
            if is_index:
                fuyao_client = None
            else:
                raise ImportError(
                    "fuyao_client could not be imported. Ensure Financial-API repository is present."
                ) from exc

        if is_index:
            # Fuyao API index historical prices (/api/a-share-index/prices/historical) only retain
            # ~5 years of rolling history. Passing a start timestamp older than ~5 years causes
            # the upstream API to return empty items ([]). Clamp start_ms to 5 years ago if needed.
            now_ms = int(pd.Timestamp.now(tz="Asia/Shanghai").timestamp() * 1000)
            five_years_ms = int(5 * 365 * 86400 * 1000)
            min_index_start_ms = now_ms - five_years_ms
            effective_start_ms = start_ms
            if effective_start_ms < min_index_start_ms:
                clamped_dt = pd.to_datetime(min_index_start_ms, unit="ms", utc=True).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d")
                req_start_str = start if start else start_dt.strftime("%Y-%m-%d")
                warnings.warn(
                    f"Fuyao index prices ({thscode}) only retain ~5 years of rolling history. "
                    f"Clamping requested start '{req_start_str}' to '{clamped_dt}'."
                )
                effective_start_ms = min_index_start_ms

            df = pd.DataFrame()
            if fuyao_client is not None and effective_start_ms <= end_ms:
                try:
                    items = fuyao_client.index_prices_historical(
                        thscode=thscode,
                        start_ms=effective_start_ms,
                        end_ms=end_ms,
                        interval="1d",
                    )
                    if items:
                        raw_df = pd.DataFrame(items)
                        if "date_ms" in raw_df.columns:
                            raw_df["date"] = pd.to_datetime(raw_df["date_ms"], unit="ms", utc=True).dt.tz_convert("Asia/Shanghai").dt.normalize().dt.tz_localize(None)
                            raw_df = raw_df.set_index("date").sort_index()
                            for src_col, target_col in rename_map.items():
                                if src_col in raw_df.columns:
                                    raw_df[target_col] = raw_df[src_col].astype(float)
                            cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in raw_df.columns]
                            if len(cols) == 5:
                                df = raw_df[cols]
                except Exception:
                    df = pd.DataFrame()

            # If Fuyao returned empty or failed, fall back to BaoStock and then EastMoney
            if df.empty:
                try:
                    df = BaoStockDataProvider().fetch_ohlcv(thscode, start, end, interval)
                except Exception:
                    df = pd.DataFrame()
                if df.empty:
                    try:
                        df = EastMoneyDataProvider().fetch_ohlcv(thscode, start, end, interval)
                    except Exception:
                        df = pd.DataFrame()

            # Pre-5y historical stitching from BaoStock if requested start is earlier than df.index.min()
            if not df.empty and start:
                req_start_date = pd.to_datetime(start).date()
                if req_start_date < df.index.min().date():
                    try:
                        pre_df = BaoStockDataProvider().fetch_ohlcv(
                            thscode,
                            req_start_date.strftime("%Y-%m-%d"),
                            df.index.min().strftime("%Y-%m-%d"),
                            interval=interval,
                        )
                        if not pre_df.empty:
                            df = pd.concat([pre_df[pre_df.index < df.index.min()], df])
                    except Exception:
                        pass

            # Trailing recent bars stitching from EastMoney / BaoStock if df lags behind end
            if not df.empty and end:
                req_end_date = pd.to_datetime(end).date()
                if req_end_date > df.index.max().date():
                    try:
                        post_df = EastMoneyDataProvider().fetch_ohlcv(
                            thscode,
                            df.index.max().strftime("%Y-%m-%d"),
                            req_end_date.strftime("%Y-%m-%d"),
                            interval=interval,
                        )
                        if post_df.empty:
                            post_df = BaoStockDataProvider().fetch_ohlcv(
                                thscode,
                                df.index.max().strftime("%Y-%m-%d"),
                                req_end_date.strftime("%Y-%m-%d"),
                                interval=interval,
                            )
                        if not post_df.empty:
                            df = pd.concat([df, post_df[post_df.index > df.index.max()]])
                    except Exception:
                        pass

            if not df.empty:
                df = df[~df.index.duplicated(keep="last")].sort_index()
                if start:
                    df = df[df.index >= pd.to_datetime(start)]
                if end:
                    df = df[df.index <= pd.to_datetime(end)]
                df = _drop_invalid_ohlcv_rows(df, symbol)
                if not df.empty:
                    return df

            raise ValueError(
                f"No price data returned from Fuyao API / fallbacks for {symbol} ({thscode}) between {start} and {end} "
                "(Fuyao API index history is limited to the last ~5 years)"
            )
        elif is_etf:
            # Fuyao API fund historical prices (/api/fund/market/historical) only retain ~5 years
            # of rolling history, and requires that end_ms <= _five_year_limit_ms(start_ms).
            # Passing a start timestamp older than ~5 years causes the upstream API to return empty
            # items ([]) or raises ValueError('fund history window must not exceed five years').
            # Clamp start_ms to ~5 years ago and bound the window so fund_market_historical succeeds.
            now_ms = int(pd.Timestamp.now(tz="Asia/Shanghai").timestamp() * 1000)
            five_years_ms = int(5 * 365 * 86400 * 1000)
            min_fund_start_ms = now_ms - five_years_ms
            effective_start_ms = start_ms
            if effective_start_ms < min_fund_start_ms:
                clamped_dt = pd.to_datetime(min_fund_start_ms, unit="ms", utc=True).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d")
                req_start_str = start if start else start_dt.strftime("%Y-%m-%d")
                warnings.warn(
                    f"Fuyao fund/ETF prices ({thscode}) only retain ~5 years of rolling history. "
                    f"Clamping requested start '{req_start_str}' to '{clamped_dt}'."
                )
                effective_start_ms = min_fund_start_ms

            limit_fn = getattr(fuyao_client, "_five_year_limit_ms", None)
            max_end_ms = effective_start_ms + five_years_ms
            if callable(limit_fn):
                try:
                    res_limit = limit_fn(effective_start_ms)
                    if isinstance(res_limit, (int, float)):
                        max_end_ms = int(res_limit)
                except Exception:
                    pass
            effective_end_ms = min(end_ms, max_end_ms)

            items = []
            if effective_start_ms <= effective_end_ms:
                try:
                    res = fuyao_client.fund_market_historical(
                        thscode=thscode,
                        start_ms=effective_start_ms,
                        end_ms=effective_end_ms,
                        interval="1d",
                    )
                    items = res.get("item", []) if isinstance(res, dict) else res
                except Exception as fund_exc:
                    # If fund_market_historical fails (e.g. symbol is actually an equity with 15/16/51 prefix),
                    # fall back to general prices_historical
                    try:
                        items = fuyao_client.prices_historical(
                            thscode=thscode,
                            start_ms=start_ms,
                            end_ms=end_ms,
                            interval="1d",
                            adjust=eff_adjust,
                        )
                    except Exception:
                        raise fund_exc
        else:
            items = fuyao_client.prices_historical(
                thscode=thscode,
                start_ms=start_ms,
                end_ms=end_ms,
                interval="1d",
                adjust=eff_adjust,
            )

        if not items:
            extra = " (Fuyao API fund/ETF history is limited to the last ~5 years)" if is_etf else ""
            raise ValueError(f"No price data returned from Fuyao API for {symbol} ({thscode}) between {start} and {end}{extra}")

        df = pd.DataFrame(items)
        if "date_ms" not in df.columns:
            raise ValueError(f"Unexpected response structure from Fuyao API: missing 'date_ms'")

        df["date"] = pd.to_datetime(df["date_ms"], unit="ms", utc=True).dt.tz_convert("Asia/Shanghai").dt.normalize().dt.tz_localize(None)
        df = df.set_index("date").sort_index()

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

    def _fetch_ohlcv_internal(
        self, symbol: str, start: str, end: str, interval: str = "1d", adjust: Optional[str] = None
    ) -> pd.DataFrame:
        if interval != "1d":
            raise ValueError(f"FuyaoDataProvider only supports interval='1d', got '{interval}'")

        eff_adjust = adjust or self.adjust
        thscode = normalize_thscode(symbol)
        _validate_symbol_for_path(thscode)

        now_cst = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)
        target_end_dt = pd.to_datetime(end).normalize() if end else now_cst
        effective_target_end = min(target_end_dt, now_cst) if target_end_dt > now_cst else target_end_dt

        # 1. Try local DuckDB if prefer_local is enabled
        local_df: Optional[pd.DataFrame] = None
        if self._local_provider is not None:
            try:
                if eff_adjust == "none" and hasattr(self._local_provider, "fetch_raw_ohlcv"):
                    df = self._local_provider.fetch_raw_ohlcv(thscode, start, end, interval)
                else:
                    df = self._local_provider.fetch_ohlcv(thscode, start, end, interval)
                if not df.empty:
                    local_df = df
            except Exception:
                local_df = None

        if local_df is None:
            final_df = self._fetch_remote_ohlcv(symbol, start, end, interval, adjust=eff_adjust)
        else:
            is_index = is_index_symbol(thscode) or thscode.endswith(".TI")
            if is_index and start:
                req_start_dt = pd.to_datetime(start).normalize()
                if local_df.index[0] > req_start_dt:
                    try:
                        bs_p = BaoStockDataProvider()
                        if eff_adjust == "none" and hasattr(bs_p, "fetch_raw_ohlcv"):
                            pre_df = bs_p.fetch_raw_ohlcv(
                                thscode,
                                req_start_dt.strftime("%Y-%m-%d"),
                                local_df.index[0].strftime("%Y-%m-%d"),
                                interval=interval,
                            )
                        else:
                            pre_df = bs_p.fetch_ohlcv(
                                thscode,
                                req_start_dt.strftime("%Y-%m-%d"),
                                local_df.index[0].strftime("%Y-%m-%d"),
                                interval=interval,
                            )
                        if not pre_df.empty:
                            local_df = pd.concat([pre_df[pre_df.index < local_df.index[0]], local_df])
                    except Exception:
                        pass

            local_max_date = local_df.index[-1]
            missing_days = check_missing_trading_days(local_max_date.date(), effective_target_end.date())
            if local_max_date >= effective_target_end or not missing_days:
                final_df = local_df
            else:
                # Local data ends before effective target end AND genuine trading days are missing; fetch incremental
                try:
                    inc_start_str = local_max_date.strftime("%Y-%m-%d")
                    remote_df = self._fetch_remote_ohlcv(symbol, inc_start_str, end, interval, adjust=eff_adjust)
                    if not remote_df.empty:
                        # Re-scale local data if a corporate action changed the forward-adjustment factor
                        if local_max_date in remote_df.index:
                            loc_c = local_df.loc[local_max_date, "Close"]
                            rem_c = remote_df.loc[local_max_date, "Close"]
                            if loc_c > 0 and abs(rem_c - loc_c) / loc_c > 1e-4:
                                scale = rem_c / loc_c
                                local_df[["Open", "High", "Low", "Close"]] *= scale
                        final_df = pd.concat([local_df, remote_df])
                        final_df = final_df[~final_df.index.duplicated(keep="last")].sort_index()
                    else:
                        final_df = local_df
                except Exception as exc:
                    if "No price data returned" not in str(exc):
                        warnings.warn(
                            f"Failed to fetch latest data for {symbol} from Fuyao API ({exc}); "
                            f"falling back to local MarketDB data up to {local_max_date.strftime('%Y-%m-%d')}."
                        )
                    final_df = local_df

        # Calendar-aware warning if query date is newer than retrieved data
        if not final_df.empty:
            retrieved_latest_date = final_df.index[-1].date()
            query_target_date = target_end_dt.date()
            if query_target_date > retrieved_latest_date:
                missed = check_missing_trading_days(retrieved_latest_date, query_target_date)
                if missed:
                    missed_str = [d.strftime("%Y-%m-%d") for d in missed]
                    warnings.warn(
                        f"Query date '{query_target_date.strftime('%Y-%m-%d')}' is newer than "
                        f"retrieved data range latest date '{retrieved_latest_date.strftime('%Y-%m-%d')}' "
                        f"for {symbol} ({thscode}) (missing trading days: {missed_str})."
                    )

        return final_df

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        return self._fetch_ohlcv_internal(symbol, start, end, interval=interval, adjust=self.adjust)

    def fetch_raw_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        """Fetch raw unadjusted OHLCV bars without mutating instance basis."""
        return self._fetch_ohlcv_internal(symbol, start, end, interval=interval, adjust="none")

    def fetch_adjust_factors(
        self,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """Adjustment factors for `symbol`, delegated to the local MarketDB (M3.8).

        Fuyao's REST surface has no factor endpoint, so there is nothing to compute here: the only
        factor source this provider can reach is the `MarketDBDataProvider` that `__init__` builds
        as `self._local_provider` (`prefer_local=True`, the default). That provider's
        `fetch_adjust_factors` reads the dense `calc_adjust_factor_daily` table, and the returned
        frame is passed through VERBATIM:

          * columns `["factor_qfq", "factor_hfq"]` (dense, one row per trading day) -- kept as they
            are, not compressed into BaoStock-style event rows and not trimmed to one column.
            `DuckDBCache.store_adjust_factors` / `_apply_factors_to_df` resolve either granularity
            with an as-of lookup, and `_apply_factors_to_df` consumes `factor_hfq` only
            (`qfq_price = raw * factor_hfq[t] / factor_hfq[latest]`, i.e. exactly BaoStock's
            `fore = back / back[latest]` identity), so the dense shape is the lossless one.
          * index is a `DatetimeIndex` named `date`, ascending.
          * `start`/`end` are forwarded unchanged: the read is local and cheap, and the caller's
            window is respected. Note the qfq ANCHOR, however, is `DuckDBCache.get_latest_factor_hfq`
            -- the latest row in the WHOLE `adjust_factors` table for the symbol, not the latest row
            inside this window. A corporate action outside the delegated window therefore still
            rescales a qfq read of it, by a constant (returns untouched). That single-anchor rule is
            deliberate (`docs/version.md` §22.5 F5), not a delegation bug.

        Factor-neutral symbols (indices, cash ETF 511880) return the same empty frame BaoStock's
        implementation returns: `factor_hfq` column, empty `DatetimeIndex` named `date`.

        DEGRADED MODE (no local factor table: `prefer_local=False`, or MarketDB unavailable): this
        instance reports `adjust_factor_source_available = False` (set in `__init__`), so
        `CachedDataProvider.get_bars` never calls this method at all -- it takes the documented
        legacy direct path instead: the provider's own `fetch_ohlcv` (native basis), stored with
        that basis, no raw bars, no factors, no read-time derivation, and one
        `_warn_factor_capability_gap` warning per provider. That is a CAPABILITY GAP, not a data
        failure, so it must not fail closed. A direct caller that ignores the flag still gets an
        empty frame here, which must be read as "no factor source", never as "factor-neutral".
        Inventing factors from Fuyao's own raw x qfq closes was considered and deliberately
        rejected: the inference is only defined up to a multiplicative constant per window, it
        cannot respect the `fore = back / back[latest]` invariance under new corporate actions, and
        a wrong-but-plausible factor is worse than a loud, documented degradation. No network
        exception is raised from here either way.
        """
        if is_factor_neutral_symbol(normalize_thscode(symbol)):
            return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))

        local = getattr(self, "_local_provider", None)
        if local is None or not hasattr(local, "fetch_adjust_factors"):
            _warn_no_local_factor_table(symbol)
            return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))

        try:
            factors = local.fetch_adjust_factors(symbol, start=start, end=end)
        except Exception as exc:
            # Same style as the rest of this module: never let a data-source exception abort the
            # caller's chain. The empty frame turns into `MissingAdjustFactorsError` upstream, so
            # the failure stays visible where it matters instead of being silently swallowed.
            warnings.warn(
                f"FuyaoDataProvider: local MarketDB adjustment-factor read failed for {symbol!r}: "
                f"{type(exc).__name__}: {exc}. Returning an empty factor table (fail-closed)."
            )
            return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))

        if factors is None or factors.empty:
            # No rows for this symbol in the local table (not a factor-neutral symbol, not an
            # exception): still fail-closed, and no extra warning -- `MissingAdjustFactorsError`
            # names the symbol and the fix is the same (`scripts/migrate_adjust_basis.py` style
            # data repair / re-sync), so a per-symbol warning would only add noise.
            return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))

        return factors

    def fetch_universe(
        self, symbols: List[str], start: str, end: str, interval: str = "1d"
    ) -> Dict[str, pd.DataFrame]:
        if interval != "1d":
            raise ValueError(f"FuyaoDataProvider only supports interval='1d', got '{interval}'")

        if not symbols:
            return {}

        now_cst = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)
        target_end_dt = pd.to_datetime(end).normalize() if end else now_cst
        effective_target_end = min(target_end_dt, now_cst) if target_end_dt > now_cst else target_end_dt
        query_target_date = target_end_dt.date()

        # If local DuckDB is available, batch-fetch what is locally available
        local_results: Dict[str, pd.DataFrame] = {}
        if self._local_provider is not None:
            try:
                local_results = self._local_provider.fetch_universe(symbols, start, end, interval)
            except Exception:
                local_results = {}

        result: Dict[str, pd.DataFrame] = {}
        for s in symbols:
            thscode = normalize_thscode(s)
            if s in local_results:
                df = local_results[s]
                if not df.empty:
                    local_max_date = df.index[-1]
                    missing_days = check_missing_trading_days(local_max_date.date(), effective_target_end.date())
                    if df.index[-1] < effective_target_end and missing_days:
                        try:
                            inc_start_str = local_max_date.strftime("%Y-%m-%d")
                            remote_df = self._fetch_remote_ohlcv(s, inc_start_str, end, interval)
                            if not remote_df.empty:
                                if local_max_date in remote_df.index:
                                    loc_c = df.loc[local_max_date, "Close"]
                                    rem_c = remote_df.loc[local_max_date, "Close"]
                                    if loc_c > 0 and abs(rem_c - loc_c) / loc_c > 1e-4:
                                        scale = rem_c / loc_c
                                        df[["Open", "High", "Low", "Close"]] *= scale
                                df = pd.concat([df, remote_df])
                                df = df[~df.index.duplicated(keep="last")].sort_index()
                        except Exception as exc:
                            if "No price data returned" not in str(exc):
                                warnings.warn(
                                    f"Failed to fetch latest data for {s} from Fuyao API ({exc}); "
                                    f"using local MarketDB data up to {local_max_date.strftime('%Y-%m-%d')}."
                                )
                result[s] = df
            else:
                try:
                    result[s] = self.fetch_ohlcv(s, start, end, interval)
                except Exception as exc:
                    warnings.warn(f"Skipping {s}: {exc}")

            # Warning check for symbols that came from local_results (fetch_ohlcv already checks for others)
            if s in result and s in local_results:
                df = result[s]
                if not df.empty:
                    retrieved_latest_date = df.index[-1].date()
                    if query_target_date > retrieved_latest_date:
                        missed = check_missing_trading_days(retrieved_latest_date, query_target_date)
                        if missed:
                            missed_str = [d.strftime("%Y-%m-%d") for d in missed]
                            warnings.warn(
                                f"Query date '{query_target_date.strftime('%Y-%m-%d')}' is newer than "
                                f"retrieved data range latest date '{retrieved_latest_date.strftime('%Y-%m-%d')}' "
                                f"for {s} ({thscode}) (missing trading days: {missed_str})."
                            )

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

        try:
            import fuyao_client
        except ImportError:
            return result

        is_etf = is_etf_code(thscode)
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


_INDUSTRY_TIER_MAP: Dict[str, Tuple[str, float]] = {
    # Tier 1: Technology & High Growth (terminal_pe: 25.0)
    "电子": ("tier1_technology", 25.0),
    "计算机": ("tier1_technology", 25.0),
    "通信": ("tier1_technology", 25.0),
    "传媒": ("tier1_technology", 25.0),
    "半导体": ("tier1_technology", 25.0),
    # Tier 2: Consumer & Healthcare (terminal_pe: 20.0)
    "医药生物": ("tier2_consumer", 20.0),
    "食品饮料": ("tier2_consumer", 20.0),
    "农林牧渔": ("tier2_consumer", 18.0),
    "家用电器": ("tier2_consumer", 18.0),
    "社会服务": ("tier2_consumer", 18.0),
    "商贸零售": ("tier2_consumer", 18.0),
    "美容护理": ("tier2_consumer", 20.0),
    # Tier 3: Advanced Manufacturing & Materials (terminal_pe: 15.0)
    "电力设备": ("tier3_manufacturing", 16.0),
    "机械设备": ("tier3_manufacturing", 15.0),
    "汽车": ("tier3_manufacturing", 15.0),
    "国防军工": ("tier3_manufacturing", 18.0),
    "基础化工": ("tier3_manufacturing", 14.0),
    "有色金属": ("tier3_manufacturing", 14.0),
    "轻工制造": ("tier3_manufacturing", 14.0),
    # Tier 4: Utilities, Energy & Cyclicals (terminal_pe: 12.0)
    "公用事业": ("tier4_utilities", 12.0),
    "交通运输": ("tier4_utilities", 12.0),
    "煤炭": ("tier4_utilities", 10.0),
    "石油石化": ("tier4_utilities", 10.0),
    "钢铁": ("tier4_utilities", 10.0),
    "建筑材料": ("tier4_utilities", 12.0),
    "建筑装饰": ("tier4_utilities", 10.0),
    "环保": ("tier4_utilities", 12.0),
    # Tier 5: Financials & Real Estate (terminal_pe: 8.0)
    "银行": ("tier5_financials", 7.0),
    "非银金融": ("tier5_financials", 10.0),
    "房地产": ("tier5_financials", 8.0),
    "综合": ("tier3_manufacturing", 12.0),
}


class BaoStockDataProvider(BaseDataProvider):
    """Data provider sourcing historical daily and intraday K-lines from BaoStock (100% offline-mockable).

    Features:
    - Thread-safe socket queries via class-level lock.
    - Lazy baostock import inside _get_bs().
    - Frequency mapping ('5m'->'5', '15m'->'15', '30m'->'30', '60m'->'60', '1h'->'60', '1d'->'d', '1wk'->'w', '1mo'->'m').
    - Strict field restriction: intraday omits preclose, turn, peTTM, pctChg, isST.
    - 180-day chunking for intraday intervals to avoid BaoStock's 10,000-row limit.
    - 17-digit time parsing (YYYYMMDDHHMMSSsss -> YYYY-MM-DD HH:MM:SS).
    - Warnings and empty DataFrame for BSE (.BJ) or index intraday queries (not supported by BaoStock).
    """

    _lock = threading.Lock()
    _BAOSTOCK_FREQ_MAP = {
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "60m": "60",
        "1h": "60",
        "1d": "d",
        "1wk": "w",
        "1mo": "m",
    }

    def __init__(
        self,
        adjustflag: str = "2",
        keep_logged_in: bool = False,
        cache_dir: Optional[str] = None,
        cache: Optional[DuckDBCache] = None,
        **kwargs,
    ):
        self.adjustflag = str(adjustflag)
        self._keep_logged_in = bool(keep_logged_in)
        self._is_logged_in = False
        if cache is not None:
            self._duckdb_cache = cache
        elif cache_dir is not None:
            self._duckdb_cache = DuckDBCache(db_path=cache_dir)
        else:
            self._duckdb_cache = DuckDBCache(db_path="data") if os.path.exists("data") else None

    def logout(self) -> None:
        """Explicitly log out of BaoStock if keep_logged_in was used."""
        with self._lock:
            if self._is_logged_in:
                try:
                    bs = self._get_bs()
                    bs.logout()
                except Exception:
                    pass
                self._is_logged_in = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.logout()

    def _get_bs(self):
        try:
            import baostock as bs
            return bs
        except ImportError as exc:
            raise ImportError(
                "baostock is required to use BaoStockDataProvider. Install it via 'pip install baostock'."
            ) from exc

    def _fetch_ohlcv_internal(
        self, symbol: str, start: str, end: str, interval: str = "1d", adjustflag: Optional[str] = None
    ) -> pd.DataFrame:
        if interval not in self._BAOSTOCK_FREQ_MAP:
            raise ValueError(
                f"Unsupported interval '{interval}' for BaoStockDataProvider. "
                f"Supported intervals: {list(self._BAOSTOCK_FREQ_MAP.keys())}"
            )
        freq = self._BAOSTOCK_FREQ_MAP[interval]
        norm_sym = normalize_thscode(symbol)
        is_intraday = is_intraday_interval(interval)
        is_index = is_index_symbol(norm_sym)

        # BaoStock does not support intraday K-lines for BSE stocks (.BJ) or broad-market indices
        if is_intraday and (norm_sym.endswith(".BJ") or is_index):
            warnings.warn(
                f"BaoStock does not support intraday bars for BSE stocks or indices ({symbol}). "
                "Returning empty DataFrame."
            )
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"],
                index=pd.DatetimeIndex([], name="Date"),
            )

        # Strict field list restriction & adjustflag
        if is_intraday:
            fields = "date,time,code,open,high,low,close,volume,amount,adjustflag"
            req_adjustflag = adjustflag or self.adjustflag
        elif is_index:
            fields = "date,code,open,high,low,close,preclose,volume,amount,pctChg"
            req_adjustflag = "3"
        else:
            fields = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,pctChg,isST"
            req_adjustflag = adjustflag or self.adjustflag

        start_dt = pd.to_datetime(start).date()
        end_dt = pd.to_datetime(end).date()
        if start_dt > end_dt:
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"],
                index=pd.DatetimeIndex([], name="Date"),
            )

        # 180-day chunking for intraday intervals
        if is_intraday:
            chunks = []
            curr = start_dt
            while curr <= end_dt:
                nxt = min(curr + datetime.timedelta(days=179), end_dt)
                chunks.append((curr.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
                curr = nxt + datetime.timedelta(days=1)
        else:
            chunks = [(start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))]

        bs_code = to_baostock_code(symbol)
        dfs = []

        with self._lock:
            bs = self._get_bs()
            if not self._keep_logged_in or not self._is_logged_in:
                lg = bs.login()
                if hasattr(lg, "error_code") and lg.error_code != "0":
                    warnings.warn(f"BaoStock login failed: {getattr(lg, 'error_msg', 'unknown error')}")
                    return pd.DataFrame(
                        columns=["Open", "High", "Low", "Close", "Volume"],
                        index=pd.DatetimeIndex([], name="Date"),
                    )
                if self._keep_logged_in:
                    self._is_logged_in = True
            try:
                for c_start, c_end in chunks:
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        fields,
                        start_date=c_start,
                        end_date=c_end,
                        frequency=freq,
                        adjustflag=req_adjustflag,
                    )
                    if hasattr(rs, "error_code") and rs.error_code != "0":
                        warnings.warn(f"BaoStock query error: {getattr(rs, 'error_msg', '')}")
                        continue

                    chunk_df = None
                    if (
                        hasattr(rs, "data")
                        and isinstance(getattr(rs, "data", None), list)
                        and hasattr(rs, "cur_row_num")
                    ):
                        # Real BaoStock ResultData: extract page-by-page by advancing cur_row_num = len(rs.data)
                        # (Avoids baostock's broken Pandas-1.x df.append in rs.get_data() and avoids row-by-row overhead)
                        data_list = []
                        if rs.data:
                            data_list.extend(rs.data)
                            rs.cur_row_num = len(rs.data)
                            while getattr(rs, "error_code", "0") == "0" and rs.next():
                                if rs.data:
                                    data_list.extend(rs.data)
                                    rs.cur_row_num = len(rs.data)
                                else:
                                    break
                        if data_list:
                            col_names = rs.fields if (hasattr(rs, "fields") and rs.fields) else fields.split(",")
                            chunk_df = pd.DataFrame(data_list, columns=col_names)
                    elif hasattr(rs, "get_data"):
                        chunk_df = rs.get_data()
                    elif hasattr(rs, "next"):
                        data_list = []
                        while getattr(rs, "error_code", "0") == "0" and rs.next():
                            data_list.append(rs.get_row_data())
                        if data_list:
                            chunk_df = pd.DataFrame(data_list, columns=fields.split(","))

                    if chunk_df is not None and not chunk_df.empty:
                        dfs.append(chunk_df)
            finally:
                if not self._keep_logged_in:
                    try:
                        bs.logout()
                    except Exception:
                        pass

        if not dfs:
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"],
                index=pd.DatetimeIndex([], name="Date"),
            )

        raw_df = pd.concat(dfs, ignore_index=True)
        if raw_df.empty:
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"],
                index=pd.DatetimeIndex([], name="Date"),
            )

        # Parse DatetimeIndex: 17-digit time parsing for intraday intervals
        if is_intraday and "time" in raw_df.columns:
            ts_str = raw_df["time"].astype(str).str.strip().str[:14]
            idx = pd.to_datetime(ts_str, format="%Y%m%d%H%M%S", errors="coerce")
        else:
            idx = pd.to_datetime(raw_df["date"], format="%Y-%m-%d", errors="coerce")

        raw_df.index = idx
        raw_df.index.name = "Date"
        raw_df = raw_df[raw_df.index.notna()]

        col_map = {
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
            "amount": "Amount",
        }
        available_cols = {k: v for k, v in col_map.items() if k in raw_df.columns}
        df = raw_df[list(available_cols.keys())].rename(columns=available_cols)
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # Deduplicate index and sort
        df = df[~df.index.duplicated(keep="last")]
        df = df.sort_index()

        for req_col in ["Open", "High", "Low", "Close", "Volume"]:
            if req_col not in df.columns:
                df[req_col] = np.nan

        # Filter requested date range
        if is_intraday:
            start_ts = pd.Timestamp(start_dt)
            end_ts = pd.Timestamp(end_dt) + pd.Timedelta(days=1, microseconds=-1)
            df = df.loc[(df.index >= start_ts) & (df.index <= end_ts)]
        else:
            df = df.loc[(df.index.date >= start_dt) & (df.index.date <= end_dt)]

        return _drop_invalid_ohlcv_rows(df, symbol)

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        return self._fetch_ohlcv_internal(symbol, start, end, interval=interval, adjustflag=self.adjustflag)

    def fetch_raw_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        """Fetch raw unadjusted OHLCV bars without mutating instance basis."""
        return self._fetch_ohlcv_internal(symbol, start, end, interval=interval, adjustflag="3")

    def fetch_universe(
        self, symbols: List[str], start: str, end: str, interval: str = "1d"
    ) -> Dict[str, pd.DataFrame]:
        was_logged_in = self._keep_logged_in
        self._keep_logged_in = True
        try:
            res: Dict[str, pd.DataFrame] = {}
            for sym in symbols:
                try:
                    df = self.fetch_ohlcv(sym, start, end, interval)
                    if not df.empty:
                        res[sym] = df
                except Exception as exc:
                    warnings.warn(f"BaoStockDataProvider failed for {sym}: {exc}")
            return res
        finally:
            if not was_logged_in:
                self._keep_logged_in = False
                self.logout()

    def fetch_adjust_factors(
        self,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetch daily backward adjustment factor events from BaoStock.

        Real BaoStock fields: code, dividOperateDate, foreAdjustFactor, backAdjustFactor, adjustFactor.
        Index symbols and cash ETF 511880.SH return an empty DataFrame (factor-neutral).
        Always queries full history ('1990-01-01' to end) and returns only event rows.
        """
        norm_sym = normalize_thscode(symbol)
        if is_factor_neutral_symbol(norm_sym):
            return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))

        effective_end = end if end else datetime.datetime.now().strftime("%Y-%m-%d")
        bs_code = to_baostock_code(symbol)

        with self._lock:
            try:
                bs = self._get_bs()
            except Exception as exc:
                warnings.warn(f"BaoStock import failed in fetch_adjust_factors: {exc}")
                return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))

            if not self._keep_logged_in or not self._is_logged_in:
                lg = bs.login()
                if hasattr(lg, "error_code") and lg.error_code != "0":
                    warnings.warn(f"BaoStock login failed: {getattr(lg, 'error_msg', 'unknown error')}")
                    return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))
                if self._keep_logged_in:
                    self._is_logged_in = True

            try:
                rs = bs.query_adjust_factor(code=bs_code, start_date="1990-01-01", end_date=effective_end)
                if hasattr(rs, "error_code") and rs.error_code != "0":
                    warnings.warn(f"BaoStock query_adjust_factor error: {getattr(rs, 'error_msg', '')}")
                    return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))

                data_list = []
                fields = (
                    rs.fields
                    if hasattr(rs, "fields") and rs.fields
                    else ["code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"]
                )
                if (
                    hasattr(rs, "data")
                    and isinstance(getattr(rs, "data", None), list)
                    and hasattr(rs, "cur_row_num")
                ):
                    if rs.data:
                        data_list.extend(rs.data)
                        rs.cur_row_num = len(rs.data)
                        while getattr(rs, "error_code", "0") == "0" and rs.next():
                            if rs.data:
                                data_list.extend(rs.data)
                                rs.cur_row_num = len(rs.data)
                            else:
                                break
                elif hasattr(rs, "next"):
                    while getattr(rs, "error_code", "0") == "0" and rs.next():
                        data_list.append(rs.get_row_data())

                if not data_list:
                    return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))

                df_raw = pd.DataFrame(data_list, columns=fields)
                date_col = "dividOperateDate" if "dividOperateDate" in df_raw.columns else "date"
                df_raw["date"] = pd.to_datetime(df_raw[date_col], errors="coerce")
                df_raw = df_raw.dropna(subset=["date"]).sort_values("date")

                hfq_col = None
                for c in ["backAdjustFactor", "adjustFactor", "backward_factor", "back_adjust_factor", "factor_hfq"]:
                    if c in df_raw.columns:
                        hfq_col = c
                        break

                if hfq_col is None:
                    return pd.DataFrame(columns=["factor_hfq"], index=pd.DatetimeIndex([], name="date"))

                hfq_vals = pd.to_numeric(df_raw[hfq_col], errors="coerce").fillna(1.0)
                events_df = pd.DataFrame(
                    {"factor_hfq": hfq_vals.values.astype(float)},
                    index=pd.DatetimeIndex(df_raw["date"], name="date"),
                )
                events_df = events_df[~events_df.index.duplicated(keep="last")].sort_index()
                return events_df
            finally:
                if not self._keep_logged_in:
                    try:
                        bs.logout()
                    except Exception:
                        pass

    def fetch_financial_indicators(
        self, symbol: str, as_of_date: Optional[str] = None, lookback_years: int = 5
    ) -> FundamentalMetrics:
        """Fetch fundamental financial indicators for a China A-share from BaoStock with DuckDB caching."""
        # 1. Parse target year
        if as_of_date:
            as_of_str = str(as_of_date).strip()
            if len(as_of_str) == 4 and as_of_str.isdigit():
                target_year = int(as_of_str)
                req_end_date = f"{target_year}-12-31"
            else:
                as_of_dt = pd.to_datetime(as_of_date)
                req_end_date = as_of_dt.strftime("%Y-%m-%d")
                target_year = as_of_dt.year if as_of_dt.month >= 5 else as_of_dt.year - 1
        else:
            now_dt = datetime.datetime.now()
            req_end_date = now_dt.strftime("%Y-%m-%d")
            target_year = now_dt.year - 1

        # 2. Check DuckDB cache first if available
        if self._duckdb_cache is not None:
            cached = self._duckdb_cache.query_fundamentals("BaoStockDataProvider", symbol, report_year=target_year)
            if cached is not None:
                return cached

        # Default fallback metrics
        base_metrics = super().fetch_financial_indicators(symbol, as_of_date=as_of_date, lookback_years=lookback_years)

        def _to_float(v: Any) -> float:
            if v is None:
                return float("nan")
            try:
                s = str(v).strip()
                if not s or s.lower() in ("none", "nan", "null"):
                    return float("nan")
                val = float(s)
                return val if np.isfinite(val) else float("nan")
            except (ValueError, TypeError):
                return float("nan")

        bs_code = to_baostock_code(symbol)

        with self._lock:
            try:
                bs = self._get_bs()
            except Exception as exc:
                warnings.warn(f"BaoStock import failed: {exc}")
                return base_metrics

            if not self._keep_logged_in or not self._is_logged_in:
                lg = bs.login()
                if hasattr(lg, "error_code") and lg.error_code != "0":
                    warnings.warn(f"BaoStock login failed: {getattr(lg, 'error_msg', 'unknown error')}")
                    return base_metrics
                if self._keep_logged_in:
                    self._is_logged_in = True

            try:
                # Safe row extraction avoiding Pandas 2.x broken rs.get_data()
                def _safe_rows(rs) -> List[Dict[str, Any]]:
                    rows = []
                    if rs is None or (hasattr(rs, "error_code") and rs.error_code != "0"):
                        return rows
                    fields = rs.fields if (hasattr(rs, "fields") and rs.fields) else []
                    if hasattr(rs, "data") and isinstance(getattr(rs, "data", None), list) and hasattr(rs, "cur_row_num"):
                        raw_list = []
                        if rs.data:
                            raw_list.extend(rs.data)
                            rs.cur_row_num = len(rs.data)
                            while getattr(rs, "error_code", "0") == "0" and rs.next():
                                if rs.data:
                                    raw_list.extend(rs.data)
                                    rs.cur_row_num = len(rs.data)
                                else:
                                    break
                        for item in raw_list:
                            if fields and len(fields) == len(item):
                                rows.append(dict(zip(fields, item)))
                            else:
                                rows.append({f"col_{i}": v for i, v in enumerate(item)})
                    elif hasattr(rs, "next"):
                        while getattr(rs, "error_code", "0") == "0" and rs.next():
                            item = rs.get_row_data()
                            if fields and len(fields) == len(item):
                                rows.append(dict(zip(fields, item)))
                            else:
                                rows.append({f"col_{i}": v for i, v in enumerate(item)})
                    return rows

                # 1. Industry metadata
                industry_name = "Unknown"
                industry_tier = "tier3_manufacturing"
                terminal_pe = 10.0
                is_blacklisted = False
                blacklist_reason = None

                rs_ind = bs.query_stock_industry(code=bs_code)
                ind_rows = _safe_rows(rs_ind)
                if ind_rows:
                    row_i = ind_rows[0]
                    industry_name = row_i.get("industry") or "Unknown"
                    code_name = row_i.get("code_name", "")
                    if "*ST" in code_name or "ST" in code_name or "退" in code_name:
                        is_blacklisted = True
                        blacklist_reason = f"Special treatment / delisting risk ({code_name})"

                    for ind_key, (t_tier, t_pe) in _INDUSTRY_TIER_MAP.items():
                        if ind_key in industry_name:
                            industry_tier = t_tier
                            terminal_pe = t_pe
                            break

                # 2. Profit data (Q4 annual)
                rs_profit = bs.query_profit_data(code=bs_code, year=target_year, quarter=4)
                profit_rows = _safe_rows(rs_profit)
                if not profit_rows and target_year > 2000:
                    target_year -= 1
                    rs_profit = bs.query_profit_data(code=bs_code, year=target_year, quarter=4)
                    profit_rows = _safe_rows(rs_profit)

                stat_date = f"{target_year}-12-31"
                roe = float("nan")
                net_profit = float("nan")
                total_shares = float("nan")

                if profit_rows:
                    p_row = profit_rows[0]
                    stat_date = p_row.get("statDate") or f"{target_year}-12-31"
                    roe_val = _to_float(p_row.get("roeAvg"))
                    if not np.isnan(roe_val):
                        roe = roe_val / 100.0 if abs(roe_val) > 1.0 else roe_val
                    net_profit = _to_float(p_row.get("netProfit"))
                    total_shares = _to_float(p_row.get("totalShare"))

                # 3. Growth data & 5-year CAGR
                rs_growth = bs.query_growth_data(code=bs_code, year=target_year, quarter=4)
                growth_rows = _safe_rows(rs_growth)

                cagr_5yr = float("nan")
                try:
                    past_year = target_year - lookback_years
                    rs_past_profit = bs.query_profit_data(code=bs_code, year=past_year, quarter=4)
                    past_profit_rows = _safe_rows(rs_past_profit)
                    if past_profit_rows:
                        np_start = _to_float(past_profit_rows[0].get("netProfit"))
                        if not np.isnan(np_start) and not np.isnan(net_profit) and np_start > 0 and net_profit > 0:
                            cagr_5yr = float((net_profit / np_start) ** (1.0 / lookback_years) - 1.0)
                except Exception:
                    pass

                if np.isnan(cagr_5yr) and growth_rows:
                    yoy_ni = _to_float(growth_rows[0].get("YOYNI"))
                    if not np.isnan(yoy_ni):
                        cagr_5yr = yoy_ni / 100.0 if abs(yoy_ni) > 1.0 else yoy_ni

                # 4. Balance data
                rs_balance = bs.query_balance_data(code=bs_code, year=target_year, quarter=4)
                balance_rows = _safe_rows(rs_balance)
                debt_to_assets = float("nan")
                debt_to_equity = float("nan")
                if balance_rows:
                    b_row = balance_rows[0]
                    l_to_a = _to_float(b_row.get("liabilityToAsset"))
                    if not np.isnan(l_to_a):
                        debt_to_assets = l_to_a / 100.0 if l_to_a > 1.0 else l_to_a
                    a_to_e = _to_float(b_row.get("assetToEquity"))
                    if not np.isnan(a_to_e) and a_to_e > 1.0:
                        debt_to_equity = a_to_e - 1.0
                    elif not np.isnan(debt_to_assets) and debt_to_assets < 1.0:
                        debt_to_equity = debt_to_assets / (1.0 - debt_to_assets)

                # 5. Cash flow data
                rs_cf = bs.query_cash_flow_data(code=bs_code, year=target_year, quarter=4)
                cf_rows = _safe_rows(rs_cf)
                cfo_to_net_profit = float("nan")
                cfo = float("nan")
                if cf_rows:
                    cf_row = cf_rows[0]
                    cfo_np_raw = _to_float(cf_row.get("CFOToNP"))
                    if not np.isnan(cfo_np_raw):
                        cfo_to_net_profit = cfo_np_raw
                        if not np.isnan(net_profit):
                            cfo = cfo_to_net_profit * net_profit

                # 6. Dividend data
                rs_div = bs.query_dividend_data(code=bs_code, year=target_year)
                div_rows = _safe_rows(rs_div)
                div_per_share = float("nan")
                dividend_payout_ratio = float("nan")
                if div_rows:
                    d_row = div_rows[-1]
                    div_per_share = _to_float(d_row.get("diviPretaxPerShare"))
                    d_pct = _to_float(d_row.get("diviPercent"))
                    if not np.isnan(d_pct):
                        dividend_payout_ratio = d_pct / 100.0 if d_pct > 1.0 else d_pct

                # 7. Recent daily K-lines for latest peTTM, pbMRQ, close
                k_end = req_end_date
                k_start = (pd.to_datetime(k_end) - datetime.timedelta(days=40)).strftime("%Y-%m-%d")
                rs_k = bs.query_history_k_data_plus(
                    bs_code,
                    "date,close,peTTM,pbMRQ",
                    start_date=k_start,
                    end_date=k_end,
                    frequency="d",
                    adjustflag="2",
                )
                k_rows = _safe_rows(rs_k)
                close_price = float("nan")
                pe_ttm = float("nan")
                pb = float("nan")
                if k_rows:
                    last_k = k_rows[-1]
                    close_price = _to_float(last_k.get("close"))
                    pe_ttm = _to_float(last_k.get("peTTM"))
                    pb = _to_float(last_k.get("pbMRQ"))

                market_cap = (
                    close_price * total_shares
                    if (not np.isnan(close_price) and not np.isnan(total_shares))
                    else float("nan")
                )
                dividend_yield = (
                    div_per_share / close_price
                    if (not np.isnan(div_per_share) and not np.isnan(close_price) and close_price > 0)
                    else float("nan")
                )

                # Total assets and liabilities estimation
                total_assets = float("nan")
                total_liabilities = float("nan")
                if not np.isnan(market_cap) and not np.isnan(pb) and pb > 0:
                    equity = market_cap / pb
                    if not np.isnan(debt_to_assets) and 0 <= debt_to_assets < 1.0:
                        total_assets = equity / (1.0 - debt_to_assets)
                        total_liabilities = total_assets * debt_to_assets
                    else:
                        total_assets = equity

                metrics = FundamentalMetrics(
                    symbol=symbol,
                    report_year=target_year,
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
                    interest_bearing_debt=float("nan"),
                    dividend_yield=dividend_yield,
                    dividend_payout_ratio=dividend_payout_ratio,
                    pe_ttm=pe_ttm,
                    pb=pb,
                    total_shares=total_shares,
                    market_cap=market_cap,
                    industry_name=industry_name,
                    industry_tier=industry_tier,
                    terminal_pe=terminal_pe,
                    is_blacklisted=is_blacklisted,
                    blacklist_reason=blacklist_reason,
                )

                # Store into DuckDB cache if available
                if self._duckdb_cache is not None:
                    self._duckdb_cache.store_fundamentals("BaoStockDataProvider", metrics)

                return metrics

            except Exception as exc:
                warnings.warn(f"BaoStock fundamental indicators query failed for {symbol}: {exc}")
                return base_metrics
            finally:
                if not self._keep_logged_in:
                    try:
                        bs.logout()
                    except Exception:
                        pass


class EastMoneyDataProvider(BaseDataProvider):
    """Data provider sourcing real-time and historical K-lines from EastMoney (push2his.eastmoney.com).

    Features:
    - Direct connection to EastMoney Push2His endpoint.
    - O-C-H-L field order correction (EastMoney returns Open, Close, High, Low).
    - Mandatory lots-to-shares volume scaling (f56 is in lots of 100 shares).
    - Cross-check implied VWAP against amount / volume.
    - Reusable requests.Session with browser User-Agent/Referer and 0.15s rate-limit throttling.
    - Exponential backoff on HTTP 429 or transient network errors.
    """

    _lock = threading.Lock()
    _EASTMONEY_KLT_MAP = {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "60m": "60",
        "1h": "60",
        "1d": "101",
        "1wk": "102",
        "1mo": "103",
    }

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        adjust: str = "qfq",
        min_interval_sec: float = 0.15,
        **kwargs,
    ):
        self.adjust = adjust
        self.fqt = "1" if adjust == "qfq" else ("2" if adjust == "hfq" else "0")
        self.min_interval_sec = min_interval_sec
        self._last_request_time = 0.0
        if session is None:
            self.session = requests.Session()
            self.session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://quote.eastmoney.com/",
            })
        else:
            self.session = session

    def _fetch_ohlcv_internal(
        self, symbol: str, start: str, end: str, interval: str = "1d", fqt: Optional[str] = None
    ) -> pd.DataFrame:
        if interval not in self._EASTMONEY_KLT_MAP:
            raise ValueError(
                f"Unsupported interval '{interval}' for EastMoneyDataProvider. "
                f"Supported intervals: {list(self._EASTMONEY_KLT_MAP.keys())}"
            )
        klt = self._EASTMONEY_KLT_MAP[interval]
        secid = to_eastmoney_secid(symbol)

        start_dt = pd.to_datetime(start).date()
        end_dt = pd.to_datetime(end).date()
        if start_dt > end_dt:
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"],
                index=pd.DatetimeIndex([], name="Date"),
            )

        beg_str = start_dt.strftime("%Y%m%d")
        end_str = end_dt.strftime("%Y%m%d")
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        req_fqt = fqt if fqt is not None else self.fqt
        params = {
            "secid": secid,
            "klt": klt,
            "fqt": req_fqt,
            "lmt": "10000",
            "beg": beg_str,
            "end": end_str,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        }

        resp = None
        max_retries = 3
        for attempt in range(max_retries):
            with self._lock:
                now = time.time()
                elapsed = now - self._last_request_time
                if elapsed < self.min_interval_sec:
                    time.sleep(self.min_interval_sec - elapsed)
                self._last_request_time = time.time()
            try:
                r = self.session.get(url, params=params, timeout=10)
                if r.status_code == 429:
                    retry_after = r.headers.get("Retry-After")
                    wait_time = float(retry_after) if retry_after else 0.5 * (2 ** attempt)
                    time.sleep(wait_time)
                    continue
                r.raise_for_status()
                resp = r
                break
            except Exception as exc:
                if attempt == max_retries - 1:
                    warnings.warn(f"EastMoney request failed for {symbol}: {exc}")
                    return pd.DataFrame(
                        columns=["Open", "High", "Low", "Close", "Volume"],
                        index=pd.DatetimeIndex([], name="Date"),
                    )
                time.sleep(0.5 * (2 ** attempt))

        if resp is None:
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"],
                index=pd.DatetimeIndex([], name="Date"),
            )

        try:
            data = resp.json()
        except Exception:
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"],
                index=pd.DatetimeIndex([], name="Date"),
            )

        d = data.get("data") if isinstance(data, dict) else None
        klines = d.get("klines") if isinstance(d, dict) else None
        if not klines:
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"],
                index=pd.DatetimeIndex([], name="Date"),
            )

        records = []
        timestamps = []
        is_index = is_index_symbol(symbol)
        for line in klines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                continue
            ts_str = parts[0]
            # O-C-H-L field mapping: parts[1]=Open, parts[2]=Close, parts[3]=High, parts[4]=Low
            o = float(parts[1])
            c = float(parts[2])
            h = float(parts[3])
            l = float(parts[4])
            # Mandatory lots-to-shares volume scaling (f56 is in lots of 100 shares)
            vol = float(parts[5]) * 100.0
            amount = float(parts[6]) if len(parts) > 6 else float("nan")

            if vol > 0 and not np.isnan(amount) and amount > 0 and not is_index:
                implied_vwap = amount / vol
            else:
                implied_vwap = (h + l + c) / 3.0

            timestamps.append(ts_str)
            records.append({
                "Open": o,
                "High": h,
                "Low": l,
                "Close": c,
                "Volume": vol,
                "Amount": amount,
                "VWAP": implied_vwap,
            })

        if not records:
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume"],
                index=pd.DatetimeIndex([], name="Date"),
            )

        df = pd.DataFrame(records, index=pd.to_datetime(timestamps))
        df.index.name = "Date"
        df = df[~df.index.duplicated(keep="last")]
        df = df.sort_index()

        is_intraday = is_intraday_interval(interval)
        if is_intraday:
            start_ts = pd.Timestamp(start_dt)
            end_ts = pd.Timestamp(end_dt) + pd.Timedelta(days=1, microseconds=-1)
            df = df.loc[(df.index >= start_ts) & (df.index <= end_ts)]
        else:
            df = df.loc[(df.index.date >= start_dt) & (df.index.date <= end_dt)]

        return _drop_invalid_ohlcv_rows(df, symbol)

    def fetch_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        return self._fetch_ohlcv_internal(symbol, start, end, interval=interval, fqt=self.fqt)

    def fetch_raw_ohlcv(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        """Fetch raw unadjusted OHLCV bars without mutating instance basis."""
        return self._fetch_ohlcv_internal(symbol, start, end, interval=interval, fqt="0")


# Self-register into data provider registry once classes are fully defined
try:
    from .data import register_provider

    register_provider("marketdb", MarketDBDataProvider)
    register_provider("fuyao", FuyaoDataProvider)
    register_provider("financial_api", FuyaoDataProvider)
    register_provider("hithink", FuyaoDataProvider)
    register_provider("baostock", BaoStockDataProvider)
    register_provider("eastmoney", EastMoneyDataProvider)
except Exception:
    pass

