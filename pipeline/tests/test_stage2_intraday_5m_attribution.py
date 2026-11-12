"""Stage-2 5m reporting: real attribution + freshness, never "provider does not support 5m".

FORENSIC CONTEXT (2026-11-11, `六` item 6)
-----------------------------------------
The zero-row branch of `scripts/run_live_strategy.py` printed

    [WARN] 5m 日内特征不可用：... 常见原因：provider does not support 5m (该数据源仅支持日线 interval='1d')

That diagnosis was DISPROVEN. The live run used a 5m-capable provider whose cache reached
2026-11-10, but the fallback queried a *different* provider whose 5m rows stopped at
2026-11-09, so the 5m layer was silently one session older than the daily data. Downstream
`RS_Down_5m` froze (via `dropna().iloc[-21:]`) and `realized_vol_21d` came out identical for
three consecutive sessions -- while the log blamed the provider's capabilities.

`common/intraday_features.load_intraday_5m_universe` now stamps each frame's `df.attrs`
with its provenance (`intraday_5m_provider` / `_source` / `_latest_ts`, `fallback_from` /
`fallback_to` / `fallback_reason`, and -- when `expected_last_session` is supplied --
`intraday_5m_stale` / `intraday_5m_lag_calendar_days`). `summarize_intraday_5m_loading`
turns exactly those attrs into the lines the runner prints:

* zero rows -> "该 provider 无这批标的的 5m 行" + the fallback chain it tried,
  and the discredited one-liner is NOT emitted as a diagnosis;
* rows present -> the `[INFO]` line carries `provider` / `source` / `fallback_reason` /
  `stale` / `lag_calendar_days` instead of only a timestamp;
* rows present but frozen -> an explicit "5m 缓存陈旧 (lag=N)" warning.

100% offline: synthetic frames and attrs only; no provider, no cache, no network.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd

from scripts.run_live_strategy import summarize_intraday_5m_loading


def _empty_frame():
    df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df.attrs["intraday_5m_available"] = False
    return df


def _frame(index, **attrs):
    df = pd.DataFrame(
        {"Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.0, "Volume": 100.0},
        index=pd.DatetimeIndex(index),
    )
    df.attrs.update({"intraday_5m_available": True, **attrs})
    return df


def _load(frames, symbols, **kwargs):
    return summarize_intraday_5m_loading(
        frames,
        symbols,
        provider_label=kwargs.pop("provider_label", "fuyao"),
        cutoff_time=kwargs.pop("cutoff_time", "14:15"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Zero rows: the real attribution replaces the disproven one
# ---------------------------------------------------------------------------


def test_zero_rows_names_missing_coverage_and_the_fallback_chain():
    frames = {"AAA": _empty_frame(), "BBB": _empty_frame()}

    text = "\n".join(_load(frames, ["AAA", "BBB"]))

    assert "5m 日内特征不可用" in text
    assert "该 provider 无这批标的的 5m 行" in text
    assert "provider=fuyao" in text
    # The fallback chain it actually walked is named, not guessed.
    assert "DuckDB 缓存" in text and "parquet" in text
    # The disproven diagnosis is gone (neither as a claim nor as a "常见原因").
    assert "provider does not support 5m" not in text
    assert "常见原因" not in text
    # ...and the consequence is still spelled out.
    assert "仅按日线信号生成工单" in text


def test_zero_rows_still_emits_the_web_marker_prefix():
    """`execution_service.INTRADAY_5M_WARN_MARKER` parses the child's stdout by this text."""
    lines = _load({"AAA": None}, ["AAA"])

    assert len(lines) == 1
    assert lines[0].startswith("[WARN] 5m 日内特征不可用")


# ---------------------------------------------------------------------------
# 2. Rows present: the INFO line carries provenance + freshness
# ---------------------------------------------------------------------------


