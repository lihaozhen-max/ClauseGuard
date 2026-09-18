"""Rule Engine 编排（SPEC FR-RULE-01…FR-RULE-11、IF-05）。

流程：加载启用规则 → 逐条判定（单条异常**禁止**中断其他规则）→ 快照命中 →
按 ``(task_id, rule_id)`` upsert 落库 → RL-AGG 汇总 → 模板摘要与关注点 → 落日志。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.enums import HitSource, HitStatus, LogLevel, LogType, RiskLevel
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.timeutil import format_duration
from app.db.models import ApprovalTask, ContractParse, ReviewRule, RuleHit
from app.llm.base import LLMClient
from app.llm.factory import build_llm_client
from app.modules.approval.state import (
    BLOCKED_STAGE_REVIEWING,
    fail_and_block,
)
from app.modules.logging.service import write_task_log
from app.modules.parser.models import ParsedDocument
from app.modules.review.summary import build_template_focus_points, build_template_summary
from app.modules.rules.aggregator import aggregate_overall_risk, count_by_level, count_by_status
from app.modules.rules.context import RuleContext, RuleOutcome, uncertain
from app.modules.rules.loader import load_enabled_rules
from app.modules.rules.predicates import resolve_handler

logger = get_logger(__name__)


@dataclass
class EvaluatedRule:
    """一条规则的完整判定记录（含快照，落库与返回共用）。"""

    rule_id: int
    rule_code: str
    rule_name: str
    risk_level: str
    suggestion_text: str
    outcome: RuleOutcome
    error: str | None = None

    def to_hit_row(self, task_id: int) -> dict[str, Any]:
        """DT-05 行值：``risk_level``/``suggestion_text`` 是**命中时快照**（FR-RULE-09）。"""
        return {
            "task_id": task_id,
            "rule_id": self.rule_id,
            "risk_level": self.risk_level,
            "evidence_text": self.outcome.evidence.text,
            "evidence_position": self.outcome.evidence.position,
            "suggestion_text": self.suggestion_text,
            "hit_source": str(self.outcome.hit_source),
            "hit_status": str(self.outcome.hit_status),
        }

    def to_dict(self) -> dict[str, Any]:
        """IF-05 的 ``rule_hits`` 元素（字段名遵循 SPEC §5.1 示例）。"""
        return {
            "rule_code": self.rule_code,
            "rule_name": self.rule_name,
            "risk_level": self.risk_level,
            "hit_status": str(self.outcome.hit_status),
            "hit_source": str(self.outcome.hit_source),
            "evidence_text": self.outcome.evidence.text,
            "evidence_position": self.outcome.evidence.position,
            "suggestion": self.suggestion_text,
            "reason": self.outcome.reason,
        }


@dataclass
class RuleRunResult:
    """IF-05 的返回结构。"""

    task_id: int
    overall_risk_level: str
    hit_count: int
    uncertain_count: int
    rule_hits: list[dict[str, Any]] = field(default_factory=list)
    summary_text: str = ""
    focus_points: list[str] = field(default_factory=list)
    evaluated_rules: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.task_id,
            "overall_risk_level": self.overall_risk_level,
            "hit_count": self.hit_count,
            "uncertain_count": self.uncertain_count,
            "rule_hits": self.rule_hits,
            "summary_text": self.summary_text,
            "focus_points": self.focus_points,
            "evaluated_rules": self.evaluated_rules,
            "warnings": self.warnings,
        }


async def _evaluate_rule(ctx: RuleContext, rule: ReviewRule) -> EvaluatedRule:
    """执行一条规则；异常**不向上抛**（FR-RULE-06），转 ``uncertain`` 并留 error。"""
    suggestion = str(rule.suggestion_text or "")
    handler = resolve_handler(rule)
    try:
        outcome = await handler(ctx, rule)
    except Exception as exc:  # noqa: BLE001 - 单条规则异常禁止中断其他规则
        logger.warning("规则 %s 执行异常：%s: %s", rule.rule_code, type(exc).__name__, exc)
        outcome = uncertain(suggestion, reason=f"规则执行异常：{type(exc).__name__}: {exc}")
        return EvaluatedRule(
            rule.id,
            rule.rule_code,
            rule.rule_name,
            rule.risk_level,
            suggestion,
            outcome,
            error=f"{type(exc).__name__}: {exc}",
        )
    return EvaluatedRule(
        rule.id, rule.rule_code, rule.rule_name, rule.risk_level, suggestion, outcome
    )


async def run_rules_for_task(
    session: AsyncSession,
    task_id: int,
    *,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
) -> RuleRunResult:
    """IF-05 的核心实现：对已解析的任务执行全部启用规则。

    异常：``task_id`` 不存在 → ``APPROVAL_NOT_FOUND``(404)；
    无解析结果 → ``PARSE_REQUIRED``(409)（SPEC IF-05 约束）。
    """
    settings = settings or get_settings()
    started = time.perf_counter()

    task = await session.get(ApprovalTask, task_id)
    if task is None:
        raise AppError(
            ErrorCode.APPROVAL_NOT_FOUND,
            f"任务 {task_id} 不存在",
            task_id=task_id,
            detail={"case_id": task_id},
        )

    parse_row = await session.scalar(
        select(ContractParse)
        .where(ContractParse.task_id == task_id)
        .order_by(ContractParse.id.desc())
        .limit(1)
    )
    if parse_row is None or not parse_row.full_text:
        raise AppError(
            ErrorCode.PARSE_REQUIRED,
            f"任务 {task_id} 尚未解析，无法执行规则审查",
            task_id=task_id,
            detail={"case_id": task_id},
        )

    try:
        document = ParsedDocument.from_stored(
            parse_row.full_text, parse_row.page_map_json, parse_row.parse_mode
        )
        ctx = RuleContext.build(
            document=document,
            basic_info=list(parse_row.basic_info_json or []),
            clause_info=list(parse_row.clause_info_json or []),
            settings=settings,
            llm=llm or build_llm_client(settings),
        )

        rules = await load_enabled_rules(session)
        evaluated: list[EvaluatedRule] = []
        warnings: list[str] = []
        for rule in rules:
            item = await _evaluate_rule(ctx, rule)
            if item.error:
                warnings.append(f"{item.rule_code}：{item.error}")
            evaluated.append(item)

        # FR-RULE-07：按 (task_id, rule_id) upsert，重复执行覆盖旧命中，不产生重复行
        for item in evaluated:
            values = item.to_hit_row(task_id)
            statement = mysql_insert(RuleHit.__table__).values(**values)
            await session.execute(
                statement.on_duplicate_key_update(
                    risk_level=values["risk_level"],
                    evidence_text=values["evidence_text"],
                    evidence_position=values["evidence_position"],
                    suggestion_text=values["suggestion_text"],
                    hit_source=values["hit_source"],
                    hit_status=values["hit_status"],
                )
            )
        await session.flush()
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        # FR-RULE-11：规则执行**阶段**异常（非单条规则异常）→ 任务 blocked / reviewing。
        # 单条规则自身的异常已由 _evaluate_rule 兜住并记 uncertain（FR-RULE-06），不会走到这里。
        error = AppError(
            ErrorCode.RULE_EXECUTION_FAILED,
            f"规则执行阶段异常：{type(exc).__name__}: {exc}",
            task_id=task_id,
        )
        await fail_and_block(
            session,
            task,
            stage=BLOCKED_STAGE_REVIEWING,
            error_code=ErrorCode.RULE_EXECUTION_FAILED,
            log_type=LogType.RULE,
            message=error.message,
        )
        raise error from exc

    counts = count_by_status([item.outcome for item in evaluated])
    overall = aggregate_overall_risk(
        [
            {"hit_status": str(item.outcome.hit_status), "risk_level": item.risk_level}
            for item in evaluated
        ]
    )
    hits = [item.to_dict() for item in evaluated]
    levels = count_by_level(
        [{"hit_status": str(item.outcome.hit_status), "risk_level": item.risk_level} for item in evaluated]
    )

    # ── 日志（FR-LOG-01：规则执行必须落 task_logs）────────────────────────
    hit_names = "、".join(
        item.rule_code
        for item in evaluated
        if item.outcome.hit_status is HitStatus.HIT
    )
    await write_task_log(
        session,
        task_id,
        LogType.RULE,
        f"规则审查完成：执行 {len(evaluated)} 条规则｜命中 {counts['hit']} 条"
        f"（高风险 {levels.get(RiskLevel.HIGH.value, 0)}、中风险 {levels.get(RiskLevel.MEDIUM.value, 0)}）"
        f"｜uncertain {counts['uncertain']} 条｜整体风险 {overall}"
        + (f"｜命中：{hit_names}" if hit_names else "")
        + f"｜耗时 {format_duration(time.perf_counter() - started)}",
    )
    if counts["uncertain"]:
        uncertain_names = "、".join(
            item.rule_code for item in evaluated if item.outcome.hit_status is HitStatus.UNCERTAIN
        )
        await write_task_log(
            session,
            task_id,
            LogType.RULE,
            f"存在无法判定的规则（需人工确认，不计入整体风险等级）：{uncertain_names}",
            level=LogLevel.WARNING,
        )
    if warnings:
        await write_task_log(
            session,
            task_id,
            LogType.RULE,
            "规则执行异常：" + "；".join(warnings),
            level=LogLevel.ERROR,
        )

    return RuleRunResult(
        task_id=task_id,
        overall_risk_level=overall,
        hit_count=counts["hit"],
        uncertain_count=counts["uncertain"],
        rule_hits=hits,
        summary_text=build_template_summary(hits),
        focus_points=build_template_focus_points(hits),
        evaluated_rules=len(evaluated),
        warnings=warnings,
    )


async def list_rule_hits(session: AsyncSession, task_id: int) -> list[RuleHit]:
    """读回已落库的命中（IF-15 用）。"""
    rows = (
        await session.execute(
            select(RuleHit).where(RuleHit.task_id == task_id).order_by(RuleHit.rule_id)
        )
    ).scalars().all()
    return list(rows)


__all__ = [
    "EvaluatedRule",
    "RuleRunResult",
    "list_rule_hits",
    "run_rules_for_task",
    "HitSource",
]
