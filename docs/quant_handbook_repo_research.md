# quant-handbook 外部仓库研究：一份「文档厚、代码薄」的 A 股教学体系 / External Repo Research: QuantMLResearch/quant-handbook

**一句话结论**：该仓库是一套刚建立约两天的 A 股量化教学工程（8 模块 31 讲），**对外的教学承诺与其代码体量严重不成比例**（正文 ≈ 478 KB Markdown vs. 示例源码 ≈ 252 KB Python，单讲平均 7.7 KB 脚本），且**没有任何许可证文件**；对本仓的可迁移增量只集中在两处可离线验证的口径问题（相对财富指数回撤的起点/复利口径、HRP 退化宇宙的健壮性），其模块 6–7（Barra / cvxpy / 事件驱动撮合）的绝大部分能力**本仓已落地且实现更细**，不建议引入。

## 1. 标题、结论与元信息

| 项 | 值 | 来源与口径 |
| :--- | :--- | :--- |
| 仓库 URL | <https://github.com/QuantMLResearch/quant-handbook> | 页面 200 |
| 访问日期 | 2026-10-29 | 本次抓取日期 |
| 可访问性 | **可访问（HTTP 200）**，公开、非 fork、非 archived、默认分支 `main` | GitHub 页面 + `api.github.com` 均 200 |
| 许可证 | **无**。`/LICENSE` 返回 **404**；GitHub API `"license": null` | 见 §8 的链接状态表 |
| 仓库描述 | `Quantitative Research Handbook`；主页 <https://www.quantml.cn/tutorial/> | API `description` / `homepage` |
| 规模（API `size`） | 483 KB | API 字段，非自行打包测量 |
| 文件构成 | `examples/*.py` **37 个**（31 讲 + 5 个公共工具 `common`/`fixtures`/`factor_utils`/`ml_utils`/`portfolio_utils` + `__init__`）、`module_01..08/*.md` 31 篇、`tests/*.py` 4 个、根级 `README.md` + `syllabus.md` + `learning_path.md` + `resources.md` + `index.md`(85B) | `git/trees/main?recursive=1` 全量清单，未截断 |
| 语言 | Python（API `language`） | 实际最大体积是 `uv.lock` 275 KB 与 `package-lock.json` 154 KB |
| 创建 / 最后推送 | `2026-10-27T02:31:33Z` / `2026-10-29T07:37:43Z` | **仓库年龄约 2 天** |
| 提交数 | **4 次**（`fe51839` Initial commit → `6bb68ea` → `c3c2363` → `29665c7` update readme） | `/commits/main/` 页面 |
| 星标 / 复刻 | **144 stars / 34 forks / 1 watcher / 0 open issues** | 页面与 API 一致 |

> **不作的推断（明示）**：144 星对 2 天龄仓库而言是异常高的比值，但本文**不**据此推断其真实影响力、推广渠道或代码质量 —— 星标只能证明"有人点了星"，不能证明任何工程或研究结论。同理，`description` 与站点的自我定位（"买方/私募机构级"）属**作者自述**，本文只按原文引用，不背书。

---

## 2. 仓库定位与内容盘点

### 2.1 目录树（`git/trees/main?recursive=1`，已核实，未截断）

```text
quant-handbook/
├── .vitepress/            config.mts 4.2K / curriculum.ts 8.1K / theme/（Layout.vue、CourseHome.vue、CourseDiagram.vue、style.css 27K）
├── examples/              __init__.py 89B
│   ├── common.py          1.8K      fixtures.py    2.7K
│   ├── factor_utils.py    1.9K      ml_utils.py    6.3K      portfolio_utils.py 2.2K
│   └── lecture_01..31_*.py         2.8K–12.7K（31 个，合计 ≈ 237 KB，均值 ≈ 7.7 KB）
├── module_01/             01 28.4K / 02 20.1K / 03 15.2K / 04 12.1K
├── module_02/             05 13.6K / 06 11.0K / 07 9.6K  / 08 8.1K
├── module_03/             09 10.4K / 10 12.7K / 11 11.0K / 12 11.6K / 13 13.8K
├── module_04/             14 13.7K / 15 10.8K / 16 10.6K / 17 9.6K
├── module_05/             18 9.7K  / 19 10.8K / 20 11.1K / 21 9.5K / 22 10.3K
├── module_06/             23 25.4K / 24 14.9K / 25 13.5K / 26 13.8K
├── module_07/             27 12.2K / 28 13.2K / 29 11.6K
├── module_08/             30 14.5K / 31 19.3K
├── tests/                 test_tutorial.py 12.6K / test_factor_modules.py 12.4K
│                          test_ml_risk_modules.py 13.6K / test_execution_monitoring.py 17.1K
├── public/favicon.svg     index.md 85B（字节数已核实，内容未读）
├── README.md 23.7K        syllabus.md 18.6K / learning_path.md 14.0K / resources.md 9.6K
├── package.json / package-lock.json 154K   （VitePress 站点与 npm 脚本）
├── pyproject.toml 484B / uv.lock 275K
└── .gitignore
```

**合计**：`module_*.md` ≈ **412 KB**（31 篇，单篇 8.1K–28.4K），根级 md ≈ **66 KB**，两者合计 ≈ **478 KB**；`examples/` ≈ **252 KB**；`tests/` ≈ **54 KB**。**没有 `docs/` 子树，没有 `LICENSE`，没有 `tutorial/` 目录**（后者是讲义正文引用路径，见 §2.3）。

### 2.2 内容清单：逐项与核实状态

