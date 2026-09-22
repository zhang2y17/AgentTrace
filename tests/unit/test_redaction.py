"""脱敏、截断与摘要的单元测试。

契约 TRACE_SCHEMA §7.2 定义了 6 类必须被替换的模式。
本测试逐模式断言，确保没有一类被漏掉。
"""

from __future__ import annotations

import pytest

from app.core.redaction import (
    count_chars,
    redact,
    redact_mapping,
    summarize,
    truncate,
)

pytestmark = pytest.mark.unit


class TestRedactPatterns:
    """TRACE_SCHEMA §7.2 的 6 类模式逐一验证。"""

    def test_openai_style_key(self) -> None:
        text = "使用密钥 sk-abcdefghijklmnopqrstuvwxyz123456 调用"
        result = redact(text)
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in result
        assert "[REDACTED_API_KEY]" in result

    def test_bearer_token(self) -> None:
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abc"
        result = redact(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abc" not in result
        assert "[REDACTED_BEARER]" in result

    @pytest.mark.parametrize(
        "text",
        [
            "api_key=abcdef123456",
            "api-key: abcdef123456",
            "API_KEY = 'abcdef123456'",
            "token=abcdef123456",
            "secret: abcdef123456",
            "password=abcdef123456",
            "access_token=abcdef123456",
        ],
    )
    def test_assignment_style_secrets(self, text: str) -> None:
        """赋值形式（含多种命名与分隔符）必须被替换。"""
        result = redact(text)
        assert "abcdef123456" not in result
        assert "[REDACTED]" in result

    def test_url_credentials(self) -> None:
        text = "连接 postgresql://user:hunter2@db.example.com:5432/app 失败"
        result = redact(text)
        assert "hunter2" not in result
        assert "[REDACTED_CREDENTIALS]" in result

    def test_cn_phone_number(self) -> None:
        text = "联系人电话 13812345678 请记录"
        result = redact(text)
        assert "13812345678" not in result
        assert "[REDACTED_PHONE]" in result

    def test_email(self) -> None:
        text = "发送到 alice.smith+tag@example.co.uk 处理"
        result = redact(text)
        assert "alice.smith+tag@example.co.uk" not in result
        assert "[REDACTED_EMAIL]" in result


class TestRedactNonPatterns:
    """不应误伤的普通内容。"""

    def test_normal_chinese_text_unchanged(self) -> None:
        text = "AgentTrace 通过 TraceMiddleware 记录工具调用事件。"
        assert redact(text) == text

    def test_short_number_not_treated_as_phone(self) -> None:
        """长度不足 11 位的数字不应被当作手机号。"""
        text = "端口 6379，超时 30 秒"
        assert redact(text) == text

    def test_empty_string(self) -> None:
        assert redact("") == ""

    def test_document_id_not_redacted(self) -> None:
        """文档 ID 形如 doc-trace-schema，不应被误伤。"""
        text = "引用 [doc-trace-schema] 与 [doc-api-contract]"
        assert redact(text) == text


class TestRedactMapping:
    """递归脱敏：键名与值都要处理。"""

    def test_nested_values_redacted(self) -> None:
        data = {
            "outer": {
                "api_key": "abcdef123456",
                "nested": ["sk-abcdefghijklmnopqrstuvwxyz123456", 42],
            },
            "safe": "普通值",
        }
        result = redact_mapping(data)

        assert "abcdef123456" not in str(result)
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in str(result)
        assert result["safe"] == "普通值"
        assert result["outer"]["nested"][1] == 42

    def test_non_string_values_preserved(self) -> None:
        """数字、布尔、None 必须保持原类型，不能被转成字符串。"""
        data = {"count": 3, "flag": True, "nothing": None, "ratio": 0.5}
        result = redact_mapping(data)

        assert result == data
        assert isinstance(result["count"], int)
        assert isinstance(result["flag"], bool)

    @pytest.mark.parametrize(
        "key",
        [
            "total_tokens",
            "prompt_tokens",
            "completion_tokens",
            "token_count",
            "tokens_used",
            "max_tokens",
        ],
    )
    def test_token_counting_fields_are_not_redacted(self, key: str) -> None:
        """``*_tokens`` 是**计量字段**，值必须原样保留。

        这是一个真实的缺陷回归测试。脱敏规则曾把 ``"token"`` 当作普通子串
        放进敏感词表，于是 ``total_tokens`` 等键名全部命中 —— 一个整数值
        被整体替换成 ``[REDACTED_TOKEN]``，Trace 里的 token 用量彻底不可读，
        而且**不可逆**（替换发生在写入时）。

        判据不能只看"值还在"，还要看**类型没被改成字符串**：
        字符串化的 ``"1234"`` 同样能让 ``!= "[REDACTED_TOKEN]"`` 成立，
        但已经破坏了字段类型。
        """
        result = redact_mapping({key: 1234})

        assert result[key] == 1234, f"{key} 被误判为敏感键并替换了值"
        assert isinstance(result[key], int), f"{key} 的类型被改成 {type(result[key])}"

    def test_token_word_boundary_does_not_break_real_credentials(self) -> None:
        """收紧 ``token`` 匹配后，真正的凭证字段**必须**仍然命中。

        这条与上面那条是一对：只测一边就会走向另一个极端 ——
        为了让 ``total_tokens`` 不被误伤而把 ``token`` 从敏感词里删掉，
        结果是 ``access_token`` 的真实凭证被原样写进 Trace。
        """
        for key, expected in {
            "token": "[REDACTED_TOKEN]",
            "access_token": "[REDACTED_TOKEN]",
            "authToken": "[REDACTED_TOKEN]",
            "refresh_token": "[REDACTED_TOKEN]",
            "id_token": "[REDACTED_TOKEN]",
        }.items():
            result = redact_mapping({key: "real-credential-value"})
            assert result[key] == expected, f"{key} 未被脱敏，得到 {result[key]!r}"

    def test_realistic_model_call_attributes_survive_redaction(self) -> None:
        """端到端形态：一次 ``model_call`` 的 attributes 走完脱敏后应基本不变。

        契约 D-06 要求 ``model_call`` 落库含三个 token 字段且非 null。
        这条测试锁住的是**整条链路在真实数据形状下的行为** ——
        单键测试只能证明规则本身，证明不了"真实调用链里不会互相干扰"。
        """
        attrs = {
            "node_name": "question_parser",
            "provider": "fake",
            "is_test_double": True,
            "total_tokens": 1234,
            "prompt_tokens": 1000,
            "completion_tokens": 234,
            "latency_ms": 1,
            "estimated_cost_usd": 0.0001,
        }
        result = redact_mapping(attrs)

        assert result == attrs

    def test_arbitrary_object_falls_back_to_repr(self) -> None:
        """自定义对象应降级为 repr 后脱敏，而非抛错或原样保留。"""

        class Holder:
            def __repr__(self) -> str:
                return "Holder(api_key='abcdef123456')"

        result = redact_mapping({"obj": Holder()})
        assert "abcdef123456" not in str(result)


class TestTruncate:
    """截断与原始长度标注。"""

    def test_short_text_unchanged(self) -> None:
        assert truncate("hello", 100) == "hello"

    def test_exact_boundary_unchanged(self) -> None:
        text = "x" * 50
        assert truncate(text, 50) == text

    def test_truncated_with_original_length(self) -> None:
        """截断标记必须包含原始长度，便于判断数据是否完整。"""
        text = "a" * 200
        result = truncate(text, 50)

        assert len(result) <= 50
        assert "truncated:200" in result

    def test_result_never_exceeds_budget(self) -> None:
        """无论输入多长，结果长度必须 <= max_chars。"""
        for length in (0, 1, 10, 500, 10_000):
            for budget in (10, 50, 500):
                result = truncate("y" * length, budget)
                assert len(result) <= budget, (length, budget, len(result))

    def test_tiny_budget_degrades_to_hard_cut(self) -> None:
        """预算小于截断后缀长度时退化为硬截断，而不是抛错或超长。"""
        result = truncate("z" * 100, 5)
        assert len(result) == 5

    def test_rejects_non_positive_budget(self) -> None:
        with pytest.raises(ValueError, match="max_chars 必须为正整数"):
            truncate("abc", 0)


class TestSummarize:
    """summarize 是 Trace 与日志写入的唯一入口（TRACE_SCHEMA §7.1）。"""

    def test_dict_serialized_as_compact_json(self) -> None:
        result = summarize({"a": 1, "b": "文本"})
        assert result == '{"a":1,"b":"文本"}'

    def test_string_passed_through_and_redacted(self) -> None:
        result = summarize("key=abcdef123456")
        assert "abcdef123456" not in result

    def test_none_returns_empty(self) -> None:
        assert summarize(None) == ""

    def test_respects_max_chars(self) -> None:
        result = summarize({"payload": "x" * 5000}, max_chars=100)
        assert len(result) <= 100
        assert "truncated:5000" in result or "truncated" in result

    def test_redacts_inside_nested_structure(self) -> None:
        result = summarize({"messages": [{"role": "user", "content": "api_key=abcdef123456"}]})
        assert "abcdef123456" not in result

    def test_unserializable_object_does_not_raise(self) -> None:
        """无法 JSON 序列化的对象必须降级而非抛错，否则会打断 Trace 写入。"""

        class Weird:
            def __repr__(self) -> str:
                return "<Weird>"

        result = summarize({"obj": Weird()})
        assert "Weird" in result


class TestCountChars:
    """字符规模统计，用于日志中替代完整内容。"""

    def test_counts_string(self) -> None:
        assert count_chars("abc") == 3

    def test_counts_none_as_zero(self) -> None:
        assert count_chars(None) == 0

    def test_counts_nested_structure(self) -> None:
        assert count_chars({"a": "bc"}) == 3  # 1 (key) + 2 (value)
