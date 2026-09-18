"""摘要与审批关注点生成（SPEC §10.1 LM-02/LM-03、§10.3 LM-16、§10.4 模板降级）。

**用语边界**：LLM 只被允许生成 ``summary_text`` 与 ``focus_points`` 两项文本；
风险等级由 RL-AGG 决定（禁止 LLM 参与，RL-AGG-03），证据与建议一律取自规则命中的快照，
**不经过模型**（LM-05：禁止用 LLM 生成证据原文）。

**为什么要求模型带 ``rule_code`` 返回关注点**：评论模板（§4.6.1）里每个编号关注点后面必须跟
"证据：{position}"或"建议：{suggestion}"行。若关注点与命中无法可靠对齐，就会把**不相关的
原文位置**当成该关注点的证据展示——对法务审查工具而言这是不可接受的缺陷。因此关注点按
``rule_code`` 与命中配对，认不出的条目直接丢弃。

LLM 失败（未启用/超时/空 content/非法 JSON）→ 一律回落 §10.4 的模板文案（LM-16），
任务**禁止**因此转 ``blocked``（FR-REV-07 / LM-14）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import RISK_LEVEL_ZH
from app.core.logging import get_logger
from app.llm.base import LLMClient
from app.modules.review.summary import (
    EMPTY_SUMMARY_TEXT,
    FOCUS_POINT_LIMIT,
    FOCUS_POINT_MAX_LENGTH,
    build_template_focus_points,
    build_template_summary,
)

logger = get_logger(__name__)

SYSTEM_PROMPT = (
    "你是企业法务合同审查助手。你只负责把**已有的规则命中清单**转写成中文摘要与审批关注点，"
    "禁止新增清单之外的风险，禁止给出审批结论，禁止输出 JSON 以外的任何文字。"
)

USER_TEMPLATE = """下面是合同审查系统规则引擎给出的风险命中清单。

整体风险等级：{overall_zh}（{overall}）

命中清单：
{hit_lines}

请用简体中文输出一个 JSON 对象：
{{
  "summary_text": "总体风险摘要，1~2 句、不超过 120 字，必须点出主要风险类型",
  "focus_points": [
    {{"rule_code": "上面清单里的规则编号，如 R001", "text": "面向审批人的可执行关注点，不超过 40 字"}}
  ]
}}

