"""API 层依赖注入（FastAPI ``Depends`` 的集中定义）。"""

from __future__ import annotations

from app.clients.approval_client import ApprovalSystemClient
from app.llm.base import LLMClient
from app.llm.factory import get_llm_client as _get_llm_client


def get_approval_client() -> ApprovalSystemClient:
    """审批系统客户端（CF-05 / CF-06）。

    测试中通过 ``app.dependency_overrides[get_approval_client]`` 注入
    ``httpx.ASGITransport``，即可让工具服务在**不启动独立进程**的情况下
    真实走一遍 HTTP 语义（含 404 与错误状态）访问模拟审批系统。
    """
    return ApprovalSystemClient()


def get_llm_client() -> LLMClient:
    """LLM 客户端依赖（LM-07）。

    生产路径是进程级单例（避免每请求新建连接池）；测试通过
    ``app.dependency_overrides[get_llm_client]`` 注入桩，**不会**产生真实调用与计费。
    """
    return _get_llm_client()
