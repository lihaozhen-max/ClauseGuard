"""模拟审批系统的 HTTP 客户端（CF-05 / CF-06）。

工具服务**只**通过 HTTP 访问审批系统（FR-SYS-01），本模块是唯一的出口；
其他模块禁止直接使用 httpx 访问审批系统。

异常约定（FR-APP-06 / SPEC §5.4）：
- 网络错误、超时、5xx → ``APPROVAL_API_ERROR``（并记录请求信息、返回状态、错误信息）；
- 详情查询 404 → ``APPROVAL_NOT_FOUND``。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 认证头：工具服务 → 审批系统（CF-06）
API_KEY_HEADER = "X-API-Key"


class ApprovalSystemClient:
    """审批系统客户端。

    ``transport`` 仅用于测试注入（如 ASGI 直连内存中的模拟审批系统），生产路径为 None。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.approval_base_url).rstrip("/")
        self.api_key = api_key or settings.approval_api_key
        self.timeout = timeout
        self._transport = transport

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": self.timeout,
            "headers": {API_KEY_HEADER: self.api_key},
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**kwargs) as client:
            yield client

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """发请求并把传输层/服务端异常统一转成 ``APPROVAL_API_ERROR``。

        404 原样返回，由各方法自行决定语义（如详情不存在 → ``APPROVAL_NOT_FOUND``）。
        """
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            detail = {
                "method": method,
                "url": f"{self.base_url}{url}",
                "error": f"{type(exc).__name__}: {exc}",
            }
            logger.error("审批接口调用失败：%s %s%s（%s）", method, self.base_url, url, type(exc).__name__)
            raise AppError(
                ErrorCode.APPROVAL_API_ERROR,
                f"审批系统接口调用失败：{method} {url}",
                detail=detail,
            ) from exc

        if response.status_code == 404:
            return response

        if response.status_code >= 400:
            body = response.text[:500]
            logger.error("审批接口返回异常状态：%s %s → HTTP %s", method, url, response.status_code)
            raise AppError(
                ErrorCode.APPROVAL_API_ERROR,
                f"审批系统返回 HTTP {response.status_code}",
                detail={
                    "method": method,
                    "url": f"{self.base_url}{url}",
                    "status_code": response.status_code,
                    "body": body,
                },
            )
        return response

    # ── 待办列表 ──────────────────────────────────────────────────────────
    async def list_pending(self, limit: int) -> list[dict[str, Any]]:
        """``GET /approvals/pending``（FR-MOCK-01）。"""
        async with self._client() as client:
            response = await self._request(
                client, "GET", "/approvals/pending", params={"limit": limit}
            )
        payload = response.json()
        return list(payload.get("items", []))

    # ── 审批详情 ──────────────────────────────────────────────────────────
    async def get_approval(self, instance_id: str) -> dict[str, Any]:
        """``GET /approvals/{instance_id}``；不存在 → ``APPROVAL_NOT_FOUND``（IF-02）。"""
        async with self._client() as client:
            response = await self._request(client, "GET", f"/approvals/{instance_id}")
        if response.status_code == 404:
            raise AppError(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"审批实例 {instance_id} 不存在",
                detail={"instance_id": instance_id},
            )
        return dict(response.json())

    # ── 附件下载（M2 使用）────────────────────────────────────────────────
    async def download_attachment(self, instance_id: str, attachment_id: str) -> httpx.Response:
        """``GET /approvals/{instance_id}/attachments/{attachment_id}/download``。

        返回原始响应，让调用方按业务语义映射错误：
        404 → ``CONTRACT_ATTACHMENT_MISSING``；其余失败 → ``DOWNLOAD_FAILED``（SPEC §5.4）。
        """
        async with self._client() as client:
            return await self._request(
                client,
                "GET",
                f"/approvals/{instance_id}/attachments/{attachment_id}/download",
            )

    # ── 评论回写（M4 使用）────────────────────────────────────────────────
    async def write_comment(
        self, instance_id: str, idempotency_key: str, content: str
    ) -> httpx.Response:
        """``POST /approvals/{instance_id}/comments``（FR-MOCK-02，按幂等键去重）。"""
        async with self._client() as client:
            return await self._request(
                client,
                "POST",
                f"/approvals/{instance_id}/comments",
                json={"idempotency_key": idempotency_key, "content": content},
            )
