# 持仓策略内核收敛设计（冷却 / 惰性 / 生命周期三合一）

> **文档性质**：架构设计规范，**不是**实施记录。任务拆解与状态以 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.16 为唯一权威（SSOT）。
> **代码基线**：2026-10-26 工作区 HEAD；`file:line` 为撰写时证据，落地前须重核。
> **结论先行**：新建 `common/position_policy.py` 作为唯一内核；`position_lifecycle.py` 降级为 live adapter；`blend_base` 与 `allocation_templates` 的旧实现分四阶段退役。**迁移期间允许改变的行为：无。**

## 0. 问题不是"重复"，是"同词异义"

当前有四处重叠实现：`common/position_lifecycle.py`（6 态 FSM）、`blend_base.py`（资产冷却 + 自适应惰性 + 入场确认）、`allocation_templates.py`（`apply_asset_inertia`）、`run_live_strategy.py`（阈值 resolver + 再入场冷却）。

重复本身可以容忍；不可容忍的是**同名参数在不同引擎里含义不同**。例如 `normal_cooldown_bars` 在 FSM 里是 bar、在实盘是交易日、在 blend 里是 `master_index` 绝对位置；`stop_cooldown_bars` 在 FSM 里是**个股**止损冷却（5），在 blend 里是**组合回撤自愈**冷却（21）。这意味着一份配置在回测与实盘会得到不同的再入场节奏，而现有 parity harness 只证明"归档重放一致"，覆盖不了"同一配置在两条路径上的语义差"。

## 1. 权责清单

| 实现 | 权重决策拥有者 | 状态拥有者 | 状态内容 | 时钟 | 关键默认值 |
|---|---|---|---|---|---|
| `position_lifecycle.py` | `step_position_lifecycle` → `LifecycleDecision`（每-symbol，`:271-761`） | `AssetLifecycleState`（`:163-226`） | 6 态 `PositionState`（`:20-27`）+ `held_bars/entry_streak/exit_streak/cooldown_remaining/entry_price/peak_price/current_weight/broker_weight/last_exit_type` | bar（每次调用递减；休市冻结 `:312-337`） | `guard_latency=2`, `min_holding_bars=2`, `normal_cooldown_bars=2`, `stop_cooldown=5`, `inertia=0.05`, `inertia_relative_ratio=0.0`, `hard_stop=0.08`, `atr_mult=4.5`, `vol_surge=1.5`, `breadth=0.60`（`:139-160`）（2026-10-27 复核更正：`PositionLifecycleConfig` 现为 `min_holding_bars=2`、`normal_cooldown_bars=2`，与实盘 `--reentry-cooldown-bars=2` 已一致）（2026-10-27 复核更正：现行为 `PositionState` `:25`、`AssetLifecycleState` `:186-248`、`step_position_lifecycle` `:293-800`；`scripts/run_live_strategy.py:1558-1559` 与 `:1565` 经实测仍准确，不必改） |
| `blend_base.py`（日循环） | `generate_weights` 内部 per-asset stop/cooldown/entry-confirm/expansion/inertia（`:874-964,1412-1559`） | 裸字典 `asset_cooldown_until/asset_entry_price/asset_peak_price/entry_signal_bars/expansion_signal_bars/holding_age/last_buy_bar`（`:832-850`） | 无 FSM，靠计数 + 绝对位置 `t` | `master_index` 位置 t（交易日 bar） | `min_weight_change=0.05`（`:580`）, `entry_threshold=min_weight_change`（`:581`）, `min_holding=2`（`:582`）, `min_entry_bars=guard_latency=2`（`:583`）, `asset_stop_cooldown=5`（`:606`）, `normal_cooldown=reentry_cooldown=1`（`:607`）, **`stop_cooldown_bars=21`（组合回撤自愈，`:589`）**, `asymmetric_sells=(prefix=="cdhb")`（`:609`）, `stop_atr_mult=4.5`（`:605`） |
| `allocation_templates.py` | `_step_asset_inertia`（每-symbol 冻结，`:217-311`）+ `apply_asset_inertia`（`:314-398`） | `holding_age/last_buy_bar` 字典（`:369-370`） | 无 FSM，年龄 + 最近买入 bar | row index t（bar） | `min_weight_change=0.02`（`:316`）, `asymmetric_sell_ratio=0.60`（`:322/231/407`）, `entry_threshold=None`（`:320`）, `min_holding_bars=0`（`:321`） |
| `run_live_strategy.py` | 复用 `step_position_lifecycle`（`:2117-2130`），前置 resolver 定阈值 | 冷启动重建 `AssetLifecycleState`（`:2058-2101`） | 同上 FSM + `last_exit_date` | **交易日**（`check_missing_trading_days`，`:762-763`） | `resolve_trade_inertia_threshold` 默认 `0.05`（`:303-320`）；`min_entry_bars/min_holding_bars` 默认 2（`:323-344,347+`）；`--reentry-cooldown-bars=2`（`:1070-1075`）；`normal=2, stop=max(5,2)=5`（`:1558-1559`）；`inertia_relative_ratio` 默认 `0.0`（`:1565`） |

