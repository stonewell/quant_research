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
    default_start_date: Optional[str] = Field("2024-01-01", description="Default start date for execution lookback")


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
    #: Explicit cash balance for the dividend-aware NAV engine (`valuation.compute_nav`).
    #: ``None`` (blank field) means "use the parked-cash holding's market value
    #: (BIL / 511880)"; an empty book with neither is a `MissingCashError`, never a zero.
    cash: Optional[float] = Field(None, description="Explicit cash balance; blank = use the BIL/511880 market value")
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
    holding_bars: Optional[Dict[str, int]] = None
    entry_prices: Optional[Dict[str, float]] = None
    last_exit_date: Optional[Dict[str, str]] = None
    exit_bars_ago: Optional[Dict[str, int]] = None
    entry_signal_streak: Optional[Dict[str, int]] = None
    zero_target_streak: Optional[Dict[str, int]] = None


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
    start_date: Optional[str] = None
    portfolio_value: float = 100000.0
    peak_nav: Optional[float] = None
    data_provider: Optional[str] = None
    allow_intraday: bool = Field(True, description="Allow running during intraday/lunch sessions using latest published session")
    refresh_data: bool = Field(True, description="Refresh market data before Stage 1 starts")


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
    start_date: Optional[str] = None
    portfolio_value: float = 100000.0
    #: Explicit cash balance for `valuation.compute_nav`. ``None`` -> the engine falls back
    #: to the parked-cash holding (BIL / 511880) and reports ``cash_source="bil_fallback"``.
    cash: Optional[float] = Field(None, description="现金余额；留空则用 BIL/511880 市值（cash_source=bil_fallback）")
    #: Human override: keep ``portfolio_value`` as the NAV instead of the computed mark.
    #: Logged at WARNING with the computed value and the drift; the UI highlights it.
    nav_override: bool = Field(False, description="人工覆盖净值：以 portfolio_value 作为 NAV（会记录 WARNING 与偏差）")
    current_holdings: Optional[Dict[str, Any]] = None
    #: (fix 1) The ONLY way to deploy an EMPTY book.  `{}` cannot be told apart from "the
    #: holdings table was never filled in", and an empty book makes the engine size a full
    #: liquidation (a SELL for every held symbol) -- so the service REFUSES it
    #: (`execution_service.EmptyBookConfirmationRequiredError`) unless this is `True`.
    #: The UI sets it only after the operator accepts the "treat the account as fully in
    #: cash" dialog; API/batch clients that genuinely want an all-cash book pass it too.
    confirm_empty_book: bool = Field(
        False,
        description=(
            "确认为“全部空仓”持仓簿：仅当持仓表为空且确实要按全现金部署时置 true"
            "（否则空簿会被拒绝，因为空簿会生成整仓卖出工单）"
        ),
    )
    #: (ITEM 1, 2026-10-30) the operator asserts the submitted holdings were reviewed against
    #: broker fills; never inferred.
    #:
    #: An UNEDITED book that is byte-identical to this same date's plan is never assumed to
    #: be actual holdings: `ExecutionService` refuses it with
    #: `UnconfirmedPlanAsInputError` unless this is `True`.  Rationale: the UI paints the
    #: round's planned post book into the table, so re-submitting the table unchanged asks
    #: the engine to size "held == target" -- which is zero orders, silently.  This gate is
    #: INDEPENDENT of `confirm_empty_book` (that one governs an EMPTY book, this one a
    #: plan-shaped non-empty book); neither implies the other.
    edited: bool = Field(
        False,
        description=(
            "已对照券商实际成交核实/编辑过提交的持仓簿（edited=true）；"
            "未标记且持仓与同日票面计划完全一致时会被拒绝"
        ),
    )
    data_provider: Optional[str] = None

    lot_size: Optional[int] = None
    guard_latency_bars: int = Field(2, description="UPLSM entry confirmation latency guard bars (default 2)")
    allow_intraday: bool = Field(True, description="Allow running during intraday/lunch sessions using latest published session")


