"""DeepSeek 客户端（CF-13…CF-18、设计 §9.2）。

两个已实测的硬性约束（设计 §9.2.2 / 风险 R10、R11）：

1. **推理 token 计入 ``max_tokens``**：配额不足时 HTTP 200 但 ``content`` 为空——
   属**静默失败**，必须显式防御（``max_tokens`` 默认 1024，CF-17 强制 ≥512）；
2. **只解析 ``message.content``**：``reasoning_content`` 仅可入调试日志，
   且**禁止**在多轮上下文中回传。

端点、模型、Key 全部来自配置（LM-07），代码中不出现任何硬编码凭据。
"""

from __future__ import annotations

import time
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.llm.base import MAX_ATTEMPTS, LLMCallLog, extract_json_object

logger = get_logger(__name__)


class DeepSeekClient:
    """OpenAI 兼容端点客户端（默认 DeepSeek 官方 API）。"""

    def __init__(self, settings: Settings | None = None, http_client: Any | None = None) -> None:
        from openai import AsyncOpenAI  # noqa: PLC0415 - 延迟导入，未启用 LLM 时不加载

        settings = settings or get_settings()
        self.enabled = True
        self.model = settings.llm_model
        self.max_tokens = settings.llm_max_tokens
        self.temperature = settings.llm_temperature
        self._settings = settings
        kwargs: dict[str, Any] = {
            "base_url": settings.llm_base_url,
            "api_key": settings.llm_api_key,
            "timeout": settings.llm_timeout_seconds,
            "max_retries": 0,  # 重试由本类控制（LM-12：≤2 次），避免 SDK 再放大概略
        }
        if http_client is not None:
            # 仅测试注入（httpx.MockTransport），生产路径为 None
            kwargs["http_client"] = http_client
        self._client = AsyncOpenAI(**kwargs)

    async def judge_json(self, *, system: str, user: str) -> dict[str, Any] | None:
        """调用聊天补全并解析 JSON；失败返回 ``None``（LM-13）。"""
        last_error: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            try:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - 网络/超时/鉴权都要降级
                last_error = f"{type(exc).__name__}: {exc}"
                self._log(LLMCallLog("semantic_judge", self.model, time.perf_counter() - started, True, error=last_error))
                logger.warning("LLM 调用失败（第 %d/%d 次）：%s", attempt, MAX_ATTEMPTS, last_error)
                continue

            duration = time.perf_counter() - started
            usage = getattr(response, "usage", None)
            message = response.choices[0].message if response.choices else None
            # LM-10：只取 content；reasoning_content 只进调试日志，绝不回传
            content = (message.content if message else None) or ""
            reasoning = getattr(message, "reasoning_content", None) if message else None

            if not content.strip():
                # 最典型的成因是 max_tokens 被推理过程吃掉（HTTP 仍为 200）
                last_error = "content 为空（疑似推理 token 耗尽 max_tokens）"
                self._log(
                    LLMCallLog(
                        "semantic_judge",
                        self.model,
                        duration,
                        True,
                        getattr(usage, "prompt_tokens", None),
                        getattr(usage, "completion_tokens", None),
                        last_error,
                        {"reasoning_chars": len(reasoning or "")},
                    )
                )
                logger.warning("LLM 返回空 content（第 %d/%d 次），按调用失败处理", attempt, MAX_ATTEMPTS)
                continue

            payload = extract_json_object(content)
            self._log(
                LLMCallLog(
                    "semantic_judge",
                    self.model,
                    duration,
                    payload is None,
                    getattr(usage, "prompt_tokens", None),
                    getattr(usage, "completion_tokens", None),
                    None if payload is not None else "非法 JSON",
                )
            )
            if payload is None:
                last_error = f"非法 JSON：{content[:120]!r}"
                logger.warning("LLM 返回非法 JSON（第 %d/%d 次）：%s", attempt, MAX_ATTEMPTS, last_error)
                continue
            return payload

        logger.warning("LLM 判定最终失败，按降级处理：%s", last_error)
        return None

    @staticmethod
    def _log(entry: LLMCallLog) -> None:
        """LM-17：记录耗时/模型/token/降级标记（内容中不含 Key）。"""
        logger.info(entry.to_text())
