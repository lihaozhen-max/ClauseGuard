"""规则审查结果模型（SPEC IF-05 / IF-15 / IF-16）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RuleHitModel(BaseModel):
    """单条规则判定结果（字段名遵循 SPEC §5.1 IF-05 示例）。"""

    rule_code: str
    rule_name: str
    risk_level: str = Field(description="命中时快照（FR-RULE-09）")
    hit_status: str = Field(description="hit / miss / uncertain")
    hit_source: str = Field(description="rule / llm")
    evidence_text: str | None = Field(default=None, description="合同原文连续子串；缺失型命中为 null")
    evidence_position: str | None = Field(default=None, description="§2.6 格式")
    suggestion: str = ""
    reason: str = ""


class RuleRunResponse(BaseModel):
    """IF-05 / IF-15 / IF-16 的返回结构。"""

    case_id: int
    overall_risk_level: str = Field(description="RL-AGG 结果：low / medium / high")
    hit_count: int = 0
    uncertain_count: int = 0
    rule_hits: list[RuleHitModel] = Field(default_factory=list)
    summary_text: str = ""
    focus_points: list[str] = Field(default_factory=list)
    evaluated_rules: int = 0
    warnings: list[str] = Field(default_factory=list)