## 2. 冲突表

| 概念 | 冲突点 | 后果 |
|---|---|---|
| **cooldown 同名三义** | `normal_cooldown_bars`：FSM=1（`:143`）vs 实盘 `--reentry-cooldown-bars`=2（`:1558`）vs blend=1（`:607`）；单位：bar（FSM 递减 `:340-341`）vs 交易日（`:762-763`）vs 绝对位置 `asset_cooldown_until=t+N`（`:956,1575-1577`） | 再入场节奏在回测/实盘各差 1 根（2026-10-27 复核更正：FSM 已对齐为 2（`common/position_lifecycle.py:161`），此条「各差 1 根」的冲突**已消除**；现存语义差仅剩 blend 默认 `normal_cooldown_bars=1`（`pipeline/research/blend/blend_base.py:607`）与 bar/交易日/绝对位置三种单位差异） |
| **`stop_cooldown_bars` 撞名** | FSM 的 5 = 个股止损后再入场冷却（`:144,390`）；blend 的 21 = **组合回撤自愈**冷却（`:589,1004`），per-asset 另名 `asset_stop_cooldown_bars=5`（`:606,956`） | 同键跨引擎同值异义，极易误改 |
| **inertia 阈值三套默认** | FSM=0.05（`:145`）；blend `min_weight_change=0.05`（`:580`）；templates=`0.02`（`:316`）；`resolve_trade_inertia_threshold` 优先 `min_weight_change` 再 `inertia_threshold`（`:313-316`） | 同一策略换引擎阈值即变 |
| **"门槛"与"确认期"混排** | `min_entry_bars`（确认期）与 `entry_threshold`（建仓门槛）在 resolver 里并排解析（`:1539,1542-1547`） | 语义被稀释，调参易错 |
| **asymmetric sell 两套公式** | templates 硬编码 `asymmetric_sell_ratio=0.60`（`:254,322`），仅 `asymmetric_sells` 开启时生效；实盘用 `inertia_relative_ratio`：`max(threshold, ratio×cur_w)`（`:505-509`），`cfsb=0.30`（`strategies_config.json:401`） | cfsb 回测无不对称卖、实盘有 0.30 —— **同一策略两套卖出行为** |
| **tenure lock vs 硬止损** | `can_sell = market!=china or held_bars>=min_holding_bars`（`:365`）；止损 fast path **显式豁免** tenure（`:382-388`） | 顺序若重排，会重新引入"T+1 锁住止损"的尾损 bug |
| **emergency 语义相反** | blend/templates：`emergency_mask` 直接返回 `ideal_w`（`:251-252`）→ 可 BUY；FSM：`is_emergency_halt` 强制 SELL（`:371,382-425`） | 同名开关一进一出 |
| **0.0 的三种含义** | 稀疏层：`NaN`=无指令、`0.0`=平仓；FSM 层：`target_weight=0.0` **不表示平仓**（MIN_ENTRY/PENDING_ENTRY/休市冻结都写 0.0，`:599-617,691-702,320-337`），只有 `action=="SELL"` 是平仓（`:120-135`） | 转换责任不清 → phantom-anchor |

## 3. 不可丢失的独有能力

- **FSM**：`PENDING_ENTRY` 确认 streak（`:668-703`）；`PENDING_EXIT` + exit streak（`:427-469`）；`CANCEL_PENDING` 取证（`:704-748`）；量价 + breadth 双 fast path（1.5x AND 60%，`:619-666`）与 2.5x 独立突破（`:637`）；硬止损 -8% / ATR 4.5x；T+1（`:365`）；休市冻结 + `blocking_reason`。
- **blend**：自适应惰性（vol-scaled，clip 0.02–0.08，`:841-848`）；自适应 ATR 乘数（3.5–7.5，`:892-896`）；结构 ZG/VAL 吊灯（`:899-911`）+ NPOC/3S 止盈（`:928-949`）；entry/expansion 双确认（`:1419-1493`）；sector throttle + incumbent 保护（`:1269-1350`）；组合回撤断路器 + 自愈（`:1000-1017`）；vol targeting（`:1392-1410`）；thrust 现金部署（`:1168-1390`）。
- **templates**：sells-first（`:286-291`）、买力 = freed cash（`:293-306`）、残差 → `cash_proxy`（`:308-309`）、cap；`last_buy_bar` 使增量加仓不重置 tenure（`:273`）；`entry_threshold` 对新入场豁免惯性（`:256-258`）。
- **live**：全策略阈值 resolver（`:303-320`）；交易日口径的 exit cooldown（`:600-767`）；`should_anchor_reentry_cooldown`（SELL + 零股，`:802-843`）；整手归零 → COOLDOWN（`:2391-2415`）；SWEEP/RELEASE 现金（`:2020-2027`）。

