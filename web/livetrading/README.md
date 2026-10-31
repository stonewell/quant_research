[ English | [简体中文](README_ZH.md) ]

# Live Trading Deployment Web Station (实盘量化交易与风控部署平台)

An institutional-grade, responsive live trading dashboard and execution workstation deployed under `web/livetrading`.
Features complete Stage 1 Health Gate inspection, Stage 2 live order generation (SELLS FIRST -> BUYS SECOND -> POSITIONS HELD -> CASH SWEEP), custom universe management, interactive holdings editing, and daily historical snapshot archives.

---

## 🌟 Key Features

1. **Universe Configuration**:
   - Select from regional Core-Satellite presets (China A-Shares, US Equities, Hong Kong Stocks, Global ETFs).
   - Define custom universes in real-time and save them to your personal universe library (`custom_universes.json`).
2. **Strategy Selection**:
   - Browse and select from pre-configured strategy JSON dumps (e.g., `chan_four_state_blend`, `chan_risk_managed_blend`, `chan_crisis_shield_blend`, `chan_dual_hybrid_blend`, `generic_blend`, etc.) scanned from configured strategy directories.
   - Defaults automatically to the production-validated champion: **Chan Four-State Risk-Managed Blend Strategy** (`chan_four_state_blend`) featuring 60-day Rolling IC-IR adaptive weighting ($t \ge 2.0$), Löwdin symmetric orthogonalization ($W^* = W(W^T W)^{-1/2}$), Garman-Klass extreme-value volatility targeting, and Chan FSM `WAIT_OBSERVE` consolidation avoidance.
3. **Interactive Holdings & Cash Management**:
   - Add, edit, or remove held positions (shares or target weights).
   - Real-time auto-calculation of total equity valuation, remaining idle cash, and cash allocation percentage.
   - The book is persisted **only** inside the run archive (see item 6 for the seed precedence). There is no global holdings file any more.
   - **A manual save also records that a human asserted the book.** `POST /api/holdings` (a save with no pinned run directory) writes `operator_holdings.json` next to the byte-identical `holdings.json`: `{"saved_at", "source": "operator_manual_correction", "holdings"}`. It is the only record of a **partial fill** (this system receives no broker fill receipt), so it outranks the round's plan in both the displayed book and the deploy seed. A deploy always pins its run directory and therefore never writes this file.
4. **Stage 1: Portfolio Health & Macro Gate**:
   - Evaluates peak-to-trough drawdown from High-Water Mark (HWM).
   - Evaluates Circuit Breaker Tiers (Normal, Tier 1 Damping, Tier 2 Defensive, Tier 3 Halt) and auto-healing cooldowns across all blend variants (`cfsb_`, `crb_`, `ccsb_`, `cdhb_`, `blend_`).
   - Calculates 50-day market breadth ($\ge 30\%$) and 10-day momentum thrust ($\ge 60\%$).
   - Computes Barroso 21-day realized volatility scaling factor ($\sigma = 12\%$) via Garman-Klass (1980) OHLC extreme-value estimator (with Close-to-Close fallback).
   - Outputs visual directive badges: `🟢 GO`, `🟡 CAUTION`, `🔴 NO-GO`.
5. **Stage 2: Live Trading Ticket Generation**:
   - Translates target weights vs. current holdings into concrete trading orders.
   - Enforces execution hierarchy: **Phase 1: SELLS FIRST** (release purchasing power) $\rightarrow$ **Phase 2: BUYS SECOND** (exact lot-rounded entries) $\rightarrow$ **Phase 3: POSITIONS HELD** (inertia-filtered holds) $\rightarrow$ **Phase 4: CASH SWEEP** (idle cash into `BIL`/`511880`/`GC001`).
   - Applies the 5% inertia turnover filter, single-stock caps, and market-specific lot sizing (100 for A-shares, 1 for US, board lots for HK).
   - One-click CSV ticket export for broker execution.
