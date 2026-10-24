# P1.4 非对称减仓阈值比例参数化规范 (Asymmetric Sells Ratio Specification)

> **单一真实源 (Single Source of Truth, SSOT)**: 本模块的设计意图、工程任务状态与演进计划均收录于 [开发计划](file:///home/stone/Work/github/quant/docs/development_plan.md)。

---

## 1. 概述与背景

在资产惯性再平衡过滤层（`apply_asset_inertia` / `_step_asset_inertia`，位于 [`common/allocation_templates.py`](file:///home/stone/Work/github/quant/common/allocation_templates.py)）中，原先在开启 `asymmetric_sells=True` 时，减仓触发阈值被硬编码为固定系数 `0.60`：

$$\theta_{\text{sell}} = 0.60 \cdot \theta_t$$

任务 `P1.4` 将该系数正式参数化为 `asymmetric_sell_ratio: float = 0.60`，向外暴露灵活调优接口，同时保持默认值 $0.60$ 以保证历史回测与现有策略的 100% 严格后向兼容。

---

## 2. 数学公式与执行机制

### 2.1 资产惯性过滤公式

在单步惯性计算 `_step_asset_inertia` 中，对于风险资产 $s \in \mathcal{S}_{\text{risky}}$：

- 目标权重变化量：
  $$\Delta w_s(t) = w_s^*(t) - w_s(t-1)$$
- 基础重平衡阈值：
  $$\theta_t \quad (\text{来自标量、向量或波动率自适应 DataFrame})$$
- 动态减仓阈值：
  $$\theta_{\text{sell}} = \begin{cases} \alpha_{\text{asym}} \cdot \theta_t & \text{若 } \text{asymmetric\_sells} = \text{True} \\ \theta_t & \text{若 } \text{asymmetric\_sells} = \text{False} \end{cases}$$
  其中 $\alpha_{\text{asym}} = \text{asymmetric\_sell\_ratio}$（默认 $0.60$）。
- 减仓触发判定：
  $$\Delta w_s(t) \le -\theta_{\text{sell}}$$
- 加仓触发判定：
  $$\Delta w_s(t) \ge \theta_{\text{buy}}$$
  （其中 $\theta_{\text{buy}}$ 可由 `entry_threshold` 独立调控）。

### 2.2 量化设计意图

1. **下行防守敏捷性（Fast De-risking）**：
   在多头头寸出现走弱、信号衰退或波动率放大时，将减仓门槛收紧（乘以 $\alpha_{\text{asym}} \in (0, 1)$），使得较小的减仓信号亦能迅速触发抛售，快速释放现金流动性。
2. **上行摩擦抑制（Friction-Suppressed Adding）**：
   对于建仓与加仓，仍维持全额阈值 $\theta_t$，有效滤除噪声微调产生的频繁双边摩擦与滑点损耗。

---

## 3. 架构辨析：`asymmetric_sell_ratio` 与 `cfsb_inertia_relative_ratio`

为防止概念混淆与命名污染，下表明确区分两者的数学定义、作用层级与设计维度：

| 属性维度 | `asymmetric_sell_ratio` ($\alpha_{\text{asym}}$) | `cfsb_inertia_relative_ratio` ($r_{\text{rel}}$) |
| :--- | :--- | :--- |
| **所属模块** | 通用底层惯性层 [`common/allocation_templates.py`](file:///home/stone/Work/github/quant/common/allocation_templates.py) | 策略特化生命周期层 (`cfsb_` / UPLSM `step_position_lifecycle`) |
| **默认值** | `0.60` | `0.30` |
| **缩放对象** | **基础重平衡绝对阈值** $\theta_t$（或波动率自适应阈值） | **当前持仓权重** $w_{\text{cur}}$ |
| **核心数学公式** | $\theta_{\text{sell}} = \alpha_{\text{asym}} \cdot \theta_t$ | $\theta_{\text{eff}} = \max(\theta_{\text{abs}}, r_{\text{rel}} \cdot w_{\text{cur}})$ |
| **方向性** | **单向非对称**：仅在减仓/平仓（$\Delta w < 0$）方向按比例收窄阈值 | **双向相对缩放**：根据当前已有持仓体量等比例放大再平衡带宽 |
| **核心解决问题** | 止损/减仓动作相对于加仓动作的非对称反应敏感度 | 大权重核心底仓容易因净值轻微漂移而触发无效微量调仓的问题 |
| **正交性与解耦** | 全局资产再平衡级参数，与单标的生命周期状态无关 | 状态机/生命周期管理特化参数，仅作用于特定混合策略实例 |

---

## 4. 接口与代码路径

### 4.1 函数签名变更

```python
def _step_asset_inertia(
    t: int,
    ideal_w: pd.Series,
    current_w: pd.Series,
    risky_symbols: list,
    symbols: list,
    thresh_t: pd.Series,
    holding_age: Dict[str, int],
    last_buy_bar: Dict[str, int],
    cash_proxy: Optional[str] = "CASH",
    em_row: Optional[Union[bool, pd.Series]] = None,
    asymmetric_sells: bool = False,
    entry_threshold: Optional[float] = None,
    min_holding_bars: int = 0,
    asymmetric_sell_ratio: float = 0.60,
) -> pd.Series:
```

```python
def apply_asset_inertia(
    daily: pd.DataFrame,
    min_weight_change: Union[float, Dict[str, float], pd.Series, pd.DataFrame] = 0.02,
    cash_proxy: Optional[str] = "CASH",
    emergency_mask: Optional[Union[pd.Series, pd.DataFrame]] = None,
    asymmetric_sells: bool = False,
    entry_threshold: Optional[float] = None,
    min_holding_bars: int = 0,
    asymmetric_sell_ratio: float = 0.60,
) -> pd.DataFrame:
```

```python
def _sparse_from_daily(
    daily: pd.DataFrame,
    min_weight_change: Union[float, Dict[str, float], pd.Series] = 0.0,
    cash_proxy: Optional[str] = None,
    emergency_mask: Optional[pd.Series] = None,
    asymmetric_sells: bool = False,
    asymmetric_sell_ratio: float = 0.60,
) -> pd.DataFrame:
```

### 4.2 测试验证

测试用例 `test_apply_asset_inertia_asymmetric_sells_ratio_anchor` 位于 [`common/tests/test_allocation_templates.py`](file:///home/stone/Work/github/quant/common/tests/test_allocation_templates.py)，严格断言：
1. 默认参数与显式传入 `0.60` 输出完全一致（比特级一致）；
2. 传入 `0.80` 与 `0.40` 时减仓敏感度的动态缩放与冻结行为；
3. `asymmetric_sells=False` 时该比例完全不产生副作用。
