"""模拟审批系统 — 独立 FastAPI 服务（SPEC FR-MOCK-01…FR-MOCK-04、设计 §10）。

**边界铁律（FR-SYS-01）**：它是"外部系统"角色，必须以**独立进程**运行，
工具服务只能通过 HTTP 访问它。因此本文件**不导入** ``backend/app`` 的任何模块，
自带数据（``seed.py``）与文件服务（``sample_contracts/``）。

启动（约定端口 8100，见 CF-05）：

```powershell
cd ClauseGuard
uv run --project backend uvicorn --app-dir mock-approval-system main:app --host 127.0.0.1 --port 8100
```

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | ``/approvals/pending?limit=`` | 待办列表（FR-MOCK-01） |
| GET | ``/approvals/{instance_id}`` | 审批详情（表单字段、合同类型、附件信息） |
| GET | ``/approvals/{instance_id}/attachments/{attachment_id}/download`` | 附件下载（真实文件流；不存在 → 404） |
| POST | ``/approvals/{instance_id}/comments`` | 评论回写，按 ``idempotency_key`` 去重并返回 ``remark_id``（FR-MOCK-02） |
| GET | ``/approvals/{instance_id}/comments`` | 回读已写入评论（**辅助接口**，用于 AC15 核对） |

鉴权：``X-API-Key`` 必须等于 ``APPROVAL_API_KEY``（CF-06）。
"""

from __future__ import annotations

import itertools
import mimetypes
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from seed import APPROVALS, APPROVALS_BY_ID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = PROJECT_ROOT / "sample_contracts"

_dotenv = dotenv_values(PROJECT_ROOT / ".env")


def _conf(key: str, default: str = "") -> str:
    return os.environ.get(key) or str(_dotenv.get(key) or "") or default


API_KEY = _conf("APPROVAL_API_KEY")

app = FastAPI(
    title="模拟审批系统（ClauseGuard 外部系统）",
    version="1.0.0",
    description="SPEC FR-MOCK-01…FR-MOCK-04：待办列表 / 审批详情 / 附件下载 / 评论写入",
)

# ── 内存态：评论（FR-MOCK-02 幂等）──────────────────────────────────────────
_remark_seq = itertools.count(88001)
_comments: dict[str, dict[str, Any]] = {}
#: instance_id → [idempotency_key, ...]
_comments_by_instance: dict[str, list[str]] = {}


