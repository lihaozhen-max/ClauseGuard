"""审批接入的出入参模型（SPEC IF-01 / IF-02 与 IF-10…IF-12）。

工具接口返回这些模型，``model_dump(mode="json")`` 即为 SPEC 约定的 JSON 形状
（时间统一 ``...Z``，见 ``schemas/common.py``）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ApiDateTime


class ApprovalListItem(BaseModel):
    """IF-01 的单项：审批系统字段 + 本系统任务字段 + 去重标记。"""

    instance_id: str
    approval_code: str
    approval_title: str
    applicant_name: str
    apply_time: ApiDateTime | None = None
    attachment_count: int = 0
    current_status: str | None = None
    # 本系统侧
    task_id: int
    task_status: str
    dedup: str = Field(description="created / updated（SPEC IF-01）")


class PullResult(BaseModel):
    """IF-01 的整体返回。"""

    items: list[ApprovalListItem] = Field(default_factory=list)
    created_count: int = 0
    updated_count: int = 0


class AttachmentInfo(BaseModel):
    """审批系统侧的附件元数据（IF-02）。"""

    attachment_id: str
    file_name: str
    file_type: str
    file_size: int | None = None


class ApprovalDetail(BaseModel):
    """IF-02 返回：审批详情 + 本系统任务映射。"""

    instance_id: str
    approval_code: str
    approval_title: str
    applicant_name: str
    apply_time: ApiDateTime | None = None
    current_status: str | None = None
    contract_type: str | None = None
    form_data: dict[str, Any] = Field(default_factory=dict)
    attachments: list[AttachmentInfo] = Field(default_factory=list)
    task_id: int | None = None
    task_status: str | None = None


class PullRequest(BaseModel):
    """IF-10 请求体。"""

    limit: int = Field(default=20, ge=1, le=200)


class LocalAttachment(BaseModel):
    """本系统已下载/登记的附件记录（DT-02 / IF-12）。

    对外字段名用 SPEC 的 ``attachment_id``，内部对应 ``approval_attachments.attachment_code``。
    """

    attachment_id: str
    file_name: str
    file_type: str
    file_size: int | None = None
    file_checksum: str | None = None
    download_status: str


class TaskDetail(BaseModel):
    """IF-12 返回：任务详情（含附件），全部来自本系统数据库。"""

    task_id: int
    instance_id: str
    approval_code: str
    approval_title: str
    applicant_name: str
    apply_time: ApiDateTime | None = None
    attachment_count: int = 0
    current_status: str | None = None
    contract_type: str | None = None
    form_data: dict[str, Any] = Field(default_factory=dict)
    task_status: str
    write_status: str
    blocked_stage: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_count: int = 0
    created_at: ApiDateTime
    updated_at: ApiDateTime
    attachments: list[LocalAttachment] = Field(default_factory=list)


class TaskListItem(BaseModel):
    """IF-11 的单项：本系统任务列表行（对应 FR-UI-01 的展示字段）。"""

    task_id: int
    instance_id: str
    approval_code: str
    approval_title: str
    applicant_name: str
    apply_time: ApiDateTime | None = None
    attachment_count: int = 0
    current_status: str | None = None
    task_status: str
    write_status: str


class TaskListResponse(BaseModel):
    """IF-11 返回：分页的任务列表。"""

    items: list[TaskListItem]
    total: int
    page: int
    size: int
