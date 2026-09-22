"""质量门禁端点：``POST /quality-gates/check``。

契约 API_CONTRACT §9，判定规则见 EVALUATION §6。

**最重要的三种语义**，都在本模块与服务层体现：

1. **门禁未通过也返回 200**，用 ``passed`` 表达结论。门禁失败是
   **业务判断**，不是 HTTP 错误。用 4xx 会让 CI 分不清
   "质量不达标"与"请求写错了"，而这两者的处置完全不同：前者要
   回去改 Agent，后者要改调用代码。

2. **阈值非法返回 400**（未知指标名、运算符与指标类型不匹配）。
   静默忽略出错的阈值条目是最坏的一类沉默 ——
   调用方会以为阈值生效了，实际门禁用的是另一套阈值。

3. **跳过的指标必须出现在 ``skipped_metrics`` 里**，且**不得**
   在文档中把"跳过"说成"通过"（EVALUATION §6.3 与反模式清单）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body

from app.api.deps import EvaluationServiceDep
from app.core.logging import get_logger
from app.schemas.eval import QualityGateRequest, QualityGateResponse

logger = get_logger(__name__)

router = APIRouter(tags=["quality-gates"])

# 依赖用函数包装，理由同 evaluations.py。
ServiceDep = EvaluationServiceDep


@router.post(
    "/quality-gates/check",
    response_model=QualityGateResponse,
    summary="按阈值检查评测结果",
)
def check_quality_gate(
    payload: Annotated[QualityGateRequest, Body(description="门禁检查请求体")],
    service: ServiceDep,
) -> QualityGateResponse:
    """按阈值检查一次评测，并阻断不达标的版本。

    ``blocked = true`` 表示该版本**不应**通过门禁，
    CI 或本地脚本应据此返回非 0 退出码（EVALUATION §6.4）。

    门禁结论会写入 ``quality_gate`` 表，含**阈值快照** ——
    阈值后来被改了也能复现"当时为什么算通过"。
    """
    thresholds = _thresholds_to_mapping(payload.thresholds)

    result = service.run_gate(
        evaluation_id=payload.evaluation_id,
        gate_name=payload.gate_name,
        thresholds=thresholds,
    )

    logger.info(
        "quality_gate_requested",
        extra={
            "gate_id": result.gate_id,
            "gate_name": result.gate_name,
            "evaluation_id": payload.evaluation_id,
            "passed": result.passed,
            "blocked": result.blocked,
            "violation_count": len(result.violations),
            "skipped_metrics": result.skipped_metrics,
        },
    )

    return _to_response(result)


def _thresholds_to_mapping(thresholds: object) -> dict[str, dict[str, float]] | None:
    """把 Pydantic 阈值模型转成服务层用的普通字典。

    ``None`` 表示"没传阈值，用默认阈值"。

    **空 bounds 必须原样传下去，不能在这里丢弃。**
    ``{"run_success_rate": {}}``（指标写了但没给 min/max）在下面
    逐条转换时会被过滤成一个空 entry，若再顺手把空 entry 丢掉，
    整个映射就变成了 ``{}``，于是退化成"用默认阈值" ——
    调用方明确写了阈值、却被静默换成了另一套，而响应里看不出任何区别。

    转换后为空时**也**返回 ``{}`` 而不是 ``None``：让 ``validate_thresholds``
    去区分"没传"（``None``）与"传了但为空"（``{}``）并各自给出正确结论。
    在那里报 400 有两个好处：错误来源唯一，
    且 Pydantic 模型这一层本来就极难区分 ``{}`` 与 ``None``。

    注意 ``ThresholdSpec`` 的 ``min``/``max`` 默认是 ``None``，
    所以"没给"与"显式给了 null"在这一层不可区分 —— 两者都按"未提供"处理，
    由"整个对象为空"这一事实来兜底报错。
    """
    if thresholds is None:
        return None

    if isinstance(thresholds, dict) and not thresholds:
        return {}

    mapping: dict[str, dict[str, float]] = {}
    for metric, spec in thresholds.items():  # type: ignore[union-attr]
        entry: dict[str, float] = {}
        if getattr(spec, "min", None) is not None:
            entry["min"] = float(spec.min)
        if getattr(spec, "max", None) is not None:
            entry["max"] = float(spec.max)
        # 空 entry 也要保留，理由见 docstring。
        mapping[metric] = entry

    return mapping or {}


def _to_response(result: Any) -> QualityGateResponse:
    """组装契约 §9 的响应体。

    两处归一：

    - ``thresholds`` 要转成 ``ThresholdSpec`` 形状（``{"min": x}``），
      GateResult 内部已经是这个形状，直接用；
    - ``observed_metrics`` 里可能含 ``None``（被跳过的指标），
      而 ``QualityGateResponse`` 声明为 ``dict[str, float]``。
      这里把 ``None`` 值剔除 —— 它已经由 ``skipped_metrics``
      单独表达了，重复出现在 observed 里会让类型不符，
      而放 0 又等于谎报观测值。
    """
    observed = {
        metric: value
        for metric, value in (result.observed_metrics or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }

    return QualityGateResponse(
        gate_id=result.gate_id,
        gate_name=result.gate_name,
        evaluation_id=result.evaluation_id,
        passed=result.passed,
        blocked=result.blocked,
        status=result.status,
        observed_metrics=observed,
        thresholds=result.thresholds,
        violations=[item.to_dict() for item in result.violations],
        skipped_metrics=result.skipped_metrics,
        checked_at=result.checked_at,
        data_source_note=result.data_source_note,
    )


__all__ = ["check_quality_gate", "router"]
