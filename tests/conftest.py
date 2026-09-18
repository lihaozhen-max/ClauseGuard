"""pytest 公共夹具。

数据库集成用例统一用 :func:`db_run` 在**独立事件循环**中执行：
engine 是进程级单例，跨事件循环复用会出问题，因此每次用完整轮次都释放连接池。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest

from app.clients.approval_client import ApprovalSystemClient
from app.core.config import Settings, get_settings
from app.core.enums import ParseMode
from app.db.session import check_connection, dispose_engine, reset_engine_state
from app.llm.null import NullLLMClient
from app.modules.parser.fields import extract_fields
from app.modules.parser.models import ParsedDocument, TextBlock
from app.modules.rules.context import RuleContext

T = TypeVar("T")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCK_DIR = PROJECT_ROOT / "mock-approval-system"


def db_run(factory: Callable[[], Awaitable[T]]) -> T:
    """在一个新事件循环中跑协程，结束后释放连接池。

    开头先 :func:`reset_engine_state`：测试里可能已有别的循环（如 FastAPI TestClient
    的 portal 循环）建过 engine，进程级单例会让新循环复用到它并抛
    ``Future attached to a different loop``。
    """

    async def _wrapper() -> T:
        try:
            return await factory()
        finally:
            await dispose_engine()

    reset_engine_state()
    return asyncio.run(_wrapper())


@pytest.fixture(scope="session")
def db_runner() -> Callable[[Callable[[], Awaitable[T]]], T]:
    """把 async 查询函数交给它执行，自动处理事件循环与连接池释放。"""
    return db_run


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def live_db() -> str:
    """MySQL 不可用则跳过依赖数据库的用例（不伪造通过）。"""

    async def _probe() -> tuple[bool, str]:
        try:
            return await check_connection()
        finally:
            await dispose_engine()

    ok, info = asyncio.run(_probe())
    if not ok:
        pytest.skip(f"MySQL 未就绪，跳过数据库集成用例：{info}")
    return info


def as_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


@pytest.fixture(scope="session")
def mock_module() -> Any:
    """加载模拟审批系统（独立服务）的模块对象（``main.py``）。

    用 ASGI 内存传输直连，既保留完整的 HTTP 语义（状态码、404、文件流），
    又不必为跑测试而额外拉起一个进程——FR-SYS-01 的"独立进程"要求由
    ``mock-approval-system/README.md`` 记录的真实启动方式保证（见 M1 验收记录）。
    """
    if str(MOCK_DIR) not in sys.path:
        sys.path.insert(0, str(MOCK_DIR))
    spec = importlib.util.spec_from_file_location("mock_approval_main", MOCK_DIR / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mock_approval_main"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def mock_app(mock_module: Any) -> Any:
    return mock_module.app


@pytest.fixture
def approval_client(mock_app: Any) -> ApprovalSystemClient:
    """指向内存版模拟审批系统的工具服务客户端。"""
    return ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))


@pytest.fixture
def unreachable_client() -> ApprovalSystemClient:
    """指向"打不通"的审批系统，用于验证 APPROVAL_API_ERROR（FR-APP-06）。"""

    def _boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("模拟网络不可达")

    return ApprovalSystemClient(transport=httpx.MockTransport(_boom))


# ── M3：规则引擎测试用夹具 ────────────────────────────────────────────────


class StubLLMClient:
    """测试用 LLM 客户端：按预设 JSON 作答，不联网。

    ``payload=None`` 模拟"调用失败"（LM-13：超时/空 content/非法 JSON 一律算失败）。
    """

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        enabled: bool = True,
        model: str = "stub",
        raise_error: bool = False,
    ) -> None:
        self.payload = payload
        self.enabled = enabled
        self.model = model
        self.raise_error = raise_error
        self.calls: list[dict[str, str]] = []

    async def judge_json(self, *, system: str, user: str) -> dict[str, Any] | None:
        self.calls.append({"system": system, "user": user})
        if self.raise_error:
            raise RuntimeError("stub LLM 故障")
        return self.payload


@pytest.fixture
def stub_llm() -> Callable[..., StubLLMClient]:
    def _make(payload: dict[str, Any] | None = None, **kwargs: Any) -> StubLLMClient:
        return StubLLMClient(payload, **kwargs)

    return _make


def document_from_pages(
    pages: dict[int, list[str]], parse_mode: ParseMode = ParseMode.TEXT
) -> ParsedDocument:
    """由"页码 → 段落列表"构造 ``ParsedDocument``（偏移与全文按真实规则拼装）。"""
    blocks: list[TextBlock] = []
    chunks: list[str] = []
    cursor = 0
    for page_no in sorted(pages):
        for para_no, paragraph in enumerate(pages[page_no], start=1):
            blocks.append(TextBlock(page_no, para_no, paragraph, cursor, cursor + len(paragraph)))
            chunks.append(paragraph)
            cursor += len(paragraph) + 1
    return ParsedDocument(
        full_text="\n".join(chunks),
        blocks=blocks,
        parse_mode=parse_mode,
        page_count=len(pages),
    )


@pytest.fixture
def rule_context_factory() -> Callable[..., RuleContext]:
    """``rule_context_factory(pages, llm=..., settings=...)`` → 可直接喂给规则处理器的上下文。

    字段提取走与生产**完全相同**的 :func:`extract_fields`，因此规则测试不仅能验判定逻辑，
    也能连带验证字段/条款提取（RL-00-06 要求的"输入 = full_text + basic_info + clause_info"）。
    """

    def _make(
        pages: dict[int, list[str]],
        *,
        llm: Any = None,
        settings: Settings | None = None,
        parse_mode: ParseMode = ParseMode.TEXT,
    ) -> RuleContext:
        document = document_from_pages(pages, parse_mode)
        basic, clauses, _ = extract_fields(document)
        return RuleContext.build(
            document=document,
            basic_info=[record.to_dict() for record in basic],
            clause_info=[record.to_dict() for record in clauses],
            settings=settings or get_settings(),
            llm=llm or NullLLMClient(),
        )

    return _make
