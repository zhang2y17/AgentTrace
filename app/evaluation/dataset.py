"""评测集加载与 schema 校验。

契约 EVALUATION §1 定义了 case 的字段与语义，本模块负责把
``data/eval/<dataset_version>.jsonl`` 读成 ``EvalCaseSpec`` 列表。

**为什么校验错误必须带行号**

这是本模块最重要的设计约束。评测集是手写的 JSONL，写错一个字段名
（``expect_sucesss``）或漏一个逗号都会让某一行失效。如果只报
"schema 校验失败"，使用者面对 12 行 JSON 只能逐行肉眼比对 ——
而这个数据集会随版本增长。

因此所有错误都带上 ``line``（1-based 行号）与 ``field``，
直接指向要改的那一处。

**为什么用 dataclass 而不是直接建 ORM 行**

加载与入库是两件事：前者是"文件是否合法"，后者是"评测是否开始"。
把它们揉在一起会让"只想校验一下评测集"这种廉价操作也去连数据库。
``runner`` 负责在真正开始评测时把 spec 落成 ``eval_case`` 行。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.errors import DatasetNotFoundError, DatasetValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

# case_key 的格式约束：小写字母/数字/连字符，便于用作稳定 ID。
# 不强制这个格式也不会出错，但稳定的命名让"跨版本对齐 case"可靠得多。
_CASE_KEY_PATTERN_MAX = 80

# 单个 case 的必备字段。缺任何一个都无法执行该 case。
_REQUIRED_FIELDS = ("case_key", "question", "expected_tools", "required_assertions")

# 允许出现在 JSONL 里的字段全集。未知字段一律报错而不是忽略 ——
# 静默忽略会让 "expect_success" 拼错成 "expect_sucess" 这种事
# 变成一个"看起来跑过了、实际没验证"的评测。
_KNOWN_FIELDS = frozenset(
    {
        "case_key",
        "question",
        "expected_tools",
        "expected_arguments",
        "required_assertions",
        "required_citations",
        "expect_success",
        "tags",
    }
)


@dataclass(slots=True)
class EvalCaseSpec:
    """一个评测 case 的不可变描述。

    Attributes:
        case_key: 稳定唯一 ID。
        question: 输入问题。
        expected_tools: 期望工具序列（**有序**，顺序参与 M3 判定）。
        expected_arguments: ``{tool_name: {arg_name: expected_value}}``。
        required_assertions: 关键断言名（见 ``assertions.py``）。
        required_citations: 最少引用数，默认 0。
        expect_success: 期望 run 终态是否为 ``succeeded``，默认 True。
        tags: 分类标签。
        line_number: 在 JSONL 中的行号（1-based），仅用于报错定位。
    """

    case_key: str
    question: str
    expected_tools: list[str] = field(default_factory=list)
    expected_arguments: dict[str, dict[str, Any]] | None = None
    required_assertions: list[str] = field(default_factory=list)
    required_citations: int = 0
    expect_success: bool = True
    tags: list[str] = field(default_factory=list)
    line_number: int = 0

    @property
    def has_tool_expectation(self) -> bool:
        """是否参与 M3 工具选择准确率。

        ``expected_tools`` 为空的 case 不参与该指标（EVALUATION §3 M3 分母说明）。
        """
        return bool(self.expected_tools)


@dataclass(slots=True)
class EvalDataset:
    """一份已校验的评测集。"""

    dataset_version: str
    cases: list[EvalCaseSpec]
    path: Path

    @property
    def case_count(self) -> int:
        return len(self.cases)

    def __iter__(self) -> Iterator[EvalCaseSpec]:
        """让 ``EvalDataset`` 可直接迭代。

        调用方已有两条路径（``for case in dataset`` 与
        ``for case in dataset.cases``），只提供后者会诱使大家
        去记 ``.cases`` 这个内部字段名 —— 而把它改成别的名字时
        就是一次静默的破坏性变更。
        """
        return iter(self.cases)

    def __len__(self) -> int:
        return len(self.cases)

    def by_key(self, case_key: str) -> EvalCaseSpec | None:
        """按 case_key 查 case；不存在返回 None。"""
        for case in self.cases:
            if case.case_key == case_key:
                return case
        return None

    def select(self, case_keys: list[str] | None) -> EvalDataset:
        """按 case_keys 取子集。

        Args:
            case_keys: 要保留的 case_key；``None`` 或空列表表示全选。

        Raises:
            DatasetValidationError: 含未知 case_key。
        """
        if not case_keys:
            return self

        known = {case.case_key for case in self.cases}
        unknown = [key for key in case_keys if key not in known]
        if unknown:
            # 契约 API_CONTRACT §6：case_keys 含未知用例 → 400 INVALID_ARGUMENT。
            # 这里报出全部未知键而不是第一个 —— 使用者可以一次改完。
            raise DatasetValidationError(
                f"case_keys 含未知用例：{sorted(unknown)}。",
                details={
                    "unknown_case_keys": sorted(unknown),
                    "available_case_keys": sorted(known),
                    "dataset_version": self.dataset_version,
                },
            )

        wanted = set(case_keys)
        return EvalDataset(
            dataset_version=self.dataset_version,
            cases=[case for case in self.cases if case.case_key in wanted],
            path=self.path,
        )


def dataset_path(dataset_version: str, *, base_dir: str | Path | None = None) -> Path:
    """解析评测集文件路径，并挡住路径穿越。"""
    if base_dir is None:
        from app.core.config import get_settings

        base_dir = get_settings().eval_dataset_dir

    root = Path(base_dir)
    candidate = (root / f"{dataset_version}.jsonl").resolve()

    # dataset_version 直接来自请求体，必须挡 ``../../etc/passwd`` 这类输入。
    # 评测集目录之外的任何文件都不是合法数据集。
    try:
        candidate.relative_to(Path(root).resolve())
    except ValueError as exc:
        raise DatasetValidationError(
            f"dataset_version 不合法：{dataset_version!r} 指向评测集目录之外。",
            details={"dataset_version": dataset_version},
        ) from exc

    return candidate


def load_dataset(
    dataset_version: str,
    *,
    base_dir: str | Path | None = None,
) -> EvalDataset:
    """加载并校验一份评测集。

    Args:
        dataset_version: 数据集版本名（对应 ``<version>.jsonl``）。
        base_dir: 评测集目录；默认取配置 ``EVAL_DATASET_DIR``。

    Returns:
        已校验的 ``EvalDataset``。

    Raises:
        DatasetNotFoundError: 文件不存在（404 EVALUATION_NOT_FOUND）。
        DatasetValidationError: 内容不合法（400 INVALID_ARGUMENT，带行号）。
    """
    path = dataset_path(dataset_version, base_dir=base_dir)
    if not path.is_file():
        raise DatasetNotFoundError(
            f"评测集不存在：{dataset_version}。",
            details={
                "dataset_version": dataset_version,
                "expected_path": str(path),
            },
        )

    cases: list[EvalCaseSpec] = []
    seen_keys: dict[str, int] = {}
    errors: list[dict[str, Any]] = []

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        # 空行与 # 注释是 JSONL 的常见便利写法，容忍它们不会掩盖错误。
        if not stripped or stripped.startswith("#"):
            continue

        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            errors.append(
                {
                    "line": line_number,
                    "field": None,
                    "error": f"JSON 解析失败：{exc.msg}",
                    "column": exc.colno,
                }
            )
            continue

        if not isinstance(payload, dict):
            errors.append(
                {
                    "line": line_number,
                    "field": None,
                    "error": f"每行必须是一个 JSON 对象，实际是 {type(payload).__name__}。",
                }
            )
            continue

        case, case_errors = _parse_case(payload, line_number)
        errors.extend(case_errors)
        if case is None:
            continue

        # 重复 case_key 会让"跨版本对齐"失去意义：
        # 两行同 key 时，取到的究竟是哪一个取决于遍历顺序。
        if case.case_key in seen_keys:
            errors.append(
                {
                    "line": line_number,
                    "field": "case_key",
                    "error": f"case_key 重复：{case.case_key!r}（首次出现在第 {seen_keys[case.case_key]} 行）。",
                }
            )
            continue

        seen_keys[case.case_key] = line_number
        cases.append(case)

    if errors:
        raise DatasetValidationError(
            f"评测集 {dataset_version} 校验失败，共 {len(errors)} 处问题。",
            details={
                "dataset_version": dataset_version,
                "path": str(path),
                "error_count": len(errors),
                # 只回传前 10 条：错误多到几十条时，响应体会长得无法阅读，
                # 而使用者改完前几条后重跑即可看到后面的。
                "errors": errors[:10],
            },
        )

    if not cases:
        raise DatasetValidationError(
            f"评测集 {dataset_version} 不含任何用例。",
            details={"dataset_version": dataset_version, "path": str(path)},
        )

    logger.info(
        "eval_dataset_loaded",
        extra={
            "dataset_version": dataset_version,
            "case_count": len(cases),
            "path": str(path),
        },
    )

    return EvalDataset(dataset_version=dataset_version, cases=cases, path=path)


def _parse_case(  # noqa: C901 —— 逐字段校验天然是长函数，拆开反而更难对照 schema 阅读
    payload: dict[str, Any], line_number: int
) -> tuple[EvalCaseSpec | None, list[dict[str, Any]]]:
    """校验单行 JSON，返回 ``(case, errors)``。"""
    errors: list[dict[str, Any]] = []

    def _error(field_name: str, message: str) -> None:
        errors.append({"line": line_number, "field": field_name, "error": message})

    unknown = sorted(set(payload) - _KNOWN_FIELDS)
    if unknown:
        _error(
            None,
            f"含未知字段 {unknown}。允许的字段：{sorted(_KNOWN_FIELDS)}。",
        )

    for required in _REQUIRED_FIELDS:
        if required not in payload:
            _error(required, f"缺少必填字段 {required!r}。")

    if errors:
        return None, errors

    case_key = payload["case_key"]
    if not isinstance(case_key, str) or not case_key.strip():
        _error("case_key", "必须是非空字符串。")
    elif len(case_key) > _CASE_KEY_PATTERN_MAX:
        _error("case_key", f"长度不得超过 {_CASE_KEY_PATTERN_MAX}。")

    question = payload["question"]
    if not isinstance(question, str) or not question.strip():
        _error("question", "必须是非空字符串。")

    expected_tools = payload["expected_tools"]
    if not _is_str_list(expected_tools):
        _error("expected_tools", "必须是字符串数组（可为空数组）。")

    required_assertions = payload["required_assertions"]
    if not _is_str_list(required_assertions):
        _error("required_assertions", "必须是字符串数组（可为空数组）。")

    expected_arguments = payload.get("expected_arguments")
    if expected_arguments is not None:
        if not isinstance(expected_arguments, dict):
            _error("expected_arguments", "必须是对象：{tool_name: {arg_name: value}}。")
        else:
            for tool_name, arg_expectations in expected_arguments.items():
                if not isinstance(arg_expectations, dict):
                    _error(
                        "expected_arguments",
                        f"{tool_name!r} 的值必须是对象：{{arg_name: expected_value}}。",
                    )
                elif not arg_expectations:
                    # 空期望集会让 M4 的分子分母都不计该对，
                    # 写出来只有一个效果：让人以为验了参数。
                    _error(
                        "expected_arguments",
                        f"{tool_name!r} 的参数期望为空对象；请删除该键以免误读。",
                    )

    required_citations = payload.get("required_citations", 0)
    if not isinstance(required_citations, int) or isinstance(required_citations, bool):
        _error("required_citations", "必须是整数。")
    elif required_citations < 0:
        _error("required_citations", "不得为负数。")

    expect_success = payload.get("expect_success", True)
    if not isinstance(expect_success, bool):
        _error("expect_success", "必须是布尔值。")

    tags = payload.get("tags", [])
    if not _is_str_list(tags):
        _error("tags", "必须是字符串数组。")

    if errors:
        return None, errors

    return (
        EvalCaseSpec(
            case_key=case_key,
            question=question,
            expected_tools=list(expected_tools),
            expected_arguments=(
                {k: dict(v) for k, v in expected_arguments.items()} if expected_arguments else None
            ),
            required_assertions=list(required_assertions),
            required_citations=int(required_citations),
            expect_success=bool(expect_success),
            tags=list(tags),
            line_number=line_number,
        ),
        [],
    )


def _is_str_list(value: Any) -> bool:
    """判断是否为字符串列表（bool 不是 int 意义上的可接受标量，这里只管字符串）。"""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_dataset(dataset_version: str, *, base_dir: str | Path | None = None) -> EvalDataset:
    """只校验不入库的便捷入口（供脚本与 CI 使用）。"""
    return load_dataset(dataset_version, base_dir=base_dir)


__all__ = [
    "EvalCaseSpec",
    "EvalDataset",
    "dataset_path",
    "load_dataset",
    "validate_dataset",
]
