#!/usr/bin/env python3
"""Live Trading Ruleset & SOP Generator.

Inspects a quantitative strategy's configuration, parameters, factor tags,
and source code to generate standardized, production-grade live trading
operating manuals in English (rules.md) and Chinese (rules_cn.md).

Usage:
  python generate_rules.py --strategy <strategy_name> [--output-dir PATH] [--force]
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def find_repo_root() -> Path:
    """Finds the root repository path."""
    cur = Path(__file__).resolve().parent
    for p in [cur, cur.parent, cur.parent.parent, cur.parent.parent.parent]:
        if (p / ".git").is_dir() or (p / "pipeline").is_dir():
            return p
    return Path.cwd()


def load_strategy_metadata(strategy_key: str, repo_root: Path) -> Dict[str, Any]:
    """Loads strategy configuration from strategies_config.json or dumps."""
    clean_key = strategy_key.replace("_strategy", "").strip()

    # 1. Search strategies_config.json
    config_paths = [
        repo_root / "pipeline/research_strategy/strategies_config.json",
        repo_root / "research_strategy/strategies_config.json",
    ]
    for cp in config_paths:
        if cp.is_file():
            try:
                with open(cp, "r", encoding="utf-8") as f:
                    cfg_all = json.load(f)
                if clean_key in cfg_all:
                    data = cfg_all[clean_key]
                    data["strategy_key"] = clean_key
                    return data
            except Exception as e:
                print(f"[WARN] Error reading {cp}: {e}", file=sys.stderr)

    # 2. Search strategy_dumps
    dump_dirs = [
        repo_root / "pipeline/research_strategy/results/strategy_dumps",
        repo_root / "research_strategy/results/strategy_dumps",
    ]
    for dd in dump_dirs:
        dump_file = dd / f"{clean_key}_strategy.json"
        if dump_file.is_file():
            try:
                with open(dump_file, "r", encoding="utf-8") as f:
                    dump_data = json.load(f)
                if "research_strategy_spec" in dump_data:
                    spec = dump_data["research_strategy_spec"].get("entry_data", {})
                    spec["strategy_key"] = clean_key
                    spec["explanation"] = dump_data.get("explanation", "")
                    if "parameters" not in spec and "params" in dump_data:
                        spec["parameters"] = dump_data["params"]
                    return spec
                return {
                    "strategy_key": clean_key,
                    "name": dump_data.get("template_name", clean_key),
                    "parameters": dump_data.get("params", {}),
                    "explanation": dump_data.get("explanation", ""),
                }
            except Exception as e:
                print(f"[WARN] Error reading dump file {dump_file}: {e}", file=sys.stderr)

    # Fallback minimal dictionary
    return {
        "strategy_key": clean_key,
        "name": clean_key.replace("_", " ").title(),
        "parameters": {},
        "description": f"Quantitative strategy: {clean_key}",
    }


def find_strategy_source(class_name: Optional[str], repo_root: Path) -> Optional[str]:
    """Finds Python class source code or docstrings if available."""
    if not class_name:
        return None

    search_dirs = [
        repo_root / "pipeline/research_strategy/rs",
        repo_root / "common",
    ]
    pattern = re.compile(rf"class\s+{re.escape(class_name)}\b")

    for sdir in search_dirs:
        if not sdir.is_dir():
            continue
        for py_file in sdir.glob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8")
                if pattern.search(content):
                    return content
            except Exception:
                pass
    return None


def extract_docstring_and_rules(source_code: str, class_name: str) -> Dict[str, str]:
    """Extracts class docstring and key comment blocks."""
    info = {"docstring": "", "warmup": "252"}
    if not source_code:
        return info

    pattern = re.compile(rf"class\s+{re.escape(class_name)}\s*\(.*?\):\s*\"\"\"(.*?)\"\"\"", re.DOTALL)
    m = pattern.search(source_code)
    if m:
        info["docstring"] = m.group(1).strip()
    return info


def build_english_rules(meta: Dict[str, Any], doc_info: Dict[str, str]) -> str:
    """Generates the English live trading operating manual."""
    strat_key = meta.get("strategy_key", "strategy")
    name = meta.get("name", strat_key.replace("_", " ").title())
    class_name = meta.get("class_name", "")
    params = meta.get("parameters", {})
    desc = meta.get("description", meta.get("plain_english_description", ""))
    factors = meta.get("factors", [])
    cash_proxy = params.get("cash_proxy", "BIL")

    # Circuit breakers & risk controls
    dd_red = params.get("crb_dd_reduce_thresh", 0.10)
    dd_def = params.get("crb_dd_defensive_thresh", 0.15)
    dd_stop = params.get("crb_dd_stop_thresh", 0.20)
    max_pos = params.get("crb_max_single_position", params.get("max_single_position", 0.20))
    min_trade = params.get("crb_min_weight_change", params.get("min_weight_change", 0.04))
    stop_loss_pct = float(params.get("stop_loss_pct", params.get("crb_stop_loss_pct", params.get("chan_comp_stop_loss_pct", params.get("chan3_stop_loss_pct", 0.08)))))
    max_holding_days = int(params.get("max_holding_days", params.get("crb_max_holding_days", params.get("chan_comp_max_holding_days", params.get("chan3_max_holding_days", 90)))))

    doc = f"""# {name}: Live Trading Operating Manual & Rulebook

