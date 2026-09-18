"""降级实现：``LLM_ENABLED=false`` 或 Key 为空时使用（SPEC §10.3 LM-14…LM-18）。

它**不报错**，只是永远返回"无法判定"，让语义型规则退化为关键词/存在性判定，
从而保证无网络、无 Key 也能跑通 AC01–AC19 全闭环（LM-18）。
"""

from __future__ import annotations

from typing import Any


class NullLLMClient:
    """空实现。``judge_json`` 恒返回 None → 调用方记 ``uncertain`` 并走模板降级。"""

    def __init__(self, reason: str = "LLM 未启用") -> None:
        self.enabled = False
        self.model = "null"
        self.reason = reason

    async def judge_json(self, *, system: str, user: str) -> dict[str, Any] | None:  # noqa: ARG002
        return None
