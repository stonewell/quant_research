"""Advisory signal-layer YELLOW / ORANGE / RED alert matrix (task `AT5`).

WHAT THIS IS
============
`docs/development_plan.md:411` (`AT5`) asks for a signal-layer alert matrix that
**consumes** the `AT3` IC/ICIR decay report (`common/ic_decay_monitor.py`) and the
`AT4` live-capacity labels (`common/live_capacity.py`) and renders them as one
severity-ordered, machine-readable report. Nothing else.

The frozen mapping (`docs/signal_alerts.md`) was written down **before** this code,
per `AT5` clause 1 ("先冻结映射，冻结前不得写码"). The same rows live in
`FROZEN_MAPPING` below and a test asserts they still appear verbatim in the doc, so
the table and the implementation cannot silently drift apart:

    输入（来源）                 取值                                 层级            动作（报告文本）
    AT3 status                  INSUFFICIENT_HISTORY                 INSUFFICIENT    样本不足，不出结论
    AT3 ic_ir                   <= -0.50                             RED             腿 IC 反向显著，建议人工复核该腿
    AT3 ic_ir                   -0.50 < x <= -0.25                   ORANGE          腿 IC 转负，建议关注
    AT3 ic_ir                   -0.25 < x < 0                        YELLOW          腿 IC 轻微负值
    AT3 ic_ir                   >= 0                                 GREEN           正常
    AT4 turnover_label          BREACH                               ORANGE          换手超 `L2.36` 硬上限（标签引自 AT4，不在此重算）
    AT4 turnover_label          WATCH                                YELLOW          换手达合格线
    AT4 turnover_label          MISSING                              INSUFFICIENT    换手不可测
    AT4 participation           available=False                      INSUFFICIENT    参与率不可测
    AT4 participation           exceeds_daily_volume 或 exceeds_average_volume  YELLOW  参与率超当日/均量

AGGREGATION (占先规则, frozen with the table)
============================================
`severity_rank` orders `GREEN < YELLOW < ORANGE < RED`. `INSUFFICIENT` is **not**
on that axis -- it is a completeness state, not a severity, and `severity_rank`
raises for it rather than inventing a position for it.

`combine_levels` implements the frozen rule exactly:

* no input at all -> `INSUFFICIENT` (an empty matrix never renders as `GREEN`);
* any `INSUFFICIENT` present -> `INSUFFICIENT` (completeness outranks severity);
* otherwise -> the most severe ordinal level present.

Consequence, stated because it is deliberate: a `RED` leg next to an unmeasurable
participation rate yields an overall `INSUFFICIENT`, **not** `RED`. A partially
measured matrix cannot claim a bounded severity, and "无法判断" is never allowed to
read as "正常". The `RED` is not lost: `AlertMatrix.worst_measured_level` keeps the
most severe *measured* level, and `missing_dimensions` names exactly what was
unmeasurable, so the report says both "there is a red leg" and "this matrix is
incomplete".

WHY THESE THRESHOLDS ARE AT5's OWN (AT5 clause 2: 阈值不得互相复用)
==================================================================
`IC_IR_RED = -0.50` / `IC_IR_ORANGE = -0.25` / `IC_IR_YELLOW_FLOOR = 0.0` are
**new, AT5-owned band edges on the IC-IR axis**. They are NOT reuse of, and NOT
comparable to, any existing threshold family:

=================================================  ==========================  ==============================
existing threshold (owner)                          its own axis               AT5's relation to it
=================================================  ==========================  ==============================
portfolio drawdown breakers 10% / 15% / 20%         multi-day NAV drawdown     different dimension; AT5 reads no NAV
(`blend_base.py`, `dd_*_thresh`)                                               and defines no drawdown band
`LV1`                                               freshness / staleness       different dimension; untouched
`R1` intraday drawdown -2% / -3% / -4%              single-session dd           different dimension; AT5 does not import R1
(`common/intraday_drawdown_derisk.py`)                                         and does not reuse its ladder
`R4` cross-sectional dispersion 0.75 / 0.50 * B     dispersion vs its baseline AT5 does not import R4, reuse its
(`common/cs_dispersion_exposure.py`)                                           multiplier bands, or read dispersion at all
`R6` overnight exposure                             overnight gap exposure      different dimension; untouched
`L2.36` turnover 8.0x / 10.0x one-way annualised    turnover                   AT5 consumes AT4's **label only**
                                                                               (`BREACH`/`WATCH`/`MISSING`) and
                                                                               never re-derives a turnover number
or a participation percentage cap                   participation              AT5 consumes AT4's factual
                                                                               `exceeds_*` flags; it invents no
                                                                               participation ratio cut-off
=================================================  ==========================  ==============================

An AST test pins that this module's float literals contain none of `8.0`, `10.0`,
`0.75`, `0.50`, `0.10`, `0.15`, `0.20`, `-0.02`: the non-reuse claim is checkable,
not just prose.

NO RE-DERIVATION OF AT3 / AT4 NUMBERS
=====================================
This module imports **nothing** from `common/` and calls no math on AT3/AT4 values:
`alert_for_leg` reads `status` and `ic_ir` off the report; `alert_for_capacity`
reads the flat dict produced by `live_capacity.capacity_labels(...)` (or any object
with the same field names). A raw `TurnoverObservation` handed in without a label
degrades to `INSUFFICIENT` ("换手不可测") instead of being banded here -- the band is
`L2.36`'s, and `AT4` owns it.

TRI-STATE, ALWAYS
=================
`NaN`, `inf`, a missing key, an unknown label, `None` -- all become
`INSUFFICIENT` with a machine-readable `code`, never `GREEN`. Specifically a
non-finite `ic_ir` is `INSUFFICIENT` and not `>= 0`: `NaN` compares `False` against
every bound, which is exactly how "unmeasured" silently becomes "normal" in a
comparison chain.

Input tri-state: `build_alert_matrix(capacity=None)` means "the capacity dimension
was **not supplied**" and contributes no item; a *supplied* capacity mapping with
missing keys means "supplied but unmeasurable" and contributes `INSUFFICIENT`.

ADVISORY FENCE (AT5 clause 4: 首版只出报告，不接降权/清盘执行)
=============================================================
`AlertMatrix` carries `advisory=True` / `gates_orders=False`, and constructing one
with `gates_orders=True` (or `advisory=False`) raises `AdvisoryContractError`.
Every `action` is **report text** for a human; none of it is an instruction, a
weight multiplier, or a liquidation trigger. The module imports no `subprocess`, no
`requests`, no `scripts/`, no `web/`, performs no IO and is not wired into
`scripts/run_live_strategy.py` in this round (see `docs/signal_alerts.md`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "ALERT_LEVELS",
    "ALERT_LEVELS_ALL",
    "ALERT_LEVEL_GREEN",
    "ALERT_LEVEL_YELLOW",
    "ALERT_LEVEL_ORANGE",
    "ALERT_LEVEL_RED",
    "ALERT_LEVEL_INSUFFICIENT",
    "IC_IR_RED",
    "IC_IR_ORANGE",
    "IC_IR_YELLOW_FLOOR",
    "AT3_STATUS_OK",
    "AT3_STATUS_INSUFFICIENT_HISTORY",
    "FROZEN_MAPPING",
    "severity_rank",
    "combine_levels",
    "AlertItem",
    "AlertMatrix",
    "AdvisoryContractError",
    "alert_for_leg",
    "alert_for_capacity",
    "build_alert_matrix",
]

# ---------------------------------------------------------------------------
# Frozen level vocabulary
# ---------------------------------------------------------------------------
ALERT_LEVEL_GREEN = "GREEN"
ALERT_LEVEL_YELLOW = "YELLOW"
ALERT_LEVEL_ORANGE = "ORANGE"
ALERT_LEVEL_RED = "RED"
#: Completeness state, NOT a severity. Never produced by downgrading from "unknown"
#: and never upgraded into GREEN -- see `combine_levels`.
ALERT_LEVEL_INSUFFICIENT = "INSUFFICIENT"

#: Ordinal severity axis, ascending. `INSUFFICIENT` is deliberately absent.
ALERT_LEVELS: Tuple[str, ...] = (
    ALERT_LEVEL_GREEN,
    ALERT_LEVEL_YELLOW,
    ALERT_LEVEL_ORANGE,
    ALERT_LEVEL_RED,
)
ALERT_LEVELS_ALL: Tuple[str, ...] = ALERT_LEVELS + (ALERT_LEVEL_INSUFFICIENT,)

_SEVERITY: Dict[str, int] = {level: rank for rank, level in enumerate(ALERT_LEVELS)}

# ---------------------------------------------------------------------------
# Frozen AT5-owned band edges (IC-IR axis only). See "WHY THESE THRESHOLDS".
# ---------------------------------------------------------------------------
IC_IR_RED = -0.50        # ic_ir <= -0.50            -> RED
IC_IR_ORANGE = -0.25     # -0.50 < ic_ir <= -0.25    -> ORANGE
IC_IR_YELLOW_FLOOR = 0.0  # -0.25 < ic_ir < 0        -> YELLOW, ic_ir >= 0 -> GREEN

#: Mirrors of `common/ic_decay_monitor.py`'s public status strings. Not imported
#: (this module imports nothing from `common/`); a test pins the literal equality,
#: so a rename in AT3 fails loudly here instead of silently mislabelling statuses.
AT3_STATUS_OK = "OK"
AT3_STATUS_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"

# AT4 label vocabulary consumed as-is (never re-derived here).
AT4_TURNOVER_BREACH = "BREACH"
AT4_TURNOVER_WATCH = "WATCH"
AT4_TURNOVER_MISSING = "MISSING"
AT4_TURNOVER_OK = "OK"
AT4_PARTICIPATION_OBSERVED = "OBSERVED"
AT4_PARTICIPATION_MISSING = "MISSING"

# ---------------------------------------------------------------------------
# Frozen actions -- report text for a human, never an instruction.
# ---------------------------------------------------------------------------
ACTION_NORMAL = "正常"
ACTION_LEG_RED = "腿 IC 反向显著，建议人工复核该腿"
ACTION_LEG_ORANGE = "腿 IC 转负，建议关注"
ACTION_LEG_YELLOW = "腿 IC 轻微负值"
ACTION_LEG_INSUFFICIENT = "样本不足，不出结论"
ACTION_TURNOVER_BREACH = "换手超 `L2.36` 硬上限（标签引自 AT4，不在此重算）"
ACTION_TURNOVER_WATCH = "换手达合格线"
ACTION_TURNOVER_MISSING = "换手不可测"
ACTION_PARTICIPATION_MISSING = "参与率不可测"
ACTION_PARTICIPATION_EXCEEDS = "参与率超当日/均量"
ACTION_NO_INPUT = "无任何信号层输入，无法判断"

DIMENSION_LEG_IC = "leg_ic"
DIMENSION_TURNOVER = "turnover"
DIMENSION_PARTICIPATION = "participation"
DIMENSION_CAPACITY = "capacity"

SOURCE_AT3 = "AT3"
SOURCE_AT4 = "AT4"
SOURCE_AT5 = "AT5"

#: The frozen `指标 -> 层级 -> 动作` mapping, one dict per row, in the doc's order.
#: `docs/signal_alerts.md` must contain every `action` and `condition` string; the
#: doc test asserts it, so editing one without the other fails.
FROZEN_MAPPING: Tuple[Dict[str, str], ...] = (
    {
        "source": SOURCE_AT3,
        "input": "status",
        "condition": "INSUFFICIENT_HISTORY",
        "level": ALERT_LEVEL_INSUFFICIENT,
        "action": ACTION_LEG_INSUFFICIENT,
        "code": "leg_status_insufficient_history",
    },
    {
        "source": SOURCE_AT3,
        "input": "ic_ir",
        "condition": "<= -0.50",
        "level": ALERT_LEVEL_RED,
        "action": ACTION_LEG_RED,
        "code": "leg_ic_ir_red",
    },
    {
        "source": SOURCE_AT3,
        "input": "ic_ir",
        "condition": "-0.50 < x <= -0.25",
        "level": ALERT_LEVEL_ORANGE,
        "action": ACTION_LEG_ORANGE,
        "code": "leg_ic_ir_orange",
    },
    {
        "source": SOURCE_AT3,
        "input": "ic_ir",
        "condition": "-0.25 < x < 0",
        "level": ALERT_LEVEL_YELLOW,
        "action": ACTION_LEG_YELLOW,
        "code": "leg_ic_ir_yellow",
    },
    {
        "source": SOURCE_AT3,
        "input": "ic_ir",
        "condition": ">= 0",
        "level": ALERT_LEVEL_GREEN,
        "action": ACTION_NORMAL,
        "code": "leg_ic_ir_green",
    },
    {
        "source": SOURCE_AT4,
        "input": "turnover_label",
        "condition": "BREACH",
        "level": ALERT_LEVEL_ORANGE,
        "action": ACTION_TURNOVER_BREACH,
        "code": "turnover_breach",
    },
    {
        "source": SOURCE_AT4,
        "input": "turnover_label",
        "condition": "WATCH",
        "level": ALERT_LEVEL_YELLOW,
        "action": ACTION_TURNOVER_WATCH,
        "code": "turnover_watch",
    },
    {
        "source": SOURCE_AT4,
        "input": "turnover_label",
        "condition": "MISSING",
        "level": ALERT_LEVEL_INSUFFICIENT,
        "action": ACTION_TURNOVER_MISSING,
        "code": "turnover_missing",
    },
    {
        "source": SOURCE_AT4,
        "input": "participation",
        "condition": "available=False",
        "level": ALERT_LEVEL_INSUFFICIENT,
        "action": ACTION_PARTICIPATION_MISSING,
        "code": "participation_missing",
    },
    {
        "source": SOURCE_AT4,
        "input": "participation",
        "condition": "exceeds_daily_volume 或 exceeds_average_volume",
        "level": ALERT_LEVEL_YELLOW,
        "action": ACTION_PARTICIPATION_EXCEEDS,
        "code": "participation_exceeds",
    },
)


class AdvisoryContractError(ValueError):
    """The advisory fence was violated (e.g. `gates_orders=True`).

    Structural misuse raises instead of being silently coerced, because a matrix
    that claims it may gate orders is exactly the failure `AT5` clause 4 forbids.
    """


class AlertInputError(ValueError):
    """An unknown alert level / label was supplied (structural, not data)."""


# ---------------------------------------------------------------------------
# Level machinery
# ---------------------------------------------------------------------------
def _normalise_level(level: Any) -> str:
    """Upper-case/validate a level name; unknown -> `AlertInputError`."""
    if not isinstance(level, str):
        raise AlertInputError(f"alert level must be a string, got {type(level).__name__}")
    normalised = level.strip().upper()
    if normalised not in ALERT_LEVELS_ALL:
        raise AlertInputError(
            f"unknown alert level {level!r}; expected one of {ALERT_LEVELS_ALL}"
        )
    return normalised


def severity_rank(level: str) -> int:
    """Ordinal rank on `GREEN < YELLOW < ORANGE < RED` (0..3).

    `INSUFFICIENT` raises: it is a completeness state, not a severity, and giving
    it a rank would let "无法判断" be compared against "正常" as if they were
    neighbours. Callers that need a total order over mixed inputs must use
    `combine_levels`.
    """
    normalised = _normalise_level(level)
    if normalised == ALERT_LEVEL_INSUFFICIENT:
        raise AlertInputError(
            "INSUFFICIENT is not on the ordinal severity axis (GREEN<YELLOW<ORANGE<RED); "
            "use combine_levels() for mixed input"
        )
    return _SEVERITY[normalised]


def combine_levels(levels: Iterable[str]) -> str:
    """Frozen aggregation: empty -> INSUFFICIENT, any INSUFFICIENT -> INSUFFICIENT.

    Otherwise the most severe ordinal level. See the module docstring
    "AGGREGATION" for why completeness outranks severity.
    """
    normalised = [_normalise_level(level) for level in levels]
    if not normalised:
        return ALERT_LEVEL_INSUFFICIENT
    if ALERT_LEVEL_INSUFFICIENT in normalised:
        return ALERT_LEVEL_INSUFFICIENT
    return max(normalised, key=severity_rank)


def _json_number(value: Any) -> Optional[float]:
    """`float` iff finite, else `None` -- a JSON report never carries NaN/Infinity."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ---------------------------------------------------------------------------
