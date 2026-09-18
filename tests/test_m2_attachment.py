"""M2 附件下载用例（AC03、TS-02、TS-19）。

覆盖：真实下载落盘 + SHA-256 + DT-02 记录、附件不存在 → blocked、下载失败 →
blocked、重复下载不新增记录、路径穿越消毒。
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.core.config import PROJECT_ROOT, get_settings
from app.core.enums import DownloadStatus, TaskStatus
from app.core.errors import AppError, ErrorCode
from app.db.models import ApprovalAttachment, ApprovalTask
from app.db.session import session_scope
from app.modules.attachment.service import (
    absolute_path_of,
    guess_file_type,
    sanitize_filename,
)
from app.tools.approval import list_pending_contract_approvals
from app.tools.attachment import download_contract_attachment

pytestmark = pytest.mark.requires_db


async def _task_of(instance_id: str) -> ApprovalTask | None:
    async with session_scope() as session:
        task = await session.scalar(
            select(ApprovalTask).where(ApprovalTask.instance_id == instance_id)
        )
        if task is not None:
            session.expunge(task)
    return task


async def _attachment_count(task_id: int, attachment_code: str) -> int:
    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ApprovalAttachment)
                .where(
                    ApprovalAttachment.task_id == task_id,
                    ApprovalAttachment.attachment_code == attachment_code,
                )
            )
            or 0
        )


async def _task_state(task_id: int) -> dict[str, Any]:
    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    "SELECT task_status, blocked_stage, error_code FROM approval_tasks WHERE id=:t"
                ),
                {"t": task_id},
            )
        ).first()
    return dict(row._mapping)


@pytest.fixture(scope="module", autouse=True)
def _pulled(db_runner, mock_app):
    """确保 AP-001…AP-005 都已拉取为任务（下载的前置条件）。"""
    import httpx

    from app.clients.approval_client import ApprovalSystemClient

    client = ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))
    db_runner(lambda: list_pending_contract_approvals(20, client=client))


# ── TS-19：路径穿越消毒 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
        ("C:\\Windows\\notepad.exe", "notepad.exe"),
        ("/etc/shadow", "shadow"),
        ("a/b/../../x.pdf", "x.pdf"),
        ("..", "attachment"),
        (".", "attachment"),
        ("", "attachment"),
        ("   ", "attachment"),
        ("合同（扫描件）.pdf", "合同（扫描件）.pdf"),
        ("wo:rk?in*val|id.pdf", "workinvalid.pdf"),
        ("con.pdf", "_con.pdf"),
        ("nul.docx", "_nul.docx"),
    ],
)
def test_sanitize_filename_blocks_traversal(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_limits_length() -> None:
    long_name = "甲" * 200 + ".pdf"
    result = sanitize_filename(long_name)
    assert len(result) <= 100
    assert result.endswith(".pdf")


def test_guess_file_type() -> None:
    assert guess_file_type("a.PDF") == "pdf"
    assert guess_file_type("b.docx") == "docx"
    assert guess_file_type("c.jpeg") == "jpeg"
    assert guess_file_type("noext") == "unknown"


# ── AC03：能够下载合同附件并保存附件记录 ──────────────────────────────────


def test_download_persists_file_and_record(live_db: str, db_runner, approval_client) -> None:
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None

    result = db_runner(
        lambda: download_contract_attachment(
            "AP-001", "ATT-001", "AP-001_采购合同.pdf", client=approval_client
        )
    )

    # 返回值（IF-03 结构）
    assert result.task_id == task.id
    assert result.attachment_id == "ATT-001"
    assert result.file_type == "pdf"
    assert result.file_size > 1000
    assert len(result.file_checksum) == 64
    assert result.download_status == DownloadStatus.SUCCESS.value

    # 文件真的落盘，且校验值与内容一致
    path = PROJECT_ROOT / result.file_path
    assert path.is_file(), f"附件未落盘：{path}"
    assert path.stat().st_size == result.file_size
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result.file_checksum

    # 落盘目录按 task_id 隔离（FR-ATT-03）
    assert path.parent.name == str(task.id)
    assert path.name == "ATT-001_AP-001_采购合同.pdf"

    # DT-02 有记录（AC03 判据）
    async def fetch():
        async with session_scope() as session:
            row = await session.scalar(
                select(ApprovalAttachment).where(
                    ApprovalAttachment.task_id == task.id,
                    ApprovalAttachment.attachment_code == "ATT-001",
                )
            )
            session.expunge(row)
            return row

    row = db_runner(fetch)
    assert row is not None
    assert row.download_status == DownloadStatus.SUCCESS.value
    assert row.file_checksum == result.file_checksum
    assert row.file_size == result.file_size
    assert absolute_path_of(row) == path

    # ST-01：pending → parsing
    assert db_runner(lambda: _task_state(task.id))["task_status"] == TaskStatus.PARSING.value


def test_repeated_download_updates_single_record(live_db: str, db_runner, approval_client) -> None:
    """FR-ATT-06：重复下载覆盖更新，不产生第二条附件记录。"""
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None

    db_runner(
        lambda: download_contract_attachment(
            "AP-001", "ATT-001", "AP-001_采购合同.pdf", client=approval_client
        )
    )
    assert db_runner(lambda: _attachment_count(task.id, "ATT-001")) == 1


# ── 附件不存在 → blocked（FR-ATT-04，AC16 的输入侧）───────────────────────


def test_missing_attachment_blocks_task(live_db: str, db_runner, approval_client) -> None:
    task = db_runner(lambda: _task_of("AP-004"))
    assert task is not None

    with pytest.raises(AppError) as excinfo:
        db_runner(
            lambda: download_contract_attachment(
                "AP-004", "ATT-004", "AP-004_办公用品采购合同.pdf", client=approval_client
            )
        )
    assert excinfo.value.code is ErrorCode.CONTRACT_ATTACHMENT_MISSING
    assert excinfo.value.http_status == 404

    state = db_runner(lambda: _task_state(task.id))
    assert state["task_status"] == TaskStatus.BLOCKED.value
    assert state["blocked_stage"] == "parsing"
    assert state["error_code"] == ErrorCode.CONTRACT_ATTACHMENT_MISSING.value


# ── 下载失败 → blocked（FR-ATT-05）───────────────────────────────────────


def test_download_failure_blocks_task(live_db: str, db_runner, unreachable_client) -> None:
    task = db_runner(lambda: _task_of("AP-002"))
    assert task is not None
    state_before = db_runner(lambda: _task_state(task.id))

    with pytest.raises(AppError) as excinfo:
        db_runner(
            lambda: download_contract_attachment(
                "AP-002", "ATT-002", "AP-002_服务合同.pdf", client=unreachable_client
            )
        )
    assert excinfo.value.code is ErrorCode.DOWNLOAD_FAILED
    assert excinfo.value.http_status == 502
    assert "ConnectError" in str(excinfo.value.detail)

    state = db_runner(lambda: _task_state(task.id))
    if state_before["task_status"] == TaskStatus.DONE.value:
        assert state["task_status"] == TaskStatus.DONE.value  # ST-01-03：done 不回退
    else:
        assert state["task_status"] == TaskStatus.BLOCKED.value
        assert state["error_code"] == ErrorCode.DOWNLOAD_FAILED.value


def test_download_requires_existing_task(live_db: str, db_runner, approval_client) -> None:
    with pytest.raises(AppError) as excinfo:
        db_runner(
            lambda: download_contract_attachment("AP-999", "ATT-001", None, client=approval_client)
        )
    assert excinfo.value.code is ErrorCode.APPROVAL_NOT_FOUND


# ── AP-005：0 字节文件下载成功，交给解析阶段报错 ────────────────────────────


def test_zero_byte_attachment_downloads_successfully(
    live_db: str, db_runner, approval_client
) -> None:
    result = db_runner(
        lambda: download_contract_attachment(
            "AP-005", "ATT-005", "AP-005_空文件.pdf", client=approval_client
        )
    )
    assert result.file_size == 0
    assert result.download_status == DownloadStatus.SUCCESS.value
    assert (PROJECT_ROOT / result.file_path).is_file()


# ── 存储目录不对外暴露（FR-SYS-02）─────────────────────────────────────────


def test_attachment_not_served_by_any_static_route() -> None:
    """附件只落盘、不挂静态路由：应用路由表里不应出现 storage 前缀。"""
    from app.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any("storage" in path for path in paths)
    settings = get_settings()
    assert settings.storage_path.is_absolute()
