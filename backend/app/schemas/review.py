"""审查结果与评论回写模型（SPEC IF-05 / IF-06 / IF-07 / IF-15…IF-18）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import ApiDateTime


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
    """IF-05 的返回结构（规则判定 + 汇总）。"""

    case_id: int
    overall_risk_level: str = Field(description="RL-AGG 结果：low / medium / high")
    hit_count: int = 0
    uncertain_count: int = 0
    rule_hits: list[RuleHitModel] = Field(default_factory=list)
    summary_text: str = ""
    focus_points: list[str] = Field(default_factory=list)
    evaluated_rules: int = 0
    warnings: list[str] = Field(default_factory=list)


class ReviewPipelineResponse(BaseModel):
    """IF-15 / IF-16 的返回结构：规则判定 + 汇总 + 评论正文（IF-06 的产物）。"""

    case_id: int
    review_id: int | None = Field(default=None, description="review_results.id")
    overall_risk_level: str
    hit_count: int = 0
    uncertain_count: int = 0
    evaluated_rules: int = 0
    rule_hits: list[RuleHitModel] = Field(default_factory=list)
    summary_text: str = ""
    focus_points: list[str] = Field(default_factory=list)
    comment_text: str = Field(default="", description="§4.6.1 模板产物，即将写入评论区")
    summary_degraded: bool = Field(default=False, description="摘要/关注点是否走了 §10.4 模板降级")
    task_status: str | None = None
    write_status: str | None = None
    warnings: list[str] = Field(default_factory=list)


class CommentWriteModel(BaseModel):
    """IF-07 的返回结构。"""

    task_id: int
    review_id: int
    write_status: str = Field(description="writing / success / failed")
    remark_id: str | None = None
    write_response_text: str | None = None
    duplicate: bool = Field(default=False, description="已成功回写过则为 true（FR-COM-06）")


class CommentLogModel(BaseModel):
    """DT-07 的一行（IF-18）。"""

    id: int
    task_id: int
    write_status: str
    write_response_text: str | None = None
    remark_id: str | None = None
    idempotency_key: str
    created_at: ApiDateTime


class CommentLogList(BaseModel):
    items: list[CommentLogModel] = Field(default_factory=list)
    total: int = 0
