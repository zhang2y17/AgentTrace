"""密钥边界测试（契约 SECURITY / B8，IMPLEMENTATION_PLAN §7 第 1、2 项）。

这些测试回答一个具体问题：**应用自己的密钥会不会出现在它不该出现的地方？**
三个"不该出现的地方"：

1. 数据库的任何表；
2. HTTP 响应体；
3. 日志输出。

**测试里刻意区分两类"密钥"**，因为它们的期望行为相反：

- **应用自身的配置密钥**（``LLM_API_KEY`` / 带密码的 ``LLM_BASE_URL``）
  → 必须完全不落库。这是本文件的主要断言对象。
- **用户输入里恰好长得像密钥的字符串**（``question`` 里写了 ``api_key=sk-...``）
  → 会原样保存在 ``run.question``。这是**有意为之**：question 是"当时问了什么"
  的权威记录，改写它会让回放失去意义。它由 Trace 写入点的脱敏
  （``summarize``）负责，而不是由 question 字段负责 —— question 本身就是原始输入。

把两者混为一谈会导致两种错误：要么以为"用户输入里有密钥=系统泄露"，
要么为了让后者通过而把 question 也脱敏掉，从而破坏回放的可复现性。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.api, pytest.mark.test_double]

# 形如真实密钥的测试值。刻意足够长，能命中 ``sk-`` 脱敏模式。
API_KEY = "sk-" + "Q" * 40

# 带密码的连接串 —— 密码部分必须被脱敏
BASE_URL_WITH_PASSWORD = "https://user:hunter2@api.example.com/v1"


def _dump_all_tables(db_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """读出库里所有表的全部行，用于全文扫描。"""
    connection = sqlite3.connect(db_path)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        dump: dict[str, list[tuple[Any, ...]]] = {}
        for table in tables:
            try:
                dump[table] = connection.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.Error:
                continue
        return dump
    finally:
        connection.close()


def _search_everywhere(dump: dict[str, list[tuple[Any, ...]]], needle: str) -> list[str]:
    """返回含 ``needle`` 的表名列表。"""
    hits: list[str] = []
    for table, rows in dump.items():
        blob = json.dumps([str(cell) for row in rows for cell in row], ensure_ascii=False)
        if needle in blob:
            hits.append(table)
    return hits


class TestConfiguredSecretNeverPersisted:
    """应用配置里的密钥不得进入数据库。"""

    def test_api_key_is_not_persisted(
        self, client: TestClient, settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``LLM_API_KEY`` 的值不得出现在任何表里。

        这条断言是"密钥只从 env 读、不落库"的直接检验。
        它比"代码里看不到写密钥的语句"强得多 —— 后者无法覆盖
        间接写入（比如某个 summary 里带上了整个 settings）。
        """
        monkeypatch.setenv("LLM_API_KEY", API_KEY)

        from app.core.config import get_settings

        get_settings.cache_clear()

        response = client.post("/runs", json={"question": "trace 事件的 sequence 作用？"})
        assert response.status_code == 201, response.text

        db_path = Path(settings.database_url.split("///")[-1])
        hits = _search_everywhere(_dump_all_tables(db_path), API_KEY)
        assert hits == [], f"密钥出现在这些表里：{hits}"

    def test_base_url_password_is_not_persisted(
        self, client: TestClient, settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``LLM_BASE_URL`` 里的密码不得落库。

        这个字段比 API Key 更容易漏：它看着像个普通 URL，
        很容易被整个塞进某个 metadata / 配置快照里。
        """
        monkeypatch.setenv("LLM_BASE_URL", BASE_URL_WITH_PASSWORD)

        from app.core.config import get_settings

        get_settings.cache_clear()

        assert client.post("/runs", json={"question": "证据覆盖率怎么算？"}).status_code == 201

        db_path = Path(settings.database_url.split("///")[-1])
        hits = _search_everywhere(_dump_all_tables(db_path), "hunter2")
        assert hits == [], f"URL 密码出现在这些表里：{hits}"

    def test_api_key_absent_from_http_responses(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """响应体不得回显密钥。"""
        monkeypatch.setenv("LLM_API_KEY", API_KEY)

        from app.core.config import get_settings

        get_settings.cache_clear()

        for path in ("/health", "/metrics/summary"):
            response = client.get(path)
            assert API_KEY not in response.text, f"{path} 回显了密钥"

        run = client.post("/runs", json={"question": "trace 事件有哪些类型？"})
        assert API_KEY not in run.text, "/runs 回显了密钥"


class TestHealthOnlyExposesBoolean:
    """``/health`` 只能暴露"是否配置了密钥"，不能暴露密钥本身。"""

    def test_health_reports_boolean_not_value(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``llm_provider.api_key_configured`` 是**顶层**字段。

        注意它不在 ``components`` 里 —— 我第一版测试写成了
        ``components["llm_provider"]`` 并因此 KeyError。
        ``components`` 只放基础设依赖（api / database / redis），
        模型提供方是与它们并列的另一类信息。
        """
        monkeypatch.setenv("LLM_API_KEY", API_KEY)

        from app.core.config import get_settings

        get_settings.cache_clear()

        body = client.get("/health").json()
        provider = body["llm_provider"]

        assert provider["api_key_configured"] is True
        assert API_KEY not in json.dumps(body)

    def test_health_reports_false_when_unset(self, client: TestClient) -> None:
        provider = client.get("/health").json()["llm_provider"]
        assert provider["api_key_configured"] is False


class TestTraceSummariesAreRedacted:
    """Trace 的 summary 字段是外部内容进入数据库的通道，必须已脱敏。"""

    def test_tool_arguments_with_secret_are_redacted(self, client: TestClient, settings) -> None:
        """用户输入里的疑似密钥不得以明文进入 Trace 事件。

        与 ``run.question`` 的期望**相反**：question 是原始输入记录，
        而 Trace 的 ``input_summary`` / ``output_summary`` 是
        "为了可观测性而额外记录"的派生数据 —— 派生数据没有任何理由
        承载密钥明文，因此这里必须被替换成占位符。
        """
        secret = "sk-" + "Z" * 40
        response = client.post(
            "/runs", json={"question": f"请说明工具调用参数，注意 token={secret}"}
        )
        assert response.status_code == 201, response.text

        db_path = Path(settings.database_url.split("///")[-1])
        dump = _dump_all_tables(db_path)

        # question 保留原文是本项目的既有决定，因此扫描时排除它，
        # 只检查"派生数据"所在的表。
        derived_only = {k: v for k, v in dump.items() if k != "run"}
        hits = _search_everywhere(derived_only, secret)
        assert hits == [], f"密钥明文出现在派生数据表里：{hits}"

    def test_redaction_placeholder_is_visible_in_trace(self, client: TestClient, settings) -> None:
        """脱敏不是"删掉"而是"替换成可识别占位符"。

        只删不标会让人以为原始数据里本来就没有这个字段，
        从而无法判断脱敏是否生效。

        **扫描范围排除 ``run`` 表**：``run.question`` 刻意保留用户原文
        （见 ``TestUserInputVerbatimVsDerived``），因此它必然含原始密钥；
        断言"全库不含密钥"会与那条设计冲突，且冲突的原因不在脱敏。
        """
        secret = "sk-" + "Y" * 40
        client.post("/runs", json={"question": f"检查 api_key={secret} 的用法"})

        db_path = Path(settings.database_url.split("///")[-1])
        dump = _dump_all_tables(db_path)
        derived = {k: v for k, v in dump.items() if k != "run"}

        assert _search_everywhere(derived, secret) == [], "派生数据表里出现了密钥明文"

        blob = json.dumps(
            [str(cell) for rows in derived.values() for row in rows for cell in row],
            ensure_ascii=False,
        )
        assert "[REDACTED" in blob, "发生了替换但找不到占位符，无法确认脱敏生效"


class TestUserInputVerbatimVsDerived:
    """把两类内容的期望差异固化成测试，避免有人"顺手统一"掉。"""

    def test_question_keeps_user_input_verbatim(self, client: TestClient, settings) -> None:
        """``run.question`` 原样保存用户输入 —— 这是刻意设计。

        改写 question 会让回放无法重现"当时到底问了什么"，
        而回放的可复现性正是本项目评测结论可信的前提。
        如果这条测试失败，说明有人把输入脱敏逻辑套到了 question 上，
        需要重新评估回放保真度与脱敏的权衡。
        """
        secret = "sk-" + "V" * 40
        question = f"请解释 trace，api_key={secret}"

        assert client.post("/runs", json={"question": question}).status_code == 201

        db_path = Path(settings.database_url.split("///")[-1])
        rows = _dump_all_tables(db_path)["run"]
        questions = [str(cell) for row in rows for cell in row if cell == question]
        assert questions, "question 字段被改写了，回放将无法复现原始输入"
