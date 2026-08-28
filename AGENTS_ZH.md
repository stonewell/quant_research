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

- 每个策略/配置模板均为 `AllocationTemplate` 的子类（`common/allocation_templates.py`），实现 `generate_weights(universe, params) -> DataFrame`、`explain_weights(params) -> str` 和 `warmup_bars(params) -> int`。两大家族实现了该接口：`common/allocation_templates.py` 中的 9 个静态无参模板，以及 `pipeline/research_strategy/rs/strategy.py` 中 18 个更丰富、由 `StrategyConfig` 驱动的模板（组合预设 + 单资产择时策略）。

- **稀疏权重契约（至关重要）：** `generate_weights` 返回以日期为索引的 DataFrame，其中除**实际再平衡日**外，每一行的所有单元格均为 `NaN`；在再平衡日则包含真实的目标权重。模板**绝不能自行向前填充（forward-fill）其输出**--回测器（`common/allocation_backtester.py`）通过该行是否存在来区分“下达了再平衡指令”与“今天无事发生”，而非通过其数值是否相比前一行发生改变。

- **单元格级 NaN vs. 0.0（至关重要）：** `run_allocation_backtest` 在整个 Frame 上执行 **列向** `sparse_weights.ffill().fillna(0.0)`。在实际再平衡日行内部，某个标的的 `NaN` 单元格**并不意味着“零”**--它会向前漂移继承该标的上一次再平衡时的非 NaN 数值。如果模板的逻辑决定排除或平仓此前持有的某个标的，它**必须在该再平衡日显式写入 `0.0`**，绝不能留为 `NaN`，否则回测器会静默继续持有旧仓位。

- **等效随机搜索 (ERS)** (`common/allocation_search.py`)：胜出的模板/参数通过对比其夏普比率与 N 个随机权重组合来进行验证；仅当其超越设定百分位门槛*且*具备足够再平衡次数时才被标记为 `trusted`。**每次搜索仅在已选出的胜者上运行一次 ERS**--绝不要对每个候选模板分别运行 ERS。

- **因子分类学** (`common/factor_taxonomy.py`)：模板/策略上的标签（如 `relative_momentum`, `mean_reversion`, `volatility_targeting`）为 `strategy_generator` 可选的因子报告平局决胜提供支持--它仅能在顶级候选者非常接近时解决平局，绝不能覆盖在回测夏普比率上明显胜出的模板。

- **切面组合 (Aspect Composition)** (`common/strategy_aspects.py` 针对篮子模板，`pipeline/research_strategy/rs/timing_aspects.py` 针对单资产择时模板)：`strategy_generator` 不再仅挑选单个整体模板。它将多个模板拆解为可复用的组件--*选择 (Selection)* + *加权 (Weighting)* 针对 9 个静态模板；*入场信号 (Entry)* + *出场/风控/仓位 (Exit)* 针对 4 个择时模板--并搜索跨不同来源模板的混合配对（如动量的选股 + 逆波动率的仓位）。胜出的混合策略会在 `strategy.json` 中嵌入 `composite_spec` 块，以便 `backtester` 重建确切实例。

---

## 文档与代码风格规范

本代码库非常注重披露与文档注释--请保持一致，不要删减：

- Port 了已知策略或技术的非平凡函数/类需注明其学术背景（作者、年份、发表期刊），并显式披露相比原论文的任何简化（如“HONEST CAVEAT”、“DISCLOSED APPROXIMATION”注释）。
- 注释解释*为什么 (Why)*，通常会指出当前代码形式所修复的具体历史 Bug--在修改附近代码时请勿将这些注释作为“显而易见”的内容删掉。
- 优先复用现有的共享原语（`common/indicators.py`, `common/allocation_search.py`, `common/covariance.py`, `common/scheduling.py`, `common/testing.py`），避免重复实现已存在的数学逻辑。
- Git 提交信息需保持简洁、小写、现在时态摘要（例如 "add chan pivot", "add denoise"）。