| 项 | 内容（原文口径） | 核实状态 |
| :--- | :--- | :--- |
| `README.md` | 8 模块 / 31 讲索引表 + 数据契约 + 快速上手 + 边界声明 | **已核实**（全文抓取） |
| `syllabus.md` | 31 讲逐讲设计思路与"当前进度"自述 | **已核实**（全文抓取） |
| `learning_path.md` | 入门六题自测、三条学习线、逐讲验收产物表、评分方式 | **已核实**（全文抓取） |
| `resources.md` | R01–R19 公开资源导读（MIT 18.06 / Boyd / Fama-French / 上交所规则 / AKShare 等） | **已核实**（全文抓取） |
| `module_04/16`（Alpha 衰减） | 秩自相关、累计 IC vs 边际 IC、半衰期对数拟合、调仓周期情景表 | **已核实**（全文抓取） |
| `module_04/17`（伪 Alpha 排查） | 市值寄生 OLS 残差、涨停不可买入现金保留、分市值子池审计 | **已核实**（全文抓取） |
| `module_06/25`（换手约束） | L1 税费 + L2 Almgren-Chriss 冲击、τ_max 半范数约束、帕累托前沿、机构换手参考矩阵 | **已核实**（全文抓取） |
| `module_06/26`（风险平价 / HRP） | MRC/TRC 欧拉定理、Spinu 对数障碍、HRP 三步法 | **已核实**（全文抓取） |
| `module_07/27`（回测引擎） | 向量化 vs 事件驱动对比、T+1 状态机、复式记账恒等式 | **已核实**（全文抓取） |
| `module_08/31`（实盘监控） | 实现 TE、CNE5 bias statistic/MRAD、风格漂移、三级滞回熔断、PSI | **已核实**（全文抓取） |
| 其余 **25 篇**讲座正文 | 仅见文件名、字节数、README 索引表的一行摘要 | **未核实**（未读正文） |
| `examples/lecture_01..31*.py` | 31 个脚本**确实存在**，字节数已核实 | **文件存在已核实；内容未核实**（未读源码、未运行） |
| `tests/test_*.py` 4 个 | 文件存在、字节数已核实 | **内容未核实；"100% 离线通过"仅为 README badge 自述** |
| `.vitepress/` 站点 | `curriculum.ts` 8.1K 作为大纲数据源 | **未核实**（未抓取） |
| `package.json` | 515 B，README 称提供 `npm run test:examples` | **未核实** |
| `pyproject.toml` | 依赖 `numpy/pandas/polars/pyarrow/scipy`；extras：`data`(akshare)、`ml`(lightgbm+shap)、`deep`(torch)、`optimization`(cvxpy) | **已核实**（全文抓取） |

### 2.3 已发现的自相矛盾（对"可复现"主张的诚实折扣）

- **运行命令三套并存**：`README.md` 用 `uv run --locked python -m examples.lecture_XX`；`module_04/17`、`module_04/16` 正文写 `PYTHONPATH=tutorial python3 -m examples.lecture_XX`；`module_08/31` 写 `python3 -m examples.lecture_31_...`。而**仓库里不存在 `tutorial/` 目录**（§2.1 清单为全量）。⇒ 讲义中的 `PYTHONPATH=tutorial` 是**迁移前残留**，照抄会失败。
- **首讲自认非终态**：`syllabus.md` 原文：「**第 1 讲已恢复至本次细化前的版本；深化方案尚待调整**」，同页又称「当前进度：第 1–31 讲已有基础正文及可运行示例，正在逐章深化」。⇒ 作者本人声明这是**进行中的初稿**，不是定稿教材。
- **"验收产物"是计划不是交付**：`learning_path.md` §5 明确「每行的"深化重点"和"产物"均为待落实的教学标准，**不能把规划当成已交付实现**」。这条自我披露应当被认真采信。

---

## 3. 关键内容摘要（按主题，不按文件罗列）

### 3.1 PIT 数据契约与"七个表"

来源：<https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/README.md>（已核实全文）

它把「Point-in-Time」落成一张具体的表结构契约，最值得记录的是**把"生效时间"与"可见时刻"拆成两个字段**：

> 「`effective_date` 为状态在现实中实际生效的业务日期。`available_at` 为该信息经由上游数据源处理后、量化系统**真正可知并可下单**的客观物理时间。二者决不可混同。」

七张表：`trading_calendar` / `stock_universe`（含 `delist_date`，防幸存者偏差）/ `daily_quote`（`close_hfq` 单独存）/ `status_event`（ST/退市整理）/ `financial_pit`（保留历次更正版本）/ `industry_event` / `index_weight`。另有两条可直接引用的口径纪律：

> 「上市交易天数按交易所开市日逐日计数，包含停牌期间，绝对不等于已下载行情记录行数。」

> 「仅表示该标的在此截面具备选股候选资格，不代表持仓被剔除后可以立即按最新价变现。」

**评价**：这是一份**设计得相当干净的契约**，但它服务于"截面选股 + 财报因子"这类研究形态；本仓的主力是长仓 ETF / 个股的**权重模板搜索 + walkforward**，没有财报 PIT 需求（`common/financial_api.py` 仅做 BaoStock 行情/接口，`fundamental_screener` 是独立旁支）。⇒ 契约本身不可直接移植，可借鉴的是**"两个时间字段不得合流"这条措辞纪律**。

### 3.2 伪 Alpha 的三种伪装（第 17 讲）

来源：<https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/module_04/17_fake_alpha_forensics.md>（已核实全文）

三个测试，都是**可用合成数据离线实现**的形态：

