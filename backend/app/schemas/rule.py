"""规则维护的出入参模型（SPEC IF-21 / FR-UI-08）。

字段与 DT-05 ``review_rules`` 一一对应（设计 §7.1）：
``rule_code`` + ``rule_name`` + ``risk_level`` + ``rule_status`` + ``match_mode``
+ ``match_text`` + ``match_params_json`` + ``suggestion_text`` + ``target_section``。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.enums import MatchMode, RiskLevel, RuleStatus
from app.schemas.common import ApiDateTime

#: ``rule_code`` 形如 ``R001``（与 ``database/seed_rules.sql`` 的种子保持同一命名）
RULE_CODE_PATTERN = re.compile(r"^R\d{3}$")


class RuleModel(BaseModel):
    """一条规则（IF-21 的出参）。"""

    rule_id: int
    rule_code: str
    rule_name: str
    risk_level: str = Field(description="low / medium / high")
    rule_status: str = Field(description="enabled / disabled")
    match_mode: str = Field(description="regex / keyword / threshold / presence / llm_semantic")
    match_text: str | None = None
    match_params_json: dict[str, Any] | None = None
    suggestion_text: str = ""
    target_section: str | None = None
    updated_at: ApiDateTime


class RuleListResponse(BaseModel):
    """IF-21 ``GET`` 的返回：全量规则（含停用）+ 启停统计。"""

    items: list[RuleModel] = Field(default_factory=list)
    total: int = 0
    enabled_count: int = 0
    disabled_count: int = 0


class RuleCreateRequest(BaseModel):
    """IF-21 ``POST`` 请求体（新增规则，FR-RULE-08）。"""

    rule_code: str = Field(description="形如 R012，全局唯一")
    rule_name: str = Field(min_length=1, max_length=128)
    risk_level: RiskLevel
    rule_status: RuleStatus = RuleStatus.ENABLED
    match_mode: MatchMode
    match_text: str | None = None
    match_params_json: dict[str, Any] | None = None
    suggestion_text: str = Field(min_length=1)
    target_section: str | None = Field(default=None, max_length=32)

    @field_validator("rule_code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        value = value.strip().upper()
        if not RULE_CODE_PATTERN.match(value):
            raise ValueError("rule_code 必须形如 R001（R + 3 位数字）")
        return value


class RuleUpdateRequest(BaseModel):
    """IF-21 ``PUT`` 请求体：按 ``rule_id`` 定位，只改传入的字段。

    ``rule_code`` **不可修改**——它是规则的对外标识，``seed_rules.sql`` 与
    回归用例都按它比对（设计 §7.1）；``rule_id`` 才是主键。
    """

    rule_id: int = Field(ge=1)
    rule_name: str | None = Field(default=None, min_length=1, max_length=128)
    risk_level: RiskLevel | None = None
    rule_status: RuleStatus | None = None
    match_mode: MatchMode | None = None
    match_text: str | None = None
    match_params_json: dict[str, Any] | None = None
    suggestion_text: str | None = Field(default=None, min_length=1)
    target_section: str | None = Field(default=None, max_length=32)

    def changes(self) -> dict[str, Any]:
        """仅返回显式传入的字段（未传 = 不改），供服务层做增量更新。"""
        return self.model_dump(exclude_unset=True, exclude={"rule_id"})
