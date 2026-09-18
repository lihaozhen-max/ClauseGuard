"""日志基础设施（SPEC FR-LOG-03 / NF-06）。

- 控制台日志格式统一，级别来自 ``APP_LOG_LEVEL``（CF-03）。
- 内置**脱敏过滤器**：数据库密码、审批系统 Key、内部 API Key、LLM Key 一律不得出现在日志中。
- ``task_logs`` 表（DT-08）由 ``modules/logging`` 负责落库，本模块只管进程日志。
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Iterable

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

#: 兜底脱敏规则：即便某个密钥未登记，也按形态抹除
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)\b(password|passwd|pwd)\s*[=:]\s*\S+"), r"\1=***"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret)\s*[=:]\s*\S+"), r"\1=***"),
    (re.compile(r"(?i)mysql\+\w+://[^:@/\s]+:[^@/\s]+@"), "mysql://***:***@"),
)


class RedactingFilter(logging.Filter):
    """把已登记的敏感值替换为 ``***``，再套用形态规则。"""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def add_secrets(self, secrets: Iterable[str]) -> None:
        merged = set(self._secrets) | {s for s in secrets if s}
        self._secrets = sorted(merged, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        # 先渲染消息（含 %s 参数），确保参数里的密钥同样被抹除
        record.msg = self.redact(record.getMessage())
        record.args = ()
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        return True


#: 进程级共享过滤器——``setup_logging`` 之外也可用于测试与临时 logger
redactor = RedactingFilter()


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    """初始化根日志。重复调用是安全的（幂等），便于测试与热重载。"""
    redactor.add_secrets(secrets)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    handler.addFilter(redactor)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn 自带 handler 会绕过根 logger 的过滤器，这里统一交给根 logger
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