1. **市值寄生**：截面 OLS 回归 $\ln(\text{MarketCap})$ 取残差，算残差 Rank IC，定义 `DropRatio = 1 - IC(ε)/IC(f)`，原文红线「若 $\text{DropRatio} > \mathbf{60\%}$，说明该因子本质是市值风格暴露的傀儡，严禁作为独立 Alpha 因子入库」。
2. **涨停不可买入**：原文最关键的一句是**处置方式而非阈值** ——
   > 「若无法买入，**该笔目标资金必须强制保留在现金账户（当日收益为 0）**」

   并明确批评"把买不进的标的从池里剔除再重新均分"是「强行重构投资组合，掩盖了资金踏空闲置的真实损耗」。
3. **分市值子池审计**：Top 30% / Mid 40% / Bottom 30% 三池独立算 IC，原文把"容量"降级为"稳健性"（`Test 3: 分市值池稳健性检查（不是容量测算）`）。

**评价**：第 2 条与本仓 `§4.17` 的"指令空转"是**同一类失败的两种表现** —— 一边是"下了指令没成交却按成交记收益"，一边是"下了调仓指令但零腿、两份产物 CSV 都看不见"（`walkforward_rebalances.csv` 只记真的生成了腿的日子）。这是本次最值得记的一条**方法论同构**。

### 3.3 换手、摩擦与信息半衰期（第 16 / 25 讲）

来源：`module_04/16_...md`、`module_06/25_...md`（均全文核实）

- 第 16 讲把"换手"拆成 **名单替换率（Membership Replacement）** vs **真实资本换手**（含价格漂移），并论证后者才是口径；同时把 **累计持有期 IC** 与 **单日滞后边际 IC** 解耦，用 $\ln(d\cdot IC_{marginal}(h)) \approx \ln a - \lambda h$ 拟合**信息半衰期** $t_{1/2}=\ln 2/\lambda$。
- 第 25 讲把 L1 线性费与 L2 冲击 $\frac12 \Delta w^T \Gamma \Delta w$、$\gamma_i = k\,\sigma_i\,\text{AUM}/\text{ADV}_i$ 编进 cvxpy，加单边换手半范数约束 $\frac12\|\mathbf w-\mathbf w_{prev}\|_1 \le \tau_{max}$，再扫 $\tau_{max}\in[5\%,50\%]$ 画"换手-净超额"帕累托前沿。
- 第 25 讲还给了一张**机构换手参考矩阵**（原文，年化**单边**）：基本面多因子 `1.2×~2.0×`、中频指增 `8×~12×`、高频截面 `30×~60×`。

**与本仓的直接对照（重要）**：本仓 `L2.51` 已有**双边**年化换手 **> 10.0×/年 ⇒ 不得评 A** 的硬闸（2026-10-25 用户裁决「选的 C = 双边 10.0」，≡ 单边 5.0×/年）。按单边读，该手册的"中频指增 8×~12×"折算双边 **16×~24×**，**是该闸门的 1.6–2.4 倍**。⇒ 这份参考矩阵若被当成目标区间引用，会**直接与本仓已裁决的口径冲突**，不能拿来当"行业正常水平"的辩护。

### 3.4 组合构建：Barra / cvxpy / ERC / HRP（第 23–26 讲）

来源：`module_06/25_...md`、`module_06/26_...md`（26 全文核实；23、24 未读正文，仅有索引表摘要与 25.4K / 14.9K 的字节数）

第 26 讲是本仓唯一有**具体缺口对应**的一讲：HRP 三步法（相关系数距离 $d_{ij}=\sqrt{(1-\rho_{ij})/2}$ → SciPy `linkage` → 拟对角化重排 → 递归二分反方差分配），并把"**完全免矩阵求逆**"作为 HRP 相对于 ERC 的核心卖点，列出 EW / IVP / ERC / HRP 四方案对比表（原文对 HRP 的定位：「树聚类隔离同质资产，自动分层风控」，样本外稳定性「极高」）。

### 3.5 回测引擎：向量化 vs 事件驱动（第 27 讲）

来源：`module_07/27_...md`（全文核实）

四个可检验的设计点：(a) 决策时点强制 `signal_date < trade_date`；(b) **开盘估值与收盘估值物理隔离** ——「严禁在开盘撮合逻辑中偷看当天的 $P_{high}, P_{low}, P_{close}$」；(c) 持仓拆 `total_shares` / `sellable_shares`（T+1 锁定）；(d) 资产负债恒等式 $E_t = C_t + D^{receivable}_t + \sum Q_{it}P_{it}$，**应收股利计入净值但不得当买入现金用**。另附一份开源框架横评（Backtrader / Zipline-reloaded / Qlib / NautilusTrader，逐项评"A 股本土化适配程度"）。

### 3.6 实盘监控与三级熔断（第 31 讲）

来源：`module_08/31_...md`（全文核实）

五个组件里，**两个口径**对本次研究最有价值：

1. **相对财富指数**：$V_t=\prod_{s\le t}\frac{1+R_{p,s}}{1+R_{b,s}}$，$DD_t = 1 - V_t/\max_{0\le s\le t}V_s$，并给出了一个明确的实现陷阱——
   > 「历史极值序列 $\max_{0 \le s \le t} V_s$ 必须显式包含起点 $V_0 = 1.0$！否则若策略从第 1 天起便发生亏损（$V_1 = 0.90$）……进而得出'回撤为 0%'的荒谬结论」

   同时点名"用主动收益直接累加 $\sum a_t$"是错的（复利口径 vs 算术口径）。
