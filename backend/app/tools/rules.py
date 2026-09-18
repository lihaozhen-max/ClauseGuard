"""规则审查工具接口 IF-05（签名与 PRD §15 一致：``run_contract_rules(case_id)``）。

``case_id`` ≡ ``approval_tasks.id``（SPEC §2.1）。
"""

from __future__ import annotations

from app.core.errors import AppError
from app.db.session import session_scope
from app.modules.rules.service import RuleRunResult, run_rules_for_task


async def run_contract_rules(
    case_id: int,
    *,
    settings=None,
    llm=None,
) -> RuleRunResult:
    """IF-05：执行全部启用的风险规则并返回命中、整体等级、摘要与关注点。

    异常：``case_id`` 不存在 → 404；无解析结果 → 409 ``PARSE_REQUIRED``。
    """
    async with session_scope() as session:
        try:
            result = await run_rules_for_task(session, case_id, settings=settings, llm=llm)
        except AppError:
            await session.commit()  # 失败前可能已写入部分命中，不能丢
            raise
        await session.commit()
    return result
