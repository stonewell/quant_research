[ [English](AGENTS.md) | 简体中文 ]

# AGENTS_ZH.md

适用于在本项目仓库中工作的任何 Coding Agent（Claude Code, Codex, Cursor 等）的指南。
本文件故意比 `README_ZH.md` 和各项目的 `README_ZH.md`/`SCHEMAS_ZH.md` 更短——它捕获了 Agent 为不破坏代码所需了解的规范和陷阱，而非完整的 CLI 参考。请先阅读 `README_ZH.md` 以了解端到端流水线全景和数据流图。

---

## 仓库结构与定位

这是一个模块化的量化交易研究/回测工作区，组织为两个项目组以及位于仓库根目录的共享基础设施：
- **`pipeline/`** -- 核心研究/回测流水线项目组，共享一个 `uv` 环境：
  `research_strategy` (因子研究) -> `instrument_selection` (标的筛选) -> `pattern_mining` (可选的拐点模式挖掘) -> `strategy_generator` (配置策略搜索)，加上 `fundamental_screener` (真实基本面买卖筛选，未接入流水线)。`pipeline/run_pipeline.py` 通过子进程串联前 4 个阶段加上 `backtester`。
- **`ml/`** -- 基于 ML/DL 的策略项目，每个拥有**独立隔离**的 `uv` 环境（更重、版本敏感的依赖栈，若与 `pipeline/` 合并会产生冲突）：目前为 `bnn_forecaster` (AutoBNN 概率预测)。
- `common/` 与 `backtester/` 驻留在仓库根目录，由两个项目组共享，**无独立的 `pyproject.toml`/虚拟环境**——由运行它们的项目组虚拟环境通过 `sys.path` 注入调用。

参阅 `README_ZH.md` 的“工作区架构与数据流”章节了解结构图及各阶段的输入输出 Schema。

---

## 环境配置与运行命令

```bash
cd pipeline && uv sync                             # pipeline/ 的共享环境 (research_strategy,
                                                    # instrument_selection, pattern_mining,
                                                    # strategy_generator, fundamental_screener)
cd ml/bnn_forecaster && uv sync                    # bnn_forecaster 独立的隔离环境

# 从 pipeline/ 内部运行（或在任何位置通过 pipeline/.venv/Scripts/python.exe 运行）：
uv run python <project>/run_*.py --help            # 每个 pipeline 项目的 CLI
uv run python run_pipeline.py --data-provider synthetic --universe SPY QQQ ...
```

`common/` 和 `backtester/` 没有独立的虚拟环境——使用适合您所触及策略的项目组虚拟环境运行它们。两个项目组的虚拟环境都可以运行 `common` 和 `backtester` 自身的完整测试套件。

在 Windows 上，如果当前 Shell 中不可用 `uv run`，虚拟环境的解释器位于 `<group>/.venv/Scripts/python.exe`。

---

## 测试策略 -- 在运行或编写测试前必读

- **绝不在测试或示例命令中使用真实市场数据或网络访问。** 每个测试套件均通过 `SyntheticDataProvider`/`common/testing.py` 的合成 OHLCV 生成器 100% 离线运行。编写的任何 CLI 示例默认使用 `--data-provider synthetic`；仅当用户明确要求时才使用 `yfinance` 真实数据运行。
- 每个项目拥有独立的 `tests/` 目录（`common/tests`, `pipeline/research_strategy/tests`, `pipeline/instrument_selection/tests`, `pipeline/pattern_mining/tests`, `pipeline/strategy_generator/tests`, `pipeline/fundamental_screener/tests`, `pipeline/tests`, `backtester/tests`, `ml/bnn_forecaster/tests`）。这些目录均没有 `__init__.py`，且多个项目包含同名测试文件（`test_allocation_templates.py`, `test_indicators.py` ...），因此从仓库根目录直接运行裸 `pytest`（或同时传入多个目录）会因同名文件导致 `import file mismatch` 错误。两种解决方式：
  - 一次运行一个目录（各项目 README 所示）：`pytest common/tests -q`。
  - 或传入 `--import-mode=importlib` 在单个调用中同时运行多个/所有目录：`pytest common/tests pipeline/strategy_generator/tests backtester ... -q --import-mode=importlib`。

