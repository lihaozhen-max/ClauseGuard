"""评论正文渲染（SPEC §4.6.1，**规范文本，禁止改动措辞**）。

两种模板严格对应 SPEC：

- **存在命中**（``overall_risk_level`` 为 ``medium``/``high``）；
- **无命中**（``low``）。

每个编号关注点后面**必须**跟一行"证据：{position}。"或"建议：{suggestion}。"：

- 关注点为风险项（有原文可定位）→ 证据行；
- 关注点为缺失项（无原文可定位）→ 建议行（§4.6.1 的硬约束）。

风险等级一律输出中文（低/中/高，SPEC §2.2），关注点从 1 开始编号、最多 5 条。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.core.enums import RISK_LEVEL_ZH, RiskLevel
from app.core.logging import get_logger
from app.modules.review.summary import FOCUS_POINT_LIMIT, FOCUS_POINT_MAX_LENGTH
from app.modules.review.summarizer import active_hits

logger = get_logger(__name__)

TITLE = "【合同自动审查结果】"
DISCLAIMER = "以上结果由合同审查系统自动生成，仅供审批人员参考，最终审批结论由审批人员判断。"
NO_HITS_SUMMARY = "本次自动审查未发现明确的高风险或中风险条款。"

#: 评论中会出现命中的等级（§4.6.1：low 走"无命中"模板）
_LEVELS_WITH_HITS = {RiskLevel.MEDIUM.value, RiskLevel.HIGH.value}


@dataclass
class CommentItem:
    """一个编号关注点及其附件行。"""

    index: int
    text: str
    position: str | None
    suggestion: str

    def render(self) -> str:
        head = f"{self.index}. {self.text}"
        if self.position:
            return f"{head}\n证据：{self.position}。"
        return f"{head}\n建议：{self.suggestion}。"


def _get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _truncate(text: str) -> str:
    text = text.strip().strip("。；;，,")
    if len(text) > FOCUS_POINT_MAX_LENGTH:
        return text[: FOCUS_POINT_MAX_LENGTH - 1] + "…"
    return text


def build_comment_items(hits: Iterable[Any], focus_by_rule: dict[str, str]) -> list[CommentItem]:
    """按命中顺序（等级降序）构造编号条目，最多 5 条。"""
    items: list[CommentItem] = []
    for index, hit in enumerate(active_hits(hits)[:FOCUS_POINT_LIMIT], start=1):
        code = str(_get(hit, "rule_code") or "")
        suggestion = str(_get(hit, "suggestion") or _get(hit, "suggestion_text") or "").strip()
        text = focus_by_rule.get(code) or _truncate(suggestion) or code
        position = _get(hit, "evidence_position")
        position = str(position).strip() if position else None
        if not position and not suggestion:
            # 兜底：既无位置也无建议时，用条目文本自身充当建议，避免出现空行
            suggestion = text
        items.append(CommentItem(index=index, text=text, position=position, suggestion=suggestion))
    return items


def render_comment(
    *,
    overall_risk_level: str,
    summary_text: str,
    hits: Iterable[Any],
    focus_by_rule: dict[str, str] | None = None,
) -> str:
    """渲染最终写入审批系统评论区的正文（§4.6.1）。"""
    level_zh = RISK_LEVEL_ZH.get(overall_risk_level, overall_risk_level)
    items = build_comment_items(hits, focus_by_rule or {})

    if overall_risk_level not in _LEVELS_WITH_HITS or not items:
        return "\n".join(
            [
                TITLE,
                "",
                f"整体风险等级：{level_zh}",
                "",
                NO_HITS_SUMMARY,
                "",
                DISCLAIMER,
            ]
        )

    body = "\n\n".join(item.render() for item in items)
    return "\n".join(
        [
            TITLE,
            "",
            f"整体风险等级：{level_zh}",
            "",
            "风险摘要：",
            summary_text.strip(),
            "",
            "重点关注：",
            "",
            body,
            "",
            DISCLAIMER,
        ]
    )