> **Strategy Identifier**: `{strat_key}`{f' (`{class_name}`)' if class_name else ''}  
> **Instrument Context**: Equity / ETF Universe + Cash Proxy (`{cash_proxy}`)  
> **Trading Frequency**: Daily rebalance evaluation, executing between **14:00 – 14:50** (to avoid open auction volatility and end-of-day market-on-close distortions).  
> **Underlying Factors**: {', '.join(f'`{f}`' for f in factors) if factors else 'Quantitative Alpha & Momentum'}

---

## 1. Strategy Identity & Architecture Overview

**{name}** is a quantitative trading strategy designed for disciplined portfolio execution.

### Strategy Description
{desc}

```mermaid
flowchart TD
    Universe["Universe Data (Risky Basket + Cash Proxy: {cash_proxy})"] --> AlphaEngine["Quantitative Alpha & Signal Engine"]
    AlphaEngine --> RegimeCheck{{"Macro Regime / Risk Checks"}}
    RegimeCheck --> Sizing["Position Sizing & Concentration Caps (Max {max_pos:.0%} per stock)"]
    Sizing --> FrictionFilter{{"Asset Turnover Inertia Filter (|Delta W| >= {min_trade:.0%})"}}
    FrictionFilter -- "Pass" --> OrderExec["Execution: 1. Sells First -> 2. Buys Second"]
    FrictionFilter -- "Fail" --> HoldPrior["Hold Prior Positions (No Trade)"]
```

---

## 2. Daily Live Trading Routine (Operational Checklist)

A human trader must follow this 5-step daily routine in strict sequential order before placing any trades:

```mermaid
flowchart LR
    T1["Step 1: Drawdown Tier Check"] --> T2["Step 2: Regime / Breadth Check"]
    T2 --> T3["Step 3: Execute Sells FIRST"]
    T3 --> T4["Step 4: Size & Execute Buys"]
    T4 --> T5["Step 5: Apply {min_trade:.0%} Friction Filter"]
```

---

## 3. Step-by-Step Decision Logic & Rules

### Step 1: Drawdown Circuit Breaker Check (Portfolio Level)
Calculate your current portfolio High-Water Mark (HWM) and peak-to-trough drawdown at 14:00:
$$\\text{{Drawdown}} = \\frac{{\\text{{Current NAV}} - \\text{{Peak NAV}}}}{{\\text{{Peak NAV}}}}$$

* **IF Drawdown < {dd_red:.0%} (Normal Regime)**:
  * Proceed to Step 2 with full risk budget. Standard single-stock cap = **{max_pos:.0%}**.
* **IF {dd_red:.0%} <= Drawdown < {dd_def:.0%} (Tier 1: Risk Damping)**:
  * **Action**: Cut all active stock positions by **50%**.
  * Remaining 50% must sit in Cash Proxy (`{cash_proxy}`).
* **IF {dd_def:.0%} <= Drawdown < {dd_stop:.0%} (Tier 2: Tactical Defense)**:
  * **Action**: Liquidate discretionary offensive positions.
  * Switch into defensive mode: Hold 70% in Cash Proxy, and up to 30% only in leading defensive assets.
* **IF Drawdown >= {dd_stop:.0%} (Tier 3: Hard Stop / Emergency Halt)**:
  * **Action**: **Liquidate 100% of all risk assets into Cash Proxy immediately**.
  * **Lockout**: **Do not buy for 21 consecutive trading days**. On Day 22, reset HWM to current NAV to allow fresh cycle re-entry.

---

### Step 2: Regime & Market Breadth Check
* **IF Market Regime is Favorable**:
  * Allocate up to target equity exposure according to model conviction.
  * Dynamically deploy cash into top qualifying assets without exceeding position caps.
