"""工具接口层：SPEC §5.1 的 7 个工具，本模块实现 IF-01 / IF-02。

**签名与 PRD §15 一致，禁止改名改参数**：
``list_pending_contract_approvals(limit)``、``get_contract_approval(instance_id)``。

工具自带会话（可能被 Agent 直接调用），因此不把 ``session`` 暴露为必填参数；
``client`` 为**仅限测试**的关键字参数（注入内存 ASGI 传输），不影响对外签名。

IF-01 用**两个短事务**编排（阶段划分与原因见 ``modules/approval/service.py``）：
HTTP 拉取 → 写事务（幂等 upsert）→ 读事务（回读 + 落日志）。
"""

from __future__ import annotations

import time

from app.clients.approval_client import ApprovalSystemClient
from app.core.logging import get_logger
from app.core.timeutil import format_duration
from app.db.session import session_scope
from app.modules.approval.service import (
    fetch_approval_detail,
    finalize_pull,
    upsert_pending_approvals,
)
from app.schemas.approval import ApprovalDetail, PullResult

logger = get_logger(__name__)


async def list_pending_contract_approvals(
    limit: int = 20,
    *,
    client: ApprovalSystemClient | None = None,
) -> PullResult:
    """IF-01：拉取待处理审批单并按 ``instance_id`` 幂等入库（FR-APP-01/02/03/05）。"""
    client = client or ApprovalSystemClient()
    started = time.perf_counter()

    # HTTP 调用放在数据库事务之外，避免把网络等待变成持锁时间
    raw_items = await client.list_pending(limit)
    logger.info("待办拉取：审批系统返回 %d 条（limit=%d）", len(raw_items), limit)
    if not raw_items:
        return PullResult(items=[], created_count=0, updated_count=0)

    async with session_scope() as session:
        dedup_map = await upsert_pending_approvals(session, raw_items)
        await session.commit()

    async with session_scope() as session:
        result = await finalize_pull(session, raw_items, dedup_map)
        await session.commit()

    logger.info(
        "待办拉取完成：新增 %d、更新 %d，耗时 %s",
        result.created_count,
        result.updated_count,
        format_duration(time.perf_counter() - started),
    )
    return result


async def get_contract_approval(
    instance_id: str,
    *,
    client: ApprovalSystemClient | None = None,
) -> ApprovalDetail:
    """IF-02：按 ``instance_id`` 获取审批详情（FR-APP-04）。

    实例不存在 → 404 ``APPROVAL_NOT_FOUND``。
    """
    async with session_scope() as session:
        return await fetch_approval_detail(session, instance_id, client=client)
