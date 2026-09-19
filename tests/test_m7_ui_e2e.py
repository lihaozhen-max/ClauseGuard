"""浏览器端到端用例（加固轮）：用真实 Chrome **点**界面，而不是断言 HTML 片段。

前置条件（缺一即 ``skip``，不会伪造通过）：

1. 工具服务在 ``127.0.0.1:8000``；
2. 调用端 dev server 在 ``127.0.0.1:5173``…``5180`` 之一（``cd frontend-or-client; npm run dev``）。
   Vite 默认端口是 5173，但**可能被本机其他项目占用**（``scripts/dev_up.ps1`` 占用时自动顺延），
   因此这里不写死端口，而是按**页面身份**（HTML 含 ``ClauseGuard``）在 5173–5180 上定位；
   也可用环境变量 ``CLAUSEGUARD_WEB_URL`` 直接指定；
3. 本机有 Chrome / Edge。

跑法::

    cd backend
    uv run pytest -q -m ui            # 只跑浏览器用例
    uv run pytest -q -m "not ui"      # 跳过浏览器用例（默认全量会带上它）
    $env:CLAUSEGUARD_WEB_URL = "http://127.0.0.1:5174"   # 需要时手工指定

覆盖 FR-UI-01…FR-UI-08：待办拉取、详情（含 IF-02 表单）、解析、规则命中、结果与回写、
任务日志、`blocked` 重试入口、规则维护启停。
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterator

import httpx
import pytest

from app.core.config import get_settings

# ``tests/`` 不是包（无 __init__.py），pytest 会把本目录加入 sys.path，因此直接按模块名导入
from _cdp_client import Chrome, find_browser

pytestmark = [pytest.mark.requires_db, pytest.mark.ui]

SERVICE_URL = "http://127.0.0.1:8000"

#: 调用端候选端口。5173 是 Vite 默认值，但本机可能被其他项目占用，故顺延探测。
WEB_CANDIDATE_PORTS = (5173, 5174, 5175, 5176, 5177, 5178, 5179, 5180)


def _resolve_web_url() -> tuple[str, str]:
    """按**页面身份**定位本项目调用端，返回 ``(url, 说明)``；定位不到返回 ``("", 原因)``。

    只探"端口返回 200"是不够的：本机其他项目（如 InsightTrace）的 dev server 同样会返回 200，
    却把 ``/api`` 代理到它自己的后端 —— 那样浏览器用例会去**驱动别人的界面**，
    失败信息还指向本项目的代码。多一层 ``ClauseGuard`` 字样校验，才能保证测的是本项目。
    """
    override = os.environ.get("CLAUSEGUARD_WEB_URL", "").strip().rstrip("/")
    if override:
        return override, "端口来自环境变量 CLAUSEGUARD_WEB_URL"

    occupied: list[str] = []
    for port in WEB_CANDIDATE_PORTS:
        url = f"http://127.0.0.1:{port}"
        try:
            response = httpx.get(f"{url}/", timeout=2.0)
        except Exception:  # noqa: BLE001 —— 端口没人听就试下一个
            continue
        if response.status_code != 200:
            continue
        if "ClauseGuard" in response.text:
            return url, f"按页面身份在端口 {port} 上确认"
        occupied.append(str(port))

    if occupied:
        return "", (
            f"端口 {'/'.join(occupied)} 上有服务但不是本项目调用端（页面不含 ClauseGuard）"
        )
    return "", "未在 5173–5180 上发现本项目调用端（cd frontend-or-client; npm run dev）"


WEB_URL, _WEB_URL_REASON = _resolve_web_url()


def _service_ready() -> tuple[bool, str]:
    try:
        payload = httpx.get(f"{SERVICE_URL}/health", timeout=3.0).json()
    except Exception as exc:  # noqa: BLE001
        return False, f"工具服务不可达：{exc}"
    if not payload.get("database", {}).get("connected"):
        return False, "工具服务在跑，但数据库未连通"
    return True, "ok"


def _web_ready() -> tuple[bool, str]:
    if not WEB_URL:
        return False, _WEB_URL_REASON
    try:
        response = httpx.get(f"{WEB_URL}/", timeout=3.0)
    except Exception as exc:  # noqa: BLE001
        return False, f"调用端 dev server 不可达（{WEB_URL}）：{exc}"
    if response.status_code != 200:
        return False, f"调用端返回 HTTP {response.status_code}（{WEB_URL}）"
    if "ClauseGuard" not in response.text:
        return False, f"{WEB_URL} 上的页面不是本项目调用端（页面不含 ClauseGuard）"
    return True, "ok"


@pytest.fixture(scope="module")
def page(live_db: str) -> Iterator[Any]:
    """拉起 headless Chrome 并打开调用端首页。"""
    if find_browser() is None:
        pytest.skip("未找到 Chrome/Edge，跳过浏览器端到端用例")
    ok, reason = _service_ready()
    if not ok:
        pytest.skip(reason)
    ok, reason = _web_ready()
    if not ok:
        pytest.skip(reason)

    chrome = Chrome()
    try:
        page = chrome.open(f"{WEB_URL}/tasks")
        assert page.wait_for_text("待办调用", timeout=30), "首页未渲染出「待办调用」"
        yield page
    finally:
        chrome.close()


def _api() -> httpx.Client:
    return httpx.Client(
        base_url=SERVICE_URL,
        headers={"X-API-Key": get_settings().internal_api_key},
        timeout=300.0,
    )


def _task_id(instance_id: str) -> int:
    with _api() as client:
        client.post("/api/tasks/pull", json={"limit": 20})
        items = client.get("/api/tasks", params={"size": 50}).json()["items"]
    for item in items:
        if item["instance_id"] == instance_id:
            return int(item["task_id"])
    raise AssertionError(f"未找到任务 {instance_id}")


def _reset_to_pending(db_runner: Any, instance_id: str) -> int:
    """把任务恢复到"未处理"基线（清掉附件/解析/审查/回写痕迹，任务态回到 ``pending``）。

    **为什么必须做**：``test_m5_retry`` 会用一个"能取到附件"的桩客户端把 AP-004 重试到
    ``done``，并留下附件记录。那样 AC16 的"附件缺失 → blocked"前提就不成立了——
    用例不能假设自己没有被执行过，而是要把**自己的前提**建起来。
    """
    from sqlalchemy import text

    from app.db.session import session_scope

    async def run() -> int:
        async with session_scope() as session:
            task_id = await session.scalar(
                text("SELECT id FROM approval_tasks WHERE instance_id = :iid"), {"iid": instance_id}
            )
            assert task_id is not None, f"{instance_id} 未拉取"
            for table in (
                "comment_logs",
                "review_results",
                "rule_hits",
                "contract_parses",
                "approval_attachments",
                "task_logs",
            ):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE task_id = :tid"), {"tid": task_id}
                )
            await session.execute(
                text(
                    "UPDATE approval_tasks SET task_status='pending', write_status='not_written', "
                    "blocked_stage=NULL, error_code=NULL, error_message=NULL, retry_count=0 "
                    "WHERE id = :tid"
                ),
                {"tid": task_id},
            )
            await session.commit()
            return int(task_id)

    return db_runner(run)


# ── FR-UI-01：待办列表与拉取按钮 ───────────────────────────────────────────


def test_task_list_renders_and_pull_button_works(page: Any) -> None:
    """FR-UI-01：列表显示审批编号等字段；点「拉取待办」后出现成功提示。"""
    # 会话开始时业务表被清空（conftest 的基线重置），所以先点一次「拉取待办」造数据
    page.navigate(f"{WEB_URL}/tasks", settle=1.5)
    assert page.wait_for_text("待办调用", timeout=30)
    assert page.click_by_text("拉取待办"), "未找到「拉取待办」按钮"
    assert page.wait_for_text("拉取完成", timeout=60), "拉取后没有出现结果提示"

    assert page.wait_for_text("CG-2026-0001", timeout=30), "列表未渲染出样例待办"
    body = page.text()
    for field in ("审批编号", "标题", "申请人", "申请时间", "附件数", "任务状态"):
        assert field in body, f"列表缺少列「{field}」"


# ── FR-UI-02/03/04/05/07：任务内五个模块 ──────────────────────────────────


def test_task_pages_render_and_actions_work(page: Any) -> None:
    """一路点下去：详情 → 解析 → 审查 → 结果与回写 → 日志（并真的触发这些动作）。"""
    task_id = _task_id("AP-001")
    page.navigate(f"{WEB_URL}/tasks/{task_id}/detail", settle=1.5)

    # ② 详情（FR-UI-02）：审批基本信息 + 表单数据（IF-02）+ 附件元数据
    assert page.wait_for_text("审批基本信息", timeout=30)
    assert page.wait_for_text("表单数据", timeout=15)
    assert page.wait_for_text("amount", timeout=15), "表单数据未渲染（IF-02 未生效？）"
    assert page.wait_for_text("SHA-256", timeout=15), "附件元数据区未渲染"
    assert "FR-SYS-02" in page.text(), "缺少「附件不对外直链」的说明"

    # ③ 解析结果（FR-UI-03）：点「触发解析」并断言状态标签
    assert page.click_tab("解析结果"), "未找到页签「解析结果」"
    assert page.click_by_text("触发解析"), "未找到「触发解析」按钮"
    assert page.wait_for_text("解析成功", timeout=60), "解析后未出现成功状态"
    parse_body = page.text()
    assert "原文片段" in parse_body, "缺少原文片段列"
    assert "提取状态" in parse_body, "缺少提取状态列"
    assert "缺失" in parse_body, "缺少 missing 状态（AP-001 有 3 条缺失条款）"

    # ④ 规则命中（FR-UI-04 / NF-10）：点「触发审查」并断言总风险与明细
    assert page.click_tab("规则命中"), "未找到页签「规则命中」"
    assert page.click_by_text("触发审查"), "未找到「触发审查」按钮"
    assert page.wait_for_text("整体风险", timeout=90), "审查后未出现整体风险"
    review_body = page.text()
    assert "规则判定明细" in review_body
    assert "R001" in review_body and "证据" in review_body
    badge = page.eval(
        "(() => {const n=document.querySelector('.risk-badge'); return n ? n.className : ''})()"
    )
    assert isinstance(badge, str) and ("risk-high" in badge or "risk-medium" in badge or "risk-low" in badge), (
        f"整体风险未使用 NF-10 的三色类名，实际 class={badge!r}"
    )

    # ⑤ 结果处理（FR-UI-05）：点「回写评论」并断言回写状态
    assert page.click_tab("结果处理"), "未找到页签「结果处理」"
    assert page.wait_for_text("评论内容", timeout=30)
    # 标题先渲染、正文后到（异步取数），所以这里必须等正文出现而不是立刻断言
    assert page.wait_for_text("【合同自动审查结果】", timeout=30), "评论正文未按 §4.6.1 模板渲染"
    assert page.click_by_text("回写评论"), "未找到「回写评论」按钮"
    assert page.wait_for_text("回写成功", timeout=60), "回写后未出现成功状态"
    assert page.wait_for_text("回写历史", timeout=15)

    # ⑦ 任务日志（FR-UI-07 / AC18）
    assert page.click_tab("任务日志"), "未找到页签「任务日志」"
    assert page.wait_for_text("8 类核心操作覆盖情况", timeout=30)
    log_body = page.text()
    for log_type in ("待办获取", "附件下载", "文档解析", "字段提取", "规则执行", "结果保存", "评论回写"):
        assert log_type in log_body, f"日志页未列出「{log_type}」"


# ── FR-UI-06：blocked 任务的重试入口 ───────────────────────────────────────


def test_blocked_task_shows_retry_entry(page: Any, db_runner: Any) -> None:
    """FR-UI-06 / AC16：附件缺失的任务进 blocked，页头给出告警与「人工重试」按钮。"""
    _task_id("AP-004")  # 确保已拉取
    task_id = _reset_to_pending(db_runner, "AP-004")

    with _api() as client:
        # 前置：确认起点确实是 pending（而不是被别的用例重试过）
        assert client.get(f"/api/tasks/{task_id}").json()["task_status"] == "pending"
        failed = client.post(f"/api/tasks/{task_id}/parse")
        detail = client.get(f"/api/tasks/{task_id}").json()

    assert failed.status_code == 404, f"AP-004 附件缺失应返回 404，实际 {failed.status_code}"
    assert detail["task_status"] == "blocked", f"AP-004 应为 blocked，实际 {detail['task_status']}"

    page.navigate(f"{WEB_URL}/tasks/{task_id}/detail", settle=1.5)
    assert page.wait_for_text("任务已阻塞于", timeout=30), "页头未出现阻塞告警"
    body = page.text()
    assert "CONTRACT_ATTACHMENT_MISSING" in body, "未显示错误码"
    assert "parsing" in body, "未显示阻塞阶段"
    assert page.wait_for_text("人工重试", timeout=15), "未提供人工重试入口"


# ── FR-UI-08：规则维护页的启停开关 ─────────────────────────────────────────


def test_rules_page_toggle_persists(page: Any) -> None:
    """FR-UI-08 / IF-21：在页面上关掉一条规则，刷新后状态保持，再改回。"""
    page.navigate(f"{WEB_URL}/rules", settle=2.0)
    assert page.wait_for_text("规则维护", timeout=30)
    assert page.wait_for_text("R011", timeout=20), "规则列表未渲染"

    def first_switch_state() -> bool:
        return bool(
            page.eval(
                "(() => {const s=document.querySelector('.el-switch');"
                " return s ? s.classList.contains('is-checked') : null})()"
            )
        )

    before = first_switch_state()
    assert page.eval(
        "(() => {const s=document.querySelector('.el-switch'); if(!s) return false; s.click(); return true})()"
    ), "未找到启用开关"
    assert page.wait_for(
        f"(() => {{const s=document.querySelector('.el-switch');"
        f" return s && s.classList.contains('is-checked') === {str(not before).lower()}}})()",
        timeout=30,
    ), "切换开关后状态没有变化"

    # 刷新页面确认已落库（不是只改了前端状态）
    page.navigate(f"{WEB_URL}/rules", settle=2.0)
    assert page.wait_for_text("规则维护", timeout=30)
    assert first_switch_state() is not before, "刷新后开关状态回退，说明没有真正保存"

    # 还原，避免影响后续用例
    page.eval(
        "(() => {const s=document.querySelector('.el-switch'); if(s) s.click(); return true})()"
    )
    assert page.wait_for(
        f"(() => {{const s=document.querySelector('.el-switch');"
        f" return s && s.classList.contains('is-checked') === {str(before).lower()}}})()",
        timeout=30,
    ), "还原开关失败"


def test_no_api_key_dialog_blocks_nothing_when_key_present(page: Any) -> None:
    """密钥已通过构建期变量注入时，不应弹出密钥对话框挡住页面。"""
    page.navigate(f"{WEB_URL}/tasks", settle=1.5)
    assert page.eval("document.querySelectorAll('.el-dialog').length") == 0, (
        "页面出现了对话框：调用端未拿到 X-API-Key（检查 frontend-or-client/.env.local）"
    )
    started = time.perf_counter()
    assert page.wait_for_text("待办调用", timeout=15)
    assert time.perf_counter() - started < 15


# ── 窄屏适配（加固轮）─────────────────────────────────────────────────────


@pytest.mark.parametrize(("width", "height"), [(1024, 768), (800, 900)])
def test_narrow_viewport_has_no_horizontal_overflow(page: Any, width: int, height: int) -> None:
    """窄屏下不能出现横向滚动条：外壳/侧栏/表格都得收进视口。

    这是"能用/不能用"的硬边界——内网里把窗口拖窄、或在 1024 的投影仪上演示是常态。
    """
    task_id = _task_id("AP-001")
    page.set_viewport(width, height)
    try:
        for path, marker in (
            ("/tasks", "待办调用"),
            (f"/tasks/{task_id}/detail", "审批基本信息"),
            ("/rules", "规则维护"),
        ):
            page.navigate(f"{WEB_URL}{path}", settle=1.2)
            assert page.wait_for_text(marker, timeout=30), f"{path} 在 {width}px 下没有渲染出来"

            overflow = page.eval("document.documentElement.scrollWidth - window.innerWidth")
            assert isinstance(overflow, (int, float)) and overflow <= 2, (
                f"{path} 在 {width}px 视口下横向溢出 {overflow}px"
            )

            aside = page.eval(
                "(() => {const n=document.querySelector('.app-aside');"
                " return n ? Math.round(n.getBoundingClientRect().width) : -1})()"
            )
            assert isinstance(aside, (int, float)) and aside <= 70, (
                f"{path} 在 {width}px 下侧栏应折叠成图标条，实际 {aside}px"
            )

            # 表格允许自己横向滚动，但不能把外层卡片撑破
            table_overflow = page.eval(
                "(() => {const t=document.querySelector('.card .el-table');"
                " if(!t) return 0;"
                " return Math.round(t.getBoundingClientRect().right - window.innerWidth)})()"
            )
            assert isinstance(table_overflow, (int, float)) and table_overflow <= 2, (
                f"{path} 在 {width}px 下表格超出视口 {table_overflow}px"
            )
    finally:
        page.clear_viewport()
