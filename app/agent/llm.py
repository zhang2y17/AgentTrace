"""LLM 网关：抽象 + 三个 provider。

契约 T13 / B5：**必须区分真实调用与测试替身**。
``FakeLLMProvider`` 是明确标注的替身，其输出是**确定性规则**的结果，
不是随机文本 —— 随机输出会让测试不稳定，也让评测不可复现。

三个 provider：

| provider | 用途 | 需要密钥 | 访问网络 |
|---|---|---|---|
| ``fake``   | 默认，测试与离线开发 | 否 | 否 |
| ``openai`` | OpenAI-compatible API | 是（``LLM_API_KEY``） | 是 |
| ``ollama`` | 本地 Ollama | 通常否 | 仅本机 |

契约 B8：API Key **只从环境变量读取**。本模块不读文件、不接受参数传入密钥，
只经 ``Settings.llm_api_key``（``SecretStr``）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import Settings
from app.core.errors import LlmProviderError
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class LlmResponse:
    """一次模型调用的结果。

    ``is_test_double`` 是**强制**字段（契约 T13）：
    它必须被写入 ``model_call`` 行与 API 响应，
    让任何读者都能判断"这个数字来自真实模型还是替身"。
    """

    text: str
    provider: str
    model_name: str
    is_test_double: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int | None = None
    # 无法估算成本时必须为 True，而不是编一个 0.0 当真实成本
    cost_estimation_unavailable: bool = False
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class LlmProvider(Protocol):
    """provider 协议。

    只要求 ``complete`` 一个方法：本项目的模型调用点只有两类
    （解析问题、生成答案），都不需要流式输出或多轮对话。
    保持接口最小，替身实现就足够简单，测试也稳定。
    """

    provider_name: str
    is_test_double: bool

    def complete(self, *, system: str, user: str, node_name: str) -> LlmResponse:
        """执行一次补全。"""
        ...


# ---------------------------------------------------------------------------
# Fake provider（确定性规则实现）
# ---------------------------------------------------------------------------


# 虚词与疑问尾词。出现在关键词里只会稀释区分度：
# 它们在所有文档中都高频出现，等于给每篇文档加同样的分数。
_PARTICLES: tuple[str, ...] = (
    "的作用是什么",
    "的作用",
    "是什么",
    "什么是",
    "有哪些",
    "为什么",
    "怎么样",
    "如何",
    "作用",
    "的",
    "了",
    "是",
    "吗",
    "呢",
    "会",
    "在",
    "和",
    "与",
)


def _strip_particles(text: str) -> str:
    """剪掉虚词与疑问尾词。

    按**长词优先**替换，避免先剪掉"的"之后剩下"作用是什么"这种残缺形态
    （那样反而更难清理）。
    """
    result = text
    for particle in sorted(_PARTICLES, key=len, reverse=True):
        result = result.replace(particle, "")
    return result.strip()


class FakeLLMProvider:
    """测试替身：**确定性**规则实现，不访问网络、不需要密钥。

    设计要点：**不用随机数**。随机输出会让同一输入在两次运行中产生
    不同结果，从而让指标不可复现 —— 这对一个以"评测"为核心的
    项目是致命的。

    实现方式是关键词匹配 + 模板拼装：给定输入，输出必然唯一确定。
    这当然不"智能"，但它是**可控**的，而可控正是测试替身的要求。
    """

    provider_name = "fake"
    is_test_double = True

    def __init__(self, model_name: str = "fake-echo-1") -> None:
        self.model_name = model_name

    def complete(self, *, system: str, user: str, node_name: str) -> LlmResponse:
        """按节点产出确定性的结构化文本。"""
        started = time.perf_counter()

        if node_name == "question_parser":
            text = self._parse_question(user)
        elif node_name == "answer_writer":
            text = self._write_answer(system, user)
        else:
            # 其他节点不该调用 LLM。若发生了，明确报出来而不是静默编造，
            # 否则"哪个节点真的用了模型"这个问题会失去答案。
            text = json.dumps(
                {"note": f"{node_name} 未定义替身输出", "echo_chars": len(user)},
                ensure_ascii=False,
            )

        latency_ms = int((time.perf_counter() - started) * 1000)

        return LlmResponse(
            text=text,
            provider=self.provider_name,
            model_name=self.model_name,
            is_test_double=True,
            # 替身不产生真实 Token 消耗，如实记 0
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=latency_ms,
            # 替身没有真实价格 → 成本"不可估算"，而不是编一个数字
            cost_estimation_unavailable=True,
            metadata={"note": "本地测试替身，确定性规则输出，未访问网络。"},
        )

    # ------------------------------------------------------------ 规则实现
    @staticmethod
    def _extract_keywords(question: str) -> list[str]:
        """从问题中抽取检索关键词。

        规则：
        1. 剥掉疑问词与常见虚词；
        2. 保留英文标识符（``parent_event_id``）与长度 ≥ 2 的中文片段；
        3. 结果去重且保持出现顺序（确定性）。

        刻意**不**做分词之外的语义扩展 —— 扩展交给检索层的同义词表，
        职责分离后两处都可以各自单测。
        """
        # 疑问词与虚词表：这些词在检索中没有区分度
        stopwords = {
            "的", "了", "是", "吗", "呢", "什么", "怎么", "如何", "为什么",
            "哪些", "哪个", "多少", "是否", "请问", "以及", "还有", "作用",
            "时候", "问题", "可以", "需要", "这个", "那个", "一个", "我们",
            "它", "会", "有", "在", "与", "和", "对", "用", "被", "把", "从",
            "吗？", "？", "?", "。", "，",
        }

        keywords: list[str] = []

        # 英文标识符（含下划线）优先 —— 它们区分度最高
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", question):
            keywords.append(token)

        # 中文：按标点切分成小段，再切出长度 2~6 的连续片段
        for segment in re.split(r"[，。！？；：、\s]+", question):
            chinese_only = re.findall(r"[\u4e00-\u9fff]+", segment)
            for run in chinese_only:
                # 先把"的/了/是"这类虚词剪掉，避免"的作用是什么"整段成为关键词。
                # 不剪的话，切片得到的"的作用"、"作用是"都会进关键词表，
                # 它们在所有文档里都出现，等于给每篇文档都加同样的分，
                # 反而稀释了真正的主题词的区分度。
                trimmed = _strip_particles(run)
                if len(trimmed) < 2:
                    continue

                if len(trimmed) <= 8:
                    keywords.append(trimmed)
                else:
                    for size in (4, 3, 2):
                        for i in range(len(trimmed) - size + 1):
                            piece = trimmed[i : i + size]
                            if piece not in stopwords:
                                keywords.append(piece)

        # 去重保序
        seen: set[str] = set()
        ordered: list[str] = []
        for keyword in keywords:
            lowered = keyword.lower()
            if lowered in seen or keyword in stopwords:
                continue
            seen.add(lowered)
            ordered.append(keyword)
        return ordered[:12]

    def _parse_question(self, user: str) -> str:
        """产出 ``question_parser`` 的结构化输出。"""
        payload = {
            "keywords": self._extract_keywords(user),
            "intent": self._classify_intent(user),
            "question_chars": len(user),
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _classify_intent(question: str) -> str:
        """粗分类问题意图。

        只影响答案的组织方式（是否需要给出"理由"小节），
        不参与打分，因此规则简单即可。
        """
        if re.search(r"为什么|原因是|为什么用|为何|理由|动机", question):
            return "决策理由"
        if re.search(r"上限|下限|范围|多少|几个|默认值|参数", question):
            return "参数确认"
        if re.search(r"是什么|什么是|含义|作用|解释|怎么理解", question):
            return "概念解释"
        return "其他"

    def _write_answer(self, system: str, user: str) -> str:
        """产出 ``answer_writer`` 的答案正文。

        替身的答案是"模板 + 证据引用"的形式，刻意保持结构化：
        它必须包含 ``[doc-xxx]`` 引用，否则 ``contains_citation``
        断言会失败，而失败原因是**替身的限制**而非被测逻辑的缺陷 ——
        那会让评测结果无法解释。因此替身被设计为"总是能给出引用"。
        """
        # 从 user 中解析出传入的证据引用（由节点组装）
        cited = re.findall(r"\[(doc-\d{3})\]", user)
        unique_cited = list(dict.fromkeys(cited))

        # system 里带原始问题（节点会传进来），用于生成主题句
        topic_match = re.search(r"问题[:：]\s*(.+)", user)
        topic = topic_match.group(1).strip() if topic_match else "该问题"

        lines = [
            f"关于「{topic}」：",
            "",
            "依据样例文档，要点如下（由本地测试替身按证据拼接，非真实模型生成）：",
        ]

        if unique_cited:
            for index, document_id in enumerate(unique_cited, start=1):
                lines.append(f"{index}. 见文档 [{document_id}] 的相关说明。")
        else:
            lines.append("（未检索到可用证据，本答案不包含引用。）")

        lines += [
            "",
            "证据：" + ("、".join(f"[{item}]" for item in unique_cited) if unique_cited else "无"),
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# OpenAI-compatible provider
# ---------------------------------------------------------------------------


class OpenAICompatibleProvider:
    """通过 HTTP 调用 OpenAI-compatible 接口。

    契约 B8：密钥只从 ``Settings.llm_api_key`` 读取（来自环境变量），
    绝不禁用日志输出、绝不入库。

    本类**不**在 import 时构造客户端 —— 那会在缺少密钥时直接抛错，
    导致应用无法启动（而"缺密钥"应当由 ``/health`` 如实报告为 degraded）。
    """

    provider_name = "openai"
    is_test_double = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_name = settings.llm_model
        self.base_url = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")

    def complete(self, *, system: str, user: str, node_name: str) -> LlmResponse:
        """调用真实接口。

        Raises:
            LlmProviderError: 缺少密钥、网络失败、或响应结构不符合预期。
        """
        api_key = self.settings.llm_api_key
        if api_key is None or not api_key.get_secret_value().strip():
            # 明确报"缺密钥"，而不是让它变成一个含义不明的 401
            raise LlmProviderError(
                "LLM_PROVIDER=openai 但未配置 LLM_API_KEY，无法调用真实模型。",
                details={"provider": self.provider_name, "hint": "设置环境变量 LLM_API_KEY"},
            )

        started = time.perf_counter()
        try:
            import urllib.error
            import urllib.request

            payload = json.dumps(
                {
                    "model": self.model_name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                }
            ).encode("utf-8")

            request = urllib.request.Request(  # noqa: S310 —— 端点来自配置
                f"{self.base_url}/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key.get_secret_value()}",
                },
                method="POST",
            )

            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self.settings.llm_timeout_seconds
            ) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 —— 统一转领域异常
            # 注意：异常消息可能含 URL 与响应体，不能直接当作 details 外传
            raise LlmProviderError(
                "调用真实模型失败。",
                details={
                    "provider": self.provider_name,
                    "model": self.model_name,
                    "error_type": type(exc).__name__,
                },
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            text = raw["choices"][0]["message"]["content"]
            usage = raw.get("usage") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmProviderError(
                "模型响应结构不符合预期。",
                details={"provider": self.provider_name, "model": self.model_name},
            ) from exc

        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)

        return LlmResponse(
            text=text,
            provider=self.provider_name,
            model_name=self.model_name,
            is_test_double=False,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=int(usage.get("total_tokens") or (prompt_tokens + completion_tokens)),
            latency_ms=latency_ms,
            cost_estimation_unavailable=False,
        )


# ---------------------------------------------------------------------------
# Ollama provider
# ---------------------------------------------------------------------------


class OllamaProvider:
    """调用本机 Ollama。

    默认不需要密钥，但端点来自配置，因此与 OpenAI 走同一套错误处理。
    """

    provider_name = "ollama"
    is_test_double = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_name = settings.llm_model
        self.base_url = (settings.llm_base_url or "http://localhost:11434").rstrip("/")

    def complete(self, *, system: str, user: str, node_name: str) -> LlmResponse:
        """调用本机 Ollama。"""
        started = time.perf_counter()
        try:
            import urllib.request

            payload = json.dumps(
                {
                    "model": self.model_name,
                    "prompt": f"{system}\n\n{user}",
                    "stream": False,
                    "options": {"temperature": 0},
                }
            ).encode("utf-8")

            request = urllib.request.Request(  # noqa: S310 —— 端点来自配置
                f"{self.base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self.settings.llm_timeout_seconds
            ) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise LlmProviderError(
                "调用 Ollama 失败。",
                details={
                    "provider": self.provider_name,
                    "model": self.model_name,
                    "error_type": type(exc).__name__,
                },
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        prompt_tokens = int(raw.get("prompt_eval_count") or 0)
        completion_tokens = int(raw.get("eval_count") or 0)

        return LlmResponse(
            text=str(raw.get("response") or ""),
            provider=self.provider_name,
            model_name=self.model_name,
            is_test_double=False,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            latency_ms=latency_ms,
            cost_estimation_unavailable=False,
        )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_provider(settings: Settings) -> LlmProvider:
    """按配置构造 provider。

    未知取值回退到 fake 并记 warning，而不是抛错 ——
    配置写错时应用仍能启动，由 ``/health`` 如实报告当前用的是替身。
    """
    provider = settings.llm_provider
    if provider == "openai":
        return OpenAICompatibleProvider(settings)
    if provider == "ollama":
        return OllamaProvider(settings)
    if provider != "fake":
        logger.warning(
            "unknown_llm_provider_falling_back_to_fake",
            extra={"configured": provider},
        )
    return FakeLLMProvider()


__all__ = [
    "FakeLLMProvider",
    "LlmProvider",
    "LlmResponse",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "build_provider",
]
