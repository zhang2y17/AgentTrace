"""评测集加载与校验单测（EVALUATION §1）。

重点在**报错质量**上：评测集是手写的 JSONL，一行写错就要能直接定位到
第几行、哪个字段。如果只报"schema 校验失败"，使用者面对几十行 JSON
只能肉眼逐行比对 —— 而这份数据集会随版本增长。

第二类重点是**未知字段必须报错而不是忽略**：静默忽略会把
``expect_sucess``（拼错的 ``expect_success``）变成一个
"看起来跑过了、实际没验证"的评测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.errors import DatasetNotFoundError, DatasetValidationError
from app.evaluation.dataset import (
    EvalDataset,
    dataset_path,
    load_dataset,
    validate_dataset,
)


def _write_jsonl(tmp_path: Path, name: str, cases: list[object]) -> Path:
    """把 case 列表写成 JSONL 文件，返回文件路径。"""
    lines: list[str] = []
    for case in cases:
        lines.append(case if isinstance(case, str) else json.dumps(case, ensure_ascii=False))
    path = tmp_path / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _minimal_case(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "case_key": "case-001",
        "question": "LangGraph 的状态如何持久化？",
        "expected_tools": ["search_documents", "get_document"],
        "required_assertions": ["contains_citation"],
    }
    base.update(overrides)
    return base


class TestLoadHappyPath:
    def test_loads_all_cases(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path, "ds", [_minimal_case(case_key=f"case-{i:03d}") for i in range(3)])
        dataset = load_dataset("ds", base_dir=tmp_path)
        assert dataset.case_count == 3
        assert dataset.dataset_version == "ds"

    def test_skips_blank_lines_and_comments(self, tmp_path: Path) -> None:
        """空行与 ``#`` 注释是 JSONL 的常见便利写法，容忍它们不掩盖错误。"""
        path = tmp_path / "ds.jsonl"
        path.write_text(
            "# 这是文件头注释\n"
            "\n"
            + json.dumps(_minimal_case(), ensure_ascii=False)
            + "\n"
            "   \n"
            "# 中间注释\n",
            encoding="utf-8",
        )
        assert load_dataset("ds", base_dir=tmp_path).case_count == 1

    def test_line_number_records_physical_line(self, tmp_path: Path) -> None:
        """注释行也占行号 —— 报错时的行号要能直接跳转，不能是"第几个 case"。"""
        path = tmp_path / "ds.jsonl"
        path.write_text(
            "# 头注释\n" + json.dumps(_minimal_case(), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        dataset = load_dataset("ds", base_dir=tmp_path)
        assert dataset.cases[0].line_number == 2

    def test_optional_fields_have_defaults(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path, "ds", [_minimal_case()])
        case = load_dataset("ds", base_dir=tmp_path).cases[0]
        assert case.required_citations == 0
        assert case.expect_success is True
        assert case.tags == []
        assert case.expected_arguments is None

    def test_validate_dataset_is_load_only(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path, "ds", [_minimal_case()])
        assert validate_dataset("ds", base_dir=tmp_path).case_count == 1


class TestMissingFile:
    def test_raises_dataset_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(DatasetNotFoundError) as exc:
            load_dataset("nope", base_dir=tmp_path)
        assert exc.value.details["dataset_version"] == "nope"
        assert "expected_path" in exc.value.details

    def test_details_carry_the_path_that_was_checked(self, tmp_path: Path) -> None:
        """把"我找的是哪个路径"写进 details —— 目录配错时一眼可见。"""
        with pytest.raises(DatasetNotFoundError) as exc:
            load_dataset("nope", base_dir=tmp_path)
        assert str(tmp_path) in exc.value.details["expected_path"]


class TestPathTraversal:
    @pytest.mark.parametrize(
        "bad_version",
        ["../outside", "..\\outside", "sub/../../outside"],
    )
    def test_rejects_escaping_base_dir(self, tmp_path: Path, bad_version: str) -> None:
        """``dataset_version`` 直接来自请求体，必须挡住路径穿越。"""
        with pytest.raises(DatasetValidationError) as exc:
            dataset_path(bad_version, base_dir=tmp_path)
        assert exc.value.details["dataset_version"] == bad_version

    def test_plain_name_resolves_inside_base_dir(self, tmp_path: Path) -> None:
        resolved = dataset_path("ds", base_dir=tmp_path)
        assert resolved == (tmp_path / "ds.jsonl").resolve()


class TestCaseValidation:
    def test_missing_required_field_reports_field_and_line(self, tmp_path: Path) -> None:
        case = _minimal_case()
        del case["question"]
        _write_jsonl(tmp_path, "ds", [case])
        with pytest.raises(DatasetValidationError) as exc:
            load_dataset("ds", base_dir=tmp_path)
        errors = exc.value.details["errors"]
        assert errors[0]["line"] == 1
        assert errors[0]["field"] == "question"

    def test_unknown_field_is_rejected_not_ignored(self, tmp_path: Path) -> None:
        """拼错的字段名必须报错。

        ``expect_sucess``（少一个 c）若被静默忽略，这条 case 的
        ``expect_success`` 就退回默认 True —— 使用者以为验了失败路径，
        实际验了个恒真的东西。
        """
        _write_jsonl(tmp_path, "ds", [_minimal_case(expect_sucess=False)])
        with pytest.raises(DatasetValidationError) as exc:
            load_dataset("ds", base_dir=tmp_path)
        assert "expect_sucess" in str(exc.value.details["errors"])

    def test_invalid_json_reports_line_and_column(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path, "ds", ["{不是合法 JSON"])
        with pytest.raises(DatasetValidationError) as exc:
            load_dataset("ds", base_dir=tmp_path)
        error = exc.value.details["errors"][0]
        assert error["line"] == 1
        assert "column" in error

    def test_non_object_line_is_rejected(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path, "ds", ["[1, 2, 3]"])
        with pytest.raises(DatasetValidationError) as exc:
            load_dataset("ds", base_dir=tmp_path)
        assert "JSON 对象" in str(exc.value.details["errors"])

    def test_duplicate_case_key_is_rejected_with_both_lines(self, tmp_path: Path) -> None:
        """重复 key 会让"跨版本对齐 case"失效 —— 取到哪个取决于遍历顺序。"""
        _write_jsonl(tmp_path, "ds", [_minimal_case(), _minimal_case()])
        with pytest.raises(DatasetValidationError) as exc:
            load_dataset("ds", base_dir=tmp_path)
        message = str(exc.value.details["errors"])
        assert "重复" in message
        assert "第 1 行" in message, "应指出首次出现的行号"

    def test_empty_dataset_is_rejected(self, tmp_path: Path) -> None:
        """只有注释、没有 case 的文件不算合法数据集。"""
        path = tmp_path / "ds.jsonl"
        path.write_text("# 只有注释\n", encoding="utf-8")
        with pytest.raises(DatasetValidationError) as exc:
            load_dataset("ds", base_dir=tmp_path)
        assert "不含任何用例" in str(exc.value)

    def test_error_count_and_truncation_to_ten(self, tmp_path: Path) -> None:
        """错误多到几十条时响应体会长得无法阅读，只回传前 10 条。"""
        _write_jsonl(tmp_path, "ds", [_minimal_case(case_key="")] * 25)
        with pytest.raises(DatasetValidationError) as exc:
            load_dataset("ds", base_dir=tmp_path)
        assert len(exc.value.details["errors"]) == 10
        assert exc.value.details["error_count"] > 10


class TestFieldTypeValidation:
    @pytest.mark.parametrize(
        ("field", "value", "needle"),
        [
            ("case_key", "", "非空字符串"),
            ("case_key", "x" * 81, "长度不得超过"),
            ("question", "   ", "非空字符串"),
            ("expected_tools", "search_documents", "字符串数组"),
            ("expected_tools", [1, 2], "字符串数组"),
            ("required_assertions", {"a": 1}, "字符串数组"),
            ("tags", [1], "字符串数组"),
            ("expected_arguments", ["search_documents"], "必须是对象"),
            ("required_citations", -1, "不得为负数"),
            ("required_citations", "3", "必须是整数"),
            ("expect_success", "true", "必须是布尔值"),
        ],
    )
    def test_type_errors_are_reported(
        self, tmp_path: Path, field: str, value: object, needle: str
    ) -> None:
        _write_jsonl(tmp_path, "ds", [_minimal_case(**{field: value})])
        with pytest.raises(DatasetValidationError) as exc:
            load_dataset("ds", base_dir=tmp_path)
        errors = exc.value.details["errors"]
        assert any(field == err.get("field") for err in errors)
        assert needle in str(errors)

    def test_bool_is_not_accepted_as_required_citations(self, tmp_path: Path) -> None:
        """``isinstance(True, int)`` 为真，所以必须单独排除 bool。

        否则 ``"required_citations": true`` 会被当成 1 ——
        而写出 ``true`` 的人显然想表达别的东西。
        """
        _write_jsonl(tmp_path, "ds", [_minimal_case(required_citations=True)])
        with pytest.raises(DatasetValidationError) as exc:
            load_dataset("ds", base_dir=tmp_path)
        assert "必须是整数" in str(exc.value.details["errors"])

    def test_empty_expected_arguments_is_rejected(self, tmp_path: Path) -> None:
        """空参数期望集只有一个效果：让人以为验了参数。"""
        _write_jsonl(tmp_path, "ds", [_minimal_case(expected_arguments={"t": {}})])
        with pytest.raises(DatasetValidationError) as exc:
            load_dataset("ds", base_dir=tmp_path)
        assert "参数期望为空对象" in str(exc.value.details["errors"])

    def test_non_dict_tool_expectation_is_rejected(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path, "ds", [_minimal_case(expected_arguments={"t": 5})])
        with pytest.raises(DatasetValidationError) as exc:
            load_dataset("ds", base_dir=tmp_path)
        assert "必须是对象" in str(exc.value.details["errors"])

    def test_multiple_errors_in_one_case_are_all_reported(self, tmp_path: Path) -> None:
        """一次报全，使用者可以一次改完而不必重跑多轮。"""
        _write_jsonl(
            tmp_path, "ds", [_minimal_case(case_key="", expected_tools="not-a-list")]
        )
        with pytest.raises(DatasetValidationError) as exc:
            load_dataset("ds", base_dir=tmp_path)
        fields = {err.get("field") for err in exc.value.details["errors"]}
        assert {"case_key", "expected_tools"} <= fields


class TestDatasetSelect:
    def _dataset(self, tmp_path: Path) -> EvalDataset:
        _write_jsonl(
            tmp_path,
            "ds",
            [_minimal_case(case_key=f"case-{i:03d}") for i in range(4)],
        )
        return load_dataset("ds", base_dir=tmp_path)

    def test_none_returns_whole_dataset(self, tmp_path: Path) -> None:
        dataset = self._dataset(tmp_path)
        assert dataset.select(None).case_count == 4

    def test_empty_list_returns_whole_dataset(self, tmp_path: Path) -> None:
        dataset = self._dataset(tmp_path)
        assert dataset.select([]).case_count == 4

    def test_selects_subset_preserving_original_order(self, tmp_path: Path) -> None:
        """输出顺序跟随数据集内的顺序，不跟随 ``case_keys`` 的传入顺序。

        否则同一份 case_keys 的两次请求可能产出顺序不同的报告，
        让"先跑 A 还是先跑 B"变成一份隐形的输入。
        """
        dataset = self._dataset(tmp_path)
        selected = dataset.select(["case-003", "case-000"])
        assert [case.case_key for case in selected.cases] == ["case-000", "case-003"]

    def test_unknown_key_raises_with_available_keys(self, tmp_path: Path) -> None:
        dataset = self._dataset(tmp_path)
        with pytest.raises(DatasetValidationError) as exc:
            dataset.select(["case-999"])
        assert exc.value.details["unknown_case_keys"] == ["case-999"]
        assert "case-000" in exc.value.details["available_case_keys"]

    def test_reports_all_unknown_keys_at_once(self, tmp_path: Path) -> None:
        """一次报全未知键，使用者改一次即可。"""
        dataset = self._dataset(tmp_path)
        with pytest.raises(DatasetValidationError) as exc:
            dataset.select(["a", "b", "case-001"])
        assert exc.value.details["unknown_case_keys"] == ["a", "b"]

    def test_by_key_returns_none_for_unknown(self, tmp_path: Path) -> None:
        dataset = self._dataset(tmp_path)
        assert dataset.by_key("case-001") is not None
        assert dataset.by_key("nope") is None

    def test_has_tool_expectation_reflects_empty_list(self, tmp_path: Path) -> None:
        """``expected_tools`` 为空的 case 不参与 M3 —— 这个属性是那个判断的入口。"""
        _write_jsonl(tmp_path, "ds", [_minimal_case(expected_tools=[])])
        assert load_dataset("ds", base_dir=tmp_path).cases[0].has_tool_expectation is False


class TestShippedDataset:
    """对随包评测集本身的校验 —— 它必须能通过自己的校验器。"""

    def test_default_dataset_loads(self) -> None:
        dataset = load_dataset("doc_research_v1")
        assert dataset.case_count >= 10, "评测集规模应足以支撑分位数统计"

    def test_every_case_asserts_at_least_one_thing(self) -> None:
        """一个不断言任何东西的 case 只会贡献延迟数字，不贡献质量信号。

        更糟的是它会把 "case_count" 撑大，让通过率看起来更有代表性。
        """
        dataset = load_dataset("doc_research_v1")
        empty = [case.case_key for case in dataset.cases if not case.required_assertions]
        assert empty == [], f"以下 case 没有断言：{empty}"

    def test_case_keys_are_unique_and_stable_named(self) -> None:
        dataset = load_dataset("doc_research_v1")
        keys = [case.case_key for case in dataset.cases]
        assert len(keys) == len(set(keys))
        assert all(key == key.strip() for key in keys)

    def test_every_case_has_tool_expectation(self) -> None:
        """这批 case 全部走文档检索链路，因此都该参与 M3。"""
        dataset = load_dataset("doc_research_v1")
        missing = [case.case_key for case in dataset.cases if not case.has_tool_expectation]
        assert missing == [], f"以下 case 缺 expected_tools：{missing}"
