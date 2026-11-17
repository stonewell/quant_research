# 2026-11-17 实盘操作手册（chan_four_state_blend / 波动率上限切换为组合口径）

> 任务、状态与验收登记见 [`docs/development_plan.md` §2.27](development_plan.md)（`NV1`–`NV13`）。
> 本文只作当日操作手册（operator runbook），不登记任务，也不是策略文档：不解释模型、不改版本轴。

## 0. 结论：可以执行

- dump 已是 `strategy_version 1.5.0` / `base_version 2.0.0`，且
  `cfsb_vol_ceiling_scope = "portfolio"`。
- `select_prior_state` 只比对 `base_version`（`scripts/live_preflight.py:1123-1132`），
  不看 `strategy_version`；base 仍为 `2.0.0` ⇒ `1.4.0→1.5.0` 不会跳过 11-16 快照，
  不会冷启动，`−8%` 止损基准不会丢。

一句话：版本轴只动了 per-class `version`，先验快照链只认 `base_version`，所以
「升级策略版本」与「沿用昨天账簿」这两件事不冲突。

执行路径二选一：**09:30 前**走 §1 阶段 A′（首选，重跑 11-16 一步建仓）；
**已过 09:30** 才走 §2 阶段 A → §3 阶段 B 的两段式。

## 1. 阶段 A′（首选）：若 11-17 09:30 尚未到，直接重跑 11-16

判定只看时间，不看日期：`now < 2026-11-17 09:30 (+08)`。

- **仍满足** ⇒ 以 `as_of = 2026-11-16` 重跑 Stage 1 → Stage 2，用新口径重出一张票，
  11-17 开盘**一次**建仓到 ~69%，省掉「先建到 32.3% 再补到 69%」的两段式
  与约 ¥48,000 的往返损耗。
- **已过 09:30** ⇒ 不能重跑。声明成交时点已过，闸门会以 `after_open` 拒绝
  （11-13 / `192803` 的 `lateness_minutes=119.2` 即此情形）；`--allow-intraday` 只放宽窗口，
  不放行已过的开盘，且收盘价参考价已失真。此时走下文阶段 A → B 的两段式路径。

重跑前必须先确认旧票的成交情况：

| 情形 | 账本处理 |
| --- | --- |
| 11-17 开盘一笔未成交 | 账本不变（400 / 1200 / 200），直接重跑 |
| 已部分成交 | 先按券商实际成交改账本，再重跑；否则新票基于假账本定量 |

同一句核对先做一遍：**先核对、再重跑；绝不在计划账本上重跑。**
表格里若出现 `601728` / `600900` 这类从未持有过的标的，那是 `post_holdings.json` 的计划值，
不是账户现状（详见 §3 B3 的警告框）。

重跑后核对（一步到位，不再有 32.3% 的中间态）：

- 回撤 `−3.08%` 左右、HWM `203,049.96`、Tier `NORMAL`；
- **页面若显示 `−5.77%` / `¥208,830.78`**：引擎读到的是**陈旧的 `prior_state`** ——
  同日重跑走的正是这条路径（见 §3 B4 的 `prior_state` 一行与核对命令）；
- `scope = portfolio`、因子 ≈`1.00`、`capped_equity_sum ≈ 0.75`、`stage1_factor ≈ 0.396`（对照）；
- `EQUITY DIRECTION` 一次性**加仓** ≈ `+37pp` / 净买入 ≈ ¥70,000。

旧实例 `2026-11-16/124508` **不删不改**：它是「当时按 `stage1` 口径下过的指令」的不可变证据；
新实例落在 `2026-11-16/<新HHMMSS>/`。该日的取代关系**已经落盘**：
`2026-11-16/supersession.json` 记 `superseded_by 2026-11-16/142959`、
`authoritative_run 142959`、`strategy_version_observed "1.4.0 -> 1.5.0"`，并注明 `124508` 的数字
**从未被采纳**为执行账本（11-12 已有 `162625` / `162857` / `195821` 三实例并存 + 目录级
`supersession.json` 的先例；该文件是**人工留证**的审计记录，不是自动生成物）。

禁止就地编辑 `live_trading_ticket.json` / `.csv` / `post_holdings.json` —— 会破坏审计链、
与同轮 `nav_breakdown` / `account_state` 失去自洽，并否证「归档未被污染」这一属性。

## 2. 阶段 A：11-17 开盘（券商端，不用 web；仅当已过窗口、无法走 A′ 时）

CSV（11-16 生成的那张票，用它下今天的单）：

```bash
/home/stone/Work/stock/simulate/202609/runs/chan_four_state_blend/custom_astock_202609_20_new/2026-11-16/124508/live_trading_ticket.csv
```

| 动作 | 标的 | 数量 | 限价 |
| --- | --- | --- | --- |
| 卖出 | `600362` 江西铜业 | 200 股 | ≤30.09 或市价 |

