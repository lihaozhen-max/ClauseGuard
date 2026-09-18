"""R001–R011 的判定实现（SPEC §8.2–§8.12）。

设计原则（设计 §7.3）：**能确定性判定的绝不用 LLM**。
只有 R003 / R004 / R009 / R010 会在"关键词/条款存在性"判完之后，把语义变体交给 LLM；
且 LLM 只在**允许用途**（LM-01）内出场，返回的原文片段必须通过 PS-10 回验。

每条规则都是 ``async def handler(ctx, rule) -> RuleOutcome``，可脱离数据库独立测试（RL-00-06）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from app.core.enums import HitSource
from app.core.logging import get_logger
from app.llm.base import judge_semantic
from app.modules.parser.fields import chinese_number_to_int
from app.modules.rules.context import RuleContext, RuleOutcome, hit, miss, uncertain
from app.modules.rules.evidence import NULL_EVIDENCE, Evidence, find_all_matches

logger = get_logger(__name__)

#: 百分比
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
#: 预付款线索（含 SPEC RL-001 正例的"签订后…支付…%"句式）
_PREPAY_HINT_RE = re.compile(r"预付|先付|首付|定金|签订后|签订之日起")
#: 付款线索
_PAY_HINT_RE = re.compile(r"支付|付款|账期|结算|周期|款项")
#: 账期时长：阿拉伯数字或中文数字 + 单位
_DURATION_RE = re.compile(
    r"(\d+|[一二三四五六七八九十百零]+)\s*(?:个)?\s*(工作日|日历日|日|天|个月|月|年)\s*(?:内|以内|之内)?"
)
#: 明确"须双方确认才续约"的排除句式（RL-003 边界）
_EXPLICIT_RENEWAL_RE = re.compile(r"书面确认|另行签署|双方确认后")

_UNIT_DAYS = {"工作日": 1, "日历日": 1, "日": 1, "天": 1, "个月": 30, "月": 30, "年": 365}

#: RL-011 三要素的中文名（suggestion 里要指明缺失项）
_ACCEPTANCE_LABELS = {
    "acceptance_time": "验收时间",
    "acceptance_method": "验收方式",
    "acceptance_criteria": "验收标准",
}


def _params(rule: Any) -> dict[str, Any]:
    params = getattr(rule, "match_params_json", None)
    return params if isinstance(params, dict) else {}


def _keywords(rule: Any) -> list[str]:
    raw = getattr(rule, "match_text", None) or ""
    return [item.strip() for item in re.split(r"[,，\n]", raw) if item.strip()]


def _suggestion(rule: Any) -> str:
    return str(getattr(rule, "suggestion_text", "") or "")


def _iter_sentences(text: str, base: int = 0) -> Iterator[tuple[str, int, int]]:
    """按句读切分，返回 ``(句子, 全文起始, 全文结束)``。"""
    for match in re.finditer(r"[^。！？；\n]*[。！？；\n]?", text):
        chunk = match.group(0)
        if not chunk.strip():
            continue
        yield chunk, base + match.start(), base + match.end()


def _to_days(number_text: str, unit: str) -> int | None:
    if number_text.isdigit():
        number = int(number_text)
    else:
        number = chinese_number_to_int(number_text)
    if not number:
        return None
    return number * _UNIT_DAYS.get(unit, 1)


async def _semantic(ctx: RuleContext, rule: Any, *, section: str | None, default_question: str) -> Any:
    """调用 LLM 做语义判定（问题取自 ``match_params_json.llm_question``）。"""
    question = str(_params(rule).get("llm_question") or default_question)
    judgement = await judge_semantic(
        ctx.llm, question=question, context=ctx.semantic_context(section)
    )
    return judgement


def _llm_hit(ctx: RuleContext, rule: Any, judgement: Any) -> RuleOutcome:
    """把 LLM 的"命中"结论落成结果；证据回验失败即降级 uncertain（PS-11）。"""
    suggestion = _suggestion(rule)
    evidence = ctx.locator.make_from_text(judgement.evidence_text)
    if evidence.is_empty:
        return uncertain(
            suggestion,
            reason=f"LLM 判定命中但证据回验失败（PS-11）：{evidence.note}",
            source=HitSource.LLM,
        )
    return hit(evidence, suggestion, source=HitSource.LLM, reason=judgement.reason or "LLM 语义判定命中")


# ── RL-001 预付款比例风险 ────────────────────────────────────────────────


async def rule_prepay_ratio(ctx: RuleContext, rule: Any) -> RuleOutcome:
    params = _params(rule)
    max_ratio = float(params.get("max_ratio") or ctx.settings.rule_prepay_max_ratio)
    suggestion = _suggestion(rule)
    span = ctx.clause_span("payment_clause")
    if span is None:
        return uncertain(suggestion, reason="未提取到付款条款，无法判定预付款比例（RL-001 边界）")

    for sentence, start, end in _iter_sentences(ctx.document.full_text[span[0] : span[1]], span[0]):
        if not _PREPAY_HINT_RE.search(sentence):
            continue
        match = _PERCENT_RE.search(sentence)
        if not match:
            continue
        ratio = float(match.group(1)) / 100
        evidence = ctx.locator.make(start, end)
        if ratio > max_ratio:  # RL-00-05：严格大于才命中
            return hit(
                evidence,
                suggestion,
                reason=f"预付款比例 {ratio:.0%} 超过阈值 {max_ratio:.0%}",
            )
        return miss(suggestion, reason=f"预付款比例 {ratio:.0%} 未超过阈值 {max_ratio:.0%}")
    return uncertain(suggestion, reason="付款条款中未找到预付款比例表述（RL-001 边界）")


# ── RL-002 付款周期风险 ──────────────────────────────────────────────────


async def rule_payment_period(ctx: RuleContext, rule: Any) -> RuleOutcome:
    params = _params(rule)
    max_days = int(params.get("max_days") or ctx.settings.rule_payment_max_days)
    suggestion = _suggestion(rule)
    span = ctx.clause_span("payment_clause")
    if span is None:
        return uncertain(suggestion, reason="未提取到付款条款，无法判定付款周期（RL-002 边界）")

    best: tuple[int, int, int] | None = None
    for sentence, start, end in _iter_sentences(ctx.document.full_text[span[0] : span[1]], span[0]):
        if not _PAY_HINT_RE.search(sentence):
            continue
        for match in _DURATION_RE.finditer(sentence):
            days = _to_days(match.group(1), match.group(2))
            if days and (best is None or days > best[0]):
                best = (days, start, end)
    if best is None:
        return uncertain(suggestion, reason="付款条款中未解析出付款账期天数（RL-002 边界）")

    days, start, end = best
    evidence = ctx.locator.make(start, end)
    if days > max_days:
        return hit(evidence, suggestion, reason=f"付款账期 {days} 天超过阈值 {max_days} 天")
    return miss(suggestion, reason=f"付款账期 {days} 天未超过阈值 {max_days} 天")


# ── RL-003 自动续约风险 ──────────────────────────────────────────────────


async def rule_auto_renewal(ctx: RuleContext, rule: Any) -> RuleOutcome:
    suggestion = _suggestion(rule)
    dispute_span = ctx.clause_span("dispute_clause")
    for keyword, start, end in find_all_matches(ctx.document.full_text, _keywords(rule)):
        # SPEC RL-003 的 target_section = "dispute_clause 之外的全文"
        if dispute_span and dispute_span[0] <= start < dispute_span[1]:
            continue
        return hit(
            ctx.locator.make(start, end),
            suggestion,
            reason=f"命中自动续约关键词「{keyword}」",
        )

    if _EXPLICIT_RENEWAL_RE.search(ctx.document.full_text):
        return miss(suggestion, reason="仅约定须经双方书面确认后另行签署续约协议，非默认自动续约")

    judgement = await _semantic(
        ctx,
        rule,
        section=None,
        default_question="合同是否存在无需双方再次确认即可自动续期的默认续约安排？",
    )
    if judgement.hit is True:
        return _llm_hit(ctx, rule, judgement)
    if judgement.hit is False:
        return miss(suggestion, reason=judgement.reason or "LLM 判定不存在默认自动续约")
    return uncertain(suggestion, reason="无自动续约关键词且 LLM 不可用（LM-15 降级）")


# ── RL-004 违约责任风险 ──────────────────────────────────────────────────


async def rule_breach_liability(ctx: RuleContext, rule: Any) -> RuleOutcome:
    suggestion = _suggestion(rule)
    if not ctx.ok("breach_clause"):
        return hit(NULL_EVIDENCE, suggestion, reason="合同未约定违约责任条款（RL-004①）")

    clause_text = ctx.section_text("breach")
    judgement = await _semantic(
        ctx,
        rule,
        section="breach",
        default_question="违约责任是否明显不对等（一方承担全部责任、另一方免责）？",
    )
    if judgement.hit is True:
        return _llm_hit(ctx, rule, judgement)
    if judgement.hit is False:
        return miss(suggestion, reason=judgement.reason or "LLM 判定双方责任对等")

    signals = [str(item) for item in (_params(rule).get("unequal_signals") or [])]
    found = [signal for signal in signals if signal in clause_text]
    if found:
        span = ctx.clause_span("breach_clause")
        start = ctx.document.full_text.find(found[0], span[0] if span else 0)
        return hit(
            ctx.locator.make(start, start + len(found[0])),
            suggestion,
            reason=f"LLM 不可用，但命中不对等信号「{found[0]}」（规则侧辅助判定）",
        )
    return uncertain(suggestion, reason="LLM 不可用且无不对等信号（RL-004 边界）")


# ── RL-005 管辖地风险 ────────────────────────────────────────────────────


async def rule_jurisdiction(ctx: RuleContext, rule: Any) -> RuleOutcome:
    suggestion = _suggestion(rule)
    params = _params(rule)
    adverse = [str(item) for item in (params.get("adverse_terms") or [])] or _keywords(rule)
    span = ctx.clause_span("dispute_clause")

    search_from, search_to = (span if span else (0, len(ctx.document.full_text)))
    if span is None:
        logger.warning("R005 未定位到争议解决条款，回退全文匹配（RL-00-01）")

    for term in adverse:
        index = ctx.document.full_text.find(term, search_from, search_to)
        if index >= 0:
            return hit(
                ctx.locator.make(index, index + len(term)),
                suggestion,
                reason=f"争议解决条款出现不利管辖地表述「{term}」",
            )
    return miss(suggestion, reason="未发现不利管辖地表述")


# ── RL-006 / RL-007 / RL-008 缺失型（presence）──────────────────────────


async def _presence_fields(ctx: RuleContext, rule: Any, labels: dict[str, str]) -> RuleOutcome:
    suggestion = _suggestion(rule)
    fields = [str(item) for item in (_params(rule).get("fields") or [])]
    missing = [field for field in fields if not ctx.ok(field)]
    if not missing:
        return miss(suggestion, reason="所需字段均已成功提取")

    present = [field for field in fields if ctx.ok(field)]
    evidence: Evidence = NULL_EVIDENCE
    if present:  # RL-006：若有部分信息，取其原文
        evidence = ctx.locator.make_from_text(ctx.source_text(present[0]))
    names = "、".join(labels.get(field, field) for field in missing)
    return hit(evidence, suggestion, reason=f"缺失：{names}")


async def rule_subject_missing(ctx: RuleContext, rule: Any) -> RuleOutcome:
    return await _presence_fields(ctx, rule, {"party_a": "签约主体", "party_b": "对方名称"})


async def rule_amount_missing(ctx: RuleContext, rule: Any) -> RuleOutcome:
    return await _presence_fields(ctx, rule, {"contract_amount": "合同金额", "currency": "币种"})


async def rule_confidentiality_missing(ctx: RuleContext, rule: Any) -> RuleOutcome:
    return await _presence_fields(ctx, rule, {"confidentiality_clause": "保密条款"})


# ── RL-009 数据处理风险 ──────────────────────────────────────────────────


async def rule_data_processing(ctx: RuleContext, rule: Any) -> RuleOutcome:
    suggestion = _suggestion(rule)
    matches = find_all_matches(ctx.document.full_text, _keywords(rule))
    if matches:
        clause_text = ctx.source_text("data_clause") or ctx.document.full_text
        required = _params(rule).get("required_elements") or {}
        missing = [
            name
            for name, terms in required.items()
            if not any(str(term) in clause_text for term in (terms or []))
        ]
        if missing:
            _, start, end = matches[0]
            return hit(
                ctx.locator.make(start, end),
                suggestion,
                reason=f"涉及数据处理但未约定：{'、'.join(missing)}",
            )
        return miss(suggestion, reason="数据处理的目的、范围、安全措施与删除义务均已约定")

    judgement = await _semantic(
        ctx,
        rule,
        section="data",
        default_question="合同是否实际涉及个人信息的采集、共享、处理或存储？",
    )
    if judgement.hit is True:
        return _llm_hit(ctx, rule, judgement)
    if judgement.hit is False:
        return miss(suggestion, reason=judgement.reason or "LLM 判定不涉及数据处理")
    return uncertain(suggestion, reason="无数据关键词且 LLM 不可用（LM-15 降级）")


# ── RL-010 知识产权风险 ──────────────────────────────────────────────────


async def rule_intellectual_property(ctx: RuleContext, rule: Any) -> RuleOutcome:
    suggestion = _suggestion(rule)
    if not ctx.ok("ip_clause"):
        return hit(NULL_EVIDENCE, suggestion, reason="合同未约定知识产权条款（RL-010①）")

    clause_text = ctx.section_text("ip")
    judgement = await _semantic(
        ctx,
        rule,
        section="ip",
        default_question="知识产权归属是否不明确（存在风险）？已明确写清归属方的应判定为 false。",
    )
    if judgement.hit is True:
        return _llm_hit(ctx, rule, judgement)
    if judgement.hit is False:
        return miss(suggestion, reason=judgement.reason or "LLM 判定知识产权归属明确")

    clear_signals = [str(item) for item in (_params(rule).get("clear_signals") or [])]
    if any(signal in clause_text for signal in clear_signals):
        return miss(suggestion, reason="条款已明确约定知识产权归属（RL-010 边界）")
    return uncertain(suggestion, reason="LLM 不可用且未见明确归属表述")


# ── RL-011 验收标准缺失 ──────────────────────────────────────────────────


async def rule_acceptance_missing(ctx: RuleContext, rule: Any) -> RuleOutcome:
    suggestion = _suggestion(rule)
    if not ctx.ok("acceptance_clause"):
        return hit(NULL_EVIDENCE, suggestion, reason="合同未约定验收条款（RL-011①）")

    clause_text = ctx.section_text("acceptance")
    required = _params(rule).get("required_elements") or {}
    missing = [
        name
        for name, patterns in required.items()
        if not any(re.search(str(pattern), clause_text) for pattern in (patterns or []))
    ]
    if not missing:
        return miss(suggestion, reason="验收时间、验收方式、验收标准三要素齐备")

    span = ctx.clause_span("acceptance_clause")
    evidence = ctx.locator.make(span[0], span[0] + 1) if span else NULL_EVIDENCE
    labels = "、".join(_ACCEPTANCE_LABELS.get(name, name) for name in missing)
    return hit(
        evidence,
        f"建议补充{labels}",  # RL-011：建议中必须指明缺失要素
        reason=f"验收条款缺少：{labels}",
    )


# ── 数据驱动的通用匹配器（库里新增规则但无专用处理器时的兜底）──────────────


async def generic_keyword(ctx: RuleContext, rule: Any) -> RuleOutcome:
    keywords = _keywords(rule)
    matches = find_all_matches(ctx.document.full_text, keywords)
    if matches:
        keyword, start, end = matches[0]
        return hit(ctx.locator.make(start, end), _suggestion(rule), reason=f"命中关键词「{keyword}」")
    return miss(_suggestion(rule), reason="未命中任何关键词")


async def generic_regex(ctx: RuleContext, rule: Any) -> RuleOutcome:
    pattern = str(getattr(rule, "match_text", "") or "")
    if not pattern:
        return uncertain(_suggestion(rule), reason="规则未配置 match_text")
    span = ctx.locator.find_span(pattern)
    if span is None:
        return miss(_suggestion(rule), reason="正则未命中")
    return hit(ctx.locator.make(*span), _suggestion(rule), reason=f"正则命中 /{pattern}/")


async def generic_presence(ctx: RuleContext, rule: Any) -> RuleOutcome:
    return await _presence_fields(ctx, rule, _params(rule).get("labels") or {})


async def generic_threshold(ctx: RuleContext, rule: Any) -> RuleOutcome:
    return uncertain(_suggestion(rule), reason="阈值类规则需要专用处理器，当前规则未实现")


#: rule_code → 专用处理器
HANDLERS: dict[str, Any] = {
    "R001": rule_prepay_ratio,
    "R002": rule_payment_period,
    "R003": rule_auto_renewal,
    "R004": rule_breach_liability,
    "R005": rule_jurisdiction,
    "R006": rule_subject_missing,
    "R007": rule_amount_missing,
    "R008": rule_confidentiality_missing,
    "R009": rule_data_processing,
    "R010": rule_intellectual_property,
    "R011": rule_acceptance_missing,
}

#: match_mode → 通用处理器（FR-RULE-02 的 5 种模式）
MODE_HANDLERS: dict[str, Any] = {
    "keyword": generic_keyword,
    "regex": generic_regex,
    "presence": generic_presence,
    "threshold": generic_threshold,
    "llm_semantic": generic_threshold,
}


def resolve_handler(rule: Any) -> Any:
    """优先用 rule_code 的专用实现，其次按 ``match_mode`` 走通用匹配器。"""
    code = str(getattr(rule, "rule_code", "") or "")
    if code in HANDLERS:
        return HANDLERS[code]
    mode = str(getattr(rule, "match_mode", "") or "")
    return MODE_HANDLERS.get(mode, generic_threshold)
