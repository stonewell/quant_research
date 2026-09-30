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