* **ELSE (Market Regime is Bearish or Indeterminate)**:
  * Enforce standard hard cap of **{max_pos:.0%}** per stock.
  * Retain remaining capital in Cash Proxy (`{cash_proxy}`).

---

### Step 3: Asset-Level Sell Rules (Execute Sells FIRST)
Always execute sell orders before buy orders to guarantee purchasing power and avoid margin strain.

Liquidate an asset down to **0.0%** if **ANY** of the following conditions trigger:
1. **Model Sell Signal**: Formal exit or sell signal printed by the strategy engine.
2. **Structural Invalidation**: Price action invalidates the setup or breaks key support.
3. **Hard Stop-Loss**: Asset drops **>= {stop_loss_pct:.0%}** below entry price -> **Market exit immediately**.
4. **Time Stop**: Position held for **>= {max_holding_days} trading days** without upward follow-through -> Close position and release capital.

---

### Step 4: Asset-Level Buy Rules (Position Sizing)
1. **Entry Confirmation**: Only buy when entry signals are confirmed at the 14:00 evaluation.
2. **Tranche Pyramiding**: Scale into positions progressively (e.g. 30% initial base tranche, 40% confirmation tranche, 30% breakout tranche) rather than buying 100% upfront.
3. **Single-Stock Cap**: Never allocate more than **{max_pos:.0%}** of total portfolio NAV to any single ticker.

---

### Step 5: Turnover & Minimum Trade Filter (|Delta W| >= {min_trade:.0%})
Before entering an order into your broker terminal, calculate the weight change:
$$\\Delta W = |W_{{\\text{{target}}}} - W_{{\\text{{current}}}}|$$

* **Normal Trading Days**:
  * **IF $\\Delta W < {min_trade:.0%}$**: **DO NOT TRADE**. Hold prior quantity to avoid transaction fee erosion.
  * **IF $\\Delta W \\ge {min_trade:.0%}$**: Place order.
* **Circuit Breaker Days (Emergency Override)**:
  * If a circuit breaker triggered or an asset hit a hard stop-loss:
    * **Emergency Sells**: Bypass the {min_trade:.0%} rule down to **0.1%** ($|\\Delta W| \\ge 0.001$) and execute sales immediately.
    * **Emergency Buys**: Still require the full **{min_trade:.0%}** threshold ($|\\Delta W| \\ge {min_trade:.04f}$) to avoid buying into falling markets.

---

## 4. Human Trader Quick-Reference Card

| Check | Item | Condition | Human Trader Action |
| :--- | :--- | :--- | :--- |
| **Risk** | **HWM Drawdown** | >= {dd_stop:.0%} | **STOP ALL TRADING**: Liquidate 100% to `{cash_proxy}`. Freeze 21 days (HWM resets Day 22). |
| **Risk** | **HWM Drawdown** | {dd_def:.0%} - {dd_stop:.0%} | **DEFENSIVE SHIFT**: Route 100% to defensive mode (70% cash proxy buffer). |
| **Risk** | **HWM Drawdown** | {dd_red:.0%} - {dd_def:.0%} | **HALVE RISK**: Cut all open equity weights by 50%; sweep remainder to cash. |
| **Regime** | **Regime Gate** | Bullish | **NORMAL / EXPANDED**: Target full model exposure up to caps. |
| **Regime** | **Regime Gate** | Bearish / Neutral | **CONSERVATIVE**: Keep single-stock cap at {max_pos:.0%}; hold cash buffer. |
| **Exit** | **Stop-Loss** | Loss >= {stop_loss_pct:.0%} from entry | **EXIT IMMEDIATELY**: Sell 100% of position. |
| **Exit** | **Time Stop** | Held >= {max_holding_days} days no progress | **EXIT**: Close position to release capital. |
| **Exit** | **Signal Exit** | Model Sell / S-point | **EXIT**: Sell position to 0.0%. |
| **Entry** | **Signal Buy** | Valid Model Buy Signal | **SCALE IN**: Buy tranche up to single-stock cap ({max_pos:.0%}). |
| **Execution**| **Friction Filter**| |Delta W| < {min_trade:.0%} | **SKIP**: Do not place order if change is under {min_trade:.0%} portfolio NAV. |
| **Execution**| **Sequence** | Multi-asset rebalance | **SELLS FIRST** (free up cash) -> **BUYS SECOND**. |

---

## 5. Execution Nuances & Slippage Management