只下这一笔：`−9.6%` 已触 `−8%` 硬止损，必须出清；其余四腿并入下一轮，
避免约 ¥48,000 的往返损耗（同一笔仓位在一日内先卖后买，只付摩擦不改变暴露）。

记下**成交均价 / 成交数量 / 成交后可用资金**，供阶段 B 录入。

## 3. 阶段 B：11-17 收盘后（web，出新票）

时间窗口 `15:30 → 次日 09:30`；错过会被 `MissedExecutionOpenError` 拒绝。

### B0 前置校验

先确认 dump 的版本与口径真的生效（不是看配置文件，是看 dump）：

```bash
cd /home/stone/Work/github/quant
pipeline/.venv/bin/python - <<'PY'
import json
d=json.load(open('pipeline/research_strategy/results/strategy_dumps/chan_four_state_blend_strategy.json',
                 encoding='utf-8'))
print("version:", d["strategy_version"], "| base:", d["base_version"])
print("scope  :", (d.get("params") or {}).get("cfsb_vol_ceiling_scope"))
PY
```

预期 `1.5.0 | 2.0.0` 与 `portfolio`；不是 `portfolio` 就停，不要往下走。

再跑一次归档完整性巡检（确认 11-16 之后没有新的断档）：

```bash
pipeline/.venv/bin/python scripts/check_live_run_integrity.py \
  --runs-root /home/stone/Work/stock/simulate/202609/runs/chan_four_state_blend/custom_astock_202609_20_new \
  --strict; echo "exit=$?"
```

当前实测（2026-11-16，全区间 `2026-10-29..2026-11-16`，两种调用各跑一次）：

| 调用 | 退出码 | 结论正文 |
| --- | --- | --- |
| 不加 `--strict` | `0` | `DIRTY`（18 条发现） |
| 加 `--strict` | `1` | 与上行**逐字节相同**（`cmp` 无差异），仅退出码不同 |

原文（判定以正文为准，不以退出码为准）：

```text
VERDICT: DIRTY — 18 条发现（HIGH 2 / MEDIUM 14 / LOW 2） [MISSED_SESSION=1, TIMING_REFUSED=1, DEPLOY_FAILED=4, POST_HOLDINGS_MISSING=10, DUPLICATE_INSTANCES=2]，另有 1 条被取代的 TIMING_REFUSED；缺票交易日 2026-11-13；无票可填的开盘日 2026-11-16
```

工具的文档化语义（`scripts/check_live_run_integrity.py` 文件头）：发现**总是**打印；
无 `--strict` 时即便有发现也退出 `0`；`--strict` 只在「至少一条发现」时退出 `1`，
且判据是**原始**发现 —— 被取代（`[sup]`）的条目不能把 `1` 洗成 `0`；`2` 留给用法/目录/日历错误。

已知历史条目（**不是今天新增**，不要试图清零，也不要为此改归档）：

- 11-13 `MISSED_SESSION`（`144835` / `192803`）、
  `TIMING_REFUSED`（`192803`，`lateness_minutes=119.2`）；
- 11-12 `TIMING_REFUSED`（`195821`，`lateness_minutes=149.4`）；
- 4 条 `DEPLOY_FAILED`（11-04 / 11-06 / 11-09 / 11-12）、10 条 `POST_HOLDINGS_MISSING`、
  2 条 `DUPLICATE_INSTANCES`（11-02 / 11-11）。

判定标准**不是 exit 码**，而是：**是否出现 11-17 / 11-18 新增的 `MISSED_SESSION`**。
出现 → 先补跑；未出现 → 可继续核对 Stage 1 / Stage 2 后再下单。

范围陷阱：加 `--from 2026-11-16 --to 2026-11-16` 只扫一天会得到 `CLEAN … exit=0`，
那是**区间干净**，不是**归档干净**；判断今天有没有断档时不要收窄区间。

### B1 启动

```bash
cd /home/stone/Work/github/quant/web/livetrading
uv run python app.py --port 8080
```

打开 `http://127.0.0.1:8080`。归档路径由 `data/settings.local.json` 固定为
`/home/stone/Work/stock/simulate/202609/runs`（**不是** repo 内那个空的
`web/livetrading/data/runs`）；页面若写到后者，说明读的不是 `settings.local.json`。

### B2 运行参数

| 参数 | 设为 |
| --- | --- |
| 策略 | `chan_four_state_blend` |
| 标的池 | `custom_astock_202609_20_new` |
| as_of | `2026-11-17` |
| data_provider | **`fuyao`**（默认值是 `marketdb`，必须手改） |
| 起始日期 | `2024-01-01` |

走 §1 阶段 A′（重跑 11-16）时，`as_of` 改填 `2026-11-16`，其余同上。

