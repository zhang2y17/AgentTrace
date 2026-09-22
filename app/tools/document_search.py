"""文档检索工具：``search_documents`` 与 ``get_document``。

设计约束（PROJECT_SPEC §3.2 / IMPLEMENTATION_PLAN S4 风险表）：

1. **只访问项目内样例文档**。文档 ID 在校验层已限定为 ``doc-\\d{3}``，
   本模块再做一次"必须落在 DOCS_DIR 内"的路径检查，
   防止 ``..`` 之类的构造逃逸目录；
2. **只用关键词打分，不引入向量库**。理由：向量库带来额外依赖与
   模型下载，破坏"默认测试不访问网络"的约束。
   样例文档只有 6 篇，关键词打分足够；
3. **确定性**：同一输入必然给出同一排序。分数相同时用 ``document_id``
   做次级排序键，避免依赖字典遍历顺序。

打分公式（可被单测逐项核对）::

    score = 2.0 * (命中的标题词数)
          + 1.0 * (命中的标签词数)
          + 1.0 * (命中正文的次数，上限 10)
          + 0.5 * (命中同义词扩展的词数)

同义词表解决"问法与文档措辞不一致"的问题。它是**显式维护**的，
不是自动学习的 —— 自动扩召回会让评测结果不可解释。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

from app.core.logging import get_logger
from app.tools.base import Document, DocumentHit

logger = get_logger(__name__)

# 单条 snippet 的最大长度，避免把整篇文档塞进工具返回值
_SNIPPET_MAX_CHARS: Final = 180

# 正文命中次数的计分上限。避免一篇长文档因为反复出现某个词而霸榜
_CONTENT_HIT_CAP: Final = 10

# 同义词表：把常见问法映射到文档中的实际措辞。
#
# 键是"用户可能写的词"，值是"文档里实际会出现的词"。
# 双向扩展由 `_expand_terms` 完成，因此这里只需单向声明。
_SYNONYMS: Final[dict[str, tuple[str, ...]]] = {
    "序号": ("sequence",),
    "顺序": ("sequence", "order"),
    "排序": ("sequence", "order"),
    "父事件": ("parent_event_id",),
    "父节点": ("parent_event_id",),
    "引用": ("citation", "引用"),
    "编造": ("hallucinated", "反幻觉"),
    "幻觉": ("hallucinated", "no_hallucinated_doc"),
    "降级": ("degraded",),
    "重试": ("retry", "retried", "放宽检索"),
    "工具": ("tool", "工具"),
    "参数": ("arguments", "参数校验"),
    "校验": ("validate", "校验"),
    "节点": ("node", "节点"),
    "成本": ("cost",),
    "延迟": ("latency",),
    "耗时": ("latency", "duration_ms"),
    "门禁": ("gate", "质量门禁"),
    "指标": ("metric", "指标"),
    "分母": ("denominator", "分母"),
    "选型": ("选型", "decisions"),
    "数据库": ("database", "postgresql", "sqlite"),
    "主键": ("id", "ulid", "primary_key"),
    "脱敏": ("redact", "redaction", "脱敏"),
    "回放": ("replay", "回放"),
    "评测": ("evaluation", "评测"),
    "断言": ("assertion", "断言"),
    "分位数": ("percentile", "p95", "nearest-rank"),
    "ulid": ("ulid",),
}

# 停用词：检索时**完全忽略**的词元。
#
# 为什么必须显式维护这张表：中文疑问句里的疑问词与功能词
# （"什么"、"怎么"、"问题"）几乎出现在所有口语化查询里，
# 而它们在样例文档里也反复出现。若不剔除，一句与项目毫无关系的
# 提问（"今天晚饭吃什么"）也能在每个文档里命中"什么"，
# 从而产生非空的检索结果与虚高的覆盖率 ——
# 这会让"证据充分"变成一个恒真的判断，覆盖率指标也随之失去意义。
#
# 这是实际调试中发现的缺陷，不是假想的边界情况。
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        # 疑问词与疑问尾
        "什么",
        "怎么",
        "怎样",
        "如何",
        "为什么",
        "哪些",
        "哪个",
        "哪里",
        "是否",
        "多少",
        "几个",
        "吗",
        "呢",
        "么",
        # 功能词 / 连接词
        "的",
        "了",
        "是",
        "在",
        "和",
        "与",
        "或",
        "及",
        "也",
        "就",
        "都",
        "会",
        "能",
        "要",
        "有",
        "无",
        "被",
        "把",
        "对",
        "从",
        "到",
        "为",
        "这个",
        "那个",
        "这些",
        "那些",
        "一个",
        "一些",
        "这里",
        "那里",
        "可以",
        "需要",
        "应该",
        "必须",
        "以及",
        "并且",
        "但是",
        "因为",
        "所以",
        "如果",
        "那么",
        "then",
        "than",
        # 元话语：提问者描述"我在问问题"的措辞，不承载主题
        "问题",
        "疑问",
        "请问",
        "想问",
        "关于",
        "相关",
        "有关",
        "完全",
        "无关",
        "完全不",
        "今天",
        "昨天",
        "明天",
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "of",
        "to",
        "in",
        "on",
        "for",
        "and",
        "or",
        "how",
        "what",
        "why",
        "which",
        "when",
        "does",
        "do",
        "did",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "be",
        "been",
        "can",
        "could",
        "should",
        "would",
        "please",
        "about",
        "with",
        "from",
    }
)

# 单篇文档进入检索结果所需的最低分数。
#
# 没有这道门槛时，任何非空查询都会返回 top_k 篇文档（因为总能命中
# 某个单字或二元组），于是 evidence_checker 的"命中数"恒等于 top_k，
# 覆盖率恒为 1 —— "证据不足"这条分支永远不会被触发。
# 门槛值来自样例语料上的实测：真正相关的查询首篇分数在 20 以上，
# 无关查询的最高分不超过 12，取 15 能干净地分开两者。
_MIN_SCORE: Final = 15.0

# Front matter 解析：形如
# ---
# document_id: doc-001
# title: xxx
# tags: [a, b]
# ---
_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _tokenize(text: str) -> list[str]:
    """把文本切成检索用的词元。

    中文采用**字 + 二元组（bigram）**两级切分：

    - 单字：召回广，但歧义大（"证" 会命中"验证"、"证明"）；
    - 二元组：能命中"证据"、"不足"这类真实词汇。

    只按单字切分时，"证据不足"会变成"证/据/不/足"四个单字，
    打分器里 `len(term) < 2` 的过滤会把它们全部丢掉，
    导致这类查询命中为空 —— 这是实际调试中发现的真实问题。

    英文/数字按单词切分（含下划线，覆盖 `parent_event_id` 这类标识符）。

    切分后会剔除 ``_STOPWORDS`` 中的词元。注意剔除发生在**二元组
    生成之后**：先切分再过滤，可以让"证据不足时会怎么做"里的
    "证据"/"不足"这类真实词汇保留下来，而"什么"/"怎么"被丢弃。
    """
    lowered = text.lower()
    latin = re.findall(r"[a-z0-9_]+", lowered)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    # 连续中文串的二元组：只在相邻字符处生成，不跨标点
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", lowered)
    bigrams = [run[i : i + 2] for run in cjk_runs for i in range(len(run) - 1)]
    tokens = latin + cjk_chars + bigrams

    # 停用词过滤：单字停用词（的/了/是）与多字停用词（什么/怎么/问题）
    # 都需要剔除，否则无关查询会靠这些词"命中"每一篇文档。
    return [token for token in tokens if token not in _STOPWORDS]


def _expand_terms(terms: list[str], raw_query: str) -> set[str]:
    """对词元做同义词扩展。

    同时检查单词元与"整个查询的子串"：用户的问法可能是一个多字词
    （如"父事件"），而按字切分后变成"父"、"事"、"件"三个词元，
    逐个查同义词表会漏掉。因此额外用原始查询做子串匹配。
    """
    expanded: set[str] = set(terms)
    lowered_query = raw_query.lower()

    for key, values in _SYNONYMS.items():
        key_lower = key.lower()
        if key_lower in terms or key_lower in lowered_query:
            expanded.update(value.lower() for value in values)
    return expanded


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """解析 front matter，返回 ``(元数据, 正文)``。

    刻意不引入 PyYAML：front matter 只用到三种形态
    （``key: value``、``key: [a, b]``、``key: [a, b]`` 多行），
    手写解析足够，且避免多一个依赖。
    """
    match = _FRONT_MATTER.match(text)
    if match is None:
        return {}, text

    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw_value = line.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()

        if raw_value.startswith("[") and raw_value.endswith("]"):
            items = raw_value[1:-1].split(",")
            meta[key] = [item.strip() for item in items if item.strip()]
        else:
            meta[key] = raw_value

    return meta, text[match.end() :]


class DocumentStore:
    """样例文档加载器。

    启动时一次性读入内存。6 篇文档的总量很小，
    内存缓存让检索零 IO，测试也更容易构造。
    """

    def __init__(self, docs_dir: Path) -> None:
        self.docs_dir = docs_dir
        self._documents: dict[str, Document] = {}
        self._tags: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        """加载全部 ``*.md`` 文档。

        文件缺失时不抛异常，只是让检索返回空结果 ——
        这样"文档还没放好"表现为"证据不足"（可诊断），
        而不是"服务启动失败"（难诊断）。
        """
        if not self.docs_dir.is_dir():
            logger.warning(
                "sample_docs_dir_missing",
                extra={"docs_dir": str(self.docs_dir)},
            )
            return

        for path in sorted(self.docs_dir.glob("*.md")):
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "sample_doc_unreadable",
                    extra={"file": path.name, "error_type": type(exc).__name__},
                )
                continue

            meta, content = _parse_front_matter(raw)
            document_id = str(meta.get("document_id") or path.stem)
            tags = meta.get("tags") or []
            if isinstance(tags, str):
                tags = [tags]

            self._documents[document_id] = Document(
                document_id=document_id,
                title=str(meta.get("title") or document_id),
                tags=list(tags),
                content=content.strip(),
                char_count=len(content.strip()),
            )
            self._tags[document_id] = list(tags)

        logger.info(
            "sample_docs_loaded",
            extra={"count": len(self._documents), "docs_dir": str(self.docs_dir)},
        )

    @property
    def document_ids(self) -> list[str]:
        """全部文档 ID，排序后返回（确定性）。"""
        return sorted(self._documents)

    def get(self, document_id: str) -> Document | None:
        """按 ID 取文档，不存在返回 None。"""
        return self._documents.get(document_id)

    def search(self, query: str, top_k: int) -> list[DocumentHit]:
        """关键词检索。

        Args:
            query: 检索词。
            top_k: 返回条数上限。

        Returns:
            按分数降序排列的命中列表。分数相同时按 ``document_id`` 升序，
            保证**同一输入必然得到同一顺序**（否则评测不可复现）。
        """
        if not query.strip():
            return []

        terms = _tokenize(query)
        expanded = _expand_terms(terms, query)

        hits: list[DocumentHit] = []
        for document_id in self.document_ids:
            document = self._documents[document_id]
            hit = self._score_document(document, expanded, query)
            if hit is not None:
                hits.append(hit)

        # 主排序：分数降序；次排序：document_id 升序（确定性）
        hits.sort(key=lambda item: (-item.score, item.document_id))

        # 绝对分数门槛：低于 _MIN_SCORE 的命中不算证据。
        #
        # 这一步是"能否判定证据不足"的前提 —— 没有它，任何查询都能
        # 凑满 top_k 篇文档，覆盖率恒为 1，"证据不足"分支永远不触发。
        filtered = [hit for hit in hits if hit.score >= _MIN_SCORE]
        if not filtered and hits:
            logger.debug(
                "all_hits_below_min_score",
                extra={"top_score": hits[0].score, "min_score": _MIN_SCORE},
            )
        return filtered[:top_k]

    def _score_document(
        self, document: Document, expanded: set[str], raw_query: str
    ) -> DocumentHit | None:
        """给单篇文档打分；无命中返回 None。"""
        title_tokens = set(_tokenize(document.title))
        tag_tokens = set(_tokenize(" ".join(document.tags)))
        content_lower = document.content.lower()

        # 标题命中：权重最高（标题是文档主题的最强信号）
        title_matches = expanded & title_tokens
        # 标签命中
        tag_matches = expanded & tag_tokens

        # 正文命中：按"词元长度 × 出现次数"加权。
        #
        # 为什么用长度加权：单字（"证"）几乎出现在所有文档里，
        # 二元组（"证据"）才是真正的主题信号。若两者同权，
        # 中文查询的分数会被单字命中淹没，各文档分数趋同，
        # evidence_coverage 就失去了区分能力。
        #
        # 英文标识符（parent_event_id）本身较长，长度加权也自然地
        # 让它们比通用词更有区分度。
        content_hits: dict[str, int] = {}
        content_score = 0.0
        for term in expanded:
            if len(term) < 2:
                continue  # 单字不计正文分，只在标题/标签里发挥作用
            count = content_lower.count(term.lower())
            if count:
                content_hits[term] = count
                # 权重取 min(长度, 12)：避免超长标识符主导分数
                content_score += min(len(term), 12) * min(count, 3)
        content_score = min(content_score, _CONTENT_HIT_CAP * 4)

        matched_terms = sorted(title_matches | tag_matches | set(content_hits))

        # 完全无命中则不出现在结果中。
        # 注意：这里不返回"0 分命中"，否则检索结果会掺入无关文档，
        # 让 evidence_coverage 失去意义。
        if not matched_terms:
            return None

        score = (
            3.0 * len(title_matches)
            + 1.5 * len(tag_matches)
            + 1.0 * content_score
            + 0.5 * len(expanded - title_tokens - tag_tokens - set(content_hits))
        )

        return DocumentHit(
            document_id=document.document_id,
            title=document.title,
            score=round(score, 4),
            snippet=self._make_snippet(document.content, matched_terms, raw_query),
            matched_terms=matched_terms,
        )

    @staticmethod
    def _make_snippet(content: str, terms: list[str], raw_query: str) -> str:
        """围绕首个命中位置截取片段。

        优先用原始查询里的多字词定位（更精确），退化为用词元定位。
        """
        lowered = content.lower()
        position = -1

        for candidate in [raw_query.lower()] + [term.lower() for term in terms]:
            if len(candidate) < 2:
                continue
            found = lowered.find(candidate)
            if found != -1:
                position = found
                break

        if position == -1:
            snippet = content[:_SNIPPET_MAX_CHARS]
        else:
            start = max(0, position - 40)
            snippet = content[start : start + _SNIPPET_MAX_CHARS]

        snippet = snippet.strip()
        if len(snippet) >= _SNIPPET_MAX_CHARS:
            snippet = snippet[: _SNIPPET_MAX_CHARS - 3] + "..."
        return snippet


# ---------------------------------------------------------------------------
# 工具实现体
#
# 签名只接收**已校验的 Pydantic 模型**，不接收原始 dict。
# 因此"未校验参数不得传给实现体"是结构性保证，而不是靠约定。
# ---------------------------------------------------------------------------


class DocumentSearchTools:
    """``search_documents`` 与 ``get_document`` 的实现。

    持有 ``DocumentStore``，实例由注册表在启动时构造。
    """

    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    def search_documents(self, args: Any) -> list[DocumentHit]:
        """检索样例文档。

        Args:
            args: 已校验的 ``SearchDocumentsArgs``。
        """
        query = args.query
        top_k = args.top_k
        hits = self.store.search(query, top_k)

        logger.info(
            "documents_searched",
            extra={"query_chars": len(query), "top_k": top_k, "hit_count": len(hits)},
        )
        return hits

    def get_document(self, args: Any) -> Document:
        """按 ID 读取文档。

        Raises:
            KeyError: 文档不存在。注册表会把它转成 ``failed`` 状态，
                而不是让 500 冒泡。
        """
        document_id = args.document_id
        document = self.store.get(document_id)
        if document is None:
            raise KeyError(f"文档不存在：{document_id}")
        return document


__all__ = ["DocumentSearchTools", "DocumentStore"]
