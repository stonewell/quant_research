[ [English](README.md) | 简体中文 ]

# 实盘量化交易与风控部署平台 (`web/livetrading`)

部署在 `web/livetrading` 的机构级响应式实盘量化交易工作站与执行控制台。
提供完整的一阶（Stage 1）宏观健康度与熔断门禁检查、二阶（Stage 2）实盘订单生成（先卖后买、持仓惰性过滤维持、闲置现金归集）、自定义股票池实时管理、交互式持仓编辑以及每日不可变历史运行快照归档。

---

## 🌟 核心特性

1. **标的池配置 (Universe Configuration)**：
   - 支持跨区域核心-卫星资产预设（中国 A 股、美股、港股、全球 ETF）。
   - 支持在 Web 界面实时自定义标的池并保存到个人标的库 (`custom_universes.json`)。
2. **策略选择 (Strategy Selection)**：
   - 自动扫描并加载策略转储文件（如 `chan_four_state_blend`、`chan_risk_managed_blend`、`chan_crisis_shield_blend`、`chan_dual_hybrid_blend`、`generic_blend` 等）。
   - 默认自动选中国家级/机构级实盘冠军：**缠论四状态风控混合策略** (`chan_four_state_blend`)，原生融合 60 日滚动 IC-IR 贝叶斯自适应赋权 ($t \ge 2.0$)、Löwdin 对称正交化去共线性 ($W^* = W(W^T W)^{-1/2}$)、Garman-Klass 极值波动率目标化及缠论有限状态机 (FSM) 中枢“拒绝盘整”空仓纪律。
3. **交互式持仓与现金管理 (Interactive Holdings & Cash Management)**：
   - 增删改现有持仓（支持股数或目标权重）。
   - 实时计算组合总资产净值 (NAV)、闲置资金与现金占比。
   - 持仓账本**仅**持久化于运行归档中（取簿优先级见第 6 项）；不再存在任何全局持仓文件。
   - **手工保存同时记录"这是人确认过的账本"。** `POST /api/holdings`（未钉住运行目录的保存）会在逐字节相同的 `holdings.json` 旁写入 `operator_holdings.json`：`{"saved_at", "source": "operator_manual_correction", "holdings"}`。它是**部分成交**的唯一记录（本系统不接收任何券商成交回执），因此在**展示**与**种子**两条路径上都压过该轮的计划账本。部署路径始终钉住自己的运行目录，故永不写这个文件。
4. **一阶：组合健康度与宏观门禁 (Stage 1: Portfolio Health & Macro Gate)**：
   - 计算自历史最高净值 (HWM) 的峰谷回撤。
   - 评估熔断层级（正常、一阶阻尼、二阶防守、三阶暂停冷冻）及自愈冷却计数，全面兼容混合策略参数前缀 (`cfsb_`, `crb_`, `ccsb_`, `cdhb_`, `blend_`)。
   - 计算 50 日市场宽度 ($\ge 30\%$) 与 10 日动量冲力 ($\ge 60\%$)。
   - 计算 Barroso 21 日实现波动率缩放因子 ($\sigma = 12\%$)，优先采用 Garman-Klass (1980) OHLC 高精极值估计器（具备收盘价方差回退保障）。
   - 输出醒目的直观操作指令徽章：`🟢 GO`、`🟡 CAUTION`、`🔴 NO-GO`。
5. **二阶：实盘交易单生成 (Stage 2: Live Trading Ticket Generation)**：
   - 将目标权重与当前持仓自动折算为具体委托交易单。
   - 严格执行阶段顺序：**第一阶段【先卖出】**（释放购买力） $\rightarrow$ **第二阶段【后买入】**（按整手取整建仓） $\rightarrow$ **第三阶段【持仓惰性过滤维持】** $\rightarrow$ **第四阶段【闲置资金归集】**。
   - 落实 5% 调仓换手摩擦过滤、单票仓位上限及各市场整手规则（A 股 100 股一手向下取整、美股 1 股、港股整手）。
   - 一键导出 CSV 券商执行清单。
