"""任务状态机（SPEC §7 ST-01）。

把"哪些迁移合法、进入 blocked 要写什么、离开 blocked 要清什么"收敛到一处，
避免各模块各自赋值 ``task_status`` 导致非法迁移（如把 ``done`` 拉回 ``pending``）。

合法迁移**仅**下表所列（SPEC ST-01），其余一律拒绝：

| 从 | 到 | 触发条件 |
|---|---|---|
| — | `pending` | 待办拉取创建任务（由 INSERT 完成，不走本模块） |
| `pending` | `parsing` | 开始下载/解析 |
| `parsing` | `reviewing` | 解析成功 |
| `reviewing` | `done` | 审查结果保存成功 |
| `parsing` | `blocked` | 附件缺失/下载失败/内容为空/OCR 失败/解析失败 |
| `reviewing` | `blocked` | 规则执行异常/审批接口异常 |
| `pending` | `blocked` | 审批接口异常 |
| `blocked` | `parsing` | 人工重试，``blocked_stage=parsing`` |
| `blocked` | `reviewing` | 人工重试，``blocked_stage=reviewing`` |
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import LogLevel, LogType, TaskStatus
from app.core.errors import ErrorCode
from app.core.logging import get_logger
from app.core.timeutil import utc_now
from app.db.models import ApprovalTask
from app.modules.logging.service import write_task_log

logger = get_logger(__name__)

#: 合法迁移表（SPEC ST-01）
LEGAL_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.PARSING, TaskStatus.BLOCKED}),
    TaskStatus.PARSING: frozenset({TaskStatus.REVIEWING, TaskStatus.BLOCKED}),
    TaskStatus.REVIEWING: frozenset({TaskStatus.DONE, TaskStatus.BLOCKED}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.PARSING, TaskStatus.REVIEWING}),
    TaskStatus.DONE: frozenset(),
}

#: blocked_stage 取值（DT-01）
BLOCKED_STAGE_PARSING = "parsing"
BLOCKED_STAGE_REVIEWING = "reviewing"


class IllegalStateTransition(RuntimeError):
    """非法状态迁移（SPEC ST-01-03 等）。"""


async def transition_to(
    session: AsyncSession,
    task: ApprovalTask,
    target: TaskStatus,
    *,
    log_type: LogType,
    log_message: str | None = None,
    blocked_stage: str | None = None,
    error_code: ErrorCode | None = None,
    error_message: str | None = None,
    level: LogLevel = LogLevel.INFO,
) -> None:
    """执行一次任务状态迁移；非法迁移直接抛错，不做"尽力而为"。

    - **ST-01-01**：进入 ``blocked`` 必须同时写 ``blocked_stage`` 与 ``error_code``；
    - **ST-01-02**：离开 ``blocked`` 时清空二者并把 ``retry_count`` 加 1；
    - **ST-01-03**：``done`` 是终态，任何退回都被拒绝；
    - **ST-01-05**：每次迁移更新 ``updated_at`` 并写 ``task_logs``。
    """
    source = TaskStatus(task.task_status)
    if target not in LEGAL_TRANSITIONS[source]:
        raise IllegalStateTransition(
            f"非法状态迁移：{source.value} → {target.value}"
            f"（task_id={task.id}，合法目标 {sorted(t.value for t in LEGAL_TRANSITIONS[source])}）"
        )

    if target is TaskStatus.BLOCKED:
        if not blocked_stage or error_code is None:
            raise ValueError("进入 blocked 必须同时提供 blocked_stage 与 error_code（ST-01-01）")
        if blocked_stage not in {BLOCKED_STAGE_PARSING, BLOCKED_STAGE_REVIEWING}:
            raise ValueError(f"非法 blocked_stage：{blocked_stage}")
        task.blocked_stage = blocked_stage
        task.error_code = error_code.value
        task.error_message = error_message
    elif source is TaskStatus.BLOCKED:
        # ST-01-02：清空断点信息并累计重试次数
        task.blocked_stage = None
        task.error_code = None
        task.error_message = None
        task.retry_count = (task.retry_count or 0) + 1

    task.task_status = target.value
    task.updated_at = utc_now()

    message = log_message or f"任务状态迁移：{source.value} → {target.value}"
    await write_task_log(session, task.id, log_type, message, level=level)
    logger.info(
        "任务状态迁移 task_id=%s %s → %s%s",
        task.id,
        source.value,
        target.value,
        f"（blocked_stage={task.blocked_stage}, error_code={task.error_code}）"
        if target is TaskStatus.BLOCKED
        else "",
    )


async def fail_and_block(
    session: AsyncSession,
    task: ApprovalTask,
    *,
    stage: str,
    error_code: ErrorCode,
    log_type: LogType,
    message: str,
) -> None:
    """把任务置为 ``blocked``，并正确处理重复失败与已完成任务。

    - 处于 ``pending``/``parsing``/``reviewing`` → 正常迁移到 ``blocked``（ST-01）；
    - 已经 ``blocked`` → **不做**非法迁移，只刷新 ``error_code``/``error_message`` 并记日志；
    - 已经 ``done`` → **禁止**改任务状态（ST-01-03/ST-01-04 精神），只记 warning 日志。
    """
    status = TaskStatus(task.task_status)

    if status is TaskStatus.BLOCKED:
        task.blocked_stage = stage
        task.error_code = error_code.value
        task.error_message = message
        task.updated_at = utc_now()
        await write_task_log(session, task.id, log_type, message, level=LogLevel.ERROR)
        logger.error("任务已处于 blocked，刷新错误信息 task_id=%s error_code=%s", task.id, error_code.value)
        return

    if status is TaskStatus.DONE:
        await write_task_log(
            session,
            task.id,
            log_type,
            f"任务已完成（done），按 ST-01-03 不回退状态，仅记录异常：{message}",
            level=LogLevel.WARNING,
        )
        logger.warning("已完成任务出现异常，保持 done task_id=%s：%s", task.id, message)
        return

    await transition_to(
        session,
        task,
        TaskStatus.BLOCKED,
        log_type=log_type,
        log_message=message,
        blocked_stage=stage,
        error_code=error_code,
        error_message=message,
        level=LogLevel.ERROR,
    )
