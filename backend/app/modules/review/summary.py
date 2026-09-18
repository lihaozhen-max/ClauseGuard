"""摘要与审批关注点（SPEC §10.4 **规范文本**的模板降级实现）。

M3 先用模板生成，保证 IF-05 的返回结构完整、且**无 LLM 也能跑通全闭环**（LM-16/LM-18）；
M4 接入 LLM 生成（LM-02/LM-03）后，本模块仍作为降级路径保留——两处调用同一套规范文案。

规范文本（**禁止改动措辞**）：

- ``summary_text``：``本合同共命中{N}项风险，其中高风险{H}项、中风险{M}项，主要涉及：{规则名顿号连接}。``
  无命中时：``本次自动审查未发现明确的高风险或中风险条款。``
- ``focus_points``：取命中项按等级（high > medium）降序，最多 5 条；每条取该规则的 ``suggestion_text``，
  超出 40 字截断加 ``…``；无命中时**必须**为空数组。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.modules.rules.aggregator import active_hits, count_by_level

#: §10.4：关注点单条长度上限
FOCUS_POINT_MAX_LENGTH = 40

#: §10.4：关注点条数上限
FOCUS_POINT_LIMIT = 5

EMPTY_SUMMARY_TEXT = "本次自动审查未发现明确的高风险或中风险条款。"


def build_template_summary(hits: Iterable[Any]) -> str:
    """§10.4 的摘要模板。"""
    hits = list(hits)
    active = active_hits(hits)
    if not active:
        return EMPTY_SUMMARY_TEXT

    levels = count_by_level(hits)
    names = "、".join(str(_get(item, "rule_name") or _get(item, "rule_code") or "") for item in active)
    return (
        f"本合同共命中{len(active)}项风险，"
        f"其中高风险{levels.get('high', 0)}项、中风险{levels.get('medium', 0)}项，"
        f"主要涉及：{names}。"
    )


def build_template_focus_points(hits: Iterable[Any]) -> list[str]:
    """§10.4 的关注点模板：按等级降序取最多 5 条建议，每条 ≤40 字。"""
    points: list[str] = []
    for item in active_hits(hits):
        suggestion = str(_get(item, "suggestion_text") or _get(item, "suggestion") or "").strip()
        if not suggestion:
            continue
        if len(suggestion) > FOCUS_POINT_MAX_LENGTH:
            suggestion = suggestion[: FOCUS_POINT_MAX_LENGTH - 1] + "…"
        points.append(suggestion)
        if len(points) >= FOCUS_POINT_LIMIT:
            break
    return points


def _get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)
