# 量化对齐取证审计报告：基于 Vidovich & Zerjal (2024/2026) 理论框架
**理论文献**: *'Where Backtests and Live Trading Part Ways: Six Defects from One Intraday System'* (Alex Vidovich & Zivana Zerjal)  
**审计目标**: `quant` 代码库 (`common/`, `pipeline/`, `backtester/`, `scripts/`, `web/livetrading/`, `docs/`)  
**审计级别**: 生产级量化系统取证与架构对齐 (Production-Grade Forensic Audit)  
**审计日期**: 2026年10月19日  

---

## 摘要与理论背景 (Executive Summary)

在量化金融经典文献《回测与实盘的分道扬镳：来自日内系统的六大缺陷》(*Where Backtests and Live Trading Part Ways: Six Defects from One Intraday System*) 中，作者 Alex Vidovich 与 Zivana Zerjal 揭示了一个残酷的行业现象：**常规的回测统计摘要指标（年化夏普比率、收益率、最大回撤等）往往极易掩盖底层严重的结构性分歧与系统性失真**。

为了穿透这种黑盒伪装，作者设计了著名的**“对齐测试床 (Parity Harness)”**——构建双轨回放系统，将生产环境记录的逐笔行情与账户状态逐 Tick 灌入历史回测引擎，对信号输出、订单指令、成交回报与现金变动做**严格的二进制级等价性断言**。该研究最终提炼出横跨数据源、算法代码与配置管理的**六大经典缺陷**：
1. **Defect 1: 缺省数据源 (The Default Data Feed)**：未锁定数据源导致的开盘成交量坍塌与量纲错配。
2. **Defect 2: 除权历史与未除权成交 (Dividend-Adjusted History vs. Unadjusted Fills)**：在连续平滑的前复权线上算信号，在离散真实的交易所未除权盘口撮合。
3. **Defect 3: 幽灵数据与未来信息 (Ghost Data in the Live Path / Lookahead)**：日内截断时点不对称、向后填充导致的未来函数污染。
4. **Defect 4: 超越账户物理约束的仓位估算 (Sizing Past Account Limits)**：假设无限购买力、碎股精细化、跌停板无法卖出却算作可用资金（假死透支）。
5. **Defect 5: 时钟、时区与日历错配 (The Clock / Timezone Mismatch)**：非交易日/周末运行污染持仓周期与防反复抽搐冷却周期。
6. **Defect 6: 僵尸配置 (Dead Configuration)**：在配置文件或命令行中声明，但在执行路径中从未生效的虚荣参数。

本报告对当前量化工程进行了逐行代码取证与深度审计，全面比对历史已修补缺陷、当前存活风险点及后续工程防御方案。

---

## 一、 Defect 1: 缺省数据源 (The Default Data Feed)

### 1.1 论文理论剖析
在论文所述案例中，系统在缺少显式配置时默认回退到单交易所数据源（如 BATS/EDGX），由于缺失主交易所（NYSE/NASDAQ）的集合竞价与开盘交叉盘（Opening Cross），导致开盘时段成交量出现**灾难性坍塌（Volume Collapse at Market Open）**，彻底摧毁了 VWAP 指标与流动性参与率模型。

### 1.2 本地代码取证与漏洞排查