1. **Execution Window**: 
   * Evaluate data at **14:00**.
   * Transmit orders between **14:20 – 14:45**. 
   * Avoid trading in the first 30 minutes of the market open (09:30–10:00).
2. **Order Style**:
   * For standard liquid equities/ETFs: Use **limit orders pegged to current bid/ask** or TWAP slices over 15 minutes.
   * For emergency stop-losses or circuit breakers: Use **market or aggressive limit orders** to guarantee fills.
3. **Cash Proxy Management**:
   * Unallocated capital must be held in interest-bearing cash equivalents (`{cash_proxy}`).
"""
    return doc


def build_chinese_rules(meta: Dict[str, Any], doc_info: Dict[str, str]) -> str:
    """Generates the Chinese live trading operating manual."""
    strat_key = meta.get("strategy_key", "strategy")
    name = meta.get("name", strat_key.replace("_", " ").title())
    class_name = meta.get("class_name", "")
    params = meta.get("parameters", {})
    desc = meta.get("description", meta.get("plain_english_description", ""))
    factors = meta.get("factors", [])
    cash_proxy = params.get("cash_proxy", "BIL")

    dd_red = params.get("crb_dd_reduce_thresh", 0.10)
    dd_def = params.get("crb_dd_defensive_thresh", 0.15)
    dd_stop = params.get("crb_dd_stop_thresh", 0.20)
    max_pos = params.get("crb_max_single_position", params.get("max_single_position", 0.20))
    min_trade = params.get("crb_min_weight_change", params.get("min_weight_change", 0.04))
    stop_loss_pct = float(params.get("stop_loss_pct", params.get("crb_stop_loss_pct", params.get("chan_comp_stop_loss_pct", params.get("chan3_stop_loss_pct", 0.08)))))
    max_holding_days = int(params.get("max_holding_days", params.get("crb_max_holding_days", params.get("chan_comp_max_holding_days", params.get("chan3_max_holding_days", 90)))))

    doc = f"""# {name}: 实盘交易操作规程与规则手册

> **策略标识**: `{strat_key}`{f' (`{class_name}`)' if class_name else ''}  
> **交易标的**: 股票 / ETF 股票池 + 现金管理替代资产 (`{cash_proxy}` / `511880` / `511990` / 国债逆回购)  
> **调仓频率**: 日频评估与决策，实盘下单主要集中在 **14:00 – 14:50** (避开早盘集合竞价剧烈波动及尾盘收盘集合竞价冲击)。  
> **底层因子**: {', '.join(f'`{f}`' for f in factors) if factors else '量化阿尔法与动量因子'}

---

## 1. 策略架构与模型体系

**{name}** 是一套严谨的量化实盘投资策略，旨在通过结构化信号与风险控制实现可持续收益。

### 策略简介
{desc}

```mermaid
flowchart TD
    Universe["标的资产池 (股票池 + 现金替代资产: {cash_proxy})"] --> AlphaEngine["量化阿尔法与信号引擎"]
    AlphaEngine --> DDCheck{{"账户回撤熔断检查 (自 HWM)"}}
    DDCheck -- "正常" --> BreadthCheck{{"宏观环境与市场宽度检查"}}
    DDCheck -- "熔断触发" --> RiskCut["强制降风险/切换防御/清仓止损"]
    BreadthCheck --> Sizing["仓位分配与个股上限控制 (单票上限 {max_pos:.0%})"]
    Sizing & RiskCut --> FrictionFilter{{"资产级调仓摩擦过滤 (|权重变动| >= {min_trade:.0%})"}}
    FrictionFilter -- "满足条件" --> OrderExec["执行顺序: 1. 先卖出释放资金 -> 2. 后买入"]
    FrictionFilter -- "未达阈值" --> HoldPrior["保持现有持仓不动 (避免摩擦磨损)"]
```

---

## 2. 实盘交易员日内标准作业程序 (SOP)

实盘交易员在每个交易日下午 **14:00 – 14:50** 严格按照以下五个先后次序执行检查与下单：

```mermaid
flowchart LR
    T1["第 1 步: 账户回撤等级检查"] --> T2["第 2 步: 市场环境与宽度评估"]
    T2 --> T3["第 3 步: 筛选并【优先执行卖出】"]
    T3 --> T4["第 4 步: 计算可用资金并执行买入"]
    T4 --> T5["第 5 步: {min_trade:.0%} 调仓摩擦过滤与发单"]
```

---

## 3. 详细交易逻辑与分步规则