### B3 录入账本

切到「💼 当前实盘持仓」→「➕ 增持标的」（`#btn-add-holding`）/ 行内编辑 → 填券商实际股数：

> ⚠️ **出票后表格默认显示的是「计划账本」，不是成交回执。**
> Stage 2 成功后，页面会用票面 `target_shares` 推演的票后账簿（`holdings_plan`，
> 与 `post_holdings.json` 同源）重绘表格，
> 并提示「该表为计划持仓，非成交回执」。2026-11-16 的计划值是
> `600660 300 / 601919 900 / 601728 2300 / 600900 500` —— 后两个标的账户**从未持有过**。
> 磁盘上的真值没变：`2026-11-16/124508/holdings.json` 与 `2026-11-16/operator_holdings.json`
> 都仍是 `600362 200 / 600660 400 / 601919 1200`（`saved_at 2026-11-16T12:45`，
> `source: operator_manual_correction`），且不存在 `2026-11-17/` 目录。

**先核对、再保存；绝不保存计划账本。若表格里出现了你从未持有过的标的，说明它是计划值。**

此时若直接点「💾 保存持仓」，计划值会以 `source: operator_manual_correction` 写进
`operator_holdings.json`，变成一份「人工核实过」的假账本；此后每一轮都按它定量
（引擎以为 `601728` / `600900` 已持有），可能出零单或错量单，而真实账户只投了 ~25%。

| 标的 | 股数 |
| --- | --- |
| `600660` 福耀玻璃 | 400 |
| `601919` 中远海控 | 1200 |
| `600362` 江西铜业 | 已清仓，删除该行 |

现金栏填券商可用资金（11-16 票前 `147,165.34` + 卖出所得 − 费用）；**不得留空**。

点「💾 保存持仓」（`#btn-save-holdings`），写出 `operator_holdings.json`。

三条红线：

- 原样回传计划账本（把系统给的计划书当实际持仓填回去）触发 `UnconfirmedPlanAsInputError`；
- 空表格被读成**整仓清仓**，需要 `confirm_empty_book` 才放行；
- 只删**真正已清仓**的行 —— 顺手删一行等于凭空减仓，下一轮权重会漂。

### B4 Stage 1（点黄色「1.风控」= `#btn-run-health`）

不要点绿色「⚡ 全流程」（`#btn-run-all`）：今天必须先人工核对 Stage 1，再放行 Stage 2。

| 项目 | 预期 | 不符含义 |
| --- | --- | --- |
| 峰谷回撤 | `−3.08%` 左右 | 仍 `−5.77%` ⇒ 先查下一行；嵌套也已修却仍报高，才是「修复未被读到」 |
| HWM | `203,049.96` | 若 `208,830.78` ⇒ 同上一行 |
| **`prior_state` 峰值** | 该轮 `account_state.json` 的 `prior_state.peak_nav` = **`203,049.96`** | 若是 `208,830.78` ⇒ 引擎读到**陈旧的 `prior_state`**，停（见下方核对命令） |
| 熔断层级 | `NORMAL` | — |
| Scope check | 单名中位 `30.3%` vs 等权组合 `9.9%`（`ratio 3.05x`）+ `[WARN] SCOPE MISMATCH` | 缺失 ⇒ 诊断层未生效，记录 |

> **页面报 `−5.77%` / `¥208,830.78` 只有一个来源**：`inspect_portfolio_drawdown` 在
> `as_of_date == state["as_of_date"]` 时**换用 `prior_state`**（上一轮的拷贝），而
> **同日重跑（§1 阶段 A′）走的正是这条路径** —— 故该项检查**以 A′ 路径为最要紧**。
> 「顶层看起来已修、页面仍报高」= 嵌套副本没过修，不是 HWM 修复失效。
> 只读核对（取该日最新实例）：

```bash
cd /home/stone/Work/stock/simulate/202609/runs/chan_four_state_blend/custom_astock_202609_20_new
python - <<'PY'
import glob, json
p = sorted(glob.glob('2026-11-16/*/account_state.json'))[-1]
ps = json.load(open(p))['prior_state']
print(p, ps['peak_nav'], ps['drawdown_pct'])
PY
```

> 预期 `203049.96` 与 `-0.0317095…`。若读到 `208830.78` / `-0.058513…`，说明该实例继承了未修的
> 副本：停手、按 §1 的取代关系处理，**不要手改 `account_state.json`**（见 §7）。

`R1` 仍为 advisory（`r1_effective=false`），**不得**据其改单。

### B5 Stage 2（点蓝色「2.调仓」= `#btn-run-deploy`）

先读三块日志，再动券商：

1. **口径**：`scope = portfolio`；`vol_scaling_factor ≈ 1.00`；`capped_equity_sum ≈ 0.75`
   （仍是 `0.396` ⇒ 参数未生效，停）；`stage1_factor ≈ 0.396`（对照项，用于确认旧口径已让位）。