#### 1. 东方财富 `f56` 成交量“手 vs 股”百倍量纲坍塌漏洞
- **代码位置**: [`common/financial_api.py:1704`](file:///home/stone/Work/github/quant/common/financial_api.py#L1704) (`EastMoneyDataProvider.fetch_ohlcv`)
- **取证详情**: 东方财富日内 5 分钟 K 线接口 `fields2="f51,f52,f53,f54,f55,f56,f57,f58"` 中，`f56` 原生单位是**“手”（1 手 = 100 股）**，而本系统（BaoStock、Fuyao、SyntheticDataProvider）全局约定 `Volume` 单位必须严格为**“股”**。
- **修复核验**:
  ```python
  # 必须乘以 100 换算为股数，否则成交量出现 99% 的灾难性坍塌
  vol = float(parts[5]) * 100.0
  amount = float(parts[6]) if len(parts) > 6 else float("nan")
  ```
  若漏乘 `100.0`，系统在通过 `amount / vol` 反算日内 VWAP 时，价格将被**放大 100 倍**，且量比突破指标（`volume_breakout_mult`）将完全失效。
- **残余风险**: 若未来引入新的 Web 爬虫或三方 API 时未经过数据层标准化包装，此类量纲陷阱仍有重现风险。

#### 2. BaoStock 分页游标死循环与内存泄露
- **代码位置**: [`common/financial_api.py:1164-1195`](file:///home/stone/Work/github/quant/common/financial_api.py#L1164-L1195)
- **修复核验**: BaoStock 的 `ResultData` 在 Pandas 2.x 中会触发 `df.append()` 崩溃，且其 `rs.next()` 会重写第一页。当前代码已强制在每页追加后显式推进游标 `rs.cur_row_num = len(rs.data)`，并在 `AGENTS.md` 中立为绝对底线红线，杜绝了单连接 OOM 崩溃。

#### 3. 合成数据布朗桥日内成交量严格守恒
- **代码位置**: [`common/data.py:552-560`](file:///home/stone/Work/github/quant/common/data.py#L552-L560)
- **修复核验**: 合成数据采用抛物线 U 型日内成交量权重（早盘尾盘放量、午盘缩量），并强制推导机器精度守恒补偿：`V[-1] += V_d - V.sum()`，确保全日日内成交量之和精确等于日线成交量。

---

## 二、 Defect 2: 除权历史与未除权成交 (Dividend-Adjusted History vs. Unadjusted Fills)

### 2.1 论文理论剖析
回测在连续除权（前复权/后复权）线上评估 Alpha 信号，但实盘订单必须在交易所**未除权真实撮合价格**上挂单成交。
- **失真 1**: 用后复权（`hfq`，茅台价格上万元）去算实盘下单股数，导致买入股数缩水数十倍，资金严重闲置；
- **失真 2**: 前复权（`qfq`）在发生分红送配后，历史所有数据均会发生位移。持久化缓存（如 DuckDB）若未检测到除权漂移，会导致历史与今日增量行情“断层跳空”。

### 2.2 本地代码取证与漏洞排查

#### 1. 前复权历史漂移自愈机制
- **代码位置**: [`common/financial_api.py:850-865`](file:///home/stone/Work/github/quant/common/financial_api.py#L850-L865) (`FuyaoDataProvider`)
- **取证详情**: Fuyao 接口已具备自动除权漂移检测。当发现本地缓存的最新收盘价与远程前复权收盘价偏差超过 `1e-4` 时，自动计算缩放比例 `scale = rem_c / loc_c`，并对历史本地缓存做全量自愈缩放。
- **当前存活漏洞 (Rule R4)**: 储存在 `DuckDBCache` 中的 5 分钟日内 K 线，一旦标的发生大比例分红除权，历史日内数据仍保留旧的前复权基准，需要依赖元数据表强制触发全量失效重拉。

#### 2. 实盘订单股数计算的复权类型约束
- **代码位置**: [`scripts/run_live_strategy.py:1356, 1406`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py#L1356)
- **取证详情**: 实盘脚本当前取 `universe[sym]["Close"].iloc[-1]` 作为成交价格。在前复权（`qfq`）模式下，最新一条 K 线的复权价恰好等于交易所真实最新价，因此整手截断（`sym_lot = 100`）在正常交易日计算无误。
- **致命告警**: 若用户在 CLI 误传 `--adjustflag 1`（后复权 `hfq`），该价格将变成数百上千元，导致计算出的下单股数发生断崖式暴跌，实盘执行完全瘫痪。

---

## 三、 Defect 3: 幽灵数据与未来信息 (Ghost Data in the Live Path / Lookahead)

### 3.1 论文理论剖析
幽灵数据包括两类：
1. **回测中的未来信息（Lookahead）**：回测在计算信号时，不经意间调用了未来才会产生的数据（如向后填充、全天收盘价提前用于盘中决策）；
2. **实盘截断时点不对称**：实盘在 14:15 必须敲定并挂单，而回测却使用 15:00 已经定格的全日收盘价与收盘集合竞价，回测坐享 45 分钟的未来确定性。

### 3.2 本地代码取证与漏洞排查

#### 1. Alpha101 VWAP `.bfill()` 跨越 6 年的历史未来函数灾难 (历史 P0 缺陷 T0.2)
- **代码位置**: [`pipeline/research_strategy/rs/alpha101_factors.py:112-121`](file:///home/stone/Work/github/quant/pipeline/research_strategy/rs/alpha101_factors.py#L112-L121)
- **取证详情**: 在提交 `26dbd67` 之前，代码中存在致命语句：
  ```python
  # 致命未来函数：2020-2025 年无 5 分钟 VWAP 时，直接向后填充！
  vw = df["VWAP"].ffill().bfill()
  ```
  在仅有 2026 年日内数据的标的上，`.bfill()` 竟将 **2026年1月5日** 的真实 VWAP 价格，倒灌填满了 2020 年至 2025 年整整 6 年的历史空白！导致回测中的 Alpha 策略在 2020 年根据 2026 年的价格买卖股票，产生了荒谬的虚假超额。
- **修复核验**: 提交 `26dbd67` 已彻底取缔 `.bfill()`，改为严格无偏的时序因果代理：`vw = df["VWAP"].fillna(typical_price)`，从根源上杜绝了数据穿越。

#### 2. 日内 14:15 执行截断 vs 回测 15:00 收盘价错配 (规则 R1/R6)
- **代码位置**: [`scripts/run_live_strategy.py:1072`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py#L1072)
- **取证详情**: A 股日内全天共 48 根 5 分钟 K 线。第一阶段宏观体检在 **14:00 (第 36 根)** 触发，第二阶段实盘调仓在 **14:15 (第 39 根)** 触发，尾盘尚余 9 根 K 线（占下午成交量的 18.75% 及收盘集合竞价）未发生。
- **当前存活风险**: 基础回测器 `common/allocation_backtester.py` 默认使用 15:00 全天 Close。实盘与回测若要实现严格 Parity 对齐，回测阶段必须使用截断到 14:15 的特征集进行决策（待 `T2.1` 完全打通）。

---

## 四、 Defect 4: 超越账户物理约束的仓位估算 (Sizing Past Account Limits)

### 4.1 论文理论剖析
量化回测往往假定无限流动性与完美的资金即时清算。但在真实交易中：
- 跌停板股票由于缺乏对手盘，挂单无法成交，卖出资金实质冻结；
- 若算法依然将跌停卖单的“预期回笼资金”计入购买力，将在后续买入步骤引发**严重的资金穿仓透支（Cash Overdraft）**。

### 4.2 本地代码取证与漏洞排查

#### 1. 跌停板引发双重穿仓透支重大漏洞 (当前高危缺陷: R14 / T4.2)
- **代码位置**: [`scripts/run_live_strategy.py:1394, 1419-1425`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py#L1394)
- **取证详情**:
  ```python
  # 第 1394 行：累加所有计划卖单的预估金额
  total_sales_proceeds = abs(sum(r["trade_value"] for r in rows if r["action"] == "SELL" and r["symbol"] != cash_proxy))
  available_purchasing_power = max(0.0, initial_cash_val + total_sales_proceeds)
  ...
  # 第 1419-1425 行：现金余额扫仓至 511880/GC001
  equity_trades_val = order_df.loc[~cash_mask, "trade_value"].sum()
  actual_post_trade_cash = initial_cash_val - equity_trades_val
  order_df.loc[cash_mask, "trade_value"] = -equity_trades_val
  ```
- **灾难机理**:
  1. 若某持仓标的一字跌停（-10%/-20%），在实盘中**一股都卖不出去**，真实回笼现金为 **0.00 元**；
  2. 但 `run_live_strategy.py` 在计算 Phase 2 的 `available_purchasing_power` 时，**依然将这笔卖不掉的资金算作可用购买力**！
  3. 导致 Phase 2 虚增买入计划，同时 Phase 4 将账面结余资金全部扫入理财 ETF；
  4. 最终实盘券商端因可用现金不足直接拒单报错，或者引发杠杆账户强制平仓。
- **修复方案**: 必须在 Phase 1 引入跌停检测，标记 `LIMIT_DOWN_ILLIQUID_RISK`，在计算购买力和扫仓时将其剔除（计划在 `T4.2` 彻底封闭）。

#### 2. 整手截断与高价股失真
- **代码位置**: [`common/allocation_backtester.py:241`](file:///home/stone/Work/github/quant/common/allocation_backtester.py#L241), [`scripts/run_live_strategy.py:203`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py#L203)
- **取证详情**: 本地回测与实盘均已实现 `resolve_lot_size`（A 股 100 股整手、港股手数映射）。对于 10 万元小资金账户，若策略配置贵州茅台（1 手需 14 万元），整手截断后股数为 0，造成资金拖滞（Cash Drag），这在 `docs/black_list.md` 中已立项跟踪。

---

## 五、 Defect 5: 时钟、时区与日历错配 (The Clock / Timezone Mismatch)

### 5.1 论文理论剖析
交易时钟错配包含夏令时时区位移、Bar 周期左闭右开/左开右闭混淆，以及最为隐蔽的**“日历渗漏（Calendar Leakage）”**——在法定节假日或周末执行模拟/批处理，将非交易日当作一个真实交易 Bar 推进，造成持仓周期、防震荡冷却计数器虚增失效。

### 5.2 本地代码取证与漏洞排查

#### 1. 2026-10-18 周日模拟运行对冷却计数器的污染
- **代码位置**: [`scripts/run_live_strategy.py:436-507`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py#L436-L507)
- **实证复盘**: 2026年10月18日（周日），系统执行了一次实盘模拟跑批，由于生成了 `2026-10-18/` 目录：
  - `resolve_holding_bars` 通过扫描历史目录名，将周日当作了一个持仓日（持仓天数被错误加 1）；
  - 标的在周五（10-16）卖出后，若采用自然日计算，周日被当作经过了 2 天，导致周一开盘时本应防反复抽搐的 2 日冷却期（`cooldown_bars=2`）被提早解除。
- **修复核验**: 提交 `71c09b6` 紧急开发了 `check_missing_trading_days` 与交易日历校验，强制滤除非交易日，彻底封闭了日历穿透漏洞。

#### 2. DuckDB 纯日期过滤尾部吞食 Bug (R13)
- **代码位置**: [`common/duckdb_cache.py:76-88`](file:///home/stone/Work/github/quant/common/duckdb_cache.py#L76-L88)
- **修复核验**: 在 DuckDB 中用字符串 `WHERE ts <= '2026-10-16'` 查询日内表时，会自动被隐式转换为 `00:00:00`，导致当天全部 48 根 5 分钟 K 线被全部截断抛弃！现已实现 `_parse_intraday_end` 补全至 `23:59:59.999999`。

---

## 六、 Defect 6: 僵尸配置 (Dead Configuration)

### 6.1 论文理论剖析
“僵尸配置”指在 CLI 参数、配置文件中隆重声明，甚至在前端界面提供给用户调节，但在代码执行路径中**从未被读取、消费或产生任何业务逻辑分支**的配置项。

### 6.2 本地代码取证与漏洞排查

#### 1. 历史上被架空的 `--health-report-file` 悬空参数 (历史 P0 缺陷 T0.3)
- **代码位置**: [`scripts/run_live_strategy.py:760, 1005`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py#L760)
- **取证详情**: 在提交 `26dbd67` 之前，Stage 2 脚本声称支持 `--health-report-file` 参数，Web 前端也按约定将其传入，但在脚本内部，该参数**从未被打开读取**！Stage 1 的高位回撤预警与市场宽度闸门完全沦为摆设。
- **修复核验**: 现已在 `26dbd67` 中接入 JSON 读取、门控代码解析与日内分布截断（L1005–L1020），彻底复活了该参数。

#### 2. `--auto-run-stage2` 子进程调用漏传参数隐患
- **代码位置**: [`scripts/check_live_portfolio_health.py:875-890`](file:///home/stone/Work/github/quant/scripts/check_live_portfolio_health.py#L875-L890)
- **取证详情**: 自动串联 Stage 1 与 Stage 2 时，子进程命令行数组 `cmd` 遗漏了 `--health-report-file` 的显式拼装，仅依赖 Stage 2 内部对默认文件名的后备回退。若用户自定义输出目录或定制报告名，该健康报告将再度失效。

#### 3. 非调仓日的日内止损配置“休眠”假象
- **代码位置**: [`scripts/run_live_strategy.py:1100-1110`](file:///home/stone/Work/github/quant/scripts/run_live_strategy.py#L1100-L1110)
- **取证详情**: 当策略设置 `rebalance_freq_days: 21` 时，若未在非调仓日生成稀疏权重行，在中间的 20 个交易日中，脚本将简单沿用上次目标仓位，此时配置的日内止损参数在非调仓日实际上是处于不活跃状态的。

---

## 七、 平台工程化对齐建设建议 (Actionable Engineering Roadmap)

为彻底消灭上述六大对齐缺陷，建议在后续版本中落地三项工程防线：

1. **构建自动化对齐测试床 (Automated Parity Harness - `tests/test_parity_harness.py`)**:
   - 定期抽取实盘历史存档 `data/runs/<date>/` 中的 `holdings.json` 与 `live_trading_ticket.csv`；
   - 灌入完全相同的历史行情数据至 `common/allocation_backtester.py`，断言回测生成的每一笔调仓权重、整手数、现金变动与实盘记录完全一致（$|\Delta w| < 10^{-4}, \Delta \text{Shares} \equiv 0$）。
2. **AST 配置穿透静态分析器 (Config Wiring Linter - `tests/test_config_wiring.py`)**:
   - 使用 Python `ast` 模块遍历 `StrategyConfig` 与 CLI 声明，静态断言每一个定义的属性/参数必须在核心代码逻辑中存在实际运算或条件分支，杜绝出现新的“僵尸配置”。
3. **紧急修复跌停板卖单双重穿仓透支 (R14 / T4.2)**:
   - 在 `run_live_strategy.py` 中对封死跌停标的打标 `LIMIT_DOWN_ILLIQUID_RISK`，在购买力计算与尾盘扫仓中坚决剔除其虚拟回笼资金，守住账户资金绝对安全红线。

---
*本取证报告所有论点均基于代码库实际 commit 记录、文件行号与严谨离线测试推演。*
