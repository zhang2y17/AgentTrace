r"""成本估算（契约 EVALUATION §3 M8）。

$$\text{cost} = \frac{\text{prompt\_tokens}}{10^6} \cdot P_{in}
+ \frac{\text{completion\_tokens}}{10^6} \cdot P_{out}$$

**三条必须声明的限制**（EVALUATION §3 M8，本模块用代码而非注释来落实）：

1. 价目表是**硬编码的参考价**，会过时 —— 每次估算结果的
   ``pricing_version`` 会带上表里的版本号，让读者知道用的是哪一版；
2. 估算值**不等于真实账单**（不含缓存命中折扣、批处理折扣、阶梯价）；
3. 未知模型名 → 成本记 0 **并且**返回
   ``cost_estimation_unavailable = True``，**绝不静默当 0 处理**。
   这是第 3 条被放在返回结构里的原因：如果只返回一个 float，
   调用方无法区分"真的不花钱"与"我不知道多少钱"。

计算全程用 ``Decimal`` 而不是 ``float``：浮点累加会产生
``0.30000000000000004`` 这类尾差，而金额是最终要展示给用户看的数字。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# 金额小数位。契约 API_CONTRACT §6 示例用 6 位（0.002244）。
_COST_QUANTUM = Decimal("0.000001")

# 1M tokens 的除数
_TOKENS_PER_UNIT = Decimal("1000000")

# 内置兜底价目表。正常路径从 pricing.json 读；文件缺失/损坏时用这份，
# 保证"成本估算不可用"不会连带让整个评测挂掉 ——
# 但会记 warning 并在结果里标注用了兜底表。
_FALLBACK_TABLE: dict[str, Any] = {
    "_units": "USD per 1,000,000 tokens",
    "_updated": "unknown",
    "models": {
        "fake-model": {"input_per_1m": 0.0, "output_per_1m": 0.0},
        "gpt-4o-mini": {"input_per_1m": 0.15, "output_per_1m": 0.60},
        "gpt-4o": {"input_per_1m": 2.50, "output_per_1m": 10.00},
    },
    "prefixes": {
        "ollama/": {"input_per_1m": 0.0, "output_per_1m": 0.0},
    },
}


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """一次成本估算的结果。

    ``unavailable`` 是本结构存在的核心理由：它把
    "0 美元" 与 "不知道多少钱" 区分开。
    """

    cost_usd: Decimal
    model_name: str
    pricing_version: str
    unavailable: bool = False
    reason: str | None = None

    def to_float(self) -> float:
        """转成可序列化的 float（已按 6 位收敛）。"""
        return float(quantize_cost(self.cost_usd))


class PricingTable:
    """价目表。

    Args:
        models: ``{model_name: {"input_per_1m": float, "output_per_1m": float}}``。
        prefixes: ``{prefix: {...}}``，用于 ``ollama/`` 这类整族模型。
        version: 表版本标签，写入估算结果以便追溯。
        source: 表来源描述（文件路径或 ``"fallback"``）。
    """

    def __init__(
        self,
        *,
        models: dict[str, dict[str, Any]],
        prefixes: dict[str, dict[str, Any]] | None = None,
        version: str = "unknown",
        source: str = "builtin",
    ) -> None:
        self.models = {name: _normalize_entry(entry) for name, entry in models.items()}
        self.prefixes = {
            prefix: _normalize_entry(entry) for prefix, entry in (prefixes or {}).items()
        }
        self.version = version
        self.source = source

    def lookup(self, model_name: str) -> tuple[dict[str, Decimal] | None, str]:
        """查模型的单价。

        Returns:
            ``(单价字典, 匹配方式)``；未命中时单价为 ``None``，
            匹配方式为 ``"unknown"``。

        先精确匹配再前缀匹配：``gpt-4o`` 与 ``gpt-4o-mini`` 同时存在时，
        若先做前缀匹配，``gpt-4o-mini`` 会错误命中 ``gpt-4o`` 的价。
        """
        entry = self.models.get(model_name)
        if entry is not None:
            return entry, "exact"

        # 前缀按长度降序，保证最长前缀优先（``ollama/llama3`` 优于 ``ollama/``）
        for prefix in sorted(self.prefixes, key=len, reverse=True):
            if model_name.startswith(prefix):
                return self.prefixes[prefix], "prefix"

        return None, "unknown"

    @classmethod
    def load(cls, path: str | Path | None = None) -> PricingTable:
        """从 JSON 文件加载价目表。

        文件缺失或损坏时退化为内置兜底表并记 warning ——
        "价格表读不到"不该让评测无法进行，但必须留下痕迹。
        """
        if path is None:
            from app.core.config import get_settings

            path = get_settings().pricing_table_path

        file_path = Path(path)
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "pricing_table_fallback",
                extra={
                    "path": str(file_path),
                    "error_type": type(exc).__name__,
                },
            )
            return cls(
                models=_FALLBACK_TABLE["models"],
                prefixes=_FALLBACK_TABLE["prefixes"],
                version=str(_FALLBACK_TABLE["_updated"]),
                source=f"fallback (读取 {file_path} 失败: {type(exc).__name__})",
            )

        return cls(
            models=payload.get("models") or {},
            prefixes=payload.get("prefixes") or {},
            version=str(payload.get("_updated") or "unknown"),
            source=str(file_path),
        )


def estimate_cost(
    *,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    table: PricingTable | None = None,
) -> CostEstimate:
    """估算一次模型调用的成本。

    Args:
        model_name: 模型名。
        prompt_tokens: 输入 token 数。
        completion_tokens: 输出 token 数。
        table: 价目表；``None`` 时加载默认表。

    Returns:
        ``CostEstimate``。**未知模型返回 ``unavailable=True`` 且
        ``cost_usd = 0``** —— 与"免费模型"在数值上相同但语义不同，
        调用方必须检查 ``unavailable``。

    Raises:
        ValueError: token 数为负。
    """
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("token 数不得为负")

    if table is None:
        table = PricingTable.load()

    entry, match_kind = table.lookup(model_name)

    if entry is None:
        # 关键分支：**不静默当 0**。契约 EVALUATION §3 M8 限制 3 与
        # 反模式清单都点名了这一条。
        return CostEstimate(
            cost_usd=Decimal("0"),
            model_name=model_name,
            pricing_version=table.version,
            unavailable=True,
            reason=f"模型 {model_name!r} 不在价目表中，成本无法估算。",
        )

    input_cost = (Decimal(prompt_tokens) / _TOKENS_PER_UNIT) * entry["input_per_1m"]
    output_cost = (Decimal(completion_tokens) / _TOKENS_PER_UNIT) * entry["output_per_1m"]

    return CostEstimate(
        cost_usd=quantize_cost(input_cost + output_cost),
        model_name=model_name,
        pricing_version=table.version,
        # 前缀命中（如 ``ollama/*``）价格已知（0），因此**不是**不可估算。
        # "本地推理免费" 与 "我不知道价格" 是两件事。
        unavailable=False,
        reason=(f"matched by {match_kind}" if match_kind == "prefix" else None),
    )


def quantize_cost(value: Decimal) -> Decimal:
    """把金额收敛到 6 位小数（四舍五入）。"""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)


def get_pricing_table() -> PricingTable:
    """取得默认价目表（进程级不缓存，便于测试替换文件）。"""
    return PricingTable.load()


__all__ = [
    "CostEstimate",
    "PricingTable",
    "estimate_cost",
    "get_pricing_table",
    "quantize_cost",
]


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Decimal]:
    """把价目表的 JSON 条目转成 ``Decimal``。

    用 ``str()`` 中转而不是 ``Decimal(float)``：后者会引入浮点的
    二进制表示误差（``Decimal(0.15)`` 得到 ``0.1499999...``），
    而价格恰恰是需要精确表示的东西。
    """
    return {
        "input_per_1m": Decimal(str(entry.get("input_per_1m", 0))),
        "output_per_1m": Decimal(str(entry.get("output_per_1m", 0))),
    }
