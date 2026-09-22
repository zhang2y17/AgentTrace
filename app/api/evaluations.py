"""评测端点：``POST /evaluations`` 与 ``GET /evaluations/{evaluation_id}``。

契约 API_CONTRACT §6/§7，流程见 EVALUATION §5。

**本层职责边界**：参数解析、服务调用、异常映射。
执行与判定在 ``app/evaluation/runner.py``，编排与重读在
``app/services/evaluation_service.py``。

三处契约语义必须原样遵守，都在本模块体现：

1. **``case_keys`` 含未知用例 → 400**，不是静默忽略。忽略会让调用方
   以为"跑了我指定的 3 个 case"，实际跑了别的；
2. **``only_failures`` 需 ``include_cases=true``**。单独给
   ``only_failures=true`` 而没要 cases 时，响应里没有 ``cases`` 字段 ——
   契约只要求"仅返回失败 case（需 include_cases=true）"。
   这里选择**不报错**：``only_failures`` 在没有 ``cases`` 时是无害的
   no-op，为一个无害组合报 400 会让客户端多写一层分支；
3. **评测结果的指标必须以重算为准**。``GET`` 不读缓存，
   从 ``eval_run`` 行重新聚合（见 ``EvaluationService.get_evaluation``）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Path, Query, Response, status

from app.api.deps import EvaluationServiceDep
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["evaluations"])

# 依赖用 ``EvaluationServiceDep``（函数包装），不要写成
# ``Depends(EvaluationService)`` —— 直接依赖类会让 FastAPI 内省
# ``__init__`` 并把 ``session_factory`` / ``run_service`` 暴露成请求参数。
ServiceDep = EvaluationServiceDep

from app.schemas.eval import (  # noqa: E402 —— 放在 ServiceDep 之后以保持依赖注释邻近
    CreateEvaluationRequest,
    EvaluationCaseResult,
    EvaluationResponse,
)


@router.post(
    "/evaluations",
    response_model=EvaluationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="对指定评测集执行一次评测",
)
def create_evaluation(
    payload: Annotated[CreateEvaluationRequest, Body(description="评测请求体")],
    response: Response,
    service: ServiceDep,
) -> EvaluationResponse:
    """执行一次离线评测。

    逐 case 串行执行；单个 case 失败**不中断整批** ——
    "Agent 在有挑战的 case 上表现如何"正是评测要回答的问题。

    带 ``thresholds`` 时顺带做门禁判定并把结论落进 ``quality_gate``；
    判定结果不改变本端点的状态码（评测跑通了就是 201），
    它体现在 ``metrics`` 与后续的 ``POST /quality-gates/check`` 里。
    """
    thresholds = _thresholds_to_mapping(payload.thresholds)

    result, _gate = service.create_evaluation(
        dataset_version=payload.dataset_version,
        agent_version=payload.agent_version,
        prompt_version=payload.prompt_version,
        case_keys=payload.case_keys,
        thresholds=thresholds,
    )

    response.headers["Location"] = f"/evaluations/{result.evaluation_id}"

    logger.info(
        "evaluation_created",
        extra={
            "evaluation_id": result.evaluation_id,
            "dataset_version": payload.dataset_version,
            "case_count": result.case_count,
            "passed_cases": result.passed_cases,
            "failed_cases": result.failed_cases,
            "is_test_double": result.is_test_double,
        },
    )

    return EvaluationResponse(**_normalize_payload(result.to_dict()))


@router.get(
    "/evaluations/{evaluation_id}",
    response_model=EvaluationResponse,
    summary="查询评测结果",
)
def get_evaluation(
    service: ServiceDep,
    evaluation_id: Annotated[str, Path(description="评测批次 ID（eval_ 前缀）")],
    include_cases: Annotated[bool, Query(description="内联逐 case 结果")] = False,
    only_failures: Annotated[
        bool, Query(description="仅返回失败 case（需 include_cases=true）")
    ] = False,
) -> EvaluationResponse:
    """查询评测结果。

    指标从 ``eval_run`` 行**重新聚合**而不是读缓存 ——
    这样报告与库里记录的事实永远自洽。
    """
    _stored, payload = service.get_evaluation(
        evaluation_id,
        include_cases=include_cases,
        only_failures=only_failures,
    )

    return EvaluationResponse(**_normalize_payload(payload))


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


def _normalize_payload(payload: dict) -> dict:
    """把服务层的输出收敛到契约模型的字段集。

    三件事，都是"响应必须自洽"的具体体现：

    1. **剔除契约模型未声明的字段**。``EvaluationResponse`` 是
       ``extra="forbid"`` 的 —— 这是刻意的：契约模型不该悄悄接受
       未文档化的键。而 ``compute_metrics`` 会产出几个**内部诊断用**
       的键（``tool_argument_eligible_count``、``latency_sample_size``、
       ``latency_p95_small_sample``），它们服务于 CLI 报告与门禁，
       不在 API_CONTRACT §6 的 ``metrics`` 形状里。
       处理方式是**在这里剔除**，而不是放宽契约模型 ——
       放宽会让"响应比文档多出字段"变成常态，调用方再也无法
       依赖契约判断字段是否存在。

    2. **``data_source_note`` 不能被清空** —— 契约 B9 要求任何指标输出
       都带口径声明。宁可重复声明也不能静默失去它。

    3. **补 ``skipped_metrics``** —— 契约模型有这个字段，
       重读路径不产出它，补空列表而不是让 Pydantic 因缺字段而报错。
    """
    # 契约 MetricsBlock 允许的键（唯一真源：模型自身的字段声明）
    from app.schemas.metrics import OFFLINE_EVAL_NOTE, MetricsBlock

    allowed_metric_keys = set(MetricsBlock.model_fields)
    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        payload["metrics"] = {
            key: value for key, value in metrics.items() if key in allowed_metric_keys
        }

    # 逐 case 同样收敛。``tool_sequence`` 与 ``needs_human_review`` 是
    # 评测内部的诊断字段（前者解释 M3 为何判错，后者解释 M10 为何计数），
    # 它们出现在 CLI 报告与调试输出里很有用，但不属契约 §7 的 case 形状。
    cases = payload.get("cases")
    if isinstance(cases, list):
        allowed_case_keys = set(EvaluationCaseResult.model_fields)
        payload["cases"] = [
            {key: value for key, value in case.items() if key in allowed_case_keys}
            for case in cases
            if isinstance(case, dict)
        ]

    if not payload.get("data_source_note"):
        payload["data_source_note"] = OFFLINE_EVAL_NOTE
    payload.setdefault("skipped_metrics", [])

    # 顶层同样收敛：内部诊断字段（pricing_version / cost_estimation_unavailable）
    # 不属契约 §6 的响应形状。它们的价值在 CLI 报告里，那里不走 Pydantic。
    allowed_top_keys = set(EvaluationResponse.model_fields)
    for key in [key for key in payload if key not in allowed_top_keys]:
        payload.pop(key)

    return payload


__all__ = ["create_evaluation", "get_evaluation", "router"]
