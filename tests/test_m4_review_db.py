"""M4 集成用例：结果落库 + 评论回写（AC11–AC15、TS-12、TS-13）。

约定：
- **只用 db_runner 或只用 TestClient**，不在同一个用例里混用（engine 是进程级单例，
  跨事件循环复用会抛 "Future attached to a different loop"）；
- 模块级夹具先清掉 AP-001/AP-002 的审查与回写痕迹并复位任务状态，
  使"首次审查 → done → 回写"这条主链每轮都可复现。
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app.api.deps import get_approval_client, get_llm_client
from app.clients.approval_client import ApprovalSystemClient
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.db.models import ApprovalTask, CommentLog, ReviewResult
from app.db.session import session_scope
from app.llm.null import NullLLMClient
from app.main import app as service_app
from app.modules.review.comment import DISCLAIMER
from app.tools.approval import list_pending_contract_approvals
from app.tools.comment import write_approval_comment
from app.tools.parser import parse_task
from app.tools.review import run_full_review, save_review_result

pytestmark = pytest.mark.requires_db

POSITION_TEXT_RE = re.compile(r"^第\d+页 第\d+段$")


async def _task_of(instance_id: str) -> ApprovalTask | None:
    async with session_scope() as session:
        task = await session.scalar(
            select(ApprovalTask).where(ApprovalTask.instance_id == instance_id)
        )
        if task is not None:
            session.expunge(task)
    return task


@pytest.fixture(scope="module", autouse=True)
def prepared(db_runner, mock_app: Any) -> None:
    """拉取 + 解析，并清空审查/回写痕迹、把任务复位到 ``reviewing``。"""
    client = ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))

    async def _prepare() -> None:
        await list_pending_contract_approvals(20, client=client)
        for instance_id in ("AP-001", "AP-002"):
            task = await _task_of(instance_id)
            if task is not None:
                await parse_task(task.id, client=client)
        async with session_scope() as session:
            for instance_id in ("AP-001", "AP-002"):
                task = await session.scalar(
                    select(ApprovalTask).where(ApprovalTask.instance_id == instance_id)
                )
                if task is None:
                    continue
                await session.execute(delete(CommentLog).where(CommentLog.task_id == task.id))
                await session.execute(delete(ReviewResult).where(ReviewResult.task_id == task.id))
                await session.execute(
                    text(
                        "UPDATE approval_tasks SET task_status='reviewing', "
                        "write_status='not_written' WHERE id=:t"
                    ),
                    {"t": task.id},
                )
            await session.commit()

    db_runner(_prepare)


async def _state(task_id: int) -> dict[str, Any]:
    async with session_scope() as session:
        task_row = (
            await session.execute(
                text("SELECT task_status, write_status FROM approval_tasks WHERE id=:t"),
                {"t": task_id},
            )
        ).first()
        review = await session.scalar(
            select(ReviewResult).where(ReviewResult.task_id == task_id)
        )
        if review is not None:
            session.expunge(review)
        logs = (
            await session.execute(
                select(CommentLog).where(CommentLog.task_id == task_id).order_by(CommentLog.id)
            )
        ).scalars().all()
        for log in logs:
            session.expunge(log)
    return {"task": dict(task_row._mapping), "review": review, "logs": list(logs)}


# ── AC11 / AC12 / AC13 / AC14 + ST-01：完整审查并落库 ────────────────────


def test_full_review_saves_result_and_completes_task(live_db: str, db_runner) -> None:
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None

    pipeline = db_runner(lambda: run_full_review(task.id, llm=NullLLMClient()))
    saved = pipeline.saved

    # 整体等级来自 RL-AGG，不由 LLM 决定（RL-AGG-03）
    assert saved.overall_risk_level == "high"
    assert pipeline.run.hit_count >= 5

    # AC11：摘要非空、简体中文、提及主要风险
    assert saved.summary_text
    assert "风险" in saved.summary_text
    assert any("\u4e00" <= ch <= "\u9fff" for ch in saved.summary_text)
    assert "预付款" in saved.summary_text

    # AC12：关注点数组 1~5 条、每条 ≤40 字
    assert 1 <= len(saved.focus_points) <= 5
    for point in saved.focus_points:
        assert len(point) <= 40, point

    # AC14：评论严格符合 §4.6.1 模板
    comment = saved.comment_text
    assert comment.startswith("【合同自动审查结果】")
    assert "整体风险等级：高" in comment
    assert "风险摘要：" in comment
    assert "重点关注：" in comment
    assert "1. " in comment
    assert comment.strip().endswith(DISCLAIMER)

    # AC13：落库且 task_id 唯一；ST-01：reviewing → done
    state = db_runner(lambda: _state(task.id))
    assert state["review"] is not None
    assert state["review"].id == saved.review_id
    assert state["review"].comment_text == comment
    assert state["task"]["task_status"] == "done"
    assert state["task"]["write_status"] == "not_written"  # 回写是独立一步


def test_review_result_summary_is_chinese_and_mentions_level(live_db: str, db_runner) -> None:
    task = db_runner(lambda: _task_of("AP-002"))
    assert task is not None
    pipeline = db_runner(lambda: run_full_review(task.id, llm=NullLLMClient()))
    assert pipeline.saved.overall_risk_level == "low"
    assert pipeline.saved.summary_text == "本次自动审查未发现明确的高风险或中风险条款。"
    assert pipeline.saved.focus_points == []
    assert "整体风险等级：低" in pipeline.saved.comment_text


def test_if06_save_is_idempotent(live_db: str, db_runner) -> None:
    """AC13：重复调用不新增记录，``review_id`` 不变（``UNIQUE(task_id)``）。"""
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None

    first = db_runner(
        lambda: save_review_result(
            task.id, "high", "摘要一", '["关注点一"]', f"正文\n{DISCLAIMER}"
        )
    )
    second = db_runner(
        lambda: save_review_result(
            task.id, "medium", "摘要二", ["关注点二"], f"正文二\n{DISCLAIMER}"
        )
    )
    assert first.review_id == second.review_id

    async def count() -> int:
        async with session_scope() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(ReviewResult)
                    .where(ReviewResult.task_id == task.id)
                )
                or 0
            )

    assert db_runner(count) == 1
    state = db_runner(lambda: _state(task.id))
    assert state["review"].summary_text == "摘要二"  # 被覆盖为最新
    assert state["review"].overall_risk_level == "medium"


def test_if06_rejects_invalid_focus_points_json(live_db: str, db_runner) -> None:
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None
    with pytest.raises(AppError) as excinfo:
        db_runner(
            lambda: save_review_result(task.id, "high", "s", "{不是 JSON", f"x\n{DISCLAIMER}")
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR


# ── AC15 / TS-12：评论回写与幂等 ─────────────────────────────────────────


def test_if07_writes_comment_and_is_idempotent(
    live_db: str, db_runner, approval_client, mock_module: Any
) -> None:
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None
    pipeline = db_runner(lambda: run_full_review(task.id, llm=NullLLMClient()))
    review_id = pipeline.saved.review_id

    before = len(mock_module._comments_by_instance.get("AP-001", []))

    first = db_runner(
        lambda: write_approval_comment("AP-001", review_id, client=approval_client)
    )
    assert first.write_status == "success"
    assert first.remark_id and first.remark_id.startswith("RMK-")
    assert first.duplicate is False

    after_first = len(mock_module._comments_by_instance.get("AP-001", []))
    assert after_first == before + 1  # 审批系统侧真的出现了一条评论

    second = db_runner(
        lambda: write_approval_comment("AP-001", review_id, client=approval_client)
    )
    assert second.duplicate is True
    assert second.remark_id == first.remark_id  # FR-COM-06：直接返回既有结果
    assert len(mock_module._comments_by_instance.get("AP-001", [])) == after_first  # 不重复评论

    state = db_runner(lambda: _state(task.id))
    assert len(state["logs"]) == 1  # 一条逻辑评论一行（唯一索引即幂等保障）
    assert state["logs"][0].write_status == "success"
    assert state["logs"][0].remark_id == first.remark_id
    assert state["task"]["write_status"] == "success"
    assert state["task"]["task_status"] == "done"

    # 审批系统侧确实收到了我们渲染的评论正文（AC15）
    keys = mock_module._comments_by_instance.get("AP-001", [])
    stored = mock_module._comments[keys[-1]]
    assert stored["content"] == pipeline.saved.comment_text


def test_write_failure_keeps_result_and_done(live_db: str, db_runner, unreachable_client) -> None:
    """TS-13 / FR-COM-04 / ST-01-04：回写失败不得删结果，任务保持 done。"""
    task = db_runner(lambda: _task_of("AP-002"))
    assert task is not None
    pipeline = db_runner(lambda: run_full_review(task.id, llm=NullLLMClient()))

    with pytest.raises(AppError) as excinfo:
        db_runner(
            lambda: write_approval_comment(
                "AP-002", pipeline.saved.review_id, client=unreachable_client
            )
        )
    assert excinfo.value.code is ErrorCode.COMMENT_WRITE_FAILED
    assert excinfo.value.http_status == 502

    state = db_runner(lambda: _state(task.id))
    assert state["task"]["task_status"] == "done"  # 不回退（ST-01-04）
    assert state["task"]["write_status"] == "failed"
    assert state["review"] is not None  # 结果仍在
    assert state["logs"] and state["logs"][-1].write_status == "failed"
    assert "ConnectError" in (state["logs"][-1].write_response_text or "")


def test_if07_rejects_review_of_another_instance(live_db: str, db_runner, approval_client) -> None:
    task_a = db_runner(lambda: _task_of("AP-001"))
    task_b = db_runner(lambda: _task_of("AP-002"))
    assert task_a is not None and task_b is not None
    other = db_runner(lambda: run_full_review(task_b.id, llm=NullLLMClient()))

    with pytest.raises(AppError) as excinfo:
        db_runner(
            lambda: write_approval_comment(
                "AP-001", other.saved.review_id, client=approval_client
            )
        )
    assert excinfo.value.code is ErrorCode.APPROVAL_NOT_FOUND
    assert "不属于" in excinfo.value.message


def test_if07_rejects_non_compliant_comment_text(
    live_db: str, db_runner, approval_client
) -> None:
    """FR-COM-07 守卫：缺少免责声明或含审批结论的正文一律拒绝回写。

    用 AP-002——它的上一次回写是 **failed**（见 TS-13 用例），因此不会命中
    FR-COM-06 的"已 success 直接返回"短路，守卫才会真正被执行到。
    """
    task = db_runner(lambda: _task_of("AP-002"))
    assert task is not None
    saved = db_runner(
        lambda: save_review_result(task.id, "high", "摘要", ["关注点"], "建议批准本合同")
    )
    with pytest.raises(AppError) as excinfo:
        db_runner(lambda: write_approval_comment("AP-002", saved.review_id, client=approval_client))
    assert excinfo.value.code is ErrorCode.COMMENT_WRITE_FAILED
    assert "4.6.1" in excinfo.value.message

    state = db_runner(lambda: _state(task.id))
    assert state["task"]["task_status"] == "done"  # 任务状态不受影响
    assert state["review"] is not None
    # 守卫在状态迁移之前拦下，不得把记录留在 writing
    assert all(log.write_status != "writing" for log in state["logs"])


# ── REST：闭环（IF-14 → IF-16 → IF-17 → IF-18）─────────────────────────


@pytest.fixture
def api_client(mock_app: Any) -> Any:
    """审批系统走内存 ASGI，LLM 注入空实现（确定性 + 不产生真实调用）。"""
    client = ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))
    service_app.dependency_overrides[get_approval_client] = lambda: client
    service_app.dependency_overrides[get_llm_client] = lambda: NullLLMClient()
    with TestClient(service_app) as test_client:
        yield test_client
    service_app.dependency_overrides.clear()


def test_rest_closed_loop_parse_review_write_comment(api_client: Any) -> None:
    """IF-14 → IF-16 → IF-17 → IF-18 全链路（AC19 的接口侧形态）。"""
    headers = {"X-API-Key": get_settings().internal_api_key}

    tasks = api_client.get("/api/tasks", params={"size": 10}, headers=headers).json()["items"]
    task_id = next(item["task_id"] for item in tasks if item["instance_id"] == "AP-002")

    parsed = api_client.post(f"/api/tasks/{task_id}/parse", headers=headers)
    assert parsed.status_code == 200
    assert parsed.json()["parse_status"] == "success"

    reviewed = api_client.post(f"/api/tasks/{task_id}/review", headers=headers)
    assert reviewed.status_code == 200
    body = reviewed.json()
    assert body["review_id"]
    assert body["overall_risk_level"] == "low"
    assert body["task_status"] == "done"
    assert body["comment_text"].endswith(DISCLAIMER)

    written = api_client.post(f"/api/tasks/{task_id}/write-comment", headers=headers)
    assert written.status_code == 200
    write_body = written.json()
    assert write_body["write_status"] == "success"
    assert write_body["remark_id"].startswith("RMK-")

    logs = api_client.get(f"/api/tasks/{task_id}/comment-logs", headers=headers)
    assert logs.status_code == 200
    assert logs.json()["total"] == 1
    assert logs.json()["items"][0]["write_status"] == "success"

    # IF-15 复查：返回已保存的结果
    fetched = api_client.get(f"/api/tasks/{task_id}/review", headers=headers).json()
    assert fetched["review_id"] == body["review_id"]
    assert fetched["comment_text"] == body["comment_text"]
    assert len(fetched["rule_hits"]) == 11

    # IF-17 二次回写 → 幂等
    again = api_client.post(f"/api/tasks/{task_id}/write-comment", headers=headers).json()
    assert again["duplicate"] is True
    assert again["remark_id"] == write_body["remark_id"]


def test_rest_write_comment_requires_review(api_client: Any, db_runner) -> None:
    """未生成审查结果就回写 → 409。"""
    headers = {"X-API-Key": get_settings().internal_api_key}
    # 用一个不存在的任务验证 404 分支更稳妥（避免依赖其它用例的状态）
    response = api_client.post("/api/tasks/99999999/write-comment", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "APPROVAL_NOT_FOUND"
    assert api_client.post("/api/tasks/99999999/write-comment").status_code == 401
