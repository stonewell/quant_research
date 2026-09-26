"""Unit tests for Universe Provider Framework (common/universe.py).
Guaranteed 100% offline using temporary files and mock modules.
"""

import argparse
import json
import os
import sys
import tempfile

import pytest

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.universe import (
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


def test_get_universe_provider_bare_script_path_uses_code_provider():
    # Regression test: get_universe_provider() used to check os.path.exists()
    # before checking for code-provider intent, so a bare ".py" script path
    # (no ":function_name" suffix -- the exact usage CodeUniverseProvider's
    # "get_universe" default attr exists to support) that happened to exist
    # on disk was silently misrouted to FileUniverseProvider, which parsed
    # the Python source as a plain-text symbol list and returned garbage
    # tokens (e.g. 'DEF', 'GET_UNIVERSE():') instead of executing the script.
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=os.getcwd()) as f:
        f.write("def get_universe():\n    return ['AAPL', 'MSFT']\n")
        script_path = f.name

    try:
        prov = get_universe_provider(script_path)
        assert isinstance(prov, CodeUniverseProvider)
        assert prov.get_symbols() == ["AAPL", "MSFT"]
    finally:
        os.remove(script_path)


def test_get_universe_provider_windows_absolute_json_path_not_misdetected_as_code_spec():
    # A Windows absolute path like "C:\\...\\universe.json" contains a colon
    # (the drive letter) that must NOT be mistaken for a "path:attr" code
    # specifier -- it should still resolve as a FileUniverseProvider.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, dir=os.getcwd()) as f:
        json.dump(["QQQ", "GLD"], f)
        json_path = f.name

    try:
        prov = get_universe_provider(json_path)
        assert isinstance(prov, FileUniverseProvider)
        assert prov.get_symbols() == ["QQQ", "GLD"]
    finally:
        os.remove(json_path)


def test_static_universe_provider():
    p1 = StaticUniverseProvider(["SPY", "qqq", "spy", " IWM "])
    assert p1.get_symbols() == ["SPY", "QQQ", "IWM"]

    p2 = StaticUniverseProvider("SPY, QQQ, GLD")
    assert p2.get_symbols() == ["SPY", "QQQ", "GLD"]

    p3 = StaticUniverseProvider(None)
    assert p3.get_symbols() == []


def test_file_universe_provider_json():
    # Test JSON list
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(["spy", "qqq", "aapl"], f)
        json_path = f.name

    try:
        p = FileUniverseProvider(json_path)
        assert p.get_symbols() == ["SPY", "QQQ", "AAPL"]
    finally:
        os.remove(json_path)

    # Test JSON dict with 'basket'
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"basket": [{"symbol": "GLD"}, {"ticker": "TLT"}, "VNQ"]}, f)
        json_dict_path = f.name

    try:
        p = FileUniverseProvider(json_dict_path)
        assert p.get_symbols() == ["GLD", "TLT", "VNQ"]
    finally:
        os.remove(json_dict_path)


def test_file_universe_provider_text():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# Comment line\n")
        f.write("SPY QQQ\n")
        f.write("AAPL, MSFT\n")
        txt_path = f.name

    try:
        p = FileUniverseProvider(txt_path)
        assert p.get_symbols() == ["SPY", "QQQ", "AAPL", "MSFT"]
    finally:
        os.remove(txt_path)


def test_file_universe_provider_rejects_non_list_basket_value():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"basket": "SPY"}, f)  # a string, not a list -- would otherwise iterate char-by-char
        json_path = f.name

    try:
        p = FileUniverseProvider(json_path)
        with pytest.raises(ValueError, match="must be a JSON list"):
            p.get_symbols()
    finally:
        os.remove(json_path)


def test_file_universe_provider_warns_on_malformed_entries():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"basket": ["SPY", 123, {"not_symbol_or_ticker": "x"}, "QQQ"]}, f)
        json_path = f.name

    try:
        p = FileUniverseProvider(json_path)
        with pytest.warns(UserWarning, match="malformed"):
            symbols = p.get_symbols()
        assert symbols == ["SPY", "QQQ"]
    finally:
        os.remove(json_path)


def test_file_universe_provider_raises_on_all_malformed():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"basket": [123, {"not_symbol_or_ticker": "x"}]}, f)
        json_path = f.name

    try:
        p = FileUniverseProvider(json_path)
        with pytest.raises(ValueError, match="zero valid ticker symbols"):
            p.get_symbols()
    finally:
        os.remove(json_path)


def test_code_universe_provider_callable():
    def custom_generator(sector="tech"):
        if sector == "tech":
            return ["AAPL", "NVDA", "MSFT"]
        return ["SPY", "GLD"]

    p = CodeUniverseProvider(custom_generator)
    assert p.get_symbols(sector="tech") == ["AAPL", "NVDA", "MSFT"]
    assert p.get_symbols(sector="macro") == ["SPY", "GLD"]


