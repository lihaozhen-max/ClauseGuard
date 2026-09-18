"""Logging Module — 8 类核心操作落 ``task_logs``（SPEC FR-LOG-01…FR-LOG-05、DT-08）。

约束：
- 每条日志必须含 ``task_id`` / ``log_level`` / ``log_type`` / ``log_content`` / ``created_at``（FR-LOG-02）；
- 写库前**必须**脱敏（禁止 API Key/Token/密码/连接串，FR-LOG-03）；
- 合同原文截断至 ≤ 500 字符（FR-LOG-04）；
- 依赖方向铁律：本模块**禁止**反向依赖任何业务模块（架构图 FIG-03）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.enums import LogLevel, LogType, enum_values
from app.core.logging import get_logger, redactor
from app.core.textutil import DEFAULT_LOG_TEXT_LIMIT, truncate
from app.db.models import TaskLog

logger = get_logger(__name__)

_VALID_LEVELS = enum_values(LogLevel)
_VALID_TYPES = enum_values(LogType)


def sanitize(content: str) -> str:
    """脱敏 + 截断，任何写入 ``task_logs`` 的文本都必须先过这里。"""
    redactor.add_secrets(get_settings().secrets_to_redact)
    return truncate(redactor.redact(content), DEFAULT_LOG_TEXT_LIMIT)


async def write_task_log(
    session: AsyncSession,
    task_id: int,
    log_type: LogType | str,
    content: str,
    level: LogLevel | str = LogLevel.INFO,
) -> TaskLog:
    """写一条任务日志（调用方负责 commit）。

    ``log_level`` / ``log_type`` 必须取自 SPEC §2.2 枚举，传错即报错——
    避免拼错的动作静默写进库、到 AC18 才发现少了某类日志。
    """
    level_value = str(level)
    type_value = str(log_type)
    if level_value not in _VALID_LEVELS:
        raise ValueError(f"非法 log_level：{level_value}（合法值 {sorted(_VALID_LEVELS)}）")
    if type_value not in _VALID_TYPES:
        raise ValueError(f"非法 log_type：{type_value}（合法值 {sorted(_VALID_TYPES)}）")

    safe_content = sanitize(content)
    entry = TaskLog(
        task_id=task_id,
        log_level=level_value,
        log_type=type_value,
        log_content=safe_content,
    )
    session.add(entry)

    # 同时落进程日志，便于演示时实时观察（同样已脱敏）
    log_fn = {
        LogLevel.INFO.value: logger.info,
        LogLevel.WARNING.value: logger.warning,
        LogLevel.ERROR.value: logger.error,
    }[level_value]
    log_fn("[task=%s][%s] %s", task_id, type_value, safe_content)
    return entry


async def list_task_logs(
    session: AsyncSession,
    task_id: int,
    *,
    log_type: str | None = None,
    level: str | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[TaskLog], int]:
    """任务日志查询（IF-19 / AC18）：返回 ``(本页日志, 总数)``。

    可按 ``log_type``、``log_level`` 过滤；按 id 正序返回——按链路顺序阅读最自然。
    """
    conditions = [TaskLog.task_id == task_id]
    if log_type:
        conditions.append(TaskLog.log_type == log_type)
    if level:
        conditions.append(TaskLog.log_level == level)

    total = await session.scalar(select(func.count()).select_from(TaskLog).where(*conditions))
    rows = (
        await session.execute(
            select(TaskLog)
            .where(*conditions)
            .order_by(TaskLog.id)
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return list(rows), int(total or 0)


async def collect_log_types(session: AsyncSession, task_id: int) -> set[str]:
    """该任务出现过的全部 ``log_type``（AC18 核验 8 类核心操作是否齐备）。"""
    rows = await session.execute(
        select(TaskLog.log_type).where(TaskLog.task_id == task_id).distinct()
    )
    return {str(row[0]) for row in rows}
