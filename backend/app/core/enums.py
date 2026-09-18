"""SPEC §2.2 枚举取值的唯一来源（**禁止扩展，禁止大小写变体**）。

这些枚举同时是数据库列取值、接口 JSON 取值与日志取值的约束基准；
任何模块需要判断状态/等级时必须引用本模块，不得就地写字符串字面量。
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    """DT-01 ``task_status`` / ST-01 任务状态机。"""

    PENDING = "pending"
    PARSING = "parsing"
    REVIEWING = "reviewing"
    BLOCKED = "blocked"
    DONE = "done"


class WriteStatus(StrEnum):
    """DT-01 ``write_status`` / ST-02 回写状态机。"""

    NOT_WRITTEN = "not_written"
    WRITING = "writing"
    SUCCESS = "success"
    FAILED = "failed"


class RiskLevel(StrEnum):
    """风险等级（DT-04/DT-05/DT-06）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RuleStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class MatchMode(StrEnum):
    """FR-RULE-02 的 5 种匹配模式。"""

    REGEX = "regex"
    KEYWORD = "keyword"
    THRESHOLD = "threshold"
    PRESENCE = "presence"
    LLM_SEMANTIC = "llm_semantic"


class ExtractStatus(StrEnum):
    """FR-PARSE-05 字段提取状态。"""

    SUCCESS = "success"
    MISSING = "missing"
    FAILED = "failed"


class ParseStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ParseMode(StrEnum):
    """决定 ``position`` 格式（SPEC §2.6）。"""

    TEXT = "text"
    OCR = "ocr"


class HitStatus(StrEnum):
    HIT = "hit"
    MISS = "miss"
    UNCERTAIN = "uncertain"


class HitSource(StrEnum):
    """命中来源，保证可审计（FR-RULE-03）。"""

    RULE = "rule"
    LLM = "llm"


class DownloadStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class LogLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogType(StrEnum):
    """FR-LOG-01 的 8 类核心操作 + 人工重试。"""

    PULL = "pull"
    DOWNLOAD = "download"
    PARSE = "parse"
    OCR = "ocr"
    EXTRACT = "extract"
    RULE = "rule"
    SAVE = "save"
    WRITE_COMMENT = "write_comment"
    RETRY = "retry"


#: AC18 要求这 8 类 log_type 在闭环链路中各至少出现一条（不含 retry）
CORE_LOG_TYPES: tuple[LogType, ...] = (
    LogType.PULL,
    LogType.DOWNLOAD,
    LogType.PARSE,
    LogType.OCR,
    LogType.EXTRACT,
    LogType.RULE,
    LogType.SAVE,
    LogType.WRITE_COMMENT,
)

#: SPEC §2.2：整体风险等级面向用户时映射为中文；接口 JSON 一律英文枚举
RISK_LEVEL_ZH: dict[str, str] = {
    RiskLevel.LOW.value: "低",
    RiskLevel.MEDIUM.value: "中",
    RiskLevel.HIGH.value: "高",
}


def enum_values(enum_cls: type[StrEnum]) -> frozenset[str]:
    """枚举的合法取值集合，供校验用。"""
    return frozenset(member.value for member in enum_cls)
