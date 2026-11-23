"""Unit tests for Universe Provider Framework (common/universe.py).
Guaranteed 100% offline using temporary files and mock modules.
"""

import argparse
import json
import os
import re
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
    get_stock_name,
    get_universe_provider,
    is_placeholder_stock_name,
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


def _parse_china_universe_structure(file_path: str):
    """Parse section headers, subsection counts, and per-ticker inline comment names."""
    sections = {}  # section_name -> (declared_count, actual_count)
    subsections = []  # list of (header, declared_count, actual_count)
    comment_names = {}  # ticker -> short Chinese name from preceding '# <name> (...)' comment

    current_section = None
    last_comment_name = None

    section_re = re.compile(r"^#\s*---\s*\[.*?\b(Core|Satellite):\s*(\d+)只")
    subsec_re = re.compile(r"^#\s*---\s*\d+\.\s+.*\((\d+)只\)\s*---$")
    name_comment_re = re.compile(r"^#\s*([^\s(#]+)\s*\(")
    ticker_re = re.compile(r"^\d{6}\.(SH|SZ)$")

    with open(file_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue

            sec_m = section_re.match(line)
            if sec_m:
                current_section = sec_m.group(1)
                sections[current_section] = [int(sec_m.group(2)), 0]
                last_comment_name = None
                continue

            sub_m = subsec_re.match(line)
            if sub_m:
                subsections.append([line, int(sub_m.group(1)), 0])
                last_comment_name = None
                continue

            if line.startswith("#"):
                name_m = name_comment_re.match(line)
                if name_m and current_section is not None:
                    last_comment_name = name_m.group(1).strip()
                continue

            if ticker_re.match(line):
                assert current_section is not None, f"Ticker {line} appeared before any section header"
                assert subsections, f"Ticker {line} appeared before any subsection header"
                sections[current_section][1] += 1
                subsections[-1][2] += 1
                assert last_comment_name is not None, f"Missing '# <简称> (...)' comment before {line} in {file_path}"
                comment_names[line] = last_comment_name
                last_comment_name = None

    return sections, subsections, comment_names


def _parse_blacklist_md(blacklist_path: str):
    """Parse §2 (#1-6 price-cap, #7-35 non-price-cap) and §5 (#1-5 forensic) from docs/black_list.md."""
    price_cap = set()
    non_price_cap = set()
    mode = None
    row_re = re.compile(r"^\|\s*(\d+)\s*\|\s*`(\d{6}\.(?:SH|SZ))`\s*\|")

    with open(blacklist_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if line.startswith("## 2."):
                mode = "sec2"
                continue
            if line.startswith("## 5."):
                mode = "sec5"
                continue
            if line.startswith("## "):
                mode = None
                continue

            if mode in ("sec2", "sec5"):
                m = row_re.match(line)
                if not m:
                    continue
                idx = int(m.group(1))
                sym = m.group(2)
                if mode == "sec2" and 1 <= idx <= 6:
                    price_cap.add(sym)
                else:
                    non_price_cap.add(sym)

    return price_cap, non_price_cap


def _parse_js_known_stock_names(js_path: str):
    """Parse KNOWN_STOCK_NAMES object literal from web/livetrading/static/js/app.js."""
    with open(js_path, "r", encoding="utf-8") as f:
        content = f.read()

    m = re.search(r"const\s+KNOWN_STOCK_NAMES\s*=\s*\{(.*?)\};", content, re.DOTALL)
    assert m is not None, f"Could not find KNOWN_STOCK_NAMES in {js_path}"
    block = m.group(1)
    pairs = re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', block)
    return dict(pairs)


def test_china_core_satellite_100_and_200k_universes_invariants():
    """Verify structural, blacklist, set-divergence, and 3-way name-parity invariants for both 100-stock China pools."""
    repo_root = _PROJECT_ROOT
    path_100 = os.path.join(repo_root, "docs", "universe", "china", "core_satellite_100_stocks.txt")
    path_200k = os.path.join(repo_root, "docs", "universe", "china", "core_satellite_200k_stocks.txt")

    # 1. Both symlinks exist, resolve to distinct real files, and each loads 100 unique valid .SH/.SZ tickers.
    assert os.path.islink(path_100), f"Expected symlink at {path_100}"
    assert os.path.islink(path_200k), f"Expected symlink at {path_200k}"
    real_100 = os.path.realpath(path_100)
    real_200k = os.path.realpath(path_200k)
    assert os.path.isfile(real_100), f"Symlink target missing: {real_100}"
    assert os.path.isfile(real_200k), f"Symlink target missing: {real_200k}"
    assert real_100 != real_200k, "core_satellite_100_stocks.txt and core_satellite_200k_stocks.txt must resolve to distinct files"

    syms_100 = FileUniverseProvider(path_100).get_symbols()
    syms_200k = FileUniverseProvider(path_200k).get_symbols()
    assert len(syms_100) == 100 and len(set(syms_100)) == 100
    assert len(syms_200k) == 100 and len(set(syms_200k)) == 100

    ticker_re = re.compile(r"^\d{6}\.(SH|SZ)$")
    for sym in syms_100 + syms_200k:
        assert ticker_re.match(sym), f"Invalid A-share symbol format: {sym}"

    # 2. Section headers (54+46=100; 58+42=100) and all 20 subsection count comments match actual ticker lines.
    sec_100, sub_100, names_100 = _parse_china_universe_structure(real_100)
    sec_200k, sub_200k, names_200k = _parse_china_universe_structure(real_200k)

    assert sec_100["Core"] == [54, 54]
    assert sec_100["Satellite"] == [46, 46]
    assert sec_200k["Core"] == [58, 58]
    assert sec_200k["Satellite"] == [42, 42]

    assert len(sub_100) == 20
    assert len(sub_200k) == 20
    for header, declared, actual in sub_100:
        assert actual == declared, f"100_stocks subsection mismatch ({actual} != {declared}): {header}"
    for header, declared, actual in sub_200k:
        assert actual == declared, f"200k_stocks subsection mismatch ({actual} != {declared}): {header}"
    assert sum(actual for _, _, actual in sub_100) == 100
    assert sum(actual for _, _, actual in sub_200k) == 100

    # 3. Exact 6-for-6 divergence and 94 shared backbone.
    set_100 = set(syms_100)
    set_200k = set(syms_200k)
    expected_100_only = {"600519.SH", "688256.SH", "002371.SZ", "300308.SZ", "300502.SZ", "300750.SZ"}
    expected_200k_only = {"600919.SH", "601319.SH", "600150.SH", "600066.SH", "605499.SH", "002920.SZ"}
    assert set_100 - set_200k == expected_100_only
    assert set_200k - set_100 == expected_200k_only
    assert len(set_100 & set_200k) == 94

    # 4. Blacklist, liquor/condiment exclusions, and 002601.SZ bug-fix checks parsed directly from docs/black_list.md.
    blacklist_path = os.path.join(repo_root, "docs", "black_list.md")
    price_cap_blacklist, non_price_cap_blacklist = _parse_blacklist_md(blacklist_path)
    assert len(price_cap_blacklist) == 6
    assert price_cap_blacklist == expected_100_only
    assert len(non_price_cap_blacklist) == 34

    liquor_condiment_exclusions = {"002304.SZ", "000568.SZ", "000858.SZ", "600809.SH", "603288.SH"}
    forbidden_both = non_price_cap_blacklist | liquor_condiment_exclusions | {"002601.SZ"}

    assert set_100.isdisjoint(forbidden_both), f"100_stocks overlaps forbidden tickers: {set_100 & forbidden_both}"
    assert set_200k.isdisjoint(forbidden_both), f"200k_stocks overlaps forbidden tickers: {set_200k & forbidden_both}"
    assert set_200k.isdisjoint(price_cap_blacklist), f"200k_stocks overlaps price-cap blacklist: {set_200k & price_cap_blacklist}"

    # 5. Name resolution and 3-way parity across .txt comments, common/universe.py, and app.js KNOWN_STOCK_NAMES.
    assert get_stock_name("002601.SZ") == "龙佰集团"
    assert get_stock_name("603596.SH") == "伯特利"

    js_path = os.path.join(repo_root, "web", "livetrading", "static", "js", "app.js")
    js_known_names = _parse_js_known_stock_names(js_path)
    assert js_known_names.get("002601.SZ") == "龙佰集团"
    assert js_known_names.get("603596.SH") == "伯特利"

    for sym in sorted(set_100 | set_200k):
        py_name = get_stock_name(sym)
        assert not is_placeholder_stock_name(py_name, sym), f"Placeholder stock name for {sym}: {py_name!r}"
        if sym in names_100:
            assert names_100[sym] == py_name, f"100_stocks comment name mismatch for {sym}: {names_100[sym]} != {py_name}"
        if sym in names_200k:
            assert names_200k[sym] == py_name, f"200k_stocks comment name mismatch for {sym}: {names_200k[sym]} != {py_name}"
        assert js_known_names.get(sym) == py_name, f"JS KNOWN_STOCK_NAMES mismatch for {sym}: {js_known_names.get(sym)!r} != {py_name!r}"