def test_code_universe_provider_script_file():
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write("def my_symbols(prefix=''):\n")
        f.write("    return [f'{prefix}A', f'{prefix}B']\n")
        script_path = f.name

    try:
        p = CodeUniverseProvider(f"{script_path}:my_symbols")
        assert p.get_symbols(prefix="TEST_") == ["TEST_A", "TEST_B"]
    finally:
        os.remove(script_path)


def test_code_universe_provider_class_object():
    class DynamicClass:
        def get_symbols(self, **kwargs):
            return ["SPY", "QQQ", "IWM"]

    p = CodeUniverseProvider(lambda **kw: DynamicClass())
    assert p.get_symbols() == ["SPY", "QQQ", "IWM"]


def test_universe_registry_and_factory():
    class DummyProvider(BaseUniverseProvider):
        def get_symbols(self, **kwargs):
            return ["DUMMY1", "DUMMY2"]

    register_universe_provider("dummy", DummyProvider)

    p = get_universe_provider("dummy")
    assert isinstance(p, DummyProvider)
    assert p.get_symbols() == ["DUMMY1", "DUMMY2"]


def test_resolve_universe_symbols_priority():
    # 1. file_path takes priority over symbols / default
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(["FILE_A", "FILE_B"], f)
        json_path = f.name

    try:
        res = resolve_universe_symbols(
            symbols=["SYM_A"],
            file_path=json_path,
            default_symbols=["DEF_A"],
        )
        assert res == ["FILE_A", "FILE_B"]
    finally:
        os.remove(json_path)

    # 2. explicit symbols take priority over default
    res = resolve_universe_symbols(symbols=["SYM_A", "SYM_B"], default_symbols=["DEF_A"])
    assert res == ["SYM_A", "SYM_B"]

    # 3. default symbols used when nothing else provided
    res = resolve_universe_symbols(default_symbols=["DEF_A", "DEF_B"])
    assert res == ["DEF_A", "DEF_B"]


def test_cli_argument_helpers():
    parser = argparse.ArgumentParser()
    add_universe_cli_args(parser, default_universe=["DEF1", "DEF2"])

    # Test explicit --universe
    args = parser.parse_args(["--universe", "AAPL", "MSFT"])
    res = resolve_universe_from_args(args, default_symbols=["DEF1", "DEF2"])
    assert res == ["AAPL", "MSFT"]

    # Test default fallback
    args = parser.parse_args([])
    res = resolve_universe_from_args(args, default_symbols=["DEF1", "DEF2"])
    assert res == ["DEF1", "DEF2"]

    # Test --universe-file
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(["FILE_SYM"], f)
        json_path = f.name

    try:
        args = parser.parse_args(["--universe-file", json_path])
        res = resolve_universe_from_args(args)
        assert res == ["FILE_SYM"]
    finally:
        os.remove(json_path)


def test_core_satellite_universes_exist_and_load():
    """Verify that China, Hong Kong, and US core-satellite universes exist and load 22 valid symbols."""
    repo_root = _PROJECT_ROOT

    # 1. China
    cn_path = os.path.join(
        repo_root, "docs", "universe", "china", "核心卫星组合精选(央国企红利底仓+科技龙头增强)", "core_satellite_22_stocks.txt"
    )
    assert os.path.exists(cn_path), f"China universe missing at {cn_path}"
    p_cn = FileUniverseProvider(cn_path)
    symbols_cn = p_cn.get_symbols()
    assert len(symbols_cn) == 22
    assert "601872.SH" in symbols_cn
    assert "300394.SZ" in symbols_cn

    # 2. Hong Kong
    hk_path = os.path.join(
        repo_root, "docs", "universe", "hongkong", "核心卫星组合精选(高股息红利底仓+科技互联网增强)", "core_satellite_22_stocks.txt"
    )
    assert os.path.exists(hk_path), f"HK universe missing at {hk_path}"
    p_hk = FileUniverseProvider(hk_path)
    symbols_hk = p_hk.get_symbols()
    assert len(symbols_hk) == 22
    assert "0883.HK" in symbols_hk
    assert "0700.HK" in symbols_hk
    for s in symbols_hk:
        assert s.endswith(".HK")

    # 3. United States
    us_path = os.path.join(
        repo_root, "docs", "universe", "us", "核心卫星组合精选(价值现金流红利底仓+AI科技巨头增强)", "core_satellite_22_stocks.txt"
    )
    assert os.path.exists(us_path), f"US universe missing at {us_path}"
    p_us = FileUniverseProvider(us_path)
    symbols_us = p_us.get_symbols()
    assert len(symbols_us) == 22
    assert "BRK-B" in symbols_us
    assert "NVDA" in symbols_us
    assert "AAPL" in symbols_us

