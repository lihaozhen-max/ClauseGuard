"""规则维护路由（SPEC IF-21 / §5.2）。

| 编号 | 方法 | 路径 | 用途 |
|---|---|---|---|
| IF-21 | GET | ``/api/rules`` | 规则查询（含停用） |
| IF-21 | POST | ``/api/rules`` | 规则新增 |
| IF-21 | PUT | ``/api/rules`` | 规则修改（``rule_id`` 置于请求体） |

关于 ``PUT`` 的路径：SPEC §5.2 的"路径"列对 IF-21 三种方法统一写作 ``/api/rules``，
未给路径参数位；为不改动规范表，本实现照字面落成 ``PUT /api/rules``，
由请求体的 ``rule_id`` 定位目标规则（见 ``docs/M6-验收记录.md`` M6-B）。

鉴权同 IF-10…IF-20：``X-API-Key``（FR-SYS-03）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_api_key
from app.db.session import get_session
from app.modules.rules.admin import (
    count_by_status,
    create_rule,
    list_rules,
    to_rule_model,
    update_rule,
)
from app.schemas.rule import RuleCreateRequest, RuleListResponse, RuleModel, RuleUpdateRequest

router = APIRouter(prefix="/api", tags=["规则"], dependencies=[Depends(require_api_key)])


@router.get("/rules", response_model=RuleListResponse, summary="IF-21 规则查询")
async def get_rules(session: AsyncSession = Depends(get_session)) -> RuleListResponse:
    """返回全部规则（含 ``disabled``）与启停统计，供规则维护页渲染。"""
    rows = await list_rules(session)
    counts = await count_by_status(session)
    return RuleListResponse(
        items=[to_rule_model(rule) for rule in rows],
        total=len(rows),
        enabled_count=counts.get("enabled", 0),
        disabled_count=counts.get("disabled", 0),
    )


@router.post(
    "/rules",
    response_model=RuleModel,
    status_code=status.HTTP_201_CREATED,
    summary="IF-21 规则新增",
)
async def post_rule(
    payload: RuleCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> RuleModel:
    """新增一条规则；``rule_code`` 重复 → ``422 VALIDATION_ERROR``。"""
    rule = await create_rule(session, payload)
    return to_rule_model(rule)


@router.put("/rules", response_model=RuleModel, summary="IF-21 规则修改")
async def put_rule(
    payload: RuleUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> RuleModel:
    """按 ``rule_id`` 增量修改规则（启用/停用、改等级、改建议等，FR-UI-08）。

    - 规则不存在 → ``404 RULE_NOT_FOUND``
    - 未传任何可改字段 → ``422 VALIDATION_ERROR``
    """
    rule = await update_rule(session, payload)
    return to_rule_model(rule)
