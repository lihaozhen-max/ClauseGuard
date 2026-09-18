"""内部接口鉴权（SPEC FR-SYS-03 / IF-10…IF-21）。

调用端 → 工具服务的所有请求**必须**带 ``X-API-Key: {INTERNAL_API_KEY}``，
缺失或不匹配 → 401 ``UNAUTHORIZED``。
"""

from __future__ import annotations

import secrets as _secrets
from typing import Annotated

from fastapi import Header

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode


def verify_api_key(x_api_key: str | None) -> None:
    """校验一个候选 Key；不通过则抛 ``AppError(UNAUTHORIZED)``。

    独立成函数便于单元测试直接调用，不必构造完整请求。
    """
    expected = get_settings().internal_api_key
    if not x_api_key or not _secrets.compare_digest(x_api_key, expected):
        raise AppError(ErrorCode.UNAUTHORIZED, "缺少或错误的 X-API-Key")


async def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """FastAPI 依赖：挂在需要鉴权的路由上。"""
    verify_api_key(x_api_key)