2. **方向**：`[INFO/EQUITY DIRECTION]` 应为**加仓**，≈ `+37pp`（净买入 ≈ ¥70,000），
   目标权重前 ≈`25%` → 后 ≈`69%`；若显示**减仓**，说明信号已变，重新理解后再下单。
3. **摩擦与映射**：`[INFO/LOT ROUNDING DRAG]` ≈ `−2~3%` NAV（整手取整吃掉的暴露）；
   `[WARN/SECTOR MAP]` 会点名 `600900.SH` / `000999.SZ`（行业取代码前缀兜底，节流空转）。

确认无误后点「📥 导出工单 CSV」（`#btn-download-csv`），把 CSV 交券商。

### B6 出票后必查：账本是不是计划值

| 检查 | 通过条件 | 不通过含义 |
| --- | --- | --- |
| `summary.json["holdings_seed_source"]` | `operator_manual_correction` / `operator_input` | 其它取值 ⇒ 账本不是人工核实过的 |
| `summary.json["holdings_input_check"]["n_mismatched"]` | `0` | 非 `0` ⇒ 输入账本与上一轮 `post_holdings` 冲突 |
| `EQUITY DIRECTION` 的「前」 | 2026-11-16 重跑应为 `25.1%` | 读到 `32.3%` ⇒ 引擎吃了计划账本：**停** |

```bash
cd /home/stone/Work/stock/simulate/202609/runs/chan_four_state_blend/custom_astock_202609_20_new/2026-11-16/124508
python -c "import json;d=json.load(open('summary.json'));print(d['holdings_seed_source'], d['holdings_input_check']['n_mismatched'])"
```

- 第三条命中 ⇒ 先恢复真账本（`600362 200 / 600660 400 / 601919 1200`），再重跑；
  不要在污染账本上继续出票。
- `EQUITY DIRECTION` 取自 Stage 2 日志 / 票面（`前 25.1% -> 后 ...`），
  `summary.json` 里该键可能为 `null`。

## 4. 阶段 C：11-18 开盘（券商端）

严格顺序：**先卖**（释放购买力，A 股 T+1 当日可用）→ **后买**（整手取整）→
**最后现金归集**（`BIL` / `511880` / 逆回购）。

限价口径：卖 `≤ 收盘价×0.997`，买 `≥ 收盘价×1.002`；跳空越过限价则放弃该笔，下一轮补。

## 5. 阶段 D：11-18 收盘后（回填 + 巡检）

1. web 表格改为**实际成交后**的股数与现金 →「💾 保存持仓」。
2. 确认写出 `post_holdings.json`：

   ```bash
   ls /home/stone/Work/stock/simulate/202609/runs/chan_four_state_blend/custom_astock_202609_20_new/2026-11-18/*/post_holdings.json
   ```

   缺失会退回 `operator_manual_correction`：重置 `holding_bars` / `entry_prices`、
   冷却期失效（该归档已断 10 轮，不要再来一次）。

3. 再跑一次完整性巡检（同 B0 第二条命令）。

## 6. 回滚

| 范围 | 做法 |
| --- | --- |
| 仅本次 | Stage 2 用 CLI 加 `--vol-ceiling-scope stage1`（web 表单不暴露该字段） |
| 长期 | 把 `pipeline/research_strategy/strategies_config.json` 里 `chan_four_state_blend` 的 `"cfsb_vol_ceiling_scope"` 改回 `"stage1"`，再重出 dump |

长期回滚重出 dump（在 `pipeline/` 下执行）：

```bash
cd /home/stone/Work/github/quant/pipeline
uv run python research_strategy/run_research_strategy.py --dump-strategies
```

回滚**不需要改代码、不需要动版本号**：口径是参数，不是契约。

## 7. 不要做

| ❌ | 原因 |
| --- | --- |
| 点「一键全流程」 | 跳过 Stage 1 人工核对，今天必须先确认 scope 与回撤 |
| 用 `--allow-intraday` 补跑 | 只放宽窗口，不放行已过的开盘（11-13 因此废掉） |
| 手改 `account_state.json` 的 `peak_nav` 或用 `--peak-nav` | 守卫会拦非同源 NAV，且改动留痕 |
| 提交空持仓表 | 被读成整仓清仓，需 `confirm_empty_book` |
| 在计划账本上点「保存持仓」 | 计划值会被写成 `operator_manual_correction` 的「真账本」，此后每轮按它定量（见 §3 B3 / B6） |
| 现金栏留空 | 会落 `identical_to_previous_round_residual`，无法与「忘了更新」区分 |
| Stage 2 后再改账本 | 票据已按旧账本定量，改账本会让下一轮权重漂移 |
