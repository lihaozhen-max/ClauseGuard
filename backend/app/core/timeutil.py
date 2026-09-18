"""UTC 时间处理（SPEC §2.3）。

约定：
- **数据库**：``DATETIME`` 存 **naive UTC**（容器以 ``--default-time-zone=+00:00`` 启动）。
- **接口**：一律 ISO 8601 UTC 字符串 ``YYYY-MM-DDTHH:MM:SSZ``（即 tz-aware UTC）。

两个方向各一个函数，禁止在各模块就地 ``replace(tzinfo=...)``。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def parse_api_time(raw: str | None) -> datetime | None:
    """解析接口时间字符串 → **naive UTC**（可直接入库）。

    容忍 ``Z`` 与 ``+08:00`` 形式；带时区的值会先归一到 UTC。
    """
    if not raw:
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def to_api_time(value: datetime | None) -> datetime | None:
    """把库里的 naive UTC 标记为 tz-aware UTC，供接口序列化为 ``...Z``。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_now() -> datetime:
    """当前 UTC 的 naive 表示（与数据库时间列口径一致）。"""
    return datetime.now(UTC).replace(tzinfo=None)


def format_duration(seconds: float) -> str:
    """日志可读的耗时格式（FR-LOG-01 记录处理过程）。"""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    return str(timedelta(seconds=round(seconds)))
