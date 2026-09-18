"""M1 模拟审批系统用例（SPEC FR-MOCK-01…FR-MOCK-04）。

覆盖：4 个必需接口、5 个内置样例审批单、附件真实文件流与 404、评论写入幂等、鉴权。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings

pytestmark = pytest.mark.mock

API_KEY = get_settings().approval_api_key
HEADERS = {"X-API-Key": API_KEY}


@pytest.fixture(scope="module")
def mock(mock_app) -> TestClient:
    return TestClient(mock_app)


# ── FR-MOCK-03：内置 5 个样例审批单 ─────────────────────────────────────────


def test_pending_returns_all_five_samples(mock: TestClient) -> None:
    response = mock.get("/approvals/pending", params={"limit": 20}, headers=HEADERS)
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["instance_id"] for item in items] == ["AP-001", "AP-002", "AP-003", "AP-004", "AP-005"]
    # 7.1 要求的字段必须齐备
    required = {
        "instance_id",
        "approval_code",
        "approval_title",
        "applicant_name",
        "apply_time",
        "attachment_count",
        "current_status",
    }
    assert required <= set(items[0])
    assert items[0]["attachment_count"] == 1


def test_pending_honours_limit(mock: TestClient) -> None:
    assert len(mock.get("/approvals/pending", params={"limit": 2}, headers=HEADERS).json()["items"]) == 2


def test_detail_contains_form_and_attachments(mock: TestClient) -> None:
    body = mock.get("/approvals/AP-001", headers=HEADERS).json()
    assert body["contract_type"] == "采购合同"
    assert body["form_data"]["amount"] == "500000"
    assert body["current_status"] == "pending"
    assert body["attachments"][0]["attachment_id"] == "ATT-001"
    assert body["attachments"][0]["file_size"] > 0


def test_detail_unknown_instance_returns_404(mock: TestClient) -> None:
    assert mock.get("/approvals/AP-999", headers=HEADERS).status_code == 404


# ── FR-MOCK-04：附件下载返回真实文件流，且能模拟附件不存在 ──────────────────


@pytest.mark.parametrize(
    ("instance_id", "attachment_id", "expect_status"),
    [
        ("AP-001", "ATT-001", 200),
        ("AP-002", "ATT-002", 200),
        ("AP-003", "ATT-003", 200),
        ("AP-005", "ATT-005", 200),  # 0 字节文件仍应下载成功
        ("AP-004", "ATT-004", 404),  # 审批单声明有附件，但文件不存在（AC16）
        ("AP-001", "ATT-999", 404),  # 附件编号不存在
    ],
)
def test_download_attachment(
    mock: TestClient, instance_id: str, attachment_id: str, expect_status: int
) -> None:
    response = mock.get(
        f"/approvals/{instance_id}/attachments/{attachment_id}/download", headers=HEADERS
    )
    assert response.status_code == expect_status
    if expect_status == 200:
        assert response.headers["content-type"].startswith(("application/pdf", "image/png"))
    else:
        assert response.json()["detail"]["code"] == 404


def test_download_pdf_returns_real_stream(mock: TestClient) -> None:
    content = mock.get("/approvals/AP-001/attachments/ATT-001/download", headers=HEADERS).content
    assert content.startswith(b"%PDF")  # 真实 PDF 文件流
    assert len(content) > 1000


# ── FR-MOCK-02：评论写入按幂等键去重 ────────────────────────────────────────


def test_comment_write_is_idempotent(mock: TestClient) -> None:
    payload = {"idempotency_key": "test-key-0001", "content": "【合同自动审查结果】测试评论"}

    first = mock.post("/approvals/AP-001/comments", json=payload, headers=HEADERS)
    assert first.status_code == 200
    body = first.json()
    assert body["code"] == 0
    assert body["remark_id"].startswith("RMK-")
    assert body["duplicate"] is False

    second = mock.post("/approvals/AP-001/comments", json=payload, headers=HEADERS).json()
    assert second["duplicate"] is True
    assert second["remark_id"] == body["remark_id"]  # 同一幂等键 → 同一条评论

    stored = mock.get("/approvals/AP-001/comments", headers=HEADERS).json()
    assert stored["total"] == 1
    assert stored["items"][0]["content"] == payload["content"]


def test_comment_on_unknown_instance_returns_404(mock: TestClient) -> None:
    response = mock.post(
        "/approvals/AP-999/comments",
        json={"idempotency_key": "k", "content": "c"},
        headers=HEADERS,
    )
    assert response.status_code == 404


# ── CF-06：鉴权 ─────────────────────────────────────────────────────────────


def test_mock_requires_api_key(mock: TestClient) -> None:
    assert mock.get("/approvals/pending").status_code == 401
    assert mock.get("/approvals/pending", headers={"X-API-Key": "wrong"}).status_code == 401
    assert mock.get("/health").status_code == 200  # 健康检查不需要鉴权


def test_health_reports_missing_attachment_files(mock: TestClient) -> None:
    body = mock.get("/health").json()
    assert body["status"] == "ok"
    assert body["approvals"] == 5
    # 只有 AP-004 的附件故意缺失
    assert body["missing_attachment_files"] == ["AP-004_办公用品采购合同.pdf"]
