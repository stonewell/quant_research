"""Unit tests for Live Trading Web Application: Storage, API, and Execution Service."""

import os
import shutil
import sys
import tempfile
from typing import Dict

import pytest
from fastapi.testclient import TestClient

# Ensure web/livetrading and repo root are in sys.path
_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for p in [_WEB_DIR, _REPO_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

from execution_service import ExecutionService
from server import create_app
from storage import StorageManager, find_repo_root


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory for test data and archives."""
    temp_dir = tempfile.mkdtemp()
    data_dir = os.path.join(temp_dir, "data")
    archive_dir = os.path.join(temp_dir, "runs")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(archive_dir, exist_ok=True)

    storage = StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=data_dir,
        archive_dir=archive_dir,
    )

    yield storage, temp_dir

    shutil.rmtree(temp_dir, ignore_errors=True)


def test_storage_manager_path_resolution(temp_workspace):
    storage, temp_dir = temp_workspace
    assert storage.repo_root == _REPO_ROOT
    assert os.path.isdir(storage.data_dir)
    assert os.path.isdir(storage.archive_dir)

    settings = storage.get_settings()
    assert settings["repo_root"] == _REPO_ROOT
    assert settings["data_dir"] == storage.data_dir
    assert settings["archive_dir"] == storage.archive_dir


def test_custom_universe_management(temp_workspace):
    storage, _ = temp_workspace

    # Initially empty
    assert len(storage.list_custom_universes()) == 0

    # Save custom universe
    univ = storage.save_custom_universe(
        name="AI Leaders",
        symbols=["NVDA", "msft", "GOOGL "],
        description="Top AI tech platforms",
    )
    assert univ["name"] == "AI Leaders"
    assert univ["symbols"] == ["NVDA", "MSFT", "GOOGL"]
    assert univ["count"] == 3

    # Check listing
    universes = storage.list_custom_universes()
    assert len(universes) == 1
    assert universes[0]["name"] == "AI Leaders"

    # Delete
    deleted = storage.delete_custom_universe("AI Leaders")
    assert deleted is True
    assert len(storage.list_custom_universes()) == 0


def test_holdings_and_account_state_daily_snapshot(temp_workspace):
    storage, _ = temp_workspace

    # 1. Global holdings
    test_holdings = {"601872.SH": 1000, "300394.SZ": 500}
    storage.save_holdings(test_holdings)
    loaded_global = storage.get_holdings()
    assert loaded_global == test_holdings

    # 2. Historical holdings snapshot for 2026-09-20
    hist_holdings = {"601872.SH": 800, "601728.SH": 2000}
    storage.save_holdings(hist_holdings, date_str="2026-09-20")

    assert storage.get_holdings(date_str="2026-09-20") == hist_holdings
    # Global holdings must remain updated
    assert storage.get_holdings() == hist_holdings

    # 3. Account state snapshot
    state_day1 = {"as_of_date": "2026-09-20", "current_nav": 105000.0, "peak_nav": 105000.0}
    storage.save_account_state(state_day1, date_str="2026-09-20")

    state_day2 = {"as_of_date": "2026-09-21", "current_nav": 102000.0, "peak_nav": 105000.0}
    storage.save_account_state(state_day2, date_str="2026-09-21")

    assert storage.get_account_state(date_str="2026-09-20")["current_nav"] == 105000.0
    assert storage.get_account_state(date_str="2026-09-21")["current_nav"] == 102000.0


def test_daily_run_archival_and_listing(temp_workspace):
    storage, _ = temp_workspace

    date_str = "2026-09-25"
    health_data = {"directive": "GO", "circuit_breaker_tier": "NORMAL", "drawdown": -0.02}
    ticket_data = [
        {"action": "SELL", "symbol": "601872.SH", "delta_shares": -200, "trade_value": -2000.0},
        {"action": "BUY", "symbol": "300394.SZ", "delta_shares": 500, "trade_value": 15000.0},
    ]
    csv_text = "action,symbol,delta_shares,trade_value\nSELL,601872.SH,-200,-2000.0\nBUY,300394.SZ,500,15000.0\n"
    summary = {"date": date_str, "status": "SUCCESS"}
    holdings = {"601872.SH": 800, "300394.SZ": 500}
    account_state = {"current_nav": 100000.0, "peak_nav": 102000.0}

    run_dir = storage.save_daily_run(
        date_str=date_str,
        health_data=health_data,
        ticket_data=ticket_data,
        csv_content=csv_text,
        summary=summary,
        holdings=holdings,
        account_state=account_state,
    )
    assert os.path.isdir(run_dir)

    # List runs
    runs = storage.list_runs()
    assert len(runs) == 1
    assert runs[0]["date"] == date_str
    assert runs[0]["directive"] == "GO"
    assert runs[0]["trade_count"] == 2

    # Get single run bundle
    bundle = storage.get_run(date_str)
    assert bundle is not None
    assert bundle["health_report"]["directive"] == "GO"
    assert len(bundle["trading_ticket"]) == 2
    assert bundle["holdings"] == holdings
    assert bundle["account_state"]["current_nav"] == 100000.0

    # Get CSV
    retrieved_csv = storage.get_run_csv(date_str)
    assert retrieved_csv == csv_text


def test_fastapi_endpoints(temp_workspace):
    storage, _ = temp_workspace
    app = create_app(storage)
    client = TestClient(app)

    # 1. GET /api/config
    res = client.get("/api/config")
    assert res.status_code == 200
    cfg = res.json()
    assert "strategies" in cfg
    assert "universes" in cfg
    assert "settings" in cfg

    # 2. Settings update
    res = client.post("/api/settings", json={"default_portfolio_value": 150000.0})
    assert res.status_code == 200
    assert res.json()["default_portfolio_value"] == 150000.0

    # 3. Custom universe via API
    res = client.post(
        "/api/universes",
        json={"name": "TestETF", "symbols": ["SPY", "QQQ", "BIL"], "description": "ETF basket"},
    )
    assert res.status_code == 200
    assert res.json()["name"] == "TestETF"

    # List universes includes custom
    res = client.get("/api/universes")
    assert res.status_code == 200
    names = [u["name"] for u in res.json()]
    assert any("TestETF" in n for n in names)

    # Delete custom universe
    res = client.delete("/api/universes/TestETF")
    assert res.status_code == 200

    # 4. Holdings API
    holdings_payload = {"holdings": {"AAPL": 100, "MSFT": 50}, "date": "2026-09-28"}
    res = client.post("/api/holdings", json=holdings_payload)
    assert res.status_code == 200

    res = client.get("/api/holdings?date=2026-09-28")
    assert res.status_code == 200
    assert res.json() == {"AAPL": 100, "MSFT": 50}

    # 5. Index page
    res = client.get("/")
    assert res.status_code == 200
    assert "Live Trading Station" in res.text

    # 6. Runs Archive API
    test_date = "2026-09-22"
    storage.save_daily_run(
        date_str=test_date,
        health_data={"directive": "GO"},
        ticket_data=[{"action": "BUY", "symbol": "AAPL"}],
        csv_content="action,symbol\nBUY,AAPL\n",
        holdings={"AAPL": 100},
        account_state={"current_nav": 100000.0},
    )

    res = client.get(f"/api/runs/{test_date}")
    assert res.status_code == 200
    bundle = res.json()
    assert bundle["health_report"]["directive"] == "GO"
    assert bundle["holdings"] == {"AAPL": 100}
    assert len(bundle["trading_ticket"]) == 1

    res = client.get(f"/api/runs/{test_date}/csv")
    assert res.status_code == 200
    assert "BUY,AAPL" in res.text


def test_execution_service_synthetic_runs(temp_workspace):
    """End-to-end offline execution test of Stage 1 Health and Stage 2 Live Deploy with synthetic data."""
    storage, _ = temp_workspace
    service = ExecutionService(storage)

    # Pick a standard strategy dump and universe
    strategy_file = os.path.join(
        _REPO_ROOT, "pipeline", "research_strategy", "results", "strategy_dumps", "chan_four_state_blend_strategy.json"
    )
    if not os.path.exists(strategy_file):
        pytest.skip(f"Strategy file not found at {strategy_file}")

    # Use a small universe with 2 assets for fast synthetic testing
    custom_symbols = ["601872.SH", "300394.SZ"]
    val_date = "2024-05-10"

    # 1. Stage 1 Health Check
    health_res = service.run_health_check(
        strategy_file=strategy_file,
        custom_symbols=custom_symbols,
        as_of_date=val_date,
        portfolio_value=100000.0,
        data_provider="synthetic",
    )
    assert health_res["status"] == "success"
    assert health_res["health_report"] is not None
    assert "directive" in health_res["health_report"]
    assert health_res["health_report"]["directive"] in ("GO", "CAUTION", "NO_GO")

    # 2. Stage 2 Live Deployment
    initial_holdings = {"601872.SH": 1000}
    deploy_res = service.run_live_deploy(
        strategy_file=strategy_file,
        custom_symbols=custom_symbols,
        as_of_date=val_date,
        portfolio_value=100000.0,
        current_holdings=initial_holdings,
        data_provider="synthetic",
    )
    assert deploy_res["status"] == "success"
    assert deploy_res["trading_ticket"] is not None
    assert len(deploy_res["trading_ticket"]) > 0

    # Verify that artifacts were archived under runs/2024-05-10/
    archived_run = storage.get_run(val_date)
    assert archived_run is not None
    assert archived_run["health_report"] is not None
    assert len(archived_run["trading_ticket"]) > 0
    assert archived_run["holdings"] == initial_holdings
    assert archived_run["has_csv"] is True


def test_strategy_name_extraction_and_discovery(temp_workspace):
    """Test that discover_strategies returns human-readable strategy_name without .json filenames."""
    storage, temp_dir = temp_workspace

    # Create dummy strategy files with different metadata structures
    strat_dir = os.path.join(temp_dir, "custom_strats")
    os.makedirs(strat_dir, exist_ok=True)
    storage.strategy_dir = strat_dir

    # 1. Spec with research_strategy_spec.entry_data.name
    spec1 = {
        "template_name": "chan_four_state_blend",
        "research_strategy_spec": {
            "entry_data": {"name": "Chan Four-State Structural Blend"}
        },
        "params": {"cash_proxy": "BIL"},
    }
    storage._save_json(os.path.join(strat_dir, "chan_blend_strategy.json"), spec1)

    # 2. Spec with top-level strategy_name
    spec2 = {
        "strategy_name": "Dual Momentum Trend Following",
        "template_name": "dual_momentum",
        "params": {"cash_proxy": "SHV"},
    }
    storage._save_json(os.path.join(strat_dir, "dual_mom_strategy.json"), spec2)

    # 3. Spec with only template_name
    spec3 = {
        "template_name": "adaptive_asset_allocation",
        "params": {},
    }
    storage._save_json(os.path.join(strat_dir, "adaptive_strategy.json"), spec3)

    all_discovered = storage.discover_strategies()
    discovered = [
        s for s in all_discovered
        if s["filename"] in ("chan_blend_strategy.json", "dual_mom_strategy.json", "adaptive_strategy.json")
    ]
    assert len(discovered) == 3

    names = {s["strategy_name"]: s for s in discovered}
    assert "Chan Four-State Structural Blend" in names
    assert names["Chan Four-State Structural Blend"]["strategy_key"] == "chan_four_state_blend"
    assert not names["Chan Four-State Structural Blend"]["strategy_name"].endswith(".json")

    assert "Dual Momentum Trend Following" in names
    assert names["Dual Momentum Trend Following"]["strategy_key"] == "dual_momentum"

    assert "Adaptive Asset Allocation" in names
    assert names["Adaptive Asset Allocation"]["strategy_key"] == "adaptive_asset_allocation"


def test_keyed_archive_hierarchy_and_strict_scoping(temp_workspace):
    """Test that runs are archived under <strategy_key>/<universe_key>/<date> and strictly scoped."""
    storage, _ = temp_workspace

    date_str = "2026-09-30"

    # Save run for Strategy A + Universe A
    storage.save_daily_run(
        date_str=date_str,
        strategy_key="strat_a",
        strategy_name="Strategy Alpha",
        universe_key="univ_a",
        universe_name="Universe Alpha",
        health_data={"directive": "GO", "score": 95},
        ticket_data=[{"action": "BUY", "symbol": "AAPL", "delta_shares": 100}],
        csv_content="action,symbol,delta_shares\nBUY,AAPL,100\n",
        holdings={"AAPL": 100},
    )

    # Save run for Strategy B + Universe B on the SAME date
    storage.save_daily_run(
        date_str=date_str,
        strategy_key="strat_b",
        strategy_name="Strategy Beta",
        universe_key="univ_b",
        universe_name="Universe Beta",
        health_data={"directive": "CAUTION", "score": 60},
        ticket_data=[{"action": "SELL", "symbol": "MSFT", "delta_shares": -50}],
        csv_content="action,symbol,delta_shares\nSELL,MSFT,-50\n",
        holdings={"MSFT": 0},
    )

    # Verify hierarchical directory structure
    dir_a = storage.get_run_dir("strat_a", "univ_a", date_str)
    dir_b = storage.get_run_dir("strat_b", "univ_b", date_str)
    assert os.path.isdir(dir_a)
    assert os.path.isdir(dir_b)
    assert dir_a != dir_b

    # Verify Strict Scoped listing: require_both=True
    # 1. Missing one or both keys must return empty list
    assert storage.list_runs(require_both=True) == []
    assert storage.list_runs(strategy_key="strat_a", require_both=True) == []
    assert storage.list_runs(universe_key="univ_a", require_both=True) == []

    # 2. Both keys provided returns strictly matching runs
    runs_a = storage.list_runs(strategy_key="strat_a", universe_key="univ_a", require_both=True)
    assert len(runs_a) == 1
    assert runs_a[0]["strategy_key"] == "strat_a"
    assert runs_a[0]["universe_key"] == "univ_a"
    assert runs_a[0]["directive"] == "GO"

    runs_b = storage.list_runs(strategy_key="strat_b", universe_key="univ_b", require_both=True)
    assert len(runs_b) == 1
    assert runs_b[0]["strategy_key"] == "strat_b"
    assert runs_b[0]["universe_key"] == "univ_b"
    assert runs_b[0]["directive"] == "CAUTION"

    # Mismatched pair returns empty list
    assert storage.list_runs(strategy_key="strat_a", universe_key="univ_b", require_both=True) == []

    # Verify scoped get_run and get_run_csv
    bundle_a = storage.get_run(date_str, strategy_key="strat_a", universe_key="univ_a")
    assert bundle_a is not None
    assert bundle_a["health_report"]["score"] == 95
    assert bundle_a["holdings"] == {"AAPL": 100}
    assert "BUY,AAPL,100" in storage.get_run_csv(date_str, strategy_key="strat_a", universe_key="univ_a")

    bundle_b = storage.get_run(date_str, strategy_key="strat_b", universe_key="univ_b")
    assert bundle_b is not None
    assert bundle_b["health_report"]["score"] == 60
    assert bundle_b["holdings"] == {"MSFT": 0}
    assert "SELL,MSFT,-50" in storage.get_run_csv(date_str, strategy_key="strat_b", universe_key="univ_b")


def test_latest_run_and_api_scoped_routes(temp_workspace):
    """Test get_latest_run and FastAPI endpoints with scoped parameters."""
    storage, _ = temp_workspace
    app = create_app(storage)
    client = TestClient(app)

    # Setup 2 runs for (strat_a, univ_a): day 1 and day 2
    storage.save_daily_run(
        date_str="2026-09-28",
        strategy_key="strat_a",
        strategy_name="Strat A",
        universe_key="univ_a",
        universe_name="Univ A",
        health_data={"directive": "CAUTION", "day": 1},
    )
    storage.save_daily_run(
        date_str="2026-09-29",
        strategy_key="strat_a",
        strategy_name="Strat A",
        universe_key="univ_a",
        universe_name="Univ A",
        health_data={"directive": "GO", "day": 2},
        ticket_data=[{"action": "BUY", "symbol": "NVDA"}],
        csv_content="action,symbol\nBUY,NVDA\n",
    )

    # 1. get_latest_run
    latest = storage.get_latest_run("strat_a", "univ_a")
    assert latest is not None
    assert latest["date"] == "2026-09-29"
    assert latest["health_report"]["day"] == 2

    # None for non-existent pair
    assert storage.get_latest_run("strat_nonexistent", "univ_a") is None

    # 2. GET /api/runs?require_both=true
    res = client.get("/api/runs?require_both=true")
    assert res.status_code == 200
    assert res.json() == []

    res = client.get("/api/runs?strategy=strat_a&universe=univ_a&require_both=true")
    assert res.status_code == 200
    assert len(res.json()) == 2
    assert res.json()[0]["date"] == "2026-09-29"

    # 3. GET /api/runs/latest
    res = client.get("/api/runs/latest")
    assert res.status_code == 400

    res = client.get("/api/runs/latest?strategy=strat_a&universe=univ_a")
    assert res.status_code == 200
    assert res.json()["date"] == "2026-09-29"
    assert res.json()["health_report"]["day"] == 2

    # 4. GET /api/runs/{strategy_key}/{universe_key}/{date}
    res = client.get("/api/runs/strat_a/univ_a/2026-09-29")
    assert res.status_code == 200
    assert res.json()["date"] == "2026-09-29"
    assert len(res.json()["trading_ticket"]) == 1

    # CSV download
    res = client.get("/api/runs/strat_a/univ_a/2026-09-29/csv")
    assert res.status_code == 200
    assert "BUY,NVDA" in res.text


def test_scoped_holdings_and_account_state_api(temp_workspace):
    """Test scoped holdings and account state endpoints."""
    storage, _ = temp_workspace
    app = create_app(storage)
    client = TestClient(app)

    # 1. Save scoped holdings for strat_a / univ_a on 2026-09-29
    res = client.post(
        "/api/holdings",
        json={
            "holdings": {"NVDA": 200, "AAPL": 50},
            "date": "2026-09-29",
            "strategy_key": "strat_a",
            "universe_key": "univ_a",
        },
    )
    assert res.status_code == 200

    # 2. Save scoped holdings for strat_b / univ_b on same date
    res = client.post(
        "/api/holdings",
        json={
            "holdings": {"600519.SH": 300},
            "date": "2026-09-29",
            "strategy_key": "strat_b",
            "universe_key": "univ_b",
        },
    )
    assert res.status_code == 200

    # 3. Retrieve scoped holdings
    res_a = client.get("/api/holdings?date=2026-09-29&strategy=strat_a&universe=univ_a")
    assert res_a.status_code == 200
    assert res_a.json() == {"NVDA": 200, "AAPL": 50}

    res_b = client.get("/api/holdings?date=2026-09-29&strategy=strat_b&universe=univ_b")
    assert res_b.status_code == 200
    assert res_b.json() == {"600519.SH": 300}

    # 4. Scoped account state
    res = client.post(
        "/api/account-state",
        json={
            "as_of_date": "2026-09-29",
            "strategy_key": "strat_a",
            "universe_key": "univ_a",
            "current_nav": 125000.0,
            "peak_nav": 130000.0,
        },
    )
    assert res.status_code == 200

    res_state = client.get("/api/account-state?date=2026-09-29&strategy=strat_a&universe=univ_a")
    assert res_state.status_code == 200
    assert res_state.json()["current_nav"] == 125000.0


def test_stock_name_resolution_and_custom_fix(temp_workspace):
    """Test that 601872.SH resolves to 招商轮船 and legacy 'Custom' names are sanitized."""
    storage, _ = temp_workspace
    from common.universe import get_stock_name

    # 1. Base resolution
    assert get_stock_name("601872.SH") == "招商轮船"
    assert get_stock_name("000938.SZ") == "紫光股份"

    # 2. Saving a ticket that mistakenly contains 'Custom' for 601872.SH
    date_str = "2026-10-03"
    storage.save_daily_run(
        date_str=date_str,
        strategy_key="adaptive_grid",
        universe_key="astock_custom",
        ticket_data=[
            {"symbol": "601872.SH", "name": "Custom", "action": "BUY", "delta_shares": 100},
            {"symbol": "000938.SZ", "name": "紫光股份", "action": "BUY", "delta_shares": 200},
        ],
    )

    # 3. Reading via get_run ensures 'Custom' is sanitized to '招商轮船'
    bundle = storage.get_run(date_str, strategy_key="adaptive_grid", universe_key="astock_custom")
    assert bundle is not None
    tickets = bundle["trading_ticket"]
    assert len(tickets) == 2
    sym_to_name = {t["symbol"]: t["name"] for t in tickets}
    assert sym_to_name["601872.SH"] == "招商轮船"
    assert sym_to_name["000938.SZ"] == "紫光股份"


def test_load_symbol_names_with_custom_universe_header(tmp_path):
    """Test that load_symbol_names never misinterprets header comments as stock names."""
    import sys
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
    from scripts.run_live_four_state_blend import load_symbol_names

    # Universe file generated with header comment
    univ_file = tmp_path / "resolved_universe.txt"
    univ_file.write_text(
        "# Custom universe generated 2026-10-03T10:02:22.960270\n"
        "601872.SH\n"
        "601728.SH\n"
        "000938.SZ\n",
        encoding="utf-8",
    )

    names = load_symbol_names(str(univ_file))
    assert names["601872.SH"] == "招商轮船"
    assert names["601872.SH"] != "Custom"
    assert names["601728.SH"] == "中国电信"
    assert names["000938.SZ"] == "紫光股份"


def test_resolve_universe_materialization_and_clean_parsing(temp_workspace):
    """Test ExecutionService._resolve_universe generates clean headers and stock annotations."""
    storage, _ = temp_workspace
    service = ExecutionService(storage)

    run_dir = os.path.join(storage.archive_dir, "test_run")
    os.makedirs(run_dir, exist_ok=True)

    resolved_path = service._resolve_universe(
        universe_file="__CUSTOM__",
        custom_symbols=["601872.SH", "300394.SZ"],
        run_dir=run_dir,
    )
    assert os.path.isfile(resolved_path)

    content = open(resolved_path, "r", encoding="utf-8").read()
    assert "# ==============================================================================" in content
    assert "601872.SH" in content
    assert "招商轮船" in content

    # Verify load_symbol_names on this generated file
    from scripts.run_live_four_state_blend import load_symbol_names
    names = load_symbol_names(resolved_path)
    assert names["601872.SH"] == "招商轮船"
    assert names["300394.SZ"] == "天孚通信"


def test_data_provider_local_storage_persistence():
    """Verify app.js includes localStorage persistence for selected data provider."""
    app_js_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "app.js")
    assert os.path.isfile(app_js_path)
    content = open(app_js_path, "r", encoding="utf-8").read()

    # 1. SESSION_KEYS definition includes DATA_PROVIDER
    assert "DATA_PROVIDER: \"livetrading_session_data_provider\"" in content

    # 2. saveSessionState saves selectProvider.value
    assert "localStorage.setItem(SESSION_KEYS.DATA_PROVIDER, el.selectProvider.value)" in content

    # 3. restoreSessionState restores selectProvider value
    assert "localStorage.getItem(SESSION_KEYS.DATA_PROVIDER)" in content

    # 4. change event listener on selectProvider triggers saveSessionState
    assert "el.selectProvider.addEventListener(\"change\"" in content


def test_custom_universe_key_alias_and_prefix_suffix_equivalence(temp_workspace):
    """Test that custom universe keys with prefix 'custom_' and suffix '_custom' are equivalent."""
    storage, _ = temp_workspace
    from storage import normalize_universe_key, keys_match, get_universe_key_variants

    # 1. Normalization & Matching
    assert normalize_universe_key("custom_astock_202609_20") == "astock_202609_20"
    assert normalize_universe_key("astock_202609_20_custom") == "astock_202609_20"
    assert normalize_universe_key("astock_202609_20") == "astock_202609_20"

    assert keys_match("custom_astock_202609_20", "astock_202609_20_custom") is True
    assert keys_match("astock_202609_20_custom", "custom_astock_202609_20") is True
    assert keys_match("custom_astock_202609_20", "astock_202609_20") is True
    assert keys_match("chan_four_state_blend", "CHAN_FOUR_STATE_BLEND") is True
    assert keys_match("sp500", "nasdaq100") is False

    # 2. Variants generation
    variants = get_universe_key_variants("custom_astock_202609_20")
    assert "astock_202609_20_custom" in variants
    assert "astock_202609_20" in variants

    # 3. Save run under astock_202609_20_custom on disk
    date_str = "2026-10-03"
    storage.save_daily_run(
        date_str=date_str,
        strategy_key="chan_four_state_blend",
        universe_key="astock_202609_20_custom",
        ticket_data=[{"symbol": "601872.SH", "action": "BUY", "delta_shares": 100}],
        csv_content="symbol,name,action,delta_shares\n601872.SH,招商轮船,BUY,100\n",
        summary={"strategy_key": "chan_four_state_blend", "universe_key": "astock_202609_20_custom"},
    )

    # 4. Lookup using custom_astock_202609_20
    bundle = storage.get_run(date_str, strategy_key="chan_four_state_blend", universe_key="custom_astock_202609_20")
    assert bundle is not None
    assert bundle["date"] == date_str
    assert bundle["strategy_key"] == "chan_four_state_blend"

    # CSV lookup using custom_astock_202609_20
    csv_text = storage.get_run_csv(date_str, strategy_key="chan_four_state_blend", universe_key="custom_astock_202609_20")
    assert csv_text is not None
    assert "601872.SH" in csv_text

    # list_runs filtering using custom_astock_202609_20
    runs = storage.list_runs(strategy_key="chan_four_state_blend", universe_key="custom_astock_202609_20", require_both=True)
    assert len(runs) == 1
    assert runs[0]["date"] == date_str

    # get_latest_run
    latest = storage.get_latest_run("chan_four_state_blend", "custom_astock_202609_20")
    assert latest is not None
    assert latest["date"] == date_str


def test_keyed_runs_api_custom_universe_prefix_suffix_resolution(temp_workspace):
    """Test FastAPI endpoints resolve custom universe runs across prefix and suffix conventions."""
    storage, _ = temp_workspace
    app = create_app(storage)
    client = TestClient(app)

    # Save run under suffix convention: astock_202609_20_custom
    date_str = "2026-10-03"
    storage.save_daily_run(
        date_str=date_str,
        strategy_key="chan_four_state_blend",
        universe_key="astock_202609_20_custom",
        ticket_data=[{"symbol": "601872.SH", "action": "BUY", "delta_shares": 500}],
        csv_content="symbol,name,action,delta_shares\n601872.SH,招商轮船,BUY,500\n",
        summary={"strategy_key": "chan_four_state_blend", "universe_key": "astock_202609_20_custom"},
    )

    # 1. GET /api/runs/{strategy_key}/{universe_key}/{date} with prefix 'custom_astock_202609_20'
    res = client.get(f"/api/runs/chan_four_state_blend/custom_astock_202609_20/{date_str}")
    assert res.status_code == 200
    data = res.json()
    assert data["date"] == date_str
    assert data["strategy_key"] == "chan_four_state_blend"
    assert len(data["trading_ticket"]) == 1

    # 2. GET /api/runs/.../csv with prefix 'custom_astock_202609_20'
    res_csv = client.get(f"/api/runs/chan_four_state_blend/custom_astock_202609_20/{date_str}/csv")
    assert res_csv.status_code == 200
    assert "601872.SH" in res_csv.text

    # 3. GET /api/runs?strategy=...&universe=...&require_both=true
    res_list = client.get("/api/runs?strategy=chan_four_state_blend&universe=custom_astock_202609_20&require_both=true")
    assert res_list.status_code == 200
    assert len(res_list.json()) == 1
    assert res_list.json()[0]["date"] == date_str

    # 4. GET /api/runs/latest?strategy=...&universe=...
    res_latest = client.get("/api/runs/latest?strategy=chan_four_state_blend&universe=custom_astock_202609_20")
    assert res_latest.status_code == 200
    assert res_latest.json()["date"] == date_str


def test_extract_post_run_holdings(temp_workspace):
    """Test extracting post-run target holdings from ticket CSV/JSON or snapshot files."""
    storage, temp_dir = temp_workspace

    # Case 1: live_trading_ticket.json with target_shares > 0
    run_dir1 = os.path.join(temp_dir, "run1")
    os.makedirs(run_dir1, exist_ok=True)
    ticket1 = [
        {"symbol": "600519.SH", "action": "BUY", "current_shares": 0, "target_shares": 200},
        {"symbol": "000858.SZ", "action": "HOLD", "current_shares": 500, "target_shares": 500},
        {"symbol": "601872.SH", "action": "SELL", "current_shares": 1000, "target_shares": 0},
        {"symbol": "BIL", "action": "HOLD", "current_shares": 100, "target_shares": 100},
    ]
    storage._save_json(os.path.join(run_dir1, "live_trading_ticket.json"), ticket1)
    h1 = storage.extract_post_run_holdings(run_dir1)
    assert h1 == {"600519.SH": 200, "000858.SZ": 500}
    assert "601872.SH" not in h1
    assert "BIL" not in h1

    # Case 2: post_holdings.json takes highest priority
    storage._save_json(os.path.join(run_dir1, "post_holdings.json"), {"601318.SH": 800})
    h2 = storage.extract_post_run_holdings(run_dir1)
    assert h2 == {"601318.SH": 800}

    # Case 3: ticket CSV only
    run_dir3 = os.path.join(temp_dir, "run3")
    os.makedirs(run_dir3, exist_ok=True)
    csv_content = (
        "symbol,name,action,target_shares\n"
        "601872.SH,招商轮船,BUY,1200\n"
        "300394.SZ,天孚通信,HOLD,600\n"
        "000001.SZ,平安银行,SELL,0\n"
    )
    with open(os.path.join(run_dir3, "live_trading_ticket.csv"), "w", encoding="utf-8") as f:
        f.write(csv_content)
    h3 = storage.extract_post_run_holdings(run_dir3)
    assert h3 == {"601872.SH": 1200, "300394.SZ": 600}
    assert "000001.SZ" not in h3


def test_previous_run_discovery_and_fallback(temp_workspace):
    """Test get_previous_run and fallback for get_holdings and get_account_state."""
    storage, _ = temp_workspace

    strat_k = "chan_four_state_blend"
    univ_k = "astock_202609_20_custom"

    # Day 1: 2026-10-01
    storage.save_daily_run(
        date_str="2026-10-01",
        strategy_key=strat_k,
        universe_key=univ_k,
        ticket_data=[{"symbol": "600519.SH", "action": "BUY", "target_shares": 100}],
        account_state={"current_nav": 100000.0, "peak_nav": 100000.0, "circuit_breaker_tier": "NORMAL"},
        summary={"strategy_key": strat_k, "universe_key": univ_k, "timestamp": "2026-10-01T15:00:00"},
    )

    # Day 2: 2026-10-03
    storage.save_daily_run(
        date_str="2026-10-03",
        strategy_key=strat_k,
        universe_key=univ_k,
        ticket_data=[
            {"symbol": "600519.SH", "action": "HOLD", "target_shares": 100},
            {"symbol": "601872.SH", "action": "BUY", "target_shares": 500},
        ],
        account_state={"current_nav": 105000.0, "peak_nav": 105000.0, "circuit_breaker_tier": "NORMAL"},
        summary={"strategy_key": strat_k, "universe_key": univ_k, "timestamp": "2026-10-03T15:00:00"},
    )

    # 1. Day 3: 2026-10-04 (no run) -> previous run should be Day 2 (2026-10-03)
    prev = storage.get_previous_run("2026-10-04", strategy_key=strat_k, universe_key=univ_k)
    assert prev is not None
    assert prev["date"] == "2026-10-03"

    # Holdings for 2026-10-04 should fallback to post-trade holdings of 2026-10-03
    h = storage.get_holdings("2026-10-04", strategy_key=strat_k, universe_key=univ_k)
    assert h == {"600519.SH": 100, "601872.SH": 500}

    # Account state for 2026-10-04 should fallback to 2026-10-03
    acc = storage.get_account_state("2026-10-04", strategy_key=strat_k, universe_key=univ_k)
    assert acc["current_nav"] == 105000.0
    assert acc["peak_nav"] == 105000.0

    # 2. Date between Day 1 and Day 2: 2026-10-02 -> previous run should be Day 1 (2026-10-01)
    prev_mid = storage.get_previous_run("2026-10-02", strategy_key=strat_k, universe_key=univ_k)
    assert prev_mid is not None
    assert prev_mid["date"] == "2026-10-01"

    # 3. Date before any run: 2026-09-30 -> no previous run
    prev_none = storage.get_previous_run("2026-09-30", strategy_key=strat_k, universe_key=univ_k)
    assert prev_none is None


def test_api_previous_run_endpoint(temp_workspace):
    """Test GET /api/runs/previous endpoint returns previous available run data."""
    storage, _ = temp_workspace
    app = create_app(storage)
    client = TestClient(app)

    strat_k = "chan_four_state_blend"
    univ_k = "astock_202609_20_custom"

    # Save run for 2026-10-03
    storage.save_daily_run(
        date_str="2026-10-03",
        strategy_key=strat_k,
        universe_key=univ_k,
        ticket_data=[
            {"symbol": "601872.SH", "action": "BUY", "target_shares": 800},
            {"symbol": "300394.SZ", "action": "BUY", "target_shares": 400},
        ],
        account_state={"current_nav": 102000.0, "peak_nav": 102000.0, "circuit_breaker_tier": "NORMAL"},
        summary={"strategy_key": strat_k, "universe_key": univ_k},
    )

    # 1. Query for date 2026-10-04 (which has no run data)
    res = client.get(f"/api/runs/previous?date=2026-10-04&strategy={strat_k}&universe={univ_k}")
    assert res.status_code == 200
    data = res.json()
    assert data["has_previous"] is True
    assert data["previous_date"] == "2026-10-03"
    assert data["holdings"] == {"601872.SH": 800, "300394.SZ": 400}
    assert data["account_state"]["current_nav"] == 102000.0

    # 2. Query for date before 2026-10-03
    res_before = client.get(f"/api/runs/previous?date=2026-10-01&strategy={strat_k}&universe={univ_k}")
    assert res_before.status_code == 200
    data_before = res_before.json()
    assert data_before["has_previous"] is False
    assert data_before["previous_date"] is None

    # 3. Query without params should not crash
    res_empty = client.get("/api/runs/previous")
    assert res_empty.status_code == 200
    assert res_empty.json()["has_previous"] is False


def test_app_js_date_change_and_previous_fallback():
    """Verify app.js includes handleDateSelected, calls /api/runs/previous, and updates holdings."""
    app_js_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "app.js")
    assert os.path.isfile(app_js_path)
    content = open(app_js_path, "r", encoding="utf-8").read()

    # 1. handleDateSelected is defined
    assert "async function handleDateSelected(targetDate" in content

    # 2. handleDateSelected fetches /api/runs/previous
    assert "/api/runs/previous?date=" in content

    # 3. handleDateSelected updates state.holdings and calls renderHoldingsTable
    assert "renderHoldingsTable(state.holdings)" in content
    assert "recalcHoldings()" in content

    # 4. handleDateSelected resets displays when no run exists
    assert "resetRunDisplaysToAwaiting(" in content

    # 5. onTopDateChange calls handleDateSelected
    assert "await handleDateSelected(newDate, true)" in content


def test_empty_date_dir_is_not_treated_as_valid_run_and_falls_back_to_previous(temp_workspace):
    """Test that an empty date directory on disk (e.g. 10-03) is not treated as a valid run and falls back to 09-29."""
    storage, temp_dir = temp_workspace
    app = create_app(storage)
    client = TestClient(app)

    strat_k = "chan_four_state_blend"
    univ_k = "astock_202609_20_custom"

    # Day 1: 2026-09-29 with valid ticket and account state
    storage.save_daily_run(
        date_str="2026-09-29",
        strategy_key=strat_k,
        universe_key=univ_k,
        ticket_data=[
            {"symbol": "601288.SH", "action": "SELL", "current_shares": 4900, "target_shares": 3300},
            {"symbol": "300394.SZ", "action": "HOLD", "current_shares": 100, "target_shares": 100},
        ],
        account_state={"current_nav": 203049.96, "peak_nav": 203049.96, "circuit_breaker_tier": "NORMAL"},
        summary={"strategy_key": strat_k, "universe_key": univ_k},
    )

    # Day 2: 2026-10-03 - empty directory created on disk without any run files
    empty_1003_dir = storage.get_run_dir(strat_k, univ_k, "2026-10-03", prefer_existing=False)
    os.makedirs(empty_1003_dir, exist_ok=True)
    assert os.path.exists(empty_1003_dir)

    # 1. get_run for 2026-10-03 must return None because it has no artifacts
    assert storage.get_run("2026-10-03", strategy_key=strat_k, universe_key=univ_k) is None

    # 2. GET /api/runs/.../2026-10-03 must return 404
    res_run = client.get(f"/api/runs/{strat_k}/{univ_k}/2026-10-03")
    assert res_run.status_code == 404

    # 3. GET /api/runs/previous for 2026-10-03 must return 2026-09-29 post-trade holdings
    res_prev = client.get(f"/api/runs/previous?date=2026-10-03&strategy={strat_k}&universe={univ_k}")
    assert res_prev.status_code == 200
    data_prev = res_prev.json()
    assert data_prev["has_previous"] is True
    assert data_prev["previous_date"] == "2026-09-29"
    assert data_prev["holdings"] == {"601288.SH": 3300, "300394.SZ": 100}
    assert data_prev["account_state"]["current_nav"] == 203049.96

    # 4. GET /api/holdings for 2026-10-03 must also fall back to 2026-09-29 holdings
    res_h = client.get(f"/api/holdings?date=2026-10-03&strategy={strat_k}&universe={univ_k}")
    assert res_h.status_code == 200
    assert res_h.json() == {"601288.SH": 3300, "300394.SZ": 100}


def test_quotes_endpoint_and_storage_quotes(temp_workspace):
    """Test get_quotes method and /api/quotes GET/POST endpoints."""
    storage, temp_dir = temp_workspace
    client = TestClient(create_app(storage))

    # 1. Direct storage.get_quotes with synthetic data
    res = storage.get_quotes(
        symbols=["AAPL", "BIL"],
        as_of_date="2026-09-29",
        data_provider="synthetic",
    )
    assert "quotes" in res
    assert res["quotes"]["BIL"]["price"] == 1.0
    assert res["quotes"]["BIL"]["source"] == "cash_proxy"
    assert "AAPL" in res["quotes"]
    assert res["quotes"]["AAPL"]["price"] is not None
    assert res["quotes"]["AAPL"]["price"] > 0

    # 2. GET /api/quotes
    res_get = client.get("/api/quotes?symbols=AAPL,BIL&as_of_date=2026-09-29&data_provider=synthetic")
    assert res_get.status_code == 200
    data_get = res_get.json()
    assert "quotes" in data_get
    assert data_get["quotes"]["BIL"]["price"] == 1.0
    assert data_get["quotes"]["AAPL"]["price"] is not None

    # 3. POST /api/quotes
    res_post = client.post("/api/quotes", json={
        "symbols": ["AAPL", "BIL"],
        "as_of_date": "2026-09-29",
        "data_provider": "synthetic"
    })
    assert res_post.status_code == 200
    data_post = res_post.json()
    assert "quotes" in data_post
    assert data_post["quotes"]["BIL"]["price"] == 1.0
    assert data_post["quotes"]["AAPL"]["price"] is not None


def test_previous_run_includes_prices(temp_workspace):
    """Test that /api/runs/previous returns prices mapping along with holdings and account state."""
    storage, temp_dir = temp_workspace
    client = TestClient(create_app(storage))

    strat_k = "test_strat"
    univ_k = "test_univ"

    storage.save_daily_run(
        date_str="2026-09-29",
        strategy_key=strat_k,
        universe_key=univ_k,
        ticket_data=[
            {"symbol": "601288.SH", "action": "SELL", "price": 6.94, "current_shares": 4900, "target_shares": 3300},
            {"symbol": "300394.SZ", "action": "HOLD", "price": 255.99, "current_shares": 100, "target_shares": 100},
            {"symbol": "BIL", "action": "RELEASE_CASH", "price": 1.0, "current_shares": 0, "target_shares": 0},
        ],
        account_state={"current_nav": 203049.96, "peak_nav": 203049.96},
        summary={"strategy_key": strat_k, "universe_key": univ_k, "data_provider": "marketdb"},
    )

    res = client.get(f"/api/runs/previous?date=2026-10-03&strategy={strat_k}&universe={univ_k}")
    assert res.status_code == 200
    data = res.json()
    assert data["has_previous"] is True
    assert data["previous_date"] == "2026-09-29"
    assert data["holdings"] == {"601288.SH": 3300, "300394.SZ": 100}
    assert "prices" in data
    assert data["prices"]["601288.SH"] == 6.94
    assert data["prices"]["300394.SZ"] == 255.99
    assert data["prices"]["BIL"] == 1.0


def test_app_js_holdings_value_and_provider_logic():
    """Verify app.js includes price handling, value calculation, and quote fetching."""
    js_path = os.path.join(find_repo_root(), "web", "livetrading", "static", "js", "app.js")
    with open(js_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "fetchQuotesForCurrentHoldings" in content
    assert "priceMap" in content
    assert "dataset.price" in content
    assert "valTd.dataset.val" in content
    assert "totalEquity += rowVal" in content



