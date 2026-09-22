"""样例数据播种脚本。

用法::

    python scripts/seed_data.py              # 幂等播种（已存在的记录跳过）
    python scripts/seed_data.py --force      # 先清理本脚本写入的记录再重播
    python scripts/seed_data.py --check      # 只报告当前播种状态，不写库
    python scripts/seed_data.py --no-demo-run
                                             # 只播 agent_definition，不造演示 run

本脚本写入三类数据（契约 PROJECT_SPEC §4 / DATA_MODEL §2）：

1. ``agent_definition``：示例 Agent（``doc-research``）的版本定义，
   含 5 个节点与条件边，供评测按版本维度分组；
2. ``eval_case``：调用 ``app.evaluation.dataset`` 读取
   ``data/eval/doc_research_v1.jsonl``，若该模块与数据集尚未实现（S4/S6 之前），
   则跳过并提示，**不**伪造数据；
3. 演示 run：一条 **显式标注为测试替身** 的完整 Trace，让 ``GET /runs`` 与
   ``GET /metrics/summary`` 在空库时也有可展示的样例。

设计原则（契约 B4/B5/B9 与 ACCEPTANCE_CHECKLIST K 组）
------------------------------------------------------
- **不编造数据**：演示 run 的 ``is_test_double=True``，``llm_provider="fake"``，
  ``result_summary.data_source_note`` 写明"合成演示数据"。
  它**不能**用来说明模型能力；
- **幂等**：重复执行不产生重复记录。以确定性 ID（由固定种子派生）作为主键，
  已存在则跳过；
- **不依赖 S4 之后才存在的模块**：用 ``try/except ImportError`` 探测，
  缺失时明确提示而非崩溃；
- **不写密钥**：所有摘要经 ``app.core.redaction.summarize`` 处理。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete, select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.core.redaction import summarize  # noqa: E402
from app.db.base import utcnow  # noqa: E402
from app.db.models import AgentDefinition, Run  # noqa: E402
from app.db.session import (  # noqa: E402
    check_database_health,
    dispose_engine,
    session_scope,
)

logger = get_logger("scripts.seed_data")

# ---------------------------------------------------------------------------
# 确定性 ID
#
# 播种必须幂等，因此不能用 new_run_id() 这类随机 ULID——那会每次产生新行。
# 这里用固定的 ULID 字面量：时间戳部分固定，后 80 位固定，
# 仍然满足 DATA_MODEL §4 的格式（前缀 + 26 位 Crockford Base32）。
# 前缀统一以 0 开头便于一眼识别为播种数据（ULID 合法字符集含 0-9）。
# ---------------------------------------------------------------------------

_SEED_AGENT_DEFINITION_ID = "agentdef_00000000000000000000000001"
_SEED_RUN_ID = "run_00000000000000000000000001"

# 示例 Agent 的名字与图定义，必须与 docs/contract.lock.json 的
# ``agent_definition`` 段保持一致（S8 由 verify_contract.py 校验）。
_AGENT_NAME = "doc-research"
_NODE_ORDER = (
    "question_parser",
    "document_search",
    "evidence_checker",
    "answer_writer",
    "final_validator",
)


def _graph_definition() -> dict[str, object]:
    """构造与文档一致的图定义快照。

    存进 ``agent_definition.graph_definition`` 的是"那一刻的真实图结构"，
    这样评测结果才能回溯到具体的工作流版本（DATA_MODEL §2.1）。
    """
    return {
        "nodes": list(_NODE_ORDER),
        "node_order_fixed": True,
        "edges": [
            {"from": _NODE_ORDER[i], "to": _NODE_ORDER[i + 1]} for i in range(len(_NODE_ORDER) - 1)
        ],
        "conditional_edges": [
            {
                "from": "evidence_checker",
                "to": "document_search",
                "condition": "evidence_insufficient_and_retry_available",
            }
        ],
        "entry_point": _NODE_ORDER[0],
        "finish_point": _NODE_ORDER[-1],
        "max_evidence_retries": 1,
    }


# ---------------------------------------------------------------------------
# 1. agent_definition
# ---------------------------------------------------------------------------


def seed_agent_definition(*, force: bool = False) -> tuple[bool, str]:
    """播种示例 Agent 的版本定义。

    Returns:
        ``(是否写入, 说明)``
    """
    settings = get_settings()

    with session_scope() as session:
        existing = session.get(AgentDefinition, _SEED_AGENT_DEFINITION_ID)

        if existing is not None:
            # 图定义可能因文档更新而变化。--force 时重建，否则保持原样并提示差异。
            desired = _graph_definition()
            if existing.graph_definition != desired:
                if not force:
                    return (
                        False,
                        "已存在但 graph_definition 与当前文档不一致（用 --force 重建）",
                    )
                existing.graph_definition = desired
                existing.name = _AGENT_NAME
                existing.agent_version = settings.agent_version
                existing.prompt_version = settings.prompt_version
                existing.description = _AGENT_DESCRIPTION
                return True, "已更新（graph_definition 与文档对齐）"
            return False, "已存在，跳过"

        session.add(
            AgentDefinition(
                id=_SEED_AGENT_DEFINITION_ID,
                name=_AGENT_NAME,
                agent_version=settings.agent_version,
                prompt_version=settings.prompt_version,
                graph_definition=_graph_definition(),
                description=_AGENT_DESCRIPTION,
            )
        )
        return True, "已创建"


_AGENT_DESCRIPTION = (
    "技术文档研究 Agent：解析问题 → 检索项目内样例文档 → 证据充分性检查 → "
    "生成带引用的结构化答案 → 引用与字段校验。"
    "证据不足时最多进行一次放宽检索，重试耗尽则标记 degraded（不伪装为成功）。"
)


# ---------------------------------------------------------------------------
# 2. 演示 run（显式标注为测试替身）
# ---------------------------------------------------------------------------

# 演示用的 5 个节点的耗时（毫秒）。刻意让 document_search 最慢，
# 这样 /metrics/summary 的 p95 与均值有可观察差异。
_DEMO_NODE_TIMINGS: tuple[tuple[str, int], ...] = (
    ("question_parser", 12),
    ("document_search", 46),
    ("evidence_checker", 18),
    ("answer_writer", 31),
    ("final_validator", 9),
)

_DEMO_QUESTION = "AgentTrace 的 trace_event 表中 sequence 字段解决了什么问题？"

_DEMO_ANSWER = (
    "sequence 是同一 run 内单调递增的整数，用于保证回放顺序稳定。"
    "原因是：时间戳只有毫秒精度，同一毫秒内产生的多个事件若仅按 started_at 排序，"
    "顺序可能抖动；UNIQUE(run_id, sequence) 约束则让每一行的相对次序可确定地重建。"
    "依据：[doc-002] AgentTrace Trace 事件模型。"
)


def seed_demo_run(*, force: bool = False) -> tuple[bool, str]:
    """播种一条完整的演示 run 及其 Trace。

    这条记录的价值是让空库状态下 ``GET /runs``、``GET /runs/{id}/events``、
    ``GET /metrics/summary`` 有可展示内容，便于第一次跑起来时确认链路通畅。

    **必须显式标注为测试替身**：``is_test_double=True``、``llm_provider="fake"``，
    且 ``result_summary`` 内含 ``data_source_note``。任何展示该 run 的地方
    都必须能看出它不是真实模型调用。

    本函数会**先确保 agent_definition 存在**再写入 run：
    run 的 ``agent_definition_id`` 是外键，若调用方忘记先播 agent，
    数据库会直接拒绝插入（FOREIGN KEY constraint failed）。
    与其让调用方记住调用顺序，不如在这里自给自足。
    """
    # 先保证外键目标存在，避免调用顺序依赖
    seed_agent_definition()

    settings = get_settings()
    started_at = _demo_timeline_start()
    total_ms = sum(ms for _, ms in _DEMO_NODE_TIMINGS)
    ended_at = started_at + timedelta(milliseconds=total_ms)

    with session_scope() as session:
        existing = session.get(Run, _SEED_RUN_ID)
        if existing is not None:
            if not force:
                return False, "已存在，跳过"
            # run 的事件/调用记录靠 CASCADE 一并删除（SQLite 下需要 PRAGMA 生效，
            # 见 db/session.py 的说明）
            session.delete(existing)
            session.flush()

        run = Run(
            id=_SEED_RUN_ID,
            agent_definition_id=_SEED_AGENT_DEFINITION_ID,
            source_run_id=None,
            question=_DEMO_QUESTION,
            status="succeeded",
            agent_version=settings.agent_version,
            prompt_version=settings.prompt_version,
            model_name=settings.llm_model if not settings.is_test_double_mode else "fake-echo-1",
            llm_provider=settings.llm_provider,
            # 关键：明确标注为替身，避免被误读为真实模型表现
            is_test_double=True,
            result_summary={
                "answer_preview": summarize(_DEMO_ANSWER, 300),
                "citation_count": 1,
                "evidence_coverage": 0.83,
                "degraded": False,
                "data_source_note": (
                    "合成演示数据：由 scripts/seed_data.py 写入，使用本地测试替身，"
                    "不涉及任何真实模型调用，不代表任何线上表现。"
                ),
            },
            started_at=started_at,
            ended_at=ended_at,
            total_duration_ms=total_ms,
            total_tokens=0,
            estimated_cost_usd=Decimal("0"),
        )
        session.add(run)
        session.flush()
        run_id = run.id

    # 事件与调用记录交给仓储层写入，保证 sequence 分配与脱敏逻辑只有一份实现。
    # 这里在独立事务中做，因为 run 行需要先可见（外键约束）。
    _write_demo_trace(run_id, started_at)

    return True, f"已创建（run_id={run_id}）"


def _node_window(node_name: str) -> tuple[datetime, int]:
    """返回某个节点在演示时间线上的 ``(起始时刻, 时长毫秒)``。

    节点的起止时刻由 ``_DEMO_NODE_TIMINGS`` 顺序累加得出（第 n 个节点的起点
    等于前 n-1 个节点时长之和），因此子调用（工具/模型）可以落在父节点的时间窗内，
    整个 Trace 的时间线是自洽的、可被人工核对。

    Returns:
        ``(节点起始时刻, 节点时长毫秒)``
    """
    elapsed_ms = 0
    for name, duration_ms in _DEMO_NODE_TIMINGS:
        if name == node_name:
            return _demo_timeline_start() + timedelta(milliseconds=elapsed_ms), duration_ms
        elapsed_ms += duration_ms
    raise KeyError(f"未知节点：{node_name}")


def _demo_timeline_start() -> datetime:
    """演示时间线的统一基准点。

    刻意把微秒截掉（``replace(microsecond=0)``）：整条时间线由"基准点 + N 毫秒"
    累加而成，若基准点带微秒，加毫秒后会出现微秒级余数，
    导致最末端的事件比 ``run.ended_at`` 晚几微秒——看起来像"事件越出 run 时间窗"。
    对齐到整毫秒后，时间窗比较就是精确的。
    """
    return (utcnow() - timedelta(seconds=2)).replace(microsecond=0)


def _write_demo_trace(run_id: str, started_at: datetime) -> None:
    """用仓储层写入演示 Trace。

    刻意复用 ``TraceRepository`` 而不是直接构造 ORM 对象：
    这样播种数据与真实运行产生的数据结构**完全一致**，
    也顺带覆盖了 sequence 分配、脱敏、计时等路径。

    树的形状（与真实 run 一致）::

        run
        ├── node: question_parser
        ├── node: document_search
        │   └── tool_call: search_documents
        ├── node: evidence_checker
        ├── node: answer_writer
        │   └── model_call: answer_writer
        ├── node: final_validator
        └── final_result

    ``?event_type=node`` 与 ``?event_type=tool_call`` 的过滤因此都能查到内容，
    便于第一次跑通时验证过滤逻辑。
    """
    from app.db.repository import TraceRepository

    with session_scope() as session:
        repo = TraceRepository(session)

        run_event = repo.append_event(
            run_id=run_id,
            event_type="run",
            name="doc-research",
            status="ok",
            input_summary={"question": _DEMO_QUESTION},
            attributes={"agent_version": get_settings().agent_version},
            started_at=started_at,
        )
        repo.close_event(
            run_event.event_id, status="ok", ended_at=started_at + timedelta(milliseconds=2)
        )

        # 节点一律挂在 run 事件下（扁平一层），工具/模型调用挂在所属节点下。
        # 这比"节点链式父子"更贴近真实拓扑：节点是并列的步骤，不是嵌套的。
        #
        # 注意：close_event 会用 ``ended_at - started_at`` **重算** duration_ms，
        # 因此不能"直接传 duration_ms"——那会被随后的 close 覆盖成真实经过时间
        # （在本机上是 0ms，看起来像没记录耗时）。这里显式给出 started_at/ended_at，
        # 让仓储层算出与 _DEMO_NODE_TIMINGS 一致的时长。
        node_event_ids: dict[str, str] = {}
        cursor = started_at
        for node_name, duration_ms in _DEMO_NODE_TIMINGS:
            node_start = cursor
            node_end = node_start + timedelta(milliseconds=duration_ms)
            cursor = node_end

            event = repo.append_event(
                run_id=run_id,
                event_type="node",
                name=node_name,
                status="ok",
                parent_event_id=run_event.event_id,
                input_summary={"node": node_name},
                started_at=node_start,
            )
            repo.close_event(
                event.event_id,
                status="ok",
                output_summary={"completed": node_name, "duration_ms": duration_ms},
                ended_at=node_end,
            )
            node_event_ids[node_name] = event.event_id

        # 一次工具调用：search_documents（挂在 document_search 节点下）
        # 子调用的时间窗落在父节点的时间窗内，保证 Trace 时序自洽。
        tool_start, tool_duration = _node_window("document_search")
        tool_event = repo.append_event(
            run_id=run_id,
            event_type="tool_call",
            name="search_documents",
            status="ok",
            parent_event_id=node_event_ids["document_search"],
            input_summary={"query": "sequence 回放顺序", "top_k": 3},
            started_at=tool_start,
        )
        repo.close_event(
            tool_event.event_id,
            status="ok",
            output_summary={"hits": 3, "top_document_id": "doc-002"},
            ended_at=tool_start + timedelta(milliseconds=tool_duration),
        )
        repo.record_tool_call(
            run_id=run_id,
            event_id=tool_event.event_id,
            node_name="document_search",
            tool_name="search_documents",
            arguments={"query": "sequence 回放顺序", "top_k": 3},
            validated=True,
            status="ok",
            result_summary="3 hits, top=doc-002",
            result_count=3,
            duration_ms=tool_duration,
            started_at=tool_start,
            ended_at=tool_start + timedelta(milliseconds=tool_duration),
        )

        # 一次模型调用（替身）。Token 与成本如实记为 0 并标注"不可估算"，
        # 而不是编一个看起来合理的数字——那会污染成本指标。
        model_start, model_duration = _node_window("answer_writer")
        model_event = repo.append_event(
            run_id=run_id,
            event_type="model_call",
            name="answer_writer",
            status="ok",
            parent_event_id=node_event_ids["answer_writer"],
            input_summary={"node": "answer_writer"},
            started_at=model_start,
        )
        repo.close_event(
            model_event.event_id,
            status="ok",
            output_summary={"chars": len(_DEMO_ANSWER)},
            ended_at=model_start + timedelta(milliseconds=model_duration),
        )
        repo.record_model_call(
            run_id=run_id,
            event_id=model_event.event_id,
            node_name="answer_writer",
            provider="fake",
            model_name="fake-echo-1",
            is_test_double=True,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            estimated_cost_usd=Decimal("0"),
            # 替身没有真实价格表，成本"不可估算"比编一个数字更诚实
            cost_estimation_unavailable=True,
            status="ok",
            latency_ms=model_duration,
        )

        # 终态事件（时间点落在 run 的结束时刻，即时间线末端）
        final_start, final_duration = _node_window("final_validator")
        final_event = repo.append_event(
            run_id=run_id,
            event_type="final_result",
            name="final_result",
            status="ok",
            parent_event_id=run_event.event_id,
            input_summary={"validator": "final_validator"},
            started_at=final_start,
        )
        repo.close_event(
            final_event.event_id,
            status="ok",
            output_summary={"answer": _DEMO_ANSWER, "citations": 1},
            ended_at=final_start + timedelta(milliseconds=final_duration),
        )


# ---------------------------------------------------------------------------
# 3. eval_case（依赖 S6 的 dataset 模块，缺失时跳过）
# ---------------------------------------------------------------------------


def seed_eval_cases(*, force: bool = False) -> tuple[int, str]:
    """从评测集 JSONL 载入 eval_case。

    评测集加载与解析复用 ``app.evaluation.dataset``（S6 的实现），
    **不再自己解析 JSONL** —— 两份解析逻辑迟早会分叉，
    而分叉时"播种进去的 case"与"评测时读到的 case"会悄悄变成两批数据。

    Args:
        force: 为 True 时先删掉同名 case 再重建。

    Returns:
        ``(写入条数, 说明文本)``。
    """
    try:
        from app.evaluation.dataset import load_dataset  # type: ignore[import-not-found]
    except ImportError:
        return 0, "app.evaluation.dataset 尚未实现，跳过"

    dataset_version = "doc_research_v1"

    try:
        # 路径解析交给 ``load_dataset``（它读 ``EVAL_DATASET_DIR`` 并挡路径穿越）。
        # 这里不再自己拼路径 —— 本脚本曾经的那份拼接逻辑与
        # ``dataset_path`` 是两套实现，其中一套改了就立刻分叉。
        dataset = load_dataset(dataset_version)
    except Exception as exc:  # noqa: BLE001 —— 播种失败不应阻断服务启动
        return 0, f"加载评测集失败：{type(exc).__name__}: {exc}"

    from app.db.models import EvalCase

    written = 0
    with session_scope() as session:
        # ``EvalDataset`` 实现了 ``__iter__``，因此 ``for case in dataset``
        # 直接拿到 ``EvalCaseSpec``。这里**不**去碰 ``dataset.cases`` ——
        # 少一处对内部字段名的依赖，重构时少一处会静默断掉的地方。
        for case in dataset:
            case_key = case.case_key

            stmt = select(EvalCase).where(EvalCase.case_key == case_key)
            existing = session.execute(stmt).scalar_one_or_none()

            if existing is not None:
                if not force:
                    continue
                session.delete(existing)
                session.flush()

            from app.core.ids import new_eval_case_id

            session.add(
                EvalCase(
                    id=new_eval_case_id(),
                    case_key=case_key,
                    dataset_version=dataset_version,
                    question=case.question,
                    expected_tools=list(case.expected_tools),
                    expected_arguments=case.expected_arguments,
                    required_assertions=list(case.required_assertions),
                    required_citations=case.required_citations,
                    expect_success=case.expect_success,
                    tags=list(case.tags),
                )
            )
            written += 1

    return written, f"已载入 {written} 个 case（共 {len(dataset)} 个）"


# ---------------------------------------------------------------------------
# 状态检查
# ---------------------------------------------------------------------------


def report_status() -> dict[str, object]:
    """报告当前播种状态，不写库。供 ``--check`` 与容器启动日志使用。"""
    from app.db.models import EvalCase

    with session_scope() as session:
        agent = session.get(AgentDefinition, _SEED_AGENT_DEFINITION_ID)
        run = session.get(Run, _SEED_RUN_ID)
        case_count = len(session.execute(select(EvalCase)).scalars().all())

    return {
        "agent_definition": "present" if agent is not None else "missing",
        "demo_run": "present" if run is not None else "missing",
        "eval_case_count": case_count,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="播种 AgentTrace 样例数据")
    parser.add_argument(
        "--force", action="store_true", help="重建已存在的播种记录（只影响本脚本写入的行）"
    )
    parser.add_argument("--check", action="store_true", help="只报告播种状态，不写库")
    parser.add_argument(
        "--no-demo-run", action="store_true", help="不创建演示 run（只播 agent_definition）"
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level, force=True)

    print("=" * 74)
    print("AgentTrace 样例数据播种")
    print("=" * 74)
    print(f"方言     : {settings.database_dialect}")
    print(f"连接串   : {settings.safe_database_url()}")
    print(
        f"替换模式 : {'是（fake provider，不调用真实模型）' if settings.is_test_double_mode else '否'}"
    )
    print()

    healthy, error = check_database_health(settings)
    if not healthy:
        print("[失败] 无法连接数据库。")
        print(f"       错误: {error}")
        print()
        print("提示：先执行 python scripts/init_db.py 建表。")
        return 2

    if args.check:
        status = report_status()
        print("当前状态：")
        print(f"  agent_definition : {status['agent_definition']}")
        print(f"  demo_run         : {status['demo_run']}")
        print(f"  eval_case 数量   : {status['eval_case_count']}")
        dispose_engine()
        return 0

    # ---------------------------------------------------------- 1. agent_definition
    written, note = seed_agent_definition(force=args.force)
    print(f"[{'写入' if written else '跳过'}] agent_definition（{_AGENT_NAME}）：{note}")

    # ---------------------------------------------------------- 2. 演示 run
    if args.no_demo_run:
        print("[跳过] 演示 run（--no-demo-run）")
    else:
        written, note = seed_demo_run(force=args.force)
        print(f"[{'写入' if written else '跳过'}] 演示 run：{note}")

    # ---------------------------------------------------------- 3. eval_case
    count, note = seed_eval_cases(force=args.force)
    print(f"[{'写入' if count else '跳过'}] eval_case：{note}")

    print()
    status = report_status()
    print("播种后状态：")
    print(f"  agent_definition : {status['agent_definition']}")
    print(f"  demo_run         : {status['demo_run']}")
    print(f"  eval_case 数量   : {status['eval_case_count']}")
    print()

    # 显式提醒数据的真实性质，避免被误读（契约 B9 / K 组检查项）
    print("数据性质声明：")
    print("  演示 run 使用本地测试替身（is_test_double=true），")
    print("  其延迟/Token/成本数值不可用于说明任何模型的实际能力。")
    print("  评测集为作者自建的合成数据，不含任何真实用户或业务数据。")
    print()

    dispose_engine()
    return 0


def _cleanup_seed_rows() -> None:
    """删除本脚本写入的行。仅用于测试与本地重置，不在 CLI 中暴露。"""
    with session_scope() as session:
        session.execute(delete(Run).where(Run.id == _SEED_RUN_ID))
        session.execute(
            delete(AgentDefinition).where(AgentDefinition.id == _SEED_AGENT_DEFINITION_ID)
        )


__all__ = [
    "main",
    "report_status",
    "seed_agent_definition",
    "seed_demo_run",
    "seed_eval_cases",
]


if __name__ == "__main__":
    sys.exit(main())