2. **三级滞回熔断**：Level 1 `DD ≥ 4%` 冻结模型更新（回 `DD < 2%` 解除）/ Level 2 `DD ≥ 7%` 风险厌恶×2 + 个股上限减半 + TE 收缩（回 `< 4%` 降级）/ Level 3 `DD ≥ 10%` **Latched State**，`allow_new_buys = False`，**禁止自动解除**，须"风控总监输入带审计编号的人工审批指令（`reset_emergency`）"。
3. 另有：实现 TE 与事前 TE 偏离比 `> 1.5` 告警；CNE5 **bias statistic** $B_n=\mathrm{std}(R_{nt}/\sigma_{nt})$（理想 1.0，`[0.66, 1.34]` 带外判过度/低估）与 **MRAD**（理论下界 ≈0.17，厚尾 ≈0.19）；风格主动暴露 `±0.10σ` 黄灯、`±0.20σ` 阻断；**PSI** 十等频分箱 + 两端 $\pm\infty$ + **Laplace 0.5 伪计数**平滑。

**评价**：这一讲的技术密度是全仓最高的一篇（19.3 KB），但它描述的是**一个把"风控声明"与"可观测遥测"绑在一起的监控层**，不是能直接搬进本仓的可执行件（本仓的熔断在 `blend_base.py` 内，没有独立的监控服务）。

### 3.7 公开资源导读（`resources.md`）

来源：<https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/resources.md>（全文核实）

19 条 R01–R19，逐条给了"读什么 / 读完做什么 / 对应讲次"，并且**主动标注了边界**（这正是本仓文档风格欣赏的部分）：

> 「公开可访问不等于可以任意转载或商用；数据使用与软件许可应分别查看来源条款。」

> 「本次搜索能定位官方合约内容，但直接读取出现超时，故不将其计入主表的可直接读取资源」

值得记的两条**负面/口径**判断：(a) 对 Fama-French 数据明确"**不是 A 股的现成风险模型**"；(b) 对 Qlib 明确"开源框架的存在不证明默认配置已符合你的执行规则"。这两句比资源列表本身更有用。

---

## 4. 与本仓的对照分析

### 4.1 逐模块映射表

| 本仓模块 | 手册对应 | 重叠点 | 冲突点 | 增量价值 |
| :--- | :--- | :--- | :--- | :--- |
| `common/allocation_templates.py`（9 个静态模板，含 `HierarchicalRiskParityAllocation` `:697`、`_step_asset_inertia` `:217`） | 26（ERC/HRP）、25（换手上限） | HRP 三步法（相关距离→`linkage`→拟对角化→递归二分）；反方差权重；换手半范数 $\frac12\|\Delta w\|_1$ | 手册的 HRP 是"一次性资产配置"，**没有稀疏权重契约、没有 cell-level `NaN` vs `0.0` 区分、没有 per-class `version`**；直接搬入会破坏本仓 `generate_weights` 契约 | **仅一处**：其流程把 HRP 的退化点（N=2、全等收益⇒零距离、单簇）显式化，可作本仓已知 `HierarchicalRiskParityAllocation` → `IndexError` 的修复参考（见 §5-A2） |
| `pipeline/research/blend/`（`BASE_VERSION` 底座、三级熔断 `:1070-1103`、`fast_recovery` `:988`、tier 冷却 `:1001-1017`） | 31（滞回熔断）、25（换手成本）、16（衰减） | 多级回撤降仓、冷却/自动修复、换手-摩擦权衡 | **正面冲突**：手册要求 Level-3 **禁止自动解除**、须人工审批复位；本仓 `fast_recovery` 是"冲力/广度/10 日动量任一为正即钉回 `scale=1.0`"的**自动旁路**。这不是 bug 修补而是**改变 as-deployed 行为**，属 `L2.50` 第 6 条明令禁止的"re-parameterized 冒充验证" | **有**：相对财富指数 $V_t$ 的复利定义 + "HWM 必须含 $V_0=1.0$" 的实现陷阱，是本仓熔断口径可以立刻自检的一条（见 §5-A1） |
| `pipeline/research_strategy/rs/`（Chan 结构、WorldQuant/GTJA 因子、timing aspects） | 9–13（因子）、19/22（ML 选股） | 量价因子、正交化、截面排序 | 本仓主力是**长仓 ETF/单资产择时 + blend**，不是截面 ML 选股；手册整条链路（PIT 财报、Barra、指增 TE）在本仓**没有对应消费者** | **接近零**。因子定义层面它讲的是教科书内容（动量/反转/Amihud/MAX），本仓 `common/indicators.py`、`alpha101_operators.py` 已覆盖且带实现 |
| `backtester/`（`run_backtest.py` walkforward 折；`common/allocation_backtester.py` 佣金 `:353` / 滑点 `:354` / 印花税 `:358` / 平方根冲击 + ADV20 `:1040-1100` / `execution_price` `next_open`·`next_vwap` `:898-945` / `china_trading` 涨跌停 / 港股每手） | 27（DES 引擎）、28（摩擦与冲击）、29（A 股撮合规则） | **高度重叠**：次日开盘撮合、ADV 参与率上限、平方根冲击 $\gamma\propto\sigma\cdot\text{AUM/ADV}$、印花税、涨跌停不可成交、整手取整、复式账本（`B1.1` 已把"按权重递推净值"改成真账本，修掉过 ghost leverage） | 手册主张"向量化引擎无法处理资金约束"是本仓**已修复的历史问题**（`allocation_backtester.py` 的 ACCOUNTING MODEL 段落原文记录了旧实现的 ghost leverage）。**冲突仅在方法层**：它建议另建 DES 引擎，本仓已有账本 + 因果 `shift(1)` + 显式降级告警 | **接近零**。唯一可对照项：其"应收股利计入净值但不得当买入现金"的四时点账本。本仓 `L2.54`（股息净值引擎）已闭环，但**四时点应收/送股是否逐时点建模未核实**，不作断言 |
| `common/position_lifecycle.py`（UPLSM：`FLAT/PENDING_ENTRY/HELD_GESTATION/HELD_ACTIVE/PENDING_EXIT/COOLDOWN`、`BlockingReason`） | 27（T+1 双簿）、31（锁存态） | `HELD_GESTATION` 与本仓 T+1 妊娠期；`COOLDOWN` 与其熔断滞回同构 | **红线邻近**：手册的"latched state + 人工复位"若被映射到**个股再入闸门**，即触 `§4.18` 第 3 项（再入侧冷却延长属 `L2.36` 已证伪的"出场确认"等价物，禁止）。组合级熔断锁存是另一条轴，但同样不能作为"验证"手段引入 | **低**。其 `BlockingReason` 式的"为何没下单"显式标注与本仓已有设计同源，无新增 |
| `pipeline/.agents/skills/walkforward-audit/`（`run_audit.py::TURNOVER_CEILING_ANN = 10.0`、`turnover_annualised_from_folds`、LOO/明星赢家/摩擦出血/现金归一化回撤） | 17（伪 Alpha 法医）、16（换手/衰减）、31（bias stat） | 换手闸、集中度、DSR、异常值 | **阈值冲突**：手册用 `DropRatio > 60%`、`Haircut > 40%`、大盘池 `IC > 0.015` 三个**硬编码阈值**，本仓纪律是"阈值先预注册再跑数"（`L2.49` 第 5 条 (a)），不能直接采信外部数字 | **中低**："市值寄生"这一**检验形状**（残差化后 IC 缩水率）可作审计阶段候选，但本仓 walkforward 产出是**组合级收益**而非截面因子 IC，需重新设计（见 §5-A4） |
| `web/livetrading/`（`app.py`/`execution_service.py`/`storage.py`/`valuation.py`，`data/runs/<date>/` 不可变快照） | 31（监控看板） | 风控看板、快照留痕、阶段化执行 | 手册的"人工审批复位"需要**操作员 UX 与审计编号**，与本仓"Stage-1 健康检查 → Stage-2 自动出票"的无人值守链路形态不同 | **低**。PSI/TE 面板在**没有常驻部署模型**的前提下无输入（见 §6-D8） |
| `ml/bnn_forecaster/`（独立 `uv` 环境，AutoBNN 概率预测） | 18（Purged/Embargo）、19/21（LightGBM / 深度边界） | 时序 CV 的防泄漏、概率预测 vs 点预测 | 手册用 `sklearn`/`lightgbm` 封闭环境；本仓该目录是**强隔离依赖栈**（AGENTS.md 明示不与 `pipeline/` 共用），任何"参考实现"都要跨环境搬运 | **接近零**。Purged/Embargo 在本仓搜索流程里已是既有纪律，非新增知识 |

