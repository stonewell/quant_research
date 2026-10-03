"""FastAPI server application for Live Trading Deployment.

Provides REST APIs for live quantitative trading operations, dynamic path configuration,
custom universe management, interactive holdings, daily archival, and responsive UI serving.
"""

import os
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

try:
    from .execution_service import ExecutionService
    from .models import (
        AccountStateModel,
        CustomUniverseModel,
        HoldingsUpdateModel,
        QuotesRequestModel,
        RunDeployRequest,
        RunHealthRequest,
        RunResponse,
        SettingsModel,
    )
    from .storage import StorageManager
except (ImportError, ValueError):
    from execution_service import ExecutionService
    from models import (
        AccountStateModel,
        CustomUniverseModel,
        HoldingsUpdateModel,
        QuotesRequestModel,
        RunDeployRequest,
        RunHealthRequest,
        RunResponse,
        SettingsModel,
    )
    from storage import StorageManager


def create_app(storage: Optional[StorageManager] = None) -> FastAPI:
    """Application factory for FastAPI live trading web app."""
    if storage is None:
        storage = StorageManager()

    execution_service = ExecutionService(storage)

    app = FastAPI(
        title="Live Trading Deployment Dashboard",
        description="Institutional quantitative trading operations, portfolio health gate, and live order generation",
        version="0.1.0",
    )

    # Enable CORS for local and web development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Attach storage and service to app state for testability and route access
    app.state.storage = storage
    app.state.execution_service = execution_service

    # --------------------------------------------------------------------------
    # API Routes: Configuration & Settings
    # --------------------------------------------------------------------------
    @app.get("/api/config", summary="Get overall operational configuration")
    async def get_config() -> Dict[str, Any]:
        """Return discovered strategies, universes, paths, and default parameters."""
        return {
            "strategies": storage.discover_strategies(),
            "universes": storage.discover_universes(),
            "settings": storage.get_settings(),
            "data_providers": ["synthetic", "marketdb", "fuyao", "yfinance"],
            "default_date": date.today().isoformat(),
            "default_portfolio_value": storage.default_portfolio_value,
        }

    @app.get("/api/settings", summary="Get path and runtime settings")
    async def get_settings() -> Dict[str, Any]:
        return storage.get_settings()

    @app.post("/api/settings", summary="Update path and runtime settings")
    async def update_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
        return storage.update_settings(payload)

    # --------------------------------------------------------------------------
    # API Routes: Universes
    # --------------------------------------------------------------------------
    @app.get("/api/universes", summary="List pre-defined and custom universes")
    async def list_universes() -> List[Dict[str, Any]]:
        return storage.discover_universes()

    @app.post("/api/universes", summary="Create or update a custom universe")
    async def create_custom_universe(payload: CustomUniverseModel) -> Dict[str, Any]:
        if not payload.name.strip():
            raise HTTPException(status_code=400, detail="Universe name cannot be empty")
        if not payload.symbols:
            raise HTTPException(status_code=400, detail="Symbols list cannot be empty")
        return storage.save_custom_universe(payload.name, payload.symbols, payload.description or "")

    @app.delete("/api/universes/{name}", summary="Delete a custom universe")
    async def delete_custom_universe(name: str) -> Dict[str, Any]:
        deleted = storage.delete_custom_universe(name)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Custom universe '{name}' not found")
        return {"status": "success", "message": f"Universe '{name}' deleted"}

    # --------------------------------------------------------------------------
    # API Routes: Holdings
    # --------------------------------------------------------------------------
    @app.get("/api/holdings", summary="Get current or historical holdings")
    async def get_holdings(
        date_str: Optional[str] = Query(None, alias="date"),
        strategy_key: Optional[str] = Query(None, alias="strategy"),
        universe_key: Optional[str] = Query(None, alias="universe"),
    ) -> Dict[str, Any]:
        return storage.get_holdings(date_str, strategy_key=strategy_key, universe_key=universe_key)

    @app.post("/api/holdings", summary="Save current holdings")
    async def save_holdings(payload: HoldingsUpdateModel) -> Dict[str, Any]:
        storage.save_holdings(
            payload.holdings,
            date_str=payload.date,
            strategy_key=payload.strategy_key,
            universe_key=payload.universe_key,
        )
        return {"status": "success", "message": "Holdings saved successfully", "holdings": payload.holdings}

    # --------------------------------------------------------------------------
    # API Routes: Quotes & Market Prices
    # --------------------------------------------------------------------------
    @app.get("/api/quotes", summary="Get current or historical market quotes for symbols")
    async def get_quotes_get(
        symbols: str = Query(..., description="Comma-separated symbols list"),
        as_of_date: Optional[str] = Query(None, description="Valuation date (YYYY-MM-DD)"),
        data_provider: Optional[str] = Query(None, description="Data provider name"),
        strategy_key: Optional[str] = Query(None, description="Strategy key filter for ticket fallback"),
        universe_key: Optional[str] = Query(None, description="Universe key filter for ticket fallback"),
    ) -> Dict[str, Any]:
        sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
        return storage.get_quotes(
            sym_list,
            as_of_date=as_of_date,
            data_provider=data_provider,
            strategy_key=strategy_key,
            universe_key=universe_key,
        )

    @app.post("/api/quotes", summary="Get market quotes for symbols payload")
    async def get_quotes_post(payload: QuotesRequestModel) -> Dict[str, Any]:
        return storage.get_quotes(
            payload.symbols,
            as_of_date=payload.as_of_date,
            data_provider=payload.data_provider,
            strategy_key=payload.strategy_key,
            universe_key=payload.universe_key,
        )

    # --------------------------------------------------------------------------
    # API Routes: Account State
    # --------------------------------------------------------------------------
    @app.get("/api/account-state", summary="Get current or historical account state")
    async def get_account_state(
        date_str: Optional[str] = Query(None, alias="date"),
        strategy_key: Optional[str] = Query(None, alias="strategy"),
        universe_key: Optional[str] = Query(None, alias="universe"),
    ) -> Dict[str, Any]:
        return storage.get_account_state(date_str, strategy_key=strategy_key, universe_key=universe_key)

    @app.post("/api/account-state", summary="Update account state")
    async def update_account_state(payload: AccountStateModel) -> Dict[str, Any]:
        data = payload.model_dump(exclude_unset=True)
        storage.save_account_state(
            data,
            date_str=payload.as_of_date,
            strategy_key=payload.strategy_key,
            universe_key=payload.universe_key,
        )
        return {"status": "success", "account_state": data}

    # --------------------------------------------------------------------------
    # API Routes: Execution (Stage 1 Health Check & Stage 2 Live Deploy)
    # --------------------------------------------------------------------------
    @app.post("/api/run-health", response_model=RunResponse, summary="Execute Stage 1 Health Check")
    async def run_health_check(payload: RunHealthRequest) -> Dict[str, Any]:
        try:
            return execution_service.run_health_check(
                strategy_file=payload.strategy_file,
                universe_file=payload.universe_file,
                custom_symbols=payload.custom_symbols,
                as_of_date=payload.as_of_date,
                portfolio_value=payload.portfolio_value,
                peak_nav=payload.peak_nav,
                data_provider=payload.data_provider,
                strategy_key=payload.strategy_key,
                universe_key=payload.universe_key,
                strategy_name=payload.strategy_name,
                universe_name=payload.universe_name,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/run-deploy", response_model=RunResponse, summary="Execute Stage 2 Live Deployment")
    async def run_live_deploy(payload: RunDeployRequest) -> Dict[str, Any]:
        try:
            return execution_service.run_live_deploy(
                strategy_file=payload.strategy_file,
                universe_file=payload.universe_file,
                custom_symbols=payload.custom_symbols,
                as_of_date=payload.as_of_date,
                portfolio_value=payload.portfolio_value,
                current_holdings=payload.current_holdings,
                data_provider=payload.data_provider,
                lot_size=payload.lot_size,
                strategy_key=payload.strategy_key,
                universe_key=payload.universe_key,
                strategy_name=payload.strategy_name,
                universe_name=payload.universe_name,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # --------------------------------------------------------------------------
    # API Routes: Historical Runs Archive (Scoped by Strategy & Universe)
    # --------------------------------------------------------------------------
    @app.get("/api/runs", summary="List historical daily run archives")
    async def list_runs(
        strategy: Optional[str] = Query(None, description="Optional strategy key filter"),
        universe: Optional[str] = Query(None, description="Optional universe key filter"),
        require_both: bool = Query(False, description="Strict mode: require both strategy and universe"),
    ) -> List[Dict[str, Any]]:
        return storage.list_runs(strategy_key=strategy, universe_key=universe, require_both=require_both)

    @app.get("/api/runs/latest", summary="Get latest run for a specific strategy and universe")
    async def get_latest_run(
        strategy: Optional[str] = Query(None, description="Strategy key"),
        strategy_key: Optional[str] = Query(None, description="Alias for strategy"),
        universe: Optional[str] = Query(None, description="Universe key"),
        universe_key: Optional[str] = Query(None, description="Alias for universe"),
    ) -> Dict[str, Any]:
        eff_strat = strategy or strategy_key
        eff_univ = universe or universe_key
        if not eff_strat or not eff_univ:
            raise HTTPException(status_code=400, detail="Both strategy and universe are required to fetch latest run")
        bundle = storage.get_latest_run(strategy_key=eff_strat, universe_key=eff_univ)
        if not bundle:
            raise HTTPException(
                status_code=404,
                detail=f"No archived runs found for strategy '{eff_strat}' and universe '{eff_univ}'",
            )
        return bundle

    @app.get("/api/runs/previous", summary="Get previous available run data before a given date")
    async def get_previous_run(
        date: Optional[str] = Query(None, description="Reference date (YYYY-MM-DD)"),
        target_date: Optional[str] = Query(None, description="Alias for date"),
        strategy: Optional[str] = Query(None, description="Strategy key"),
        strategy_key: Optional[str] = Query(None, description="Alias for strategy"),
        universe: Optional[str] = Query(None, description="Universe key"),
        universe_key: Optional[str] = Query(None, description="Alias for universe"),
    ) -> Dict[str, Any]:
        eff_date = date or target_date
        eff_strat = strategy or strategy_key
        eff_univ = universe or universe_key
        if not eff_strat or not eff_univ:
            return {
                "has_previous": False,
                "previous_date": None,
                "holdings": storage.get_holdings(date_str=eff_date),
                "account_state": storage.get_account_state(date_str=eff_date),
            }

        prev_bundle = storage.get_previous_run(eff_date, strategy_key=eff_strat, universe_key=eff_univ)
        if not prev_bundle:
            return {
                "has_previous": False,
                "previous_date": None,
                "holdings": storage.get_holdings(date_str=eff_date, strategy_key=eff_strat, universe_key=eff_univ),
                "account_state": storage.get_account_state(date_str=eff_date, strategy_key=eff_strat, universe_key=eff_univ),
            }

        prev_path = prev_bundle.get("path", "")
        post_holdings = storage.extract_post_run_holdings(prev_path) or prev_bundle.get("holdings", {})
        return {
            "has_previous": True,
            "previous_date": prev_bundle.get("date"),
            "strategy_key": prev_bundle.get("strategy_key"),
            "universe_key": prev_bundle.get("universe_key"),
            "holdings": post_holdings,
            "prices": prev_bundle.get("prices") or {},
            "account_state": prev_bundle.get("account_state") or {},
            "bundle": prev_bundle,
        }

    @app.get("/api/runs/{strategy_key}/{universe_key}/{date_str}", summary="Get keyed daily run archive")
    async def get_keyed_run(strategy_key: str, universe_key: str, date_str: str) -> Dict[str, Any]:
        run_bundle = storage.get_run(date_str, strategy_key=strategy_key, universe_key=universe_key)
        if not run_bundle:
            raise HTTPException(
                status_code=404,
                detail=f"No run archive found for '{strategy_key}/{universe_key}/{date_str}'",
            )
        return run_bundle

    @app.get("/api/runs/{strategy_key}/{universe_key}/{date_str}/csv", summary="Download trading ticket CSV for keyed run")
    async def download_keyed_ticket_csv(strategy_key: str, universe_key: str, date_str: str) -> Response:
        csv_text = storage.get_run_csv(date_str, strategy_key=strategy_key, universe_key=universe_key)
        if not csv_text:
            raise HTTPException(
                status_code=404,
                detail=f"No ticket CSV found for '{strategy_key}/{universe_key}/{date_str}'",
            )
        filename = f"live_trading_ticket_{strategy_key}_{universe_key}_{date_str}.csv"
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.get("/api/runs/{date_str}", summary="Get full daily archive for a specific date")
    async def get_run(
        date_str: str,
        strategy: Optional[str] = Query(None, description="Optional strategy key filter"),
        universe: Optional[str] = Query(None, description="Optional universe key filter"),
    ) -> Dict[str, Any]:
        run_bundle = storage.get_run(date_str, strategy_key=strategy, universe_key=universe)
        if not run_bundle:
            raise HTTPException(status_code=404, detail=f"No run archive found for date '{date_str}'")
        return run_bundle

    @app.get("/api/runs/{date_str}/csv", summary="Download trading ticket CSV for a date")
    async def download_ticket_csv(
        date_str: str,
        strategy: Optional[str] = Query(None, description="Optional strategy key filter"),
        universe: Optional[str] = Query(None, description="Optional universe key filter"),
    ) -> Response:
        csv_text = storage.get_run_csv(date_str, strategy_key=strategy, universe_key=universe)
        if not csv_text:
            raise HTTPException(status_code=404, detail=f"No ticket CSV found for date '{date_str}'")
        filename = f"live_trading_ticket_{date_str}.csv"
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    # --------------------------------------------------------------------------
    # Static Assets & Webpage Serving
    # --------------------------------------------------------------------------
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @app.api_route("/", methods=["GET", "HEAD"], summary="Serve Live Trading Web Dashboard", response_class=FileResponse)
        async def serve_index():
            index_path = os.path.join(static_dir, "index.html")
            if os.path.exists(index_path):
                return FileResponse(index_path)
            return PlainTextResponse("Static index.html not found.", status_code=404)

    return app
