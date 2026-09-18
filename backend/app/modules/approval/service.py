"""Approval Module — 待办拉取 / 审批详情 / 任务去重（SPEC §4.1 FR-APP-01…FR-APP-06）。

去重实现口径（FR-APP-03：**必须以数据库唯一索引为最终保障**）：

1. 批量 ``SELECT`` 出本批已存在的 ``instance_id`` —— 仅用于判定 ``dedup=created/updated``，
   **不承担**唯一性职责；
2. 新任务走 ``INSERT ... ON DUPLICATE KEY UPDATE``，旧任务走 ``UPDATE``；
   并发下两条请求同时插入时，唯一索引 ``uk_approval_tasks_instance_id`` 会让
   其中一条落到 ``ON DUPLICATE KEY UPDATE`` 分支，因此**绝不会产生第二个任务**。

状态保护（FR-APP-05）：更新语句**不含** ``task_status`` / ``write_status`` /
``blocked_stage`` / ``error_code`` / ``retry_count``，重复拉取不会把已 ``done`` 的任务拉回 ``pending``。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.approval_client import ApprovalSystemClient
from app.core.enums import LogType
from app.core.logging import get_logger
from app.core.timeutil import parse_api_time, to_api_time
from app.db.models import ApprovalTask
from app.modules.logging.service import write_task_log
from app.schemas.approval import (
    ApprovalDetail,
    ApprovalListItem,
    AttachmentInfo,
    PullResult,
)

logger = get_logger(__name__)

#: 仅这些列会随重复拉取被刷新；任务态与异常字段一律不动（FR-APP-05）
_REFRESHABLE_FIELDS = (
    "approval_code",
    "approval_title",
    "applicant_name",
    "apply_time",
    "attachment_count",
    "current_status",
)


def _fields_from_remote(raw: dict[str, Any]) -> dict[str, Any]:
    """审批系统返回值 → ``approval_tasks`` 列值。"""
    return {
        "instance_id": raw["instance_id"],
        "approval_code": raw.get("approval_code") or "",
        "approval_title": raw.get("approval_title") or "",
        "applicant_name": raw.get("applicant_name") or "",
        "apply_time": parse_api_time(raw.get("apply_time")),
        "attachment_count": int(raw.get("attachment_count") or 0),
        "current_status": raw.get("current_status"),
    }


async def upsert_pending_approvals(
    session: AsyncSession,
    raw_items: list[dict[str, Any]],
) -> dict[str, str]:
    """**阶段 1（写事务）**：按 ``instance_id`` 幂等 upsert，返回 ``instance_id → created/updated``。

    只做写入，不做回读，也不做 HTTP——让事务尽可能短。
    并发场景下这是避免 ``INSERT ... ON DUPLICATE KEY UPDATE`` 之间死锁的关键。
    """
    instance_ids = [str(item["instance_id"]) for item in raw_items]
    existing_rows = (
        await session.execute(
            select(ApprovalTask.id, ApprovalTask.instance_id).where(
                ApprovalTask.instance_id.in_(instance_ids)
            )
        )
    ).all()
    already_known = {row.instance_id for row in existing_rows}

    dedup_map: dict[str, str] = {}
    for item in raw_items:
        fields = _fields_from_remote(item)
        instance_id = fields["instance_id"]
        if instance_id in already_known:
            # 已存在 → 只刷新审批侧信息，绝不新建第二个任务（FR-APP-02）
            await session.execute(
                ApprovalTask.__table__.update()
                .where(ApprovalTask.instance_id == instance_id)
                .values(**{key: fields[key] for key in _REFRESHABLE_FIELDS})
            )
            dedup_map[instance_id] = "updated"
        else:
            # 新增 → 依赖唯一索引兜底并发（FR-APP-03）：
            # 两条请求同时插入同一 instance_id 时，后到者落到 ON DUPLICATE KEY UPDATE 分支。
            statement = mysql_insert(ApprovalTask.__table__).values(**fields)
            statement = statement.on_duplicate_key_update(
                **{key: fields[key] for key in _REFRESHABLE_FIELDS}
            )
            await session.execute(statement)
            dedup_map[instance_id] = "created"
    await session.flush()
    return dedup_map


async def finalize_pull(
    session: AsyncSession,
    raw_items: list[dict[str, Any]],
    dedup_map: dict[str, str],
) -> PullResult:
    """**阶段 2（新事务）**：回读任务主键与状态，组装 IF-01 结果并落 ``log_type=pull`` 日志。

    之所以必须换一个事务：MySQL 默认 REPEATABLE READ 下，普通读使用事务快照，
    并发双拉时后到的事务可能看不到另一事务刚提交的行（其 ``ON DUPLICATE KEY UPDATE``
    按 FR-APP-05 不改任务态字段，值相同则不产生实际变更，该行不归入本事务）。
    新事务的快照天然包含全部已提交数据，既拿到权威 ``task_id``，又不必加锁——
    曾经试过 ``SELECT ... FOR UPDATE``，结果把可见性问题换成了 1213 死锁，故不采用。
    """
    if not raw_items:
        return PullResult(items=[], created_count=0, updated_count=0)

    instance_ids = [str(item["instance_id"]) for item in raw_items]
    rows = (
        await session.execute(
            select(ApprovalTask.id, ApprovalTask.instance_id, ApprovalTask.task_status).where(
                ApprovalTask.instance_id.in_(instance_ids)
            )
        )
    ).all()
    local_by_instance = {row.instance_id: row for row in rows}

    items: list[ApprovalListItem] = []
    for raw in raw_items:
        instance_id = str(raw["instance_id"])
        local = local_by_instance.get(instance_id)
        if local is None:  # 理论上不会发生；真发生也不能静默吞掉
            logger.error("回读任务失败：instance_id=%s 在库中不存在，已跳过", instance_id)
            continue
        items.append(
            ApprovalListItem(
                instance_id=instance_id,
                approval_code=raw.get("approval_code") or "",
                approval_title=raw.get("approval_title") or "",
                applicant_name=raw.get("applicant_name") or "",
                apply_time=to_api_time(parse_api_time(raw.get("apply_time"))),
                attachment_count=int(raw.get("attachment_count") or 0),
                current_status=raw.get("current_status"),
                task_id=local.id,
                task_status=local.task_status,
                dedup=dedup_map.get(instance_id, "updated"),
            )
        )
        # FR-LOG-01：待办获取必须落 task_logs（ST-01-05 状态迁移留痕）
        action = "新建任务" if dedup_map.get(instance_id) == "created" else "命中已存在任务，更新审批信息"
        await write_task_log(
            session,
            local.id,
            LogType.PULL,
            f"待办拉取：{action}｜instance_id={instance_id}｜"
            f"approval_code={raw.get('approval_code')}｜附件 {raw.get('attachment_count') or 0} 个",
        )

    created_count = sum(1 for value in dedup_map.values() if value == "created")
    return PullResult(
        items=items,
        created_count=created_count,
        updated_count=len(items) - created_count,
    )


async def fetch_approval_detail(
    session: AsyncSession,
    instance_id: str,
    client: ApprovalSystemClient | None = None,
) -> ApprovalDetail:
    """IF-02 的实现：审批系统详情 + 本系统任务映射（只读，不写库）。

    ``instance_id`` 不存在 → ``APPROVAL_NOT_FOUND``（由客户端抛出）。
    """
    client = client or ApprovalSystemClient()
    raw = await client.get_approval(instance_id)

    task_row = (
        await session.execute(
            select(ApprovalTask.id, ApprovalTask.task_status).where(
                ApprovalTask.instance_id == instance_id
            )
        )
    ).first()

    attachments = [
        AttachmentInfo(
            attachment_id=str(item.get("attachment_id") or item.get("attachment_code") or ""),
            file_name=item.get("file_name") or "",
            file_type=item.get("file_type") or "",
            file_size=item.get("file_size"),
        )
        for item in raw.get("attachments") or []
    ]

    return ApprovalDetail(
        instance_id=instance_id,
        approval_code=raw.get("approval_code") or "",
        approval_title=raw.get("approval_title") or "",
        applicant_name=raw.get("applicant_name") or "",
        apply_time=to_api_time(parse_api_time(raw.get("apply_time"))),
        current_status=raw.get("current_status"),
        contract_type=raw.get("contract_type"),
        form_data=dict(raw.get("form_data") or {}),
        attachments=attachments,
        task_id=task_row.id if task_row else None,
        task_status=task_row.task_status if task_row else None,
    )


async def list_local_tasks(
    session: AsyncSession,
    status: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[ApprovalTask], int]:
    """IF-11 的数据来源：本系统任务分页查询（可按 task_status 过滤）。"""
    conditions = []
    if status:
        conditions.append(ApprovalTask.task_status == status)

    total = await session.scalar(
        select(func.count()).select_from(ApprovalTask).where(*conditions)
    )
    rows = (
        await session.execute(
            select(ApprovalTask)
            .where(*conditions)
            .order_by(ApprovalTask.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return list(rows), int(total or 0)


async def get_local_task(session: AsyncSession, task_id: int) -> ApprovalTask | None:
    """IF-12 的数据来源：按 ``task_id``（即 ``case_id``）取任务（含附件）。"""
    return await session.get(ApprovalTask, task_id)


__all__ = [
    "fetch_approval_detail",
    "finalize_pull",
    "get_local_task",
    "list_local_tasks",
    "upsert_pending_approvals",
]
