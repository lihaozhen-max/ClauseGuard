"""文本处理小工具（被解析与日志模块共用）。"""

from __future__ import annotations

DEFAULT_LOG_TEXT_LIMIT = 500
"""FR-LOG-04：日志中的合同原文应当截断至 ≤ 500 字符。"""

DEFAULT_SOURCE_TEXT_LIMIT = 2000
"""FR-PARSE-06：条款 source_text 可截断至 ≤ 2000 字符（M2 使用）。"""


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """超长则截断并加省略号，未超长原样返回。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(limit - len(suffix), 0)] + suffix