### 4.2 一句话总评

手册的**模块 1–2（数据/PIT）与模块 6–7（风险模型/撮合）**面向的是"截面选股 + 指增"这条本仓没有的产线；它的**模块 4（因子评估/伪 Alpha）与模块 8（监控/熔断）**才是与本仓共享地基的部分，而这两块里能落到**具体文件、可离线证伪**的只有两处口径问题（§5-A1、§5-A2）；§5-A3 属 `L2.50` 行内的**裁决/测量**（产出是定义与数值对照，不是代码），§5-A4 的先决条件（离线市值列）**未满足**。其余属于"读了更清楚，但不改代码"。

---

## 5. 可落地候选清单（按价值排序）

> 排序依据：**能否在 100% 离线条件下证伪 + 是否已有文件级落点 + 是否不触碰 §4.18 / `L2.36` / `L2.49` / `L2.50` 的红线**。下方"验收标准"一律按本仓纪律写成**先写死阈值再跑数**，不得事后补。

### A1（最高）相对财富指数与 HWM 起点口径审计 — 规模 **S**，无需联网

- **要做什么**：核对手册指出的两个陷阱在本仓是否存在 —— (i) 回撤是否用**几何复利**的相对财富 $V_t=\prod(1+R_p)/(1+R_b)$ 而非主动收益**算术累加**；(ii) 追踪极值 $\max_{s\le t}V_s$ 是否**显式包含 $V_0=1.0$**（否则"开局即亏"会被算成 0 回撤）。附加一项：确认缓降公式 `max(0, 1 − (dd − 0.10)/(0.20 − 0.10))` 的 `dd` 定义域与上述口径**同名同义**。
- **落在哪个文件**：读数在 `pipeline/research/blend/blend_base.py`（`_apply_portfolio_deleverage` `:135`、`deleverage_scale` `:1070-1103`、tier 冷却 `:1001-1017`）；用例落在 `pipeline/research_strategy/tests/`。
- **验收标准**：①一条合成 NAV 序列（第 1 天即 −10%）必须给出 `dd > 0`，删掉"含 $V_0$"的实现该用例转红；②同一路径下"算术累加"与"几何复利"两种实现给出**不同** `dd` 的数值断言（钉住口径，防将来漂移）；③若改动 `blend_base.py` 逻辑，按 `AGENTS.md` §7 升 `BASE_VERSION` + `docs/version.md` 中文 changelog + 两份版本断言测试 + 重跑 `--dump-strategies`。
- **风险**：**容易被误读成 `L2.50` 的替代交付**。必须在报告里写明：这是**可观测性/口径审计**，不是 tier 语义验证，`L2.50` 的"离线合成压力带"仍缺。
- **口径红线**：不涉及阈值、不涉及熔断参数，故不触 `L2.50` 第 6 条"禁止调阈值"。

### A2（次高）`HierarchicalRiskParityAllocation` 退化宇宙健壮性 — 规模 **M**，无需联网

