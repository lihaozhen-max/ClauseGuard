"""任务相关内部 REST 路由（SPEC §5.2）。

本里程碑实现：

| 编号 | 方法 | 路径 | 用途 |
|---|---|---|---|
| IF-10 | POST | ``/api/tasks/pull`` | 触发待办拉取（body ``{"limit":20}``） |
| IF-11 | GET | ``/api/tasks?status=&page=&size=`` | 任务列表 |
| IF-12 | GET | ``/api/tasks/{task_id}`` | 任务详情（含附件） |

全部要求 ``X-API-Key``（FR-SYS-03，缺省/错误 → 401）。
IF-13…IF-21 自 M2 起在本 router 中继续追加。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_approval_client, get_llm_client
from app.clients.approval_client import ApprovalSystemClient
from app.core.enums import LogLevel, LogType, TaskStatus
from app.core.errors import AppError, ErrorCode
from app.core.security import require_api_key
from app.db.models import ApprovalTask
from app.db.session import get_session, session_scope
from app.llm.base import LLMClient
from app.modules.approval.service import get_local_task, list_local_tasks
from app.modules.comment.service import list_comment_logs
from app.modules.logging.service import collect_log_types, list_task_logs
from app.modules.parser.service import get_parse_row, row_to_outcome
from app.modules.review.service import get_review_result as get_review_result_row
from app.modules.review.summary import build_template_focus_points, build_template_summary
from app.modules.rules.aggregator import aggregate_overall_risk, count_by_status
from app.modules.rules.loader import load_all_rules
from app.modules.rules.service import list_rule_hits
from app.schemas.approval import (
    LocalAttachment,
    PullRequest,
    PullResult,
    TaskDetail,
    TaskListItem,
    TaskListResponse,
)
from app.schemas.log import RetryResponse, TaskLogList, TaskLogModel
from app.schemas.parse import ParseResultResponse
from app.schemas.review import (
    CommentLogList,
    CommentLogModel,
    CommentWriteModel,
    ReviewPipelineResponse,
    RuleHitModel,
)
from app.tools.approval import get_contract_approval, list_pending_contract_approvals
from app.tools.comment import write_approval_comment
from app.tools.parser import parse_task
from app.tools.retry import retry_task
from app.tools.review import run_full_review

router = APIRouter(prefix="/api", tags=["任务"], dependencies=[Depends(require_api_key)])


def _to_task_item(task: ApprovalTask) -> TaskListItem:
    return TaskListItem(
        task_id=task.id,
        instance_id=task.instance_id,
        approval_code=task.approval_code,
        approval_title=task.approval_title,
        applicant_name=task.applicant_name,
        apply_time=task.apply_time,
        attachment_count=task.attachment_count,
        current_status=task.current_status,
        task_status=task.task_status,
        write_status=task.write_status,
    )


@router.post("/tasks/pull", response_model=PullResult, summary="IF-10 触发待办拉取")
async def pull_tasks(
    payload: PullRequest | None = Body(default=None),
    client: ApprovalSystemClient = Depends(get_approval_client),
) -> PullResult:
    """拉取待办并入库；同一 ``instance_id`` 二次调用只更新、不新建（FR-APP-02）。"""
    limit = payload.limit if payload is not None else PullRequest().limit
    return await list_pending_contract_approvals(limit, client=client)


@router.get("/tasks", response_model=TaskListResponse, summary="IF-11 任务列表")
async def list_tasks(
    status: TaskStatus | None = Query(default=None, description="按 task_status 过滤"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> TaskListResponse:
    rows, total = await list_local_tasks(
        session, status=status.value if status else None, page=page, size=size
    )
    return TaskListResponse(
        items=[_to_task_item(row) for row in rows],
        total=total,
        page=page,
        size=size,
    )


@router.get("/tasks/{task_id}", response_model=TaskDetail, summary="IF-12 任务详情（含附件）")
async def task_detail(
    task_id: int,
    session: AsyncSession = Depends(get_session),
) -> TaskDetail:
    task = await get_local_task(session, task_id)
    if task is None:
        # SPEC §5.4 的 12 个规范错误码未单列"任务不存在"，但 IF-05 对不存在的 case_id
        # 同样只要求 404；此处沿用 404 + APPROVAL_NOT_FOUND，并在 detail 中给出 task_id。
        raise AppError(
            ErrorCode.APPROVAL_NOT_FOUND,
            f"任务 {task_id} 不存在",
            task_id=task_id,
            detail={"task_id": task_id},
        )
    return TaskDetail(
        task_id=task.id,
        instance_id=task.instance_id,
        approval_code=task.approval_code,
        approval_title=task.approval_title,
        applicant_name=task.applicant_name,
        apply_time=task.apply_time,
        attachment_count=task.attachment_count,
        current_status=task.current_status,
        contract_type=task.contract_type,
        form_data=dict(task.form_data_json or {}),
        task_status=task.task_status,
        write_status=task.write_status,
        blocked_stage=task.blocked_stage,
        error_code=task.error_code,
        error_message=task.error_message,
        retry_count=task.retry_count,
        created_at=task.created_at,
        updated_at=task.updated_at,
        attachments=[
            LocalAttachment(
                attachment_id=a.attachment_code,
                file_name=a.file_name,
                file_type=a.file_type,
                file_size=a.file_size,
                file_checksum=a.file_checksum,
                download_status=a.download_status,
            )
            for a in task.attachments
        ],
    )


@router.get("/approvals/{instance_id}", summary="IF-02 审批详情（调试用同构入口）")
async def approval_detail(
    instance_id: str,
    client: ApprovalSystemClient = Depends(get_approval_client),
):
    """IF-02 的 REST 形态。

    SPEC §5.2 的内网路由表未列 IF-02（工具接口 IF-01…IF-07 由工具层暴露），
    但调用端详情页需要它，故一并提供；语义与工具接口完全一致。
    """
    return await get_contract_approval(instance_id, client=client)


@router.get(
    "/tasks/{task_id}/parse",
    response_model=ParseResultResponse,
    summary="IF-13 解析结果",
)
async def get_parse_result(
    task_id: int,
    session: AsyncSession = Depends(get_session),
) -> ParseResultResponse:
    """查询解析结果（16 个字段 + 原文 + 位置 + 提取状态）。

    尚未解析 → ``409 PARSE_REQUIRED``（与 IF-05 对"未解析即审查"的处理一致）。
    解析失败时返回 200 + ``parse_status=failed`` + ``parse_error``——失败原因对排查有用，
    不应藏起来（FR-PARSE-07 要求必须记录 ``parse_error``）。
    """
    task = await get_local_task(session, task_id)
    if task is None:
        raise AppError(
            ErrorCode.APPROVAL_NOT_FOUND,
            f"任务 {task_id} 不存在",
            task_id=task_id,
            detail={"task_id": task_id},
        )
    row = await get_parse_row(session, task_id)
    if row is None:
        raise AppError(
            ErrorCode.PARSE_REQUIRED,
            f"任务 {task_id} 尚未解析",
            task_id=task_id,
            detail={"task_id": task_id},
        )
    return ParseResultResponse(**row_to_outcome(row).to_dict())


@router.post(
    "/tasks/{task_id}/parse",
    response_model=ParseResultResponse,
    summary="IF-14 触发解析（自动补下载）",
)
async def trigger_parse(
    task_id: int,
    client: ApprovalSystemClient = Depends(get_approval_client),
) -> ParseResultResponse:
    """触发"下载附件 + 解析"流水线（IF-03 + IF-04）。"""
    outcome = await parse_task(task_id, client=client)
    return ParseResultResponse(**outcome.to_dict())


def _hits_from_rows(rows: list[Any], rules: dict[int, Any]) -> list[dict[str, Any]]:
    return [
        {
            "rule_code": rules[row.rule_id].rule_code if row.rule_id in rules else str(row.rule_id),
            "rule_name": rules[row.rule_id].rule_name if row.rule_id in rules else "",
            "risk_level": row.risk_level,
            "hit_status": row.hit_status,
            "hit_source": row.hit_source,
            "evidence_text": row.evidence_text,
            "evidence_position": row.evidence_position,
            "suggestion": row.suggestion_text or "",
            "reason": "",
        }
        for row in rows
    ]


def _to_pipeline_response(
    task_id: int,
    hits: list[dict[str, Any]],
    review: Any | None,
    task: Any | None,
) -> ReviewPipelineResponse:
    """由库中命中与已保存的审查结果重建 IF-15/IF-16 的返回视图。"""
    overall = (
        review.overall_risk_level
        if review is not None
        else aggregate_overall_risk(
            [{"hit_status": item["hit_status"], "risk_level": item["risk_level"]} for item in hits]
        )
    )
    counts = count_by_status([{"hit_status": item["hit_status"]} for item in hits])
    return ReviewPipelineResponse(
        case_id=task_id,
        review_id=review.id if review is not None else None,
        overall_risk_level=overall,
        hit_count=counts["hit"],
        uncertain_count=counts["uncertain"],
        evaluated_rules=len(hits),
        rule_hits=[RuleHitModel(**item) for item in hits],
        summary_text=review.summary_text if review is not None else build_template_summary(hits),
        focus_points=list(review.focus_points_json or []) if review is not None else build_template_focus_points(hits),
        comment_text=review.comment_text if review is not None else "",
        task_status=task.task_status if task is not None else None,
        write_status=task.write_status if task is not None else None,
    )


@router.get(
    "/tasks/{task_id}/review",
    response_model=ReviewPipelineResponse,
    summary="IF-15 审查结果",
)
async def get_review_result(
    task_id: int,
    session: AsyncSession = Depends(get_session),
) -> ReviewPipelineResponse:
    """查询已落库的审查结果（规则命中 + 整体等级 + 摘要 + 关注点 + 评论正文）。

    尚未执行审查 → ``409 PARSE_REQUIRED``（消息会说明"请先触发审查"）。
    """
    task = await get_local_task(session, task_id)
    if task is None:
        raise AppError(
            ErrorCode.APPROVAL_NOT_FOUND,
            f"任务 {task_id} 不存在",
            task_id=task_id,
            detail={"task_id": task_id},
        )

    review = await get_review_result_row(session, task_id)
    rows = await list_rule_hits(session, task_id)
    if review is None and not rows:
        raise AppError(
            ErrorCode.PARSE_REQUIRED,
            f"任务 {task_id} 尚未执行规则审查，请先调用 POST /api/tasks/{task_id}/review",
            task_id=task_id,
            detail={"task_id": task_id},
        )

    rules = {rule.id: rule for rule in await load_all_rules(session)}
    return _to_pipeline_response(task_id, _hits_from_rows(rows, rules), review, task)


@router.post(
    "/tasks/{task_id}/review",
    response_model=ReviewPipelineResponse,
    summary="IF-16 触发审查并保存结果",
)
async def trigger_review(
    task_id: int,
    llm: LLMClient = Depends(get_llm_client),
    session: AsyncSession = Depends(get_session),
) -> ReviewPipelineResponse:
    """触发完整审查（IF-05 规则判定 + 摘要/关注点 + §4.6.1 评论 + IF-06 落库 → ``done``）。"""
    pipeline = await run_full_review(task_id, llm=llm)

    task = await get_local_task(session, task_id)
    rules = {rule.id: rule for rule in await load_all_rules(session)}
    rows = await list_rule_hits(session, task_id)
    response = _to_pipeline_response(task_id, _hits_from_rows(rows, rules), None, task)
    # 用本次运行的结果覆盖（库中读数与运行结果一致，这里显式带上评论与降级标记）
    response.review_id = pipeline.saved.review_id
    response.summary_text = pipeline.saved.summary_text
    response.focus_points = pipeline.saved.focus_points
    response.comment_text = pipeline.saved.comment_text
    response.summary_degraded = pipeline.saved.summary_degraded
    response.warnings = list(pipeline.run.warnings)
    return response


@router.post(
    "/tasks/{task_id}/write-comment",
    response_model=CommentWriteModel,
    summary="IF-17 触发/重试评论回写",
)
async def trigger_write_comment(
    task_id: int,
    client: ApprovalSystemClient = Depends(get_approval_client),
) -> CommentWriteModel:
    """把已保存的审查结果写回审批系统评论区（幂等，IF-07）。"""
    async with session_scope() as session:
        task = await get_local_task(session, task_id)
        if task is None:
            raise AppError(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"任务 {task_id} 不存在",
                task_id=task_id,
                detail={"task_id": task_id},
            )
        review = await get_review_result_row(session, task_id)
        if review is None:
            raise AppError(
                ErrorCode.PARSE_REQUIRED,
                f"任务 {task_id} 尚未生成审查结果，请先调用 POST /api/tasks/{task_id}/review",
                task_id=task_id,
                detail={"task_id": task_id},
            )
    result = await write_approval_comment(task.instance_id, review.id, client=client)
    return CommentWriteModel(**result.to_dict())


@router.get(
    "/tasks/{task_id}/comment-logs",
    response_model=CommentLogList,
    summary="IF-18 回写历史",
)
async def get_comment_logs(
    task_id: int,
    session: AsyncSession = Depends(get_session),
) -> CommentLogList:
    """查询评论回写状态与历史（DT-07）。"""
    task = await get_local_task(session, task_id)
    if task is None:
        raise AppError(
            ErrorCode.APPROVAL_NOT_FOUND,
            f"任务 {task_id} 不存在",
            task_id=task_id,
            detail={"task_id": task_id},
        )
    rows = await list_comment_logs(session, task_id)
    return CommentLogList(
        items=[
            CommentLogModel(
                id=row.id,
                task_id=row.task_id,
                write_status=row.write_status,
                write_response_text=row.write_response_text,
                remark_id=row.remark_id,
                idempotency_key=row.idempotency_key,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=len(rows),
    )


@router.get("/tasks/{task_id}/logs", response_model=TaskLogList, summary="IF-19 任务日志")
async def get_task_logs(
    task_id: int,
    log_type: LogType | None = Query(default=None, description="按 log_type 过滤"),
    level: LogLevel | None = Query(default=None, description="按 log_level 过滤"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> TaskLogList:
    """查询任务全链路日志（8 类核心操作，FR-LOG-01 / AC18）。

    ``log_types`` 直接给出该任务出现过的全部日志类型，便于核对 8 类是否齐备。
    """
    task = await get_local_task(session, task_id)
    if task is None:
        raise AppError(
            ErrorCode.APPROVAL_NOT_FOUND,
            f"任务 {task_id} 不存在",
            task_id=task_id,
            detail={"task_id": task_id},
        )

    rows, total = await list_task_logs(
        session,
        task_id,
        log_type=log_type.value if log_type else None,
        level=level.value if level else None,
        page=page,
        size=size,
    )
    kinds = await collect_log_types(session, task_id)
    return TaskLogList(
        items=[
            TaskLogModel(
                id=row.id,
                task_id=row.task_id,
                log_level=row.log_level,
                log_type=row.log_type,
                log_content=row.log_content,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
        page=page,
        size=size,
        log_types=sorted(kinds),
    )


@router.post("/tasks/{task_id}/retry", response_model=RetryResponse, summary="IF-20 人工重试")
async def retry_blocked_task(
    task_id: int,
    client: ApprovalSystemClient = Depends(get_approval_client),
    llm: LLMClient = Depends(get_llm_client),
) -> RetryResponse:
    """从失败阶段重试 ``blocked`` 任务，并继续跑到完成（IF-20 / AC17）。

    - 任务不处于 ``blocked`` → ``422 VALIDATION_ERROR``（无需重试）
    - 重试过程中再次失败 → 任务重新 ``blocked``，错误照常返回
    """
    outcome = await retry_task(task_id, client=client, llm=llm)
    return RetryResponse(**outcome.to_dict())