def _error(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": status, "message": message})


def check_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """CF-06：工具服务 → 审批系统的鉴权。"""
    if not API_KEY:
        raise _error(500, "模拟审批系统未配置 APPROVAL_API_KEY")
    if x_api_key != API_KEY:
        raise _error(401, "缺少或错误的 X-API-Key")


def _attachment_payload(attachment: dict[str, Any]) -> dict[str, Any]:
    """附件元数据。文件存在时以磁盘真实大小为准（与真实审批系统行为一致）。"""
    path = CONTRACTS_DIR / attachment["file_name"]
    size = path.stat().st_size if path.is_file() else attachment.get("file_size")
    return {
        "attachment_id": attachment["attachment_id"],
        "file_name": attachment["file_name"],
        "file_type": attachment["file_type"],
        "file_size": size,
    }


# ── 1) 待办列表 ─────────────────────────────────────────────────────────────
@app.get("/approvals/pending", dependencies=[Depends(check_api_key)], tags=["审批"])
def list_pending(limit: int = 20) -> dict[str, Any]:
    """待办列表（注意：本路由必须声明在 ``/approvals/{instance_id}`` **之前**）。"""
    pending = [item for item in APPROVALS if item["current_status"] == "pending"]
    pending.sort(key=lambda item: (item["apply_time"], item["instance_id"]))
    selected = pending[: max(limit, 0)]
    return {
        "items": [
            {
                "instance_id": item["instance_id"],
                "approval_code": item["approval_code"],
                "approval_title": item["approval_title"],
                "applicant_name": item["applicant_name"],
                "apply_time": item["apply_time"],
                "attachment_count": len(item["attachments"]),
                "current_status": item["current_status"],
            }
            for item in selected
        ],
        "total": len(pending),
    }


# ── 2) 审批详情 ─────────────────────────────────────────────────────────────
@app.get("/approvals/{instance_id}", dependencies=[Depends(check_api_key)], tags=["审批"])
def get_approval(instance_id: str) -> dict[str, Any]:
    item = APPROVALS_BY_ID.get(instance_id)
    if item is None:
        raise _error(404, f"审批实例 {instance_id} 不存在")
    return {
        "instance_id": item["instance_id"],
        "approval_code": item["approval_code"],
        "approval_title": item["approval_title"],
        "applicant_name": item["applicant_name"],
        "apply_time": item["apply_time"],
        "current_status": item["current_status"],
        "contract_type": item["contract_type"],
        "form_data": item["form_data"],
        "attachments": [_attachment_payload(a) for a in item["attachments"]],
    }


# ── 3) 附件下载 ─────────────────────────────────────────────────────────────
@app.get(
    "/approvals/{instance_id}/attachments/{attachment_id}/download",
    dependencies=[Depends(check_api_key)],
    tags=["审批"],
)
def download_attachment(instance_id: str, attachment_id: str) -> FileResponse:
    """返回**真实文件流**；附件不存在 → 404（FR-MOCK-04，用于 AC16）。"""
    item = APPROVALS_BY_ID.get(instance_id)
    if item is None:
        raise _error(404, f"审批实例 {instance_id} 不存在")

    attachment = next(
        (a for a in item["attachments"] if a["attachment_id"] == attachment_id), None
    )
    if attachment is None:
        raise _error(404, f"审批实例 {instance_id} 下无附件 {attachment_id}")

    path = CONTRACTS_DIR / attachment["file_name"]
    if not path.is_file():
        # AP-004 命中此分支：审批单声明有附件，但文件不存在
        raise _error(404, f"附件 {attachment_id}（{attachment['file_name']}）不存在")

    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=attachment["file_name"])


# ── 4) 评论写入（幂等）──────────────────────────────────────────────────────
class CommentRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1)


@app.post("/approvals/{instance_id}/comments", dependencies=[Depends(check_api_key)], tags=["审批"])
def write_comment(instance_id: str, payload: CommentRequest) -> dict[str, Any]:
    """按 ``idempotency_key`` 去重并返回 ``remark_id``（FR-MOCK-02 / IF-07）。"""
    if instance_id not in APPROVALS_BY_ID:
        raise _error(404, f"审批实例 {instance_id} 不存在")

    existing = _comments.get(payload.idempotency_key)
    if existing is not None:
        return {
            "code": 0,
            "remark_id": existing["remark_id"],
            "duplicate": True,
            "message": "幂等键已存在，返回既有评论",
        }

    remark_id = f"RMK-{next(_remark_seq)}"
    _comments[payload.idempotency_key] = {
        "remark_id": remark_id,
        "instance_id": instance_id,
        "content": payload.content,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _comments_by_instance.setdefault(instance_id, []).append(payload.idempotency_key)
    return {"code": 0, "remark_id": remark_id, "duplicate": False}


@app.get("/approvals/{instance_id}/comments", dependencies=[Depends(check_api_key)], tags=["审批"])
def read_comments(instance_id: str) -> dict[str, Any]:
    """**辅助接口**（不属于 FR-MOCK-01 的 4 个必需接口）：回读已写入评论，供 AC15 核对。"""
    keys = _comments_by_instance.get(instance_id, [])
    return {"items": [_comments[key] for key in keys], "total": len(keys)}


# ── 健康检查 ────────────────────────────────────────────────────────────────
@app.get("/health", tags=["系统"])
def health() -> dict[str, Any]:
    missing_files = [
        a["file_name"]
        for item in APPROVALS
        for a in item["attachments"]
        if not (CONTRACTS_DIR / a["file_name"]).is_file()
    ]
    return {
        "status": "ok",
        "approvals": len(APPROVALS),
        "comments": len(_comments),
        "contracts_dir": str(CONTRACTS_DIR),
        "missing_attachment_files": missing_files,
    }