---

## 核心领域模型 -- 修改模板/策略代码前必懂

- 每个策略/配置模板均为 `AllocationTemplate` 的子类（`common/allocation_templates.py`），实现 `generate_weights(universe, params) -> DataFrame`、`explain_weights(params) -> str` 和 `warmup_bars(params) -> int`。两大家族实现了该接口：`common/allocation_templates.py` 中的 9 个静态无参模板，以及 `pipeline/research_strategy/rs/` 中 53 个更丰富、由 `StrategyConfig` 驱动的模板（组合预设、单资产择时、缠论结构分析、宏观因子复合、多策略 Alpha 账簿、WorldQuant 公式化 Alpha 模型以及 `pipeline/research/blend/` 下的模块化混合配置策略）。

- **稀疏权重契约（至关重要）：** `generate_weights` 返回以日期为索引的 DataFrame，其中除**实际再平衡日**外，每一行的所有单元格均为 `NaN`；在再平衡日则包含真实的目标权重。模板**绝不能自行向前填充（forward-fill）其输出**--回测器（`common/allocation_backtester.py`）通过该行是否存在来区分“下达了再平衡指令”与“今天无事发生”，而非通过其数值是否相比前一行发生改变。

- **单元格级 NaN vs. 0.0（至关重要）：** `run_allocation_backtest` 在整个 Frame 上执行 **列向** `sparse_weights.ffill().fillna(0.0)`。在实际再平衡日行内部，某个标的的 `NaN` 单元格**并不意味着“零”**--它会向前漂移继承该标的上一次再平衡时的非 NaN 数值。如果模板的逻辑决定排除或平仓此前持有的某个标的，它**必须在该再平衡日显式写入 `0.0`**，绝不能留为 `NaN`，否则回测器会静默继续持有旧仓位。

- **等效随机搜索 (ERS)** (`common/allocation_search.py`)：胜出的模板/参数通过对比其夏普比率与 N 个随机权重组合来进行验证；仅当其超越设定百分位门槛*且*具备足够再平衡次数时才被标记为 `trusted`。**每次搜索仅在已选出的胜者上运行一次 ERS**--绝不要对每个候选模板分别运行 ERS。

- **因子分类学** (`common/factor_taxonomy.py`)：模板/策略上的标签（如 `relative_momentum`, `mean_reversion`, `volatility_targeting`）为 `strategy_generator` 可选的因子报告平局决胜提供支持--它仅能在顶级候选者非常接近时解决平局，绝不能覆盖在回测夏普比率上明显胜出的模板。

- **切面组合 (Aspect Composition)** (`common/strategy_aspects.py` 针对篮子模板，`pipeline/research_strategy/rs/timing_aspects.py` 针对单资产择时模板)：`strategy_generator` 不再仅挑选单个整体模板。它将多个模板拆解为可复用的组件--*选择 (Selection)* + *加权 (Weighting)* 针对 9 个静态模板；*入场信号 (Entry)* + *出场/风控/仓位 (Exit)* 针对 4 个择时模板--并搜索跨不同来源模板的混合配对（如动量的选股 + 逆波动率的仓位）。胜出的混合策略会在 `strategy.json` 中嵌入 `composite_spec` 块，以便 `backtester` 重建确切实例。

- **模块化混合配置策略 (Modular Blend Allocation Strategies)** (`pipeline/research/blend/`)：构建在 `BaseBlendAllocationStrategy` (`pipeline/research/blend/blend_base.py`) 之上的机构级多策略混合系统，包含 `ChanRiskManagedBlendStrategy`、`ChanFourStateBlendStrategy`、`ChanCrisisShieldBlendStrategy`、`ChanDualHybridBlendStrategy` 与 `GenericBlendStrategy`。统一规范了动态政体加权、Garman-Klass 极值波动率目标化、附带自愈冷却的多阶回撤熔断、自适应惯性再平衡 (`apply_asset_inertia`)、行业节流阀及市场宽度冲力现金动用机制。全库统一将默认配置标准化为 `blend_weighting_mode="rolling_ic_ir"`（基于 Harvey-Liu-Zhu $t \ge 2.0$ 显著性门槛与 60 日滚动 IC-IR 贝叶斯收缩自适应）以及 `blend_enable_orthogonalization=true`（Löwdin 对称正交化算子 $W^* = W(W^T W)^{-1/2}$ 消除多策略共线性），由 `blend_6` 至 `blend_13` 的实证回测验证，实现了最高 37.35% CAGR、1.702 夏普与 0.9400 DSR 统计显著 Alpha。

