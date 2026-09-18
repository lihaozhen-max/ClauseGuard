"""规则加载（SPEC FR-RULE-01、FR-RULE-08）。

只加载 ``rule_status=enabled`` 的规则；阈值缺省时回落到 ``CF-09``/``CF-10`` 配置
（回落在 ``predicates`` 里做，本模块只负责取数据）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RuleStatus
from app.core.logging import get_logger
from app.db.models import ReviewRule

logger = get_logger(__name__)


async def load_enabled_rules(session: AsyncSession) -> list[ReviewRule]:
    """按 ``rule_code`` 顺序返回启用的规则（顺序稳定，便于回归比对）。"""
    rows = (
        await session.execute(
            select(ReviewRule)
            .where(ReviewRule.rule_status == RuleStatus.ENABLED.value)
            .order_by(ReviewRule.rule_code)
        )
    ).scalars().all()
    logger.info("加载启用规则 %d 条：%s", len(rows), "、".join(rule.rule_code for rule in rows))
    return list(rows)


async def load_all_rules(session: AsyncSession) -> list[ReviewRule]:
    """返回全部规则（含停用），供规则维护页（IF-21，M6）使用。"""
    rows = (
        await session.execute(select(ReviewRule).order_by(ReviewRule.rule_code))
    ).scalars().all()
    return list(rows)
