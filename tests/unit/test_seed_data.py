"""播种脚本测试。

覆盖 ACCEPTANCE_CHECKLIST：
- I-10：``scripts/seed_data.py`` 存在且可执行
- K-02：指标/样例数据必须能说明来源（演示 run 的 ``data_source_note``）
- K-05：样例数据来源声明（合成数据、非真实用户数据）
- D-02：节点事件带耗时
- D-03：``parent_event_id`` 树形正确（无孤儿）
- D-04：``sequence`` 同 run 内唯一且递增
- D-05/D-06：工具调用与模型调用字段完整

标记：``unit``（SQLite 临时文件，无外部服务，无网络）

设计说明：本模块**不**通过子进程调用 CLI（那会引入进程与路径依赖），
而是直接导入 ``scripts.seed_data`` 的函数。CLI 参数解析由
``test_cli_argument_parsing`` 用 monkeypatch 覆盖。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import seed_data  # noqa: E402


@pytest.fixture
def seeded(db_session):
    """已播种的数据库会话。

    依赖 ``db_session`` 提供已建表的 SQLite。播种后返回会话，
    供测试用仓储层读取数据。
    """
    seed_data.seed_agent_definition()
    seed_data.seed_demo_run()
    return db_session


# ---------------------------------------------------------------------------
# 幂等性
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_seeding_is_idempotent(db_session) -> None:
    """重复播种不产生重复记录。

    这是 ``docker compose up`` 会重复执行播种脚本的前提
    （docker-compose.yml 的 api command 每次都跑 seed_data.py）。
    """
    from app.db.models import AgentDefinition, Run
    from app.db.repository import TraceRepository

    seed_data.seed_agent_definition()
    seed_data.seed_demo_run()

    # 再播一次，应全部跳过
    agent_written, agent_note = seed_data.seed_agent_definition()
    run_written, run_note = seed_data.seed_demo_run()

    assert agent_written is False, f"第二次不应写入：{agent_note}"
    assert run_written is False, f"第二次不应写入：{run_note}"

    session = db_session
    agents = session.query(AgentDefinition).all()  # noqa: SIM118
    runs = session.query(Run).all()  # noqa: SIM118
    assert len(agents) == 1
    assert len(runs) == 1

    # 事件数不应翻倍
    repo = TraceRepository(session)
    _events, total = repo.list_events(seed_data._SEED_RUN_ID)
    assert total == 9, f"事件数应为 9（不因重复播种翻倍），实际 {total}"


@pytest.mark.unit
def test_force_rebuild_replaces_run_without_duplicating(db_session) -> None:
    """``--force`` 重建 run 时不残留旧事件。

    这依赖 ``ON DELETE CASCADE`` 真的生效。SQLite 默认关闭外键，
    ``db/session.py`` 显式打开了 ``PRAGMA foreign_keys=ON``；
    本测试是该修复的回归保障——若 PRAGMA 失效，事件会残留，断言失败。
    """
    from app.db.models import Run, TraceEvent
    from app.db.repository import TraceRepository

    seed_data.seed_demo_run()
    repo = TraceRepository(db_session)
    _events, first_total = repo.list_events(seed_data._SEED_RUN_ID)
    assert first_total == 9

    written, _note = seed_data.seed_demo_run(force=True)
    assert written is True

    session = db_session
    session.expire_all()
    runs = session.query(Run).all()  # noqa: SIM118
    assert len(runs) == 1, "不应产生第二条 run"

    all_events = session.query(TraceEvent).all()  # noqa: SIM118
    assert len(all_events) == 9, f"旧事件应被级联删除，实际残留 {len(all_events)} 条"


# ---------------------------------------------------------------------------
# agent_definition 契约
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_definition_matches_contract_lock(seeded) -> None:
    """agent_definition 的节点与条件边必须与 contract.lock.json 一致。

    对应 ACCEPTANCE_CHECKLIST C-01（5 个节点名一致）。
    """
    from app.db.models import AgentDefinition

    agent = seeded.get(AgentDefinition, seed_data._SEED_AGENT_DEFINITION_ID)
    assert agent is not None
    assert agent.name == "doc-research"

    graph = agent.graph_definition
    assert graph["nodes"] == [
        "question_parser",
        "document_search",
        "evidence_checker",
        "answer_writer",
        "final_validator",
    ]
    assert graph["node_order_fixed"] is True
    assert graph["entry_point"] == "question_parser"
    assert graph["finish_point"] == "final_validator"
    assert graph["max_evidence_retries"] == 1

    # 条件边：证据不足回到 document_search
    assert len(graph["conditional_edges"]) == 1
    edge = graph["conditional_edges"][0]
    assert edge["from"] == "evidence_checker"
    assert edge["to"] == "document_search"

    # 固定顺序链：4 条顺序边
    assert len(graph["edges"]) == 4


# ---------------------------------------------------------------------------
# 演示 run 的替身标注（契约 B5 / K-02 / K-05）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_demo_run_is_marked_as_test_double(seeded) -> None:
    """演示 run 必须被明确标注为测试替身。

    这是契约 B5 与 ACCEPTANCE_CHECKLIST K-02/K-05 的直接体现：
    样例数据必须能被一眼识别为"合成数据"，不能被误读为真实模型表现。
    """
    from app.db.repository import TraceRepository

    repo = TraceRepository(seeded)
    run = repo.get_run(seed_data._SEED_RUN_ID)
    assert run is not None

    assert run.is_test_double is True
    assert run.llm_provider == "fake"

    summary = run.result_summary or {}
    assert "data_source_note" in summary, "必须说明数据来源"
    note = summary["data_source_note"]
    assert "合成" in note or "测试替身" in note


@pytest.mark.unit
def test_demo_run_model_call_is_test_double_and_cost_unavailable(seeded) -> None:
    """模型调用必须标注替身；成本记 0 且标记"不可估算"。

    刻意不让播种脚本编一个"看起来合理"的成本数字——
    那会污染 ``estimated_cost_usd`` 指标，使读者误以为有真实成本数据。
    """
    from app.db.repository import TraceRepository

    repo = TraceRepository(seeded)
    calls = repo.list_model_calls(seed_data._SEED_RUN_ID)
    assert len(calls) == 1

    call = calls[0]
    assert call.is_test_double is True
    assert call.cost_estimation_unavailable is True
    assert call.total_tokens == 0
    assert call.estimated_cost_usd == 0


# ---------------------------------------------------------------------------
# Trace 结构不变量（D-02 / D-03 / D-04 / D-05）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_demo_trace_has_all_six_event_type_coverage(seeded) -> None:
    """演示 run 覆盖除 error 外的全部事件类型，便于验证过滤逻辑。

    刻意不含 ``error``：演示 run 是成功路径。失败路径由测试构造，
    而不是预置在样例数据里（避免样例数据看起来像"跑挂过"）。
    """
    from app.db.repository import TraceRepository

    repo = TraceRepository(seeded)
    events, total = repo.list_events(seed_data._SEED_RUN_ID)
    assert total == 9

    types = {event.event_type for event in events}
    assert types == {"run", "node", "tool_call", "model_call", "final_result"}
    assert "error" not in types


@pytest.mark.unit
def test_demo_trace_sequence_is_dense_and_increasing(seeded) -> None:
    """``sequence`` 必须是从 1 开始的连续递增序列（ACCEPTANCE D-04）。"""
    from app.db.repository import TraceRepository

    repo = TraceRepository(seeded)
    events, total = repo.list_events(seed_data._SEED_RUN_ID)
    sequences = [event.sequence for event in events]

    assert sequences == list(range(1, total + 1)), f"sequence 不连续：{sequences}"


@pytest.mark.unit
def test_demo_trace_has_no_orphan_parents(seeded) -> None:
    """每个非根事件的父事件必须存在且属于同一 run（ACCEPTANCE D-03）。"""
    from app.db.repository import TraceRepository

    repo = TraceRepository(seeded)
    events, _total = repo.list_events(seed_data._SEED_RUN_ID)
    ids = {event.event_id for event in events}

    roots = [event for event in events if event.parent_event_id is None]
    assert len(roots) == 1, "应有且仅有一个根事件"

    for event in events:
        if event.parent_event_id is None:
            continue
        assert event.parent_event_id in ids, (
            f"事件 {event.event_id} 的父 {event.parent_event_id} 不在同一 run 内"
        )


@pytest.mark.unit
def test_demo_trace_node_events_have_duration(seeded) -> None:
    """节点事件必须带非空耗时（ACCEPTANCE D-02）。

    ``close_event`` 会用 ``ended_at - started_at`` 重算 ``duration_ms``，
    因此播种时必须显式给出起止时刻，否则耗时会退化成真实的 0ms——
    本测试是该实现的回归保障。
    """
    from app.db.repository import TraceRepository

    repo = TraceRepository(seeded)
    events, _total = repo.list_events(seed_data._SEED_RUN_ID, event_types=["node"])
    assert len(events) == 5

    for event in events:
        assert event.duration_ms is not None, f"节点 {event.name} 缺 duration_ms"
        assert event.duration_ms > 0, f"节点 {event.name} 耗时为 0，可能是未显式给起止时刻"
        assert event.ended_at is not None


@pytest.mark.unit
def test_demo_trace_events_fall_within_run_window(seeded) -> None:
    """所有事件的时间窗必须落在 run 的时间窗内。

    时间线由"基准点 + 累加毫秒"生成，基准点对齐到整毫秒，
    因此比较是精确的。若基准点带微秒，末端事件会溢出几微秒——
    这正是本测试要防的问题。
    """
    from app.db.repository import TraceRepository

    repo = TraceRepository(seeded)
    run = repo.get_run(seed_data._SEED_RUN_ID)
    events, _total = repo.list_events(seed_data._SEED_RUN_ID)

    for event in events:
        assert event.started_at >= run.started_at, f"{event.name} 早于 run 开始"
        if event.ended_at is not None:
            assert event.ended_at <= run.ended_at, f"{event.name} 晚于 run 结束"


@pytest.mark.unit
def test_demo_trace_child_calls_nest_in_parent_node_window(seeded) -> None:
    """工具/模型调用的时间窗必须完整落在其父节点的时间窗内。

    这是 Trace 可解释性的关键：如果子调用跑到父节点之外，
    回放时就会看到"工具在节点开始前就被调用了"这类自相矛盾的时序。
    """
    from app.db.repository import TraceRepository

    repo = TraceRepository(seeded)
    events, _total = repo.list_events(seed_data._SEED_RUN_ID)
    by_id = {event.event_id: event for event in events}

    children = [event for event in events if event.event_type in {"tool_call", "model_call"}]
    assert len(children) == 2, "演示 run 应有 1 次工具调用 + 1 次模型调用"

    for child in children:
        parent = by_id[child.parent_event_id]
        assert parent.event_type == "node"
        assert child.started_at >= parent.started_at, f"{child.name} 早于父节点开始"
        assert child.ended_at <= parent.ended_at, f"{child.name} 晚于父节点结束"


# ---------------------------------------------------------------------------
# 工具调用记录完整性（D-05）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_demo_tool_call_records_required_fields(seeded) -> None:
    """工具调用必须记录名称、已脱敏参数、校验状态与耗时（ACCEPTANCE D-05）。"""
    from app.db.repository import TraceRepository

    repo = TraceRepository(seeded)
    calls = repo.list_tool_calls(seed_data._SEED_RUN_ID)
    assert len(calls) == 1

    call = calls[0]
    assert call.tool_name == "search_documents"
    assert call.node_name == "document_search"
    assert call.validated is True
    assert call.status == "ok"
    assert call.duration_ms == 46
    assert call.result_count == 3
    assert call.arguments == {"query": "sequence 回放顺序", "top_k": 3}


# ---------------------------------------------------------------------------
# eval_case 的探测式跳过（S4/S6 之前不应失败）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_case_seeding_skips_gracefully_before_s6(db_session, monkeypatch) -> None:
    """``app.evaluation.dataset`` 尚未实现时，``seed_eval_cases`` 应跳过而非崩溃。

    这让 ``docker compose up`` 在任何开发阶段都不会因为播种脚本而失败。
    一旦 S6 实现了 dataset 模块，本测试的前提会变化（届时返回 >0），
    因此这里显式断言"返回 0 且给出原因字符串"，而不假设具体原因文案。
    """
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "app.evaluation.dataset":
            raise ImportError("模拟 S6 尚未实现")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    count, note = seed_data.seed_eval_cases()
    assert count == 0
    assert isinstance(note, str) and note


@pytest.mark.unit
def test_report_status_reports_missing_on_empty_db(db_session) -> None:
    """空库时状态报告应全部为 missing/0，不抛异常。"""
    status = seed_data.report_status()
    assert status["agent_definition"] == "missing"
    assert status["demo_run"] == "missing"
    assert status["eval_case_count"] == 0


@pytest.mark.unit
def test_report_status_reports_present_after_seeding(seeded) -> None:
    """播种后状态报告应为 present。"""
    status = seed_data.report_status()
    assert status["agent_definition"] == "present"
    assert status["demo_run"] == "present"


# ---------------------------------------------------------------------------
# CLI 参数解析
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_arguments_are_parsed(monkeypatch, db_session) -> None:
    """``--check`` 走只读路径且退出码为 0。"""
    monkeypatch.setattr(sys, "argv", ["seed_data.py", "--check"])
    exit_code = seed_data.main()
    assert exit_code == 0


@pytest.mark.unit
def test_cli_no_demo_run_skips_run(monkeypatch, db_session) -> None:
    """``--no-demo-run`` 只播 agent_definition，不造 run。"""
    from app.db.models import Run

    monkeypatch.setattr(sys, "argv", ["seed_data.py", "--no-demo-run"])
    exit_code = seed_data.main()
    assert exit_code == 0

    assert db_session.query(Run).count() == 0  # noqa: SIM118
    status = seed_data.report_status()
    assert status["agent_definition"] == "present"
    assert status["demo_run"] == "missing"


@pytest.mark.unit
def test_cli_returns_2_when_database_unreachable(monkeypatch) -> None:
    """数据库不可达时 CLI 应返回退出码 2 并给出排查提示，而不是抛异常。"""
    monkeypatch.setattr(sys, "argv", ["seed_data.py"])
    # 指向一个不存在的目录，SQLite 无法创建文件
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:////nonexistent-dir-xyz/a.db")

    from app.core.config import get_settings
    from app.db.session import dispose_engine

    get_settings.cache_clear()
    dispose_engine()
    try:
        exit_code = seed_data.main()
    finally:
        get_settings.cache_clear()
        dispose_engine()

    assert exit_code == 2