- **混合策略子策略接线与裸名 `resolve_param` 冲突（至关重要——曾引发真实 Bug，已于 2026-10-21 修复）：** 每个混合策略的 `get_sub_strategies(universe, params)` 通过 `BaseBlendAllocationStrategy.resolve_param` 解析选择参数来构建其子策略实例。`resolve_param` 的最终回退是裸名 `getattr(cfg, name)`，因此任何与*全局* `StrategyConfig` 字段同名（且中间无 `blend_<name>` 拦截）的参数名都会静默泄漏该全局字段的值。具体受害者是 `wq_alpha_id`：`chan_four_state_blend` 与 `chan_risk_managed_blend` 解析了 `resolve_param(p, "wq_alpha_id", ...)`，泄漏了全局 `StrategyConfig.wq_alpha_id=101`（独立 `WorldQuantAlphaStrategy` 的默认值），将 `ChanFourStateExecutionStrategy` / `ChanCompositeStrategy` 静默替换为 `WorldQuantAlphaStrategy(#101)`。**规则：混合策略的 `get_sub_strategies` 必须通过策略前缀键（`p.get("cfsb_wq_alpha_id", ...)` / `p.get("crb_wq_alpha_id", ...)`）解析子策略选择参数，绝不使用与全局配置字段冲突的裸名。** 前缀选择参数（`cfsb_third_strategy_type`、`crb_third_strategy_type`）也已在 `strategies_config.json` 中显式声明。完整审计见 `docs/version.md` §7 与回归测试 `test_blend_sub_strategy_class_resolution_regression`。

- **实盘部署平台与工作站 (Live Trading Deployment & Web Station)** (`web/livetrading/`, `scripts/`)：
  实盘运营遵循两阶段标准化执行协议：
  - **Stage 1（宏观健康度门禁）** 通过 `scripts/check_live_portfolio_health.py`：评估自最高历史净值（HWM）以来的峰谷回撤、多阶熔断触发器、市场宽度（$\ge 30\%$）及动量冲力（$\ge 60\%$）。
  - **Stage 2（实盘交易单生成）** 通过 `scripts/run_live_strategy.py`：强制执行四阶段层级：第一阶段【先卖出】释放购买力 $\rightarrow$ 第二阶段【后买入】按整手取整建仓 $\rightarrow$ 第三阶段【维持持仓】惰性过滤 $\rightarrow$ 第四阶段【闲置资金归集】入现金工具（`BIL`/`511880`/`GC001`）。全面支持 55+ 种研究策略、9 种静态配置模板及自定义 JSON 策略。
  - `web/livetrading` 提供交互式 Web 交易工作站，支持实时持仓与现金管理、每日不可变运行快照归档（`data/runs/<date>/`）及自定义股票池管理。区域核心卫星池位于 `docs/universe/`（A 股、美股、港股）。

---

## 核心量化工程规范 (Core Quantitative Engineering Rules)

1. **100% 离线测试规范 (100% Offline Testing)**：
   - 单元测试与示例命令中严禁使用真实市场数据或网络请求。
   - 所有测试套件必须通过 `SyntheticDataProvider` 或 `common/testing.py` 的合成 OHLCV 生成器 100% 离线运行。
   - 命令行参数默认值必须为 `--data-provider synthetic`。
