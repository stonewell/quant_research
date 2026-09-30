# Live Trading Deployment Web Station (实盘量化交易与风控部署平台)

An institutional-grade, responsive live trading dashboard and execution workstation deployed under `web/livetrading`.
Features complete Stage 1 Health Gate inspection, Stage 2 live order generation (SELLS FIRST -> BUYS SECOND -> CASH SWEEP), custom universe management, interactive holdings editing, and daily historical snapshot archives.

---

## 🌟 Key Features

1. **Universe Configuration**:
   - Select from regional Core-Satellite presets (China A-Shares, US Equities, Hong Kong Stocks, Global ETFs).
   - Define custom universes in real-time and save them to your personal universe library (`custom_universes.json`).
2. **Strategy Selection**:
   - Browse and select from pre-configured strategy JSON dumps (e.g., `chan_four_state_blend`, `chan_risk_managed_blend`, etc.) scanned from configured strategy directories.
3. **Interactive Holdings & Cash Management**:
   - Add, edit, or remove held positions (shares or target weights).
   - Real-time auto-calculation of total equity valuation, remaining idle cash, and cash allocation percentage.
   - Persistent storage across sessions (`current_holdings.json`).
4. **Stage 1: Portfolio Health & Macro Gate**:
   - Evaluates peak-to-trough drawdown from High-Water Mark (HWM).
   - Evaluates Circuit Breaker Tiers (Normal, Tier 1 Damping, Tier 2 Defensive, Tier 3 Halt) and auto-healing cooldowns.
   - Calculates 50-day market breadth ($\ge 30\%$) and 10-day momentum thrust ($\ge 60\%$).
   - Computes Barroso 21-day realized volatility scaling factor ($\sigma = 12\%$).
   - Outputs visual directive badges: `🟢 GO`, `🟡 CAUTION`, `🔴 NO-GO`.
5. **Stage 2: Live Trading Ticket Generation**:
   - Translates target weights vs. current holdings into concrete trading orders.
   - Enforces execution hierarchy: **Phase 1: SELLS FIRST** (release purchasing power) $\rightarrow$ **Phase 2: BUYS SECOND** (exact lot-rounded entries) $\rightarrow$ **Phase 3: CASH SWEEP**.
   - Applies the 5% inertia turnover filter, single-stock caps, and market-specific lot sizing (100 for A-shares, 1 for US, board lots for HK).
   - One-click CSV ticket export for broker execution.
6. **Immutable Daily Snapshot Archive & Historical Date Browser**:
   - For every trading day, saves a complete snapshot bundle under `<archive_dir>/<date>/`:
     - `holdings.json`: Portfolio positions on that date.
     - `account_state.json`: Account NAV, peak NAV, drawdown, tier, and cooldown counters on that date.
     - `stage1_health_report.json`: Health gate metrics on that date.
     - `live_trading_ticket.json` & `live_trading_ticket.csv`: Orders generated on that date.
     - `summary.json`: Execution parameters, timing, and stdout logs.
   - Select any historical date to inspect that day's holdings, account state, and trading directives!
7. **Zero Hardcoded Paths**:
   - All directory locations (repo root, strategy directory, universe directory, data directory, archive directory, cache directory, and scripts) are completely configurable via CLI arguments, environment variables, `settings.json`, and the in-app Settings modal.
8. **Desktop & Mobile Responsive Design**:
   - Desktop ($\ge 1024\text{px}$): Dual-column high-density trading workstation.
   - Mobile ($< 1024\text{px}$): Touch-friendly segmented tabs, sticky bottom action bar, and responsive card views.

---

## 🚀 Quickstart

From inside `web/livetrading/`:
```bash
# 1. Install / sync dependencies (first time only)
uv sync

# 2. Start the web application (default: http://127.0.0.1:8080)
uv run python app.py
```

Or from the repository root:
```bash
uv run --project web/livetrading python web/livetrading/app.py --port 8080
```

Open your browser at `http://127.0.0.1:8080`.
Interactive OpenAPI Swagger docs are available at `http://127.0.0.1:8080/docs`.

---

## ⚙️ CLI Options & Path Configuration

All paths can be configured via CLI flags or environment variables:

| Argument | Environment Variable | Default | Description |
| :--- | :--- | :--- | :--- |
| `--port` | `PORT` | `8080` | Port for the HTTP server |
| `--host` | `HOST` | `127.0.0.1` | Network interface to bind |
| `--repo-root` | `LIVETRADING_REPO_ROOT` | Auto-detected | Root directory of the quantitative workspace |
| `--strategy-dir` | `LIVETRADING_STRATEGY_DIR` | `<repo_root>/pipeline/.../strategy_dumps` | Folder containing strategy JSON dumps |
| `--universe-dir` | `LIVETRADING_UNIVERSE_DIR` | `<repo_root>/docs/universe` | Folder containing pre-defined universe files |
| `--data-dir` | `LIVETRADING_DATA_DIR` | `web/livetrading/data` | Base directory for holdings and custom universes |
| `--archive-dir` | `LIVETRADING_ARCHIVE_DIR` | `<data_dir>/runs` | Directory for daily historical run snapshots |
| `--cache-dir` | `LIVETRADING_CACHE_DIR` | `<repo_root>/data` | DuckDB market data cache directory |
| `--stage1-script` | `LIVETRADING_STAGE1_SCRIPT` | `scripts/check_live_portfolio_health.py` | Stage 1 health runner |
| `--stage2-script` | `LIVETRADING_STAGE2_SCRIPT` | `scripts/run_live_four_state_blend.py` | Stage 2 live deployment runner |
| `--python-bin` | `LIVETRADING_PYTHON_BIN` | Active Python (`sys.executable`) | Python interpreter for running scripts |

---

## 📡 REST API Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/config` | Discovered strategies, universes, paths, and defaults |
| `GET` | `/api/settings` | Current path and runtime configuration |
| `POST` | `/api/settings` | Update path settings dynamically |
| `GET` | `/api/universes` | List pre-defined presets and custom-saved universes |
| `POST` | `/api/universes` | Create / save a custom named universe |
| `DELETE`| `/api/universes/{name}` | Delete a custom universe |
| `GET` | `/api/holdings?date=YYYY-MM-DD` | Retrieve latest or historical holdings snapshot |
| `POST` | `/api/holdings` | Save / update current holdings |
| `GET` | `/api/account-state?date=YYYY-MM-DD` | Retrieve latest or historical account state |
| `POST` | `/api/account-state` | Update account state |
| `POST` | `/api/run-health` | Execute Stage 1 Health Check and archive report |
| `POST` | `/api/run-deploy` | Execute Stage 2 Live Deployment and archive ticket |
| `GET` | `/api/runs` | List all historical daily run archives |
| `GET` | `/api/runs/{date}` | Retrieve full archive bundle for a specific date |
| `GET` | `/api/runs/{date}/csv` | Download generated trading ticket CSV |

---

## 🧪 Testing

To run the automated test suite offline:
```bash
uv run --project web/livetrading pytest web/livetrading/tests -q
```
