"""错误码与统一错误响应（SPEC §5.3 / §5.4）。

§5.4 的 12 个错误码是**规范**取值，本模块是它们在代码中的唯一来源；
每个码同时携带"HTTP 状态 / 任务状态影响 / 回写状态影响"，与 §5.4 表格逐行对应，
避免各个模块各自解释语义。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """§5.4 错误码总表（规范，禁止改名）。"""

    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
    APPROVAL_API_ERROR = "APPROVAL_API_ERROR"
    CONTRACT_ATTACHMENT_MISSING = "CONTRACT_ATTACHMENT_MISSING"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    EMPTY_CONTRACT_CONTENT = "EMPTY_CONTRACT_CONTENT"
    OCR_FAILED = "OCR_FAILED"
    PARSE_FAILED = "PARSE_FAILED"
    PARSE_REQUIRED = "PARSE_REQUIRED"
    RULE_EXECUTION_FAILED = "RULE_EXECUTION_FAILED"
    LLM_FAILED = "LLM_FAILED"
    COMMENT_WRITE_FAILED = "COMMENT_WRITE_FAILED"
    UNAUTHORIZED = "UNAUTHORIZED"

    # 框架级补充码（SPEC §5.4 未列，用于满足 NF-09"错误必须返回统一结构"）
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorSpec:
    """一个错误码的完整语义（对应 §5.4 表格的一行）。"""

    __slots__ = ("http_status", "task_status_effect", "write_status_effect", "blocked_stage")

    def __init__(
        self,
        http_status: int,
        task_status_effect: str,
        write_status_effect: str,
        blocked_stage: str | None = None,
    ) -> None:
        self.http_status = http_status
        self.task_status_effect = task_status_effect
        self.write_status_effect = write_status_effect
        self.blocked_stage = blocked_stage


_UNCHANGED = "不变"

#: 错误码 → §5.4 语义。``blocked_stage`` 非空表示该错误必须把任务置为 blocked。
ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.APPROVAL_NOT_FOUND: ErrorSpec(404, _UNCHANGED, _UNCHANGED),
    ErrorCode.APPROVAL_API_ERROR: ErrorSpec(502, "blocked（当前阶段）", _UNCHANGED),
    ErrorCode.CONTRACT_ATTACHMENT_MISSING: ErrorSpec(
        404, "blocked / parsing", _UNCHANGED, blocked_stage="parsing"
    ),
    ErrorCode.DOWNLOAD_FAILED: ErrorSpec(502, "blocked / parsing", _UNCHANGED, blocked_stage="parsing"),
    ErrorCode.EMPTY_CONTRACT_CONTENT: ErrorSpec(
        422, "blocked / parsing", _UNCHANGED, blocked_stage="parsing"
    ),
    ErrorCode.OCR_FAILED: ErrorSpec(500, "blocked / parsing", _UNCHANGED, blocked_stage="parsing"),
    ErrorCode.PARSE_FAILED: ErrorSpec(500, "blocked / parsing", _UNCHANGED, blocked_stage="parsing"),
    ErrorCode.PARSE_REQUIRED: ErrorSpec(409, _UNCHANGED, _UNCHANGED),
    ErrorCode.RULE_EXECUTION_FAILED: ErrorSpec(
        500, "blocked / reviewing", _UNCHANGED, blocked_stage="reviewing"
    ),
    # LM-14：LLM 失败禁止把任务置为 blocked，只做降级
    ErrorCode.LLM_FAILED: ErrorSpec(502, _UNCHANGED, _UNCHANGED),
    # FR-COM-04 / ST-01-04：回写失败不得删除结果，任务保持 done
    ErrorCode.COMMENT_WRITE_FAILED: ErrorSpec(502, "保持 done", "failed"),
    ErrorCode.UNAUTHORIZED: ErrorSpec(401, _UNCHANGED, _UNCHANGED),
    ErrorCode.VALIDATION_ERROR: ErrorSpec(422, _UNCHANGED, _UNCHANGED),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, _UNCHANGED, _UNCHANGED),
}


class AppError(Exception):
    """业务异常。携带 SPEC §5.3 统一错误体所需的全部字段。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        task_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.task_id = task_id
        self.detail = detail or {}

    @property
    def spec(self) -> ErrorSpec:
        return ERROR_SPECS[self.code]

    @property
    def http_status(self) -> int:
        return self.spec.http_status

    @property
    def blocked_stage(self) -> str | None:
        """需要置 blocked 时对应的阶段；None 表示该错误不改变任务状态。"""
        return self.spec.blocked_stage

    def to_body(self) -> dict[str, Any]:
        """SPEC §5.3 统一错误结构。

        ``message``/``detail`` 中禁止出现堆栈（NF-09）。
        """
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "task_id": self.task_id,
                "detail": self.detail,
            }
        }
