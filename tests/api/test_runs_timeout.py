"""超时路径测试（IMPLEMENTATION_PLAN §5 第 6 项要求覆盖"超时"场景）。

超时是独立终态，不是失败。这条区分有实际后果：

- ``timeout`` **不计入** ``error_rate``（EVALUATION §3）—— "没跑完"
  和"跑错了"是两回事，混在一起会让错误率失去诊断力；
- ``timeout`` **计入** ``human_review_rate`` —— 结果不能直接采信；
- run 行的状态必须是 ``timeout``，不能是 ``failed``。

测试手法：注入一个**必然超时**的图构造器。用真实的
``AGENT_TIMEOUT_SECONDS=0`` 会依赖计时的精度（0 秒超时在不同机器上
行为可能不同），而注入一个"故意睡很久"的图会让这个测试稳定地
命中超时分支 —— 测的是**超时处理逻辑**，不是计时器本身。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app

pytestmark = [pytest.mark.api, pytest.mark.test_double]


class _SleepyGraph:
    """一个"永远跑不完"的图桩。

    ``invoke`` 睡眠时间远大于超时配置，从而确定性地触发超时。
    睡眠用短间隔轮询而不是一次长 sleep：超时返回后测试会继续，
    长 sleep 会让后台线程一直占着，拖慢整个测试套件的退出。
    """

    def __init__(self, seconds: float = 5.0) -> None:
        self.seconds = seconds

    def invoke(self, _state: dict) -> dict:  # noqa: ARG002
        deadline = time.monotonic() + self.seconds
        while time.monotonic() < deadline:
            time.sleep(0.05)
        return {}


@pytest.fixture
def timeout_client(monkeypatch) -> TestClient:  # type: ignore[no-untyped-def]
    """客户端：Agent 超时被压到 1 秒，且图被替换成"跑不完"的桩。

    只替换**图构造器**，不替换整个 ``RunService`` —— 这样超时控制
    （线程池 + ``future.result(timeout)``）这条真实代码路径仍然被执行，
    而不是把整个服务换成假对象。

    **覆盖的是 ``get_run_service`` 而不是 ``RunService``**：路由的依赖是
    ``Depends(get_run_service)`` 这个函数，FastAPI 只在依赖直接写成
    ``Depends(RunService)`` 时才会去查 ``RunService`` 的覆盖表。
    覆盖错对象的表现很有迷惑性 —— 接口返回 201 一切正常，
    只是"超时"从未发生（实际跑了真图）。
    """
    import os

    from app.api.deps import get_run_service
    from app.db.session import dispose_engine
    from app.services.run_service import RunService

    previous_timeout = os.environ.get("AGENT_TIMEOUT_SECONDS")
    previous_auto = os.environ.get("AUTO_CREATE_TABLES")
    os.environ["AGENT_TIMEOUT_SECONDS"] = "1"
    os.environ["AUTO_CREATE_TABLES"] = "true"
    get_settings.cache_clear()
    dispose_engine()

    app = create_app()

    def _override() -> RunService:
        # 1 秒超时 + 5 秒睡眠 → 必然超时；留足余量避免临界抖动
        return RunService(
            get_settings(),
            graph_builder=lambda _deps: _SleepyGraph(seconds=5.0),
        )

    app.dependency_overrides[get_run_service] = _override

    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        for key, prev in (
            ("AGENT_TIMEOUT_SECONDS", previous_timeout),
            ("AUTO_CREATE_TABLES", previous_auto),
        ):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        get_settings.cache_clear()
        dispose_engine()


def test_timeout_returns_504_and_marks_run(timeout_client: TestClient) -> None:
    """超时 → HTTP 504，且 run 状态落库为 ``timeout``。

    504 Gateway Timeout 而不是 500：超时是"上游没在预期时间内返回"，
    语义上区别于"服务内部错误"。
    """
    response = timeout_client.post("/runs", json={"question": "任意问题"})

    assert response.status_code == 504, response.text
    error = response.json()["error"]
    assert error["code"] == "AGENT_TIMEOUT"
    assert {"code", "message", "details", "request_id"} <= set(error)


def test_timeout_run_is_persisted_as_timeout(timeout_client: TestClient) -> None:
    """超时后必须能在库里查到一条 ``timeout`` 的 run。

    这条比状态码更重要：若超时只体现在 HTTP 响应里而没落库，
    这次失败的尝试就无从追溯 —— 而"运行过但没记录"正是本项目
    要消灭的状态。
    """
    timeout_client.post("/runs", json={"question": "任意问题"})

    # 超时的 run 拿不到 run_id（响应是错误体），因此按列表查
    from app.db.repository import TraceRepository
    from app.db.session import session_scope

    with session_scope() as session:
        runs = TraceRepository(session).list_runs(limit=10)
        assert runs, "超时的 run 应已落库"

        run = runs[0]
        assert run.status == "timeout"
        assert run.error_code == "AGENT_TIMEOUT"
        assert run.ended_at is not None, "终态 run 必须有结束时间"


def test_timeout_not_counted_as_error(timeout_client: TestClient) -> None:
    """超时不计入 ``error_rate``，但计入 ``human_review_rate``。

    这是 EVALUATION §3 的明确口径。若实现把超时算成错误，
    一个依赖变慢的时段会被读成"Agent 质量下降"，而实际是
    "Agent 没跑完" —— 两种结论引向完全不同的处置。
    """
    timeout_client.post("/runs", json={"question": "任意问题"})

    metrics = timeout_client.get("/metrics/summary").json()["metrics"]

    assert metrics["case_count"] == 1
    assert metrics["error_rate"] == 0.0, "超时不应计入错误率"
    assert metrics["human_review_rate"] == 1.0, "超时应计入人工复核率"
    assert metrics["run_success_rate"] == 0.0, "超时不视为成功"


def test_timeout_run_can_be_replayed(timeout_client: TestClient) -> None:
    """超时的 run 是**已定论**的，因此可以回放。

    这条检验 ``_REPLAYABLE_STATUSES`` 包含 ``timeout``：
    超时给了我们一个明确结论（"在预算内没跑完"），
    拿它当基准去对比"放宽预算后能否跑完"是有意义的实验。
    """
    from app.db.repository import TraceRepository
    from app.db.session import session_scope

    timeout_client.post("/runs", json={"question": "任意问题"})

    with session_scope() as session:
        run_id = TraceRepository(session).list_runs(limit=1)[0].id

    # 回放本身也会超时（图还是那个桩），但**不该**是 409 不可回放
    response = timeout_client.post(f"/runs/{run_id}/replay", json={})

    assert response.status_code != 409, response.text
    assert response.status_code == 504
