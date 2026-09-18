"""Parser Module 编排与持久化（SPEC FR-PARSE-01…FR-PARSE-10、IF-04）。

职责边界：
- **本模块**：单个附件 → 解析 → 16 字段 → 写 ``contract_parses``；
- **工具层**（``app/tools/parser.py``）：按 ``task_id`` 编排"下载 + 解析"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.enums import LogLevel, LogType, ParseMode, ParseStatus, TaskStatus
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.timeutil import format_duration
from app.db.models import ApprovalAttachment, ApprovalTask, ContractParse
from app.modules.approval.state import BLOCKED_STAGE_PARSING, fail_and_block, transition_to
from app.modules.attachment.service import absolute_path_of
from app.modules.logging.service import write_task_log
from app.modules.parser.cleaning import build_document
from app.modules.parser.extractors import ExtractionResult, extract_document
from app.modules.parser.fields import extract_fields, summarize_status
from app.modules.parser.models import ParsedDocument

logger = get_logger(__name__)


@dataclass
class ParseOutcome:
    """IF-04 的返回结构。"""

    document_id: int
    task_id: int
    parse_mode: str
    parse_status: str
    basic_info: list[dict[str, Any]] = field(default_factory=list)
    clause_info: list[dict[str, Any]] = field(default_factory=list)
    parse_error: str | None = None
    page_count: int = 0
    used_ocr_pages: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "task_id": self.task_id,
            "parse_mode": self.parse_mode,
            "parse_status": self.parse_status,
            "basic_info": self.basic_info,
            "clause_info": self.clause_info,
            "parse_error": self.parse_error,
            "page_count": self.page_count,
            "used_ocr_pages": self.used_ocr_pages,
            "warnings": self.warnings,
        }


async def _upsert_parse_row(
    session: AsyncSession,
    *,
    task: ApprovalTask,
    attachment: ApprovalAttachment,
    values: dict[str, Any],
) -> ContractParse:
    """按 ``(task_id, attachment_id)`` upsert（FR-PARSE-09：重复解析更新原记录，不新增）。"""
    statement = mysql_insert(ContractParse.__table__).values(
        task_id=task.id, attachment_id=attachment.id, **values
    )
    statement = statement.on_duplicate_key_update(**values)
    await session.execute(statement)
    await session.flush()
    row = await session.scalar(
        select(ContractParse).where(
            ContractParse.task_id == task.id,
            ContractParse.attachment_id == attachment.id,
        )
    )
    if row is None:  # pragma: no cover - upsert 后必然存在
        raise AppError(ErrorCode.PARSE_FAILED, "解析记录写入失败", task_id=task.id)
    return row


async def _record_parse_failure(
    session: AsyncSession,
    task: ApprovalTask,
    attachment: ApprovalAttachment,
    error: AppError,
) -> None:
    """FR-PARSE-07：失败也必须落库 ``parse_error``，禁止只返回空结果。"""
    await _upsert_parse_row(
        session,
        task=task,
        attachment=attachment,
        values={
            "basic_info_json": [],
            "clause_info_json": [],
            "full_text": None,
            "page_map_json": None,
            "parse_mode": ParseMode.TEXT.value,
            "parse_status": ParseStatus.FAILED.value,
            "parse_error": f"[{error.code.value}] {error.message}",
        },
    )


async def parse_attachment(
    session: AsyncSession,
    task: ApprovalTask,
    attachment: ApprovalAttachment,
    *,
    settings: Settings | None = None,
) -> ParseOutcome:
    """解析一个附件并写库。

    解析失败 → 写 ``parse_status=failed`` + ``parse_error``（FR-PARSE-07），
    并按 §5.4 把任务置 ``blocked``/``parsing``（EMPTY_CONTRACT_CONTENT / OCR_FAILED / PARSE_FAILED）。
    """
    settings = settings or get_settings()
    path = absolute_path_of(attachment)
    if not path.is_file():
        error = AppError(
            ErrorCode.PARSE_FAILED,
            f"附件文件不存在于本地：{attachment.file_name}",
            task_id=task.id,
            detail={"file_path": attachment.file_path},
        )
        await _record_parse_failure(session, task, attachment, error)
        await fail_and_block(
            session,
            task,
            stage=BLOCKED_STAGE_PARSING,
            error_code=ErrorCode.PARSE_FAILED,
            log_type=LogType.PARSE,
            message=f"解析失败：{error.message}",
        )
        raise error

    started = time.perf_counter()
    try:
        extraction: ExtractionResult = extract_document(path, attachment.file_name, settings)
        document: ParsedDocument = build_document(
            extraction.blocks,
            parse_mode=extraction.parse_mode,
            page_count=extraction.page_count,
            used_ocr_pages=extraction.used_ocr_pages,
            warnings=extraction.warnings,
        )
        if not document.full_text.strip():
            raise AppError(
                ErrorCode.EMPTY_CONTRACT_CONTENT,
                f"合同内容为空：{attachment.file_name}",
                task_id=task.id,
            )

        basic, clauses, warnings = extract_fields(document)
        document.warnings.extend(warnings)
        all_records = [*basic, *clauses]
        parse_status = summarize_status(all_records)

        row = await _upsert_parse_row(
            session,
            task=task,
            attachment=attachment,
            values={
                "basic_info_json": [record.to_dict() for record in basic],
                "clause_info_json": [record.to_dict() for record in clauses],
                "full_text": document.full_text,
                "page_map_json": document.page_map_json(),
                "parse_mode": document.parse_mode.value,
                "parse_status": parse_status,
                "parse_error": None,
            },
        )
    except AppError as error:
        # 统一错误体（§5.3）要求带 task_id；底层抽取器不一定知道它，这里补齐
        error.task_id = error.task_id or task.id
        await _record_parse_failure(session, task, attachment, error)
        await fail_and_block(
            session,
            task,
            stage=BLOCKED_STAGE_PARSING,
            error_code=error.code,
            log_type=LogType.OCR if error.code is ErrorCode.OCR_FAILED else LogType.PARSE,
            message=f"解析失败：[{error.code.value}] {error.message}",
        )
        raise
    except Exception as exc:  # noqa: BLE001 - 未预期异常也要落 parse_error（FR-PARSE-07）
        error = AppError(
            ErrorCode.PARSE_FAILED,
            f"解析过程异常：{type(exc).__name__}: {exc}",
            task_id=task.id,
        )
        await _record_parse_failure(session, task, attachment, error)
        await fail_and_block(
            session,
            task,
            stage=BLOCKED_STAGE_PARSING,
            error_code=ErrorCode.PARSE_FAILED,
            log_type=LogType.PARSE,
            message=f"解析失败：{error.message}",
        )
        raise error from exc

    # ── 日志（FR-LOG-01：文档解析 / OCR / 字段提取三类）──────────────────
    elapsed = time.perf_counter() - started
    if document.used_ocr_pages:
        await write_task_log(
            session,
            task.id,
            LogType.OCR,
            f"OCR 识别完成：{len(document.used_ocr_pages)} 页"
            f"（第 {'、'.join(str(p) for p in document.used_ocr_pages)} 页）"
            f"｜共 {len(document.blocks)} 段｜耗时 {format_duration(extraction.ocr_seconds)}",
        )
    await write_task_log(
        session,
        task.id,
        LogType.PARSE,
        f"文档解析完成：{attachment.file_name}｜类型 {extraction.kind.value}｜"
        f"{document.page_count} 页｜parse_mode={document.parse_mode.value}｜"
        f"parse_status={parse_status}｜提取 {len(document.full_text)} 字符｜"
        f"耗时 {format_duration(elapsed)}",
    )
    status_count: dict[str, int] = {}
    for record in all_records:
        status_count[record.extract_status] = status_count.get(record.extract_status, 0) + 1
    failed_fields = [r.field_name for r in all_records if r.extract_status != "success"]
    await write_task_log(
        session,
        task.id,
        LogType.EXTRACT,
        f"字段提取完成：共 {len(all_records)} 个字段"
        f"（success {status_count.get('success', 0)}、missing {status_count.get('missing', 0)}、"
        f"failed {status_count.get('failed', 0)}）"
        + (f"｜非 success：{'、'.join(failed_fields)}" if failed_fields else ""),
        level=LogLevel.WARNING if status_count.get("failed") else LogLevel.INFO,
    )

    # ST-01：解析成功 → parsing → reviewing；
    # 从 blocked 重新解析成功视为人工重试（blocked → reviewing，ST-01-02 会清断点）
    if TaskStatus(task.task_status) in {TaskStatus.PARSING, TaskStatus.BLOCKED}:
        await transition_to(
            session,
            task,
            TaskStatus.REVIEWING,
            log_type=LogType.PARSE,
            log_message=f"解析成功，进入规则审查阶段（document_id={row.id}）",
        )

    return ParseOutcome(
        document_id=row.id,
        task_id=task.id,
        parse_mode=document.parse_mode.value,
        parse_status=parse_status,
        basic_info=[record.to_dict() for record in basic],
        clause_info=[record.to_dict() for record in clauses],
        parse_error=None,
        page_count=document.page_count,
        used_ocr_pages=document.used_ocr_pages,
        warnings=document.warnings,
    )


async def get_parse_row(session: AsyncSession, task_id: int) -> ContractParse | None:
    """按任务取最新解析记录（IF-13）。"""
    return await session.scalar(
        select(ContractParse)
        .where(ContractParse.task_id == task_id)
        .order_by(ContractParse.id.desc())
        .limit(1)
    )


def row_to_outcome(row: ContractParse) -> ParseOutcome:
    """把库里的解析记录还原成 IF-04 的返回结构。"""
    page_map = row.page_map_json or {}
    return ParseOutcome(
        document_id=row.id,
        task_id=row.task_id,
        parse_mode=row.parse_mode,
        parse_status=row.parse_status,
        basic_info=list(row.basic_info_json or []),
        clause_info=list(row.clause_info_json or []),
        parse_error=row.parse_error,
        page_count=int(page_map.get("page_count") or 0),
        used_ocr_pages=list(page_map.get("used_ocr_pages") or []),
    )


def field_status_map(records: list[dict[str, Any]]) -> dict[str, str]:
    """``field_name → extract_status``，便于测试与前端展示。"""
    return {str(item.get("field_name")): str(item.get("extract_status")) for item in records}


__all__ = [
    "ParseOutcome",
    "field_status_map",
    "get_parse_row",
    "parse_attachment",
    "row_to_outcome",
]
