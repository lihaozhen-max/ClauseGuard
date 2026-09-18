"""Review Module 编排：生成汇总 → 落库 → 状态迁移（SPEC §4.5、IF-06）。

职责：

- ``persist_review``：**完整流程**——生成摘要/关注点（LLM 或模板）→ 渲染评论（§4.6.1）
  → 按 ``task_id`` upsert ``review_results``（FR-REV-05）→ ``reviewing → done``（ST-01）；
- ``save_review_result``：IF-06 的直接落库入口（调用方已备好全部字段）。

约束：``uncertain`` 命中不计入整体等级但必须保留在结果中（FR-REV-06，由 RL-AGG 与 ``rule_hits`` 保证）；
摘要/关注点生成失败**禁止**把任务转 ``blocked``（FR-REV-07 / LM-14）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import LogType, TaskStatus
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.timeutil import format_duration
from app.db.models import ApprovalTask, ReviewResult
from app.llm.base import LLMClient
from app.llm.factory import build_llm_client
from app.modules.approval.state import transition_to
from app.modules.logging.service import write_task_log
from app.modules.review.comment import render_comment
from app.modules.review.summarizer import SummaryResult, generate_summary_and_focus
from app.modules.rules.service import RuleRunResult

logger = get_logger(__name__)


@dataclass
class SavedReview:
    """落库后的审查结果（IF-06 返回 + 评论渲染过程信息）。"""

    review_id: int
    task_id: int
    overall_risk_level: str
    summary_text: str
    focus_points: list[str] = field(default_factory=list)
    comment_text: str = ""
    summary_degraded: bool = True
    summary_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "task_id": self.task_id,
            "overall_risk_level": self.overall_risk_level,
            "summary_text": self.summary_text,
            "focus_points": self.focus_points,
            "comment_text": self.comment_text,
            "summary_degraded": self.summary_degraded,
        }


async def _upsert(
    session: AsyncSession,
    *,
    task_id: int,
    overall_risk_level: str,
    summary_text: str,
    focus_points: list[str],
    comment_text: str,
) -> ReviewResult:
    """按 ``task_id`` upsert（FR-REV-05：重复保存更新同一 ``review_id``，不新增）。"""
    values = {
        "task_id": task_id,
        "overall_risk_level": overall_risk_level,
        "summary_text": summary_text,
        "focus_points_json": list(focus_points),
        "comment_text": comment_text,
    }
    statement = mysql_insert(ReviewResult.__table__).values(**values)
    await session.execute(
        statement.on_duplicate_key_update(
            overall_risk_level=overall_risk_level,
            summary_text=summary_text,
            focus_points_json=list(focus_points),
            comment_text=comment_text,
        )
    )
    await session.flush()
    row = await session.scalar(select(ReviewResult).where(ReviewResult.task_id == task_id))
    if row is None:  # pragma: no cover - upsert 后必然存在
        raise AppError(ErrorCode.INTERNAL_ERROR, "审查结果写入失败", task_id=task_id)
    return row


async def _advance_to_done(session: AsyncSession, task: ApprovalTask, review_id: int) -> None:
    """ST-01：``reviewing → done``（审查结果保存成功）。"""
    if TaskStatus(task.task_status) is TaskStatus.REVIEWING:
        await transition_to(
            session,
            task,
            TaskStatus.DONE,
            log_type=LogType.SAVE,
            log_message=f"审查结果已保存，任务完成（review_id={review_id}）",
        )


async def save_review_result(
    session: AsyncSession,
    task: ApprovalTask,
    *,
    overall_risk_level: str,
    summary_text: str,
    focus_points: list[str],
    comment_text: str,
) -> SavedReview:
    """IF-06 的核心实现：按 ``task_id`` upsert 审查结果。"""
    row = await _upsert(
        session,
        task_id=task.id,
        overall_risk_level=overall_risk_level,
        summary_text=summary_text,
        focus_points=focus_points,
        comment_text=comment_text,
    )
    await write_task_log(
        session,
        task.id,
        LogType.SAVE,
        f"审查结果保存：review_id={row.id}｜整体风险 {overall_risk_level}｜"
        f"关注点 {len(focus_points)} 条｜评论 {len(comment_text)} 字",
    )
    await _advance_to_done(session, task, row.id)
    return SavedReview(
        review_id=row.id,
        task_id=task.id,
        overall_risk_level=overall_risk_level,
        summary_text=summary_text,
        focus_points=list(focus_points),
        comment_text=comment_text,
    )


async def persist_review(
    session: AsyncSession,
    task: ApprovalTask,
    run_result: RuleRunResult,
    *,
    llm: LLMClient | None = None,
) -> SavedReview:
    """完整流程：规则结果 → 摘要/关注点 → 评论正文 → 落库 → ``done``。"""
    started = time.perf_counter()
    llm = llm or build_llm_client()

    summary: SummaryResult = await generate_summary_and_focus(
        llm, run_result.rule_hits, run_result.overall_risk_level
    )
    comment_text = render_comment(
        overall_risk_level=run_result.overall_risk_level,
        summary_text=summary.summary_text,
        hits=run_result.rule_hits,
        focus_by_rule=summary.focus_by_rule,
    )

    row = await _upsert(
        session,
        task_id=task.id,
        overall_risk_level=run_result.overall_risk_level,
        summary_text=summary.summary_text,
        focus_points=summary.focus_points,
        comment_text=comment_text,
    )
    await write_task_log(
        session,
        task.id,
        LogType.SAVE,
        f"审查结果保存：review_id={row.id}｜整体风险 {run_result.overall_risk_level}｜"
        f"命中 {run_result.hit_count}、uncertain {run_result.uncertain_count}｜"
        f"关注点 {len(summary.focus_points)} 条｜摘要{'由 LLM 生成' if not summary.degraded else '为模板降级'}"
        f"｜评论 {len(comment_text)} 字｜耗时 {format_duration(time.perf_counter() - started)}",
    )
    if summary.degraded:
        await write_task_log(
            session,
            task.id,
            LogType.SAVE,
            f"摘要/关注点走模板降级（LM-16）：{summary.error or 'LLM 未启用或不可用'}",
        )
    await _advance_to_done(session, task, row.id)

    return SavedReview(
        review_id=row.id,
        task_id=task.id,
        overall_risk_level=run_result.overall_risk_level,
        summary_text=summary.summary_text,
        focus_points=summary.focus_points,
        comment_text=comment_text,
        summary_degraded=summary.degraded,
        summary_error=summary.error,
    )


async def get_review_result(session: AsyncSession, task_id: int) -> ReviewResult | None:
    """按任务取已保存的审查结果（IF-15）。"""
    return await session.scalar(select(ReviewResult).where(ReviewResult.task_id == task_id))


async def get_review_by_id(session: AsyncSession, review_id: int) -> ReviewResult | None:
    return await session.get(ReviewResult, review_id)
