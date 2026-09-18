"""Comment Module：评论回写、幂等与回写状态机（SPEC §4.6、IF-07、ST-02）。

**幂等设计（重要口径）**

``comment_logs.idempotency_key`` 上有 ⭐UNIQUE 索引（DT-07 / DT-00-05），而 ST-02-02 又要求
"每次状态变化新增一条 comment_logs（追加，不覆盖）"——两者在**同一个 (instance_id, review_id)**
上无法同时成立（幂等键必须稳定，否则审批系统会当成两条评论）。取舍：

- ``comment_logs``：**一条逻辑评论一行**，``write_status`` 就地从 ``writing`` 更新为
  ``success``/``failed``。唯一索引即"禁止重复评论"的最终保障（FR-COM-02 / NF-03）。
- ``task_logs(log_type=write_comment)``：**每次尝试追加一条**，保留完整的重试审计轨迹
  （FR-LOG-01 / FR-LOG-05）。

其他硬约束：

- FR-COM-06 / ST-02-01：已 ``success`` 再次调用**必须**直接返回既有结果（``duplicate=true``），
  禁止重置为 ``writing``，禁止再次请求审批系统；
- FR-COM-04 / ST-01-04：回写失败**禁止**删除审查结果，``task_status`` 保持 ``done``；
- FR-COM-07：评论正文必须以免责声明结尾，禁止出现替代人工决策的表述。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.approval_client import ApprovalSystemClient
from app.core.enums import LogLevel, LogType, WriteStatus
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.timeutil import format_duration
from app.db.models import ApprovalTask, CommentLog, ReviewResult
from app.modules.logging.service import write_task_log
from app.modules.review.comment import DISCLAIMER
from app.modules.review.summarizer import FORBIDDEN_PHRASES

logger = get_logger(__name__)


@dataclass
class CommentWriteResult:
    """IF-07 的返回结构。"""

    task_id: int
    review_id: int
    write_status: str
    remark_id: str | None = None
    write_response_text: str | None = None
    duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "review_id": self.review_id,
            "write_status": self.write_status,
            "remark_id": self.remark_id,
            "write_response_text": self.write_response_text,
            "duplicate": self.duplicate,
        }


def build_idempotency_key(instance_id: str, review_id: int) -> str:
    """SPEC DT-07：``instance_id + ':' + review_id`` 的 SHA-256（64 位十六进制）。"""
    return hashlib.sha256(f"{instance_id}:{review_id}".encode()).hexdigest()


def validate_comment_text(text: str) -> str | None:
    """FR-COM-07 守卫：返回不合规原因，合规返回 ``None``。"""
    if not text or not text.strip():
        return "评论正文为空"
    if not text.strip().endswith(DISCLAIMER):
        return "评论正文未以免责声明结尾"
    found = [word for word in FORBIDDEN_PHRASES if word in text]
    if found:
        return f"评论正文含替代人工决策的表述：{'、'.join(found)}"
    return None


async def _append_log(
    session: AsyncSession, task: ApprovalTask, message: str, level: LogLevel = LogLevel.INFO
) -> None:
    await write_task_log(session, task.id, LogType.WRITE_COMMENT, message, level=level)


async def write_approval_comment(
    session: AsyncSession,
    task: ApprovalTask,
    review: ReviewResult,
    *,
    client: ApprovalSystemClient | None = None,
) -> CommentWriteResult:
    """IF-07 的核心实现。

    异常：回写失败 → ``COMMENT_WRITE_FAILED``（§5.4：``write_status=failed``，任务保持 ``done``）。
    """
    client = client or ApprovalSystemClient()
    instance_id = task.instance_id
    key = build_idempotency_key(instance_id, review.id)

    existing = await session.scalar(
        select(CommentLog).where(CommentLog.idempotency_key == key)
    )
    if existing is not None and existing.write_status == WriteStatus.SUCCESS.value:
        # ST-02-01：success 是终态——直接返回既有结果，不重复请求审批系统
        await _append_log(
            session,
            task,
            f"评论已回写成功，直接返回既有结果（duplicate=true，remark_id={existing.remark_id}）",
        )
        return CommentWriteResult(
            task_id=task.id,
            review_id=review.id,
            write_status=WriteStatus.SUCCESS.value,
            remark_id=existing.remark_id,
            write_response_text=existing.write_response_text,
            duplicate=True,
        )

    reason = validate_comment_text(review.comment_text or "")
    if reason:
        # FR-COM-07 守卫：宁可不回写，也不能把不合规的正文写进审批系统
        await _append_log(session, task, f"评论正文不合规，拒绝回写：{reason}", LogLevel.ERROR)
        raise AppError(
            ErrorCode.COMMENT_WRITE_FAILED,
            f"评论正文不符合 §4.6.1 模板：{reason}",
            task_id=task.id,
            detail={"review_id": review.id, "reason": reason},
        )

    # ST-02：not_written / failed → writing（success 已在上面短路）
    if existing is None:
        row = CommentLog(
            task_id=task.id,
            write_status=WriteStatus.WRITING.value,
            idempotency_key=key,
        )
        session.add(row)
    else:
        row = existing
        row.write_status = WriteStatus.WRITING.value
    task.write_status = WriteStatus.WRITING.value
    await session.flush()
    await _append_log(
        session,
        task,
        f"开始回写评论：instance_id={instance_id}｜review_id={review.id}｜幂等键={key[:16]}…",
    )

    started = time.perf_counter()
    response_text: str | None = None
    remark_id: str | None = None
    try:
        response = await client.write_comment(instance_id, key, review.comment_text)
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("code") != 0 or not payload.get("remark_id"):
            raise AppError(
                ErrorCode.COMMENT_WRITE_FAILED,
                f"审批系统返回异常结构：{response.text[:200]}",
                task_id=task.id,
                detail={"status_code": response.status_code, "body": response.text[:500]},
            )
        remark_id = str(payload["remark_id"])
        response_text = response.text
    except AppError as exc:
        # FR-COM-04：只改 write_status，任务保持 done，审查结果不删除
        row.write_status = WriteStatus.FAILED.value
        row.write_response_text = f"[{exc.code.value}] {exc.message}｜{exc.detail}"
        row.remark_id = None
        task.write_status = WriteStatus.FAILED.value
        await session.flush()
        await _append_log(
            session,
            task,
            f"评论回写失败：instance_id={instance_id}｜review_id={review.id}｜"
            f"{exc.code.value}｜{exc.message}（任务保持 {task.task_status}，审查结果不删除）",
            LogLevel.ERROR,
        )
        raise AppError(
            ErrorCode.COMMENT_WRITE_FAILED,
            f"评论回写失败：{exc.message}",
            task_id=task.id,
            detail={"review_id": review.id, "instance_id": instance_id, **exc.detail},
        ) from exc

    row.write_status = WriteStatus.SUCCESS.value
    row.remark_id = remark_id
    row.write_response_text = response_text
    task.write_status = WriteStatus.SUCCESS.value
    await session.flush()
    await _append_log(
        session,
        task,
        f"评论回写成功：instance_id={instance_id}｜review_id={review.id}｜remark_id={remark_id}｜"
        f"耗时 {format_duration(time.perf_counter() - started)}",
    )
    logger.info(
        "评论回写成功 task_id=%s instance_id=%s review_id=%s remark_id=%s",
        task.id,
        instance_id,
        review.id,
        remark_id,
    )
    return CommentWriteResult(
        task_id=task.id,
        review_id=review.id,
        write_status=WriteStatus.SUCCESS.value,
        remark_id=remark_id,
        write_response_text=response_text,
        duplicate=False,
    )


async def list_comment_logs(session: AsyncSession, task_id: int) -> list[CommentLog]:
    """回写历史（IF-18）。"""
    rows = await session.execute(
        select(CommentLog).where(CommentLog.task_id == task_id).order_by(CommentLog.id)
    )
    return list(rows.scalars().all())
