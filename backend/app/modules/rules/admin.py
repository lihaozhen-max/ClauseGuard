"""规则维护（SPEC IF-21 / FR-RULE-08 / FR-UI-08）。

只负责规则的**查询与增改**——规则执行仍在 ``modules/rules/service.py``，
规则加载在 ``modules/rules/loader.py``。三个模块的边界刻意保持清晰：

| 模块 | 职责 |
|---|---|
| ``loader`` | 只读：``rule_status=enabled`` 供审查链路使用 |
| ``admin``（本模块） | 读写：规则维护页（IF-21）的查询/新增/修改 |
| ``service`` | 执行：按启用规则跑判定并落 ``rule_hits`` |

**规则变更不回改历史命中**：``rule_hits`` 存的是命中时的等级与建议快照
（FR-RULE-09），改规则只影响**之后**的审查，符合"可追溯"要求（PRD 19.1）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RuleStatus
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.db.models import ReviewRule
from app.schemas.rule import RuleCreateRequest, RuleModel, RuleUpdateRequest

logger = get_logger(__name__)


def to_rule_model(rule: ReviewRule) -> RuleModel:
    """ORM → 接口模型（内部主键 ``id`` 对外叫 ``rule_id``）。"""
    return RuleModel(
        rule_id=rule.id,
        rule_code=rule.rule_code,
        rule_name=rule.rule_name,
        risk_level=rule.risk_level,
        rule_status=rule.rule_status,
        match_mode=rule.match_mode,
        match_text=rule.match_text,
        match_params_json=rule.match_params_json,
        suggestion_text=rule.suggestion_text,
        target_section=rule.target_section,
        updated_at=rule.updated_at,
    )


async def list_rules(session: AsyncSession) -> list[ReviewRule]:
    """全部规则（含停用），按 ``rule_code`` 排序。"""
    rows = (
        await session.execute(select(ReviewRule).order_by(ReviewRule.rule_code))
    ).scalars().all()
    return list(rows)


async def count_by_status(session: AsyncSession) -> dict[str, int]:
    """启用/停用条数（规则维护页顶部统计）。"""
    rows = await session.execute(
        select(ReviewRule.rule_status, func.count()).group_by(ReviewRule.rule_status)
    )
    counts = {status: 0 for status in (RuleStatus.ENABLED.value, RuleStatus.DISABLED.value)}
    for status, amount in rows.all():
        counts[str(status)] = int(amount)
    return counts


async def _get_by_id(session: AsyncSession, rule_id: int) -> ReviewRule:
    rule = await session.get(ReviewRule, rule_id)
    if rule is None:
        raise AppError(
            ErrorCode.RULE_NOT_FOUND,
            f"规则 {rule_id} 不存在",
            detail={"rule_id": rule_id},
        )
    return rule


async def _assert_code_available(
    session: AsyncSession, rule_code: str, *, exclude_id: int | None = None
) -> None:
    """``rule_code`` 全局唯一（DT-05 唯一索引 + 这里给出可读的 422）。"""
    stmt = select(ReviewRule.id).where(ReviewRule.rule_code == rule_code)
    if exclude_id is not None:
        stmt = stmt.where(ReviewRule.id != exclude_id)
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        # 唯一键冲突属"请求参数不合法"，沿用 M5 的 422 VALIDATION_ERROR 口径
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"规则编码 {rule_code} 已存在",
            detail={"rule_code": rule_code, "rule_id": existing},
        )


async def create_rule(session: AsyncSession, payload: RuleCreateRequest) -> ReviewRule:
    """新增规则（IF-21 ``POST``）。"""
    await _assert_code_available(session, payload.rule_code)
    rule = ReviewRule(
        rule_code=payload.rule_code,
        rule_name=payload.rule_name,
        risk_level=payload.risk_level.value,
        rule_status=payload.rule_status.value,
        match_mode=payload.match_mode.value,
        match_text=payload.match_text,
        match_params_json=payload.match_params_json,
        suggestion_text=payload.suggestion_text,
        target_section=payload.target_section,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    logger.info("新增规则 %s（%s，%s）", rule.rule_code, rule.rule_name, rule.risk_level)
    return rule


async def update_rule(session: AsyncSession, payload: RuleUpdateRequest) -> ReviewRule:
    """修改规则（IF-21 ``PUT``）：启用/停用、改等级、改建议等。"""
    changes: dict[str, Any] = payload.changes()
    if not changes:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "未提供任何待修改字段",
            detail={"rule_id": payload.rule_id},
        )

    rule = await _get_by_id(session, payload.rule_id)
    before = {
        "rule_status": rule.rule_status,
        "risk_level": rule.risk_level,
        "suggestion_text": rule.suggestion_text,
    }
    for field, value in changes.items():
        # 枚举字段落库取 ``.value``；其余原样写入
        setattr(rule, field, value.value if hasattr(value, "value") else value)
    await session.commit()
    await session.refresh(rule)
    logger.info(
        "更新规则 %s：状态 %s→%s，等级 %s→%s，字段=%s",
        rule.rule_code,
        before["rule_status"],
        rule.rule_status,
        before["risk_level"],
        rule.risk_level,
        "、".join(sorted(changes)),
    )
    return rule
