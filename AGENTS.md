# AGENTS.md

Instructions for any coding agent (Claude Code, Codex, Cursor, etc.) working in this repository.
This file is deliberately shorter than `README.md` and the per-project `README.md`/`SCHEMAS.md`
files -- it captures conventions and gotchas an agent needs to not break things, not the full CLI
reference. Read `README.md` first for the end-to-end pipeline picture and data-flow diagram.

## What this repo is

A modular quantitative trading research/backtesting workspace, organized into two project groups
plus shared infrastructure at the repo root:
- **`pipeline/`** -- the core research/backtest pipeline family, all sharing one `uv` environment:
  `research_strategy` (factor research) -> `instrument_selection` (universe screening) ->
  `pattern_mining` (optional turning-point mining) -> `strategy_generator` (allocation strategy
  search), plus `fundamental_screener` (real-fundamentals buy/sell screening, not wired into the
  pipeline). `pipeline/run_pipeline.py` chains the first 4 stages plus `backtester` via subprocess.
- **`ml/`** -- ML/DL-based strategy projects, each with its OWN isolated `uv` environment (heavier,
  version-sensitive dependency stacks that would conflict with `pipeline/`'s): currently
  `bnn_forecaster` (AutoBNN probabilistic forecasting) and `alpha_mining` (two-stage GP + LightGBM
  cross-sectional alpha mining, `ML1`).
- `common/` and `backtester/` stay at the repo root, shared by both groups, with **no
  `pyproject.toml`/venv of their own** -- reached via `sys.path` injection from whichever group's
  venv is running them (see below).

See `README.md`'s "Workspace Architecture & Data Flow" section for the diagram and each stage's I/O
schema.

## Setup & running things

```bash
cd pipeline && uv sync                             # pipeline/'s shared env (research_strategy,
                                                    # instrument_selection, pattern_mining,
                                                    # strategy_generator, fundamental_screener)
cd ml/bnn_forecaster && uv sync                    # bnn_forecaster's own isolated env
cd ml/alpha_mining && uv sync                      # alpha_mining's own isolated env (lightgbm)
cd web/livetrading && uv sync                      # web/livetrading's own isolated env (fastapi, pydantic)

# from inside pipeline/ (or via pipeline/.venv/Scripts/python.exe from anywhere):
uv run python <project>/run_*.py --help            # every pipeline project's CLI
uv run python run_pipeline.py --data-provider synthetic --universe SPY QQQ ...
```

`common/` and `backtester/` have no venv of their own -- run them with WHICHEVER group's venv is
appropriate for the strategy you're touching. Both groups' venvs can run `common`'s and
`backtester`'s own full test suites (verified directly, not just assumed).

On Windows, a venv's interpreter is at `<group>/.venv/Scripts/python.exe` if `uv run` isn't
available in the current shell.

## Testing policy -- read before running or writing tests

- **Never use real market data or network access in tests or example commands.** Every test suite
  runs 100% offline via `SyntheticDataProvider`/`common/testing.py`'s synthetic OHLCV generators.
  Default any CLI example you write to `--data-provider synthetic`; only use `yfinance` when a human
  explicitly asks for a real-data run.
- Each project has its own `tests/` directory (`common/tests`, `pipeline/research_strategy/tests`,
  `pipeline/instrument_selection/tests`, `pipeline/pattern_mining/tests`,
  `pipeline/strategy_generator/tests`, `pipeline/fundamental_screener/tests`, `pipeline/tests`,
  `backtester/tests`, `ml/bnn_forecaster/tests`, `ml/alpha_mining/tests`, `web/livetrading/tests`). Note: `web/livetrading`
  is a third isolated `uv` environment (`web/livetrading/.venv`); its tests must run with its own
  interpreter, not `pipeline/.venv` (missing `fastapi`/`pydantic`). None has an `__init__.py`, and several projects
  share identically-named test files (`test_allocation_templates.py`, `test_indicators.py`, ...), so
  a bare `pytest` from the repo root -- or `pytest` given more than one of these directories at
  once -- fails with `import file mismatch`. Two ways around this:
  - Run one directory at a time (what the per-project READMEs show): `pytest common/tests -q`.
  - Or pass `--import-mode=importlib` to run several/all directories together in one invocation:
    `pytest common/tests pipeline/strategy_generator/tests backtester ... -q --import-mode=importlib`.

## Core domain model -- must-know before touching template/strategy code

- Every strategy/allocation template is an `AllocationTemplate` subclass (`common/allocation_templates.py`)
  implementing `generate_weights(universe, params) -> DataFrame`, `explain_weights(params) -> str`,
  and `warmup_bars(params) -> int`. Two families implement this interface: the static, zero-arg
  templates in `common/allocation_templates.py`, and the 53 richer, `StrategyConfig`-driven
  templates (basket presets, single-asset timing strategies, Chan structural models, macro factor
  compounders, multi-strategy alpha books, WorldQuant formulaic alpha models, and modular blend
  allocation strategies in `pipeline/research/blend/`) in `pipeline/research_strategy/rs/`.

- **Sparse weights contract (critical):** `generate_weights` returns a DataFrame indexed by date
  where a row is `NaN` on every day EXCEPT an actual rebalance date, where it holds the real target
  weight. Templates must **never forward-fill their own output** -- the backtester
  (`common/allocation_backtester.py`) tells "a rebalance was instructed" apart from "nothing
  happened today" by whether the row is present at all, not by whether its value changed from the
  prior row (a template can legitimately recompute an identical target on consecutive rebalances).

- **Cell-level NaN vs. 0.0 (critical -- caused two real bugs in this session's aspect-composition
  work, both fixed):** `run_allocation_backtest` does `sparse_weights.ffill().fillna(0.0)`
  **column-wise across the whole frame**. Inside an actual rebalance row, a `NaN` cell for one
  symbol does **not** mean "zero" -- it carries forward that symbol's last non-NaN value from a
  *previous* rebalance. If a template's own logic decides to exclude or de-risk a symbol it
  previously held, it **must write an explicit `0.0`** for that symbol at that rebalance date, never
  leave it `NaN`, or the backtester will silently keep holding the stale prior position.

- **Equivalent Random Search (ERS)** (`common/allocation_search.py`): the winning template/params is
  validated by comparing its Sharpe against N random-weight portfolios; it's only `trusted` if it
  beats a percentile threshold *and* has enough rebalances. Run ERS **once per search**, on the
  already-chosen winner -- never once per candidate template (that would burn the random-draw budget
  N times over and change what "beating random chance" means).

- **Factor taxonomy** (`common/factor_taxonomy.py`): tags like `relative_momentum`,
  `mean_reversion`, `volatility_targeting` on templates/strategies feed `strategy_generator`'s
  optional factor-report tie-break -- it can only resolve a near-tie between top candidates, never
  override a template that clearly won on backtested Sharpe.

- **Aspect composition** (added recently -- `common/strategy_aspects.py` for basket templates,
  `pipeline/research_strategy/rs/timing_aspects.py` for single-asset timing templates): `strategy_generator`
  no longer just picks the single best whole template. It decomposes several templates into
  reusable pieces -- *selection* (which symbols/how much to invest) + *weighting* (how to size
  across them) for the static templates; *entry signal* + *exit/risk/sizing* for 4 of the 18
  timing templates -- and searches hybrid pairings across DIFFERENT source templates too (e.g.
  momentum's stock-picking + inverse-volatility's sizing), on by default via
  `GeneratorConfig.enable_aspect_composition` / CLI `--no-compose-aspects`. A winning hybrid embeds a
  `composite_spec` block in `strategy.json` so `backtester` can reconstruct the exact instance. Read
  both modules' docstrings before extending this -- the NaN-vs-0.0 rule above is exactly what bit
  the first version of this feature.

- **Modular Blend Allocation Strategies** (`pipeline/research/blend/`): Institutional multi-strategy
  blend ensembles built on `BaseBlendAllocationStrategy` (`pipeline/research/blend/blend_base.py`),
  including `ChanRiskManagedBlendStrategy`, `ChanFourStateBlendStrategy`, `ChanCrisisShieldBlendStrategy`,
  `ChanDualHybridBlendStrategy`, and `GenericBlendStrategy`. Standardizes dynamic regime-adaptive
  weighting, Garman-Klass volatility targeting, multi-tier drawdown circuit breakers with auto-healing
  cooldowns, adaptive inertia rebalancing (`apply_asset_inertia`), sector throttling, and breadth thrust
  cash deployment. Defaults standardized across `StrategyConfig` and `strategies_config.json` to
  `blend_weighting_mode="rolling_ic_ir"` (rolling 60d IC-IR Bayesian adaptive weighting with Harvey-Liu-Zhu
  $t \ge 2.0$ significance hurdle) and `blend_enable_orthogonalization=true` (Löwdin symmetric
  orthogonalization $W^* = W(W^T W)^{-1/2}$ removing sub-strategy collinearity), validated by walkforward
  experiments (`blend_6` through `blend_13`) yielding up to 37.35% CAGR, 1.702 Sharpe, and 0.9400 DSR.
  **Documented EXCEPTION (2026-10-22):** `chan_four_state_blend` deliberately runs
  `blend_weighting_mode="regime_ic_ir_hybrid"` (the mode its live deployment uses), pinned in
  `strategies_config.json` and asserted by `test_cfsb_deployed_regime_mode_and_inertia_ratio_config`.
  Do NOT "normalize" it back to `rolling_ic_ir` -- a `--dump-strategies` pass did exactly that and
  silently switched the deployed engine, which also made the regime-branch defenses (bucket
  delta-clamp / thrust hysteresis) inert. See `docs/version.md` §8.
  The six Track-T intraday-5m FEATURE switches (`blend_enable_intraday_5m`,
  `blend_enable_5m_interval_nesting`, `blend_use_intraday_downside_vol`, `blend_enable_5m_stop_filter`,
  `blend_intraday_upvol_thresh`, `blend_intraday_cutoff_time`) were DELETED on 2026-11-17 as a
  negative result (the Phase-6 ablation showed interval nesting identical to baseline and the other
  variants degrading Sharpe 0.210 → 0.135, ERS untrusted); see `docs/version.md` §47 — do not
  reintroduce them as a default-`False` flag.

- **Blend sub-strategy hookup & the bare-name `resolve_param` collision (critical -- caused a real
  bug, fixed 2026-10-21):** each blend strategy's `get_sub_strategies(universe, params)` builds its
  sub-strategy instances by resolving selection params through `BaseBlendAllocationStrategy.resolve_param`.
  `resolve_param`'s final fallback is a bare-name `getattr(cfg, name)`, so any param name that collides
  with a *global* `StrategyConfig` field (and has no `blend_<name>` in between) silently leaks that
  global's value. The concrete casualty was `wq_alpha_id`: `chan_four_state_blend` and
  `chan_risk_managed_blend` resolved `resolve_param(p, "wq_alpha_id", ...)`, which leaked the global
  `StrategyConfig.wq_alpha_id=101` (the standalone `WorldQuantAlphaStrategy` default) and silently
  swapped `ChanFourStateExecutionStrategy` / `ChanCompositeStrategy` for `WorldQuantAlphaStrategy(#101)`.
  **Rule: blend `get_sub_strategies` must resolve sub-strategy-selection params via the
  strategy-PREFIXED key (`p.get("cfsb_wq_alpha_id", ...)` / `p.get("crb_wq_alpha_id", ...)`), never a
  bare name that collides with a global config field.** The prefixed selection params
  (`cfsb_third_strategy_type`, `crb_third_strategy_type`) are also declared explicitly in
  `strategies_config.json`. See `docs/version.md` §7 for the full audit and regression test
  `test_blend_sub_strategy_class_resolution_regression`.

- **Live Trading Deployment & Web Station** (`web/livetrading/`, `scripts/`):
  Live operations follow a two-stage protocol:
  - **Stage 1 (Macro Health Gate)** via `scripts/check_live_portfolio_health.py`: Evaluates
    peak-to-trough drawdown from High-Water Mark (HWM), multi-tier circuit breaker triggers, market
    breadth ($\ge 30\%$), and momentum thrust ($\ge 60\%$).
  - **Stage 2 (Order Ticket Generation)** via `scripts/run_live_strategy.py`: Enforces execution hierarchy: Phase 1: SELLS FIRST
    (releases purchasing power) $\rightarrow$ Phase 2: BUYS SECOND (lot-rounded entries) $\rightarrow$ Phase 3:
    POSITIONS HELD (inertia filtering) $\rightarrow$ Phase 4: CASH SWEEP (idle cash into `BIL`/`511880`/`GC001`).
    Supports all 55+ research strategies, the static allocation templates, and custom strategy files via `--strategy`.
  - `web/livetrading` provides an interactive web UI with real-time portfolio management, daily immutable
    snapshot archiving (`data/runs/<date>/`), and custom universe editing. Regional core-satellite
    pools reside in `docs/universe/` (China A-Shares, US equities, Hong Kong stocks).

## Core quantitative engineering rules

> Rule numbers are cited externally across code comments and `docs/` — **never renumber**. Rule 6 is retired; the gap is intentional.

1. **100% Offline Testing**:
   See **Testing policy** above — that section is the single source for this rule.
2. **Single Source of Truth (SSOT)**:
   - All quantitative engineering task breakdowns, status tracking, priority queues, and
     execution roadmaps must be centralized exclusively in `docs/development_plan.md` (in Chinese).
   - Module specification and architecture docs in `docs/` must link to `docs/development_plan.md`
     for task lists and status.
   - Before registering new tasks or proposing enhancements in the SSOT, agents must first
     cross-reference all finished empirical task results and ablation closures (see Rule 8).
3. **Sparse Weights Contract & Cell-level NaN vs. 0.0**:
   See *Sparse weights contract (critical)* and *Cell-level NaN vs. 0.0 (critical)* under
   **Core domain model** above.
4. **Single Equivalent Random Search (ERS)**:
   See *Equivalent Random Search (ERS)* under **Core domain model** above.
5. **Universe Asset Removal & Blacklist Documentation (SSOT in `docs/black_list.md`)**:
   - Anytime an agent suggests or executes removing a stock, ETF, or asset from any universe/portfolio (whether due to high unit price, structural loss, negative Alpha, fraud/ST risk, cyclical price wars, or friction bleed), the agent must record the asset symbol, name, removal date, removal trigger/category, and detailed quantitative/fundamental rationale in `docs/black_list.md` (in Chinese) for future reference and persistent memory.
7. **Strategy Modification & Semantic Version Upgrade (Dual-Axis Decoupling)**:
   - Every quantitative strategy and allocation template must strictly track its semantic version. Note the **exact** attribute layout -- do not "normalize" it:
     - `BASE_VERSION` exists on **exactly one class**: `BaseBlendAllocationStrategy` (`pipeline/research/blend/blend_base.py`).
     - `common/allocation_templates.py` (`AllocationTemplate` and the static templates) has **no file-level `BASE_VERSION`, and never had one** (verified: `grep -n BASE_VERSION common/allocation_templates.py` -> no match; `git log -S BASE_VERSION -- common/allocation_templates.py` -> empty). Do not go hunting for it, and do not invent one "for symmetry".
     - Every strategy/template class carries its own per-class `version`.
   - **Mandatory version upgrade on modification**:
     - **Base Engine Upgrade (`BASE_VERSION`)**: Anytime an agent modifies, extends, or adds features/operators/risk overlays to `BaseBlendAllocationStrategy` (`pipeline/research/blend/blend_base.py`), the agent **must increment `BASE_VERSION`** (e.g. `1.0.0` -> `1.1.0` for backward-compatible additions, `1.1.0` -> `2.0.0` for breaking contract changes).
     - **Template / Strategy Class Upgrade (`version`)**: Anytime an agent modifies `common/allocation_templates.py` (the `AllocationTemplate` base machinery or one of its static templates) or a specific strategy class's logic, signal calculation, parameter defaults, or sub-strategy composition (e.g. in `common/allocation_templates.py`, `pipeline/research/blend/*.py`, `pipeline/research_strategy/rs/*.py`), the agent **must increment that class's own `version`** (e.g. `1.0.0` -> `1.1.0`). There is no file-level base version to bump in that file -- the per-class `version` **is** the whole obligation.
     - **Synchronized updates**: When incrementing versions, the agent must synchronously:
       1. Update the unit test assertions in `pipeline/research_strategy/tests/test_blend_strategy_versioning.py` and `common/tests/test_strategy_versioning.py`.
       2. Re-dump strategy configuration files via `uv run python pipeline/research_strategy/run_research_strategy.py --dump-strategies` so persisted `strategy_dumps/*.json` reflect the new version.
       3. Ensure live trading tickets generated by `run_live_strategy.py` carry the updated `strategy_version` and `base_version`.
       4. **Version changelog persistence (SSOT in `docs/version.md`)**: Anytime a strategy version (`version`) or base engine version (`BASE_VERSION`) is upgraded, the agent must synchronously append a detailed change log and summary in Chinese to `docs/version.md` (recording modification date, strategy/base name, old $\to$ new version, exact quantitative/signal/risk logic modifications, parameter changes, and verification test status).
8. **Mandatory Cross-Reference Against Finished Tasks Before New Tasks or Enhancement Suggestions**:
   - Before registering any new task in `docs/development_plan.md` or proposing any strategy or
     architecture enhancement, agents **must first search, compare, and cross-reference all
     finished task results, ablation metrics, and negative-result closures (`[x]`)** across
     `docs/development_plan.md`, `docs/development_plan_archive.md`, and `docs/version.md`
     (e.g., `LV8`/`LV20` empty reversal-bucket negative closure, `C4.1`/`C5.2` zero-trade
     starvation lessons, `M1`–`M4` completed primitives, `L2.*` post-Sequencer & double-counting
     guardrails).
   - Never re-propose enhancements that were empirically refuted or closed as negative results,
     never duplicate primitives that have already landed, and explicitly cite the relevant
     finished task IDs when justifying net-new proposals.
9. **Empirical Positive-Value Gate Before Strategy or Trading Code Changes**:
   - Before implementing or merging any code change related to strategies, signals, alpha
     factors, portfolio construction, risk controls, or live trading execution, agents **must
     empirically verify** (via offline backtests, walkforward/ablation comparisons, or
     quantitative diagnostics against the baseline) that the change produces **positive or
     strictly non-degrading results** for strategy performance, risk-adjusted return, or trading
     execution quality.
   - Pure refactors or bugfixes meant to preserve baseline behavior must verify zero unintended
     signal or weight drift; strategy/trading enhancements that fail to demonstrate positive
     empirical value (or introduce degradation, zero-trade starvation, or double-counting risk)
     **must not be merged into production paths** unless explicitly requested as opt-in
     disabled-by-default experimental flags.
10. **Discriminating-Power Gate for A/B, Ablation, and Differential Experiments**:
    - A comparative experiment (A/B, ablation, arm-vs-arm replay) may be used as evidence about
      the defect under investigation **only if it first demonstrates discriminating power on that
      defect**: the report must name at least one probe (a value, row, counter, event, or weight
      cell) that differs between the arms *because of the behaviour being studied*, and quote its
      value on both sides.
    - If no artifact can observe the quantity in question — the ledger records only executed
      orders, the decision log was never written for those runs, the metric aggregates the effect
      away, or the two arms come out byte-identical — the experiment must **not** be run and its
      result must **not** be reported as acceptance, rejection, or closure. Build the observability
      probe first (targeted instrumentation, stage-by-stage trace, or a new recorded field), then
      decide.
    - Scope the experiment to the defect's own population and window. Measure the specific symbols,
      dates, and trades whose behaviour actually differs, and report that isolated leg or cohort
      before anything else. A general sweep across every strategy, universe, or fold is at most a
      secondary sanity check; it must never be the primary evidence, because unrelated trades
      dominate the aggregate and can reverse the sign of the effect under study.
    - If the affected window falls outside the evaluation window, or the affected population is a
      subset that the aggregate metric averages away, the broad run cannot adjudicate the question:
      say so explicitly and build the isolated measurement instead of reporting a global verdict.
    - A zero-delta A/B proves the experiment cannot see the change. It never proves the change is
      harmless, inert by design, or that the defect is absent.
    - Performance verdicts and correctness verdicts are separate. A `FAIL` / `ΔSharpe` result
      judges the **intervention**, never the **diagnosis**; conversely, a reproduced mechanism
      probe never establishes that the intervention is worth shipping.
    - The write-up must record: the probe, its value with and without the change, and why that
      quantity is the defect itself rather than a proxy for it.
11. **Backtest/Live Parity by Default**:
    - When the live engine supplies an input to a shared kernel (market breadth/thrust, volumes,
      prices, calendar, risk state), the backtest path must supply the same class of input **by
      default**. A backtest that structurally cannot exercise a live decision path is a defect to
      register and fix, not a baseline to compare against.
    - Divergence must be **opt-in and recorded**: the flag that restores the divergent behaviour
      is spelled `--no-...` / `...=False`, it is documented next to the parity note, and any
      historical result produced under it must be labelled with the differing input set.
    - A new input to a shared kernel lands with **both** paths wired in the same change, plus a
      test that the two paths agree on a synthetic fixture. Shipping only one side is a review
      blocker.
    - When genuine non-parity is unavoidable (e.g. a live-only data source), record the gap, the
      measured direction of the bias, and the pending task that closes it.



## Documentation & coding style conventions

This codebase is unusually disclosure-heavy -- match it, don't strip it out:

- Non-trivial functions/classes that port a known strategy or technique cite their academic
  grounding (author, year, venue) and explicitly disclose any simplification versus the original
  (e.g. "HONEST CAVEAT", "DISCLOSED APPROXIMATION" comments).
- Comments explain *why*, often naming a specific prior bug the current code shape fixes -- don't
  delete these as "obvious" when touching nearby code; they're load-bearing history, not clutter.
- Prefer reusing existing shared primitives (`common/indicators.py`, `common/allocation_search.py`,
  `common/covariance.py`, `common/scheduling.py`, `common/testing.py`) over reimplementing math that
  already exists there.
- Git commit messages in this repo are terse, lowercase, present-tense summaries (e.g. "add chan
  pivot", "add denoise", "pattern mining merge with current strategy gen instead of separated
  pipeline").
- **No single-line walls of text (applies to every document you write or edit):**
  documentation files (`AGENTS.md`, `README.md`, everything under `docs/`,
  per-project `README.md`/`SCHEMAS.md`, and long code-block-adjacent docstrings) must never
  be one giant unwrapped line per paragraph. Use markdown structure instead -- wrap prose at
  roughly 100 columns like the rest of this file does, one sentence or clause per line where
  it helps, blank lines between paragraphs, `-` bullets and nested sub-bullets for
  enumerations, numbered lists for ordered steps, `|` tables for multi-attribute comparisons,
  and fenced ```` ``` ```` blocks for anything code-like. The goal is that a reader (or diff
  review) sees several short, scannable lines rather than one horizontally-scrolling line.

## Known pre-existing issues & data provider gotchas

- `HierarchicalRiskParityAllocation` can raise `IndexError` on some small/degenerate synthetic
  universes. It's already caught and degraded to `-inf` Sharpe by `_portfolio_score`, so it doesn't
  break a run -- that candidate just loses -- but you may see the warning in output.
- **BaoStock `ResultData` pagination OOM & sequential-only downloading (`common/financial_api.py`):**
  - **OOM & Page-1 loss root cause:** In `baostock/data/resultset.py`, `ResultData.get_data()` calls
    `df.append(...)` when a query spans multiple pages (`> 10,000` rows), which raises `AttributeError`
    in Pandas 2.x *after* `self.next()` has already overwritten Page 1 in `rs.data` with Page 2.
    Furthermore, `ResultData.next()` checks `if self.cur_row_num < len(self.data): return True` before
    requesting the next socket page. If a caller extracts rows in bulk via `data_list.extend(rs.data)`
    inside `while rs.next():` without advancing `rs.cur_row_num = len(rs.data)` after each page,
    `rs.cur_row_num` stays `0` forever and `while rs.next():` becomes an infinite tight loop that
    exhausts all system RAM (OOM) in seconds.
  - **Mandatory solution:** Never call `rs.get_data()` on real `baostock.data.resultset.ResultData`
    instances in Pandas 2.x, and never loop `while rs.next(): data_list.extend(rs.data)` without
    advancing the cursor. Always advance `rs.cur_row_num = len(rs.data)` immediately after
    `data_list.extend(rs.data)` on both the initial page and inside every `while rs.next():` iteration
    (or advance row-by-row via `rs.get_row_data()`), and keep intraday queries chunked (`<= 180`
    calendar days per request).
  - **No parallel/multiprocessing downloaders:** Never use `multiprocessing` or multi-threaded
    parallel sessions with BaoStock (`baostock.com` blocks concurrent socket floods from the same IP).
    Always download sequentially in a single process using a persistent session (`keep_logged_in=True`)
    and persist each symbol incrementally to `DuckDBCache`.