- **要做什么**：把手册第 26 讲**显式化**的 HRP 退化点逐一变成用例并加保护：(1) `N < 2`；(2) 全等收益 ⇒ 相关系数全 1 ⇒ 距离全 0 ⇒ `linkage` 退化；(3) 某标的历史不足以覆盖 `cov_lookback`（本仓已有一层"只收满窗口历史"的过滤，`allocation_templates.py:734` 附近，需确认该过滤是否覆盖全部退化路径）；(4) 协方差近奇异。目标：**返回合法稀疏权重或显式空帧，而不是抛 `IndexError` 再由 `_portfolio_score` 降级为 `-inf`**。
- **落在哪个文件**：`common/allocation_templates.py:697`（`HierarchicalRiskParityAllocation`）。
- **验收标准**：①4 条合成宇宙用例（`SyntheticDataProvider`，零网络）各自断言"返回合法权重且行和 ≈ 1 或显式空帧"；②负向用例：制造零距离输入时不得抛异常；③改模板逻辑 ⇒ **该类的 `version` 必须升**（`1.0.0` → `1.1.0`）+ 同步 `common/tests/test_strategy_versioning.py` + `--dump-strategies` + `docs/version.md` 记变更。
- **风险**：**历史可比性断裂** —— 目前 HRP 在小/退化宇宙上"必输"，修好后可能在某些 fold 上进入候选池，`strategy_generator` 的既有搜索结果会变。必须把它标注为"改变了搜索空间的一个候选"，而不是"修了个 bug 没影响"。**不升级版本的修改在这里是违规的**。
- **为什么值得做**：`AGENTS.md` 已把它列为 known pre-existing issue（"can raise `IndexError` on some small/degenerate synthetic universes"），是一条**已记账但未修的已知缺陷**；手册提供的价值只是"把退化面列清楚了"，实现仍须按 López de Prado (2016) 原算法自己写（**不得抄其代码，见 §6-D6**）。

### A3（中）熔断语义口径裁决：绝对 NAV 回撤 vs 相对基准复利回撤 — 规模 **S**，无需联网，**属 `L2.50` 行内工作**

- **要做什么**：本仓 `L2.50` 的实测口径是"折内 NAV 重置下的**绝对**折级最大回撤"（15 折 × 63 根，最大仅 0.1001、均值 4.4%–5.8%）；手册的 `DD` 是**跨全样本连续复利、相对基准**的回撤。两者**不是同一个量**，混用会让"0.10 档碰不到"这一结论被质疑。要做的是把两种定义并列、在同一 fold 上给出数值差，并明确 as-deployed 的三级熔断究竟防哪一种。
- **落在哪个文件**：指向 `pipeline/research/blend/blend_base.py` 的 dd 计算与 `backtester/run_backtest.py` 的折切分；**产出是裁决记录与数值对照表**，不是代码。
- **验收标准**：一段可复核的定义 + 一条 fold 上的双口径数值对比；结论要么"两者在长仓 blend 上差异 < 噪声下限（沿用 `L2.49` 第 5 条 (d) 的 t 门槛）⇒ 口径无关紧要"，要么"差异显著 ⇒ 需重新声明 as-deployed 语义"。
- **风险**：**与 `L2.50` 的"不可检验"结论同源**，若被当成"把窗口拉长就能测到熔断"的翻版，即触 `L2.50` 第 9 条已证伪的设想（回撤可达性代理已证明 0.15/0.20 在 2022-01–2026-10 全样本任何敞口下不可达）。**本文档不为此新建任务号**，它是 `L2.50` 的一个子问题。

### A4（低，条件性）伪 Alpha 的"市值寄生"审计阶段 — 规模 **M–L**，**先决条件未满足**

- **要做什么**：在 walkforward 审计里加一道"组合收益对市值/流动性暴露的残差化"检验，观察剔除暴露后超额是否断崖。
- **先决条件（未核实）**：本仓离线路径是否有**市值/流通市值**列。若只有 OHLCV + 成交额，则"市值"只能用成交额做代理，而**代理本身就是一次新的度量假设**，必须先披露再使用。满足不了就不做。
- **落在哪个文件**：`pipeline/.agents/skills/walkforward-audit/scripts/run_audit.py` 新增 stage（**不构成策略模板改动，故不触版本轴**）。
- **验收标准**：①阈值必须**先测量分布再预注册** —— 手册的 `60%` / `40%` / `IC > 0.015` 是它对自家合成样本的阈值，**不可直接采信**；②负向用例：人工注入市值暴露的合成策略必须被标红，删掉检验该用例转红。
- **风险**：**形态不匹配是主要风险** —— 手册检验的是**截面因子 IC**，本仓 walkforward 产出是**组合级收益序列**，"组合收益对市值回归"是另一个统计量，容易做出一个"看起来像但结论不可迁移"的指标。若无法把它定义成可证伪的组合级量，正确结案就是**不做**。

---

## 6. 明确的"不建议做"清单及理由

