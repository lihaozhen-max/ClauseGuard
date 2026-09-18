"""Attachment Module — 附件下载 / 落盘消毒 / SHA-256 校验（SPEC §4.2、IF-03）。

安全要点：
- 附件落盘到 ``STORAGE_DIR/<task_id>/`` 子目录（FR-ATT-03）；
- 文件名**必须**做路径穿越消毒（``..``、绝对路径、Windows 保留名）；
- 附件**禁止**通过任何静态路由对外暴露（FR-SYS-02）——本模块只写文件，不注册路由。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.approval_client import ApprovalSystemClient
from app.core.config import PROJECT_ROOT, get_settings
from app.core.enums import DownloadStatus, LogType, TaskStatus
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.db.models import ApprovalAttachment, ApprovalTask
from app.modules.approval.state import (
    BLOCKED_STAGE_PARSING,
    fail_and_block,
    transition_to,
)
from app.modules.logging.service import write_task_log
from app.schemas.attachment import AttachmentDownloadResult

logger = get_logger(__name__)

#: 落盘文件名长度上限（含扩展名）
MAX_FILE_NAME_LENGTH = 100

#: Windows 不允许的文件名字符
_ILLEGAL_CHARS = set('<>:"/\\|?*')

#: Windows 保留设备名（带扩展名同样被保留，如 CON.pdf）
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: DT-02 ``file_type`` 允许值（SPEC §6）
KNOWN_FILE_TYPES = {"pdf", "docx", "png", "jpg", "jpeg"}

#: 重复下载时可被刷新的列（FR-ATT-06：覆盖更新，不新增记录）
_REFRESHABLE_FIELDS = (
    "file_name",
    "file_type",
    "file_path",
    "file_size",
    "file_checksum",
    "download_status",
)


def sanitize_filename(file_name: str, fallback: str = "attachment") -> str:
    """路径穿越消毒（FR-ATT-03 / TS-19）。

    处理：目录成分剥离 → 非法字符剔除 → 首尾点与空格清理 → 保留名与长度处理。
    消毒后的名字**只**是一个文件名，不含任何目录分隔符。
    """
    raw = (file_name or "").strip().replace("\\", "/")
    name = raw.split("/")[-1]  # 绝对路径与 ../ 都在这一步被剥掉
    name = "".join(ch for ch in name if ch not in _ILLEGAL_CHARS and ord(ch) >= 32)
    name = name.lstrip(". ").rstrip(". ")
    if not name:
        return fallback

    stem, dot, suffix = name.rpartition(".")
    if not dot:  # 无扩展名
        stem, suffix = name, ""
    if stem.upper() in _RESERVED_NAMES:
        stem = f"_{stem}"
    if len(name) > MAX_FILE_NAME_LENGTH:
        keep = MAX_FILE_NAME_LENGTH - len(suffix) - (1 if suffix else 0)
        stem = stem[: max(keep, 1)]
    return f"{stem}.{suffix}" if suffix else stem


def guess_file_type(file_name: str) -> str:
    """按扩展名给出 DT-02 的 ``file_type``（解析阶段的真实类型由魔数复核，PS-01）。"""
    suffix = Path(file_name).suffix.lower().lstrip(".")
    return suffix if suffix in KNOWN_FILE_TYPES else (suffix or "unknown")


def _storage_target(task_id: int, attachment_id: str, file_name: str) -> tuple[Path, str]:
    """返回 ``(绝对落盘路径, 项目根相对路径)``，并二次校验没有逃出任务目录。"""
    settings = get_settings()
    task_dir = (settings.storage_path / str(task_id)).resolve()
    stored_name = f"{sanitize_filename(attachment_id, 'ATT')}_{sanitize_filename(file_name)}"
    target = (task_dir / stored_name).resolve()

    if not target.is_relative_to(task_dir):  # 纵深防御：消毒失效也不能写到目录外
        raise AppError(
            ErrorCode.DOWNLOAD_FAILED,
            f"附件文件名消毒后仍越界：{file_name!r}",
            task_id=task_id,
            detail={"file_name": file_name},
        )
    # 库里存"项目根相对路径"：不随进程 CWD 漂移，也不对外暴露绝对路径
    return target, target.relative_to(PROJECT_ROOT)


def _write_file(target: Path, content: bytes) -> None:
    """先写临时文件再原子替换，避免半截文件被后续解析读到。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".part")
    temp.write_bytes(content)
    os.replace(temp, target)


