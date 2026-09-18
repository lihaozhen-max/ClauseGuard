"""附件工具接口 IF-03（签名与 PRD §15 一致：``download_contract_attachment(instance_id, attachment_id, file_name)``）。

失败路径也要保住状态：下载失败/附件缺失时任务要转 ``blocked`` 并落日志（NF-04：失败后禁止丢失
已生成数据），因此工具层在异常路径上**显式 commit** 后再抛出。
"""

from __future__ import annotations

from sqlalchemy import select

from app.clients.approval_client import ApprovalSystemClient
from app.core.errors import AppError, ErrorCode
from app.db.models import ApprovalTask
from app.db.session import session_scope
from app.modules.attachment.service import download_attachment
from app.schemas.attachment import AttachmentDownloadResult


async def download_contract_attachment(
    instance_id: str,
    attachment_id: str,
    file_name: str | None = None,
    *,
    client: ApprovalSystemClient | None = None,
) -> AttachmentDownloadResult:
    """IF-03：按 ``instance_id`` + ``attachment_id`` 下载附件并落盘（FR-ATT-01/02/03）。

    前置条件：该审批实例必须已经拉取为任务（IF-01）。未拉取 → ``APPROVAL_NOT_FOUND``。

    异常（§5.4）：
    - 附件不存在 → ``CONTRACT_ATTACHMENT_MISSING`` + 任务 ``blocked``/``parsing``；
    - 下载失败 → ``DOWNLOAD_FAILED`` + 任务 ``blocked``/``parsing``。
    """
    async with session_scope() as session:
        task = await session.scalar(
            select(ApprovalTask).where(ApprovalTask.instance_id == instance_id)
        )
        if task is None:
            raise AppError(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"审批实例 {instance_id} 尚未拉取为任务，请先调用 list_pending_contract_approvals",
                detail={"instance_id": instance_id},
            )
        try:
            result = await download_attachment(
                session, task, attachment_id, file_name, client=client
            )
        except AppError:
            # 失败态（blocked + error_code + 日志）必须落库，否则恢复现场就丢了（NF-04）
            await session.commit()
            raise
        await session.commit()
    return result
