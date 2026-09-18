"""并发写冲突用例（加固轮；对应 SPEC NF-03 幂等、FR-APP-03 唯一索引、ST-01/ST-02）。

与其它用例的区别：这里的每个"调用方"都开**自己的 session**，用 ``asyncio.gather`` 真并发，
验证的是"多个人/多个进程同时点同一个按钮"时的收敛性，而不是单线程下的正确性。

要验证的四类不变量：

1. **唯一索引是最终保障**（FR-APP-03 / DT-00-05）：并发不能产生第二行；
2. **并发冲突必须表现为可预期的业务结果**（去重 / 422），而不是 ``IntegrityError``（500）；
3. **不丢更新**：并发改同一行的**不同字段**时，两边的改动都要留下；
4. **收敛**：并发操作结束后任务状态必须是确定的一个终态。

M1 曾在这条路上踩过 1213 死锁（见 ``modules/approval/service.py`` 的注释），
所以并发拉取的用例在这里一并保留为回归。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.clients.approval_client import ApprovalSystemClient
from app.core.enums import WriteStatus
from app.core.errors import AppError, ErrorCode
from app.db.models import ApprovalTask, CommentLog, ReviewResult, ReviewRule, RuleHit
from app.db.session import session_scope
from app.llm.null import NullLLMClient
from app.modules.comment.service import build_idempotency_key, write_approval_comment
from app.modules.rules.admin import create_rule, update_rule
from app.schemas.rule import RuleCreateRequest, RuleUpdateRequest
from app.tools.approval import list_pending_contract_approvals
from app.tools.parser import parse_task
from app.tools.review import run_full_review

pytestmark = pytest.mark.requires_db

#: 并发度：6 路足以让"先检查后写入"的窗口稳定重叠
CONCURRENCY = 6


def gather(
    db_runner: Any,
    factory: Callable[[int], Coroutine[Any, Any, Any]],
    n: int = CONCURRENCY,
) -> list[Any]:
    """在同一个事件循环里并发跑 n 个协程，返回结果与异常（不抛出）。"""

    async def scenario() -> list[Any]:
        return await asyncio.gather(*(factory(i) for i in range(n)), return_exceptions=True)

    return db_runner(scenario)


def fatal(results: list[Any]) -> list[BaseException]:
    """挑出"不该出现"的异常：数据库层异常与未预期异常（``AppError`` 是允许的收敛方式）。"""
    return [
        item
        for item in results
        if isinstance(item, BaseException) and not isinstance(item, AppError)
    ]


def describe(errors: list[BaseException]) -> str:
    return str([(type(e).__name__, str(e)[:160]) for e in errors])


async def _task_id_of(instance_id: str) -> int:
    async with session_scope() as session:
        task = await session.scalar(
            select(ApprovalTask).where(ApprovalTask.instance_id == instance_id)
        )
        assert task is not None, f"{instance_id} 未拉取，模块夹具应已处理"
        return task.id


# ── 夹具 ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def comment_ready_task(db_runner, mock_app: Any, mock_module: Any) -> tuple[str, int, int]:
    """准备一份"已审查、未回写"的任务（AP-002），返回 ``(instance_id, task_id, review_id)``。

    用 AP-002 而不是 AP-005：后者是**空文件**，连解析都过不了（EMPTY_CONTRACT_CONTENT），
    拿不到审查结果。选 AP-002 还能顺带覆盖"零命中 → 无命中模板"的评论正文。

    ⚠️ 必须同时清理**两处**状态：库里的 ``comment_logs`` 与模拟审批系统**内存里**的同幂等键评论。
    只清前者的话，其它用例（如 ``test_m4_review_db`` 的 REST 闭环）已经用同一个
    ``review_id`` 写过一次，mock 侧会保留该键，导致"新增 1 条评论"的断言失败——
    这类跨用例耦合在第一次全量跑时才暴露出来。
    """
    client = ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))

    async def prepare() -> tuple[str, int, int]:
        instance_id = "AP-002"
        await list_pending_contract_approvals(20, client=client)
        task_id = await _task_id_of(instance_id)

        await parse_task(task_id, client=client)
        await run_full_review(task_id, llm=NullLLMClient())

        async with session_scope() as session:
            review = await session.scalar(
                select(ReviewResult).where(ReviewResult.task_id == task_id)
            )
            assert review is not None
            review_id = review.id
            task = await session.get(ApprovalTask, task_id)
            assert task is not None
            task.write_status = WriteStatus.NOT_WRITTEN.value
            await session.execute(delete(CommentLog).where(CommentLog.task_id == task_id))
            await session.commit()

        # 同步清掉 mock 内存里该幂等键的评论，让"本次只新增 1 条"成立
        key = build_idempotency_key(instance_id, review_id)
        mock_module._comments.pop(key, None)
        keys = mock_module._comments_by_instance.get(instance_id, [])
        mock_module._comments_by_instance[instance_id] = [item for item in keys if item != key]
        return instance_id, task_id, review_id

    return db_runner(prepare)


@pytest.fixture(scope="module")
def parsed_review_task(db_runner, mock_app: Any) -> int:
    """准备一份"已解析"的任务（AP-001），返回 ``task_id``。"""
    client = ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))

    async def prepare() -> int:
        await list_pending_contract_approvals(20, client=client)
        task_id = await _task_id_of("AP-001")
        await parse_task(task_id, client=client)
        return task_id

    return db_runner(prepare)


# ── 1. 并发回写同一份审查结果（幂等键唯一索引）─────────────────────────────


def _concurrent_write(
    db_runner: Any, approval_client: ApprovalSystemClient, task_id: int, review_id: int
) -> list[Any]:
    async def one(_: int) -> Any:
        async with session_scope() as session:
            task = await session.get(ApprovalTask, task_id)
            review = await session.get(ReviewResult, review_id)
            assert task is not None and review is not None
            result = await write_approval_comment(session, task, review, client=approval_client)
            await session.commit()
            return result

    return gather(db_runner, one)


def test_concurrent_comment_write_keeps_single_row(
    live_db: str,
    db_runner,
    approval_client: ApprovalSystemClient,
    mock_module: Any,
    comment_ready_task: tuple[str, int, int],
) -> None:
    """FR-COM-02 / NF-03：6 路并发回写同一条评论 → 库里 1 行、审批系统 1 条、remark_id 一致。

    并发冲突必须表现为 ``duplicate=True``，**不得**冒泡成 ``IntegrityError``（那是 500）。
    """
    instance_id, task_id, review_id = comment_ready_task
    key = build_idempotency_key(instance_id, review_id)
    before = list(mock_module._comments_by_instance.get(instance_id, []))
    assert key not in before, "夹具应已清掉该幂等键的 mock 侧评论，否则'只新增 1 条'无法断言"

    results = _concurrent_write(db_runner, approval_client, task_id, review_id)
    errors = fatal(results)
    assert not errors, f"并发回写出现非业务异常：{describe(errors)}"

    async def rows() -> int:
        async with session_scope() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(CommentLog)
                    .where(CommentLog.idempotency_key == key)
                )
                or 0
            )

    assert db_runner(rows) == 1, "幂等键唯一索引必须保证只有一行逻辑评论"

    ok = [r for r in results if not isinstance(r, BaseException)]
    assert ok, "至少要有一个调用成功"
    assert all(r.write_status == WriteStatus.SUCCESS.value for r in ok)
    assert {r.remark_id for r in ok} == {ok[0].remark_id}, "并发下 remark_id 必须一致"
    # 证明"真的并发到了"：6 个调用方在同一时刻都没有看到已有记录，
    # 因此除第一个外都必须走 ON DUPLICATE KEY 分支并返回 duplicate=True。
    assert any(r.duplicate for r in ok), "没有任何调用命中 duplicate 分支，说明并发窗口没有重叠（用例失去意义）"

    after = list(mock_module._comments_by_instance.get(instance_id, []))
    assert after.count(key) == 1, f"审批系统侧同一幂等键只应留下 1 条评论，实际 {after.count(key)}"
    assert len(after) - len(before) == 1


def test_concurrent_comment_write_converges_to_success(
    live_db: str,
    db_runner,
    approval_client: ApprovalSystemClient,
    comment_ready_task: tuple[str, int, int],
) -> None:
    """收敛性：并发结束后 ``write_status`` 必须是唯一的确定终态，且只有一行评论记录。"""
    _, task_id, review_id = comment_ready_task
    _concurrent_write(db_runner, approval_client, task_id, review_id)

    async def final() -> tuple[str, int, str | None]:
        async with session_scope() as session:
            task = await session.get(ApprovalTask, task_id)
            assert task is not None
            rows = (
                await session.execute(select(CommentLog).where(CommentLog.task_id == task_id))
            ).scalars().all()
            return task.write_status, len(rows), rows[0].remark_id if rows else None

    write_status, rows, remark_id = db_runner(final)
    assert write_status == WriteStatus.SUCCESS.value
    assert rows == 1
    assert remark_id and remark_id.startswith("RMK-")


# ── 2. 并发新增同一 rule_code ──────────────────────────────────────────────


def test_concurrent_rule_create_conflicts_are_business_errors(live_db: str, db_runner) -> None:
    """并发建同一个 ``rule_code``：只允许成功 1 次，其余必须是 422，**不能是 500**。"""
    code = "R951"

    async def clean() -> None:
        async with session_scope() as session:
            rule_ids = (
                await session.execute(select(ReviewRule.id).where(ReviewRule.rule_code == code))
            ).scalars().all()
            if rule_ids:
                await session.execute(delete(RuleHit).where(RuleHit.rule_id.in_(rule_ids)))
            await session.execute(delete(ReviewRule).where(ReviewRule.rule_code == code))
            await session.commit()

    db_runner(clean)

    async def one(_: int) -> str:
        async with session_scope() as session:
            rule = await create_rule(
                session,
                RuleCreateRequest(
                    rule_code=code,
                    rule_name="并发用例规则",
                    risk_level="low",
                    match_mode="keyword",
                    match_text="并发",
                    suggestion_text="并发用例建议。",
                    target_section="clause_info",
                ),
            )
            return rule.rule_code

    try:
        results = gather(db_runner, one)
        errors = fatal(results)
        assert not errors, f"并发建规则出现非业务异常：{describe(errors)}"

        succeeded = [r for r in results if not isinstance(r, BaseException)]
        conflicts = [r for r in results if isinstance(r, AppError)]
        assert len(succeeded) == 1, f"只应成功一次，实际 {len(succeeded)}"
        assert len(conflicts) == len(results) - 1
        assert all(e.code is ErrorCode.VALIDATION_ERROR for e in conflicts), (
            "冲突必须是 422 VALIDATION_ERROR（唯一索引冲突不得变成 500）"
        )

        async def rows() -> int:
            async with session_scope() as session:
                return int(
                    await session.scalar(
                        select(func.count())
                        .select_from(ReviewRule)
                        .where(ReviewRule.rule_code == code)
                    )
                    or 0
                )

        assert db_runner(rows) == 1
    finally:
        # 必须在 finally 里清理：断言失败时规则会留在库里且**默认启用**，
        # 会污染后续用例（实测就让 rule_hits 多出一条）。
        db_runner(clean)


# ── 3. 并发改同一规则的不同字段：不丢更新 ─────────────────────────────────


def test_concurrent_rule_updates_do_not_lose_changes(live_db: str, db_runner) -> None:
    """并发改**不同字段**时两边都要生效（行级锁串行化，后写者只提交自己改的列）。"""
    code = "R952"

    async def setup() -> int:
        async with session_scope() as session:
            await session.execute(delete(ReviewRule).where(ReviewRule.rule_code == code))
            await session.commit()
        async with session_scope() as session:
            rule = await create_rule(
                session,
                RuleCreateRequest(
                    rule_code=code,
                    rule_name="并发更新用例",
                    risk_level="medium",
                    match_mode="keyword",
                    match_text="x",
                    suggestion_text="原建议。",
                    target_section="clause_info",
                ),
            )
            return rule.id

    rule_id = db_runner(setup)

    async def change_level() -> str:
        async with session_scope() as session:
            rule = await update_rule(session, RuleUpdateRequest(rule_id=rule_id, risk_level="high"))
            return rule.risk_level

    async def change_suggestion() -> str:
        async with session_scope() as session:
            rule = await update_rule(
                session, RuleUpdateRequest(rule_id=rule_id, suggestion_text="并发写入的新建议。")
            )
            return rule.suggestion_text

    async def scenario() -> list[Any]:
        return await asyncio.gather(change_level(), change_suggestion(), return_exceptions=True)

    level_result, suggestion_result = db_runner(scenario)
    assert not isinstance(level_result, BaseException), level_result
    assert not isinstance(suggestion_result, BaseException), suggestion_result

    async def final() -> tuple[str, str]:
        async with session_scope() as session:
            rule = await session.get(ReviewRule, rule_id)
            assert rule is not None
            return rule.risk_level, rule.suggestion_text

    level, suggestion = db_runner(final)
    assert level == "high", "等级改动被丢弃（丢更新）"
    assert suggestion == "并发写入的新建议。", "建议改动被丢弃（丢更新）"

    async def clean() -> None:
        async with session_scope() as session:
            await session.execute(delete(ReviewRule).where(ReviewRule.rule_code == code))
            await session.commit()

    db_runner(clean)


# ── 4. 并发拉取：唯一索引兜底，且不得死锁 ─────────────────────────────────


def test_concurrent_pull_stays_single_task_per_instance(
    live_db: str, db_runner, mock_app: Any
) -> None:
    """TS-01 加强版：6 路并发拉全量待办 → 每个 ``instance_id`` 恰好 1 条任务，无 1213 死锁。"""
    client = ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))

    async def one(_: int) -> int:
        result = await list_pending_contract_approvals(20, client=client)
        return len(result.items)

    results = gather(db_runner, one)
    errors = fatal(results)
    assert not errors, f"并发拉取出现异常：{describe(errors)}"
    assert all(item == 5 for item in results), f"每路都应看到 5 条待办，实际 {results}"

    async def duplicates() -> list[tuple[str, int]]:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(ApprovalTask.instance_id, func.count())
                    .group_by(ApprovalTask.instance_id)
                    .having(func.count() > 1)
                )
            ).all()
            return [(row[0], int(row[1])) for row in rows]

    assert db_runner(duplicates) == [], "并发拉取不得产生重复任务（FR-APP-03）"


# ── 5. 并发审查同一任务：upsert 幂等，不产生第二行 ──────────────────────────


def test_concurrent_review_keeps_single_result_row(
    live_db: str, db_runner, parsed_review_task: int
) -> None:
    """并发触发同一次审查：``review_results`` 每任务唯一、``rule_hits`` 每 (task, rule) 唯一。"""
    from app.modules.rules.loader import load_enabled_rules

    task_id = parsed_review_task

    async def enabled_count() -> int:
        async with session_scope() as session:
            return len(await load_enabled_rules(session))

    expected = db_runner(enabled_count)

    async def one(_: int) -> int | None:
        pipeline = await run_full_review(task_id, llm=NullLLMClient())
        return pipeline.saved.review_id

    results = gather(db_runner, one, n=4)
    errors = fatal(results)
    assert not errors, f"并发审查出现异常：{describe(errors)}"
    assert len({r for r in results if not isinstance(r, BaseException)}) == 1, "只应有一份审查结果"

    async def counts() -> tuple[int, int, int]:
        async with session_scope() as session:
            reviews = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ReviewResult)
                    .where(ReviewResult.task_id == task_id)
                )
                or 0
            )
            hits = int(
                await session.scalar(
                    select(func.count()).select_from(RuleHit).where(RuleHit.task_id == task_id)
                )
                or 0
            )
            distinct = int(
                await session.scalar(
                    select(func.count(func.distinct(RuleHit.rule_id))).where(
                        RuleHit.task_id == task_id
                    )
                )
                or 0
            )
            return reviews, hits, distinct

    reviews, hits, distinct = db_runner(counts)
    assert reviews == 1, "review_results 每任务只允许一行（DT-06 唯一索引）"
    # 不写死 11：规则集是数据（可在维护页增删），断言的是"每规则恰好一行"
    assert hits == distinct == expected, f"rule_hits 应恰好每规则一行：{hits} 行 / {distinct} 规则 / 启用 {expected}"
