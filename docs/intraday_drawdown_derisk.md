# 日内回撤 ⇒ 当日目标暴露下调（冻结版 v1）

> **文档性质**：**冻结的判据书（frozen judgement）**，不是实施记录、不是研究结论、不是设计探讨。本文冻结 `R1` 的**参考价锚点、量纲口径、阈值阶梯、状态词表与首轮围栏**；实现、调用方与后续审计**消费**本文，不得反过来由实现倒推口径。`R1` 行（[`docs/development_plan.md`](file:///home/stone/Work/github/quant/docs/development_plan.md) `:333`）第 1 条要求「先把触发下调的日内回撤阈值与下调比例写成可复算的冻结值（含依据与失效条件）」，故本文是**先于代码**的交付物。
> **冻结日期**：**2026-11-08**。判据规格号 `intraday_drawdown/v1`，规则规格号 `r1.1`（实现侧常量 `common/intraday_drawdown_derisk.py::DECISION_SPEC_VERSION`）。
> **状态**：**FROZEN**。实现与本文字面冲突时**以本文为准**，并须以 append-only 的 `## Revision` 段更新本文，而不是就地改实现迎合文档。
> **任务状态 SSOT**：§2.20 的 `R1` 行仍是任务状态、优先级与验收的唯一权威；本文只维护**判据、口径、状态词表与失效条件**，**不维护任务状态**（本轮不改 `docs/development_plan.md` 的任何状态位与计数）。
> **实现位置**：`common/intraday_drawdown_derisk.py`（纯函数 + 冻结 dataclass）；配套离线用例 `pipeline/tests/test_intraday_drawdown_derisk.py`。**本轮无消费面**（首轮 advisory only，见 §6）。

## 1. 性质声明（先写定位，再写数字）

### 1.1 这是什么

`R1` 回答一个问题：**「昨天收盘时，组合自日内峰值已经回撤了 `dd` —— 今天的市场层暴露是否应当打折携带？」**

- 输出是**市场层目标暴露的乘数**（`1.0` / `0.75` / `0.50` / `0.25`）**加上产出该乘数的证据**（`drawdown`、状态、原因、`hwm_intra`、用到的价格或 `dd_trough`、参考价锚点）；
- 判据内**没有** symbol 维度：输入是**一个组合级**的回撤读数，模块里没有任何排序、挑选或选择；
- 它**不是**日内择时（不预测路径、不在盘中多空切换），**不是**波动率目标化叠加层（回撤读数不是任何方差预测），也**不是**回撤熔断族的替代品 —— `dd_reduce_thresh = 0.10` / `dd_defensive_thresh = 0.15` / `dd_stop_thresh = 0.20`（[`pipeline/research/blend/blend_base.py`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py) `:654-656`）仍是引擎内分级降险的唯一出处。

### 1.2 被解释量的定义（与生产侧同量）

```
drawdown = price / hwm_intra - 1          # <= 0，与生产侧 dd_trough 同义
```

`drawdown` 用**分数**（`-0.02` 即 `-2.0%`），与生产侧 `dd_trough = (intra_min - hwm_intra) / hwm_intra`（`scripts/check_live_portfolio_health.py:2857`）**同量纲**，因此生产侧与消费侧之间不存在任何百分数 / 分数转换可以漂移。

### 1.3 参考价选的是「前收（carried prior peak）」，不是会话开盘价

**这是本文最需要先钉死的一件事**，因为同一个 `hwm_intra` 挂在不同参考价上就是两个不同的量。

生产侧的 `hwm_intra` **不是**「本会话自己的最高点」，而是

```
hwm_intra = max(float(dd_info["peak_nav"]), intra_peak)        # :2856
```

即「**上一个状态里携带过来的高水位**（`peak_nav`，`account_state.json` 持久化的 All-Time / trailing peak，报告页把它标为 `High-Water Mark (HWM) / All-Time Peak`，见 `:2161`）」被本会话的日内峰值**抬升**后的结果。故参考价冻结为：

> **`REFERENCE_PRICE_SEMANTICS = "previous_close"`** —— 上一交易日的收盘 / 携带过来的前期峰值（**前收锚点**），**显式不是**会话开盘价。

三条理由（缺一不可）：

1. **会话开盘价会漏掉最该降险的那一天**：跳空低开的日子里，若以开盘价为锚，回撤恒为 `0%`，而组合相对前收已经亏了钱 —— 这正是 `R1` 存在的场景。以开盘价为锚等于给「跳空 + 阴跌」这一天发免死金牌。
2. **会与它自己声称消费的量脱钩**：`R1` 行原文说它消费 `dd_trough`；若锚点改成开盘价，消费侧算出的就不是生产侧那一列 `dd_trough`，而是另一列从未被测过的数（同一数据、两个口径，即「两侧同时失真」）。
3. **跳空高开后回吐到开盘价也不等于无回撤**：携带峰值锚点已经把这段回吐计入，正是想要的读数。

实现侧两处落实该锚点：

- `anchor_hwm_intra(reference_price, prices) = max(reference_price, max(prices))`：镜像 `:2856`；非正 / 非有限价格**被剔除**而不是当成 `0.0` 价格（零价是缺数据，不是免费资产）；全部被剔除时返回 `None`（不可测）。
- `evaluate_intraday_drawdown(..., reference_price=...)`：只作**自洽闸**用 —— 携带峰值不可能低于前收，故 `hwm_intra < reference_price` 说明锚点不自洽 ⇒ `INSUFFICIENT_DATA`，**绝不**向 `0.0` 静默钳位。

### 1.4 生产侧的一个既有退化点（本文必须披露）

`scripts/check_live_portfolio_health.py:2857` 在 `hwm_intra <= 0` 时把 `dd_trough` 写成 **`0.0`**：

```python
dd_trough = (intra_min - hwm_intra) / hwm_intra if hwm_intra > 0 else 0.0
```

`0.0` 在语义上是「恰好站在高点」，而这里表达的是「不可测」—— 两者被同一个数字吞掉。本文**不**改生产侧（那是独立任务），但实现侧因此**强制**要求 `hwm_intra` 同时在场：`hwm_intra` 缺失 / 非正时，即便拿到一个 `dd_trough = 0.0` 也一律判 `INSUFFICIENT_DATA`，不得读成「未触发」。这也是 `R1` 行第 4 条「`dd_trough is None` 不得被当成 0」的同一条纪律的延伸。

## 2. 冻结参数表（权威，全部 `NEW`）

| 参数 | 值 | 来源标记 |
| :--- | :--- | :--- |
| 参考价锚点 | **前收 / 携带前期峰值**（`REFERENCE_PRICE_SEMANTICS = "previous_close"`） | NEW（依据见 §1.3；与生产侧 `:2856` 的 `max(peak_nav, intra_peak)` 同义） |
| 无操作区 | `dd > -2.0%` ⇒ 乘子 `1.0`（**bit-identical**） | NEW（阈值）；`1.0` 语义沿用 [`common/overnight_derisk.py:18-19`](file:///home/stone/Work/github/quant/common/overnight_derisk.py) 的 "1.0 unchanged" |
| 轻档 | `-3.0% < dd ≤ -2.0%` ⇒ 乘子 `0.75` | NEW |
| 中档 | `-4.0% < dd ≤ -3.0%` ⇒ 乘子 `0.50` | NEW（与 `overnight_derisk.downscale_ratio = 0.5`（`:38`）**同值**，但**不同判据**，见 §5） |
| 深档 | `dd ≤ -4.0%` ⇒ 乘子 `0.25`（**不全平**） | NEW（不设 `0.0`，理由见 §3.3） |
| 复位 | `dd` 回到 `> -2.0%` 即恢复 `1.0`，**无滞回 / 无冷却** | NEW（首轮 advisory、无状态） |
| 边界归属 | 等号归**更深**一档（`≤` 即降） | NEW（与 `R4` / `R6` 的 `≤` 纪律同形，阈值本身不共享） |
| 现金代理 | `BIL` / `511880` / `GC001` | 复用 [`common/overnight_derisk.py:10-13`](file:///home/stone/Work/github/quant/common/overnight_derisk.py)（**本轮不进入任何计算**：模块无 symbol 维度，此条仅说明将来接线时的除法边界） |

## 3. 量纲与依据补表（逐项给出「为什么是这个数」）

### 3.1 为什么 `2% / 3% / 4%`，而不是熔断族的 `10% / 15% / 20%`

| 参数 | 量纲 | 依据 |
| :--- | :--- | :--- |
| 轻档 `-2.0%` | 单会话日内回撤（分数） | NEW。熔断族的三档是 `10% / 15% / 20%`（`blend_base.py:654-656`），量纲是**多日（峰值到谷）组合回撤**；**单日**日内回撤的典型幅度约为其**五分之一**，故阶梯取 `2 / 3 / 4%`。两者**不共享常量、不互相覆盖**：熔断族是全账户分级降险，`R1` 是开盘前的一次市场层暴露系数。 |
| 中档 `-3.0%` | 同上 | NEW。三档等距（`2 → 3 → 4`）而非按比例（`2 → 4 → 8`）：日内回撤在高波动日会**频繁穿越**较深的带，等距使相邻档之间留有 1pp 的带宽，避免在主震日于两档之间反复抖动。 |
| 深档 `-4.0%` | 同上 | NEW。`4%` 是「单日盘中已经发生一次可观的实现损失」的量级，继续加深阈值会让该档在正常波动中被反复触发（`-5%` 以上的单日日内回撤在多数 A 股交易日里已是尾部事件）。 |
| 乘子 `0.75 / 0.50 / 0.25` | 目标权重乘数 | NEW。四分之一档线性降档，形状与既有回撤熔断的线性 `deleverage_scale`（`blend_base.py:1223`）同形；`0.50` 与 `overnight_derisk.downscale_ratio`（`:38`）同值属**引用既有减半语义**，不是复用其触发条件。 |

**诚实披露**：`-2.0% / -3.0% / -4.0%` 与 `0.75 / 0.50 / 0.25` 是**运维风险预算约定**（2026-11-08 冻结），**不是**经样本优化的参数、**不是**任何论文的引用值。§2.20 表头已把 `R1` 定性为「待验证的假设清单」，故实现与本文都**不得**为这些数字编造学术出处；需要改写时，**先**以 `## Revision` 段更新本文，**再**改实现。

### 3.2 为什么参考价是前收（见 §1.3）

`hwm_intra` 的锚点是携带峰值 / 前收；开盘价锚点会在跳空低开日把回撤伪装成 `0%`，并让消费侧算出的量与它声称消费的 `dd_trough` 脱钩。详见 §1.3 的三条理由。

### 3.3 为什么深档是 `0.25` 而不是 `0.0`

- `0.0` 是**清仓**，不是「降险」：它把「今天少带一点风险」偷换成「今天不留市场层」。这一句变更属于 `R7`（缺 5m ⇒ 冻结新开仓）与熔断族（`dd_stop_thresh = 0.20`，`blend_base.py:656`）的职责，`R1` 无权越界；
- `R1` 首轮是 **advisory**：一个 advisory 读数若能把满仓归零，它的误报代价就是不可逆的；
- 因此阶梯的**地板是 `0.25`**，与 `overnight_derisk` 的 `0 < ratio < 1` 构造期断言同族：`0.0` 与 `≥ 1.0` 都在语义之外，构造期即拒绝。

## 4. 判据、状态词表与边界约定

**五态闭合词表**（`IntradayDrawdownStatus`，`.value` 字符串即落盘契约）：

| 状态 | 乘子 | 语义 |
| :--- | :--- | :--- |
| `NOT_TRIGGERED` | `1.0` | `dd > -2.0%`；返回**字面量** `MULTIPLIER_UNCHANGED`，不是 `min(1.0, …)` 算出来的浮点。 |
| `DERISK_LIGHT` | `0.75` | `-3.0% < dd ≤ -2.0%`。 |
| `DERISK_MEDIUM` | `0.50` | `-4.0% < dd ≤ -3.0%`。 |
| `DERISK_DEEP` | `0.25` | `dd ≤ -4.0%`（**不是** `0.0`，见 §3.3）。 |
| `INSUFFICIENT_DATA` | `1.0` | 不可测量。**既不是** `NOT_TRIGGERED`，**也不是** `0.0`；且 `drawdown is None`。 |

**边界约定（`≤` 即降，且被测试钉死）**：三条带的等号都归**更深**的一侧 —— `dd = -0.02` 属 `DERISK_LIGHT`（不是 `NOT_TRIGGERED`），`dd = -0.03` 属 `DERISK_MEDIUM`，`dd = -0.04` 属 `DERISK_DEEP`；比阈值高**一个 ULP** 即回到更轻一档。用 `≤` 而非 `<`：阈值是**风险预算**，恰好触到预算即视为预算已达。等价地，实现按 `deep → medium → light` **由深到浅**依次判定。

**三态（`INSUFFICIENT_DATA`）的进入条件，逐条列明**（状态词表不因此扩张，靠 `reason` 分辨）：

1. `hwm_intra` 缺失 / `NaN` / 非有限 / `≤ 0` —— 分母不存在或符号翻转；
2. `price` 缺失 / `NaN` / 非有限 / `≤ 0`，或 `price > hwm_intra`（算出正的回撤，说明 HWM 不是峰值 / 参数被对调）；
3. `dd_trough` **为正**（违反「自峰值到谷的回撤必须 `≤ 0`」的定义）；
4. `price` 与 `dd_trough` **都没给**（根本没有谷值读数）；
5. 给了 `reference_price` 而 `hwm_intra < reference_price`（锚点不自洽，见 §1.3）。

`price` 与 `dd_trough` **同时给**是**接线歧义**（不是数据不足），实现**抛 `ValueError`**：静默挑一个会让同一个量的两个读数互相矛盾而无痕。

**「不可测」与「测到 0」不得混同**：`dd = 0.0` 是合法读数（价格恰好站在高点）⇒ `NOT_TRIGGERED` + `drawdown == 0.0`；而「不可测」⇒ `drawdown is None`。这正是 `R1` 行第 4 条「`dd_trough is None` **不得**被当成 0（既不下调也不静默通过），须显式降级并告警」的实现形态；本轮**不发 `warnings`**（状态词表是唯一信号，批量扫会话不会被刷屏，也不会让「数据不足」丢进告警过滤器），接线轮才由调用方把它落成 ticket 可见字段。

**复位无状态**：阶梯是当日读数的纯函数，`dd` 回到 `> -2.0%` 立即恢复 `1.0`，**无滞回、无冷却**。首轮 advisory 若要冷却，只能把状态塞进可变缓存，那会让判定依赖「被调用了多少次」—— 纯函数不允许。

## 5. 与 `R4` / `R5` / `R6` / `R7` 的边界（不得合并、不得互调）

- **与 `R4`（横截面离散度崩塌，`common/cs_dispersion_exposure.py`）**：`R4` 的判据是**相对**比值带 `D/B`（`0.75·B` / `0.50·B`），`R1` 的是**绝对**回撤分数带（`-2% / -3% / -4%`）。`R4` 行第 5 条原文：「与 `R1` 是**两个独立判据**（回撤幅度 vs 横截面结构），**不得**合并成一条，也**不得**互相复用阈值或互相调用。」实现侧因此**硬性禁止**：
  - 本模块**不** import `common.cs_dispersion_exposure`，也**不**调用它（用例以 AST 断言钉死）；
  - 本模块**不得**出现任何 `0.75·B` / `0.50·B` 形态的**比值**阈值常量；三个阈值必须是**负数**（回撤域），三个乘子才是 `0.75 / 0.50 / 0.25`（乘子域）。两个域在用例里分别断言，故「把 `R4` 的带贴过来」会立刻转红；
  - **不得**提供任何把「回撤乘数 × 离散度乘数」相乘的「综合风险分」。
- **与 `R5`（盘中回撤门禁）**：`R5` 拥有「`dd_trough` 作为门禁」的语义。台账 `:333` 第 1 条原文禁止把 `Diagnostic only (R5)` 语义顺手改成门禁（台账引用的 `:1364` 是登记时快照，现行该标签在 `:2216`，配套文案在 `:1727`）。本轮**不改** `scripts/check_live_portfolio_health.py` 的任何一行，`trough_alert` 与 `dd_defensive_thresh = 0.15` 仍归 `R5`。
- **与 `R6`（隔夜降险，`common/overnight_derisk.py`）**：同层（都作用于市场层暴露），但 `R6` 判**隔夜跳空幅度**（`σ_gap > 2.0 × 基线中位数`）、`R1` 判**日内回撤幅度**。阈值、窗口、时点各自独立；`0.50` 同值属**引用既有减半语义**，不是复用其触发条件。
- **与 `R7`（缺 5m ⇒ 冻结新开仓）**：`R7` 拥有「缺数据 ⇒ 新开仓置零」。`R1` **不**消费 `_missing_5m`、**不**冻结任何订单（`gates_orders=False` 在 dataclass 构造期强制），**不**改 `blend_base.py` 的 5m 开关默认值（仍全 `False`）。

## 6. 首轮交付边界（advisory only，零接线）

本轮（`r1.1`）**只交付纯函数与离线用例**：

- `advisory = True`、`gates_orders = False` 随每一个结果返回，且由 `IntradayDrawdownDecision.__post_init__` **强制**：任何 `advisory is not True` / `gates_orders is not False` 的实例**构造不出来**（同一道围栏的措辞见 `common/specific_alpha.py:536-544` 的 "never a gate"）；
- **刻意不提供** `apply_*_downscale` 之类的应用函数：本轮调用方能做的唯一一件事是**打印**它；
- **不**改 `scripts/run_live_strategy.py`、**不**改票据形状、**不**改写任何权重、**不**碰 `pipeline/research/blend/blend_base.py`、**不**改任何策略类、**不**碰版本轴（无 `BASE_VERSION`、无 per-class `version`、不跑 `--dump-strategies`）；
- 无 I/O、无时钟读取、无网络、无随机、无全局状态、**无告警**。

## 7. 生产面 / 消费面实测（`hwm_intra` / `dd_trough` 有没有消费方）

实测命令：`grep -rn "hwm_intra\|dd_trough" --include=*.py --include=*.js --include=*.md --include=*.json .`

**产出方（唯一）**：[`scripts/check_live_portfolio_health.py`](file:///home/stone/Work/github/quant/scripts/check_live_portfolio_health.py)

| 行 | 作用 |
| :--- | :--- |
| `:2723-2724` | 5m 不可用时置 `None`（未测绝非 `0.0`） |
| `:2791-2792` | 初值取自 `dd_info` 的 `peak_nav` / `drawdown_pct` |
| `:2856-2857` | 盯市日内重算：`hwm_intra = max(peak_nav, intra_peak)`；`dd_trough`（**注意 `:2857` 的 `else 0.0` 退化**，见 §1.4） |
| `:2887-2888` | 落入 `intraday_5m_info` → `stage1_health_report.json::intraday_5m_metrics` |

**消费方（全部为展示 / 诊断，无一适用于下调暴露）**：

| 文件:行 | 用途 | 是否下调暴露 |
| :--- | :--- | :---: |
| `scripts/check_live_portfolio_health.py:1884-1885` | 控制台打印 `Intraday Trough DD` / `Intraday Peak` | 否 |
| `scripts/check_live_portfolio_health.py:2216` | Markdown 表 `Intraday Trough Drawdown … Diagnostic only (R5)` | 否 |
| `scripts/check_live_portfolio_health.py:1722-1727` | `trough_alert` 触发时只追加一行 CAUTION 文案，原文「Diagnostic warning only (R5 backtest parity): monitoring intraday volatility **without forced tier escalation**」（文案在 `:1727`） | 否 |
| `scripts/check_live_portfolio_health.py:2858-2860` | `trough_alert = abs(dd_trough) >= dd_defensive_thresh(0.15) …` —— 只置一个诊断布尔 | 否（真正的缩放字段 `intraday_buy_scale` 在 `:2892` 只由 `distribution_alert`（VWAP 广度）决定） |
| `scripts/check_live_portfolio_health.py:753,768,804` | 读取 `intraday_buy_scale` 进入 health-report 的 `buy_scale`，与 `dd_trough` 无关 | 否 |
| `pipeline/tests/test_nav_source_parity.py:339-358,387-388,429,455,501-502` | 断言产出侧口径（`dd_trough` 保持 diagnostic，`test_nav_source_parity.py:502` 明写「never rewritten from it」） | 否（测试） |
| `pipeline/tests/test_live_path_hardening.py:539` | 夹具里的 `"dd_trough": 0.0` | 否（测试） |

**零命中面**：`scripts/run_live_strategy.py`（**Stage 2 从不读取**，`grep` 返回 0 行 —— 与台账 `:333`「2026-11-01 登记时**未接线**」一致）、`web/`（0 行）、`backtester/`（0 行）。`common/` 内唯一命中是 `common/cs_dispersion_exposure.py:88` 的**散文**（声明 `R4` 不读这两个量），非消费者；`docs/` 下除台账本身外另有 **2 份文件共 3 行**散文命中（`docs/cs_dispersion_exposure.md:96`、`docs/overnight_exposure_contract.md:90` 与 `:179`，均为「阈值不共享」的边界声明，非消费者）。

**结论（对台账 `:333` 第 2 条原话的核对）**：原话「**但当前没有任何消费方用它们下调目标暴露** —— 即『测得到、不消费』」**在实质上成立**：`dd_trough` 被产出、被打印、被置成一个明确标注「不触发分级」的诊断告警，但**从未**用于下调任何目标暴露 / 买入预算。**需订正的措辞**：原文紧跟的「当前没有任何消费方」在**字面上过强** —— 存在**展示与诊断**消费方（上表生产侧 5 处：控制台打印、Markdown 表、CAUTION 文案、`trough_alert` 布尔、`buy_scale` 读取），但不存在**暴露下调**消费方。本文按后者精确表述，不回改台账。

## 8. 失效条件与负结果预期（不得事后粉饰）

按 §2.20 表头「本表是**待验证的假设清单**」的纪律：

- `R1` 的收益前提（**日内回撤深度先于次日市场层下跌**）**尚未在本仓库被证真**；落地后若测不出效果，**按负结果结项是预期结局之一**，不得写成「机制正确、只是样本不够」。
- 明确失效条件：①若 `dd_trough` 的分布高度集中于 `-2.0%` 内侧（例如绝大多数交易日回撤 `< 1%`），则阈值只会被尾部事件触发，样本量不足以判定有效性 —— 此时应以负结果结项，而不是继续下调阈值；②若 `hwm_intra` 的锚点在实盘里频繁等于「前收」（即会话从未创新高），则该读数退化为「相对前收的当日跌幅」，判据的含义变化须另行裁决；③生产侧 `:2857` 的 `else 0.0` 退化若未修，任何把它当作「平稳」的上游都会系统性低估风险。
- 全部验收用例 **100% 离线**、只用合成数据 / 手工构造标量，不读生产报告文件、不读生产缓存、不联网、不依赖 `backtests_research/`。

## 9. 验收映射（用例 ↔ 断言）

| 本轮要求 | 冻结口径 | 对应用例 |
| :--- | :--- | :--- |
| ① 三档边界精确，且 1 ULP 上移回到更轻一档 | `dd = -0.02 / -0.03 / -0.04` ⇒ `0.75 / 0.50 / 0.25`；`nextafter(·, 0)` ⇒ `1.0 / 0.75 / 0.50` | `test_band_boundaries_belong_to_the_deeper_band`、`test_one_ulp_above_each_boundary_moves_to_the_lighter_band` |
| ② 无操作路径 bit-identical | 字面量 `1.0`（`repr == "1.0"`），含 `dd = 0.0` | `test_no_op_path_returns_exactly_one` |
| ③ 三态 | 缺 / `NaN` / `0` / 负 `hwm_intra` ⇒ `1.0` + `drawdown is None` + `INSUFFICIENT_DATA` | `test_missing_or_degenerate_inputs_are_insufficient_not_zero` |
| ④ 阶梯单调 | 回撤越深，乘子**永不更高** | `test_ladder_is_monotonic` |
| ⑤ 无状态复位 | 同一调用两次结果全等；交错调用互不影响 | `test_ladder_is_stateless` |
| ⑥ 围栏在所有状态成立 | `advisory=True` / `gates_orders=False`，且反向构造**抛错** | `test_advisory_fence_in_every_state` |
| ⑦ 不得 import / 调用 `R4` 模块 | AST：无 import、无名字引用 | `test_module_never_imports_or_calls_r4` |
| ⑧ 不得复制 `R4` 的比值带常量 | AST：阈值常量必须全为负、乘子域与阈值域分离、无 `R4` 常量名 | `test_no_r4_ratio_band_constants_are_copied` |
| 附加（`price` / `dd_trough` 双给 / 域保护） | 双给抛 `ValueError`；正回撤 ⇒ `INSUFFICIENT_DATA`；`anchor_hwm_intra` 镜像生产锚点 | `test_price_and_dd_trough_are_mutually_exclusive`、`test_positive_drawdown_is_out_of_domain`、`test_anchor_mirrors_the_producer`、`test_config_refuses_inverted_or_positive_bands` |

## 10. 与台账 `R1` 行措辞的对齐说明

| 行文措辞 | 本文落地 | 理由 |
| :--- | :--- | :--- |
| 「先冻结触发阈值与下调比例」 | `-2.0% / -3.0% / -4.0%` ⇒ `0.75 / 0.50 / 0.25`，四条带 + 无操作区，等号归更深一档 | 见 §2 / §3；`≤` 的理由是「风险预算已达」。 |
| 「必须与 `dd_trough is None`（未测）区分开」 | 封闭五态 + `drawdown is None`，且 `INSUFFICIENT_DATA` 的乘子强制为字面 `1.0` | 见 §4；`0.0` 会读作「恰好站在高点」。 |
| 单测文件名 `pipeline/tests/test_intraday_exposure_damping.py` | 实际交付 `pipeline/tests/test_intraday_drawdown_derisk.py` | **刻意偏离**：`damping`（阻尼）与 `derisk`（降险）不是同一个语义，且 `R4` 的实现文件同批命名为 `cs_dispersion_exposure`；本批统一用 `<判据>_derisk` 命名，便于与 `overnight_derisk` 族对齐。文件名不影响验收内容。 |
| 「结果写进 Stage 1 报告与 ticket 的可观测字段」 | **本轮不接线**：只交付纯函数 + advisory 围栏 | 台账 `:333` 第 4 条的验收依赖接线，但那是一项**独立接线任务**（须触碰 `scripts/run_live_strategy.py` 与票据形状）；与 `R4` 同批的纪律是「首轮零接线、接线另行裁决」。故本文只冻结判据，并把 `reason` / `status` / `dd_source` / `reference_semantics` 全部落进 dataclass 字段，为接线预留可观测面。 |
| 「`>=` 与 `>` 择一并写明理由」 | 选用 `dd <= 阈值`（即「回撤幅度 `>=` 阈值」） | 阈值是风险预算：恰好触到即视为已达；`<` 会让边界值落在更轻一档，等于给预算留了半个带宽的空隙。 |