def test_info_line_reports_provider_source_fallback_stale_and_lag():
    frames = {
        "AAA": _frame(
            ["2026-11-10 14:15:00"],
            intraday_5m_source="duckdb_cache",
            intraday_5m_provider="BaoStockDataProvider",
            intraday_5m_latest_ts="2026-11-10T14:15:00",
            fallback_from="FuyaoDataProvider",
            fallback_to="BaoStockDataProvider",
            fallback_reason="provider_unsupported_5m",
            intraday_5m_stale=False,
            intraday_5m_lag_calendar_days=0,
        ),
        "BBB": _frame(
            ["2026-11-10 14:15:00"],
            intraday_5m_source="duckdb_cache",
            intraday_5m_provider="BaoStockDataProvider",
            fallback_reason="provider_unsupported_5m",
            intraday_5m_stale=False,
            intraday_5m_lag_calendar_days=0,
        ),
    }

    lines = _load(frames, ["AAA", "BBB"])
    info = next(line for line in lines if line.startswith("[INFO] 5m 日内特征已聚合"))

    assert "有效标的数 2/2" in info
    assert "provider=BaoStockDataProviderx2" in info
    assert "source=duckdb_cachex2" in info
    assert "fallback_reason=provider_unsupported_5m x2".replace(" ", "") in info.replace(" ", "")
    assert "stale=False" in info
    assert "lag_calendar_days=0" in info
    # The fallback itself is called out, never silently absorbed.
    fallback_warn = [line for line in lines if "5m 数据来源回退" in line]
    assert fallback_warn, lines
    assert "FuyaoDataProvider->BaoStockDataProvider" in fallback_warn[0]
    assert "不等于所配置 provider 已交付" in fallback_warn[0]


def test_frozen_cache_is_reported_as_stale_with_its_lag():
    """THE FALSE DIAGNOSIS: rows exist, one session old -- not "no 5m support"."""
    frames = {
        "AAA": _frame(
            ["2026-11-06 14:15:00"],
            intraday_5m_source="duckdb_cache",
            intraday_5m_provider="BaoStockDataProvider",
            intraday_5m_stale=True,
            intraday_5m_lag_calendar_days=4,
        ),
        "BBB": _frame(
            ["2026-11-06 14:15:00"],
            intraday_5m_source="duckdb_cache",
            intraday_5m_provider="BaoStockDataProvider",
            intraday_5m_stale=True,
            intraday_5m_lag_calendar_days=4,
        ),
    }

    lines = _load(frames, ["AAA", "BBB"], expected_last_session="2026-11-10")
    info = next(line for line in lines if line.startswith("[INFO]"))

    assert "stale=True" in info
    assert "lag_calendar_days=4" in info
    stale_warn = [line for line in lines if "5m 缓存陈旧" in line]
    assert stale_warn, lines
    assert "2026-11-10" in stale_warn[0]
    assert "lag_calendar_days=4" in stale_warn[0]
    assert "5m 日内特征不可用" not in "\n".join(lines)


def test_missing_symbols_are_listed_without_blaming_the_provider():
    frames = {
        "AAA": _frame(
            ["2026-11-10 14:15:00"],
            intraday_5m_provider="FuyaoDataProvider",
            intraday_5m_stale=False,
            intraday_5m_lag_calendar_days=0,
        ),
        "BBB": _empty_frame(),
    }

    lines = _load(frames, ["AAA", "BBB", "CCC"])
    text = "\n".join(lines)

    assert "有效标的数 1/3" in text
    missing = [line for line in lines if "缺少 5m 数据" in line]
    assert missing, lines
    assert "BBB" in missing[0] and "CCC" in missing[0]
    assert "AAA" not in missing[0]


# ---------------------------------------------------------------------------
# 3. Frames loaded WITHOUT `expected_last_session` are measured, not assumed fresh
# ---------------------------------------------------------------------------


def test_freshness_is_measured_when_the_frames_carry_no_stale_attrs(monkeypatch):
    """A caller that forgot the flag must not make a frozen cache look fresh."""
    frame = _frame(["2026-11-06 14:15:00"], intraday_5m_provider="BaoStockDataProvider")

    lines = _load({"AAA": frame}, ["AAA"], expected_last_session="2026-11-10")
    info = next(line for line in lines if line.startswith("[INFO]"))

    # 2026-11-06 -> 2026-11-10 is a 4-calendar-day lag, measured by the shared owner.
    assert "stale=True" in info
    assert "lag_calendar_days=4" in info
    assert any("5m 缓存陈旧" in line for line in lines)


def test_no_expected_session_means_no_freshness_claim():
    """Without a session to compare against, `stale=False` would be a guess -- so: `None`."""
    frames = {"AAA": _frame(["2026-11-10 14:15:00"], intraday_5m_provider="FuyaoDataProvider")}

    lines = _load(frames, ["AAA"])
    info = next(line for line in lines if line.startswith("[INFO]"))

    assert "stale=None" in info, "no staleness evidence must not be reported as 'fresh'"
    assert "lag_calendar_days=None" in info
    assert not any("5m 缓存陈旧" in line for line in lines)
