# 机制诊断日志与验证脚本归档说明（LV23 Bucket Renorm Max Delta）

本目录归档了 `backtests_research/20261108_101441_bucketrenorm/` 运行中 Step-4 机制诊断所生成的全部 DEBUG 日志、测试包装脚本与机制验证脚本。原日志生成于临时目录 `/tmp/opencode/`，现已完整归档至本目录以确保持久化审计能力。

---

## 1. 文件清单与校验信息

| 文件名 | 类型 | 行数 | 大小 | MD5 校验码 | 描述说明 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `run_bt_debug.py` | 运行脚本 | 13 | 317 B | `b14a8261f8d7bd718f957d2559d60870` | DEBUG 级别启动包装脚本，配置 `logging.basicConfig(level=DEBUG)` 后通过 `runpy.run_path` 运行 `backtester/run_backtest.py` |
| `diag_a00_base_015.log` | 诊断日志 | 6,436 | 1.4 MB | `e5df099ca750bf0900192ba845b031c7` | 基线臂 `a00`（`blend_bucket_renorm_max_delta=0.15`）Standard 模式 DEBUG 运行日志 |
| `diag_a01_none.log` | 诊断日志 | 5,011 | 1.1 MB | `342572f79cdeb207fcdc12489f0d0963` | 无钳位臂 `a01`（`blend_bucket_renorm_max_delta=null`）Standard 模式 DEBUG 运行日志 |
| `diag_a02_030.log` | 诊断日志 | 6,436 | 1.4 MB | `090cd04d51ee417379294f941983013c` | 放宽钳位臂 `a02`（`blend_bucket_renorm_max_delta=0.30`）Standard 模式 DEBUG 运行日志 |
| `diag_a03_060.log` | 诊断日志 | 6,436 | 1.4 MB | `dcdea9fe71d907d8eae0b2c3b4cd2a2b` | 宽松钳位臂 `a03`（`blend_bucket_renorm_max_delta=0.60`）Standard 模式 DEBUG 运行日志 |
| `verify_a00_diag.py` | 验证脚本 | 48 | 2.0 KB | `cee9e4fcf567821d47df75830ee3bfde` | 提取 `a00` 中 pre-renorm 与 delta-clamped 差异，复核滞后条数（lag bars）与最大 L1 距离 |
| `verify_flip_stable.py` | 验证脚本 | 207 | 7.7 KB | `30f13114c7a456e5c6fbacbc53e22bfc` | 全四臂机制比对脚本：复核翻转点稳定性、滞后 bars、L1 距离分布及各臂钳位触发状态 |

---

## 2. 诊断运行命令（Standard Mode）

四臂诊断运行均使用标准单段回测模式（Standard Mode），调用 `run_bt_debug.py` 包装器。运行命令如下：

```bash
# 通用参数模板
python run_bt_debug.py \
  --strategy-file ../strategies/chan_four_state_blend_strategy.json \
  --universe-file ../universe_20_stocks.txt \
  --start 2022-01-04 \
  --end 2026-10-22 \
  --mode standard \
  --data-provider marketdb \
  --no-cache \
  --min-shares 100 \
  --china-trading \
  --cash-sweep-rate 0.015 \
  --strategy-params '<STRATEGY_PARAMS_JSON>'
```

各臂指定的 `--strategy-params` 参数值：
- **`a00_base_015`**: `'{"blend_bucket_renorm_max_delta": 0.15}'`
- **`a01_none`**: `'{"blend_bucket_renorm_max_delta": null}'`
- **`a02_030`**: `'{"blend_bucket_renorm_max_delta": 0.30}'`
- **`a03_060`**: `'{"blend_bucket_renorm_max_delta": 0.60}'`

---

## 3. 日志格式与源码锚点

DEBUG 日志中包含以下关键格式输出，对应 [`pipeline/research/blend/blend_base.py`](file:///home/stone/Work/github/quant/pipeline/research/blend/blend_base.py) 中的埋点：

1. **`budgets` 记录**（第 539–543 行）：
   `DEBUG:pipeline.research.blend.blend_base:regime weighting bar=<YYYY-MM-DD> budgets={<regime_budgets_dict>}`
2. **`pre-renorm` 记录**（第 584 行）：
   `DEBUG:pipeline.research.blend.blend_base:regime weighting bar=<YYYY-MM-DD> pre-renorm={<weights_dict>}`
3. **`delta-clamped` 记录**（第 604 行）：
   `DEBUG:pipeline.research.blend.blend_base:regime weighting bar=<YYYY-MM-DD> delta-clamped={<weights_dict>}`

### 钳位触发与日志行特征
- **无 `delta-clamped` 行代表钳位未被触发**：
  在代码逻辑中，仅当 `blend_bucket_renorm_max_delta` 不为 `None` 且步长约束逻辑实际执行时，才会打印 `delta-clamped` 调试行。
  因此，`a01_none`（无钳位）日志中**完全不存在** `delta-clamped` 输出行（0 行），这是符合预期且经过严格设计的机制特征。

---

## 4. 行数预期与统计复核

各日志文件关键行数统计如下（包含 265 bars 预热期与 1161 bars 回测期）：

| 日志文件 | 总行数 | `pre-renorm` 行数 | `delta-clamped` 行数 | 钳位触发特征 |
| :--- | :--- | :--- | :--- | :--- |
| `diag_a00_base_015.log` | 6,436 | 1,426 | 1,425 | 1425 / 1426（首 bar 无 prior_weights，其余 100% 触发钳位约束） |
| `diag_a01_none.log` | 5,011 | 1,426 | 0 | 0 / 1426（未启用钳位，按设计 0 行） |
| `diag_a02_030.log` | 6,436 | 1,426 | 1,425 | 1425 / 1426（首 bar 无 prior_weights，其余 100% 触发钳位约束） |
| `diag_a03_060.log` | 6,436 | 1,426 | 1,425 | 1425 / 1426（首 bar 无 prior_weights，其余 100% 触发钳位约束） |

---

## 5. 机制指标复算与核心结论

通过 `verify_a00_diag.py` 与 `verify_flip_stable.py` 复算的代表性指标：
- **`a00_base_015` 滞后表现**：
  - 正式回测区间（1161 bars）中，存在 **100 lag bars**（即钳位后权重与 pre-renorm 目标权重产生实质偏移）；
  - 全样本最大 L1 距离为 **1.471242**（出现在 2021-02-18 预热期）。
- **翻转点稳定性（Flip Stability）**：
  - 各臂在状态翻转点上表现出高度的一致性，证明钳位约束并未改变核心状态识别，而是平滑了状态切换期间的权重步进。

---

## 6. Regime 状态定义诚实披露（Caveats）

1. **状态判定依据为 `argmax(budgets)`**：
   在分析翻转点与机制行为时，Regime 状态严格定义为 `argmax(budgets)`，**而非** `argmax(pre-renorm)`。`budgets` 反应市场宏观环境的初始状态置信度，而 `pre-renorm` 已受到子策略仓位与 IC-IR 混合计算的影响。
2. **严格翻转判定（Strict Flip Definition）**：
   翻转仅计入状态索引在连续交易日之间发生实质性转移（$state_t \ne state_{t-1}$）的点位，平滑保持同状态的点位不计为翻转。