2. **任务与路线图唯一事实来源 (SSOT)**：
   - 所有量化工程任务拆解、状态追踪、优先级队列及执行路线图统一集中在 `docs/development_plan.md` 中维护。
   - `docs/` 下的各模块设计与架构文档必须链接至 `docs/development_plan.md` 查看任务清单和执行状态。
3. **稀疏权重契约与单元格 NaN vs 0.0 (Sparse Weights Contract & Cell-level NaN vs. 0.0)**：
   - `generate_weights(universe, params)` 返回的权重矩阵仅在实际调仓日记录目标权重，其余非调仓日必须保持为 `NaN`。
   - 模板内部绝不进行前向填充 (`ffill()`)。
   - 在实际调仓日行中，标的若需平仓或回避风险必须显式赋予 `0.0`；若保留为 `NaN`，回测引擎的列级前向填充会将上期持仓权重隐式延续。
4. **等价随机搜索唯一校验 (Single ERS)**：
   - ERS 验证（`common/allocation_search.py`）在单次搜索中**仅执行一次**，且仅对最终选出的冠军策略/参数组合进行校验，绝不逐个候选模板运行 ERS。
5. **股票池标的剔除与黑名单档案规范 (SSOT 于 `docs/black_list.md`)**：
   - 任何时候当 Agent 建议或执行从任何股票池/ETF池/投资组合中剔除某个标的或资产时（无论是因为单手超高价资金失真、结构性失血负Alpha、财务造假/ST风险、恶性价格战内卷、还是高摩擦频繁假突破等原因），必须在 `docs/black_list.md` 中用中文建立并维护完备的剔除档案与深度原因记录，以供后续轮次研究核验与永久避坑。
6. **模型分级选择与思考优先级规范 (Model Selection Policy)**：
   - 在需要深度思考、架构设计、数学推导与全景审计选择 Heavy / Pro 级别模型时，严格遵循以下优先级阶梯：
     - **第一优先级：Claude**（若有可用额度/Credit，优先使用 Claude 3.7 / 3.5 Sonnet 进行深度推理）。
     - **第二优先级：Gemini Pro**（若 Claude 额度耗尽或不可用，接替使用 Gemini 2.5 / 1.5 Pro）。
     - **第三优先级：降级回退至 Flash**（仅在 Pro 级别额度完全受限或耗尽时，才回退至 Gemini Flash / Flash-Medium）。
7. **策略修改与语义化版本递增规范 (Strategy Version Upgrade & Dual-Axis Decoupling)**：
   - 仓库内所有量化策略与资产配置模板必须严格维护其语义化版本（`VERSION` / `version` 与 `BASE_VERSION`）。
   - **每次修改策略必须同步递增版本号 (Mandatory Version Bump on Strategy Edit)**：
     - **底座引擎升级 (`BASE_VERSION`)**：任何时候当修改、扩展或优化 `BaseBlendAllocationStrategy`（`pipeline/research/blend/blend_base.py`）或 `AllocationTemplate`（`common/allocation_templates.py`）的基础算法、风险覆盖或算子逻辑时，**必须递增 `BASE_VERSION`**（向后兼容的功能新增递增 Minor，如 `1.0.0` $\to$ `1.1.0`；重大不兼容变更递增 Major，如 `1.1.0` $\to$ `2.0.0`）。
     - **具体子类策略升级 (`version`)**：任何时候当修改某个具体策略类的信号逻辑、调仓机制、默认参数或子策略成分时（如在 `pipeline/research/blend/*.py`、`pipeline/research_strategy/rs/*.py`、`common/allocation_templates.py` 中），**必须递增该策略的 `version`**（如 `1.0.0` $\to$ `1.1.0`）。
     - **四步联动固化要求 (Four-step Synchronized Requirements)**：递增版本号时，必须同步执行以下四项动作：
       1. 同步更新单元测试断言（`pipeline/research_strategy/tests/test_blend_strategy_versioning.py` 与 `common/tests/test_strategy_versioning.py`）；
       2. 运行 `uv run python pipeline/research_strategy/run_research_strategy.py --dump-strategies` 刷新 `strategy_dumps/*.json` 持久化元数据；
       3. 确保实盘工单（`run_live_strategy.py` 导出的 `live_trading_ticket.json`）记录的 `strategy_version` 与 `base_version` 比特级对齐；
       4. **版本变更日志持久化 (SSOT 于 `docs/version.md`)**：每次策略版本（`version`）或底座引擎版本（`BASE_VERSION`）发生变更时，必须在 `docs/version.md` 中用中文追加记录该版本的变更日志与摘要（包含变更日期、策略名称/底座标识、旧版本号 $\to$ 新版本号、算法/信号/风控核心变动点、参数增减、以及配套单测验证结论），确保策略历史沿革具备不可篡改的审计追踪能力。


