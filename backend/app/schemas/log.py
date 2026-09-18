"""任务日志与人工重试模型（SPEC IF-19 / IF-20）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import ApiDateTime


class TaskLogModel(BaseModel):
    """DT-08 的一行（IF-19）。"""

    id: int
    task_id: int
    log_level: str = Field(description="info / warning / error")
    log_type: str = Field(description="pull/download/parse/ocr/extract/rule/save/write_comment/retry")
    log_content: str = Field(description="已脱敏、已截断（≤500 字符）")
    created_at: ApiDateTime


class TaskLogList(BaseModel):
    items: list[TaskLogModel] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    size: int = 50
    #: 该任务出现过的全部 log_type（AC18 直接看这里即可判断 8 类是否齐备）
    log_types: list[str] = Field(default_factory=list)


class RetryResponse(BaseModel):
    """IF-20 的返回结构。"""

    task_id: int
    resumed_stage: str = Field(description="重入阶段：parsing / reviewing")
    task_status: str
    retry_count: int
    review_id: int | None = None
    overall_risk_level: str | None = None
    comment_text: str = ""
    blocked_stage: str | None = None
    error_code: str | None = None