| 编号 | 不建议做的事 | 理由（含已触红线） |
| :--- | :--- | :--- |
| **D1** | 引入 `cvxpy` + Barra 风险模型 + 均值-方差优化器层（第 23–25 讲整条链路） | ①本仓架构是"稀疏权重模板 → 账本式回测"，**没有优化器层**，引入即 L 级重构；②新增 `cvxpy` 会进 `pipeline/` 的共享 env，而 `AGENTS.md` 把版本敏感的重栈隔离在 `ml/` 各自独立环境 ⇒ 这一步属**架构裁决**，不是顺手加一个包；③**更关键**：`L2.49` 第 10 条已实测"收紧约束侧（惯性闸）可降 Σ换手 28.4%–54.7%，但收益侧 18 格中 17 格删 ≤4 折即翻号"，**没有任何杠杆通过多折稳健性检验** ⇒ 在优化器层加换手约束属于同一族的、已被数据否掉的方向，不是新线索 |
| **D2** | 任何形式的"出场确认 / 延长确认 / 延迟卖出执行"用于降换手 | **`L2.36` 已用数据证伪并禁止**：k=2 可及集仅 111 条 = 毛额 2.19% / 成本 2.72%，年化摩擦上限仅 2.69/4.40 bp（基准 85.26 bp/年），且该集合价格比调整后 **−0.53pp（t ≈ −2，符号与"抑制它省钱"相反）**；卖侧整体远期超额 −0.05/−0.06/+0.07 pp（\|t\| ≤ 0.5）⇒ 不存在卖侧择时 α。第 16/25 讲的"降低调仓频率/加确认"若被当作方案提出，就是**重提已证伪项** |
| **D3** | 把第 31 讲的 Level-3 **锁定态（latched）+ 人工审批复位** 映射为个股再入门槛或冷却延长 | **`§4.18` 第 3 项已裁决：再入侧冷却延长属 `L2.36` 证伪的"出场确认"等价物，禁止**；`L2.52` 阶段二的合资格候选已被收缩为唯一一项（再入份额阶梯），任何"阻断/延迟再入"的机制一律判等价物不可用。组合级熔断锁存是另一条轴，但改动它 = **as-deployed 行为变更**，`L2.50` 第 6 条明令不得以"调阈值/调行为"冒充 as-deployed 验证 |
| **D4** | 引用手册第 25 讲的"机构换手参考矩阵"当行业正常水平（中频指增 `8×~12×` 单边） | 与 `L2.51` 的**双边 10.0×/年**硬闸（2026-10-25 用户裁决「选的 C = 双边 10.0」≡ 单边 5.0×/年）直接冲突：该区间折算双边 16×–24×，是本仓闸门的 1.6–2.4 倍。本仓实测 10 个受评 run 双边 9.74–15.09×/年、18 个受评 run 双边 mean 16.44 / median 14.59×/年，**17/18 超线**——这正是本仓的问题所在，不能拿外部"正常区间"来豁免 |
| **D5** | 重写为事件驱动（DES）引擎，或把 A 股制度（T+1 / 涨跌停 / 印花税）下沉到模板层 | ①`common/allocation_backtester.py` 已有 `next_open`/`next_vwap` 执行模式、ADV20 参与率上限、平方根冲击（$\gamma$ 含 AUM/ADV）、印花税、`china_trading` 涨跌停、港股每手、以及"按账本而非按权重递推"的复式记账（旧实现的 ghost leverage 已修）；②制度约束下沉到模板层会**破坏稀疏权重契约**（模板输出的是"目标权重"，不是"能否成交"），成交可行性只能在执行层判定 |
| **D6** | 复制其代码、讲义正文或图表 | **仓库无 LICENSE（`/LICENSE` 404、API `license: null`）**。按 GitHub 默认语义即"保留所有权利"，公开可见 ≠ 授权使用。本文档只引用**极短原文（≤2 行）并标注 URL**，实现一律回到原始文献（如 HRP → López de Prado 2016）自行编写 |
| **D7** | 用"信息半衰期 → 最佳调仓周期"替换现有固定调仓日历 | 该框架的前提是**信号是可排序的因子分数**，其换手来自"排名翻滚"；本仓 `§4.17` 已把换手解剖到执行层：`resize` 3,093 / `entry` 2,517 / `exit` 2,109，且 **`entry` 的 69.11%–95.33% 是"曾被清仓后的再入场"** ⇒ 换手来源是**止损出局→冷却买回的往返**，不是权重漂移。调仓周期在本仓**不是可自由优化的自由度**（它由 `rebalance_freq_days` 与折几何共同固定），做半衰期拟合只会测到错误的量。此外 `L2.49` 第 13 条已否掉"子策略权重平滑"，同一侧无剩余空间 |
| **D8** | 引入 PSI 漂移监控 / SHAP 归因面板 / CNE5 bias statistic / MRAD | 全部**缺输入**：①PSI/SHAP 需要**常驻部署的 ML 模型**；本仓 `ml/bnn_forecaster` 是独立 `uv` 环境（`AGENTS.md` 明示与 `pipeline/` 隔离），`web/livetrading` 的部署链路里**未见其接入**（未逐文件核实，但该目录只有 `app/execution_service/models/storage/valuation` 五个模块）；②bias statistic 与 MRAD 需要**结构化风险模型给出的 $\sigma_{nt}$**，本仓没有 Barra 型风险模型（`common/covariance.py` 提供的是协方差估计，不是因子风险分解）。在没有输入的情况下建面板 = 建一个永远输出常数的仪表盘 |
| **D9** | 整体移植 PIT 七表契约（`financial_pit` / `industry_event` / `index_weight` 等） | 本仓**没有财报截面因子产线**：`common/financial_api.py` 服务的是行情/接口层，`fundamental_screener` 是独立旁支且未接入主流水线。移植等于为一个不存在的消费者建库。唯一值得吸收的是**措辞纪律**（`effective_date` ≠ `available_at`），而本仓 `common/market_clock.py` / `docs/market_clock_and_asof_resolution.md` 已在同一问题上有一套自己的口径，**不应用外部契约覆盖** |

---

## 7. 局限与未核实项（诚实边界）