## 4. 分层：必须拆成三层

**`AssetPolicy`（per-symbol，纯函数）**：`step(state, raw_target_weight, ctx, cfg) -> Decision`。无墙钟、无 IO，只管单标的的持有/退出/再入场。

**`PortfolioSequencer`（组合级，仅现金有状态）**：sells-first、买力、残差 → `cash_proxy`、权重上限、显式 `0.0` 写出、sector throttle。它消费 `AssetPolicy` 的决策集合，产出组合级目标。

**`SparseProjector`（稀疏投影）**：把 decision 转成稀疏帧。写回规则必须唯一：

| 情形 | 稀疏帧动作 |
|---|---|
| `action` ∈ {HOLD, HOLD_FILTERED} 且 target 未变 | 不写该行（NaN，保持稀疏） |
| `action == "SELL"` 且 target == 0.0 | 写**显式 `0.0`** |
| `action` ∈ {BUY, SELL} 且 target ≠ 上一目标 | 写 target |
| `CANCEL_PENDING` / `COOLDOWN_BLOCKED` | 不写（NaN），**不得**写 0.0 |

**为什么不能合并成一层**：`apply_asset_inertia` 的"卖单先释放现金、买单受 freed cash 限制、残差归现金"是跨资产约束（`:286-309`），单标的 decision 无法表达；而 FSM 是单标的决策但被 ticket builder 消费。混层的结果就是现在的 blend：per-asset 与组合级逻辑写在同一个日循环里（`:1524-1563`），无法复用也无法单测。

## 5. 统一 API 草案

| 类型 | 字段（名:类型=默认） |
|---|---|
| `PositionState`(enum) | FLAT / PENDING_ENTRY / HELD_GESTATION / HELD_ACTIVE / PENDING_EXIT / COOLDOWN（保留 6 态） |
| `AssetState` | symbol:str；state:PositionState=FLAT；held_bars:int=0；entry_streak:int=0；exit_streak:int=0；expansion_streak:int=0；cooldown_remaining:int=0；entry_price:float=0.0；peak_price:float=0.0；current_weight:float=0.0；broker_weight:float=0.0；last_exit_type:Optional[str]=None |
| `PolicyContext` | price:float；atr:Optional[float]=None；vwap:Optional[float]=None；volume:Optional[float]=None；ma20_volume:Optional[float]=None；market_breadth:Optional[float]=None；market:str="china"；is_market_open:bool=True；is_emergency_halt:bool=False；is_portfolio_deleverage:bool=False；structural:dict={} |
| `Decision` | action:str；target_weight:float；blocking_reason:BlockingReason=NONE；next_state:PositionState；reason:str；detail:dict={}（**任何字段不得为 NaN**） |
| `PositionPolicyConfig` | guard_latency_bars:int=2；min_holding_bars:int=1；normal_cooldown_bars:int=1；asset_stop_cooldown_bars:int=5；inertia_threshold:float=0.05；inertia_relative_ratio:float=0.0；asymmetric_sell_ratio:float=0.60；asymmetric_sells:bool=False；entry_threshold:Optional[float]=None；hard_stop_loss_pct:float=0.08；atr_multiplier:float=4.5；volume_surge_multiplier:float=1.5；breadth_thrust_threshold:float=0.60 |

**policy chain 固定顺序（先短路者胜）**：

1. 休市冻结（`not is_market_open`）→ 短路，不评估、不改状态；
2. 止损 / emergency 清算（最高优先级，**豁免 tenure 与 inertia**）→ SELL + COOLDOWN + `asset_stop_cooldown_bars`；
3. cooldown gate（只挡入场，不挡持有）；
4. 建仓门槛 `min_entry_threshold`；
5. 入场确认 streak（`guard_latency_bars`）+ fast path；
6. 平仓确认 streak（`raw_target == 0`）；
7. inertia（绝对值门槛 + 相对 trim 门槛）；
8. tenure lock（只作用于 SELL 方向；第 2 步已豁免止损）。