# Report objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AlertItem:
    """One advisory alert for one dimension of one subject.

    `action` is **report text** (frozen, Chinese, from `FROZEN_MAPPING`); it is not
    an order, a weight, or a liquidation instruction. `code` is the stable
    machine-readable counterpart. `missing` names the sub-dimensions that were
    unmeasurable when `level` is `INSUFFICIENT` (empty otherwise).
    """

    dimension: str
    subject: str
    level: str
    action: str
    code: str
    source: str
    observed: Optional[float] = None
    missing: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "level", _normalise_level(self.level))

    @property
    def advisory(self) -> bool:
        return True

    @property
    def gates_orders(self) -> bool:
        return False

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe view (`NaN`/`inf` become `None`, never a number)."""
        return {
            "dimension": self.dimension,
            "subject": self.subject,
            "level": self.level,
            "action": self.action,
            "code": self.code,
            "source": self.source,
            "observed": _json_number(self.observed),
            "missing": list(self.missing),
            "notes": list(self.notes),
            "advisory": True,
            "gates_orders": False,
        }


@dataclass(frozen=True)
class AlertMatrix:
    """The whole advisory matrix: items + aggregate level + what was unmeasurable.

    `level` is `combine_levels(item.level)`; `worst_measured_level` is the most
    severe *ordinal* level among the items (`GREEN` when nothing ordinal is
    present, e.g. every item is `INSUFFICIENT`); `missing_dimensions` lists the
    unmeasurable dimensions verbatim. `advisory=True` / `gates_orders=False` are
    enforced by `__post_init__`, not merely documented.
    """

    items: Tuple[AlertItem, ...]
    level: str
    worst_measured_level: str
    missing_dimensions: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    as_of: Optional[str] = None
    advisory: bool = True
    gates_orders: bool = False

    def __post_init__(self) -> None:
        if not self.advisory:
            raise AdvisoryContractError(
                "advisory must stay True: this matrix is a report, never a control surface"
            )
        if self.gates_orders:
            raise AdvisoryContractError(
                "gates_orders=True is forbidden (AT5 clause 4): the matrix produces report "
                "text only and may not gate, resize, delay or liquidate anything"
            )
        object.__setattr__(self, "level", _normalise_level(self.level))
        object.__setattr__(self, "worst_measured_level", _normalise_level(self.worst_measured_level))

    @property
    def missing(self) -> Tuple[str, ...]:
        return self.missing_dimensions

    def to_dict(self) -> Dict[str, Any]:
        """`json.dumps(..., allow_nan=False)`-safe view."""
        return {
            "advisory": True,
            "gates_orders": False,
            "level": self.level,
            "worst_measured_level": self.worst_measured_level,
            "missing_dimensions": list(self.missing_dimensions),
            "as_of": self.as_of,
            "items": [item.to_dict() for item in self.items],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# AT3 -> alert
# ---------------------------------------------------------------------------
def _leg_subject(report: Any) -> str:
    parts = [getattr(report, "market", None), getattr(report, "universe_id", None),
             getattr(report, "strategy", None)]
    if any(isinstance(part, str) and part for part in parts):
        return ":".join("?" if part is None else str(part) for part in parts)
    return type(report).__name__


def alert_for_leg(leg_ic_report: Any) -> AlertItem:
    """One alert for one `LegICReport` (`AT3` output). ADVISORY.

    Reads exactly two public fields -- `status` and `ic_ir` -- and applies the
    frozen `AT3` rows. `INSUFFICIENT_HISTORY` (or any non-`OK` status, or a
    non-finite `ic_ir`) is `INSUFFICIENT`: AT3's "not measurable yet" is passed
    through verbatim and is never downgraded to `GREEN`.
    """
    subject = _leg_subject(leg_ic_report)
    status = getattr(leg_ic_report, "status", None)
    ic_ir = getattr(leg_ic_report, "ic_ir", None)
    value = _json_number(ic_ir)
    base = dict(dimension=DIMENSION_LEG_IC, subject=subject, source=SOURCE_AT3, observed=value)

    if status is None:
        return AlertItem(
            level=ALERT_LEVEL_INSUFFICIENT,
            action=ACTION_LEG_INSUFFICIENT,
            code="leg_status_missing",
            missing=(DIMENSION_LEG_IC,),
            notes=("AT3 report carries no status; an absent status is not a healthy status",),
            **base,
        )
    if status != AT3_STATUS_OK:
        # Covers INSUFFICIENT_HISTORY and any future non-OK status: fail closed.
        return AlertItem(
            level=ALERT_LEVEL_INSUFFICIENT,
            action=ACTION_LEG_INSUFFICIENT,
            code="leg_status_insufficient_history",
            missing=(DIMENSION_LEG_IC,),
            notes=(f"AT3 status={status!r}; 数据不足必须透传，不得降级为正常",),
            **base,
        )
    if value is None:
        # status OK but no finite ic_ir: NaN must not fall through `>= 0` into GREEN.
        return AlertItem(
            level=ALERT_LEVEL_INSUFFICIENT,
            action=ACTION_LEG_INSUFFICIENT,
            code="leg_ic_ir_unmeasured",
            missing=(DIMENSION_LEG_IC,),
            notes=("status is OK but ic_ir is missing/non-finite; 不可测不得当作正常",),
            **base,
        )

    if value <= IC_IR_RED:
        return AlertItem(level=ALERT_LEVEL_RED, action=ACTION_LEG_RED,
                         code="leg_ic_ir_red", **base)
    if value <= IC_IR_ORANGE:
        return AlertItem(level=ALERT_LEVEL_ORANGE, action=ACTION_LEG_ORANGE,
                         code="leg_ic_ir_orange", **base)
    if value < IC_IR_YELLOW_FLOOR:
        return AlertItem(level=ALERT_LEVEL_YELLOW, action=ACTION_LEG_YELLOW,
                         code="leg_ic_ir_yellow", **base)
    return AlertItem(level=ALERT_LEVEL_GREEN, action=ACTION_NORMAL,
                     code="leg_ic_ir_green", **base)


# ---------------------------------------------------------------------------
# AT4 -> alert (labels only; no number is re-derived here)
# ---------------------------------------------------------------------------
_CAPACITY_KEYS: Tuple[str, ...] = (
    "turnover_label",
    "participation_label",
    "participation_available",
    "participation_exceeds_daily_volume",
    "participation_exceeds_average_volume",
)


def _as_capacity_mapping(source: Any) -> Dict[str, Any]:
    """Normalise the accepted capacity inputs to a flat field mapping.

    Accepted shapes (all read-only, nothing recomputed):

    * the flat dict from `live_capacity.capacity_labels(...)`;
    * any object exposing the same attribute names;
    * a `(turnover_observation, volume_participation)` pair of AT4 objects, from
      which only AT4's own labels / factual flags are read;
    * `None` (mapping is empty -- the caller supplied no capacity input at all).
    """
    if source is None:
        return {}
    if isinstance(source, Mapping):
        return {key: source.get(key) for key in _CAPACITY_KEYS}
    if isinstance(source, (tuple, list)) and len(source) == 2:
        turnover, participation = source
        return {
            "turnover_label": getattr(turnover, "turnover_label", None),
            "participation_label": getattr(participation, "participation_label", None),
            "participation_available": getattr(participation, "available", None),
            "participation_exceeds_daily_volume": getattr(
                participation, "exceeds_daily_volume", None
            ),
            "participation_exceeds_average_volume": getattr(
                participation, "exceeds_average_volume", None
            ),
        }
    return {key: getattr(source, key, None) for key in _CAPACITY_KEYS}


def _label(value: Any) -> Optional[str]:
    """Upper-cased AT4 label, or `None` when it is not a string."""
    return value.strip().upper() if isinstance(value, str) else None


def _flag(value: Any) -> Optional[bool]:
    """Tri-state boolean: `True`/`False` only for real bools, else `None`."""
    if value is True:
        return True
    if value is False:
        return False
    return None


def _turnover_item(fields: Mapping[str, Any]) -> AlertItem:
    label = _label(fields.get("turnover_label"))
    base = dict(dimension=DIMENSION_TURNOVER, subject="portfolio", source=SOURCE_AT4)
    if label == AT4_TURNOVER_BREACH:
        return AlertItem(level=ALERT_LEVEL_ORANGE, action=ACTION_TURNOVER_BREACH,
                         code="turnover_breach", **base)
    if label == AT4_TURNOVER_WATCH:
        return AlertItem(level=ALERT_LEVEL_YELLOW, action=ACTION_TURNOVER_WATCH,
                         code="turnover_watch", **base)
    if label == AT4_TURNOVER_OK:
        return AlertItem(level=ALERT_LEVEL_GREEN, action=ACTION_NORMAL,
                         code="turnover_ok", **base)
    # MISSING, absent, or an unknown label: unmeasurable, never GREEN.
    return AlertItem(
        level=ALERT_LEVEL_INSUFFICIENT,
        action=ACTION_TURNOVER_MISSING,
        code="turnover_missing",
        missing=(DIMENSION_TURNOVER,),
        notes=(f"AT4 turnover_label={fields.get('turnover_label')!r}; 换手不可测则不得当作正常",),
        **base,
    )


def _participation_item(fields: Mapping[str, Any]) -> AlertItem:
    base = dict(dimension=DIMENSION_PARTICIPATION, subject="portfolio", source=SOURCE_AT4)
    available = _flag(fields.get("participation_available"))
    label = _label(fields.get("participation_label"))
    if available is None and label is not None:
        # Fall back to AT4's own label; never infer availability from numbers.
        available = label == AT4_PARTICIPATION_OBSERVED
    exceeds_daily = _flag(fields.get("participation_exceeds_daily_volume"))
    exceeds_average = _flag(fields.get("participation_exceeds_average_volume"))

    if available is not True:
        return AlertItem(
            level=ALERT_LEVEL_INSUFFICIENT,
            action=ACTION_PARTICIPATION_MISSING,
            code="participation_missing",
            missing=(DIMENSION_PARTICIPATION,),
            notes=("AT4 participation available != True; 参与率不可测",),
            **base,
        )
    if exceeds_daily is True or exceeds_average is True:
        return AlertItem(level=ALERT_LEVEL_YELLOW, action=ACTION_PARTICIPATION_EXCEEDS,
                         code="participation_exceeds", **base)
    if exceeds_daily is False and exceeds_average is False:
        return AlertItem(level=ALERT_LEVEL_GREEN, action=ACTION_NORMAL,
                         code="participation_ok", **base)
    return AlertItem(
        level=ALERT_LEVEL_INSUFFICIENT,
        action=ACTION_PARTICIPATION_MISSING,
        code="participation_flags_unreadable",
        missing=(DIMENSION_PARTICIPATION,),
        notes=("AT4 participation is available but its exceeds_* flags are unreadable",),
        **base,
    )


def alert_for_capacity(labels_or_observations: Any) -> AlertItem:
    """One alert for the `AT4` capacity block. ADVISORY.

    Consumes AT4's **labels** (and its factual `exceeds_*` flags), never its
    numbers: the `L2.36` 8.0x / 10.0x bands stay `AT4`'s, and no participation
    ratio cut-off is invented here. The two sub-checks (`turnover`,
    `participation`) are combined with `combine_levels`, so an unmeasurable half
    makes the whole block `INSUFFICIENT` and the reason travels in `notes` /
    `missing`.

    `None` (nothing supplied) yields `INSUFFICIENT` -- a direct caller asking for
    a capacity verdict with no capacity data gets "无法判断", not "正常".
    """
    fields = _as_capacity_mapping(labels_or_observations)
    subs = [_turnover_item(fields), _participation_item(fields)]
    level = combine_levels(item.level for item in subs)
    insufficient = [item for item in subs if item.level == ALERT_LEVEL_INSUFFICIENT]
    missing: Tuple[str, ...] = ()
    notes: List[str] = [
        "ADVISORY ONLY: capacity labels are reported, never used to gate or resize an order",
        "turnover bands are AT4's (`L2.36` 8.0x qualified / 10.0x ceiling); AT5 does not "
        "re-derive a turnover number and defines no participation ratio cut-off",
    ]
    for item in subs:
        if item.level != ALERT_LEVEL_GREEN:
            notes.append(f"{item.dimension}: {item.level} -- {item.action}")
    if insufficient:
        missing = tuple(sorted({dim for item in insufficient for dim in item.missing}))

    if level == ALERT_LEVEL_INSUFFICIENT:
        # Report the unmeasurable half's frozen text; the other half stays in notes.
        return AlertItem(
            dimension=DIMENSION_CAPACITY,
            subject="portfolio",
            level=level,
            action=insufficient[0].action,
            code=insufficient[0].code,
            source=SOURCE_AT4,
            missing=missing,
            notes=tuple(notes),
        )
    if level == ALERT_LEVEL_GREEN:
        return AlertItem(
            dimension=DIMENSION_CAPACITY,
            subject="portfolio",
            level=level,
            action=ACTION_NORMAL,
            code="capacity_ok",
            source=SOURCE_AT4,
            missing=missing,
            notes=tuple(notes),
        )
    worst = max((item for item in subs if item.level != ALERT_LEVEL_INSUFFICIENT),
                key=lambda item: severity_rank(item.level))
    return AlertItem(
        dimension=DIMENSION_CAPACITY,
        subject="portfolio",
        level=level,
        action=worst.action,
        code=worst.code,
        source=SOURCE_AT4,
        missing=missing,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------
def build_alert_matrix(
    leg_reports: Iterable[Any] = (),
    capacity: Any = None,
    *,
    as_of: Any = None,
) -> AlertMatrix:
    """Assemble the advisory matrix from `AT3` legs and one `AT4` capacity block.

    Parameters
    ----------
    leg_reports:
        `LegICReport` objects (or anything with `status` / `ic_ir`). One item each.
    capacity:
        The flat dict from `live_capacity.capacity_labels(...)`, an equivalent
        object, or a `(turnover, participation)` pair. `None` means the capacity
        dimension was **not supplied** and contributes no item -- distinct from a
        supplied-but-empty mapping, which contributes `INSUFFICIENT`.
    as_of:
        Optional session/date string echoed into the report (never parsed, never
        used for arithmetic -- this module has no clock).

    Returns
    -------
    `AlertMatrix` with `level = combine_levels(...)`, `worst_measured_level` =
    most severe ordinal item level (`GREEN` when none), and
    `missing_dimensions` listing everything that was unmeasurable.
    """
    items: List[AlertItem] = [alert_for_leg(report) for report in (leg_reports or ())]
    if capacity is not None:
        items.append(alert_for_capacity(capacity))

    levels = [item.level for item in items]
    level = combine_levels(levels)
    measured = [item.level for item in items if item.level != ALERT_LEVEL_INSUFFICIENT]
    worst_measured = max(measured, key=severity_rank) if measured else ALERT_LEVEL_GREEN

    missing = tuple(sorted({dim for item in items
                            if item.level == ALERT_LEVEL_INSUFFICIENT
                            for dim in (item.missing or (item.dimension,))}))

    notes: List[str] = [
        "ADVISORY ONLY: report text only -- no order, no re-weight, no liquidation "
        "(AT5 clause 4: 首版只出报告，不接降权/清盘执行)",
        "aggregation: RED > ORANGE > YELLOW > GREEN; 任何 INSUFFICIENT 都使整体为 "
        "INSUFFICIENT（完整性优先于严重度），空输入亦为 INSUFFICIENT",
        "no existing threshold is reused: drawdown 10/15/20, LV1, R1 -2%/-3%/-4%, "
        "R4 0.75/0.50*B, R6 and L2.36 8.0/10.0 own different axes (see docs/signal_alerts.md)",
    ]
    if not items:
        notes.append(f"no signal-layer input at all: {ACTION_NO_INPUT} (never GREEN)")
    if level == ALERT_LEVEL_INSUFFICIENT and items:
        notes.append(
            "缺失维度（不得当作正常）: "
            + ", ".join(missing)
            + "; 最严重的已测档位为 " + worst_measured
            + " (见 worst_measured_level，未被覆盖)"
        )

    return AlertMatrix(
        items=tuple(items),
        level=level,
        worst_measured_level=worst_measured,
        missing_dimensions=missing,
        notes=tuple(notes),
        as_of=None if as_of is None else str(as_of),
    )
