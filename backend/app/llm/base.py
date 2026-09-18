"""LLM 抽象与语义判定（SPEC §10 LM-01…LM-18）。

用途白名单（LM-01…LM-03，**仅此三项**）：

| 编号 | 允许用途 | 落地里程碑 |
|---|---|---|
| LM-01 | RL-003 / RL-004 / RL-010 的语义判断 | M3 |
| LM-02 | ``summary_text`` 中文摘要生成 | M4 |
| LM-03 | ``focus_points`` 审批关注点生成 | M4 |

**禁止**：用 LLM 决定风险等级（LM-04）、生成证据原文（LM-05）、生成审批结论（LM-06）。

证据口径：模型可以**指出**它认为哪段原文是依据，但系统必须回验该片段确实是
``full_text`` 的连续子串（PS-10）；回验失败即降级，绝不采信模型生成的文本。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: LM-12：重试上限 ≤ 2 次
MAX_ATTEMPTS = 2

#: LM-11：实现必须能剥离 ```json 包裹
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

SYSTEM_PROMPT = (
    "你是企业法务合同审查助手。请严格按要求只输出一个 JSON 对象，"
    "不要输出任何解释性文字，不要使用 Markdown 代码块。"
)

USER_TEMPLATE = """请就下面的判定问题审阅合同文本。

判定问题：{question}

合同文本（节选，以 --- 分隔）：
---
{context}
---

请输出如下 JSON（字段顺序不限）：
{{
  "hit": true 或 false,
  "confidence": 0 到 1 之间的小数,
  "reason": "简要中文理由（不超过 80 字）",
  "evidence_text": "支撑该判断的合同原文片段；必须逐字摘自上面的合同文本，禁止改写或概括；"
                   "若判定为 false 或找不到依据，填空字符串"
}}
"""


@dataclass
class SemanticJudgement:
    """一次语义判定的结果。

    ``hit is None`` 表示**无法判定**——调用方必须按 SPEC §10.3 降级（记 ``uncertain``），
    禁止把它当作"未命中"（LM-13：空 content / 非法 JSON / 超时一律视为调用失败）。
    """

    hit: bool | None
    reason: str = ""
    evidence_text: str | None = None
    confidence: float | None = None
    degraded: bool = False
    error: str | None = None
    duration_seconds: float = 0.0
    model: str | None = None

    @property
    def usable(self) -> bool:
        return self.hit is not None


@runtime_checkable
class LLMClient(Protocol):
    """LLM 客户端抽象（业务层只依赖它，便于替换 OpenAI 兼容端点）。

    ``complete_json`` 是**唯一的调用入口**，同时服务三类白名单用途（LM-01…LM-03）：
    语义规则判定、中文摘要生成、审批关注点生成。
    """

    enabled: bool
    model: str

    async def complete_json(
        self, *, system: str, user: str, purpose: str = "json_completion"
    ) -> dict[str, Any] | None:
        """返回解析后的 JSON 对象；任何失败（超时/空 content/非法 JSON/非 200）一律返回 None。

        ``purpose`` 只用于日志（LM-17 要求记录调用用途），取值如
        ``semantic_judge``（语义规则判定）、``summary_generation``（摘要与关注点）。
        """
        ...


def extract_json_object(content: str) -> dict[str, Any] | None:
    """从模型输出中取出 JSON 对象（LM-11：不依赖模型不输出 Markdown 代码块）。"""
    if not content or not content.strip():
        return None
    text = content.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    # 兜底：截取第一个 { 到最后一个 } 之间的内容再试
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "是", "1"}:
            return True
        if lowered in {"false", "no", "否", "0"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_judgement(payload: dict[str, Any] | None, *, model: str | None = None) -> SemanticJudgement:
    """把模型返回的 JSON 归一化成 :class:`SemanticJudgement`。"""
    if not payload:
        return SemanticJudgement(hit=None, degraded=True, error="LLM 未返回可用 JSON", model=model)

    hit = _as_bool(payload.get("hit"))
    if hit is None:
        return SemanticJudgement(
            hit=None, degraded=True, error=f"LLM 返回缺少可解析的 hit 字段：{payload!r:.200}", model=model
        )

    evidence = payload.get("evidence_text")
    if isinstance(evidence, str):
        evidence = evidence.strip() or None
    else:
        evidence = None

    return SemanticJudgement(
        hit=hit,
        reason=str(payload.get("reason") or "").strip(),
        evidence_text=evidence,
        confidence=_as_float(payload.get("confidence")),
        model=model,
    )


async def judge_semantic(
    client: LLMClient,
    *,
    question: str,
    context: str,
) -> SemanticJudgement:
    """按统一提示词调用 LLM 并归一化结果；失败即返回 ``hit=None`` 的降级结果。

    这里**额外兜一层**异常捕获（LM-14）：客户端实现（或未来的新端点适配器）抛出的任何异常
    都不得穿透到规则层——LLM 是增强项，坏掉只能降级，不能把整条审查链打断。
    """
    try:
        payload = await client.complete_json(
            system=SYSTEM_PROMPT,
            user=USER_TEMPLATE.format(question=question, context=context),
            purpose="semantic_judge",
        )
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        return SemanticJudgement(
            hit=None,
            degraded=True,
            error=f"{type(exc).__name__}: {exc}",
            model=getattr(client, "model", None),
        )
    return parse_judgement(payload, model=getattr(client, "model", None))


@dataclass
class LLMCallLog:
    """LM-17 要求的调用日志载体（禁止记录 Key）。"""

    purpose: str
    model: str | None
    duration_seconds: float
    degraded: bool
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        parts = [
            f"LLM 调用：{self.purpose}",
            f"模型 {self.model or '-'}",
            f"耗时 {self.duration_seconds:.2f}s",
        ]
        if self.prompt_tokens is not None or self.completion_tokens is not None:
            parts.append(f"token 入{self.prompt_tokens or 0}/出{self.completion_tokens or 0}")
        parts.append("降级" if self.degraded else "成功")
        if self.error:
            parts.append(f"错误 {self.error}")
        return "｜".join(parts)