**三条不变式**：
1. long-only：`target_weight >= 0`，组合 `sum <= 1`（由 `PortfolioSequencer` 保证，不由 per-symbol 保证）；
2. `Decision` 无 NaN；`blocking_reason != NONE` 时 `action` 必须是"未成交"类，**绝不把非 SELL 提升为平仓**；
3. 幂等与单调：相同 `(state, inputs, cfg)` 重跑得到相同 `Decision`；`held_bars` 持有期非降、`cooldown_remaining` 非增。

## 6. 内核放哪

**新建 `common/position_policy.py`，不扩 `position_lifecycle.py`。** 理由：`position_lifecycle` 的 docstring 宣称跨 backtest + live（`:1-7`），但代码自述"backtester 不用它"（`:725-728`），事实上已是 live 专用门面；硬扩只会把 6 态 FSM 的 live 语义污染成"所有策略都必须有 PENDING_EXIT"。（2026-10-27 复核更正：L2.48 已让 `backtester/run_backtest.py` 经 `simulate_position_lifecycle`（`common/position_lifecycle.py:1026`）复用此模块，「backtester 不用它」前提已部分过时；字面「`step_position_lifecycle` 不被 backtester 直接调用」仍为真）

处置：`position_lifecycle.py` 保留为 **live adapter** —— `step_position_lifecycle` 改为薄封装调用 `AssetPolicy`，`PositionState` / `Decision` / `BlockingReason` 枚举迁到 `position_policy.py`，旧 import 处转发，ticket builder 与现有单测继续消费同一类型。

## 7. 冷启动重建

- 可重建：`current_weight/broker_weight` 来自 `load_holdings`（`run_live_strategy.py:1629-1634`）；`entry_price/peak_price` 由 `holding_bars` 与历史 close 推断（`:2068-2071,2086-2087`）；`held_bars` 由 `resolve_holding_bars` 回溯；`cooldown_remaining` 由交易日差与 `--reentry-cooldown-bars` 计算（`:2078-2085`）。
- **不可恢复**：真实成交价与成交日期（只能用历史 close 近似，`loss_pct` 与 peak 会失真）；确认期进行到第几根（`entry_streak/exit_streak` 未持久化，缺省为 0）。
- 规则：冷启动时 `entry_streak/exit_streak/expansion_streak = 0` 并标 `cold_start=True`；状态只允许落在 `{FLAT, HELD_ACTIVE, COOLDOWN}`，**禁止伪造** `PENDING_ENTRY/PENDING_EXIT/HELD_GESTATION`；`entry_price` 缺失时用 `price` 兜底并标 `entry_price_approx=True`。

## 8. 迁移（bit-identical 门禁）

| 阶段 | 动作 | feature flag | 回滚 | 验收 |
|---|---|---|---|---|
| 1 extract | 新建 `common/position_policy.py`（`AssetPolicy`/`PortfolioSequencer`/`SparseProjector`/`PositionPolicyConfig`）；把 `_step_asset_inertia` 的 per-symbol 逻辑抽成 `AssetPolicy.inertia_step()`，sells-first/cash/emergency 抽成 `PortfolioSequencer.sequence()`；**不动任何调用方** | 无（纯新增、死代码） | 删文件 | 单测逐行等价于 `_step_asset_inertia` |
| 2 backtest adapter | `blend_base.generate_weights` 日循环改调内核（`use_inertia` 分支 `:1534-1559`、`_apply_portfolio_deleverage` `:1561-1563`）；`_sparse_from_daily` 改调 `SparseProjector`；`apply_asset_inertia` 改为委托 | `blend_use_policy_kernel`（默认 `False`） | flag=False 走旧路径 | 差分 harness：全 seed 下 `generate_weights` 逐 cell 的 NaN/0.0/值一致 |
| 3 live adapter | `run_live_strategy` 在 `step_position_lifecycle` 调用点（`:2117-2130`）前插入内核；`position_lifecycle` 降级为 adapter；ticket builder 消费同一 `Decision` | `live_use_policy_kernel`（默认 `False`） | flag=False 走旧 FSM | live archive replay 逐行 `(action, target_weight, blocking_reason)` 一致 |
| 4 delete | 删除 6 态 FSM 主体、`apply_asset_inertia/_step_asset_inertia` 旧实现、`resolve_trade_inertia_threshold` 等重复 resolver；保留 legacy key 映射 | 无（前三阶段全绿后） | `git revert` | 全量 pytest + `--dump-strategies` 重建后 `git diff` 无漂移 |

