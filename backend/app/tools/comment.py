"""评论回写工具接口 IF-07（签名与 PRD §15 一致：``write_approval_comment(instance_id, review_id)``）。

失败路径同样要 commit：回写失败时 ``write_status=failed`` 与日志必须落库，
而任务状态**保持** ``done``、审查结果**不删除**（FR-COM-04 / ST-01-04）。
"""

from __future__ import annotations

from sqlalchemy import select

from app.clients.approval_client import ApprovalSystemClient
from app.core.errors import AppError, ErrorCode
from app.db.models import ApprovalTask, ReviewResult
from app.db.session import session_scope
from app.modules.comment.service import CommentWriteResult, write_approval_comment as _write


async def write_approval_comment(
    instance_id: str,
    review_id: int,
    *,
    client: ApprovalSystemClient | None = None,
) -> CommentWriteResult:
    """IF-07：把审查结果写入审批系统评论区（幂等）。

    异常（§5.4）：回写失败 → ``COMMENT_WRITE_FAILED``，``write_status=failed``，任务保持 ``done``。
    """
    async with session_scope() as session:
        task = await session.scalar(
            select(ApprovalTask).where(ApprovalTask.instance_id == instance_id)
        )
        if task is None:
            raise AppError(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"审批实例 {instance_id} 尚未拉取为任务",
                detail={"instance_id": instance_id},
            )

        review = await session.get(ReviewResult, review_id)
        if review is None:
            raise AppError(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"审查结果 {review_id} 不存在，请先执行规则审查",
                task_id=task.id,
                detail={"review_id": review_id},
            )
        if review.task_id != task.id:
            # 传了别的审批实例的 review_id —— 拒绝，避免把 A 合同的审查结果写到 B 合同评论区
            raise AppError(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"审查结果 {review_id} 不属于审批实例 {instance_id}",
                task_id=task.id,
                detail={"review_id": review_id, "review_task_id": review.task_id, "task_id": task.id},
            )

        try:
            result = await _write(session, task, review, client=client)
        except AppError:
            await session.commit()  # 失败态与日志必须落库（FR-COM-03 / NF-04）
            raise
        await session.commit()
    return result