---

## 文档与代码风格规范

本代码库非常注重披露与文档注释--请保持一致，不要删减：

- Port 了已知策略或技术的非平凡函数/类需注明其学术背景（作者、年份、发表期刊），并显式披露相比原论文的任何简化（如“HONEST CAVEAT”、“DISCLOSED APPROXIMATION”注释）。
- 注释解释*为什么 (Why)*，通常会指出当前代码形式所修复的具体历史 Bug--在修改附近代码时请勿将这些注释作为“显而易见”的内容删掉。
- 优先复用现有的共享原语（`common/indicators.py`, `common/allocation_search.py`, `common/covariance.py`, `common/scheduling.py`, `common/testing.py`），避免重复实现已存在的数学逻辑。
- Git 提交信息需保持简洁、小写、现在时态摘要（例如 "add chan pivot", "add denoise"）。
- **任务拆解与路线图唯一权威中心 (`docs/development_plan.md`)：** 整个代码库的所有定量研究、基础设施升级、高频特征提取、多流派策略研发及实盘交易护栏工程的任务拆解、状态跟踪、优先级队列与执行路线图，必须且仅在 `docs/development_plan.md` 中集中维护。`docs/` 下的各专项研究与架构设计方案仅承载理论建模、代码审计与模块详设，任务清单与进度编排统一查阅 `docs/development_plan.md`。

---

## 已知问题与数据源避坑指南 (Known Issues & Data Provider Gotchas)

- `HierarchicalRiskParityAllocation` 在某些极小或退化的合成标的池上可能抛出 `IndexError`。`_portfolio_score` 已捕获该异常并将其夏普比率降级为 `-inf`，不会中断运行。
- **BaoStock `ResultData` 分页死循环 OOM 与单进程顺序下载规范 (`common/financial_api.py`)：**
  - **OOM 与首页丢失根因：** 在 `baostock/data/resultset.py` 中，当单次查询跨越多个分页（`> 10,000` 行）时，`ResultData.get_data()` 内部会调用 Pandas 2.x 已移除的 `df.append(...)`；且在抛出 `AttributeError` 之前，`self.next()` 已将 `rs.data` 中的第 1 页数据覆盖为第 2 页。此外，`ResultData.next()` 在请求下一网络分页前会检查 `if self.cur_row_num < len(self.data): return True`。若调用方通过 `data_list.extend(rs.data)` 批量提取整页数据而未同步更新游标 `rs.cur_row_num = len(rs.data)`，`rs.cur_row_num` 将永远停留在 `0`，导致 `while rs.next():` 陷入无限死循环并在数秒内耗尽系统内存（OOM）。
  - **强制解决方案：** 在 Pandas 2.x 环境下绝不对真实 `baostock.data.resultset.ResultData` 调用 `rs.get_data()`；使用 `data_list.extend(rs.data)` 批量读取首页及 `while rs.next():` 后续分页时，**必须在每次 `extend` 后立即执行 `rs.cur_row_num = len(rs.data)`** 推进游标（或逐行调用 `rs.get_row_data()`），且分钟线查询须保持按 `<= 180` 自然日分块请求。
  - **严禁多进程/并发下载：** 绝不对 BaoStock 使用多进程（`multiprocessing`）或多线程并发下载，以免触发服务端封禁或 Socket 阻塞；必须使用单进程顺序下载（复用持久会话 `keep_logged_in=True`），并按单标的粒度增量写入 `DuckDBCache`。

