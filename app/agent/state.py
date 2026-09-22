"""LangGraph 状态定义。

契约 IMPLEMENTATION_PLAN S4：状态用 ``TypedDict`` 而非 Pydantic 模型。

理由（写在 doc-005 里，这里是实现侧的对应说明）：

1. LangGraph 的 reducer 机制依赖 TypedDict 的注解形式
   （例如 ``Annotated[list, operator.add]``）；
2. 状态在节点间流动频繁，逐次 Pydantic 校验的收益很低 ——
   真正需要严格把关的是**工具参数**与**API 边界**，
   那两处已经用 Pydantic 模型守住了。

因此本模块只有类型注解，没有运行时校验。节点函数的正确性
由单元测试保证，而不是由类型系统在运行时兜底。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from app.tools.base import Document, DocumentHit

# ---------------------------------------------------------------------------
# 常量：节点名与状态键
#
# 这些常量是**契约冻结项**（PROJECT_SPEC §3.1）。contract.lock.json 与
# scripts/verify_contract.py 会校验它们与文档一致，因此不要在别处
# 重复定义字符串字面量。
# ---------------------------------------------------------------------------

NODE_QUESTION_PARSER = "question_parser"
NODE_DOCUMENT_SEARCH = "document_search"
NODE_EVIDENCE_CHECKER = "evidence_checker"
NODE_ANSWER_WRITER = "answer_writer"
NODE_FINAL_VALIDATOR = "final_validator"

NODE_ORDER: tuple[str, ...] = (
    NODE_QUESTION_PARSER,
    NODE_DOCUMENT_SEARCH,
    NODE_EVIDENCE_CHECKER,
    NODE_ANSWER_WRITER,
    NODE_FINAL_VALIDATOR,
)

# 条件边：证据不足且仍有重试额度时，回到 document_search 放宽检索
CONDITIONAL_EDGE_FROM = NODE_EVIDENCE_CHECKER
CONDITIONAL_EDGE_TO = NODE_DOCUMENT_SEARCH


class ParsedTask(TypedDict, total=False):
    """``question_parser`` 的输出。

    ``keywords`` 是检索用的关键词列表；``intent`` 是粗分类
    （``概念解释`` / ``参数确认`` / ``决策理由`` / ``其他``），
    目前只用于生成答案的组织方式，不参与打分。
    """

    question: str
    keywords: list[str]
    intent: str
    max_chars: int


class TraceEventRef(TypedDict, total=False):
    """节点内产生的 Trace 事件引用。

    节点不直接写库，而是把"我产生了哪些事件"记在状态里，
    由 ``TraceRecorder`` 统一落库。这样节点逻辑可以脱离数据库单测。
    """

    event_type: str
    name: str
    status: str


class AgentState(TypedDict, total=False):
    """LangGraph 的完整状态。

    字段按节点读写顺序排列，注释标注由哪个节点写入。
    ``total=False`` 让节点可以只更新自己负责的字段。
    """

    # ---------------------------------------------------------- 输入
    question: str
    run_id: str
    top_k: int
    evidence_coverage_threshold: float

    # ---------------------------------------------------------- question_parser
    parsed_task: ParsedTask

    # ---------------------------------------------------------- document_search
    search_results: list[DocumentHit]
    # 放宽检索的累计次数。0 表示首次检索；
    # 达到 max_evidence_retries 后不再放宽。
    search_attempt: int
    fetched_documents: list[Document]

    # ---------------------------------------------------------- evidence_checker
    evidence_sufficient: bool
    evidence_coverage: float
    evidence_refs: list[str]

    # ---------------------------------------------------------- answer_writer
    answer: str
    answer_chars: int
    citations: list[str]
    # 是否走过降级路径（证据不足但流程继续）
    degraded: bool

    # ---------------------------------------------------------- final_validator
    final_result: dict[str, Any]
    validation_errors: list[str]

    # ---------------------------------------------------------- 横切
    # 事件累加器：用 operator.add 让多个节点可以各自 append，
    # 而不是互相覆盖（这是 LangGraph 的 reducer 约定）
    errors: Annotated[list[str], operator.add]
    # 节点访问轨迹，便于测试断言"哪些节点被执行了、按什么顺序"
    visited_nodes: Annotated[list[str], operator.add]


__all__ = [
    "CONDITIONAL_EDGE_FROM",
    "CONDITIONAL_EDGE_TO",
    "NODE_ANSWER_WRITER",
    "NODE_DOCUMENT_SEARCH",
    "NODE_EVIDENCE_CHECKER",
    "NODE_FINAL_VALIDATOR",
    "NODE_ORDER",
    "NODE_QUESTION_PARSER",
    "AgentState",
    "ParsedTask",
    "TraceEventRef",
]
