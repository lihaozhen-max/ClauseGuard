"""数据库会话管理（OPEN-01 决策落点：SQLAlchemy 2.0 异步 + ``asyncmy``）。

约束（工程规范 §8）：全项目**只**在这里创建 engine 与 session 工厂，
业务模块一律通过 ``get_session`` 依赖或 ``session_scope`` 获取会话，禁止各自建连接。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """进程内单例 engine。"""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：``session: AsyncSession = Depends(get_session)``。"""
    async with get_session_factory()() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """脚本/工具接口中手动管理事务的入口。"""
    async with get_session_factory()() as session:
        yield session


async def dispose_engine() -> None:
    """释放连接池（应用关闭 / 测试收尾）。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def reset_engine_state() -> None:
    """**丢弃**缓存的 engine 与 session 工厂，但**不** await 释放。

    仅供测试使用：同一个进程里可能存在多个事件循环（如 FastAPI ``TestClient`` 的 portal
    循环与 ``asyncio.run`` 的临时循环）。跨循环复用同一个 engine 会抛
    ``Future attached to a different loop``，而在这里 await 另一个循环的 engine.dispose()
    同样会踩到该问题。丢弃旧引用、让下一次取用时按当前循环新建，是最安全的做法。
    """
    global _engine, _session_factory
    _engine = None
    _session_factory = None


async def check_connection() -> tuple[bool, str]:
    """健康检查：返回 ``(是否连通, 描述)``，不抛异常。"""
    try:
        async with get_engine().connect() as conn:
            version = (await conn.execute(text("SELECT VERSION()"))).scalar_one()
            charset = (await conn.execute(text("SELECT @@character_set_database"))).scalar_one()
        return True, f"MySQL {version} / charset={charset}"
    except Exception as exc:  # noqa: BLE001 - 健康检查必须吞掉一切异常，避免影响 /health
        return False, f"{type(exc).__name__}: {exc}"