### 第 1 步: 账户级回撤熔断检查 (强制首要执行)
每日 14:00 计算当前资产净值相对于历史最高净值 (High-Water Mark) 的回撤比例：
$$\\text{{回撤幅度 (Drawdown)}} = \\frac{{\\text{{当前净值}} - \\text{{历史峰值净值}}}}{{\\text{{历史峰值净值}}}}$$

* **IF 回撤 < {dd_red:.0%} (正常运作区间)**:
  * 进入第 2 步，享有完整风险预算。单个股票仓位上限为 **{max_pos:.0%}**。
* **IF {dd_red:.0%} <= 回撤 < {dd_def:.0%} (一级熔断: 风险减半抑制)**:
  * **操作**: 现存所有股票持仓**强制压缩减半 (乘 0.5)**。
  * 释放出的资金转入现金管理资产 (`{cash_proxy}`)。
* **IF {dd_def:.0%} <= 回撤 < {dd_stop:.0%} (二级熔断: 战术防御切换)**:
  * **操作**: 清退所有常规进攻仓位。
  * 仓位**100% 切换至防御模式**：保留至少 70% 现金管理资产，最多仅允许 30% 配置于最强防御标的。
* **IF 回撤 >= {dd_stop:.0%} (三级熔断: 极端硬止损空仓)**:
  * **操作**: **100% 全面清仓所有风险资产，全部移至现金替代品**。
  * **冷却锁定期**: **连续 21 个交易日绝对禁止开仓**。第 22 个交易日将峰值基准重置为当前净值，方可重新开始评估买入。

---

### 第 2 步: 市场环境与宽度评估
* **IF 市场环境处于多头/适宜状态**:
  * 按照模型信号正常开仓，充分利用多头风险预算。
* **ELSE (市场弱势或震荡不明确)**:
  * 严格遵守单票 **{max_pos:.0%}** 硬顶限制。
  * 剩余未用资金一律留在现金管理资产中。

---

### 第 3 步: 资产级卖出与止损规则 (必须【先卖后买】)
**必须先执行卖出订单，待可用资金到账后，再行分配买入，严禁盲目加仓拉高杠杆。**

若持仓标的触发以下**任意一项**条件，该股票目标仓位直接降为 **0.0% (清仓)**：
1. **模型结构卖点**: 策略信号引擎给出正式卖出/平仓信号。
2. **结构失效/破位**: 价格跌破关键防守位或形态破坏。
3. **个股硬止损**: 任意单只标的跌幅达到或超过成本价的 **-{stop_loss_pct:.0%}** -> **市价立即止损出局**。
4. **时间止损**: 买入后持有达 **{max_holding_days} 个交易日** 仍未走出有效上涨 -> 主动平仓释放沉淀资金。

---

### 第 4 步: 资产级分批买入与加仓规则 (金字塔建仓)
1. **信号确认**: 仅在 14:00 评估时确认有效的买入信号方可入场。
2. **分批建仓**: 推荐采用分批金字塔试仓机制（如 30% 初试底仓、40% 回踩确认、30% 突破加速），切忌一次性满仓追高。
3. **单票限额**: 任意单只标的总持仓市值不得超过账户总资金的 **{max_pos:.0%}**。

---

### 第 5 步: 交易调仓摩擦过滤规则 (|Delta W| >= {min_trade:.0%})
在交易终端输入发单前，必须计算单票调仓权重变动绝对值：
$$\\Delta W = |\\text{{目标权重}} - \\text{{当前实际持仓权重}}|$$

* **普通交易日 (常规调仓)**:
  * **IF $\\Delta W < {min_trade:.0%}$**: **放弃交易，维持原有持仓不变** (杜绝微幅调仓产生的摩擦磨损)。
  * **IF $\\Delta W \\ge {min_trade:.0%}$**: 正式发单。
* **熔断与风控日 (紧急通道)**:
  * 一旦触发熔断等级或个股硬止损：
    * **紧急卖出**: 门槛放宽至 **0.1%** ($|\\Delta W| \\ge 0.001$) 即刻发单执行减仓/清仓。
    * **紧急买入**: 仍须严格满足 **{min_trade:.0%}** 门槛 ($|\\Delta W| \\ge {min_trade:.04f}$)，防止弱市中逆势加仓。

---

## 4. 实盘交易员便携速查表

