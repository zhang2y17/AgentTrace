"""质量门禁（契约 EVALUATION §6）。

判定逻辑本身很简单：``violated(m, θ)`` 逐个比对，收集违规项，
``passed = (len(violations) == 0)``。

**真正需要小心的是三个边界**，它们都在本模块里逐一落实：

1. **指标为 ``null`` 时该指标不参与判定**（§6.3 第 2 条）。
   空数据（无 case、无工具期望）会让某些指标是 ``None``。
   若把 ``None`` 当 0 参与比较，一个"压根没测"的指标会被判成
   "严重不达标"，门禁失败的原因就完全指错了方向。
   正确做法是列入 ``skipped_metrics`` —— 且**不允许**在文档里
   把"跳过"说成"通过"（§6.3 与反模式清单都点了这一条）。

2. **阈值快照必须持久化**（§6.3 第 3 条）。事后必须能解释
   "当时为什么算通过"。若只存结果不存阈值，阈值后来被改了，
   历史结论就再也无法复现。

3. **运算符与指标类型必须匹配**（API_CONTRACT §9 错误表）。
   给数据加 ``max`` 是纯粹的配置错误，必须 400 而不是静默忽略。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.errors import InvalidArgumentError
from app.core.logging import get_logger
from app.evaluation.metrics import COST_METRICS, RATIO_METRICS

logger = get_logger(__name__)

# 门禁通过时对外声明的数据来源说明（契约 API_CONTRACT §9 示例末行）
GATE_DATA_SOURCE_NOTE = (
    "阈值判定基于离线评测结果，通过门禁仅表示满足本项目定义的离线质量基线，不等于可生产发布。"
)

# 允许的运算符
_OPERATOR_MIN = "min"
_OPERATOR_MAX = "max"
_ALLOWED_OPERATORS = frozenset({_OPERATOR_MIN, _OPERATOR_MAX})

# 指标名全集：比率型 ∪ 代价型。两者都不在的指标名是未知指标 → 400。
_KNOWN_METRICS = RATIO_METRICS | COST_METRICS


@dataclass(slots=True)
class Violation:
    """一条阈值违规。"""

    metric: str
    operator: str
    threshold: float
    observed: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "operator": self.operator,
            "threshold": self.threshold,
            "observed": self.observed,
            "reason": self.reason,
        }


@dataclass(slots=True)
class GateResult:
    """一次门禁检查的结果。"""

    gate_id: str
    gate_name: str
    evaluation_id: str
    passed: bool
    blocked: bool
    status: str
    thresholds: dict[str, Any]
    observed_metrics: dict[str, Any]
    violations: list[Violation] = field(default_factory=list)
    skipped_metrics: list[str] = field(default_factory=list)
    checked_at: datetime | None = None
    data_source_note: str = GATE_DATA_SOURCE_NOTE

    def to_dict(self) -> dict[str, Any]:
        """转成契约 API_CONTRACT §9 的响应体。

        无论通过与否都返回 200，用 ``passed`` 表达结果 ——
        门禁失败是**业务结论**，不是 HTTP 错误。
        用 4xx 表达会让 CI 难以区分"门禁没通过"与"请求写错了"。
        """
        return {
            "gate_id": self.gate_id,
            "gate_name": self.gate_name,
            "evaluation_id": self.evaluation_id,
            "passed": self.passed,
            "blocked": self.blocked,
            "status": self.status,
            "thresholds": self.thresholds,
            "observed_metrics": self.observed_metrics,
            "violations": [item.to_dict() for item in self.violations],
            "skipped_metrics": self.skipped_metrics,
            "checked_at": (
                self.checked_at.isoformat().replace("+00:00", "Z")
                if self.checked_at is not None
                else None
            ),
            "data_source_note": self.data_source_note,
        }


def default_thresholds() -> dict[str, dict[str, float]]:
    """默认阈值（EVALUATION §6.1）。

    从配置 ``GATE_DEFAULT_THRESHOLDS_JSON`` 读取，使阈值可通过环境变量
    调整而不必改代码；配置损坏时退化为内置常量。
    """
    from app.core.config import get_settings

    raw = get_settings().gate_default_thresholds_json
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("gate_thresholds_config_invalid", extra={"raw_length": len(raw)})
        parsed = _BUILTIN_DEFAULT_THRESHOLDS

    if not isinstance(parsed, dict) or not parsed:
        parsed = _BUILTIN_DEFAULT_THRESHOLDS

    return {str(metric): dict(bounds) for metric, bounds in parsed.items()}


def validate_thresholds(
    thresholds: dict[str, Any] | None,
) -> dict[str, dict[str, float]]:
    """校验并规整阈值定义。

    Returns:
        规整后的 ``{metric: {"min"|"max": value}}``。

    Raises:
        InvalidArgumentError: 未知指标名、运算符不匹配、值非数值。

    三类错误都必须报 400（API_CONTRACT §9 错误表），
    而不是静默丢弃出错的条目 —— 静默丢弃会让调用方以为阈值生效了，
    实际门禁用的是另一套阈值。这是最坏的一类沉默。
    """
    if thresholds is None:
        return default_thresholds()

    if not isinstance(thresholds, dict):
        raise InvalidArgumentError(
            "thresholds 必须是对象：{metric: {'min'|'max': number}}。",
            details={"received_type": type(thresholds).__name__},
        )

    if not thresholds:
        # 空阈值集必须报错，不能当作"用默认阈值"。
        #
        # 这条边界容易搞反：``thresholds={}`` 与 ``thresholds=None``
        # 在 Python 里同样 falsy，看起来可以直接合并处理。
        # 但两者的**语义完全不同**：
        #
        # - 没传 (``None``) → 使用者没说，用默认阈值是合理推断；
        # - 传了空对象 (``{}``) → 使用者明确表达了"一个阈值都不要"，
        #   而此时门禁会退化成一条空违规集（``violations == []``），
        #   于是 ``passed=true`` 恒定成立 —— 一个永远通过的门禁。
        #
        # 更隐蔽的是：它还会**覆盖掉默认阈值**。调用方看到 200
        # 与 ``passed=true``，会认为"默认基线检查通过了"，
        # 而实际一个指标都没检查。
        #
        # 注意这一类**不能**靠调整 API 层的 falsy 判断来修：
        # Pydantic 模型里的 ``thresholds={}`` 与 ``None`` 几乎无法区分，
        # 而 ``validate_thresholds`` 是同时能看到"整体为空"与
        # "单个指标为空"的唯一位置（后者已经在下面报错）。
        raise InvalidArgumentError(
            "thresholds 不能是空对象。传空对象会覆盖默认阈值，"
            "导致门禁不检查任何指标而恒返回通过；"
            "如需使用默认阈值请不要传该字段。",
            details={
                "received": {},
                "hint": "omit the field to use default thresholds",
                "default_metric_count": len(default_thresholds()),
            },
        )

    normalized: dict[str, dict[str, float]] = {}

    for metric, bounds in thresholds.items():
        if metric not in _KNOWN_METRICS:
            raise InvalidArgumentError(
                f"未知指标名：{metric!r}。",
                details={
                    "unknown_metric": metric,
                    "allowed_metrics": sorted(_KNOWN_METRICS),
                },
            )

        if not isinstance(bounds, dict) or not bounds:
            raise InvalidArgumentError(
                f"指标 {metric!r} 的阈值必须是对象，例如 {{'min': 0.9}}。",
                details={"metric": metric},
            )

        entry: dict[str, float] = {}
        for operator, value in bounds.items():
            if operator not in _ALLOWED_OPERATORS:
                raise InvalidArgumentError(
                    f"指标 {metric!r} 使用了未知运算符 {operator!r}。",
                    details={"metric": metric, "allowed_operators": sorted(_ALLOWED_OPERATORS)},
                )

            expected = _OPERATOR_MIN if metric in RATIO_METRICS else _OPERATOR_MAX
            if operator != expected:
                raise InvalidArgumentError(
                    f"指标 {metric!r} 是{'比率' if expected == _OPERATOR_MIN else '代价'}型，"
                    f"只接受 {expected!r} 运算符，收到 {operator!r}。",
                    details={
                        "metric": metric,
                        "metric_kind": "ratio" if expected == _OPERATOR_MIN else "cost",
                        "expected_operator": expected,
                        "received_operator": operator,
                    },
                )

            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidArgumentError(
                    f"指标 {metric!r} 的阈值必须是数值，收到 {type(value).__name__}。",
                    details={"metric": metric, "value_type": type(value).__name__},
                )

            entry[operator] = float(value)

        normalized[metric] = entry

    return normalized


def check_gate(
    *,
    evaluation_id: str,
    gate_name: str,
    metrics: dict[str, Any],
    thresholds: dict[str, Any] | None = None,
    gate_id: str | None = None,
) -> GateResult:
    """按阈值检查指标。

    Args:
        evaluation_id: 被检查的评测批次。
        gate_name: 门禁名。
        metrics: 评测算出的指标字典（``compute_metrics`` 的输出）。
        thresholds: 阈值覆盖；``None`` 用默认阈值。
        gate_id: 预先分配的门禁 ID；``None`` 时自动生成。

    Returns:
        ``GateResult``。

    Raises:
        InvalidArgumentError: 阈值定义非法。
    """
    from app.core.ids import new_quality_gate_id

    normalized = validate_thresholds(thresholds)

    violations: list[Violation] = []
    skipped: list[str] = []
    observed: dict[str, Any] = {}

    for metric, bounds in normalized.items():
        value = metrics.get(metric)

        # ------------------------------------------------------ 跳过语义
        # ``None`` 表示"无从计算"（空数据、无工具期望等）。
        # 不参与判定，但必须列进 skipped_metrics 让读者知道它没被检查。
        if value is None:
            skipped.append(metric)
            observed[metric] = None
            continue

        try:
            observed_value = float(value)
        except (TypeError, ValueError):
            # 非数值（如意外混入字符串）同样跳过而不是判违规 ——
            # 判违规会把"数据形状不对"误报成"质量不达标"。
            logger.warning(
                "gate_metric_not_numeric",
                extra={"metric": metric, "value_type": type(value).__name__},
            )
            skipped.append(metric)
            observed[metric] = value
            continue

        observed[metric] = observed_value

        if _OPERATOR_MIN in bounds:
            threshold = bounds[_OPERATOR_MIN]
            if observed_value < threshold:
                violations.append(
                    Violation(
                        metric=metric,
                        operator=_OPERATOR_MIN,
                        threshold=threshold,
                        observed=observed_value,
                        reason=f"{metric} {_fmt(observed_value)} 低于阈值 {_fmt(threshold)}",
                    )
                )
        elif _OPERATOR_MAX in bounds:
            threshold = bounds[_OPERATOR_MAX]
            if observed_value > threshold:
                violations.append(
                    Violation(
                        metric=metric,
                        operator=_OPERATOR_MAX,
                        threshold=threshold,
                        observed=observed_value,
                        reason=(
                            f"{metric} {_fmt(observed_value)} 高于阈值 "
                            f"{_fmt(threshold)}（越低越好）"
                        ),
                    )
                )

    # 契约 EVALUATION §6.2：passed = (len(violations) == 0)。
    # skipped 不影响 passed —— 但这份结果必须把 skipped 带出去，
    # 否则读者会把"有 3 项没检查"的门禁当成"9 项全过"。
    passed = not violations

    result = GateResult(
        gate_id=gate_id or new_quality_gate_id(),
        gate_name=gate_name,
        evaluation_id=evaluation_id,
        passed=passed,
        blocked=not passed,
        status="passed" if passed else "failed",
        thresholds=normalized,
        observed_metrics=observed,
        violations=violations,
        skipped_metrics=sorted(skipped),
        checked_at=_now(),
    )

    logger.info(
        "quality_gate_checked",
        extra={
            "gate_id": result.gate_id,
            "gate_name": gate_name,
            "evaluation_id": evaluation_id,
            "passed": passed,
            "violation_count": len(violations),
            "skipped_count": len(skipped),
        },
    )
    return result


def check_metric_invariants(metrics: dict[str, Any]) -> list[str]:
    """检查指标之间的数学关系，返回被违反的不变式描述。

    这是**自检**而不是门禁：指标之间有几条恒成立的关系，
    违反它们说明指标实现有 bug，而不是 Agent 质量差。

    - ``task_completion_rate <= run_success_rate``（EVALUATION §3 M2）。
      契约明确写"若出现 M2 > M1，说明断言逻辑有 bug"；
    - ``error_rate <= 1 - run_success_rate``：错误与成功互斥
      （``degraded`` 三者相加恒为 1，故上界如此）。

    Returns:
        违反的不变式列表；空列表表示全部成立。
    """
    violations: list[str] = []

    m1 = metrics.get("run_success_rate")
    m2 = metrics.get("task_completion_rate")
    if m1 is not None and m2 is not None and m2 > m1 + 1e-9:
        violations.append(
            f"task_completion_rate ({m2}) > run_success_rate ({m1})："
            "M2 <= M1 恒成立，违反说明完成度判定逻辑有 bug"
        )

    error = metrics.get("error_rate")
    if m1 is not None and error is not None and error > (1 - m1) + 1e-9:
        violations.append(
            f"error_rate ({error}) > 1 - run_success_rate ({1 - m1})：失败与成功是互斥口径"
        )

    return violations


def _fmt(value: float) -> str:
    """把阈值/观测值格式化成人类可读的短字符串。"""
    if isinstance(value, Decimal):
        value = float(value)
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _now() -> datetime:
    from app.db.base import utcnow

    return utcnow()


# 配置损坏时的兜底阈值，与 EVALUATION §6.1 逐字一致。
_BUILTIN_DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "run_success_rate": {"min": 0.90},
    "task_completion_rate": {"min": 0.85},
    "tool_selection_accuracy": {"min": 0.90},
    "tool_argument_accuracy": {"min": 0.85},
    "evidence_coverage": {"min": 0.70},
    "latency_ms_p95": {"max": 2000},
    "estimated_cost_usd": {"max": 0.05},
    "error_rate": {"max": 0.05},
    "human_review_rate": {"max": 0.00},
}


__all__ = [
    "GATE_DATA_SOURCE_NOTE",
    "GateResult",
    "Violation",
    "check_gate",
    "check_metric_invariants",
    "default_thresholds",
    "validate_thresholds",
]
