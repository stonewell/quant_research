# 信号层 Yellow / Orange / Red 告警矩阵（`AT5`，advisory）

- 任务行：[`docs/development_plan.md:411`](file:///home/stone/Work/github/quant/docs/development_plan.md)（`AT5`）
- 代码：[`common/signal_alerts.py`](file:///home/stone/Work/github/quant/common/signal_alerts.py)
- 测试：[`pipeline/tests/test_signal_alerts.py`](file:///home/stone/Work/github/quant/pipeline/tests/test_signal_alerts.py)
- 上游：`AT3` [`common/ic_decay_monitor.py`](file:///home/stone/Work/github/quant/common/ic_decay_monitor.py)（`LegICReport.status` / `ic_ir`）、`AT4` [`common/live_capacity.py`](file:///home/stone/Work/github/quant/common/live_capacity.py)（`capacity_labels(...)` 的标签与 `exceeds_*` 事实标志）

本文件是 `AT5` 要求的**映射冻结书**：先冻结「指标 → 层级 → 动作」，冻结之后才允许写码（`AT5` 验收第 1 条）。代码中的 `FROZEN_MAPPING` 与下表逐行一致，测试双向断言，改一处而漏另一处会失败。

---

## 一、冻结映射（逐行照抄，不得合并或改写既有阈值）

层级：`GREEN` / `YELLOW` / `ORANGE` / `RED` / `INSUFFICIENT`。动作一律是**报告文本**，不产生任何下单 / 降权 / 清盘指令。

| 输入（来源） | 取值 | 层级 | 动作（报告文本） |
| :-- | :-- | :-- | :-- |
| `AT3` `status` | `INSUFFICIENT_HISTORY` | `INSUFFICIENT` | 「样本不足，不出结论」 |
| `AT3` `ic_ir` | `<= -0.50` | `RED` | 「腿 IC 反向显著，建议人工复核该腿」 |
| `AT3` `ic_ir` | `-0.50 < x <= -0.25` | `ORANGE` | 「腿 IC 转负，建议关注」 |
| `AT3` `ic_ir` | `-0.25 < x < 0` | `YELLOW` | 「腿 IC 轻微负值」 |
| `AT3` `ic_ir` | `>= 0` | `GREEN` | 「正常」 |
| `AT4` `turnover_label` | `BREACH` | `ORANGE` | 「换手超 `L2.36` 硬上限（标签引自 AT4，不在此重算）」 |
| `AT4` `turnover_label` | `WATCH` | `YELLOW` | 「换手达合格线」 |
| `AT4` `turnover_label` | `MISSING` | `INSUFFICIENT` | 「换手不可测」 |
| `AT4` `participation` | `available=False` | `INSUFFICIENT` | 「参与率不可测」 |
| `AT4` `participation` | exceeds_daily_volume 或 exceeds_average_volume | `YELLOW` | 「参与率超当日/均量」 |

补充（表的闭包，不改动表中任何一行）：

- `AT3` `status == "OK"` 且 `ic_ir` 为非有限值（`NaN` / `±inf` / 缺失）⇒ `INSUFFICIENT`（「样本不足，不出结论」的同类语义：不可测）。`NaN` 与任何界比较都为 `False`，若不加这条，`NaN` 会穿透成「正常」。
- `AT4` `turnover_label` 缺失或不在 `OK / WATCH / BREACH / MISSING` 内 ⇒ `INSUFFICIENT`（「换手不可测」）。
- `AT4` 参与率 `available=True` 但两个 `exceeds_*` 都不可读 ⇒ `INSUFFICIENT`（「参与率不可测」）。
- `AT4` 参与率 `available=True` 且 `exceeds_*` 均为 `False` ⇒ `GREEN`（「正常」）。

## 二、聚合规则（冻结）

1. 序数严重度：`GREEN < YELLOW < ORANGE < RED`（`severity_rank()`；`INSUFFICIENT` **不在**序数轴上，对它调用 `severity_rank()` 直接抛错，避免「无法判断」被当成「正常」的邻居来比较）。
2. 取所有输入层级中的**最严重**一档（`RED > ORANGE > YELLOW > GREEN`）。
3. **若没有任何输入，或所有输入都是 `INSUFFICIENT` ⇒ 输出 `INSUFFICIENT`**，绝不降级为 `GREEN`。
4. **若存在 `INSUFFICIENT` 与正常输入混合 ⇒ 输出 `INSUFFICIENT`**，并在 `notes` / `missing_dimensions` 里列出缺失维度。「无法判断」不得当作「正常」——完整性优先于严重度。

第 4 条的后果是刻意的：一条 `RED` 腿配上不可测的参与率，整体是 `INSUFFICIENT` 而不是 `RED`——**部分可测的矩阵不能宣称有界的严重度**。`RED` 并未丢失：`AlertMatrix.worst_measured_level` 仍保留「最严重的已测档位」，`missing_dimensions` 逐条点名缺失维度，报告同时说清「有一条红腿」与「这个矩阵不完整」。

输入三态：`build_alert_matrix(capacity=None)` 表示该维度**未提供**（不计入条目）；而提供了一份键缺失的 capacity 映射表示**提供了但不可测**（计入 `INSUFFICIENT`）。两者不混淆。

## 三、五档语义

| 层级 | 含义 | 允许的后续动作 |
| :-- | :-- | :-- |
| `GREEN` | 该维度已测量且在正常区间内 | 仅记录 |
| `YELLOW` | 已测量、轻微越界（腿 IC 轻微负值 / 换手达合格线 / 参与率超当日或均量） | 仅记录，人工留意 |
| `ORANGE` | 已测量、明确越界（腿 IC 转负显著 / 换手超 `L2.36` 硬上限） | 生成报告供人工复核 |
| `RED` | 已测量、反向显著（腿 IC `<= -0.50`） | 生成报告，建议人工复核该腿 |
| `INSUFFICIENT` | 不可测 / 样本不足 / 输入缺失 | 只报告缺失维度，**不得**被读作正常，也不触发任何调整 |

## 四、advisory 围栏

- `AlertMatrix` 恒带 `advisory=True`、`gates_orders=False`；构造时传入 `gates_orders=True` 或 `advisory=False` 抛 `AdvisoryContractError`。围栏是**代码强制**的，不只是注释。
- 所有 `action` 都是给人看的**报告文本**；`code` 是机器可读标签。二者都不是权重乘数、不是清盘触发、不是下单参数。
- 本模块不 import `scripts/`、`web/`、`subprocess`、`requests`，不做任何 IO，不认识任何账户 / 票据 / 权重对象。
- `to_dict()` 严格 JSON：非有限浮点一律降为 `null`，`json.dumps(..., allow_nan=False)` 直接可序列化。

## 五、首轮不接线的声明

`AT5` 首版**只出报告**，不接降权 / 清盘执行（`AT5` 验收第 4 条）：

- 未接入 `scripts/run_live_strategy.py` 的票据生成路径，`detail_reason` 里没有 `AT5` 字段；
- 未修改任何策略 / 模板 / 混合策略的权重逻辑，因此**不触发** `AGENTS.md` §7 的版本轴义务（无 `BASE_VERSION` / 类 `version` 升版、无 `--dump-strategies`）；
- 未改动 `strategies_config.json`，未改动 `web/livetrading`；
- 下游若要消费，应当把 `AlertMatrix.to_dict()` **当作报告**渲染或归档，不得据此改写权重或跳过下单——那需要另立任务、另行冻结，并明确写出与本节的关系。

## 六、为什么不复用既有阈值（逐条对照）

`AT5` 验收第 2 条：各维阈值不得互相复用、不得合并或改写。下表中 `AT5` 与每一族既有阈值的关系逐行说明；`AT5` 自己的切点只有 `IC_IR_RED = -0.50`、`IC_IR_ORANGE = -0.25`、`IC_IR_YELLOW_FLOOR = 0.0` 三个（IC-IR 轴），以及一组**新写**的层级词，不借用任何既有数字。

| 既有阈值（归属） | 它管的维度 | `AT5` 的关系 |
| :-- | :-- | :-- |
| 组合层回撤熔断 `10% / 15% / 20%`（`pipeline/research/blend/blend_base.py` 的 `dd_*_thresh`） | 多日净值回撤 | `AT5` 不读净值、不定义回撤档位；`10/15/20` 一律不出现 |
| `LV1`（新鲜度 / 陈旧度契约） | 数据新鲜度 | 不同维度，`AT5` 不消费、不改写 |
| `R1` 日内回撤 `-2% / -3% / -4%`（`common/intraday_drawdown_derisk.py`） | 单日回撤 | `AT5` 不 import `R1`、不复用其阶梯，也不产生暴露乘数 |
| `R4` 横截面离散度 `0.75 / 0.50·B`（`common/cs_dispersion_exposure.py`） | 离散度相对自身基线 | `AT5` 不 import `R4`、不读离散度、不复用 `0.75 / 0.50` 乘数带 |
| `R6`（隔夜暴露） | 隔夜缺口暴露 | 不同维度，`AT5` 不消费 |
| `L2.36` 换手 `8.0× / 10.0×`（单边年化，`common/live_capacity.py` 的唯一阈值持有者） | 换手 | `AT5` **只消费标签**（`BREACH` / `WATCH` / `MISSING`），不重算任何换手数值、不定义第二个换手阈值 |
| 参与率百分比上限（`AT4` 刻意不设） | 参与率 | `AT5` 只消费 `AT4` 的**事实标志** `exceeds_daily_volume` / `exceeds_average_volume`，不自造比例切点 |

可检验性：测试对 `common/signal_alerts.py` 的全部浮点字面量做 AST 扫描，断言集合中**不含** `8.0`、`10.0`、`0.75`、`0.50`、`0.10`、`0.15`、`0.20`、`-0.02`——「没有偷偷复用」是被断言的，不是自述。

## 七、与上游的边界

- `AT3` / `AT4` 的数字由此二者计算，`AT5` 只做「读字段 → 定档 → 出文本」，不重算 IC、ICIR、半衰期、换手、参与率中的任何一个。
- `AT4` 的 `capacity_labels(...)` 是推荐入口（扁平、JSON-safe、`advisory`/`gates_orders` 内建）；直接把裸 `TurnoverObservation` 传进 `alert_for_capacity(...)` 而没有标签时，降级为 `INSUFFICIENT`（「换手不可测」）而不是在这里自行分档——档位是 `L2.36` 的，归 `AT4` 所有。
- `AT5` 的状态字符串镜像 `AT3` 的公开常量（`STATUS_OK` / `STATUS_INSUFFICIENT_HISTORY`），不 import、不复制私有阈值；测试钉住字面相等，`AT3` 改名会在 `AT5` 的测试里炸而不是静默错标。