6. **不可变每日快照归档与历史日期浏览 (Daily Snapshot Archive & Browser)**：
   - 每个交易日生成完整快照包并存储于 `<archive_dir>/<strategy>/<universe>/<date>/`（同一交易日多轮运行时另加 `<date>/<HHMMSS>/` 实例目录）：
     - `holdings.json`：当日组合持仓。
     - `operator_holdings.json`：**仅**当操作者为该会话手工保存过账本时存在（见第 3 项）；人工确认的账本，形如 `{"saved_at", "source", "holdings"}`。
     - `account_state.json`：当日账户净值、历史高点、回撤、熔断层级及冷却计数。
     - `stage1_health_report.json`：当日健康度门禁评估报告。
     - `live_trading_ticket.json` 与 `live_trading_ticket.csv`：当日生成的交易指令清单。
     - `post_holdings.json`：该轮交易单假设成交后的计划持仓账本。
     - `summary.json`：运行参数、执行耗时与日志输出。
   - **持仓与账户状态只持久化于此**：当请求未携带显式账本（正常 UI 流程发送 `current_holdings: null`）时，部署按以下顺序取种子：本会话的操作者手工修正（`operator_holdings.json`）→ 该轮计划盘后账本（`post_holdings.json`）→ 从未跑完二阶的会话目录中的快照（`holdings.json` / `current_holdings.json`）→ 上一轮的计划账本；全部皆无时拒绝生成交易单——以空账本定量会把已经卖出的仓位重新买回来。
   - **提交的空账本被拒绝，而非被当作"全现金"。** 形如 `{}`、全为零或每一行都没有可用股数的持仓簿会被拒绝（`EmptyBookConfirmationRequiredError`），因为引擎会为每个当前持仓标的生成卖出工单——等价于一次整仓清仓。真正意图是全现金的账户应每次请求中显式传 `confirm_empty_book: true`（Web UI 会先弹窗向操作员确认）；该轮归档时以 `holdings_seed_source = "operator_confirmed_empty_book"` 落盘，以便审计时与操作者手动录入的持仓清单（`operator_input`）区分开来。该门禁刻意不设环境变量开关（不同于 `LIVETRADING_STRICT_SEED_BOOK`），因为"表格还没填"绝不是合法的"全部现金"指令。
   - **提交的账本与同日计划完全一致时被拒绝，除非调用方明确声明已核实。** `edited: true`（`POST /api/run-deploy` 与 `POST /api/deploy/multi` 均接受）表示提交的账本已**对照券商实际成交**核实，绝不推断。未标记时，若提交的**非空**账本等于该日已完成的计划账本，会在任何副作用之前抛 `UnconfirmedPlanAsInputError`——因为 UI 会把计划账本画进表格，原样回传会使每个目标都等于当前持仓，生成**零张订单**却被归档为成功。该门禁对尚无计划的日期（全新会话）永不生效，且与 `confirm_empty_book` **相互独立**：后者授权空账本（整仓清仓），前者授权计划形态的非空账本；二者互不授权。Web UI 在操作者实际编辑过表格之前发送 `current_holdings: null`（保留键）并省略 `edited`。
   - **状态标签（面向审计的封闭取值域）。** `bundle["holdings_source"]`（UI 展示口径）恰为以下之一：`operator_manual_correction` → `post_trade_book` → `snapshot` → `pre_trade_fallback`（按优先级排序；最后一项表示"存在二阶产物但计划不可解析"，即回落展示原始盘前快照并**打标签**）。`summary.json["holdings_seed_source"]`（部署定量口径）恰为以下之一：`operator_input`、`operator_confirmed_empty_book`、`operator_manual_correction`、`operator_snapshot`、`previous_post_holdings`、`legacy_get_holdings_fallback`、`none`。
   - Web 端 `data/` 目录**只存放配置**（`settings.json`、`settings.local.json`、`custom_universes.json`、`multi_strategy_configs.json`），绝不保存持仓或账户状态。
   - 随时切换查看任意历史日期的持仓、账户状态与交易指令。
7. **全路径零硬编码 (Zero Hardcoded Paths)**：
   - 所有路径（仓库根目录、策略目录、标的目录、数据目录、归档目录、缓存目录及脚本）均可通过 CLI 参数、环境变量、`settings.json` 及应用内“设置”弹窗灵活配置。