6. **Immutable Daily Snapshot Archive & Historical Date Browser**:
   - For every trading day, saves a complete snapshot bundle under `<archive_dir>/<strategy>/<universe>/<date>/` (plus `<date>/<HHMMSS>/` when a session runs more than one round):
     - `holdings.json`: Portfolio positions on that date.
     - `operator_holdings.json`: Present **only** when an operator saved the book by hand for that session (see item 3); the human-asserted book, `{"saved_at", "source", "holdings"}`.
     - `account_state.json`: Account NAV, peak NAV, drawdown, tier, and cooldown counters on that date.
     - `stage1_health_report.json`: Health gate metrics on that date.
     - `live_trading_ticket.json` & `live_trading_ticket.csv`: Orders generated on that date.
     - `post_holdings.json`: The planned post-trade book assumed by that round's ticket.
     - `summary.json`: Execution parameters, timing, and stdout logs.
   - **Holdings and account state are persisted ONLY here.** When the request carries no explicit book (the normal UI flow sends `current_holdings: null`), a deploy seeds the book in this order: an operator's manual correction for the session (`operator_holdings.json`) → the round's planned post book (`post_holdings.json`) → a session snapshot in a directory that never ran Stage 2 (`holdings.json` / `current_holdings.json`) → the previous round's plan; when none exists it refuses to size a ticket — sizing from an empty book would re-buy lots that were already sold.
   - **An empty submitted book is refused, not assumed to be "all cash".** A book that is `{}`, all-zero, or whose every row has no usable share count is rejected (`EmptyBookConfirmationRequiredError`), because the engine would size a SELL for every currently-held symbol — a full liquidation. An intentional all-cash account is expressed per request with `confirm_empty_book: true` (the web UI shows a confirmation dialog first); the round is then archived with `holdings_seed_source = "operator_confirmed_empty_book"`, so an auditor can tell it apart from an operator-entered position list (`operator_input`). There is deliberately no environment kill-switch for this gate (unlike `LIVETRADING_STRICT_SEED_BOOK`), because "the form was not filled in" is never a legitimate all-cash instruction.
   - **A submitted book identical to the same date's plan is refused unless the caller says it was reviewed.** `edited: true` (accepted by both `POST /api/run-deploy` and `POST /api/deploy/multi`) asserts that the submitted book was checked against **broker fills**; it is never inferred. Without it, a **non-empty** book equal to that date's already-completed plan raises `UnconfirmedPlanAsInputError` before any side effect — because the UI paints the plan into the table, so re-submitting it makes every target equal the current position and yields a **zero-order** ticket archived as a success. The gate never applies to a date with no plan (a brand-new session), and it is **independent** of `confirm_empty_book`: that one licenses an empty book (a full liquidation), this one a plan-shaped non-empty one; neither authorizes the other. The web UI sends `current_holdings: null` (key kept) and omits `edited` unless the operator actually edited the table.
   - **State labels (auditor-facing, closed sets).** `bundle["holdings_source"]` (what the UI displays) is exactly one of: `operator_manual_correction` → `post_trade_book` → `snapshot` → `pre_trade_fallback` (rank order; the last means "Stage-2 artifact present but no resolvable plan", i.e. the raw pre-trade snapshot is shown, labelled). `summary.json["holdings_seed_source"]` (what the deploy was sized from) is exactly one of: `operator_input`, `operator_confirmed_empty_book`, `operator_manual_correction`, `operator_snapshot`, `previous_post_holdings`, `legacy_get_holdings_fallback`, `none`.
   - The web `data/` directory holds **configuration only** (`settings.json`, `settings.local.json`, `custom_universes.json`, `multi_strategy_configs.json`); it never stores holdings or account state.
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
| `--stage2-script` | `LIVETRADING_STAGE2_SCRIPT` | `scripts/run_live_strategy.py` | Stage 2 universal live deployment runner |
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
