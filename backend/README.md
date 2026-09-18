# ClauseGuard 工具服务（backend）

合同审批审查系统的工具服务，FastAPI 应用。规范依据：`../docs/SPEC.md`；实现依据：`../docs/技术方案与设计.md`。

## 环境

| 项 | 值 |
|---|---|
| Python | 3.13（`.python-version`；`paddlepaddle` 无 3.14 轮子，见设计 R1） |
| 依赖管理 | uv（`pyproject.toml` + `uv.lock`） |
| 虚拟环境 | `backend/.venv`（不提交仓库） |

```powershell
cd ClauseGuard/backend
uv sync                 # 创建/同步 .venv（含 paddlepaddle/paddleocr）
uv run python -c "import app; print(app.__version__)"
```

## 运行

```powershell
# 依赖 ClauseGuard/.env（复制自 .env.example），配置缺失会 fail-fast 报错退出
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

启动后：`GET http://127.0.0.1:8000/health`（无需鉴权）。

`/api/**` 全部要求请求头 `X-API-Key: {INTERNAL_API_KEY}`（SPEC FR-SYS-03）。

## 测试

```powershell
cd ClauseGuard/backend
uv run pytest           # testpaths 指向 ClauseGuard/tests
```

## 目录（SPEC 设计 §13）

```
backend/app/
├── main.py        FastAPI 入口
├── core/          配置（CF-22/23）· 错误码（§5.4）· 日志脱敏 · X-API-Key
├── db/            ORM 基类 · 会话 · 8 张表模型（DT-01…DT-08）
├── modules/       approval · attachment · parser · rules · review · comment · logging
├── llm/           LLMClient 抽象 + DeepSeek 实现 + Null 降级
├── clients/       审批系统 HTTP 客户端
├── schemas/       Pydantic 模型
├── api/           内部 REST（IF-10…IF-21）
└── tools/         7 个工具接口（IF-01…IF-07）
```

依赖方向铁律：`api → tools → modules → {clients, llm, db, core}`，禁止反向与跨层直连（架构图 FIG-03）。
