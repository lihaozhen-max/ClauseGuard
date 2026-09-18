"""ClauseGuard 工具服务入口（FastAPI）。

M0 阶段只提供骨架与健康检查；业务路由（IF-10…IF-21）自 M1 起在 ``app/api/`` 中挂载。

启动即 fail-fast：``get_settings()`` 在 lifespan 中解析 ``ClauseGuard/.env``，
必填项缺失会直接抛错并中止启动（CF-23），不会带病运行。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api.rules import router as rules_router
from app.api.tasks import router as tasks_router
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, redactor, setup_logging
from app.core.security import require_api_key
from app.db.session import check_connection, dispose_engine

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    setup_logging(settings.app_log_level, settings.secrets_to_redact)
    logger.info(
        "ClauseGuard 工具服务启动 | env=%s | %s | LLM=%s",
        settings.app_env,
        settings.redacted_database_url(),
        settings.llm_model if settings.llm_enabled else "disabled(降级)",
    )
    if not settings.llm_enabled:
        logger.warning("LLM 已降级：语义规则退化为关键词/存在性判定，摘要走模板（LM-15/LM-16/LM-18）")

    # 附件目录（CF-08）；OCR 模型走 ASCII 安全缓存目录（CF-21 + M2 记录 R-01）
    settings.storage_path.mkdir(parents=True, exist_ok=True)
    logger.info("附件存储目录就绪：%s", settings.storage_path)
    settings.ocr_cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info("OCR 模型缓存目录：%s", settings.ocr_cache_dir)

    ok, info = await check_connection()
    if ok:
        logger.info("数据库连通：%s", info)
    else:
        logger.warning("数据库暂不可用（服务继续启动，/health 会如实报告）：%s", info)

    yield

    await dispose_engine()
    logger.info("ClauseGuard 工具服务已停止")


app = FastAPI(
    title="ClauseGuard 合同审批审查系统 — 工具服务",
    version=__version__,
    description="规范依据：docs/SPEC.md（CG-SPEC-001）",
    lifespan=lifespan,
)

# 内部 REST：IF-10…IF-12（M1），IF-13…IF-20（M2–M5），IF-21（M6）
app.include_router(tasks_router)
app.include_router(rules_router)


@app.get("/health", tags=["系统"], summary="健康检查（无需鉴权）")
async def health() -> dict:
    settings = get_settings()
    ok, info = await check_connection()
    return {
        "status": "ok" if ok else "degraded",
        "app": "ClauseGuard",
        "version": __version__,
        "env": settings.app_env,
        # 连接串/驱动异常信息必须脱敏后返回（NF-06、FR-LOG-03）
        "database": {"connected": ok, "info": redactor.redact(info)},
        "llm_enabled": settings.llm_enabled,
    }


@app.get("/api/ping", tags=["系统"], summary="内部接口鉴权冒烟（FR-SYS-03）", dependencies=[Depends(require_api_key)])
async def ping() -> dict:
    """M0 冒烟端点：用于验证 `X-API-Key` 鉴权链路（缺失/错误 → 401 UNAUTHORIZED）。

    M1 起正式业务路由（IF-10…IF-21）并入 ``app/api/``，本端点保留作存活探测。
    """
    return {"pong": True}


@app.exception_handler(AppError)
async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    """业务异常 → SPEC §5.3 统一结构。"""
    logger.warning("业务错误 [%s] task_id=%s %s", exc.code.value, exc.task_id, exc.message)
    return JSONResponse(status_code=exc.http_status, content=exc.to_body())


@app.exception_handler(RequestValidationError)
async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """参数校验失败 → 统一结构（NF-09：禁止泄漏堆栈）。"""
    detail = [
        {"loc": ".".join(str(part) for part in err["loc"]), "msg": err["msg"], "type": err["type"]}
        for err in exc.errors()
    ]
    error = AppError(ErrorCode.VALIDATION_ERROR, "请求参数校验失败", detail={"errors": detail})
    return JSONResponse(status_code=error.http_status, content=error.to_body())


@app.exception_handler(Exception)
async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
    """兜底：仅记录堆栈到服务端日志，响应体不含任何内部细节（NF-09）。"""
    logger.exception("未处理异常：%s", type(exc).__name__)
    error = AppError(ErrorCode.INTERNAL_ERROR, "服务内部错误")
    return JSONResponse(status_code=error.http_status, content=error.to_body())
