"""规则执行上下文与结果结构。

SPEC RL-00-06：**所有规则必须可独立测试，输入为 ``full_text`` + ``basic_info`` + ``clause_info``，
输出为 ``hit_status`` + 证据 + 位置**——本模块把这三样输入打包成 :class:`RuleContext`，
让每条规则都能脱离数据库、脱离 HTTP 单独调用（测试即按此构造）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.core.enums import ExtractStatus, HitSource, HitStatus
from app.llm.base import LLMClient
from app.modules.parser.models import ParsedDocument
from app.modules.rules.evidence import Evidence, EvidenceLocator

#: ``target_section`` 取值 → 对应条款字段（RL-00-01 限定匹配部位）
SECTION_TO_FIELD = {
    "payment": "payment_clause",
    "delivery": "delivery_clause",
    "acceptance": "acceptance_clause",
    "breach": "breach_clause",
    "confidentiality": "confidentiality_clause",
    "data": "data_clause",
    "ip": "ip_clause",
    "dispute": "dispute_clause",
}


@dataclass
class RuleOutcome:
    """一条规则的判定结果（RL-00-06 的输出）。"""

    hit_status: HitStatus
    hit_source: HitSource = HitSource.RULE
    evidence: Evidence = field(default_factory=lambda: Evidence(text=None, position=None))
    suggestion: str = ""
    reason: str = ""

    @property
    def is_hit(self) -> bool:
        return self.hit_status is HitStatus.HIT

    @property
    def is_uncertain(self) -> bool:
        return self.hit_status is HitStatus.UNCERTAIN


def hit(
    evidence: Evidence,
    suggestion: str,
    *,
    source: HitSource = HitSource.RULE,
    reason: str = "",
) -> RuleOutcome:
    return RuleOutcome(HitStatus.HIT, source, evidence, suggestion, reason)


def miss(suggestion: str = "", *, reason: str = "") -> RuleOutcome:
    return RuleOutcome(HitStatus.MISS, HitSource.RULE, Evidence(text=None, position=None), suggestion, reason)


def uncertain(suggestion: str = "", *, reason: str = "", source: HitSource = HitSource.RULE) -> RuleOutcome:
    return RuleOutcome(HitStatus.UNCERTAIN, source, Evidence(text=None, position=None), suggestion, reason)


@dataclass
class RuleContext:
    """规则执行所需的全部输入。"""

    document: ParsedDocument
    locator: EvidenceLocator
    basic_info: dict[str, dict[str, Any]]
    clause_info: dict[str, dict[str, Any]]
    settings: Settings
    llm: LLMClient

    @classmethod
    def build(
        cls,
        document: ParsedDocument,
        basic_info: list[dict[str, Any]],
        clause_info: list[dict[str, Any]],
        settings: Settings,
        llm: LLMClient,
    ) -> RuleContext:
        return cls(
            document=document,
            locator=EvidenceLocator(document),
            basic_info={str(item.get("field_name")): item for item in basic_info},
            clause_info={str(item.get("field_name")): item for item in clause_info},
            settings=settings,
            llm=llm,
        )

    # ── 字段访问 ──────────────────────────────────────────────────────────

    def record(self, field_name: str) -> dict[str, Any]:
        return self.basic_info.get(field_name) or self.clause_info.get(field_name) or {}

    def status(self, field_name: str) -> str:
        return str(self.record(field_name).get("extract_status") or ExtractStatus.MISSING.value)

    def ok(self, field_name: str) -> bool:
        return self.status(field_name) == ExtractStatus.SUCCESS.value

    def value(self, field_name: str) -> str | None:
        raw = self.record(field_name).get("field_value")
        return str(raw) if raw is not None else None

    def source_text(self, field_name: str) -> str:
        return str(self.record(field_name).get("source_text") or "")

    # ── 部位定位（RL-00-01）───────────────────────────────────────────────

    def clause_span(self, field_name: str) -> tuple[int, int] | None:
        """条款字段在 ``full_text`` 中的字符区间。"""
        text = self.source_text(field_name)
        if not text:
            return None
        start = self.document.full_text.find(text)
        if start < 0:
            return None
        return start, start + len(text)

    def section_span(self, section: str | None) -> tuple[int, int] | None:
        """``target_section`` → 部位区间；无法定位时返回 None（调用方回退全文并记 warning）。"""
        if not section:
            return None
        field_name = SECTION_TO_FIELD.get(section)
        return self.clause_span(field_name) if field_name else None

    def section_text(self, section: str | None) -> str:
        """部位文本；部位缺失时回退为空串（由调用方决定回退策略）。"""
        span = self.section_span(section)
        if span is None:
            return ""
        return self.document.full_text[span[0] : span[1]]

    def semantic_context(self, section: str | None = None, limit: int = 3000) -> str:
        """给 LLM 的上下文：优先给限定部位，否则给全文（截断）。

        只把相关部位送进模型，既省 token 也降低"看着别处下结论"的概率。
        """
        text = self.section_text(section) if section else ""
        if not text:
            text = self.document.full_text
        return text[:limit]