# Harmonized alias for Stage 2 live strategy request
RunStrategyRequest = RunDeployRequest


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
    #: (NAV) Dividend-aware mark from `valuation.compute_nav`, and where it came from.
    #: ``nav_source``: ``"computed"`` (engine mark, the normal path), ``"override"``
    #: (operator pinned `portfolio_value`) or ``"fallback_input"`` (engine failed and
    #: `LIVETRADING_ALLOW_NAV_FALLBACK=1` degraded to the form value).
    #: MUST be declared here: FastAPI's `response_model` filters undeclared keys out, so
    #: the UI would silently lose the provenance badge otherwise.
    nav_computed: Optional[float] = None
    nav_effective: Optional[float] = None
    nav_source: Optional[str] = None
    cash: Optional[float] = None
    cash_source: Optional[str] = None
    nav_warnings: Optional[List[str]] = None
    #: Provenance of the position-open dates behind the total-return ratio:
    #: ``"run_archive"`` when derived from archived books, else ``None`` (those symbols
    #: were marked at RAW -- a lower bound that excludes dividends since entry).
    anchor_source: Optional[str] = None
    #: PLANNED post-ticket book derived from the ticket's `target_shares`
    #: (`post_holdings_from_ticket`). The UI refreshes the holdings table from this so a
    #: page left open across days cannot re-submit the PRE-trade book (defect 2026-10-27).
    #: MUST be declared here: FastAPI's `response_model` filters undeclared keys out.
    holdings_plan: Optional[Dict[str, Any]] = None
    #: Advisory reconciliation of the submitted input book vs the previous round's planned
    #: post-ticket book (`storage.diff_holdings_input_vs_prev_post`). Never a fill receipt.
    holdings_input_check: Optional[Dict[str, Any]] = None


class SubStrategyConfigModel(BaseModel):
    """Configuration for a single sub-strategy in a collaborative portfolio."""
    strategy_key: str
    capital_weight: float = 1.0
    name: Optional[str] = None
    universe_file: Optional[str] = None


class MultiStrategyDeployRequest(BaseModel):
    """Request payload for multi-strategy order netting deployment."""
    matrix_id: str = "tri_core_blend"
    matrix_name: Optional[str] = None
    strategies: Optional[List[SubStrategyConfigModel]] = None
    universe_file: Optional[str] = None
    custom_symbols: Optional[List[str]] = None
    #: (P4) The selection this matrix round is run UNDER, recorded in the matrix
    #: `summary.json` so `list_runs` can report the round under its real universe instead
    #: of the literal `multi_matrix` (which no selection can match).  RETRIEVAL METADATA
    #: ONLY: it is never an input channel into universe resolution -- `_resolve_universe`
    #: keeps taking `universe_file` / `custom_symbols`, in that order.
    universe_key: Optional[str] = None
    universe_name: Optional[str] = None
    as_of_date: Optional[str] = None
    start_date: Optional[str] = None
    portfolio_value: float = 100000.0
    current_holdings: Optional[Dict[str, Any]] = None
    #: (fix 1) Same contract as `RunDeployRequest.confirm_empty_book`: an empty/all-zero
    #: matrix book would be turned into `{"BIL": portfolio_value}` and liquidate every
    #: existing position, so it is refused unless the caller confirms an all-cash account.
    confirm_empty_book: bool = Field(
        False,
        description=(
            "确认为“全部空仓”矩阵持仓簿：仅当持仓表为空且确实要按全现金部署时置 true"
            "（否则空簿会被拒绝，因为空簿会生成整仓卖出工单）"
        ),
    )
    #: (ITEM 1, 2026-10-30) Same contract as `RunDeployRequest.edited`: the operator asserts
    #: the submitted holdings were reviewed against broker fills; never inferred.  A matrix
    #: book identical to this date's already-completed netted plan is refused with
    #: `UnconfirmedPlanAsInputError` unless this is `True`.
    edited: bool = Field(
        False,
        description=(
            "已对照券商实际成交核实/编辑过提交的矩阵持仓簿（edited=true）；"
            "未标记且持仓与同日净额化票面计划完全一致时会被拒绝"
        ),
    )
    data_provider: Optional[str] = None
    min_holding_bars: int = 2
    inertia_threshold: float = 0.05
    allow_intraday: bool = Field(True, description="Allow running during intraday/lunch sessions using latest published session")

