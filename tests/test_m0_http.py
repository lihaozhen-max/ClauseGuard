"""M0 接口骨架用例：健康检查、鉴权链路、统一错误结构（SPEC §5.2/§5.3、FR-SYS-03）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app


def test_health_is_public() -> None:
    """/health 无需鉴权，且自带进程级信息。"""
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["app"] == "ClauseGuard"
    assert {"status", "version", "env", "database", "llm_enabled"} <= set(body)


@pytest.mark.requires_db
def test_health_reports_database_connected(live_db: str) -> None:
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["database"]["connected"] is True
    assert "MySQL" in body["database"]["info"]
    # 健康检查响应不得泄漏口令或连接串（NF-06）
    settings = get_settings()
    assert settings.db_password not in str(body)


def test_internal_api_requires_valid_key() -> None:
    """FR-SYS-03：缺失或不匹配 X-API-Key → 401。"""
    with TestClient(app) as client:
        assert client.get("/api/ping").status_code == 401
        assert client.get("/api/ping", headers={"X-API-Key": "bad-key"}).status_code == 401
        ok = client.get("/api/ping", headers={"X-API-Key": get_settings().internal_api_key})
    assert ok.status_code == 200
    assert ok.json() == {"pong": True}


def test_unauthorized_uses_unified_error_body() -> None:
    """§5.3：错误响应结构统一。"""
    with TestClient(app) as client:
        body = client.get("/api/ping").json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert set(body["error"]) == {"code", "message", "task_id", "detail"}
