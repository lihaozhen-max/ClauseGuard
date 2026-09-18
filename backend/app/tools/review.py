"""审查工具接口 IF-06 与"执行审查 + 保存结果"编排。

- ``save_review_result(...)``：PRD §15 的工具签名（5 个参数），按 ``task_id`` upsert；
- ``run_full_review(task_id)``：IF-16 的内部编排（IF-05 + 摘要/关注点 + §4.6.1 评论 + IF-06）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.errors import AppError, ErrorCode
from app.db.models import ApprovalTask
from app.db.session import session_scope
from app.llm.base import LLMClient
from app.modules.review.service import (
    SavedReview,
    persist_review,
    save_review_result as save_review_result_in_session,
)
from app.modules.rules.service import RuleRunResult, run_rules_for_task


def _as_focus_points(raw: Any) -> list[str]:
    """``focus_points_json`` 允许传 JSON 字符串或列表（PRD 签名沿用 json 字样）。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError as exc:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"focus_points_json 不是合法 JSON：{exc}",
                detail={"focus_points_json": raw[:200]},
            ) from exc
    else:
        parsed = raw
    if not isinstance(parsed, list):
        raise AppError(ErrorCode.VALIDATION_ERROR, "focus_points_json 必须是字符串数组")
    return [str(item) for item in parsed]


async def save_review_result(
    case_id: int,
    overall_risk_level: str,
    summary_text: str,
    focus_points_json: Any,
    comment_text: str,
) -> SavedReview:
    """IF-06：保存审查结果（按 ``task_id`` upsert，重复调用更新同一条）。"""
    async with session_scope() as session:
        task = await session.get(ApprovalTask, case_id)
        if task is None:
            raise AppError(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"任务 {case_id} 不存在",
                task_id=case_id,
                detail={"case_id": case_id},
            )
        try:
            saved = await save_review_result_in_session(
                session,
                task,
                overall_risk_level=overall_risk_level,
                summary_text=summary_text,
                focus_points=_as_focus_points(focus_points_json),
                comment_text=comment_text,
            )
        except AppError:
            await session.commit()
            raise
        await session.commit()
    return saved


@dataclass
class ReviewPipeline:
    """IF-16 的完整产物：规则判定 + 汇总 + 评论。"""

    task_id: int
    run: RuleRunResult
    saved: SavedReview


async def run_full_review(
    task_id: int,
    *,
    llm: LLMClient | None = None,
    settings=None,
) -> ReviewPipeline:
    """IF-16 的内部编排：执行规则 → 生成摘要/关注点 → 渲染评论 → 保存结果 → ``done``。"""
    async with session_scope() as session:
        task = await session.get(ApprovalTask, task_id)
        if task is None:
            raise AppError(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"任务 {task_id} 不存在",
                task_id=task_id,
                detail={"case_id": task_id},
            )
        try:
            run = await run_rules_for_task(session, task_id, settings=settings, llm=llm)
            saved = await persist_review(session, task, run, llm=llm)
        except AppError:
            await session.commit()  # 失败前可能已写入部分命中，不能丢
            raise
        await session.commit()
    return ReviewPipeline(task_id=task_id, run=run, saved=saved)


async def get_task_for_review(session, task_id: int) -> ApprovalTask | None:
    return await session.scalar(select(ApprovalTask).where(ApprovalTask.id == task_id))