| 维度 | 核对项目 | 触发条件 | 实盘交易员必须采取的操作 |
| :--- | :--- | :--- | :--- |
| **风控** | **账户总回撤** | >= {dd_stop:.0%} | **【三级熔断】** 全面清仓至 `{cash_proxy}`；冷冻 21 天，第 22 天重置净值基准。 |
| **风控** | **账户总回撤** | {dd_def:.0%} - {dd_stop:.0%} | **【二级熔断】** 撤出进攻仓位；70% 锁入现金品，仅留至多 30% 最强防御。 |
| **风控** | **账户总回撤** | {dd_red:.0%} - {dd_def:.0%} | **【一级熔断】** 现存所有股票仓位砍半；腾出资金划转至现金品。 |
| **环境** | **市场环境** | 强势多头 | **【正常配置】** 按模型满配，充分发挥选股优势。 |
| **环境** | **市场环境** | 弱势/中性 | **【防守为主】** 单票上限锁死 {max_pos:.0%}；维持标准现金缓冲。 |
| **卖出** | **硬止损** | 单票自成本跌 >= {stop_loss_pct:.0%} | **【立即离场】** 无论任何形态，市价或积极限价平仓 100%。 |
| **卖出** | **时间止损** | 持仓达 {max_holding_days} 交易日停滞 | **【超时平仓】** 获利或微损平仓，释放资金效率。 |
| **卖出** | **模型卖点** | 触发策略平仓信号 | **【清仓出局】** 该股目标权重直降为 0.0%。 |
| **买入** | **模型买点** | 确认有效买入信号 | **【分步建仓】** 分批买入，单票严禁超过 {max_pos:.0%}。 |
| **执行** | **摩擦过滤** | 拟调仓变动 < {min_trade:.0%} | **跳过不发单**，保持原仓位不变。 |
| **执行** | **发单次序** | 涉及多资产同时调仓 | **【先卖出后买入】**：卖单确认成交/资金释放后再挂买单。 |

---

## 5. 实盘执行细节与冲击成本控制

1. **发单时间窗口**:
   * **14:00 – 14:15**: 汇总全天数据，计算当日回撤与市场环境。
   * **14:20 – 14:45**: 发出卖出与买入委托。
   * 严禁在早盘开盘前 30 分钟 (09:30–10:00) 追涨杀跌。
2. **订单执行类型**:
   * 高流动性标的 (大盘宽基 ETF、高成交股票): 挂买一/卖一限价单，或分批 15 分钟 TWAP 下单。
   * 紧急止损单或三级熔断平仓: 采用市价或最优五档即时成交剩余撤销单，确保绝对离场。
3. **现金替代品归集**:
   * 未买入股票的闲置资金切勿闲置在 0 利率现金账户中，应配置于 `{cash_proxy}` / 货币 ETF / 逆回购获取稳健无风险收益。
"""
    return doc


def main():
    parser = argparse.ArgumentParser(description="Live Trading Ruleset & SOP Generator.")
    parser.add_argument("--strategy", required=True, help="Strategy key name (e.g. chan_risk_managed_blend)")
    parser.add_argument("--output-dir", help="Explicit output directory (defaults to docs/ruleset/<strategy_name>)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files without prompting")
    args = parser.parse_args()

    repo_root = find_repo_root()
    strategy_key = args.strategy.strip()

    print(f"[INFO] Inspecting strategy: {strategy_key}")
    meta = load_strategy_metadata(strategy_key, repo_root)

    class_name = meta.get("class_name", "")
    source = find_strategy_source(class_name, repo_root) if class_name else None
    doc_info = extract_docstring_and_rules(source, class_name) if source else {}

    # Target directory
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        out_dir = repo_root / "docs" / "ruleset" / strategy_key

    out_dir.mkdir(parents=True, exist_ok=True)

    en_path = out_dir / "rules.md"
    cn_path = out_dir / "rules_cn.md"

    # If rules already exist and are richer/customized, keep them unless forced
    should_write = True
    if en_path.exists() and not args.force:
        print(f"[INFO] {en_path} already exists. Use --force to overwrite.")
        should_write = False

    if should_write:
        en_content = build_english_rules(meta, doc_info)
        en_path.write_text(en_content, encoding="utf-8")
        print(f"[SUCCESS] Exported English rules to {en_path}")

        cn_content = build_chinese_rules(meta, doc_info)
        cn_path.write_text(cn_content, encoding="utf-8")
        print(f"[SUCCESS] Exported Chinese rules to {cn_path}")
    else:
        print(f"[INFO] Target files preserved: {en_path}, {cn_path}")


if __name__ == "__main__":
    main()
