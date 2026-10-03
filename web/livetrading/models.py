"""Pydantic data models for the Live Trading Deployment Web Application."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class SettingsModel(BaseModel):
    """Dynamic path and execution configuration settings."""
    repo_root: str = Field(..., description="Root directory of the quantitative workspace")
    strategy_dir: str = Field(..., description="Directory containing strategy JSON dumps")
    universe_dir: str = Field(..., description="Directory containing pre-defined universe files")
    data_dir: str = Field(..., description="Base directory for persistence (holdings, custom universes)")
    archive_dir: str = Field(..., description="Directory for historical daily snapshot archives")
    cache_dir: Optional[str] = Field(None, description="DuckDB / parquet market data cache folder")
    stage1_script: Optional[str] = Field(None, description="Path to Stage 1 Health Check runner script")
    stage2_script: Optional[str] = Field(None, description="Path to Stage 2 Live Deployment runner script")
    python_executable: Optional[str] = Field(None, description="Python executable for script execution")
    default_data_provider: str = Field("marketdb", description="Default data provider")
    default_portfolio_value: float = Field(100000.0, description="Default portfolio NAV value")


class CustomUniverseModel(BaseModel):
    """User-customized universe definition."""
    name: str = Field(..., description="Unique name for the custom universe")
    description: Optional[str] = Field("", description="Optional description or note")
    symbols: List[str] = Field(..., description="List of asset ticker symbols")


class HoldingItemModel(BaseModel):
    """Individual holding item."""
    symbol: str
    name: Optional[str] = ""
    shares: Optional[float] = None
    weight: Optional[float] = None
    price: Optional[float] = None
    market_value: Optional[float] = None
    notes: Optional[str] = ""


class HoldingsUpdateModel(BaseModel):
    """Holdings update payload."""
    holdings: Dict[str, Any] = Field(
        ..., description="Holdings mapping (symbol -> shares or weight, e.g. {'600519.SH': 100})"
    )
    date: Optional[str] = Field(None, description="Optional date (YYYY-MM-DD) for historical archival")
    portfolio_value: Optional[float] = Field(None, description="Total portfolio value for weight/share math")
    strategy_key: Optional[str] = Field(None, description="Strategy identifier key")
    universe_key: Optional[str] = Field(None, description="Universe identifier key")


class AccountStateModel(BaseModel):
    """Persistent live account state model."""
    as_of_date: Optional[str] = None
    strategy_key: Optional[str] = None
    universe_key: Optional[str] = None
    current_nav: Optional[float] = None
    peak_nav: Optional[float] = None
    circuit_breaker_tier: Optional[str] = "NORMAL"
    circuit_breaker_label: Optional[str] = "🟢 NORMAL"
    linear_equity_scale: Optional[float] = 1.0
    tier1_consecutive_bars: Optional[int] = 0
    tier3_consecutive_bars: Optional[int] = 0
    freeze_remaining_bars: Optional[int] = 0
    nav_history_10d: Optional[List[float]] = []
    holdings: Optional[Dict[str, Any]] = None


class RunHealthRequest(BaseModel):
    """Request payload for running Stage 1 Health Check."""
    strategy_file: str
    universe_file: Optional[str] = None
    custom_symbols: Optional[List[str]] = None
    strategy_key: Optional[str] = None
    universe_key: Optional[str] = None
    strategy_name: Optional[str] = None
    universe_name: Optional[str] = None
    as_of_date: Optional[str] = None
    portfolio_value: float = 100000.0
    peak_nav: Optional[float] = None
    data_provider: str = "marketdb"


class RunDeployRequest(BaseModel):
    """Request payload for running Stage 2 Live Deployment."""
    strategy_file: str
    universe_file: Optional[str] = None
    custom_symbols: Optional[List[str]] = None
    strategy_key: Optional[str] = None
    universe_key: Optional[str] = None
    strategy_name: Optional[str] = None
    universe_name: Optional[str] = None
    as_of_date: Optional[str] = None
    portfolio_value: float = 100000.0
    current_holdings: Optional[Dict[str, Any]] = None
    data_provider: str = "marketdb"
    lot_size: Optional[int] = None


class QuotesRequestModel(BaseModel):
    """Request payload for market quotes lookup."""
    symbols: List[str] = Field(..., description="List of ticker symbols")
    as_of_date: Optional[str] = Field(None, description="Valuation date (YYYY-MM-DD)")
    data_provider: Optional[str] = Field(None, description="Data provider name (e.g. marketdb, fuyao, yfinance, synthetic)")
    strategy_key: Optional[str] = Field(None, description="Strategy key filter for ticket price reference")
    universe_key: Optional[str] = Field(None, description="Universe key filter for ticket price reference")


class RunResponse(BaseModel):
    """Response payload for health check or live deployment run."""
    status: str
    message: str
    date: str
    strategy_key: Optional[str] = None
    strategy_name: Optional[str] = None
    universe_key: Optional[str] = None
    universe_name: Optional[str] = None
    stdout: str
    stderr: str
    health_report: Optional[Dict[str, Any]] = None
    trading_ticket: Optional[List[Dict[str, Any]]] = None
    account_state: Optional[Dict[str, Any]] = None
    holdings: Optional[Dict[str, Any]] = None
