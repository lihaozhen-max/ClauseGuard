"""接口层公共类型（SPEC §2.3：接口时间一律 ISO 8601 UTC，形如 ``...Z``）。

数据库里存的是 **naive UTC**，若直接序列化会得到 ``2026-09-10T02:30:00``（缺 ``Z``），
与规范不符。``ApiDateTime`` 把"入库口径 → 接口口径"的转换收敛到一处，
任何响应模型用它即可，无需在每个构造函数里手工转换。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BeforeValidator

from app.core.timeutil import to_api_time


def _to_utc(value: Any) -> Any:
    # 只处理已经是 datetime 的情况（naive → UTC aware）；字符串留给 pydantic 自行解析
    return to_api_time(value) if isinstance(value, datetime) else value


ApiDateTime = Annotated[datetime, BeforeValidator(_to_utc)]
