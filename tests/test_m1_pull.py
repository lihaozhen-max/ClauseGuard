"""M1 待办拉取与去重用例（AC01 / AC02 / TS-01）。

用 ASGI 内存传输把"工具服务 → 模拟审批系统"的 HTTP 调用接到真实 Mock 应用上，
再落真实 MySQL，因此这是一条**真集成**链路，而不是打桩。

非破坏性约定：用例不清理历史数据，而是断言"两次拉取之间的关系"
（第二次 created_count=0、task_id 不变、库中同 instance 仅 1 行），
这样无论开发库此前是否已有数据都成立。需要验证 `created` 分支时，
临时向 Mock 注入一个专用审批单，并在用例结束时删除其数据。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app.api.deps import get_approval_client
from app.core.config import get_settings
from app.core.enums import TaskStatus
from app.core.errors import AppError, ErrorCode
from app.db.models import ApprovalTask, TaskLog
from app.db.session import session_scope
from app.main import app as service_app
from app.schemas.approval import PullResult
from app.tools.approval import get_contract_approval, list_pending_contract_approvals

pytestmark = pytest.mark.requires_db

#: 临时注入的测试审批单（保证 created 分支可确定性验证）
TEMP_APPROVAL: dict[str, Any] = {
    "instance_id": "AP-TEST-0001",
    "approval_code": "CG-TEST-0001",
    "approval_title": "临时测试审批单",
    "applicant_name": "测试机器人",
    "apply_time": "2026-09-01T00:00:00Z",
    "current_status": "pending",
    "contract_type": "采购合同",
    "form_data": {"amount": "1", "currency": "CNY"},
    "attachments": [
        {
            "attachment_id": "ATT-TEST",
            "file_name": "AP-001_采购合同.pdf",
            "file_type": "pdf",
            "file_size": 0,
        }
    ],
    "expected": {},
}


@pytest.fixture
def temp_approval(mock_module: Any, db_runner):
    """向 Mock 临时插入审批单，并在结束时连同库中数据一并清掉。"""
    mock_module.APPROVALS.append(TEMP_APPROVAL)
    mock_module.APPROVALS_BY_ID[TEMP_APPROVAL["instance_id"]] = TEMP_APPROVAL
    yield TEMP_APPROVAL
    mock_module.APPROVALS.remove(TEMP_APPROVAL)
    mock_module.APPROVALS_BY_ID.pop(TEMP_APPROVAL["instance_id"], None)

    async def _cleanup() -> None:
        async with session_scope() as session:
            task_id = (
                await session.execute(
                    select(ApprovalTask.id).where(
                        ApprovalTask.instance_id == TEMP_APPROVAL["instance_id"]
                    )
                )
            ).scalar_one_or_none()
            if task_id is not None:
                await session.execute(delete(TaskLog).where(TaskLog.task_id == task_id))
                await session.execute(delete(ApprovalTask).where(ApprovalTask.id == task_id))
            await session.commit()

    db_runner(_cleanup)


# ── AC01：能够拉取待处理审批单 ─────────────────────────────────────────────


def test_pull_returns_pending_approvals_with_required_fields(
    live_db: str, db_runner, approval_client
) -> None:
    async def scenario() -> PullResult:
        return await list_pending_contract_approvals(20, client=approval_client)

    result = db_runner(scenario)

    assert len(result.items) == 5
    assert result.created_count + result.updated_count == 5
    assert {item.instance_id for item in result.items} == {
        "AP-001",
        "AP-002",
        "AP-003",
        "AP-004",
        "AP-005",
    }
    first = result.items[0]
    # FR-APP-01 要求的字段 + IF-01 的任务字段与去重标记
    assert first.instance_id == "AP-001"
    assert first.approval_code == "CG-2026-0001"
    assert first.approval_title == "服务器采购合同审批"
    assert first.applicant_name == "张三"
    assert first.apply_time is not None and first.apply_time.year == 2026
    assert first.attachment_count == 1
    assert first.current_status == "pending"
    assert first.task_id > 0
    # 状态必须是 §2.2 的合法枚举值；"新建任务必为 pending" 由
    # test_created_branch_is_reachable 用临时新实例严格验证（避免依赖测试执行顺序）
    assert first.task_status in {s.value for s in TaskStatus}
    assert first.dedup in {"created", "updated"}


# ── AC02：相同审批单重复拉取不会生成重复任务 ───────────────────────────────


def test_second_pull_updates_instead_of_creating(live_db: str, db_runner, approval_client) -> None:
    """AC02 判据：第二次 created_count=0、updated_count=1、task_id 不变、库中仅 1 条。"""

    async def scenario() -> tuple[PullResult, PullResult, int]:
        first = await list_pending_contract_approvals(1, client=approval_client)
        second = await list_pending_contract_approvals(1, client=approval_client)
        async with session_scope() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ApprovalTask)
                .where(ApprovalTask.instance_id == first.items[0].instance_id)
            )
        return first, second, int(count or 0)

    first, second, rows = db_runner(scenario)

    assert second.created_count == 0
    assert second.updated_count == 1
    assert second.items[0].task_id == first.items[0].task_id  # task_id 稳定
    assert second.items[0].dedup == "updated"
    assert rows == 1  # 库里只有一条任务


def test_created_branch_is_reachable(live_db: str, db_runner, approval_client, temp_approval) -> None:
    """新实例首次拉取必须判为 created，且任务状态为 pending（ST-01 初始迁移）。"""

    async def scenario() -> tuple[PullResult, PullResult]:
        first = await list_pending_contract_approvals(20, client=approval_client)
        second = await list_pending_contract_approvals(20, client=approval_client)
        return first, second

    first, second = db_runner(scenario)
    by_id = {item.instance_id: item for item in first.items}
    assert by_id["AP-TEST-0001"].dedup == "created"
    assert by_id["AP-TEST-0001"].task_status == "pending"

    second_by_id = {item.instance_id: item for item in second.items}
    assert second_by_id["AP-TEST-0001"].dedup == "updated"
    assert second_by_id["AP-TEST-0001"].task_id == by_id["AP-TEST-0001"].task_id
    assert second.created_count == 0


def test_concurrent_double_pull_creates_single_task(
    live_db: str, db_runner, approval_client, temp_approval
) -> None:
    """TS-01 并发双拉：唯一索引是最终保障（FR-APP-03），不得产生第二条任务。"""

    async def scenario() -> int:
        await asyncio.gather(
            list_pending_contract_approvals(20, client=approval_client),
            list_pending_contract_approvals(20, client=approval_client),
        )
        async with session_scope() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(ApprovalTask)
                    .where(ApprovalTask.instance_id == "AP-TEST-0001")
                )
                or 0
            )

    assert db_runner(scenario) == 1


# ── FR-APP-05：重复拉取禁止把 done 的任务拉回 pending ───────────────────────


def test_repull_does_not_reset_task_status(live_db: str, db_runner, approval_client) -> None:
    async def scenario() -> str:
        first = await list_pending_contract_approvals(1, client=approval_client)
        task_id = first.items[0].task_id
        async with session_scope() as session:
            await session.execute(
                text("UPDATE approval_tasks SET task_status='done' WHERE id=:tid"), {"tid": task_id}
            )
            await session.commit()

        await list_pending_contract_approvals(1, client=approval_client)

        async with session_scope() as session:
            status = await session.scalar(
                text("SELECT task_status FROM approval_tasks WHERE id=:tid"), {"tid": task_id}
            )
        # 恢复现场，避免影响其它用例
        async with session_scope() as session:
            await session.execute(
                text("UPDATE approval_tasks SET task_status='pending' WHERE id=:tid"),
                {"tid": task_id},
            )
            await session.commit()
        return str(status)

    assert db_runner(scenario) == "done"  # FR-APP-05 / ST-01-03


# ── FR-LOG-01：待办获取必须落 task_logs ────────────────────────────────────


def test_pull_writes_task_log(live_db: str, db_runner, approval_client) -> None:
    settings = get_settings()

    async def scenario() -> list[tuple[str, str, str]]:
        result = await list_pending_contract_approvals(2, client=approval_client)
        task_ids = [item.task_id for item in result.items]
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(TaskLog.log_type, TaskLog.log_level, TaskLog.log_content).where(
                        TaskLog.task_id.in_(task_ids), TaskLog.log_type == "pull"
                    )
                )
            ).all()
        return [tuple(row) for row in rows]

    rows = db_runner(scenario)
    assert rows, "待办拉取必须写 log_type=pull 的日志"
    for log_type, level, content in rows:
        assert log_type == "pull"
        assert level in {"info", "warning", "error"}
        assert "AP-00" in content
        for secret in settings.secrets_to_redact:  # FR-LOG-03
            assert secret not in content


# ── IF-02：审批详情（FR-APP-04）───────────────────────────────────────────


def test_get_contract_approval_returns_detail(live_db: str, db_runner, approval_client) -> None:
    async def scenario():
        return await get_contract_approval("AP-001", client=approval_client)

    detail = db_runner(scenario)
    assert detail.instance_id == "AP-001"
    assert detail.contract_type == "采购合同"
    assert detail.form_data["amount"] == "500000"
    assert detail.attachments[0].attachment_id == "ATT-001"
    assert detail.attachments[0].file_name == "AP-001_采购合同.pdf"
    assert detail.task_id is not None  # 已拉取过 → 能映射到本系统任务
    assert detail.task_status == "pending"


def test_get_contract_approval_unknown_instance_raises(
    live_db: str, db_runner, approval_client
) -> None:
    async def scenario():
        return await get_contract_approval("AP-999", client=approval_client)

    with pytest.raises(AppError) as excinfo:
        db_runner(scenario)
    assert excinfo.value.code is ErrorCode.APPROVAL_NOT_FOUND
    assert excinfo.value.http_status == 404


# ── FR-APP-06：审批接口调用失败 → APPROVAL_API_ERROR 且记录请求信息 ─────────


def test_approval_api_error_is_mapped(live_db: str, db_runner, unreachable_client) -> None:
    async def scenario():
        return await list_pending_contract_approvals(5, client=unreachable_client)

    with pytest.raises(AppError) as excinfo:
        db_runner(scenario)
    error = excinfo.value
    assert error.code is ErrorCode.APPROVAL_API_ERROR
    assert error.http_status == 502
    # 必须记录请求信息与错误信息（PRD 13）
    assert error.detail["method"] == "GET"
    assert "/approvals/pending" in error.detail["url"]
    assert "ConnectError" in error.detail["error"]


# ── 内部 REST：IF-10 / IF-11 / IF-12 ──────────────────────────────────────


@pytest.fixture
def api_client(approval_client) -> Any:
    """带鉴权的 TestClient，并把审批系统客户端替换为内存版。"""
    service_app.dependency_overrides[get_approval_client] = lambda: approval_client
    with TestClient(service_app) as client:
        yield client
    service_app.dependency_overrides.clear()


def test_api_pull_list_and_detail(api_client: Any) -> None:
    headers = {"X-API-Key": get_settings().internal_api_key}

    pulled = api_client.post("/api/tasks/pull", json={"limit": 5}, headers=headers)
    assert pulled.status_code == 200
    body = pulled.json()
    assert len(body["items"]) == 5
    assert body["created_count"] + body["updated_count"] == 5

    listed = api_client.get("/api/tasks", params={"size": 5}, headers=headers)
    assert listed.status_code == 200
    listing = listed.json()
    assert listing["total"] >= 5
    assert {"task_id", "instance_id", "approval_code", "task_status"} <= set(listing["items"][0])

    task_id = body["items"][0]["task_id"]
    detail = api_client.get(f"/api/tasks/{task_id}", headers=headers).json()
    assert detail["task_id"] == task_id
    assert detail["task_status"] == "pending"
    assert detail["write_status"] == "not_written"
    assert isinstance(detail["attachments"], list)
    assert detail["created_at"].endswith("Z")  # §2.3 接口时间统一 UTC


def test_api_requires_key_and_returns_unified_error(api_client: Any) -> None:
    assert api_client.post("/api/tasks/pull", json={"limit": 1}).status_code == 401
    body = api_client.post("/api/tasks/pull", json={"limit": 1}).json()
    assert body["error"]["code"] == "UNAUTHORIZED"

    headers = {"X-API-Key": get_settings().internal_api_key}
    missing = api_client.get("/api/tasks/99999999", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "APPROVAL_NOT_FOUND"


def test_api_rejects_invalid_limit(api_client: Any) -> None:
    """Pydantic 校验失败也必须走 §5.3 统一结构（NF-09）。"""
    headers = {"X-API-Key": get_settings().internal_api_key}
    response = api_client.post("/api/tasks/pull", json={"limit": 0}, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
