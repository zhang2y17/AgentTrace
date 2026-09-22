"""节点包：5 个节点的实现。

每个节点一个文件，导出一个与节点同名的函数。函数签名统一为::

    def node_name(state: AgentState, *, ...注入的依赖...) -> dict[str, Any]

返回的是**部分状态更新**（dict），而不是完整状态 ——
这与 LangGraph 的 reducer 约定一致，也让节点可以脱离图单独单测。
"""

from app.agent.nodes.answer_writer import write_answer
from app.agent.nodes.document_search import document_search
from app.agent.nodes.evidence_checker import check_evidence
from app.agent.nodes.final_validator import validate_final_answer
from app.agent.nodes.question_parser import parse_question

__all__ = [
    "check_evidence",
    "document_search",
    "parse_question",
    "validate_final_answer",
    "write_answer",
]