要求：
- 只依据上面的命中清单，不要编造未列出的风险，rule_code 必须逐字取自清单；
- 不要出现"审批通过/不通过""建议批准"等替人工决策的表述；
- 不要输出证据原文（证据由系统从合同原文截取）；
- 关注点按风险从高到低排列，最多 5 条。"""

#: FR-COM-07 / LM-06：禁止出现替代人工决策的表述
FORBIDDEN_PHRASES = ("审批通过", "审批不通过", "建议批准", "建议拒绝", "同意签署", "拒绝签署")


@dataclass
class SummaryResult:
    """摘要与关注点生成结果。"""

    summary_text: str
    focus_points: list[str] = field(default_factory=list)
    #: ``rule_code → 关注点文本``，供评论模板把关注点与证据/建议行一一配对
    focus_by_rule: dict[str, str] = field(default_factory=dict)
    degraded: bool = True
    error: str | None = None


def _get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def active_hits(hits: Iterable[Any]) -> list[Any]:
    """命中项按风险等级降序（high → medium → low），与 §10.4 的排序口径一致。"""
    order = {"high": 0, "medium": 1, "low": 2}
    selected = [item for item in hits if _get(item, "hit_status") == "hit"]
    return sorted(selected, key=lambda item: order.get(str(_get(item, "risk_level")), 9))


def _hit_lines(hits: list[Any]) -> str:
    lines: list[str] = []
    for item in hits:
        lines.append(
            f"- {_get(item, 'rule_code')} {_get(item, 'rule_name')}"
            f"（{_get(item, 'risk_level')}）："
            f"{_get(item, 'suggestion') or _get(item, 'suggestion_text') or ''}"
        )
    return "\n".join(lines) or "（无命中）"


def _truncate(text: str) -> str:
    text = text.strip().strip("。；;，,")
    if len(text) > FOCUS_POINT_MAX_LENGTH:
        return text[: FOCUS_POINT_MAX_LENGTH - 1] + "…"
    return text


def _render_template(hits: list[Any]) -> SummaryResult:
    """§10.4 模板降级文案（**规范文本**）。

    ``focus_by_rule`` 必须按**仅命中项**（``active_hits``）的顺序对齐——用全量命中列表
    （含 miss/uncertain）对齐会整体错位，把 A 规则的关注点文字配到 B 规则的证据行上
    （实测踩过）。
    """
    active = active_hits(hits)
    points = build_template_focus_points(hits)
    by_rule: dict[str, str] = {}
    for index, item in enumerate(active[:FOCUS_POINT_LIMIT]):
        if index < len(points):
            by_rule[str(_get(item, "rule_code"))] = points[index]
    return SummaryResult(
        summary_text=build_template_summary(hits),
        focus_points=points,
        focus_by_rule=by_rule,
        degraded=True,
    )


def _parse_llm_focus(payload: Any, hits: list[Any]) -> dict[str, str]:
    """解析并**校验**模型给出的关注点：rule_code 必须命中清单里真实存在。"""
    if not isinstance(payload, list):
        return {}
    known = {str(_get(item, "rule_code")) for item in hits}
    result: dict[str, str] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("rule_code") or "").strip()
        text = str(entry.get("text") or "").strip()
        if code not in known or not text:
            logger.warning("丢弃无法与命中配对的关注点：rule_code=%r", code)
            continue
        if code in result:
            continue
        result[code] = _truncate(text)
        if len(result) >= FOCUS_POINT_LIMIT:
            break
    return result


async def generate_summary_and_focus(
    llm: LLMClient,
    hits: Iterable[Any],
    overall_risk_level: str,
) -> SummaryResult:
    """生成摘要与关注点；任何失败都回落 §10.4 模板（``degraded=True``）。"""
    hits = list(hits)
    active = active_hits(hits)
    template = _render_template(hits)

    if not active:
        # 无命中：摘要必须用规范文案（也省一次调用）
        return SummaryResult(
            summary_text=EMPTY_SUMMARY_TEXT,
            focus_points=[],
            focus_by_rule={},
            degraded=True,
        )
    if not llm.enabled:
        return template

    try:
        payload = await llm.complete_json(
            system=SYSTEM_PROMPT,
            user=USER_TEMPLATE.format(
                overall=overall_risk_level,
                overall_zh=RISK_LEVEL_ZH.get(overall_risk_level, overall_risk_level),
                hit_lines=_hit_lines(active),
            ),
            purpose="summary_generation",
        )
    except Exception as exc:  # noqa: BLE001 - LM-14：任何异常都只能降级
        logger.warning("摘要生成调用异常，回落模板：%s: %s", type(exc).__name__, exc)
        template.error = f"{type(exc).__name__}: {exc}"
        return template

    if not payload:
        logger.warning("摘要生成失败（LLM 未返回可用 JSON），回落模板")
        template.error = "LLM 未返回可用 JSON"
        return template

    summary = str(payload.get("summary_text") or "").strip()
    if not summary or any(word in summary for word in FORBIDDEN_PHRASES):
        logger.warning("摘要不合格（为空或含审批结论表述），回落模板")
        template.error = "摘要内容不合格"
        return template

    focus_by_rule = _parse_llm_focus(payload.get("focus_points"), active)
    if not focus_by_rule:
        logger.warning("关注点全部无法配对，回落模板关注点")
        template.summary_text = summary
        template.error = "关注点无法配对"
        return template

    # 按命中顺序输出展示用列表，并补齐模板文案以覆盖所有命中（保证评论每个条目都有附件行）
    ordered: list[str] = []
    merged: dict[str, str] = {}
    for item in active[:FOCUS_POINT_LIMIT]:
        code = str(_get(item, "rule_code"))
        text = focus_by_rule.get(code) or template.focus_by_rule.get(code, "")
        if not text:
            continue
        merged[code] = text
        ordered.append(text)

    logger.info(
        "摘要与关注点由 LLM 生成：摘要 %d 字、关注点 %d 条（模型 %s）",
        len(summary),
        len(ordered),
        llm.model,
    )
    return SummaryResult(
        summary_text=summary,
        focus_points=ordered,
        focus_by_rule=merged,
        degraded=False,
    )