8. **桌面与移动端全响应式设计 (Responsive UI)**：
   - 桌面端 ($\ge 1024\text{px}$)：双列高信息密度专业交易工作站。
   - 移动端 ($< 1024\text{px}$)：触控友好的分段标签页、底部吸顶快捷操作栏及响应式卡片流。

---

## 🚀 快速启动

在 `web/livetrading/` 目录下：
```bash
# 1. 安装/同步依赖（首次运行）
uv sync

# 2. 启动 Web 服务（默认端口 8080）
uv run python app.py
```

或从仓库根目录启动：
```bash
uv run --project web/livetrading python web/livetrading/app.py --port 8080
```

在浏览器打开 `http://127.0.0.1:8080` 即可访问。
交互式 OpenAPI Swagger 文档可在 `http://127.0.0.1:8080/docs` 查看。

---

## ⚙️ CLI 选项与路径配置

所有路径均可通过命令行参数或环境变量配置：

| 参数 | 环境变量 | 默认值 | 描述 |
| :--- | :--- | :--- | :--- |
| `--port` | `PORT` | `8080` | HTTP 服务监听端口 |
| `--host` | `HOST` | `127.0.0.1` | 绑定的网络接口 |
| `--repo-root` | `LIVETRADING_REPO_ROOT` | 自动检测 | 量化工作区根目录 |
| `--strategy-dir` | `LIVETRADING_STRATEGY_DIR` | `<repo_root>/pipeline/.../strategy_dumps` | 策略 JSON 转储目录 |
| `--universe-dir` | `LIVETRADING_UNIVERSE_DIR` | `<repo_root>/docs/universe` | 预定义标的池文件目录 |
| `--data-dir` | `LIVETRADING_DATA_DIR` | `web/livetrading/data` | 持仓与自定义标的存储目录 |
| `--archive-dir` | `LIVETRADING_ARCHIVE_DIR` | `<data_dir>/runs` | 每日历史运行快照归档目录 |
| `--cache-dir` | `LIVETRADING_CACHE_DIR` | `<repo_root>/data` | DuckDB 市场数据缓存目录 |
| `--stage1-script` | `LIVETRADING_STAGE1_SCRIPT` | `scripts/check_live_portfolio_health.py` | 一阶健康度门禁执行脚本 |
| `--stage2-script` | `LIVETRADING_STAGE2_SCRIPT` | `scripts/run_live_strategy.py` | 二阶通用实盘交易单生成脚本 |
| `--python-bin` | `LIVETRADING_PYTHON_BIN` | 当前 Python (`sys.executable`) | 脚本执行所用的 Python 解释器 |

---

## 📡 REST API 接口参考

| 方法 | 端点 | 描述 |
| :--- | :--- | :--- |
| `GET` | `/api/config` | 发现的策略、标的池、路径与默认值 |
| `GET` | `/api/settings` | 当前路径与运行时设置 |
| `POST` | `/api/settings` | 动态更新路径与配置 |
| `GET` | `/api/universes` | 列出预设及自定义保存的股票池 |
| `POST` | `/api/universes` | 新增/保存自定义命名股票池 |
| `DELETE`| `/api/universes/{name}` | 删除自定义股票池 |
| `GET` | `/api/holdings?date=YYYY-MM-DD` | 获取最新或历史指定日期的持仓快照 |
| `POST` | `/api/holdings` | 保存/更新当前持仓 |
| `GET` | `/api/account-state?date=YYYY-MM-DD` | 获取最新或历史指定日期的账户状态 |
| `POST` | `/api/account-state` | 更新账户状态 |
| `POST` | `/api/run-health` | 执行一阶健康度检查并归档报告 |
| `POST` | `/api/run-deploy` | 执行二阶实盘部署并归档交易单 |
| `GET` | `/api/runs` | 获取所有历史运行归档日期列表 |
| `GET` | `/api/runs/{date}` | 获取指定日期的完整归档数据包 |
| `GET` | `/api/runs/{date}/csv` | 下载生成的交易单 CSV 文件 |

---

## 🧪 单元测试

完全离线运行自动化测试套件：
```bash
uv run --project web/livetrading pytest web/livetrading/tests -q
```
