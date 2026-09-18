"""LLM 客户端工厂（设计 §9.4 可插拔）。

业务层只依赖 :class:`~app.llm.base.LLMClient` 抽象；此处按配置决定用真实端点还是降级实现。
任何初始化异常都不向外抛——LLM 是**增强项**，不是单点依赖（LM-14/LM-18）。
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.llm.base import LLMClient
from app.llm.null import NullLLMClient

logger = get_logger(__name__)


def build_llm_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    if not settings.llm_enabled:
        logger.info("LLM 未启用（LLM_ENABLED=false 或未配置 Key），语义型规则将退化为规则判定")
        return NullLLMClient("LLM_ENABLED=false 或未配置 LLM_API_KEY")
    try:
        from app.llm.deepseek import DeepSeekClient  # noqa: PLC0415

        client = DeepSeekClient(settings)
        logger.info("LLM 客户端就绪：model=%s base_url=%s", settings.llm_model, settings.llm_base_url)
        return client
    except Exception as exc:  # noqa: BLE001 - 初始化失败也必须能继续跑闭环
        logger.warning("LLM 客户端初始化失败，降级为纯规则判定：%s: %s", type(exc).__name__, exc)
        return NullLLMClient(f"{type(exc).__name__}: {exc}")


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """进程级单例（FastAPI 依赖入口）。

    客户端内部持有连接池，**不能**每次请求都新建；测试通过
    ``app.dependency_overrides[get_llm_client]`` 注入桩实现，避免真实计费调用。
    """
    return build_llm_client(get_settings())