1. **只完整读了 6 篇讲座**（`module_04/16`、`module_04/17`、`module_06/25`、`module_06/26`、`module_07/27`、`module_08/31`），其余 **25 篇正文未读** —— 它们在本文件里的描述**全部来自 README 索引表的一行摘要 + 文件字节数**，属"未核实"。任何基于第 9–15、18–24、28–30 讲内容的判断都只能当**索引级**信息使用。
2. **没有运行其任何代码或测试**。「Test Suite-100% Offline Passed」是 **README 的 badge 自述**，我既未复现，也未读 `tests/*.py` 的断言内容（仅核实 4 个文件存在、合计 ≈ 54 KB）。因此"它有契约测试"与"它的契约测试断言了正文所述性质"是两件**未被连接**的事。
3. **没有读 `examples/*.py` 的源码**。所有关于"该讲实现了什么"的描述来自**正文的自述**。已知风险：正文的代码块由 VitePress 片段注入语法 `<<< @/examples/lecture_XX.py` 引入，我未渲染站点核对注入是否成功、是否存在正文与源文件不一致。
4. **未验证外部资源可达性**。`resources.md` 里 R01–R19 的 19 条链接（MIT OCW、Boyd、Fama-French、上交所、AKShare 等）我**一条都没打开**；该文件自述核验日期为 **2026-10-26**，其自述不能替代我的核验。
5. **未读 `.vitepress/` 与 `package.json`**。`curriculum.ts` 是否与 31 篇正文一一对应、`npm run test:examples` 究竟调用什么，均**未核实**。
6. **星标/复刻是 2026-10-29 的快照**（144/34），此后可能变化；仓库仅 4 次提交、年龄约 2 天，**任何"社区验证过"的暗示都不成立**。
7. **对"规模"的量化口径**：483 KB 来自 GitHub API 的 `size` 字段（含 `uv.lock` 275 KB 与 `package-lock.json` 154 KB）；我在正文中给出的 478 KB / 252 KB / 54 KB 是**按 `git/trees` 逐文件字节数自行求和**的结果，与 483 KB 不可直接相加比较（后者是打包后大小）。这两个数字的口径不同，**不要混用**。
8. **对"无许可证"的处理是保守假设，不是法律意见**。我按"未授权即不得复制"处理；若作者后续补 LICENSE（如 MIT），D6 的结论需相应放宽，但**本文档不预付这个假设**。
9. **未核实的本仓事实（已在正文中标出）**：①`L2.54` 股息净值引擎是否覆盖"应收股利四时点"；②本仓离线数据路径是否有市值/流通市值列（§5-A4 的先决条件）。这两条**我都没有查证**，凡涉及处已写明"未核实"。

---

## 8. 参考链接（全部为本次实际尝试的 URL，注明成功/失败）

| # | URL | 用途 | 状态 |
| :--- | :--- | :--- | :--- |
| 1 | <https://github.com/QuantMLResearch/quant-handbook> | 仓库落地页（描述、星标、目录、README 渲染） | **成功（200）** |
| 2 | <https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/README.md> | README 原文（31 讲索引表、PIT 契约、上手命令） | **成功（200）** |
| 3 | <https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/syllabus.md> | 课程大纲与"当前进度"自述 | **成功（200）** |
| 4 | <https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/learning_path.md> | 学习路径 / 逐讲验收产物 / 评分方式 | **成功（200）** |
| 5 | <https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/resources.md> | R01–R19 资源导读 | **成功（200）** |
| 6 | <https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/pyproject.toml> | 依赖与 extras（判断"是否只依赖轻栈"） | **成功（200）** |
| 7 | <https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/LICENSE> | 许可证是否存在 | **失败（404）** ⇒ 判为无 LICENSE |
| 8 | <https://github.com/QuantMLResearch/quant-handbook/commits/main/> | 提交历史与最后提交时间 | **成功（200，4 次提交）** |
| 9 | <https://api.github.com/repos/QuantMLResearch/quant-handbook> | 元数据（license/size/stars/created_at/pushed_at/language） | **成功（200）** |
| 10 | <https://api.github.com/repos/QuantMLResearch/quant-handbook/git/trees/main?recursive=1> | 全量文件清单与逐文件字节数（`truncated: false`） | **成功（200）** |
| 11 | <https://api.github.com/repos/QuantMLResearch/quant-handbook/contents/examples> | `examples/` 目录逐项（交叉核对第 10 条） | **成功（200）** |
| 12 | <https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/module_04/16_turnover_and_alpha_decay.md> | 第 16 讲正文（换手/衰减/半衰期） | **成功（200）** |
| 13 | <https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/module_04/17_fake_alpha_forensics.md> | 第 17 讲正文（伪 Alpha 三测试） | **成功（200）** |
| 14 | <https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/module_06/25_turnover_constraints_and_costs.md> | 第 25 讲正文（L1/L2 成本、τ_max、机构换手矩阵） | **成功（200）** |
| 15 | <https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/module_06/26_risk_parity_and_hrp.md> | 第 26 讲正文（ERC/HRP） | **成功（200）** |
| 16 | <https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/module_07/27_vectorized_vs_event_driven_backtest.md> | 第 27 讲正文（DES 引擎、复式记账） | **成功（200）** |
| 17 | <https://raw.githubusercontent.com/QuantMLResearch/quant-handbook/main/module_08/31_live_monitoring_risk_dashboard.md> | 第 31 讲正文（TE/bias/MRAD/风格/熔断/PSI） | **成功（200）** |
| 18 | <https://github.com/QuantMLResearch/quant-handbook/tree/main/module_01> … `/module_08` | 顶层目录页（以第 10 条的 API 清单替代逐页抓取） | **未尝试（由第 10/11 条覆盖）** |
| 19 | <https://www.quantml.cn/tutorial/> | 项目自述的站点主页 | **未尝试**（本文不依赖其内容） |

> 说明：第 18、19 条**我没有发起请求**，故既不声称成功也不声称失败 —— 列出来是为了让读者知道"哪里我没看"。所有失败项都在上表中单独标注。

---

任务拆解与优先级请见 [`docs/development_plan.md`](development_plan.md)（按 SSOT 约定，本文档不自行登记任务号、不改任何 `[ ]`/`[x]`）。
