# 回测研究文档（`docs/backtest_research.md`）设计精炼与回测产物自包含落盘实施方案

> [!NOTE]
> **单一事实来源 (SSOT)**：所有量化工程任务拆解、状态追踪与执行规划集中记录于 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md)（依 `AGENTS.md` Rule 2 规范）。本文档作为回测研究规范与落地契约的专门技术实施方案。

## 目标概述 (Goal Description)

对 [`docs/backtest_research.md`](file:///home/stone/Work/github/quant/docs/backtest_research.md) 进行全面技术审查与设计精炼（Refine Design），并落实以下**五项**核心工程要求：

1. **统一回测输出根目录与时间戳路径**：将原文档中硬编码在 `/tmp/opencode/bt_*` 的所有命令输出统一迁移至 `<project_root>/backtests_research/<datetime>/`（例如 `/home/stone/Work/github/quant/backtests_research/20261025_091500/`）。
2. **每个命令/实验臂输出至独立子文件夹**：按实验轮次、策略与消融臂建立层级清晰的独立文件夹（如 `01_r1_longwin/<strategy>/`、`02_r2a_std/<strategy>/`、`03_r2b_oos/<strategy>/`、`04_r1_std_cb_compare/<arm>/<strategy>/`、`05_anatomy/<strategy>/`、`06_ablate/<arm>/<strategy>/`），同时保存 `run.log`。
3. **在同一运行文件夹内落盘生效策略配置（Dump Strategy in the Same Run Folder）**：在 [`backtester/run_backtest.py`](file:///home/stone/Work/github/quant/backtester/run_backtest.py) 中原生支持将合并了 `--strategy-params`（及 `--optimize` 结果）后的**实际生效策略定义（Effective Strategy Dump）**自动写入当前 `--results-dir` 下的 `strategy_dump.json`，使每个回测结果文件夹 100% 自包含、可审计且可直接复现。
4. **修复原研究文档中的四处设计缺陷（Design Refinements）**：
   - **修复 §8 逐模块归因轮（Ablation）跨策略参数前缀静默失效 Bug**：原文档对 `chan_risk_managed_blend`（前缀 `crb_`）和 `chan_dual_hybrid_blend`（前缀 `cdhb_`）传入了写死的 `cfsb_*` 覆盖键及被 `cdhb_max_assets_per_sector` 遮蔽的 `blend_max_assets_per_sector`，导致在非 `cfsb` 策略上 6 个消融臂沦为静默空操作（No-op）。
   - **修复 §8 `no_sectorthrot` 自适应限流配置语义**：`cdhb` 显式开启了 `cdhb_adaptive_sector_throttle: true`，而该节流在 `max_assets_per_sector` 之上**只会向上抬高**热行业上限（`allowed_sec_cap = max(allowed_sec_cap, 2)`），从不向下压缩；消融时须联动覆盖 `"${pfx}_adaptive_sector_throttle": false`，以消除「热行业抬高到 ≥2」与「放开到 20」的语义冲突。
   - **补充 §8 消融臂 × 策略可达性矩阵与生效位点解析**：澄清 `no_structregime` 仅在 `regime_adaptive` / `regime_ic_ir_hybrid` 模式下可达（三策略中仅 `chan_four_state_blend` 可达；`crb` / `cdhb` 默认 `rolling_ic_ir`，在 `_compute_adaptive_weights` 的 `rolling_ic_ir` 分支提前返回，故结构性不可达）；`no_hysteresis` 的消费符号 `enable_regime_hysteresis` 默认 `False`，仅 `chan_four_state_blend` 的 dump 将其设为 `true`，因此该臂只对该策略真实生效，在 `crb` / `cdhb` 上输出恒等于 `baseline`（no-op / 假阴性）。提供完整的消融臂 × 策略可达性矩阵表，并显式披露「标 `✗` 的臂在该策略上输出与 `baseline` 完全相同，不得据此得出『该模块无效』的结论」。
   - **修复 §6.5 熔断对照臂缺少同区间基线及 §6.1 外部路径依赖**：将 20 股池快照自包含保存在 `$BT_ROOT/universe_20_stocks.txt`；将 §6.5 熔断对照臂补齐同区间基线对照并对齐 `2022-01-04 → 2026-10-22` 口径说明。
5. **回测产物入库留档（Committed Artifacts Policy）**：回测产物根目录 `backtests_research/` **不使用 `.gitignore` 排除**，所有运行产物随仓库提交入库，便于后续交叉复核与回归对照 —— 产物是**长期留存的研究资产**，不是临时文件（规范与核验命令见 §4）。**经评审建议为 `backtests_research/` 补一条 `.gitignore` 排除条目，已由用户决策否决**：不新增任何忽略规则，产物一律入库。同时严禁将 `--cache-dir` 指向 `backtests_research/` 或外部 `/tmp/...`，默认保持读取 `<repo_root>/data/cache.duckdb` 以严格保证离线可复现性（见 §4.4）。

---

## 需要用户确认的事项 (User Review Required)

> [!IMPORTANT]
> **1. `backtester/run_backtest.py` 自动落盘 `strategy_dump.json` 的契约设计**
> - 每次运行 [`backtester/run_backtest.py`](file:///home/stone/Work/github/quant/backtester/run_backtest.py) 时，在 `results_dir` 下自动写出 `strategy_dump.json`。
> - 该文件完整保留原 `--strategy-file` 的结构（`template_name`、`strategy_version`、`base_version`、`version_info`、`explanation`、`research_strategy_spec` 等），并将 `--strategy-params`（以及 `--optimize` 通过验证后的 `best_params`）合并写入顶层 `params` 与 `research_strategy_spec.entry_data.parameters`，同时附加 `cli_strategy_params_override` 字段记录本次 CLI 传入的原始覆盖项。
> - 落盘调用时机：在 `backtester/run_backtest.py::main()` 中，`_dump_effective_strategy_def(...)` 在 `if args.optimize:` 代码块执行完毕后被**单次调用**（约第 1250 行，紧邻在 `if args.mode == "standard":` 分支判定之前），传入已更新的 `params` 与原始 CLI 覆盖字典 `strat_overrides`。
> - 该改动位于 `backtester/run_backtest.py`，不触碰 `AllocationTemplate` 或 `BaseBlendAllocationStrategy` 的信号/风控逻辑，依 `AGENTS.md` §7 **无需递增策略版本号**。

> [!WARNING]
> **2. `docs/backtest_research.md` §8 消融实验的前缀与自适应限流修复影响**
> - 原文档 §8 `run_arm` 将 `cfsb_*` 硬编码传给了 `crb` 和 `cdhb`，若按原样执行，后两个策略在 `no_voltarget`、`no_thrust`、`no_inertia`、`bull_full`、`no_circuit`、`no_sectorthrot` 六个臂上的输出将与 `baseline` 100% 相同（假阴性）。此外 `cdhb` 默认开启自适应行业限流（`cdhb_adaptive_sector_throttle: true`），但该节流在 `max_assets_per_sector` 之上只会把热行业上限**向上抬高**到至少 2（`allowed_sec_cap = max(allowed_sec_cap, 2)`），**从不向下压缩**；因此 `no_sectorthrot` 臂的真实意图是把行业上限从默认 2 放开到 20，并顺带显式关闭该节流以消除「热行业抬高到 ≥2」这条与「放开到 20」语义冲突的残余逻辑（在 cap ≥ 2 时它本已空转，显式关闭仅为可读性）。
> - 本方案将 `run_arm` 升级为根据当前策略自动映射前缀（`cfsb` / `crb` / `cdhb`），并在 `no_sectorthrot` 联动关闭自适应限流，运行后直接通过 `<run_folder>/strategy_dump.json` 校验目标参数键。

---

## 拟议变更详情 (Proposed Changes)

### 1. 回测引擎：运行目录内自动落盘生效策略配置

#### [MODIFY] [run_backtest.py](file:///home/stone/Work/github/quant/backtester/run_backtest.py)

在 `main()` 创建 `results_dir` 并完成 `--optimize` 分支更新 `params` 后（约第 1250 行，紧邻在 `if args.mode == "standard":` 之前），单次调用 `_dump_effective_strategy_def` 将当前运行实际生效的策略定义序列化落盘为 `<results_dir>/strategy_dump.json`：

```python
def _dump_effective_strategy_def(
    strategy_def: dict,
    params: dict,
    research_strategy_spec: dict | None,
    results_dir: str,
    cli_overrides: dict | None = None,
) -> str:
    """Persist the effective strategy configuration (with CLI overrides / optimize
    updates merged) into the current run's results_dir so every backtest output
    folder is self-contained and auditable."""
    dump_payload = copy.deepcopy(strategy_def)
    dump_payload["params"] = copy.deepcopy(params)
    # 恒写出：未传 --strategy-params 时写 {}，绝不省略该键（见下方 §1 NOTE）
    dump_payload["cli_strategy_params_override"] = (
        copy.deepcopy(cli_overrides) if cli_overrides is not None else {}
    )
    if isinstance(research_strategy_spec, dict):
        spec_copy = copy.deepcopy(research_strategy_spec)
        if isinstance(spec_copy.get("entry_data"), dict):
            spec_copy["entry_data"]["parameters"] = copy.deepcopy(params)
        dump_payload["research_strategy_spec"] = spec_copy
    dump_path = os.path.join(results_dir, "strategy_dump.json")
    write_json_report(dump_payload, dump_path)
    return dump_path
```

> [!NOTE]
> **`dump` 里只有 `research_strategy_spec` 一个 spec 键**：早期实现额外写过一个 `research_strategy` 键（与 `research_strategy_spec` 内容重复），但没有任何消费方读它 —— `common/strategy_spec.py::load_strategy_file` 只校验 `research_strategy_spec`（`strategy_key` + `entry_data`）。该重复键已删除，**不要**以「向后兼容」为由加回：它只会让 dump 与本节约定的 schema 出现两处真相。

**接入位点说明**：
在 `backtester/run_backtest.py::main()` 中：
```python
    # 前置声明：真码中 strat_overrides 仅在 `if args.strategy_params:` 分支内赋值，
    # 若直接把分支内的局部名传给落盘调用，未传 --strategy-params 时必然 NameError。
    strat_overrides: dict | None = None

    if args.strategy_params:
        strat_overrides = json.loads(args.strategy_params)
        if not isinstance(strat_overrides, dict):
            raise ValueError(...)
        params.update(strat_overrides)
        ...

    # 位于 args.optimize 处理完毕之后，args.mode 判断之前：
    if args.optimize:
        ...
        if opt["trusted"] and opt["best_params"]:      # 空 dict ⇒ 保持原参数并写 [!WARNING]，见 §3
            params = opt["best_params"]                # 重新绑定、整体替换（非 update）
        else:
            ...  # 保持 params 不变，记录「blend 策略 param_grid 为空，--optimize 退化，已跳过覆盖」告警

    # ── Phase 1（跑前骨架，保留在既有落盘点，紧跟 _dump_effective_strategy_def 之后）──
    dump_path = _dump_effective_strategy_def(
        strategy_def=strategy_def,
        params=params,
        research_strategy_spec=research_strategy_spec,
        results_dir=results_dir,
        cli_overrides=strat_overrides,
    )
    _dump_run_manifest(
        results_dir, mode=args.mode, group=_group_from_results_dir(results_dir),
        strategy_name=strategy_name, strategy_file=args.strategy_file,
        strategy_dump_path=dump_path,
        # 解析在上方完成：**配置的**缓存文件（--cache-dir 下存在 cache.duckdb 就用它，
        # 否则回落到 DATA_DIR/cache.duckdb）。这三个 stat 字段只描述「配置指向哪里」，
        # 是否真的被本次运行读写由 db_used 另行标明。见本节 L6 说明。
        db_path=db_path,
        db_used=db_used,     # 合成 provider / --no-cache ⇒ False（本次运行根本没打开它）
        cli_overrides=strat_overrides,
        metrics=None,        # standard/walkforward 一律先留 null
        artifacts=None,      # None ⇒ 现扫目录，该时点扫到的就是 strategy_dump.json
    )

    if args.mode == "standard":
        ...
    elif args.mode == "walkforward":
        ...

    # ── Phase 2（跑后收尾，main() 最末，两分支写完 CSV/JSON 与出图之后）──
    _finalize_run_manifest(
        results_dir,
        metrics=_extract_walkforward_metrics(summary) if args.mode == "walkforward" else None,
        artifacts=_artifact_sizes(results_dir),
    )
```

> [!IMPORTANT]
> **`run_manifest.json` 必须分两阶段写，不能写成「一次性调用」**：walkforward 的 `metrics` 要等 `walkforward_summary.json` 生成后才存在，`artifacts[].bytes` 要统计的文件（`backtest_equity.csv` / `walkforward_*.csv` / PNG …）在跑前**根本还不存在**，而 `run.log` 更是进程内永远看不见（见下方 §1 的 `run.log` 排除说明）。因此 Phase 1 只写「可复现指纹骨架」，Phase 2 在同一文件上做收尾覆写 —— 任何「把两阶段合并成跑前一次调用」的实现都必然只能填入假值或 `0`，那正是本文档反复禁止的「编造指标」。

> [!WARNING]
> **`--optimize` 的 `params` 覆盖是「重新绑定」而非「叠加」**：真码为 `params = opt["best_params"]`，即用 ERS 验证通过后的优化胜出参数**整体替换** `params` 对象。因此落盘时 `strategy_dump.json` 的顶层 `params` 反映的是**优化胜出参数**，而 `cli_strategy_params_override` 只记录**原始** CLI 传入项 —— 两者在 `--optimize` 场景下不是叠加关系，不要期望「dump 的 params = CLI 覆盖 ∪ best_params」。需要审计「这次到底传了什么」看 `cli_strategy_params_override`，需要复现「这条曲线由什么参数跑出来」看顶层 `params`。

#### 第二个落盘 helper（两阶段）：`_dump_run_manifest(...)` + `_finalize_run_manifest(...)`

`<results_dir>/run_manifest.json` 是**唯一**为「跨运行交叉复核」提供机器可校验底稿的产物：`INDEX.md`（§4.3）是人工索引，而指标按模式分散在 `walkforward_summary.json` 与纯文本 `run.log` 两处，人工抄写存在编造风险（§4.3 的 `[!NOTE]` 已把「编造索引行」列为比没有索引更危险的事）——`run_manifest.json` 就是那行的**机器可读底稿**。

它由两个 helper 分两阶段写同一个文件（阶段划分理由见上文 `[!IMPORTANT]`）：

- **Phase 1** `_dump_run_manifest(...)`：跑前骨架，落盘点与 `_dump_effective_strategy_def` 相邻；
- **Phase 2** `_finalize_run_manifest(...)`：跑后收尾，在 `main()` 最末、standard / walkforward 分支写完 CSV / JSON 与出图之后调用。

**Phase 1 — 跑前骨架的 helper 实现**：

```python
def _dump_run_manifest(
    results_dir: str,
    *,
    mode: str,
    group: str,
    strategy_name: str,
    strategy_file: str,
    strategy_dump_path: str,
    db_path: str,
    db_used: bool,
    cli_overrides: dict | None = None,
    metrics: dict | None = None,
    artifacts: list[dict] | None = None,
) -> str:
    """Phase 1: persist the reproducibility fingerprint skeleton *before* the
    backtest runs, so a mid-run crash (OOM / data-load failure / KeyboardInterrupt)
    still leaves an auditable run_manifest.json behind."""
    manifest = {
        "schema_version": 1,
        # 运行时间戳目录名形如 20261025_091500，位于 backtests_research/ 之下、
        # 而 results_dir 是 <BT_ROOT>/<group>/<strategy> 的叶子，故不能直接取
        # basename(results_dir)（那是策略名）。按正则从路径段里提取，提取不到就留 None。
        "run_ts": _run_ts_from_results_dir(results_dir),
        "git_commit": _git(["rev-parse", "HEAD"]),
        "git_dirty": bool(_git(["status", "--porcelain"])),
        # Phase 1 恒定写 "running"；Phase 2 覆写为 "completed" 并盖 completed_at_utc
        # 时间戳（UTC ISO-8601）。因此「停在 running 的 manifest」= 那次运行没跑完。
        "status": "running",
        "completed_at_utc": None,
        "mode": mode,
        "group": group,
        "strategy": strategy_name,
        "strategy_file": strategy_file,
        "strategy_dump_sha256": _sha256_file(strategy_dump_path),
        "db_path": db_path,
        "db_size_bytes": os.path.getsize(db_path) if os.path.exists(db_path) else None,
        "db_mtime_iso": _mtime_iso(db_path),
        # 前三个字段只 stat「配置指向的缓存文件」；db_used 另行回答「本次运行是否真的读写它」。
        # 合成 provider（is_synthetic_provider 为真时 CachedDataProvider 根本不构造
        # DuckDBCache）与 --no-cache 都为 False ⇒ 二者不可混为一谈，否则合成运行会把一个
        # 从未打开过的几百 MB 生产缓存记成「本次使用的行情源」，那是纯粹的溯源撒谎。
        "db_used": db_used,
        "cli_overrides": copy.deepcopy(cli_overrides or {}),
        # Phase 1 一律传 None ⇒ 落到 _NULL_METRICS（六键全 null）。standard 模式
        # 此后永久保持 null（无结构化来源，见下方设计约束 2）；walkforward 由 Phase 2 回填。
        "metrics": metrics if metrics is not None else dict(_NULL_METRICS),
        # Phase 1 传 None ⇒ 现扫目录，把「此刻已存在」的产物（通常正是 strategy_dump.json）
        # 连同真实字节数写进骨架 —— 骨架不谎称目录为空，崩溃后也能据它核出 dump 是否落盘。
        "artifacts": artifacts if artifacts is not None else _artifact_sizes(results_dir),
    }
    path = os.path.join(results_dir, "run_manifest.json")
    write_json_report(manifest, path)
    return path
```

> [!IMPORTANT]
> **Phase 1 骨架的价值（不是「多写一次文件」）**：即便回测在随后中途 OOM、数据加载抛错、或被 `Ctrl-C` 打断，`run_manifest.json` 里已经带着完整的**可复现指纹** —— `run_ts` / `git_commit` / `git_dirty` / `group` / `strategy` / `strategy_file` / `strategy_dump_sha256` / `db_path` + `db_size_bytes` + `db_mtime_iso` + `db_used` / `cli_overrides`。没有这一步，「这次跑到一半就挂了，用的是哪份行情缓存（以及它到底有没有被打开）、哪一版代码、哪一套参数」就只剩 `.bash_history` 可查。代价是 O(1) 的 `stat` 加一次几 KB 的 JSON 写。

**Phase 2 — 跑后收尾的 helper 实现**：

```python
def _finalize_run_manifest(
    manifest_path: str,          # 收 results_dir 或 run_manifest.json 路径皆可
    *,
    metrics: dict | None = None,
    artifacts: list[dict] | None = None,
) -> str:
    """Phase 2: re-open the Phase-1 skeleton and fill in post-run metrics and
    artifact sizes. Never fabricates metrics for --mode standard."""
    path = os.path.join(manifest_path, "run_manifest.json") if os.path.isdir(manifest_path) else manifest_path
    if not os.path.exists(path):
        # Phase 1 必须已写过；缺文件说明骨架落盘被跳过（或 results_dir 被删）。
        # 这里显式报错而不是静默重建 —— 静默重建会给出一个「看起来完整、实际缺指纹」的清单。
        raise FileNotFoundError(f"run manifest skeleton missing: {path}")
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    # 不接收 mode：哪些指标「可以填」由调用点在传参前决定（standard 传 None ⇒ 六键 null；
    # walkforward 传 _extract_walkforward_metrics(...)）。在这里再收一个 mode 只会被丢弃，
    # 而「有文档、却没人用」的参数正是未来的 bug 温床。
    manifest["metrics"] = metrics if metrics is not None else dict(_NULL_METRICS)
    # Phase 1 的其余字段（db_path / db_size_bytes / db_mtime_iso / db_used / 各指纹）经
    # 「读回 → 改这四个键 → 覆写」自动保留，Phase 2 不重算也不清除它们。
    manifest["artifacts"] = artifacts if artifacts is not None else _artifact_sizes(os.path.dirname(path))
    write_json_report(manifest, path)
    return path
```

**Phase 2 的取值规则（逐条落进实现，不得放宽）**：

1. **`metrics` 仅 `--mode walkforward` 可填**：来源是同一次运行刚落盘的 `walkforward_summary.json`，逐键映射（**改名映射，不是同名直抄**）：

   | manifest `metrics` 键 | `walkforward_summary.json` 来源键 |
   | :--- | :--- |
   | `sharpe` | `mean_sharpe_ratio` |
   | `cagr` | `mean_cagr` |
   | `max_drawdown` | `mean_max_drawdown` |
   | `calmar` | `mean_calmar_ratio` |
   | `n_folds` | `n_folds` |
   | `deflated_sharpe` | `deflated_sharpe_ratio` |

   映射由 `_extract_walkforward_metrics(summary)` 完成（缺键一律填 `None`，**不**用 0 或相邻指标顶替）。
2. **`--mode standard` 的 `metrics` 永久保持六键 `null`**：standard 的标量指标只经 `format_backtest_metrics_summary(...)` 打在 `run.log` 文本里（见 §4.2），进程内没有任何结构化来源。**禁止臆造**，也禁止从日志正则捞取后四舍五入回填 —— 一个假的非空指标比 `null` 危险得多；`INDEX.md` 对 standard 行的取值纪律是「据 `metrics` 为 `null` 而转抄 `run.log`」（§4.3）。
3. **`_artifact_sizes(results_dir)` 用 `os.scandir` 逐项 `getsize` 统计目录内全部文件**（不递归子目录：运行产物都直接落在叶子目录），**并显式排除 `run.log` 与 `run_manifest.json` 两个文件名**（`if entry.is_file() and entry.name not in ("run.log", "run_manifest.json")`）。
4. **`run.log` 为什么必然被排除**：它不是 Python 写的 —— 它是命令尾部的 shell `tee "$out_dir/run.log"`（§2 各组命令、§4.2 必提交清单）在**本进程退出之后**才创建/写完的，`main()` 里既看不到它、也 `stat` 不到它。若在 Phase 2 强行统计，结果只会是「不存在」或「上一次运行遗留的旧文件大小」，两者都是错数。它也不出现在 Phase 1 骨架里（那时 `tee` 还没启动）。
5. **`run_manifest.json` 为什么也被排除（它不列出自己）**：清单就是 `run_manifest.json` 本身，而它的大小在扫描那一刻**尚未写成** —— Phase 1 扫到的是上一阶段的旧文件（或根本不存在），Phase 2 扫到的是 Phase 1 骨架的字节数，随后覆写又变长。任何「先扫目录、再写清单」的顺序都只能记下一个**过期**的自身大小（实测差 300–450 字节），而这正是本节反复禁止的「编造数字」。所以它的正确定位是「清单不统计自己」，需要它的体积时用 `stat -c %s run_manifest.json`。**因此 `artifacts` 里没有 `run_manifest.json`，这是设计契约，不是漏统计**；相位一致性由 §1 的两阶段 `status` 字段（`running` / `completed`）承担，不押在自身字节数上。
6. **确需把 `run.log` 纳入清单时的唯一正确做法（可选，默认不启用）**：在 shell 侧 `tee` **之后**再补一步，把 `run.log` 的 size 写回清单：

   ```bash
   # 放在 `... 2>&1 | tee "$out_dir/run.log"` 之后（可选，默认不启用）
   LOGSZ=$(stat -c %s "$out_dir/run.log")
   jq --argjson n "$LOGSZ" \
      '.artifacts += [{"name": "run.log", "bytes": $n}]' \
      "$out_dir/run_manifest.json" > "$out_dir/run_manifest.json.tmp" \
      && mv "$out_dir/run_manifest.json.tmp" "$out_dir/run_manifest.json"
   ```

> [!WARNING]
> **第 6 条是可选覆盖，默认不启用**（本方案各组命令不加）。理由：它让「manifest 的产物清单」依赖 shell 侧额外步骤，一旦某组命令漏抄这一行，清单就会静默缺项；而 `run.log` 的存在性已由 §4.2 的必提交清单与验证计划第 5 项（`find ... -name run.log | wc -l`）独立核验，不必押在 manifest 上。**因此默认口径是：`artifacts` 里没有 `run.log`，这是设计如此，不是漏统计。**

落盘 schema（字段名照抄，不得改名；Phase 1 骨架与 Phase 2 收尾后是**同一份 schema**，差别只在 `metrics` / `artifacts` 的填充程度）：

```json
{
  "schema_version": 1,
  "run_ts": "<YYYYMMDD_HHMMSS 运行目录名；提取不到则为 null>",
  "git_commit": "<git rev-parse HEAD>",
  "git_dirty": false,
  "status": "running | completed",
  "completed_at_utc": "<Phase 2 收尾时刻，UTC ISO-8601；Phase 1 骨架为 null>",
  "mode": "standard | walkforward",
  "group": "<01_r1_longwin … 06_ablate/<arm>>",
  "strategy": "<strategy_name>",
  "strategy_file": "<--strategy-file 原值>",
  "strategy_dump_sha256": "<strategy_dump.json 的 sha256>",
  "db_path": "<配置的缓存文件（`db_used` 表明本次运行是否真正使用它）：--cache-dir 下存在 cache.duckdb 即取它，否则 <repo_root>/data/cache.duckdb>",
  "db_size_bytes": 0,
  "db_mtime_iso": "<ISO8601>",
  "db_used": false,
  "cli_overrides": {},
  "metrics": {"sharpe": null, "cagr": null, "max_drawdown": null, "calmar": null, "n_folds": null, "deflated_sharpe": null},
  "artifacts": [{"name": "strategy_dump.json", "bytes": 0}]
}
```

**`status` / `completed_at_utc` 语义（两阶段落盘的完成标记）**：
- `status`：Phase 1 恒定写 `"running"`；Phase 2 覆写为 `"completed"`。因此留 `"running"` 的 manifest **等于**「这次运行中途挂了」（正是 §1 Phase-1 骨架存在的意义，不必再去比 mtime）。
- `completed_at_utc`：Phase 1 写 `null`；Phase 2 盖上收尾时刻的 UTC ISO-8601 时间戳（`datetime.now(timezone.utc).isoformat()`）。与 `run_ts`（**目录名**里的本地启动时刻）分工不同：前者回答「何时收尾」，后者回答「哪次运行」。

> [!NOTE]
> **`artifacts` 示例里没有 `run.log`，这是刻意的**（见 Phase 2 取值规则第 4 条）：`tee` 在本进程退出后才写日志，清单统计不到它。**同样没有 `run_manifest.json` 本身**（第 5 条）：清单不能从写前扫描里如实报出自己的写后大小，故按设计排除自身。Phase 1 骨架刚写完时该数组通常只有 `strategy_dump.json` 一项；Phase 2 收尾后应覆盖该目录内全部产物（standard：`backtest_equity.csv` / `backtest_weights.csv` / `rebalance_report.csv` / `equity_curve.png` / `comparison_report.json` …；walkforward：`walkforward_*.csv` / `walkforward_summary.json` …），唯独不含 `run.log` 与 `run_manifest.json` 这两项。

> [!NOTE]
> 上面伪代码里的 `_git` / `_sha256_file` / `_mtime_iso` / `_run_ts_from_results_dir` / `_group_from_results_dir` / `_artifact_sizes` / `_extract_walkforward_metrics` / `_NULL_METRICS` 都是本方案**要新增**的模块级私有辅助（分别是对 `subprocess.run(..., capture_output=True)`、`hashlib.sha256` 分块读取、`datetime.fromtimestamp(os.stat(...).st_mtime).isoformat()`、按 `^\d{8}_\d{6}$` 匹配路径段、取时间戳段与叶子目录之间的路径段并拼成 `group`（如 `06_ablate/baseline`，取不到则留空串并打印 WARNING）、`os.scandir` 逐项 `getsize` 且跳过 `run.log` 与 `run_manifest.json`、把 `walkforward_summary.json` 的 `mean_*` / `n_folds` / `deflated_sharpe_ratio` 改名映射为 manifest `metrics` 六键（缺键填 `None`）、以及六键全 `null` 的常量模板），**不是既存 API**，不要去找。

> [!NOTE]
> **`db_path` 记的是「配置的缓存文件」，不是写死的路径，也不等于「本次运行实际读了它」**：`main()` 取 `--cache-dir`（缺省为 `DATA_DIR`）下的 `cache.duckdb`，**该文件存在就用它**，否则回落到 `DATA_DIR/cache.duckdb`（`DATA_DIR` = `common/cli_utils.shared_data_dir()` 解析出的 `<repo_root>/data`）。这是比硬编码更强的溯源：把 `--cache-dir` 指向临时副本的那些调试运行，manifest 会如实记成那份副本，而不是谎称读了生产缓存。**但 `db_path` + `db_size_bytes` + `db_mtime_iso` 三者只是对配置路径的一次 `stat`（从不开文件）**，因此合成 provider 运行（`CachedDataProvider.__init__` 在 `is_synthetic_provider` 为真时根本不构造 `DuckDBCache`）或带 `--no-cache` 的运行，会如实记录「配置指向哪个缓存」但**必须**把 `db_used` 写成 `false` —— 否则就会把一份几百 MB、本次从未打开过的生产缓存谎报成行情来源。`db_used` 的取值口径固定为：`not is_synthetic_provider(args.data_provider) and not args.no_cache`（`is_synthetic_provider` 来自 `common/duckdb_cache.py`，与数据层判断同源）。
>
> `db_used` 在 Phase 1 写入、Phase 2 经同一份 JSON 的读回-覆写自动保留 —— **不要**在 Phase 2 重算它（那时 provider 已经解引用，重算只会引入第二套口径）。
>
> 在 §4.4 第 1 条「所有研究命令**不传** `--cache-dir`」的口径下，两者**恒等** —— `--cache-dir` 缺省即 `DATA_DIR`，落到的仍是 `<repo_root>/data/cache.duckdb`，因此 `backtests_research/` 内的正式研究运行，其 `db_path` 语义与「固定写 `<repo_root>/data/cache.duckdb`」完全一致（`db_used` 在这些运行里恒为 `true`，除非显式传了 `--no-cache`）。

> [!NOTE]
> **`cli_strategy_params_override` / `cli_overrides` 都是「恒存在」字段，不是 Optional**：未传 `--strategy-params` 时写 `{}`（`strategy_dump.json` 与 `run_manifest.json` 同理）。**不要**改回「为 `None` 时省略该键」—— 省略会让读者无法区分「这次没传覆盖项」与「这份 JSON 是旧版本写的、根本没有这个字段」，而审计场景恰恰只关心这个区分。

> [!WARNING]
> **两条设计约束（必须落进实现，不得「顺手优化」掉）**：
> 1. **`db_sha256` 明确不采用**：`cache.duckdb` 是 GB 级文件，每次运行逐次全量哈希的代价远高于其收益。改用 `db_path` + `db_size_bytes` + `db_mtime_iso` 三者组合作为「数据版本指纹」，再由 `db_used` 标明这份指纹是否适用于本次运行 —— 足够回答「这次跑的是不是同一份行情缓存」，且代价是 O(1) 的 `stat`。
> 2. **`metrics` 在 `--mode standard` 下没有结构化来源**：standard 的标量指标只经 `format_backtest_metrics_summary(...)` 打进 `run.log` 文本（见 §4.2 的说明），因此该字段**允许为 `null`，且写盘时必须保留 `null`**。**禁止**为了「把字段填满」而臆造数值或从 `run.log` 正则捞取后四舍五入 —— 一个假的非空指标比 `null` 危险得多。walkforward 模式下才从 `walkforward_summary.json` 的 `mean_*` / `n_folds` / `deflated_sharpe_ratio` 填入真实值。
>
> 实现细节：`_dump_run_manifest` / `_finalize_run_manifest` 均复用 `common/reporting.py::write_json_report`，因此非有限浮点（`NaN` / `inf`）会被统一消毒为 `null`，与 `strategy_dump.json` 的序列化口径一致（Phase 2 是「读回 → 覆写 → `write_json_report`」，不会绕开这道消毒）。

---

### 2. 回测研究文档审查与设计精炼

#### [MODIFY] [backtest_research.md](file:///home/stone/Work/github/quant/docs/backtest_research.md)

1. **目录拓扑与时间戳规范（§6 / §7 / §8）**：
   所有命令统一使用项目根目录下的 `backtests_research/<datetime>/` 结构，每个命令输出独立文件夹并落盘 `strategy_dump.json` 与 `run.log`：

   ```mermaid
   flowchart TD
       ROOT["<project_root>/backtests_research/<YYYYMMDD_HHMMSS>/"]
       ROOT --> U["universe_20_stocks.txt\n(自包含 20 股池快照)"]
       ROOT --> R1["01_r1_longwin/<strategy>/\n• strategy_dump.json\n• walkforward_summary.json\n• walkforward_report.csv\n• walkforward_weights.csv\n• walkforward_rebalances.csv\n• run.log"]
       ROOT --> R2A["02_r2a_std/<strategy>/\n• strategy_dump.json\n• backtest_equity.csv\n• backtest_weights.csv\n• rebalance_report.csv\n• run.log"]
       ROOT --> R2B["03_r2b_oos/<strategy>/\n• strategy_dump.json\n• walkforward_*.csv/json\n• run.log"]
       ROOT --> CB["04_r1_std_cb_compare/<arm>/chan_four_state_blend/\n(baseline vs no_circuit)\n• strategy_dump.json\n• backtest_*.csv\n• run.log"]
       ROOT --> ANAT["05_anatomy/<strategy>/\n• strategy_dump.json\n• backtest_*.csv & equity_curve.png\n• run.log"]
       ROOT --> ABL["06_ablate/<arm>/<strategy>/\n(13 arms × 3 strategies = 39 folders)\n• strategy_dump.json\n• backtest_*.csv\n• run.log"]
   ```

2. **统一头部环境变量与自包含 Universe 构建（§6.1）**：
   ```bash
   cd /home/stone/Work/github/quant
   REPO_ROOT="$(pwd)"
   PY="$REPO_ROOT/pipeline/.venv/bin/python"
   DUMPS="$REPO_ROOT/pipeline/research_strategy/results/strategy_dumps"
   RUN_TS="$(date +%Y%m%d_%H%M%S)"
   BT_ROOT="$REPO_ROOT/backtests_research/$RUN_TS"
   mkdir -p "$BT_ROOT"

   UNIV="$BT_ROOT/universe_20_stocks.txt"
   printf '%s\n' \
     601872.SH 601728.SH 601288.SH 601225.SH 601919.SH \
     600900.SH 601766.SH 000999.SZ 601857.SH 600941.SH \
     600028.SH 601088.SH 601398.SH 600362.SH 000157.SZ \
     300394.SZ 601899.SH 600584.SH 600660.SH 000938.SZ \
     > "$UNIV"
   grep -vc '^#' "$UNIV"    # 应为 20
   ```

3. **修复 §6.5 熔断对照臂同区间基线配对**：
   提供 `04_r1_std_cb_compare/baseline/chan_four_state_blend` 与 `04_r1_std_cb_compare/no_circuit/chan_four_state_blend` 成对运行（或直接与 §6.3 `02_r2a_std` 对齐区间说明），确保 `2022-01-04 → 2024-12-31` 的无熔断 standard 臂有严格同区间、同零息假设的带熔断 standard 基线可做逐日净值相减。

4. **修复 §8 逐模块归因（Ablation）前缀动态映射与可达性说明**：
   - 编写 `pfx_of()` 辅助函数，根据策略名动态解析前缀（`cfsb` / `crb` / `cdhb`），解决 `crb_*` 和 `cdhb_*` 在 dump 中遮蔽 `cfsb_*` 与 `blend_max_assets_per_sector` 的问题，并在 `no_sectorthrot` 臂联动关闭 `adaptive_sector_throttle`：
     ```bash
     pfx_of () {
       case "$1" in
         chan_four_state_blend)   echo "cfsb" ;;
         chan_risk_managed_blend) echo "crb"  ;;
         chan_dual_hybrid_blend)  echo "cdhb" ;;
         *) echo "blend" ;;
       esac
     }

     run_arm () {
       local arm="$1"
       for s in chan_four_state_blend chan_risk_managed_blend chan_dual_hybrid_blend; do
         local pfx; pfx="$(pfx_of "$s")"
         local extra
         case "$arm" in
           baseline)        extra='{}' ;;
           no_voltarget)    extra="{\"${pfx}_enable_vol_targeting\": false}" ;;
           no_orthogon)     extra='{"blend_enable_orthogonalization": false}' ;;
           no_structregime) extra='{"blend_enable_structural_regime": false}' ;;
           no_cyq)          extra='{"blend_enable_cyq_quality_gate": false}' ;;
           no_atrstop)      extra='{"blend_enable_adaptive_stop_atr": false}' ;;
           no_lowbeta)      extra='{"blend_enable_low_beta_inertia": false}' ;;
           no_sectorthrot)  extra="{\"${pfx}_max_assets_per_sector\": 20, \"blend_max_assets_per_sector\": 20, \"${pfx}_adaptive_sector_throttle\": false}" ;;
           no_hysteresis)   extra='{"blend_enable_regime_hysteresis": false}' ;;
           no_thrust)       extra="{\"${pfx}_dynamic_cash_deployment\": false}" ;;
           no_inertia)      extra="{\"${pfx}_adaptive_inertia\": false, \"${pfx}_min_weight_change\": 0.0, \"${pfx}_min_holding_bars\": 0, \"${pfx}_entry_threshold\": 0.0}" ;;
           bull_full)       extra="{\"${pfx}_target_bull_exposure\": 1.0}" ;;
           no_circuit)      extra="{\"${pfx}_dd_reduce_thresh\": 10.0, \"${pfx}_dd_defensive_thresh\": 10.0, \"${pfx}_dd_stop_thresh\": 10.0}" ;;
         esac
         local out_dir="$BT_ROOT/06_ablate/$arm/$s"
         mkdir -p "$out_dir"
         $PY backtester/run_backtest.py \
           --strategy-file "$DUMPS/${s}_strategy.json" \
           --universe-file "$UNIV" \
           --start 2022-01-04 --end 2026-10-22 --mode standard \
           --data-provider fuyao --min-shares 100 --china-trading \
           --baseline-symbol 000300.SH \
           --cash-sweep-rate 0.015 \
           --strategy-params "$extra" \
           --results-dir "$out_dir" --n-jobs 1 \
           2>&1 | tee "$out_dir/run.log"
       done
     }
     ```

   - **消融臂 × 策略可达性矩阵 (Ablation Reachability Matrix)**：

     | 消融臂 (Arm) | 覆盖参数键与目标值 | `cfsb` (`regime_ic_ir_hybrid`) | `crb` (`rolling_ic_ir`) | `cdhb` (`rolling_ic_ir`) | 机制与生效符号（按符号引用，不引行号，理由见 §4.2） |
     | :--- | :--- | :---: | :---: | :---: | :--- |
     | `baseline` | `{}` | ✓ 完全可达 | ✓ 完全可达 | ✓ 完全可达 | 策略 dump 默认基线 |
     | `no_voltarget` | `{"${pfx}_enable_vol_targeting": false}` | ✓ 完全可达 | ✓ 完全可达 | ✓ 完全可达 | 关闭 Garman-Klass 波动率目标化：`blend_base.py` 中由 `resolve_param(p, "enable_vol_targeting", ...)` 解析出的 `enable_vol_targeting` 分支 |
     | `no_orthogon` | `{"blend_enable_orthogonalization": false}` | ✓ 完全可达 | ✓ 完全可达 | ✓ 完全可达 | 参数键 `blend_enable_orthogonalization` 经 `resolve_param` 的 `blend_{name}` 分支解出局部符号 `enable_orthogonalization`；实际生效段为「该开关为真且子策略数 > 1 时调用辅助函数 `_apply_lowdin_orthogonalization`」（Löwdin 对称正交化 $W^* = W(W^T W)^{-1/2}$）。注意：`blend_base.py` 源码里出现的标识符是 `enable_orthogonalization`，`blend_` 前缀只是 CLI / dump 的参数键形态 |
     | `no_structregime` | `{"blend_enable_structural_regime": false}` | ✓ 完全可达 | ✗ **结构性不可达** | ✗ **结构性不可达** | 消费符号 `enable_structural_regime`，其唯一实质消费点在 `_compute_adaptive_weights` 的 `weighting_mode in ("regime_adaptive", "regime_ic_ir_hybrid")` 分支内（另一处仅决定 `fractal_d_series` 是否预计算，结果同样只喂该分支）。`crb` / `cdhb` 默认 `rolling_ic_ir`，在 `if weighting_mode == "rolling_ic_ir":` 段落算完 IC-IR 权重即 `return`，结构性状态转移判定完全不执行 |
     | `no_cyq` | `{"blend_enable_cyq_quality_gate": false}` | ✓ 完全可达 | ✓ 完全可达 | ✓ 完全可达 | 参数键 `blend_enable_cyq_quality_gate` 经 `resolve_param` 解出的局部符号 `enable_cyq_quality_gate` 门控段：筹码分布获胜率否决（`min_cyq_winner_ratio`，默认 0.20）与伪突破过滤（Track C Anchor 2） |
     | `no_atrstop` | `{"blend_enable_adaptive_stop_atr": false}` | ✓ 完全可达 | ✓ 完全可达 | ✓ 完全可达 | 消费符号 `enable_adaptive_stop_atr`，实际作用仅一行：`eff_stop_mult = np.clip(stop_atr_mult * (med_vol / s_vol), 3.5, 7.5)`。关闭后 `eff_stop_mult` 恒为 `stop_atr_mult`（默认 4.5），**底层 ATR 追踪止损仍然存在** |
     | `no_lowbeta` | `{"blend_enable_low_beta_inertia": false}` | ✓ 完全可达 | ✓ 完全可达 | ✓ 完全可达 | 消费符号 `enable_low_beta_inertia`：关闭后低波动资产（`s_vol < 0.22`）不再被抬高 `eff_min_holding` 至至少 10 bar 的防御性惯性豁免 |
     | `no_sectorthrot` | `{"${pfx}_max_assets_per_sector": 20, "blend_max_assets_per_sector": 20, "${pfx}_adaptive_sector_throttle": false}` | ✓ 完全可达 | ✓ 完全可达 | ✓ 完全可达 | 两个符号：`max_assets_per_sector`（默认 2，定基线行业上限）与 `adaptive_sector_throttle`（默认关闭，仅 `cdhb` dump 设 `true`）。**真实机制：自适应节流只在某行业 ROC 进入前 20% 分位时把该行业上限「抬高」到至少 2（`allowed_sec_cap = max(allowed_sec_cap, 2)`），从不向下压缩**，且默认 `max_assets_per_sector=2` 下该抬高本身即空转（`max(2,2)=2`）。故本臂的真实意图是**把行业上限从 2 放开到 20**，并**顺带显式关闭** `"${pfx}_adaptive_sector_throttle"` 以消除「热行业抬高到 ≥2」这条与「放开到 20」语义冲突的残余逻辑（cap ≥ 2 时它本是空转，保留显式关闭仅为可读性） |
     | `no_hysteresis` | `{"blend_enable_regime_hysteresis": false}` | ✓ 完全可达 | ✗ **结构性不可达（且默认已为 False，该臂为 no-op / 假阴性）** | ✗ **结构性不可达（且默认已为 False，该臂为 no-op / 假阴性）** | 消费符号 `enable_regime_hysteresis`（**默认 `False`**；仅 `chan_four_state_blend` 的 dump 将其设为 `true`）：开启时动量冲刺走 0.62 / 0.58 施密特双阈值（`thrust_enter_thresh` / `thrust_exit_thresh`），关闭时退回单阈值 `thrust >= thrust_thresh`。该 `thrust_active` 会回流至 `_compute_adaptive_weights` 的线程化参数（仅制度分支可达时才真正参与选权），并经 `fast_recovery` / `dynamic_cash` 影响现金部署。`crb` / `cdhb` 的 dump **未**设该键 ⇒ 默认 `False`，基线本就走单阈值路径，故强制 `false` 后输出与 `baseline` 完全相同 |
     | `no_thrust` | `{"${pfx}_dynamic_cash_deployment": false}` | ✓ 完全可达 | ✓ 完全可达 | ✓ 完全可达 | 消费符号 `dynamic_cash`（由 `resolve_param(p, "dynamic_cash_deployment", ...)` 解析）：关闭 `bull_active` 下的目标暴露度抬升与 `preemptive_thrust` 抢占式现金部署（现金部署 / breadth thrust 段） |
     | `no_inertia` | `{"${pfx}_adaptive_inertia": false, "${pfx}_min_weight_change": 0.0, "${pfx}_min_holding_bars": 0, "${pfx}_entry_threshold": 0.0}` | ✓ 完全可达 | ✓ 完全可达 | ✓ 完全可达 | 必须**同时归零 `min_weight_change` 与 `min_holding_bars`**：`use_inertia = (min_weight_change > 0.0 or min_holding_bars > 0)`，而三份策略 dump 都钉了 `min_holding_bars: 2`，**只把 `min_weight_change` 归零无法让 `use_inertia` 变假**，`_step_asset_inertia` 的任期锁定（tenure lock，`if age < eff_min_holding … locked_incumbents.add(s)`）仍会压制卖出，臂将退化为假阴性。两键同时归零后 `use_inertia` 才为假、防抖阈值 `static_inertia_thresh` 也随之失效；`adaptive_inertia` 只影响 `inertia_thresh_df` 的自适应阈值构造，故一并关掉以消除残余分支。`_step_asset_inertia` 为内联调用、`apply_asset_inertia` 在本策略中仅为 import（本臂不改变这两处代码的存在性） |
     | `bull_full` | `{"${pfx}_target_bull_exposure": 1.0}` | ✓ 完全可达 | ✓ 完全可达 | ✓ 完全可达 | 将牛市仓位上限从 0.85 提升至 1.00 全仓（`dynamic_cash` 段内 `target_exp = min(target_bull_exposure, …)`） |
     | `no_circuit` | `{"${pfx}_dd_reduce_thresh": 10.0, "${pfx}_dd_defensive_thresh": 10.0, "${pfx}_dd_stop_thresh": 10.0}` | ✓ 完全可达 | ✓ 完全可达 | ✓ 完全可达 | 将回撤熔断降仓/防御/全停三档阈值同时抬到 10.0（`dd_mag` 为比例口径，10.0 即 1000%），等效彻底禁用熔断机制 |

> [!WARNING]
> **假阴性披露（消融矩阵最重要的一行）**：标 `✗` 的臂在该策略上输出将与 `baseline` **完全相同**，不得据此得出「该模块无效」的结论。这类臂只是绕过了代码路径、或本就与默认值相同，**不产生任何信息量**；引用它们与 `baseline` 的差值属于把「没跑」当成「跑出来是 0」。凡在报告中出现某臂与 `baseline` 指标逐位相同的行，必须先核 `strategy_dump.json` 确认覆盖键确实被消费，否则该行作废。

   - **`no_sectorthrot` 联动关闭自适应行业限流的真实原因**：`chan_dual_hybrid_blend` 在策略 dump 中显式设定了 `"cdhb_adaptive_sector_throttle": true`。在 `blend_base.py` 的行业配额分配段中，自适应节流的唯一动作是 `allowed_sec_cap = max(allowed_sec_cap, 2)` —— 即当某行业 21 日平均 ROC 不低于 `max(0.02, top_sec_thresh)`（行业 ROC 前 20% 分位）时，把该行业上限**抬高**到至少 2，**没有任何向下压缩分支**，也不存在把行业上限压到 1 只的路径。由于 `no_sectorthrot` 已把 `max_assets_per_sector` 放开到 20，「热行业抬高到 ≥2」这条逻辑与「放开到 20」的意图冲突且在该 cap 下必然空转，因此一并显式关闭，保持消融语义单一（只改一件事：行业上限）。

   - **`no_atrstop` 与 `no_cyq` 语义边界披露**：
     1. `no_atrstop`（`{"blend_enable_adaptive_stop_atr": false}`）所消融的仅为**波动率环境自适应 ATR 倍数动态缩放**（`blend_base.py` 中 `enable_adaptive_stop_atr` 为真时的 `eff_stop_mult = np.clip(stop_atr_mult * (med_vol / s_vol), 3.5, 7.5)`），关闭后回退至固定 4.5x ATR 追踪止损，**并不是**彻底关闭底层 ATR 止损机制本身（彻底关闭需设置 `"${pfx}_stop_mode": "none"`）。该消融臂旨在评估「环境自适应波动倍数」相对于「固定 ATR 乘数」的边际贡献。
     2. `no_cyq`（`{"blend_enable_cyq_quality_gate": false}`）所消融的为 Track C Anchor 2（`enable_cyq_quality_gate` 门控的 CYQ 获胜率与伪突破否决），而 Track C Anchor 3（筹码汇聚点加权 sizing，`enable_vp_confluence_sizing`）在默认配置下依然生效保持开启。另请注意 `enable_vp_confluence_sizing` 段内的筹码密集度硬编码阈值 `c_dens < 0.40`（`c_dens` 取自 `consolidation_density` 列）——该阈值不受任何消融臂覆盖。

   - 升级**参数校验做法**：直接通过每个运行目录下的 `$out_dir/strategy_dump.json` 校验生效参数（如 `jq '.params.cfsb_enable_vol_targeting, .cli_strategy_params_override' "$out_dir/strategy_dump.json"`），替代原先依赖 `sed -n '2p' run.log` 的脆弱方式。

---

### 3. 单元测试验证

#### [MODIFY] [test_run_backtest.py](file:///home/stone/Work/github/quant/backtester/tests/test_run_backtest.py)
- 新增离线单元测试 `test_run_backtest_dumps_effective_strategy_json_in_results_dir`：
  1. 使用 `SyntheticDataProvider` 运行 `run_backtest.py` 并传入 `--strategy-params` 与 `--results-dir`；
  2. 断言 `<results_dir>/strategy_dump.json` 存在；
  3. 断言 `strategy_dump.json` 中的 `params` 与 `research_strategy_spec.entry_data.parameters` 已准确合并 CLI 覆盖值，且包含 `cli_strategy_params_override` 结构；
  4. 断言该 dump 可再次作为 `--strategy-file` 加载运行。

- 新增 `test_main_optimize_dumps_best_params`：
  1. 复用既有 `test_main_optimize_success_scenario_tunes_and_uses_best_params` 的确定性 fixture（`cross_sectional_momentum` + `momentum_divergence_test_provider`，`--ers-percentile-threshold 0.5` 保证 `trusted`）；
  2. 断言 `strategy_dump.json["params"] == optimize_report.json["best_params"]` —— 即 `params = opt["best_params"]` 这次**重新绑定**的结果确实被落盘捕获，而不是「策略文件原始参数」，也不是「原始参数 ∪ best_params」的叠加物；
  3. 断言 `strategy_dump.json["cli_strategy_params_override"]` 只含 CLI 原始传入项（本用例未传 `--strategy-params`，故为 `{}`），把「顶层 `params` = 优化结果 / `cli_strategy_params_override` = CLI 原始项」这一分离语义钉死。

- 新增 `test_run_manifest_written_for_standard_and_walkforward`：
  1. 同一 fixture 分别以 `--mode standard` 与 `--mode walkforward` 各跑一次（`--data-provider` 走合成 provider，`--results-dir` / `--cache-dir` 均落在 `tmp_path`）；
  2. 两模式各断言 `<results_dir>/run_manifest.json` 存在，且 `schema_version == 1`、`mode` 与实际所跑模式一致、`run_ts` 匹配 `^\d{8}_\d{6}$`（fixture 须把时间戳段嵌进 `--results-dir`，例：`tmp_path / "20261025_091500" / "06_ablate" / "baseline"`；未嵌时间戳段时应断言 `run_ts is None`，而不是断言某个具体值）、`strategy_dump_sha256` 等于实测 `hashlib.sha256(strategy_dump.json).hexdigest()`、`db_size_bytes` / `db_mtime_iso` 均非 `null`（前者须是正整数且等于实测 `os.path.getsize`，后者须能被 `datetime.fromisoformat` 解析）；
  3. **缓存指纹必须可测且可复现（hermetic）**：应让 `db_path` 指向 `tmp_path` 下的替身缓存文件，并断言 `db_path` 正是它、`db_size_bytes` 正是它的字节数；同时断言 **`db_used is False`** —— 合成 provider 不会打开该替身，因此「三个 stat 字段非空 + `db_used` 为假」这一组合正是本字段存在的意义（配置指向 vs 实际使用）。原因：合成 provider 完全不构造 `DuckDBCache`（`CachedDataProvider.__init__` 在 `is_synthetic_provider` 为真时跳过），pytest 下 `common/duckdb_cache.py::refuse_production_cache_path` 又禁止可写打开生产缓存，故 `--cache-dir` 下永远不会自然出现 `cache.duckdb`；若不预置替身，`main()` 会回落到 `<repo_root>/data/cache.duckdb` —— 该文件被 gitignore 且 CI/新克隆上**不存在**，断言在那边必然失败，同时把生产路径带进了必须 hermetic 的测试。做法：运行前在 `<--cache-dir>/cache.duckdb` 写一个真实的空 DuckDB 库（`duckdb.connect(path).close()`；本次运行不会打开它），并把 `backtester.run_backtest.DATA_DIR` monkeypatch 到 `tmp_path` 下的空目录，使「替身没被采用」直接断言失败而不是静默读真实缓存；
  4. 断言 `artifacts` 的文件名集合**覆盖**该模式下实际落盘的产物（两模式均含 `strategy_dump.json`；standard 另含 `backtest_equity.csv`，walkforward 另含 `walkforward_summary.json`），且每项 `bytes` 都等于 `os.path.getsize` —— **不允许任何按文件名开小差的分支**（早期版本用 `if a["name"] != "run_manifest.json"` 豁免自身，等于把清单里唯一一个错数合法化）。**`run.log` 与 `run_manifest.json` 都必须不在 `artifacts` 内** —— 前者由 shell `tee` 在进程退出后才写、`main()` 内不可见（见 §1 Phase 2 取值规则第 4 条），后者不能从写前扫描里如实报出自己的写后大小（第 5 条），故断言 `"run.log" not in ...` 与 `"run_manifest.json" not in ...` 都是设计契约的一部分，不是漏项；
  5. 断言两阶段的完成标记已被 Phase 2 盖上：`status == "completed"`、`completed_at_utc` 非 `null`（而中途崩溃用例里必须是 `status == "running"` + `completed_at_utc is None`，见下一个用例）；
  6. 断言 `metrics` 的键集合恒为 `{sharpe, cagr, max_drawdown, calmar, n_folds, deflated_sharpe}`，且 **standard 模式下六键必须全为 `null`**（该模式没有结构化指标来源，不接受臆造数值）；walkforward 模式下 `metrics` 与同目录 `walkforward_summary.json` **逐键相等**：`sharpe == mean_sharpe_ratio`、`cagr == mean_cagr`、`max_drawdown == mean_max_drawdown`、`calmar == mean_calmar_ratio`、`n_folds == n_folds`、`deflated_sharpe == deflated_sharpe_ratio`。

- 新增 `test_run_manifest_skeleton_survives_midrun_failure`：
  1. 用 `monkeypatch` 让回测分支（`run_standard`，或 `run_walkforward`）在被调用时直接 `raise RuntimeError`，其余参数与上一用例同（`tmp_path` 下的 `--results-dir` 内嵌时间戳段、合成 provider）；
  2. `pytest.raises(RuntimeError)` 包住 `main()`；
  3. 断言 `<results_dir>/run_manifest.json` **仍然存在**（Phase 1 骨架已落盘），且能 `json.load` 成合法 JSON；
  4. 断言 `status == "running"` 且 `completed_at_utc is None`（Phase 2 没跑，完成标记必须保持未盖章的状态）；
  5. 断言 `artifacts` **不是**空数组，而是恰好列出 `strategy_dump.json` 一项且 `bytes == os.path.getsize(...)` —— Phase 1 现扫目录（§1 Phase-1 helper NOTE），骨架不谎称目录为空；`run.log` 与 `run_manifest.json` 同样不在其中；
  6. 断言 `metrics is None` 或六键全为 `null`（walkforward 崩在中途时 Phase 2 不会执行，故不得出现半填的非空指标），并断言 `git_commit` / `db_path` / `db_used` / `cli_overrides` 等指纹字段已在骨架中（这正是 Phase 1 存在的意义）。

> [!WARNING]
> **实现这两组测试前必须先核既有断言（已实测确认，勿凭猜）**：
> - `backtester/tests/test_run_backtest.py` 的 `test_main_optimize_research_strategy_spec_empty_param_grid`（约 L1500 起）在 `--optimize` 空网格场景下断言的是 **`optimize_report.json` 的两个字段** —— `report["best_params"] == {}` 与 `report["original_params"] == {}` —— 而**没有**断言「生效 `params == {}`」（该用例的策略文件里 `params` 本来就是 `{}`）。因此加入 §1 的空网格护栏后，这两条断言**仍然成立、无需修改**；护栏只影响 `params = opt["best_params"]` 这条覆盖路径，不改变 `optimize_report.json` 的写盘内容。若实现时把护栏写成「连 `optimize_report.json` 也跳过写盘」，则本用例与 §1 的契约都会被破坏 —— 护栏必须只跳过 **`params` 覆盖**。
> - `git_dirty` 在 `tmp_path` 夹具下**会因仓库里的未跟踪文件而报 `True`**（`git status --porcelain` 不区分本次运行与别的脏文件）。因此 manifest 相关测试**不得**断言 `git_dirty is False`；要么不断言该字段，要么只断言它是 `bool` 类型。

> [!WARNING]
> **已知退化路径披露：`--optimize` × blend 策略**
> - **成因**：`BaseBlendAllocationStrategy` 统一以 `param_grid={}` 构造，于是 `grid_search_template` 只产出 `grid_combinations({})` → `[{}]` 这**一个退化候选**。
> - **三重后果**：①网格搜索退化为「只评估原始参数」，`--optimize` 在 blend 上没有任何调优意义；②单候选不存在多重比较惩罚，ERS 只需越过分位阈值与最小换手数，极易判 `trusted`；③`params = opt["best_params"]` 会把参数**整体替换为空 dict `{}`**，静默抹掉 dump 里的全部调优参数（这正好踩中 §1 那条「重新绑定而非叠加」的语义）。
> - **处置结论（采用护栏，不声明「不支持」）**：在 `params = opt["best_params"]` 之前加一道护栏 —— 当 `opt["best_params"]` 为空 dict 时**跳过 `params` 覆盖**，并打印一条 WARNING 级日志（形如 `--optimize produced an empty best_params (template param_grid is empty); keeping original params`）。这样 blend 策略仍可跑 `--optimize`（`optimize_report.json` 照常落盘、ERS 照常评估），但**绝不**因空 `best_params` 而清空生效参数。
> - **对应测试断言**：同文件新增 `test_main_optimize_empty_param_grid_keeps_original_params` —— 用任一 `param_grid == {}` 的模板（blend 策略即可）跑 `--optimize`，断言 ①`optimize_report.json["best_params"] == {}`；②`strategy_dump.json["params"]` 仍等于策略文件的原始 `params`（**非** `{}`）；③`capsys` 捕获的输出含上述 WARNING 文本。

---

### 4. 回测产物入库留档规范 (Committed Artifacts Policy)

> [!IMPORTANT]
> **语义变更（先读这一条，再读下文任何命令）**：`backtests_research/` 下的产物是**长期留存的研究资产**，不是临时文件。原文档把全部输出写在 `/tmp/opencode/bt_*` 下，天然带有「跑完即弃」语义；迁移到仓库内之后，该语义必须同步翻转 —— 每次**真实研究运行**的产物都**随仓库提交入库**，供后续交叉复核、回归对照与审计追溯（`strategy_dump.json` 本身就是自审计核心，见 §1）。
>
> **经评审建议为 `backtests_research/` 增加一条 `.gitignore` 排除条目，已由用户决策否决**：本方案不新增任何忽略规则，产物一律入库。下文是该决策的可执行、可核验版本（含 `git check-ignore` 实证命令、逐产物必提交清单、`INDEX.md` 索引与体积/缓存纪律）。

#### 4.1 不忽略原则 (No-Ignore Principle)

1. `backtests_research/` 及其全部子目录**不得**加入 `.gitignore`，也不得以 `.gitattributes` 的 `export-ignore`、归档分支等方式在提交时被剥离；任何「顺手排除一下」的改动都视为对本规范的破坏，须先经用户裁决。
2. 必须确认 `.gitignore` 中**已有**的规则不会误伤这些产物。当前相关条目为 `**/data/*.csv`、`**/data/*.duckdb*`、`**/results/*.png`、`**/results/*.csv`、`**/results/*.json`、`**/results/strategy_dumps/` —— 它们只匹配名为 `data/` 或 `results/` 的目录段，而本方案的 `--results-dir` 指向 `backtests_research/<ts>/<group>/<strategy>/`，**路径中不含 `results/` 与 `data/` 段**，故不被匹配。
3. 每次新增命令组后必须**实证**而非假定「没被忽略」（期望**零输出**）：

   ```bash
   cd /home/stone/Work/github/quant
   git check-ignore -v backtests_research/<ts>/06_ablate/baseline/chan_four_state_blend/strategy_dump.json
   # 期望：无任何输出 ⇒ 该路径未被任何规则命中。
   # 有输出 ⇒ 输出里会给出命中的 .gitignore 行号与匹配模式，先解决规则冲突，再继续后续命令组。
   ```

4. `git add -f` **不是**允许的手段：它只在当下把文件塞进索引，规则本身仍然踩在上面，下一次（或换一个人执行）就会静默漏提交。要的是「规则不匹配」，不是「这一次强行绕过」。

#### 4.2 每次运行的必提交清单 (Per-Run Committed Set)

下表文件名按 [`backtester/run_backtest.py`](file:///home/stone/Work/github/quant/backtester/run_backtest.py) 与 [`common/plotting.py`](file:///home/stone/Work/github/quant/common/plotting.py) **当前源码实际写入的名字**核准，且刻意按**写入符号**引用而不引行号 —— 行号必然漂移，本仓库已有因硬编码行号导致「文案撒谎而无人被告知」的先例（见 [`docs/version.md`](file:///home/stone/Work/github/quant/docs/version.md) §20 的告警去脆化记录）。

| 产物文件名 | 生成位置（写入符号） | `--mode standard` | `--mode walkforward` | 是否必提交 |
| :--- | :--- | :---: | :---: | :---: |
| `strategy_dump.json` | 本方案 §1 新增的 `_dump_effective_strategy_def` | ✓ | ✓ | **必提交（自审计核心）** |
| `run_manifest.json` | 本方案 §1 新增的两阶段 helper：`_dump_run_manifest`（跑前骨架）→ `_finalize_run_manifest`（跑后收尾覆写） | ✓ | ✓ | **必提交（`INDEX.md` 那行的机器可读底稿，见 §4.3）**；注意其自身 `artifacts` **不包含** `run_manifest.json`（见 §1 Phase 2 取值规则第 5 条） |
| `run.log` | 命令尾部的 `tee "$out_dir/run.log"` | ✓ | ✓ | **必提交** |
| `backtest_equity.csv` | `result["equity_curve"].to_csv(...)` | ✓ | — | **必提交（净值/回撤曲线数据源）** |
| `rebalance_report.csv` | `rebal_df.to_csv(...)` | ✓ | — | **必提交（交易记录）** |
| `backtest_weights.csv` | `result["actual_weights"].to_csv(...)` | ✓ | — | 建议提交（逐日实际持仓，体积小） |
| `walkforward_summary.json` | `write_json_report(summary, ...)`（含 `mean_sharpe_ratio` / `mean_cagr` / `mean_max_drawdown` / `mean_calmar_ratio` / `n_folds` / `deflated_sharpe_ratio` 与逐 fold 明细） | — | ✓ | **必提交（metrics/summary）** |
| `walkforward_report.csv` | `folds_df.to_csv(...)`（逐 fold 指标表） | — | ✓ | **必提交** |
| `walkforward_rebalances.csv` | `wf_trades_df.to_csv(...)`（带 `fold` 列的交易记录） | — | ✓ | **必提交（交易记录）** |
| `walkforward_weights.csv` | `wf_weights_df.to_csv(...)`（带 `fold` 列的逐日权重） | — | ✓ | 建议提交 |
| `comparison_report.json` | `write_json_report(comparison_report, ...)`（alpha/beta/TE/IR/超额 CAGR，walkforward 侧为逐 fold 均值） | 仅 `--baseline-symbol` | 仅 `--baseline-symbol` | **必提交（若生成）** |
| `baseline_equity.csv` | `baseline_result["equity_curve"].to_csv(...)` | 仅 `--baseline-symbol` | — | **必提交（若生成；同区间逐日净值相减的唯一凭据）** |
| `optimize_report.json` | `write_json_report(optimize_report, ...)`（含 ERS 分位、`trusted`、`best_params`） | 仅 `--optimize` | 仅 `--optimize` | **必提交（若生成）** |
| `equity_curve.png` | `plotting.plot_equity_curve(...)` 的默认 `filename` | ✓（`--no-plots` 时无） | —（walkforward 分支不出图） | 建议提交 |

> [!NOTE]
> **「必提交」的三条判据**：①**生效策略定义**（`strategy_dump.json`）—— 没有它，任何一组指标都不可复现，也无法回答「这条曲线到底由哪套参数跑出来」；②**可重算指标的最小数据**（净值序列 + 交易记录 + summary）；③**能解释异常的过程证据**（`run.log`，其中含数据加载告警、`baseline_calendar_mismatch`、`_align_universe` 截断告警等**只出现在日志里**的信号）。PNG 与权重 CSV 属可重建品，故列为「建议」而非「必须」。
>
> **standard 模式没有独立的 metrics JSON**：其标量指标只经 `format_backtest_metrics_summary(...)` 打进 `run.log`，并在带 `--baseline-symbol` 时额外写 `comparison_report.json` —— 因此 standard 组的 `run.log` 是**唯一**能取到 Sharpe/CAGR/MaxDD 的落盘位置，不能以「体积小」为由裁掉。`run_manifest.json` 的 `metrics` 字段因此在 standard 下允许留 `null`（见 §1 该 helper 的设计约束 2）；它的职责是记录本次运行的版本指纹（含 `db_used`：本次运行是否真的读过 `db_path` 那份缓存）与产物清单，而不是替代 `run.log` 充当指标来源。
>
> **`run_manifest.json` 自身不在自己的 `artifacts` 里**：清单不可能从「写之前那一次目录扫描」里如实报出自己的「写之后大小」（Phase 1 骨架 vs Phase 2 收尾两个阶段的大小不同），任何试图把它列进去的实现都只能填一个过期数字。要它的体积请直接 `stat -c %s run_manifest.json`；清单完整性的核验口径是「除 `run.log` 与自身之外，目录内每个文件都在清单里且 `bytes` 与实际 `stat` 相等」（§1 Phase 2 取值规则第 3/4/5 条、§3 该用例第 4 条）。

#### 4.3 `backtests_research/INDEX.md` 交叉复核索引

在 `backtests_research/` **根目录**维护一份纯文本索引，每次新运行（人工或脚本）追加一行，使「哪一次运行、哪一组、哪个策略、什么改动、对应计划哪一条」不必靠翻 commit 猜：

| 时间戳目录 | 实验组 (01–06) | 策略 | 关键指标 CAGR / Sharpe / MaxDD | 本次运行的变更点或消融臂 | 对应 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) 任务条目 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `<YYYYMMDD_HHMMSS>` | `<01_r1_longwin … 06_ablate/<arm>>` | `<strategy_name>` | `<取自 walkforward_summary.json 的 mean_cagr / mean_sharpe_ratio / mean_max_drawdown>` | `<baseline / 某个消融臂 / 本次改动点>` | `<§2.10 B? 或 §4.x 条目>` |

> [!NOTE]
> 上表是**表头模板**，本文档刻意不填任何示例数值 —— 指标格子只允许填该次运行**实际**跑出来的数字（walkforward 组取 `walkforward_summary.json`；standard 组取 `run.log` / `comparison_report.json`），占位符必须替换。**编造或留空的索引行比没有索引更危险**：它会让人以为某次运行已经被复核过。索引未同步 ⇒ 本次运行视为**未完成**，不得提交。
>
> **取值纪律（硬约束）**：写这一行时必须从同目录的 `run_manifest.json` 取值 —— `时间戳目录` / `实验组` / `策略` 直接抄 `run_ts` / `group` / `strategy`，指标列抄 `metrics`（walkforward）或据 `metrics` 为 `null` 而转抄 `run.log`（standard）。**禁止手估、禁止凭记忆填、禁止只读 `run.log` 就顺手把 walkforward 的数字也一起写**。`run_manifest.json` 与 `INDEX.md` 行不一致时，以 `run_manifest.json` 为准并当场修正索引。

#### 4.4 体积与缓存纪律 (Size & Cache Discipline)

> [!WARNING]
> **入库是重量级操作，且历史无法回收**：仓库要长期承载全部运行记录，任何一次误提交都会永久留在 git 历史里（改一次提交 = 重写整条历史，代价远高于当场守规矩）。
> 1. **严禁将 `--cache-dir` 指向 `backtests_research/`，保持 `--cache-dir` 为默认值**：`--cache-dir` 默认使用 `<repo_root>/data`（工作区共享且受版本控制/既有忽略规则保护的行情与特征缓存，离线读取 `<repo_root>/data/cache.duckdb`）。`--results-dir` 仅控制回测产物（报告、净值、交易记录、生效配置与日志）的落盘，**绝不会**向 `--results-dir` 写入 `.duckdb` 或逐 bar 原始行情缓存。**回测命令中切勿传入 `--cache-dir` 或将其重定向至 `/tmp/...`**，否则将脱离仓库既有的 `data/cache.duckdb` 离线数据库，破坏离线可复现性（Offline Reproducibility）并违反 `AGENTS.md` Rule 1。
> 2. **单个运行目录超过约 50MB 即触发裁剪**：只保留 `strategy_dump.json`、`run.log`、summary JSON、交易记录 CSV 与净值曲线数据（`backtest_equity.csv` / `walkforward_report.csv` / `walkforward_weights.csv`），舍弃中间原始数据（逐日全量中间帧、重复导出的中间 CSV 等）。
> 3. 裁剪后**必须**在 `INDEX.md` 的「变更点」列注明「已裁剪」及舍弃了什么，否则后续复核会把「没提交」误读成「当初没跑」。
> 4. 裁剪**不允许**动 `strategy_dump.json` 与 `run.log`：前者是自审计唯一凭据，后者是唯一能解释「为什么这组数字长这样」的过程证据。宁可裁掉全部分析中间品，也不裁这两项。
> 5. **裁剪的审计口径是 `run_manifest.json` 的 `artifacts[].bytes`**：判断「要不要裁」「裁了多少」时以该清单为准，而不是凭 `du -sh` 的粗估或印象；裁剪后对应的 `bytes` 不再等于实际文件大小，须在 `INDEX.md` 注明「已裁剪」并列出被舍弃项（同第 3 条）。

**量级校正（先算清再谈阈值）**：本方案的单次运行产物远比「50MB 安全网」小一个数量级以上 ——

- **walkforward 权重**：约 40 fold × 63 bar × 20 symbol 的稀疏权重表，CSV 在数百 KB 量级；
- **standard 权重**：约 790 bar × 20 symbol，同为数百 KB 量级；
- **净值 / 交易记录**：`backtest_equity.csv`、`rebalance_report.csv` 均在 100KB 级；`strategy_dump.json`、`run_manifest.json`、`*_summary.json` 均在 10KB 级；PNG 视分辨率在数十至数百 KB。

因此**单次运行 CSV 合计约 1–5MB**，50MB 阈值是**安全网而非主要控制手段** —— 正常研究运行既不会触发、也不该拿它当「体积已经合规」的证明。真正的无界增长来自**跨多次运行的累积**，而 git 历史不可回收（删文件不缩小 `.git`）：已提交的 1MB 目录攒到 100 次就是 100MB，且这份体积**永久留在历史里**。

由此约定两条硬纪律：

1. **每次提交只含本次运行目录 + `INDEX.md` 一行**，禁止把多个 run 目录攒成一次大提交（攒提交会把「哪次运行对应哪次复核」彻底打散，也让单次回滚不可能）；
2. **若确需压缩 git 历史**（`filter-repo` / `filter-branch` / 重写分支），必须**单独提出并经用户批准**后执行 —— 历史重写会影响所有协作者的克隆与既有 commit 哈希，绝不能作为日常提交动作的一部分。

> [!WARNING]
> **`--cache-dir` 保持默认这一条（第 1 条）不因体积考虑而松动**：即便 `data/cache.duckdb` 再大，也**不得**为了「省 git 体积」而把它挪进 `backtests_research/` 或 `/tmp`。它本就被既有忽略规则挡在版本控制之外，挪走只会换来「下次跑出来的数不一样」。

#### 4.5 提交信息规范 (Commit Message Convention)

依 `AGENTS.md`「Documentation & coding style conventions」的既有惯例：**terse、全小写、现在时**、一句话说明做了什么，且必须带本次运行的时间戳目录名（无时间戳则跨日复核无法定位是哪一批）：

```bash
# 产物与索引必须落在同一个提交里（分开提交会让「可复核」在最需要时失效：
# 中途中断、rebase 冲突、或"先提交产物、索引下次补"都是真实可能的失败模式）
git add backtests_research/20261025_091500 backtests_research/INDEX.md
git commit -m "add r1 longwin backtest artifacts 20261025_091500"
```

- 不用 `update` / `fix` / `wip` 这类无信息量的动词去描述一次运行；`add <实验组简称> backtest artifacts <时间戳>` 是本仓库的可复用模板。
- 同一次提交**必须**同时包含 `INDEX.md` 的那一行 —— 理由见上一条注释。

> [!NOTE]
> **本规范只改变产物的「处置方式」，不改变任何命令力学**：§2 中提到的 6 组命令的 `--results-dir`、`pfx_of()` 与 `run_arm()` 的消融映射逻辑均**不因本节而改动**（那三处已评审通过）。本节新增的只是「产物进不进 git、怎么索引、怎么控体积」三层约束。
>
> **「6 组命令」的可核验性说明（重要，避免本文档被读成漏了 5 组）**：本文档只完整展示 `run_arm`（即 `06_ablate` 那一组）的命令实体；`01_r1_longwin` / `02_r2a_std` / `03_r2b_oos` / `04_r1_std_cb_compare` / `05_anatomy` 这 5 组的命令实体位于 [`docs/backtest_research.md`](file:///home/stone/Work/github/quant/docs/backtest_research.md) 的 §6.2 / §6.3 / §6.4 / §6.5 / §7（外加 §8 的 `run_arm` 循环，合计 6 组）。它们的 `--results-dir "$BT_ROOT/<cmd_group>/..."` 与 `tee "$out_dir/run.log"` 义务由该文档 §6 / §7 / §8 的迁移改动**继承**，本文档不重复抄写命令 —— 但义务本身同样受本规范约束，不因「写在另一份文档里」而豁免。

---

## 验证计划 (Verification Plan)

### 自动化测试 (Automated Tests)
通过 `tests` 子代理执行 100% 离线测试套件：
```bash
pipeline/.venv/bin/pytest backtester/tests/test_run_backtest.py -q
pipeline/.venv/bin/pytest pipeline/tests/test_config_wiring.py -q
```

> [!NOTE]
> **§4 的留档规范本身不经 pytest 验证** —— 它是文档级产物策略，把「产物必须入库」写成一条 pytest 断言只会制造「已验证」的假象（测试永远为真，因为它读的是同一份 .gitignore）。该政策的可核验性由 §4.1 的 `git check-ignore` 命令与下文手动核验第 3–9 项承担；pytest 只负责 §3 的 `strategy_dump.json` / `run_manifest.json` 功能契约。

### 手动与文档核验 (Manual Verification)
1. 核验 [`docs/backtest_research.md`](file:///home/stone/Work/github/quant/docs/backtest_research.md) 中所有 CLI 命令均已指向 `$BT_ROOT/<cmd_group>/...`（即 `<project_root>/backtests_research/<datetime>/...`），无任何残留 `/tmp/opencode/bt_*` 或外部路径（如 `/home/stone/Work/stock/...`）。
2. 用 `--data-provider synthetic` 在临时冒烟目录（如 `SMOKE_DIR="$(mktemp -d)"`）试跑一次单命令，确认 `<results_dir>/strategy_dump.json` 与回测产物同目录落盘且合并参数无误。**披露**：合成数据冒烟产物不构成研究资产，**不入库**（跑完后执行 `rm -rf "$SMOKE_DIR"` 清理，这是 §4「产物一律入库」的唯一豁免口径）；真实研究运行仍按 §4 全量入库。
3. `git check-ignore -v` 对产物路径**无任何输出**（即未被忽略），且至少覆盖三种深度不同的路径 —— `01_r1_longwin`（2 层）、`03_r2b_oos`（2 层）、`06_ablate/baseline`（3 层）各抽一个叶子 `strategy_dump.json`；任一路径有输出即失败。
4. `git status --porcelain backtests_research | head` 能看到本次新增产物（预期形如 `?? backtests_research/<ts>/...`）；补充反向证据 `git status --porcelain --ignored backtests_research | head` **不得**出现 `!!` 前缀行（`!!` 即「已被忽略」，与 §4.1 直接冲突）。
5. 每个运行子目录均含 `strategy_dump.json` 与 `run.log`：
   ```bash
   cd /home/stone/Work/github/quant
   find "backtests_research/<ts>" -name strategy_dump.json | wc -l
   find "backtests_research/<ts>" -name run.log           | wc -l
   ```
   两个数字**必须相等**，且等于本次运行的叶子运行目录数（缺失即说明某个 `tee` 或落盘分支被跳过）。
6. `backtests_research/INDEX.md` 已追加本次运行行：`grep -c "<ts>" backtests_research/INDEX.md` ≥ 1，且该行的指标列已用实际数值替换占位符（不得留 `<...>` 模板文本）。
7. 体积核验：`du -sh backtests_research/<ts>/*`，任一运行目录超过约 50MB 即须按 §4.4 裁剪，并在 `INDEX.md` 该行注明「已裁剪」及舍弃内容；`strategy_dump.json` 与 `run.log` 不得出现在被裁清单里。裁剪口径以该目录 `run_manifest.json` 的 `artifacts[].bytes` 为准（§4.4）。
8. 「6 组命令」的 `tee` 义务核验（迁移完成后执行）：`git grep -n 'tee .*run\.log' docs/backtest_research.md` 的**命中数必须等于该文档中的回测命令组数** —— 迁移完成时应为 **6 组**（§6.2 / §6.3 / §6.4 / §6.5 / §7 / §8 各一处；§8 内的 `run_arm` 是脚本内循环，**只算 1 处**）。命中数少于组数即说明某组命令仍会把输出甩到终端或 `/tmp`，该组产物无法入库复核。
9. `run_manifest.json` 一致性核验（每批运行后执行）：
   ```bash
   cd /home/stone/Work/github/quant
   find "backtests_research/<ts>" -name run_manifest.json | wc -l   # 必须等于叶子运行目录数（同第 5 项）
   find "backtests_research/<ts>" -name run_manifest.json \
     -exec jq -r '[.run_ts, .group, .strategy, (.metrics.sharpe // "null")] | @tsv' {} +   # 与 INDEX.md 对应行逐字段核对
   ```
   任一处 `run_ts` / `group` / `strategy` 与 `INDEX.md` 该行不一致，以 `run_manifest.json` 为准并当场修正索引（§4.3）。
