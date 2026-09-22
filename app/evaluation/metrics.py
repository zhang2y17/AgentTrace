"""评测指标实现（契约 EVALUATION §2/§3）。

**本模块的全部函数都是确定性的纯函数**：输入 case 结果列表，
输出指标值。不查库、不调模型、不读时钟。契约 PROJECT_SPEC §3.3
明确要求"统计、阈值和成本计算必须由确定性代码完成"。

三个贯穿全文的约定：

1. **分母是评测集大小 $N$，含失败 case**（EVALUATION §3 首段）。
   用"成功执行数"做分母会让失败同时从分子分母消失，指标虚高；
2. **``degraded`` 不计入成功，也不计入错误**（EVALUATION §3 M1/M9）。
   它有独立的 ``degraded_rate`` 口径。"跑完了但证据不足"
   既不是成功也不是失败；
3. **分母为 0 返回 ``None`` 而不是 0**（IMPLEMENTATION_PLAN S6 风险表）。
   ``0.0`` 会被读成"表现极差"，``None`` 才诚实地表示"无从计算"。
   所有除法统一走 ``_safe_ratio``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.evaluation.assertions import AssertionResult

# ---------------------------------------------------------------------------
# run 状态口径（EVALUATION §3）
# ---------------------------------------------------------------------------

# 计入 run_success_rate 分子
SUCCESS_STATUSES = frozenset({"succeeded"})
# 计入 error_rate 分子。**timeout 计错误**（EVALUATION §3 M9 明确定义），
# 注意这与"run 级 timeout 不计入 error_rate"是两套不同口径：
# 前者是评测批次的错误率，后者是 /metrics/summary 的运行级错误率。
ERROR_STATUSES = frozenset({"failed", "timeout"})
# 降级：跑完但证据不足，单独统计
DEGRADED_STATUSES = frozenset({"degraded"})


@dataclass(slots=True)
class CaseOutcome:
    """一个 case 在评测中的完整结果。

    这是指标层的输入单元。它同时承载"数值"与"判定"：
    ``task_completed`` / ``tool_selection_correct`` /
    ``tool_argument_correct`` 三个布尔由 ``runner`` 计算后填入，
    指标模块只负责聚合，不重复判定逻辑。

    ``tool_*`` 为 ``None`` 表示**该 case 无对应期望，不参与该指标**
    （EVALUATION §3 M3/M4 的分母说明）。这与 ``False``
    （参与了但不正确）语义不同，必须区分 —— 否则
    "没期望" 会被算成 "做错了"，拉低准确率。
    """

    case_key: str
    status: str
    task_completed: bool = False
    tool_selection_correct: bool | None = None
    tool_argument_correct: bool | None = None
    evidence_coverage: float | None = None
    latency_ms: int | None = None
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    needs_human_review: bool = False
    assertion_results: list[AssertionResult] = field(default_factory=list)
    failure_reason: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status in SUCCESS_STATUSES

    @property
    def is_error(self) -> bool:
        return self.status in ERROR_STATUSES

    @property
    def is_degraded(self) -> bool:
        return self.status in DEGRADED_STATUSES


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    """安全除法：分母为 0 时返回 ``None``。

    这是本模块唯一的除法入口。**不允许任何地方直接写 ``a / b``** ——
    一次疏忽就会让空评测集报出 ``nan`` 或抛 ``ZeroDivisionError``，
    而 ``nan`` 在 JSON 里会变成 ``null``（Python）或非法字面量（部分客户端），
    两种表现都与"无从计算"的语义对不上。
    """
    if not denominator:
        return None
    return numerator / denominator


def _round(value: float | None, digits: int = 4) -> float | None:
    """按契约示例的小数位收敛结果。``None`` 原样透传。"""
    if value is None:
        return None
    return round(value, digits)


def percentile(sorted_values: list[int], p: float) -> int | None:
    """nearest-rank 分位数（EVALUATION §3 M6）。

    $\\text{quantile}(p) = l_{\\lceil p \\cdot N \\rceil}$，样本升序、1-based。

    **必须传已排序的样本**：本函数为了保持纯粹不做排序 ——
    在大列表上隐式排序会让调用方无从知道代价，
    而排序在聚合层只需做一次。

    Args:
        sorted_values: **已升序排序**的样本。
        p: 0~1 之间。

    Returns:
        分位数值；``N = 0`` 时返回 ``None``。

    Raises:
        ValueError: ``p`` 不在 0~1 之间。

    用 nearest-rank 而非线性插值：N=12 这种小样本下插值会产出
    **从未观测到**的数值（例如样本只有 100/200ms，插值给出 105ms）。
    用未观测值做验收是不诚实的。
    """
    if not 0.0 <= p <= 1.0:
        raise ValueError("p 必须在 0~1 之间")

    n = len(sorted_values)
    if n == 0:
        return None

    rank = _nearest_rank(n, p)
    return sorted_values[rank - 1]


def _nearest_rank(n: int, p: float) -> int:
    """计算 nearest-rank 的位置（1-based）：``ceil(p * n)``。

    **不用 ``math.ceil(p * n)``**：浮点乘法会给出意想不到的结果，
    例如 ``0.95 * 20`` 在 IEEE 754 下是 ``18.999999999999996``，
    ``ceil`` 后得到 19 —— 而数学上 ``ceil(0.95 × 20) = ceil(19) = 20``。
    这是**静默取错样本**：结果 19 也是样本里真实存在的值，
    所以没有任何断言能发现它，报告出的 p95 只是悄悄偏低了一档。

    修法是把 p 先转成精确的十进制再乘：

    - ``Decimal(str(p))`` 用 ``str`` 中转，避免把 float 的二进制
      误差带进 Decimal（``Decimal(0.95)`` 会得到一长串尾数）；
    - ``Decimal`` 乘法是精确的十进制运算，``0.95 * 20`` 得到 ``19.00``，
      向上取整得到 20；
    - 最后夹紧到 ``[1, n]``，兼顾 ``p = 0`` 与 ``p = 1`` 两端。
    """
    from decimal import ROUND_CEILING, Decimal

    exact = Decimal(str(p)) * Decimal(n)
    rank = int(exact.to_integral_value(rounding=ROUND_CEILING))
    return max(1, min(rank, n))


# ---------------------------------------------------------------------------
# 十个指标
# ---------------------------------------------------------------------------


def run_success_rate(outcomes: list[CaseOutcome]) -> float | None:
    """M1：``|{status == succeeded}| / N``。

    ``degraded`` 不计入成功（EVALUATION §3 M1 注意）。
    """
    succeeded = sum(1 for outcome in outcomes if outcome.is_success)
    return _round(_safe_ratio(succeeded, len(outcomes)))


def task_completion_rate(outcomes: list[CaseOutcome]) -> float | None:
    """M2：``|{task_completed}| / N``。

    恒有 ``M2 <= M1``：``task_completed`` 的前提包含 ``status == succeeded``
    （或反向断言要求的 ``failed``）。若出现 ``M2 > M1``，
    说明 ``runner`` 的判定逻辑有 bug —— 这条关系由
    ``check_metric_invariants`` 断言，见 ``gate.py``。
    """
    completed = sum(1 for outcome in outcomes if outcome.task_completed)
    return _round(_safe_ratio(completed, len(outcomes)))


def tool_selection_accuracy(outcomes: list[CaseOutcome]) -> float | None:
    """M3：``|{S_i}| / |{expected_tools != ∅}|``。

    **分母只统计有工具期望的 case**（EVALUATION §3 M3 分母说明）。
    若把无期望的 case 也计入分母，它们的 ``None`` 会被当成不匹配，
    准确率被无谓拉低。

    返回 ``None`` 表示"没有任何 case 有工具期望"，
    此时该指标不参与门禁判定（进 ``skipped_metrics``）。
    """
    eligible = [o for o in outcomes if o.tool_selection_correct is not None]
    matched = sum(1 for o in eligible if o.tool_selection_correct)
    return _round(_safe_ratio(matched, len(eligible)))


def tool_selection_eligible_count(outcomes: list[CaseOutcome]) -> int:
    """M3 的分母大小。

    契约明确要求评测报告**同时给出**这个数：只报准确率而不报分母，
    读者无法知道它是在 11 个 case 上算的还是在 1 个 case 上算的。
    """
    return sum(1 for outcome in outcomes if outcome.tool_selection_correct is not None)


def tool_argument_accuracy(outcomes: list[CaseOutcome]) -> float | None:
    """M4：``|{(i,t) : A_{i,t}}| / |{(i,t) : t ∈ expected_arguments}|``。

    粒度是 **(case, tool) 对**而非 case。因此这里用
    ``tool_argument_correct``（runner 已按对聚合）而不是逐 case 布尔。

    返回 ``None`` 表示"没有任何 case 声明了参数期望"。
    """
    eligible = [o for o in outcomes if o.tool_argument_correct is not None]
    matched = sum(1 for o in eligible if o.tool_argument_correct)
    return _round(_safe_ratio(matched, len(eligible)))


def tool_argument_eligible_count(outcomes: list[CaseOutcome]) -> int:
    """M4 的分母大小。"""
    return sum(1 for o in outcomes if o.tool_argument_correct is not None)


def evidence_coverage(outcomes: list[CaseOutcome]) -> float | None:
    """M5：``Σ min(1, |citations_i| / max(1, required_citations_i)) / N``。

    **分母始终是 N**，不是"有引用要求的 case 数" —— 与 M1/M2 一致。
    无引用要求的 case（``required_citations = 0``）其
    ``max(1, 0) = 1``，因此只要给出 1 条引用即满分，给出 0 条得 0 分。
    这个设计让"该不该引用"由 data 决定，而不是靠指标隐式跳过。

    逐 case 的 ``min(1, ...)`` 已由 ``runner`` 算好存在
    ``outcome.evidence_coverage``；此处只做平均。
    缺失值（``None``）按 0 计 —— 缺数据与"零引用"在
    "证据覆盖率"这个口径下后果相同，都表示该 case 没提供证据。
    """
    if not outcomes:
        return None
    total = sum(float(outcome.evidence_coverage or 0.0) for outcome in outcomes)
    return _round(_safe_ratio(total, len(outcomes)))


def coverage_for_case(citations: int, required_citations: int) -> float:
    """单 case 覆盖率：``min(1.0, citations / max(1, required))``。

    **上限截断为 1.0**：多引用不加分。否则堆砌引用可以把这一项刷到
    远超满分，让"证据覆盖率"变成"引用数量"的同义词。
    """
    denominator = max(1, required_citations)
    return min(1.0, citations / denominator)


def latency_summary(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    """M6：延迟分位数与均值。

    ``latency_ms`` 为 ``None`` 的 case **不进入样本** —— 它表示
    "这次运行没有耗时记录"（例如 run 建行即失败），
    把它当 0 会显著拉低延迟，制造"性能很好"的假象。

    额外返回 ``latency_sample_size`` 与 ``latency_p95_small_sample``：
    契约 M6 边界要求 ``N = 1`` 时必须**标注样本过小**。
    只给一个数字而不给样本量，读者会把 1 个样本的 p95 当作可信分位数。
    """
    latencies = sorted(
        int(outcome.latency_ms) for outcome in outcomes if outcome.latency_ms is not None
    )
    sample_size = len(latencies)

    return {
        "latency_ms_p50": percentile(latencies, 0.50),
        "latency_ms_p95": percentile(latencies, 0.95),
        "latency_ms_mean": (_round(sum(latencies) / sample_size, 2) if sample_size else None),
        "latency_sample_size": sample_size,
        # N < 5 时分位数几乎没有统计意义。标注而不是拒绝计算 ——
        # 不计算会让小规模评测完全失去延迟数据。
        "latency_p95_small_sample": sample_size < 5,
    }


def token_summary(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    """M7：Token 总量。仅算术求和，不做归一化（EVALUATION §3 M7）。

    同时返回 ``case_count``：契约要求"解读时必须同时看 case_count"，
    否则 12 个 case 的总量会被误读成"单次调用的量"。
    """
    return {
        "total_tokens": sum(int(outcome.total_tokens or 0) for outcome in outcomes),
        "prompt_tokens": None,  # 预留：当前不在 case 级持久化
        "completion_tokens": None,
        "case_count": len(outcomes),
    }


def cost_summary(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    """M8：估算成本合计。

    刻意不返回"是否可估算"标志位 —— 那是 ``ModelCall`` 级的属性，
    在 case 级已被 ``RunService`` 聚合进 ``run.cost_estimation_unavailable``。
    评测层面报出 ``estimated_cost_usd`` 的同时，``runner``
    会把"是否存在不可估算的调用"写进评测结果说明里。
    """
    return {
        "estimated_cost_usd": _round(
            sum(float(outcome.estimated_cost_usd or 0.0) for outcome in outcomes), 6
        )
    }


def error_rate(outcomes: list[CaseOutcome]) -> float | None:
    """M9：``|{status ∈ {failed, timeout}}| / N``。

    关系式：``error_rate = 1 - run_success_rate - degraded_rate``。
    这里的 ``timeout`` 计入错误 —— 与运行级 ``/metrics/summary``
    的口径不同（那里超时不算错误）。差异是刻意的：
    运行级关心"服务是否健康"，评测级关心"这次评测有多少 case 没产出结论"。
    """
    failed = sum(1 for outcome in outcomes if outcome.is_error)
    return _round(_safe_ratio(failed, len(outcomes)))


def degraded_rate(outcomes: list[CaseOutcome]) -> float | None:
    """降级率。EVALUATION §3 M9 要求单独统计并报告。

    ``degraded`` 既不算成功也不算错误，若没有这个指标，
    `1 - run_success_rate - error_rate` 这个差值就没有解释。
    """
    degraded = sum(1 for outcome in outcomes if outcome.is_degraded)
    return _round(_safe_ratio(degraded, len(outcomes)))


def human_review_rate(outcomes: list[CaseOutcome]) -> float | None:
    """M10：``|{run 中任一节点状态 == handoff}| / N``。

    本项目不实现人工接管 UI，``handoff`` 仅表示"自动流程无法继续"。
    """
    flagged = sum(1 for outcome in outcomes if outcome.needs_human_review)
    return _round(_safe_ratio(flagged, len(outcomes)))


# ---------------------------------------------------------------------------
# 聚合入口
# ---------------------------------------------------------------------------


def compute_metrics(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    """计算一份 case 结果列表的全部指标。

    Returns:
        契约 API_CONTRACT §6 的 ``metrics`` 对象形状。空列表时
        所有比率/分位数为 ``None``（不是 0），``total_tokens`` 为 0。

    注意 ``total_tokens = 0`` 在空数据下**保留 0 而不是 None**：
    计数型指标的"0"是真实值（确实一个 token 都没花），
    与比率型的"分母为 0 无从计算"是两回事。
    """
    latency = latency_summary(outcomes)
    tokens = token_summary(outcomes)
    cost = cost_summary(outcomes)

    return {
        "run_success_rate": run_success_rate(outcomes),
        "task_completion_rate": task_completion_rate(outcomes),
        "tool_selection_accuracy": tool_selection_accuracy(outcomes),
        "tool_selection_eligible_count": tool_selection_eligible_count(outcomes),
        "tool_argument_accuracy": tool_argument_accuracy(outcomes),
        "tool_argument_eligible_count": tool_argument_eligible_count(outcomes),
        "evidence_coverage": evidence_coverage(outcomes),
        "latency_ms_p50": latency["latency_ms_p50"],
        "latency_ms_p95": latency["latency_ms_p95"],
        "latency_ms_mean": latency["latency_ms_mean"],
        "latency_sample_size": latency["latency_sample_size"],
        "latency_p95_small_sample": latency["latency_p95_small_sample"],
        "total_tokens": tokens["total_tokens"],
        "estimated_cost_usd": cost["estimated_cost_usd"],
        "error_rate": error_rate(outcomes),
        "degraded_rate": degraded_rate(outcomes),
        "human_review_rate": human_review_rate(outcomes),
        "case_count": len(outcomes),
    }


# 供门禁模块引用的指标名清单（键名必须与 compute_metrics 一致）。
#
# **这份清单必须与 ``docs/contract.lock.json`` 的 ``metrics`` 数组逐字一致**
# （由 ``scripts/verify_contract.py`` 的 C-07 自动校验）。
# 它对应 EVALUATION §2 的指标总览表 M1~M10 —— 注意那张表里 M6 占一行
# 但展开成 p50/p95/mean 三个键，M7/M8 各占一行。
METRIC_NAMES: tuple[str, ...] = (
    "run_success_rate",
    "task_completion_rate",
    "tool_selection_accuracy",
    "tool_argument_accuracy",
    "evidence_coverage",
    "latency_ms_p50",
    "latency_ms_p95",
    "latency_ms_mean",
    "total_tokens",
    "estimated_cost_usd",
    "error_rate",
    "human_review_rate",
)

# 补充指标：不在 EVALUATION §2 的总览表里，但 §3 正文明确要求报告。
#
# ``degraded_rate`` 就在此处：M9 的正文写着
# "``degraded`` 不计入 error，单独统计并报告 degraded_rate"，
# 而总览表里没有它（它没有自己的 M 编号）。
# 因此它**可以**出现在 compute_metrics 的输出里，但不属于契约锁定的
# 12 个指标名 —— 把它塞进 METRIC_NAMES 会让 C-07 报"多余"。
#
# 门禁对补充指标同样生效（RATIO_METRICS 含它），
# 即"能设阈值"与"是否在契约锁定清单里"是两件事。
SUPPLEMENTARY_METRIC_NAMES: tuple[str, ...] = ("degraded_rate",)

# 比率型指标：越大越好，门禁用 ``min``。
RATIO_METRICS: frozenset[str] = frozenset(
    {
        "run_success_rate",
        "task_completion_rate",
        "tool_selection_accuracy",
        "tool_argument_accuracy",
        "evidence_coverage",
        "degraded_rate",
    }
)

# 代价型指标：越小越好，门禁用 ``max``。
COST_METRICS: frozenset[str] = frozenset(
    {
        "latency_ms_p50",
        "latency_ms_p95",
        "latency_ms_mean",
        "total_tokens",
        "estimated_cost_usd",
        "error_rate",
        "human_review_rate",
    }
)


__all__ = [
    "COST_METRICS",
    "DEGRADED_STATUSES",
    "ERROR_STATUSES",
    "METRIC_NAMES",
    "RATIO_METRICS",
    "SUCCESS_STATUSES",
    "SUPPLEMENTARY_METRIC_NAMES",
    "CaseOutcome",
    "compute_metrics",
    "cost_summary",
    "coverage_for_case",
    "degraded_rate",
    "error_rate",
    "evidence_coverage",
    "human_review_rate",
    "latency_summary",
    "percentile",
    "run_success_rate",
    "task_completion_rate",
    "token_summary",
    "tool_argument_accuracy",
    "tool_argument_eligible_count",
    "tool_selection_accuracy",
    "tool_selection_eligible_count",
]
