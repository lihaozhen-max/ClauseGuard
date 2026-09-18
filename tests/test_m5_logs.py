"""M5 全链路日志用例（AC18、FR-LOG-01…FR-LOG-05）。

AC18 要求 8 类核心操作各有日志。**文本型合同没有 OCR 环节**，因此要在一份任务上同时看到
``ocr`` 与其余 7 类，必须用扫描件（AP-003）跑完整链路——该用例标 ``slow``（真实 OCR 推理）。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import get_approval_client, get_llm_client
from app.clients.approval_client import ApprovalSystemClient
from app.core.config import get_settings
from app.core.enums import CORE_LOG_TYPES
from app.core.textutil import DEFAULT_LOG_TEXT_LIMIT
from app.db.models import ApprovalTask, TaskLog
from app.db.session import session_scope
from app.llm.null import NullLLMClient
from app.main import app as service_app
from app.modules.logging.service import collect_log_types, list_task_logs
from app.tools.approval import list_pending_contract_approvals
from app.tools.comment import write_approval_comment
from app.tools.parser import parse_task
from app.tools.review import run_full_review

pytestmark = pytest.mark.requires_db

CORE_TYPE_VALUES = {str(item) for item in CORE_LOG_TYPES}


@pytest.fixture(autouse=True, scope="module")
def pulled(db_runner, mock_app: Any) -> None:
    """确保样例审批单已拉取为任务（用例自足；模块级夹具自建客户端，避免 ScopeMismatch）。"""
    client = ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))
    db_runner(lambda: list_pending_contract_approvals(20, client=client))


async def _task_of(instance_id: str) -> ApprovalTask | None:
    async with session_scope() as session:
        task = await session.scalar(
            select(ApprovalTask).where(ApprovalTask.instance_id == instance_id)
        )
        if task is not None:
            session.expunge(task)
    return task


async def _logs(task_id: int) -> list[TaskLog]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.id)
            )
        ).scalars().all()
        for row in rows:
            session.expunge(row)
    return list(rows)


# ── AC18：8 类核心操作均有日志 ───────────────────────────────────────────


@pytest.mark.slow
def test_scanned_document_loop_covers_all_eight_log_types(
    live_db: str, db_runner, approval_client
) -> None:
    """AC18：扫描件走**完整九步闭环** → 8 类 ``log_type`` 齐备（含 ``ocr`` 与 ``write_comment``）。"""
    task = db_runner(lambda: _task_of("AP-003"))
    assert task is not None

    db_runner(lambda: parse_task(task.id, client=approval_client))
    pipeline = db_runner(lambda: run_full_review(task.id, llm=NullLLMClient()))
    # 第 ⑨ 步：评论回写也要留痕，否则 8 类里会缺 write_comment
    db_runner(
        lambda: write_approval_comment(
            task.instance_id, pipeline.saved.review_id, client=approval_client
        )
    )

    async def kinds() -> set[str]:
        async with session_scope() as session:
            return await collect_log_types(session, task.id)

    found = db_runner(kinds)
    missing = CORE_TYPE_VALUES - found
    assert not missing, f"缺少日志类型：{sorted(missing)}（实际 {sorted(found)}）"
    assert "ocr" in found  # 扫描件的 OCR 环节


def test_text_document_loop_has_seven_types_without_ocr(
    live_db: str, db_runner, approval_client
) -> None:
    """文本型合同没有 OCR 环节 → 应为 7 类（这是预期语义，不是缺陷）。"""
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None
    db_runner(lambda: parse_task(task.id, client=approval_client))
    db_runner(lambda: run_full_review(task.id, llm=NullLLMClient()))

    async def kinds() -> set[str]:
        async with session_scope() as session:
            return await collect_log_types(session, task.id)

    found = db_runner(kinds)
    assert CORE_TYPE_VALUES - {"ocr"} <= found
    assert "ocr" not in found


# ── 日志质量约束（FR-LOG-02/03/04）──────────────────────────────────────


def test_every_log_row_has_required_fields(live_db: str, db_runner) -> None:
    """FR-LOG-02：每条日志必须含 task_id / log_level / log_type / log_content / created_at。"""
    task = db_runner(lambda: _task_of("AP-002"))
    assert task is not None
    rows = db_runner(lambda: _logs(task.id))
    assert rows
    for row in rows:
        assert row.task_id == task.id
        assert row.log_level in {"info", "warning", "error"}
        assert row.log_type
        assert row.log_content
        assert row.created_at is not None


def test_log_content_is_truncated_and_redacted(live_db: str, db_runner) -> None:
    """FR-LOG-04 截断 ≤500 字符；FR-LOG-03 不得出现任何密钥。"""
    settings = get_settings()
    task = db_runner(lambda: _task_of("AP-002"))
    assert task is not None
    rows = db_runner(lambda: _logs(task.id))
    assert rows
    for row in rows:
        assert len(row.log_content) <= DEFAULT_LOG_TEXT_LIMIT
        for secret in settings.secrets_to_redact:
            assert secret not in row.log_content


def test_log_query_supports_filters_and_pagination(live_db: str, db_runner) -> None:
    task = db_runner(lambda: _task_of("AP-002"))
    assert task is not None

    async def query() -> tuple[int, int, int]:
        async with session_scope() as session:
            all_rows, total = await list_task_logs(session, task.id, page=1, size=200)
            page_rows, _ = await list_task_logs(session, task.id, page=2, size=1)
            return len(all_rows), total, len(page_rows)

    count, total, page_size = db_runner(query)
    assert count == total  # size 足够大时应取回全部
    assert page_size == 1  # 分页生效


# ── REST：IF-19 ─────────────────────────────────────────────────────────


@pytest.fixture
def api_client(mock_app: Any) -> Any:
    client = ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))
    service_app.dependency_overrides[get_approval_client] = lambda: client
    service_app.dependency_overrides[get_llm_client] = lambda: NullLLMClient()
    with TestClient(service_app) as test_client:
        yield test_client
    service_app.dependency_overrides.clear()


def test_rest_logs_endpoint(api_client: Any) -> None:
    headers = {"X-API-Key": get_settings().internal_api_key}
    tasks = api_client.get("/api/tasks", params={"size": 10}, headers=headers).json()["items"]
    ap001 = next(item for item in tasks if item["instance_id"] == "AP-001")

    payload = api_client.get(f"/api/tasks/{ap001['task_id']}/logs", headers=headers).json()
    assert payload["total"] >= 1
    assert "pull" in payload["log_types"]
    assert payload["page"] == 1 and payload["size"] == 50

    # 非法 log_type / level 由枚举校验挡下 → 422 统一错误结构
    bad = api_client.get(
        f"/api/tasks/{ap001['task_id']}/logs", params={"log_type": "nope"}, headers=headers
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"

    missing = api_client.get("/api/tasks/99999999/logs", headers=headers)
    assert missing.status_code == 404