async def download_attachment(
    session: AsyncSession,
    task: ApprovalTask,
    attachment_id: str,
    file_name: str | None = None,
    client: ApprovalSystemClient | None = None,
) -> AttachmentDownloadResult:
    """IF-03 的核心实现：下载附件 → 落盘 → 记录元数据。

    异常（SPEC §5.4）：
    - 附件不存在（HTTP 404）→ ``CONTRACT_ATTACHMENT_MISSING``，任务 ``blocked``/``parsing``；
    - 其余下载失败 → ``DOWNLOAD_FAILED``，并记录请求信息、返回状态、错误信息（FR-ATT-05）。
    """
    settings = get_settings()
    client = client or ApprovalSystemClient()
    instance_id = task.instance_id
    effective_name = sanitize_filename(file_name or attachment_id)

    try:
        response = await client.download_attachment(instance_id, attachment_id)
    except AppError as exc:
        # 审批系统整体不可达/5xx → 对附件模块而言就是"下载失败"
        if exc.code is ErrorCode.APPROVAL_API_ERROR:
            await fail_and_block(
                session,
                task,
                stage=BLOCKED_STAGE_PARSING,
                error_code=ErrorCode.DOWNLOAD_FAILED,
                log_type=LogType.DOWNLOAD,
                message=(
                    f"附件下载失败：instance_id={instance_id} attachment_id={attachment_id}"
                    f"｜请求 {exc.detail.get('method')} {exc.detail.get('url')}"
                    f"｜错误 {exc.detail.get('error')}"
                ),
            )
            raise AppError(
                ErrorCode.DOWNLOAD_FAILED,
                f"附件 {attachment_id} 下载失败",
                task_id=task.id,
                detail={"instance_id": instance_id, "attachment_id": attachment_id, **exc.detail},
            ) from exc
        raise

    if response.status_code == 404:
        message = f"附件不存在：instance_id={instance_id}，attachment_id={attachment_id}"
        await fail_and_block(
            session,
            task,
            stage=BLOCKED_STAGE_PARSING,
            error_code=ErrorCode.CONTRACT_ATTACHMENT_MISSING,
            log_type=LogType.DOWNLOAD,
            message=message,
        )
        raise AppError(
            ErrorCode.CONTRACT_ATTACHMENT_MISSING,
            f"审批单 {instance_id} 未提供合同附件 {attachment_id}",
            task_id=task.id,
            detail={"instance_id": instance_id, "attachment_id": attachment_id},
        )

    if response.status_code >= 400:
        # FR-ATT-05：记录请求信息、返回状态、错误信息
        message = (
            f"附件下载失败：instance_id={instance_id} attachment_id={attachment_id}"
            f"｜HTTP {response.status_code}｜{response.text[:200]}"
        )
        await fail_and_block(
            session,
            task,
            stage=BLOCKED_STAGE_PARSING,
            error_code=ErrorCode.DOWNLOAD_FAILED,
            log_type=LogType.DOWNLOAD,
            message=message,
        )
        raise AppError(
            ErrorCode.DOWNLOAD_FAILED,
            f"附件 {attachment_id} 下载失败（HTTP {response.status_code}）",
            task_id=task.id,
            detail={
                "instance_id": instance_id,
                "attachment_id": attachment_id,
                "status_code": response.status_code,
            },
        )

    content = response.content
    checksum = hashlib.sha256(content).hexdigest()
    file_size = len(content)

    # 审批系统返回的 Content-Disposition 可能带真实文件名；优先用调用方给的名字
    resolved_name = file_name or _filename_from_headers(response) or attachment_id
    safe_name = sanitize_filename(resolved_name)
    target, relative_path = _storage_target(task.id, attachment_id, safe_name)
    _write_file(target, content)

    values = {
        "task_id": task.id,
        "attachment_code": attachment_id,
        "file_name": safe_name,
        "file_type": guess_file_type(safe_name),
        "file_path": relative_path.as_posix(),
        "file_size": file_size,
        "file_checksum": checksum,
        "download_status": DownloadStatus.SUCCESS.value,
    }
    statement = mysql_insert(ApprovalAttachment.__table__).values(**values)
    # FR-ATT-06：重复下载覆盖更新，不产生第二条记录（UNIQUE(task_id, attachment_code) 兜底）
    await session.execute(
        statement.on_duplicate_key_update(
            **{key: values[key] for key in _REFRESHABLE_FIELDS}
        )
    )
    await session.flush()

    # ST-01：pending → parsing 表示"已开始下载/解析"；
    # 处于 blocked 时再次下载视为人工重试（blocked → parsing，ST-01-02 会清断点并累加 retry_count）
    if TaskStatus(task.task_status) in {TaskStatus.PENDING, TaskStatus.BLOCKED}:
        await transition_to(
            session,
            task,
            TaskStatus.PARSING,
            log_type=LogType.DOWNLOAD,
            log_message=f"开始处理：已下载附件 {safe_name}（task_id={task.id}）",
        )

    await write_task_log(
        session,
        task.id,
        LogType.DOWNLOAD,
        f"附件下载成功：{safe_name}｜{file_size} 字节｜SHA-256 {checksum[:16]}…｜存储 {relative_path}",
    )
    logger.info(
        "附件下载成功 task_id=%s attachment_id=%s %s（%s 字节）",
        task.id,
        attachment_id,
        safe_name,
        file_size,
    )

    return AttachmentDownloadResult(
        task_id=task.id,
        attachment_id=attachment_id,
        file_name=safe_name,
        file_type=values["file_type"],
        file_path=relative_path.as_posix(),
        file_size=file_size,
        file_checksum=checksum,
        download_status=DownloadStatus.SUCCESS.value,
    )


def _filename_from_headers(response) -> str | None:
    """尽力从 Content-Disposition 还原文件名（不含路径）。"""
    disposition = response.headers.get("content-disposition", "")
    marker = "filename*=UTF-8''"
    if marker in disposition:
        from urllib.parse import unquote

        return unquote(disposition.split(marker, 1)[1].split(";")[0].strip().strip('"'))
    if "filename=" in disposition:
        return disposition.split("filename=", 1)[1].split(";")[0].strip().strip('"')
    return None


async def list_task_attachments(session: AsyncSession, task_id: int) -> list[ApprovalAttachment]:
    """任务下已登记的附件（供 IF-12 / 解析编排使用）。"""
    rows = await session.execute(
        select(ApprovalAttachment)
        .where(ApprovalAttachment.task_id == task_id)
        .order_by(ApprovalAttachment.id)
    )
    return list(rows.scalars().all())


def absolute_path_of(attachment: ApprovalAttachment) -> Path:
    """把库里的项目根相对路径还原为绝对路径（只在后端使用，禁止对外暴露）。"""
    return (PROJECT_ROOT / (attachment.file_path or "")).resolve()