**默认翻转**：flag 默认 `True` 是独立提交，必须在其前一步的差分证明合入之后；任何一位漂移即回滚，不与语义改动混在同一提交。

## 9. 差分与验收

- **随机差分**：`np.random.default_rng(seed)` 扫 0..N，构造 state/price/target 序列，断言 old（`step_position_lifecycle` / `_step_asset_inertia`）与 new（`AssetPolicy`）逐 bar 的 `(action, target_weight)` 相同。
- **golden runs**：(a) 静态模板 `equal_weight` 走 `_sparse_from_daily`；(b) `chan_four_state_blend` 的 `no_inertia`/`baseline` 两档；(c) 25 份 `live_trading_ticket.csv` 归档回放，重算 `should_anchor_reentry_cooldown` 与冷却。
- **NaN 与 0.0 分开断言**：不得把 `NaN == 0.0` 当作相等；稀疏帧要逐位置比较"是否有行"与"行内值"。
- **常驻防线**：`test_policy_parity.py` 在 CI 内旧/新双跑；`--dump-strategies` 重建后 `git diff strategy_dumps/` 为空即证无漂移。

## 10. 允许改变的行为

**无。** 迁移期间唯一目标是 bit-identical。任何语义改动（统一 0.60 与 relative 的公式、改默认值、重排 policy 顺序、合并 `stop_cooldown_bars` 双义）都必须**另开获批任务**，先登记 `docs/development_plan.md`，不得搭车。

## 11. 版本义务

- 改 `pipeline/research/blend/blend_base.py` → `BASE_VERSION 1.6.0 → 1.7.0`（向后兼容），同步 `common/tests/test_strategy_versioning.py:181` 与 `pipeline/research_strategy/tests/test_blend_strategy_versioning.py:42`。
- 改 `common/allocation_templates.py` → `AllocationTemplate` 无 file-level `BASE_VERSION`（**不要发明**），只 bump 被改类的 per-class `version`。
- 改具体策略逻辑 → bump 该类 `version`。
- 统一执行：`uv run python pipeline/research_strategy/run_research_strategy.py --dump-strategies` 重建 dumps；`docs/version.md` 追加中文 changelog；`docs/development_plan.md` 更新状态。

## 12. 反模式（明确不要做）

1. **把静态模板塞进 6 态 FSM**：backtest 没有 `PENDING_EXIT`/`HELD_GESTATION` 的日频语义，强加确认期会凭空改变换手。
2. **让内核认识稀疏 DataFrame**：内核只产 `Decision`；稀疏松紧由 `SparseProjector` 负责，否则重蹈 phantom-anchor。
3. **把组合级现金逻辑塌进 per-symbol policy**：sells-first / 买力 / 残差是跨资产约束，单标的表达不了。
4. **取最严阈值当"统一"**：`max(min_holding)`、`max(cooldown)`、`min(inertia)` 都会改换手与再入场节奏，那是改行为，不是收敛。
5. **一个 60 字段大 config**：sector throttle、thrust、Löwdin、vol targeting 与 per-symbol 生命周期无关，应拆 `PositionPolicyConfig`（单标的）+ `PortfolioConfig`（sequencer）+ 策略自有配置。
6. **删除 `blocking_reason` / `is_liquidation_event` 的防误判**：`action=="SELL"` 是唯一平仓真值，这条防护是 load-bearing。

## 13. 未验证点

- 未确认 `cfsb`/`cdhb` 是否在 `strategies_config.json` 显式设置 `asymmetric_sells`；"回测无不对称卖、实盘有 0.30"这一分歧是基于默认值推导，未逐条核对 55 个策略配置。
- `cdhb` 是否覆盖 `asymmetric_sells` 默认值（`blend_base.py:609`）未读子类确认。
- `common/market_clock.py` 的 trading-day 与 bar 时钟换算只据调用点推断，未核对函数体。
- `resolve_holding_bars` / `resolve_exit_cooldown` 的 archive 扫描边界未逐行核对。
- `min_weight_change=0.02` 是否存在第三方调用者依赖，需在阶段 1 前全仓 grep 调用点。
- `stop_cooldown_bars` 双义的完整调用面未穷尽，改名（如 `drawdown_stop_heal_bars`）前需确认无 portfolio 语义消费者。

---

> **任务索引**：本文可执行项拆为 `M4.1`–`M4.4`，登记于 [`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) §2.16（全部 `[ ]` 未开工）。本文不维护状态。
