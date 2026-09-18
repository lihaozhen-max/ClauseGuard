# 模拟审批系统（mock-approval-system）

**已实现（M1）**。作为**独立进程**运行的外部系统角色（SPEC FR-SYS-01），工具服务只能通过 HTTP 访问它，
禁止直接读写其数据库或内存——因此 `main.py` **不导入** `backend/app` 的任何模块。

## 接口

| 方法 | 路径 | 说明 | 依据 |
|---|---|---|---|
| `GET` | `/approvals/pending?limit=` | 待办列表（审批编号、标题、申请人、申请时间、附件数量、状态） | FR-MOCK-01 |
| `GET` | `/approvals/{instance_id}` | 审批详情（表单字段、合同类型、当前状态、附件信息）；不存在 → 404 | FR-MOCK-01 |
| `GET` | `/approvals/{instance_id}/attachments/{attachment_id}/download` | 附件**真实文件流**；附件不存在 → 404 | FR-MOCK-04 |
| `POST` | `/approvals/{instance_id}/comments` | 评论写入，按 `idempotency_key` 去重并返回 `remark_id` | FR-MOCK-02 |
| `GET` | `/approvals/{instance_id}/comments` | **辅助**：回读已写入评论（供 AC15 核对，不属于 4 个必需接口） | — |
| `GET` | `/health` | 健康检查，含 `missing_attachment_files` | — |

鉴权：`X-API-Key` 必须等于 `.env` 中的 `APPROVAL_API_KEY`（CF-06），否则 401。`/health` 免鉴权。

完整请求/响应样例见 `docs/接口说明.md` §4。

## 内置样例审批单（SPEC §15，FR-MOCK-03）

SD-02：全部为**自造文本**，禁止真实企业合同数据。
期望结果在 `seed.py` 的 `expected` 字段中（SD-01），并导出为 `sample_contracts/expected_results.json`。

| 样例 | 合同类型 | 附件 | 期望结果 |
|---|---|---|---|
| AP-001 | 采购合同 | `AP-001_采购合同.pdf` | 命中 R001/R003/R005/R008/R010/R011，整体 `high` |
| AP-002 | 服务合同 | `AP-002_服务合同.pdf` | **0 命中**，整体 `low` |
| AP-003 | 采购合同（扫描件） | `AP-003_扫描件.png` | 走 OCR，命中 ≥2 条 |
| AP-004 | 采购合同 | **文件故意缺失** | `blocked` / `CONTRACT_ATTACHMENT_MISSING`（AC16） |
| AP-005 | 框架协议 | `AP-005_空文件.pdf`（0 字节） | `blocked` / `EMPTY_CONTRACT_CONTENT` |

## 运行

约定端口 `8100`（CF-05 `APPROVAL_BASE_URL`）。为免重复安装重依赖，本服务复用
`backend/.venv`（FastAPI/uvicorn 已装），但**必须是独立进程**：

```powershell
cd ClauseGuard
uv run --project backend uvicorn --app-dir mock-approval-system main:app --host 127.0.0.1 --port 8100
```

自检：

```powershell
curl http://127.0.0.1:8100/health
# {"status":"ok","approvals":5,"comments":0,"contracts_dir":"…","missing_attachment_files":["AP-004_办公用品采购合同.pdf"]}
```

## 已知限制

- 评论存储在**内存**中，进程重启即清空（用于 AC15 演示时需保持同一进程）。
- 未提供"审批单状态流转"接口：`current_status` 恒为 `pending`；MVP 不做复杂审批工作流（PRD §20）。
- 未持久化到数据库：作为"外部系统"，其数据由 `seed.py` 提供，符合"自带数据"的定位。
