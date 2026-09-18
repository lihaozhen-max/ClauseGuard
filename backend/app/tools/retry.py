"""人工重试编排（SPEC IF-20、ST-01、FR-LOG-05、设计 FIG-06）。

语义（严格按 ST-01 与 FIG-06 的异常重试时序）：

1. 任务必须处于 ``blocked``；从 ``blocked_stage`` 决定重入阶段
   （``parsing`` → 重新下载 + 解析；``reviewing`` → 直接重新审查）；
2. 进入断点阶段即完成 ST-01-02 的副作用：清空 ``blocked_stage``/``error_code``、
   ``retry_count += 1``，并记 ``log_type=retry`` 日志（FR-LOG-05）；
3. 随后**继续跑完剩余流程**（解析 → 审查 → 汇总 → 保存 → ``done``）——
   这与 FIG-06 的时序图一致：管理员点一次重试，任务应跑到终态；
4. 若再次失败，任务重新进入 ``blocked``（携带新的错误码），异常照常向上抛。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.approval_client import ApprovalSystemClient
from app.core.enums import LogLevel, LogType, TaskStatus
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.timeutil import format_duration
from app.db.models import ApprovalTask
from app.db.session import session_scope
from app.modules.approval.state import BLOCKED_STAGE_PARSING, BLOCKED_STAGE_REVIEWING, transition_to
from app.modules.logging.service import write_task_log
from app.modules.review.service import get_review_result
from app.tools.parser import parse_task
from app.tools.review import run_full_review

logger = get_logger(__name__)


@dataclass
class RetryOutcome:
    """IF-20 的返回结构。"""

    task_id: int
    resumed_stage: str
    task_status: str
    retry_count: int
    review_id: int | None = None
    overall_risk_level: str | None = None
    comment_text: str = ""
    blocked_stage: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "resumed_stage": self.resumed_stage,
            "task_status": self.task_status,
            "retry_count": self.retry_count,
            "review_id": self.review_id,
            "overall_risk_level": self.overall_risk_level,
            "comment_text": self.comment_text,
            "blocked_stage": self.blocked_stage,
            "error_code": self.error_code,
        }


async def _enter_retry_stage(session: AsyncSession, task: ApprovalTask) -> str:
    """校验状态并进入断点阶段；返回重入的阶段名。"""
    status = TaskStatus(task.task_status)
    if status is not TaskStatus.BLOCKED:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"任务 {task.id} 当前状态为 {status.value}，不处于 blocked，无需重试",
            task_id=task.id,
            detail={"task_id": task.id, "task_status": status.value},
        )

    stage = task.blocked_stage or BLOCKED_STAGE_PARSING
    if stage not in {BLOCKED_STAGE_PARSING, BLOCKED_STAGE_REVIEWING}:
        stage = BLOCKED_STAGE_PARSING

    target = (
        TaskStatus.PARSING if stage == BLOCKED_STAGE_PARSING else TaskStatus.REVIEWING
    )
    await transition_to(
        session,
        task,
        target,
        log_type=LogType.RETRY,
        log_message=(
            f"人工重试：从 {stage} 阶段重新执行"
            f"（第 {task.retry_count + 1} 次）｜原错误 {task.error_code or '-'}"
        ),
        level=LogLevel.WARNING,
    )
    return stage


async def retry_task(
    task_id: int,
    *,
    client: ApprovalSystemClient | None = None,
    llm: Any = None,
    settings: Any = None,
) -> RetryOutcome:
    """IF-20：从失败阶段重试 ``blocked`` 任务，并继续跑到完成。"""
    started = time.perf_counter()

    async with session_scope() as session:
        task = await session.get(ApprovalTask, task_id)
        if task is None:
            raise AppError(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"任务 {task_id} 不存在",
                task_id=task_id,
                detail={"task_id": task_id},
            )
        # _enter_retry_stage 可能抛 VALIDATION_ERROR；失败路径也要 commit 以便留痕
        stage = await _enter_retry_stage(session, task)
        await session.commit()

    logger.info("人工重试 task_id=%s 从 %s 阶段重新执行", task_id, stage)

    if stage == BLOCKED_STAGE_PARSING:
        # 重新下载 + 解析（内部各自提交，任一失败会重新置 blocked 并向上抛）
        await parse_task(task_id, client=client, settings=settings)

    pipeline = await run_full_review(task_id, llm=llm, settings=settings)

    async with session_scope() as session:
        task = await session.get(ApprovalTask, task_id)
        review = await get_review_result(session, task_id)
        await write_task_log(
            session,
            task_id,
            LogType.RETRY,
            f"人工重试完成：从 {stage} 阶段恢复并跑完剩余流程｜"
            f"任务状态 {task.task_status}｜重试次数 {task.retry_count}｜"
            f"整体风险 {pipeline.saved.overall_risk_level}｜"
            f"耗时 {format_duration(time.perf_counter() - started)}",
        )
        await session.commit()
        outcome = RetryOutcome(
            task_id=task_id,
            resumed_stage=stage,
            task_status=task.task_status,
            retry_count=task.retry_count,
            review_id=review.id if review is not None else pipeline.saved.review_id,
            overall_risk_level=pipeline.saved.overall_risk_level,
            comment_text=pipeline.saved.comment_text,
            blocked_stage=task.blocked_stage,
            error_code=task.error_code,
        )
    return outcome
