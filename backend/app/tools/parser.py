"""解析工具接口 IF-04 与"下载 + 解析"编排。

- ``parse_contract_document(document_id)``：PRD §15 的工具签名，解析**一个**文档；
- ``parse_task(task_id)``：IF-14 的内部编排（拿附件 → 下载 → 解析），供内部 REST 调用。

``document_id`` 的解析口径（SPEC §2.1 规定 ``document_id = contract_parses.id``）：
先按 ``contract_parses.id`` 查；查不到再按 ``approval_attachments.id`` 查——
后者覆盖"首次解析、尚无解析记录"的场景，使 IF-04 在任何时刻都可被调用。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.approval_client import ApprovalSystemClient
from app.core.errors import AppError, ErrorCode
from app.db.models import ApprovalAttachment, ApprovalTask, ContractParse
from app.db.session import session_scope
from app.modules.approval.service import fetch_approval_detail
from app.modules.attachment.service import download_attachment, list_task_attachments
from app.modules.parser.service import ParseOutcome, parse_attachment
from app.schemas.approval import ApprovalDetail


async def _resolve_document(
    session: AsyncSession, document_id: int
) -> tuple[ApprovalTask, ApprovalAttachment]:
    """把 ``document_id`` 解析为 ``(任务, 附件)``。"""
    row = await session.get(ContractParse, document_id)
    attachment: ApprovalAttachment | None = None
    task: ApprovalTask | None = None

    if row is not None:
        attachment = await session.get(ApprovalAttachment, row.attachment_id)
        task = await session.get(ApprovalTask, row.task_id)

    if attachment is None:
        attachment = await session.get(ApprovalAttachment, document_id)
        if attachment is not None:
            task = await session.get(ApprovalTask, attachment.task_id)

    if attachment is None or task is None:
        raise AppError(
            ErrorCode.APPROVAL_NOT_FOUND,
            f"文档 {document_id} 不存在（既不是解析记录 id，也不是附件 id）",
            detail={"document_id": document_id},
        )
    return task, attachment


async def parse_contract_document(
    document_id: int,
    *,
    settings=None,
) -> ParseOutcome:
    """IF-04：解析合同文件并提取结构化信息（FR-PARSE-01…10）。

    失败 → 写 ``parse_error`` 并置任务 ``blocked``/``parsing``（§5.4）；异常路径同样 commit。
    """
    async with session_scope() as session:
        task, attachment = await _resolve_document(session, document_id)
        try:
            outcome = await parse_attachment(session, task, attachment, settings=settings)
        except AppError:
            await session.commit()
            raise
        await session.commit()
    return outcome


async def ensure_attachments(
    session: AsyncSession,
    task: ApprovalTask,
    *,
    client: ApprovalSystemClient | None = None,
    detail: ApprovalDetail | None = None,
) -> list[ApprovalAttachment]:
    """确保任务的附件已落盘：库中已有则直接用，否则从审批系统详情逐个下载。"""
    attachments = await list_task_attachments(session, task.id)
    if attachments:
        return attachments

    if detail is None:
        detail = await fetch_approval_detail(session, task.instance_id, client=client)
    for item in detail.attachments:
        await download_attachment(
            session, task, item.attachment_id, item.file_name, client=client
        )
    return await list_task_attachments(session, task.id)


async def parse_task(
    task_id: int,
    *,
    client: ApprovalSystemClient | None = None,
    settings=None,
) -> ParseOutcome:
    """IF-14 的内部编排：取附件（必要时下载）→ 逐个解析。

    多附件时解析全部，返回**第一个**附件的结果作为该任务的主文档结果
    （``IF-13`` 查询时按 id 倒序取最新记录）。
    """
    async with session_scope() as session:
        task = await session.get(ApprovalTask, task_id)
        if task is None:
            raise AppError(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"任务 {task_id} 不存在",
                task_id=task_id,
                detail={"task_id": task_id},
            )
        try:
            attachments = await ensure_attachments(session, task, client=client)
            if not attachments:
                raise AppError(
                    ErrorCode.CONTRACT_ATTACHMENT_MISSING,
                    f"审批单 {task.instance_id} 未提供合同附件",
                    task_id=task.id,
                    detail={"instance_id": task.instance_id},
                )
            outcomes = [
                await parse_attachment(session, task, attachment, settings=settings)
                for attachment in attachments
            ]
        except AppError:
            await session.commit()
            raise
        await session.commit()
    return outcomes[0]


async def get_document_by_task(session: AsyncSession, task_id: int) -> ContractParse | None:
    """IF-13 用：取任务最新解析记录。"""
    return await session.scalar(
        select(ContractParse)
        .where(ContractParse.task_id == task_id)
        .order_by(ContractParse.id.desc())
        .limit(1)
    )
