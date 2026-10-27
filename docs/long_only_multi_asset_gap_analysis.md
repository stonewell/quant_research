# 多资产 Long-Only 指南对照诊断与设计增强建议

> **文档性质**：只读对照诊断 + 设计增强提案，**不是**实施记录。任何任务拆解、优先级、状态勾选以 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.15 为唯一权威（SSOT）。
> **对照基线**：[`docs/multi_asset_long_only_quant_strategy_research.md`](file:///home/stone/Work/github/quant/docs/multi_asset_long_only_quant_strategy_research.md)
> **代码基线**：2026-10-26 工作区 HEAD。文中 `file:line` 为撰写时证据，行号随提交可能漂移，落地前须重新核对。
> **状态**：诊断完成；P0/P1 提案全部未开工，零代码改动、零版本轴变动。

## 1. 结论摘要

指南与仓库同向，但落地不齐。**已具备**：滚动 POC/VAL/VAH 与筹码分布、Garman-Klass 波动率目标、三档回撤熔断、ATR 吊灯止损、资产惰性换手、ADV 冲击/参与度模型、walkforward + DSR + ERS、四阶段实盘票据。

**关键缺口**：①排序区间滞后（rank-band）与 ΔScore 显著性门；②换手惩罚 QP；③行业元数据与中性化；④显式 Core-Satellite 资金 sleeve；⑤日频 VWAP/IVOL/1M-1W 因子；⑥**回测与实盘存在两套冷却/惰性实现，状态机不共享**——这是当前最大的结构性风险。

## 2. 差距矩阵

| 指南章节 | 状态 | 证据 | 关键差距 |
|---|---|---|---|
| §1 双轨架构 | 部分 | `rs/multi_strategy_alpha_book.py:131-137` | `core_satellite` 是 4 条收益流 pod 的逆波动叠加，不是同一组合内 ETF-Beta/个股-Alpha/现金的资本切分 |
| §2 ETF Beta 轮动 | 部分 | `blend_base.py:48-71,74-98,1391-1410` | 无 `0.6·R10/σ10 + 0.4·R20/σ20` 打分；行业为手写 22 只票 + 代码前缀伪类；无 EMA5/EMA20 死叉硬门 |
| §3 Alpha 截面 | 部分 | POC `structural_factors.py:166-209`；VWAP 仅日内 `intraday_features.py:258-268`；IVOL 仅 `residual_momentum_strategy.py:50-119` | 日频 VWAP 斜率、低 IVOL 筛选、1M-1W、行业中性 Z-Score 全缺 |
| §4 风险预算 | 部分 | `multi_strategy_alpha_book.py:21-24` | 无 35/20/25/20 sleeve 预算；无"卖 ETF 补个股流动性"能力 |
| §5 Anti-Whipsaw | 部分 | 滞后 `blend_base.py:979-986`；惰性 `allocation_templates.py:314-398`；冷却 `position_lifecycle.py:138-160` | rank-band、ΔScore>0.35σ、两日法则、价格再入场均缺；冷却状态机回测不接线 |
| §6 组合优化 | 部分 | `allocation_templates.py:172-202,96-170` | 无换手项、行业 ±3% 约束、冷却 mask、0.5% 剪枝（现有 `min_weight_change` 只是事后过滤） |
| §7 执行 | 部分 | `allocation_backtester.py:109,476-486,525-578`；`run_live_strategy.py:14-15` | 无 10:00-11:15/13:45-14:30 窗口建模；实盘无 TWAP 切片标注 |

## 3. 逐模块诊断

**§1 双轨架构.** 流水线层面的解耦是充分的，但"双轨"在本仓库实际是**收益流合成**：4 个 pod 各自产出权重再按逆波动分配，任何一条腿亏损都会触发全组合降杠杆。后果是大盘下跌时无法执行"赎回宽基 ETF 降 beta、保留个股 alpha"这一指南的核心动作，只能整体砍仓，Alpha 腿被 Beta 风险污染。

**§2 Beta 引擎.** 波动率目标与熔断已成熟（`target_bull_vol=0.24`/`target_bear_vol=0.12`，回撤档 0.10/0.15/0.20）。但动量打分未做波动率标准化，高波动行业会长期霸榜；行业分类覆盖仅 22 只票，未覆盖标的退化为 `sec_`+代码前两位（`blend_base.py:66-71`），使 `max_assets_per_sector=2` 的节流阀在回测中形同虚设——**回测呈现的行业分散是统计假象**。

**§3 Alpha 引擎.** POC 突破已实现且参数完整（lookback=60、30 bins、VA=70%），但 VWAP 只在 5m 日内特征里，且 `enable_intraday_5m=False` 默认关闭；IVOL 只有"残差动量"这一正向用法，没有指南要求的低异质波动**负向筛除**；1M-1W 缺失意味着买入点恰好落在短期追高回踩区。行业中性化完全缺失：`scoring.py:42-65` 只有百分位排名，无 winsorize、无行业内 Z-Score，选股容易退化为行业赌博。

**§4 风险预算.** 无 sleeve 预算。现有波动率缩放对全组合等比例生效，危机时 Alpha 与 Beta 同比例被砍，违背"只降 Beta"的原则。

**§5 Anti-Whipsaw.** 已有 regime 推力滞后（0.62/0.58，默认关）与资产惰性（卖出门槛 = 0.60×买入阈值）。但 `_select_momentum_topn`（`strategy_aspects.py:143`）每个调仓日重算全新 top-N，持有的边缘标的会被无差别换出；`position_lifecycle` 虽实现了六态机与冷却，**却只被 `run_live_strategy.py:2117` 调用**（`position_lifecycle.py:725` 有明文注释），回测走的是 `blend_base` 的另一套 `asset_cooldown_until`。这直接制造了三类真实损失：边缘排名抖动带来的佣金损耗、单日假破位误杀、以及 A 股 T+1 下卖错后无法当日买回。

**§6 组合优化.** 仅"最小方差 + 长多 + 权重和=1"。缺少换手项时，噪声协方差会给出高换手解，摩擦吞掉 alpha；缺少冷却 mask 时，优化器会把刚止损的票重新买回，使 §5 的冷却形同虚设。

**§7 执行.** 四种成交价模式、ADV 参与度上限、Almgren-Chriss sqrt 冲击、A 股 T+1/100 股整手/涨跌停都已具备。缺的是窗口建模（日频原生，只有 `intraday_cutoff_time=14:15` 静默门）与实盘切片指令。注意：**日频回测无法真实复现 10:00-11:15 窗口**，只能做已披露的近似，不能宣称"已实现指南的执行时点"。

## 4. 设计增强建议

### P0-1 统一冷却状态机（闭合 parity 缺口，对应 §2.15 `M1.1`）

- **落点**：扩展 `common/position_lifecycle.py`；`common/allocation_backtester.py` 新增 `lifecycle_config`，在 `sparse_weights.ffill()` 之后逐 bar 调用 `step_position_lifecycle`；`common/strategy_spec.py` 透传配置。
- **新增状态字段**：`last_exit_price`、`exit_streak`、`reentry_vwap_lookback=20`。
- **再入场规则**：冷却期满不直接置 FLAT，须满足 `price > last_exit_price×(1+0.02)` **或** `close>VWAP20 且 vol>vol_ma20`。
- **两日法则**：ATR/VAL 破位先置 `PENDING_EXIT` 并累计 `exit_streak`，连续 2 日收于 `min(MA20, VAL)` 下方才 SELL；破位日 `vol<0.60×vol_ma20` 豁免（缩量回踩）。
- **配置键**：`lifecycle_price_reentry_pct=0.02`、`lifecycle_two_day_rule=false`、`lifecycle_break_vol_shrink_pct=0.60`。
- **NaN vs 0.0（关键）**：所有冷却/止损/破位卖出必须在调仓行写**显式 `0.0`**。`position_lifecycle` 内部已返回 `target_weight=0.0`，但一旦接线时写成 NaN，列级 ffill 会保留陈旧持仓——正是 AGENTS.md 记录过两次的同类 bug。
- **验证**：`pipeline/parity_harness.py` 逐 bar 比对 shares 与 weights；合成"假破位反弹""缩量回踩""T+1 隔日再入场"三类场景。
- **版本义务**：改 `common/` 无需 bump BASE_VERSION；若顺带改 `blend_base` 则必须 `1.6.0→1.7.0` 并同步两份 versioning 测试与 `docs/version.md`。

> **2026-10-26 收敛升级**：本节的「统一冷却状态机」已扩展为 [`docs/position_policy_consolidation_design.md`](file:///home/stone/Work/github/quant/docs/position_policy_consolidation_design.md) 的四阶段内核收敛（`development_plan.md` §2.16 `M4.1`–`M4.4`）；`M1.1` 的验收并入 `M4.2`，本节的价格再入场、两日法则与显式 `0.0` 规则作为 `AssetPolicy` 的 policy chain 条目保留。

### P0-2 rank-band 滞后 + ΔScore 显著性门（对应 §2.15 `M1.2`）

- **落点**：`common/strategy_aspects.py` 新增 `RankBandSelectionAspect` 包装 `_select_momentum_topn`，并挂入 `ATOMIC_TEMPLATE_ASPECTS` / 复合模板。
- **规则**：新仓 `rank ≤ buy_pct`；持仓维持至 `rank > sell_pct`；换仓需 `S_new − S_held > 0.35×σ_score`，否则否决。
- **配置键**：`buy_band_pct=0.10`、`sell_band_pct=0.30`、`delta_score_sigma=0.35`。
- **稀疏交互**：跌出 sell 带写 `0.0`；落在维持区间则该标的整行不出现，保持稀疏契约。
- **验证**：构造排名边缘震荡的合成序列，断言换手下降且 Sharpe 不劣化；`build_composite_candidates` 单测覆盖新包装器。
- **版本义务**：仅加包装器无需 bump；若改 `common/allocation_templates.py` 内模板，则 bump 该类 `version` + `--dump-strategies` + `docs/version.md`。

### P0-3 换手惩罚 Long-Only QP（不引入 cvxpy，对应 §2.15 `M1.3`）

- **目标**：`min wᵀΣw − λwᵀμ + γ‖w−w₀‖₁`，约束 `0≤w≤0.20`、`Σw≤1`、行业偏离 ≤±3%、冷却标的 bounds 设 `(0,0)`、解后 `|Δw|<0.005 → w=w₀`。
- **scipy-only 实现**：L1 在 0 点不可微，推荐 `|Δw|≈sqrt(Δw²+1e-6)` 平滑后用 `SLSQP`（与现有 `_min_variance_weights` 同款，`allocation_templates.py:197`）。ε 过小会导致近零梯度病态、过大则偏置换手；备选方案是辅助变量线性化（变量翻倍、凸性更干净），仅在平滑法不收敛时启用。
- **失败回退**：`not success → γ=0 的最小方差解 → 等权`，沿用现有 `:198-199` 的回退哲学。
- **配置键**：`opt_turnover_penalty=0.0`（默认关，保证 bit-identical）、`opt_single_cap=0.20`、`opt_sector_dev=0.03`、`opt_prune=0.005`。
- **验证**：`γ=0` 时断言与 `_min_variance_weights` 逐元素一致。
- **版本义务**：bump `MinimumVarianceAllocation.version`；不触 `blend_base` 则无 BASE_VERSION 变更。

### P1-4 行业元数据与中性化（对应 §2.15 `M2.1`）

- **新文件** `common/sector_metadata.py`：JSON `{symbol: sector}`，随 `docs/universe/*` 存放或由 `common/testing.py` 合成注入 `sector` 列（全程离线）。
- **缺失处理**：未知标的退回前缀伪类或 `"other"`；单成员行业 σ=0 时 z=0，对齐 `scoring.py:80-83` 的退化处理。
- **落点分工**：`sector_neutral_zscore` 在 `scoring.py:score_universe` 内使用；**±3% 偏离预算放 QP 约束**，不要放进打分层，否则预算无法跨 sleeve 生效。
- **生存者偏差**：map 必须含历史全横截面（含退市/换行业），合成生成器不得做幸存者过滤。
- **配置键**：`sector_map_path=null`、`enable_sector_neutral=false`、`sector_dev_budget=0.03`。
- **验证**：`compute_rank_ic`（`factor_analyzer.py:60`）比较中性化前后 IC；断言行业暴露 `|dev|≤0.03`。

### P1-5 显式 Core-Satellite `SleeveAllocator`（对应 §2.15 `M2.2`）

- **接口**：新建 `common/sleeve_allocator.py`，`class SleeveAllocator(AllocationTemplate)`，实现三个抽象方法。
- **合成**：`w_total = Σ_s budget_s · w_sleeve_s`，再走 `_cap_and_deroute_to_cash`（`allocation_templates.py:436`）保证 `Σ≤1`，现金腿吸收余量。
- **危机规则**：只 `w_beta *= sleeve_crisis_scale`（默认 0.5），Alpha 腿不动，现金吸收释放的敞口。
- **稀疏/0.0**：合并后未选中标的写显式 `0.0`，再用 `_sparse_from_daily`（`:401`）压缩回稀疏。
- **为何不扩 `blend_base`**：它是"收益流 + 正交化 + 波动率目标"语义，扩展会污染 6 个已部署 blend 的契约并被迫 bump BASE_VERSION；新建顶层模板可同时被 `strategy_generator`（`extra_template`）与 `backtester` 消费，零回归。
- **配置键**：`sleeve_beta_budget=0.25`、`sleeve_alpha_budget=0.55`、`sleeve_cash_budget=0.20`、`sleeve_crisis_scale=0.5`。
- **验证**：断言 `Σbudget==1`；危机情景断言 beta 降、alpha 不变、`Σ≤1`。

### P1-6 三因子补齐（因子层，不进模板，对应 §2.15 `M3.1`）

`common/daily_factors.py` 新增 `price_to_vwap_ratio`、`vwap_slope`、`ivol_residual`、`momentum_1m_1w`，由 `scoring.py` 消费。理由：当前因子属于 `instrument_selection`/`factor_analyzer` 层，写进模板会制造第三份重复逻辑。

- 公式：`vwap20 = Σ(close·vol)/Σvol`；`ivol = std(OLS残差)`；`mom_1m_1w = close[t−5]/close[t−20]−1`。
- 配置键：`factor_vwap_lookback=20`、`factor_ivol_lookback=63`、`factor_mom_1m_1w_skip=5`。
- 验证：断言因子只用当日及以前数据（无 look-ahead）。

### P1-7 执行现实主义（对应 §2.15 `M3.2`）

- 日频无法算窗口内 VWAP，只能用**已披露近似**：`fill = daily_vwap × (1+window_premium)`，premium 由仓库已有的 5m DuckDB 缓存离线标定。
- `allocation_backtester.py` 新增 `execution_window: Optional[str]`；实盘 ticket 增加 `twap_slices`/`execution_window` 字段（仅标注，不改下单）。
- `max_volume_participation` 默认仍为 `None`，只新增可选 `0.015` 预设。
- 验证：默认 `null` 路径必须 bit-identical（沿用现有 `next_vwap`/`intraday_twap` 测试）。

### P1-8 两日法则交互（并入 P0-1 / `M1.1`）

与 ATR 吊灯止损正交：ATR 破位只触发 `PENDING_EXIT`，两日收线确认才真正 SELL；与 15/21 bar 的 tier 冷却也正交——**tier 冷却管组合敞口，个股两日法则管单票出场**，二者不可合并成一个阈值。

### P2 候选（本次未做代码级论证，未登记任务）

1. `docs/black_list.md` 目前是纯文本，无任何 Python 读取（排除靠手改 universe txt），容易与代码漂移 → 编译为可加载的 exclusion 清单。
2. 把 `SleeveAllocator` 的 beta/alpha 预算做成随 IC/波动自适应，并纳入 walkforward 搜索。
3. sector map 扩展至港股/美股并版本化。
4. 用 5m 数据标定窗口溢价与 TWAP 切片参数。

## 5. 实施路线图

| 阶段 | 内容 | 涉及文件 | 验收 |
|---|---|---|---|
| Phase 1（引擎，风险中高） | M1.1 状态机接线 + M1.2 rank-band | `position_lifecycle.py`、`allocation_backtester.py`、`strategy_aspects.py`、`selection_band.py`(新) | parity 逐 bar 一致；默认路径 bit-identical；ERS 重算 |
| Phase 2（引擎+研究，风险中） | M1.3 QP、M2.1 行业、M2.2 SleeveAllocator | `allocation_templates.py`、`sector_metadata.py`(新)、`sleeve_allocator.py`(新)、`scoring.py` | γ=0 与旧解一致；预算守恒；中性化后 IC 不降 |
| Phase 3（研究配置，风险低-中） | M3.1 三因子、M3.2 执行窗口 | `daily_factors.py`(新)、`factor_analyzer.py`、`allocation_backtester.py`、`run_live_strategy.py` | 因子因果性；默认执行路径 bit-identical |

引擎变更（M1.1/M1.3/M2.1/M2.2）需同步：`common/tests/test_strategy_versioning.py`、`pipeline/research_strategy/tests/test_blend_strategy_versioning.py`、`--dump-strategies`、`docs/version.md`；任务清单按 SSOT 规则登记在 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.15。

## 6. 风险与反模式

1. **旋钮爆炸与 ERS/DSR 记账失真**：每加一个滞后/冷却/换手参数，等效搜索自由度上升，`n_random_search=200`、`ers_percentile_threshold=0.90` 的显著性会被稀释。DSR 的 `n_trials` 必须把网格、aspect 组合、ERS 抽样全部计入，否则"通过 DSR"是假阳性。
2. **同日信号与成交的 look-ahead**：`close` 模式下若用当日收盘选股又按当日收盘成交，任何新因子都会虚高。新因子必须 `shift(1)` 或强制 `next_open`/`next_vwap` 成交。
3. **A 股 T+1 与整手约束**：M1.1 的"卖出后冷却再买入"在 T+1 下最早只能隔日；"卖 beta 买 alpha"同日切换在回测里会违反 T+1，必须用 `china_trading=True` + `next_open` 组合验证。
4. **第三次重复实现**：冷却/惰性目前已有 `position_lifecycle`、`blend_base` 资产冷却、`apply_asset_inertia` 三处相近逻辑。新方案必须**收敛**，不能在新文件里再写一套，否则 parity 鸿沟永久化。
5. **不做的事**：不引入 cvxpy（scipy 平滑 L1 足够）；不把 sleeve 塞进 `blend_base`；不允许任何负权重（指南与 AGENTS.md 均为 long-only）。

## 7. 未验证点

- `financial_api.py:1718` 的日频隐含 VWAP 字段名与缺失处理未逐行确认，M3.1 复用前需实读。
- `check_live_portfolio_health.py` 的 15/21 bar tier 冷却行号仅来自检索清单，未逐行核对。
- `metrics.py` DSR 的 `n_trials` 是否已把 aspect composition 试验数算全，需落地前确认。
- `common/testing.py` 的合成生成器是否支持注入 `sector` 列未验证，M2.1 依赖此项。

---

> **任务索引**：本文所有可执行项已拆解为 `M1.1`–`M3.2` 共 7 项，登记于 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.15（全部 `[ ]` 未开工）。本文不维护状态；状态只在开发计划中更新。
