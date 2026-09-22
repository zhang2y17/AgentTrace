"""成本估算单测（EVALUATION §3 M8）。

这个模块只有一个真正重要的性质：**"0 美元" 与 "不知道多少钱" 必须可区分**。
把未知模型静默当成 0，得到的是一份看起来干净、实际漏算了全部成本的报告 ——
而且没有任何迹象提示它漏了。

其余测试围绕两条易错路径：``gpt-4o`` / ``gpt-4o-mini`` 的精确匹配优先，
以及用 ``Decimal`` 而非 ``float`` 做金额运算。
"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.evaluation import pricing as pricing_module
from app.evaluation.pricing import (
    PricingTable,
    estimate_cost,
    quantize_cost,
)

TABLE = PricingTable(
    models={
        "gpt-4o": {"input_per_1m": 2.50, "output_per_1m": 10.00},
        "gpt-4o-mini": {"input_per_1m": 0.15, "output_per_1m": 0.60},
        "free-model": {"input_per_1m": 0.0, "output_per_1m": 0.0},
    },
    prefixes={"ollama/": {"input_per_1m": 0.0, "output_per_1m": 0.0}},
    version="test-2026-01",
)


class TestCostFormula:
    def test_one_million_prompt_tokens_costs_input_price(self) -> None:
        """公式基准：1M 输入 token 恰等于表里的单价。"""
        result = estimate_cost(
            model_name="gpt-4o", prompt_tokens=1_000_000, completion_tokens=0, table=TABLE
        )
        assert result.cost_usd == Decimal("2.500000")

    def test_input_and_output_are_summed(self) -> None:
        result = estimate_cost(
            model_name="gpt-4o", prompt_tokens=1_000_000, completion_tokens=1_000_000, table=TABLE
        )
        assert result.cost_usd == Decimal("12.500000")

    def test_zero_tokens_costs_zero_but_is_available(self) -> None:
        """0 token 是"确实不花钱"，与"不知道价格"不是一回事。"""
        result = estimate_cost(
            model_name="gpt-4o", prompt_tokens=0, completion_tokens=0, table=TABLE
        )
        assert result.cost_usd == Decimal("0.000000")
        assert result.unavailable is False

    def test_negative_tokens_raise(self) -> None:
        with pytest.raises(ValueError):
            estimate_cost(model_name="gpt-4o", prompt_tokens=-1, completion_tokens=0, table=TABLE)


class TestDecimalPrecision:
    def test_no_float_tail_error(self) -> None:
        """0.15/1M 这类价格用 ``float`` 累加会留下 ``...004`` 的尾差。

        金额是要展示给用户看的数字，尾差会被直接读成"算错了"。
        """
        result = estimate_cost(
            model_name="gpt-4o-mini",
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            table=TABLE,
        )
        assert result.cost_usd == Decimal("0.750000")

    def test_normalize_entry_avoids_binary_representation_error(self) -> None:
        """``Decimal(0.15)`` 会得到 ``0.1499999...``，因此必须用 ``str()`` 中转。"""
        local = PricingTable(models={"m": {"input_per_1m": 0.15, "output_per_1m": 0}})
        assert local.models["m"]["input_per_1m"] == Decimal("0.15")

    def test_quantize_rounds_half_up(self) -> None:
        assert quantize_cost(Decimal("0.0000005")) == Decimal("0.000001")
        assert quantize_cost(Decimal("0.0000004")) == Decimal("0.000000")

    def test_quantize_accepts_plain_number(self) -> None:
        assert quantize_cost(0.5) == Decimal("0.500000")  # type: ignore[arg-type]

    def test_to_float_returns_six_places(self) -> None:
        result = estimate_cost(
            model_name="gpt-4o", prompt_tokens=1000, completion_tokens=1000, table=TABLE
        )
        assert result.to_float() == 0.0125


class TestLookupPrecedence:
    def test_exact_match_wins_over_prefix(self) -> None:
        """先精确、后前缀。

        反过来会让 ``gpt-4o-mini`` 命中 ``gpt-4o`` 的价 ——
        而 mini 比 4o 便宜近 20 倍，这个错会静默放大成本估算。
        """
        entry, kind = TABLE.lookup("gpt-4o-mini")
        assert kind == "exact"
        assert entry == {"input_per_1m": Decimal("0.15"), "output_per_1m": Decimal("0.60")}

    def test_prefix_match_reports_kind(self) -> None:
        entry, kind = TABLE.lookup("ollama/llama3:8b")
        assert kind == "prefix"
        assert entry is not None

    def test_longest_prefix_wins(self) -> None:
        local = PricingTable(
            models={},
            prefixes={
                "ollama/": {"input_per_1m": 1.0, "output_per_1m": 1.0},
                "ollama/llama3": {"input_per_1m": 0.0, "output_per_1m": 0.0},
            },
        )
        _, kind = local.lookup("ollama/llama3:8b")
        entry, kind = local.lookup("ollama/llama3:8b")
        assert kind == "prefix"
        assert entry == {"input_per_1m": Decimal("0"), "output_per_1m": Decimal("0")}

    def test_unknown_model_returns_none(self) -> None:
        entry, kind = TABLE.lookup("some-unreleased-model")
        assert entry is None
        assert kind == "unknown"


class TestUnknownModelIsNotZero:
    def test_unavailable_flag_is_set(self) -> None:
        """本文件的核心断言。

        未知模型 → ``cost_usd == 0`` 且 ``unavailable is True``。
        只返回 float 的实现无法区分"免费"与"不知道"，调用方会
        把漏算读成"这批调用不要钱"。
        """
        result = estimate_cost(
            model_name="ghost-model", prompt_tokens=100_000, completion_tokens=50_000, table=TABLE
        )
        assert result.cost_usd == Decimal("0")
        assert result.unavailable is True
        assert result.reason and "ghost-model" in result.reason

    def test_unknown_model_still_reports_pricing_version(self) -> None:
        """版本号要跟着走 —— 否则无法判断"当时用的是哪版价目表"。"""
        result = estimate_cost(
            model_name="ghost-model", prompt_tokens=1, completion_tokens=1, table=TABLE
        )
        assert result.pricing_version == "test-2026-01"

    def test_free_model_is_available_not_unavailable(self) -> None:
        """免费模型与未知模型数值相同、语义相反。"""
        result = estimate_cost(
            model_name="free-model", prompt_tokens=1_000_000, completion_tokens=0, table=TABLE
        )
        assert result.cost_usd == Decimal("0")
        assert result.unavailable is False

    def test_prefix_hit_is_available(self) -> None:
        """``ollama/*`` 命中是"本地推理免费"，不是"不可估算"。"""
        result = estimate_cost(
            model_name="ollama/llama3:8b",
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            table=TABLE,
        )
        assert result.unavailable is False
        assert result.reason == "matched by prefix"


class TestTableLoading:
    def test_loads_from_file(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.json"
        path.write_text(
            json.dumps(
                {
                    "_updated": "2026-01-01",
                    "models": {"m": {"input_per_1m": 1.0, "output_per_1m": 2.0}},
                    "prefixes": {},
                }
            ),
            encoding="utf-8",
        )
        table = PricingTable.load(path)
        assert table.version == "2026-01-01"
        assert table.lookup("m")[0] == {
            "input_per_1m": Decimal("1.0"),
            "output_per_1m": Decimal("2.0"),
        }

    def test_missing_file_falls_back_with_audible_source(self, tmp_path: Path) -> None:
        """价目表读不到不该让评测挂掉，但必须留下可追溯的痕迹。"""
        table = PricingTable.load(tmp_path / "does-not-exist.json")
        assert "fallback" in table.source
        assert table.lookup("gpt-4o")[0] is not None

    def test_corrupt_file_falls_back(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.json"
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        table = PricingTable.load(path)
        assert "fallback" in table.source

    def test_missing_sections_default_to_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.json"
        path.write_text("{}", encoding="utf-8")
        table = PricingTable.load(path)
        assert table.lookup("anything") == (None, "unknown")

    def test_partial_entry_defaults_missing_price_to_zero(self) -> None:
        """只写了输入价时输出价按 0 处理 —— 而不是让 ``KeyError`` 冒到评测层。"""
        table = PricingTable(models={"m": {"input_per_1m": 1.0}})
        assert table.models["m"]["output_per_1m"] == Decimal("0")


class TestShippedPricingTable:
    def test_shipped_table_loads_without_fallback(self) -> None:
        """随包价目表必须能被正常读出。

        走兜底表不会报错，但会静默丢掉真实价格 —— 因此单独盯一下。
        """
        table = PricingTable.load()
        assert "fallback" not in table.source, f"价目表退化为兜底：{table.source}"
        assert table.version != "unknown", "价目表缺 _updated 版本标签"

    def test_shipped_table_covers_the_configured_default_model(self) -> None:
        """默认模型必须能查到价，否则每次评测都会报 unavailable。"""
        from app.core.config import get_settings

        settings = get_settings()
        table = PricingTable.load()
        entry, kind = table.lookup(settings.llm_model)
        assert entry is not None, f"默认模型 {settings.llm_model!r} 不在价目表中（匹配方式 {kind}）"

    def test_default_cost_estimation_path_is_available(self) -> None:
        """端到端：默认模型 + 默认价目表，估算必须可用。"""
        from app.core.config import get_settings

        result = estimate_cost(
            model_name=get_settings().llm_model,
            prompt_tokens=1000,
            completion_tokens=1000,
        )
        assert result.unavailable is False
        assert result.pricing_version != "unknown"


class TestModuleSurface:
    def test_get_pricing_table_is_not_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """每次重新读文件 —— 否则测试替换价目表会失效，
        生产中也无法在不重启的情况更新价格。
        """
        calls: list[object] = []
        original = PricingTable.load

        def _spy(path: object = None) -> PricingTable:
            calls.append(path)
            return original(path)  # type: ignore[arg-type]

        monkeypatch.setattr(pricing_module.PricingTable, "load", staticmethod(_spy))
        pricing_module.get_pricing_table()
        pricing_module.get_pricing_table()
        assert len(calls) == 2


class TestCostEstimateImmutability:
    def test_estimate_is_frozen(self) -> None:
        """结果对象不该被下游改写 —— 一个被改过的成本数字无法追溯。"""
        result = estimate_cost(
            model_name="gpt-4o", prompt_tokens=1, completion_tokens=1, table=TABLE
        )
        # frozen dataclass 抛的是 FrozenInstanceError，它是 AttributeError 的子类；
        # 这里断言具体异常而非盲捕 Exception，避免把无关错误也当成"已冻结"。
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.cost_usd = Decimal("999")  # type: ignore[misc]
