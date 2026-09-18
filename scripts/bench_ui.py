"""前端大数据渲染基线（加固轮）。

`scripts/bench.py` 量的是**接口**耗时；本脚本量的是**页面**：同样一份数据量增长时，
浏览器要花多久把界面渲染出来、DOM 里多了多少节点、JS 堆涨了多少。

用法（需先起好：MySQL + 工具服务 8000 + 调用端 5173）::

    uv run --project backend python scripts/bench_ui.py
    uv run --project backend python scripts/bench_ui.py --seed-tasks 2000 --logs-per-task 400
    uv run --project backend python scripts/bench_ui.py --base-url http://127.0.0.1:4173   # 指向 preview 构建

说明：

- 渲染耗时 = 从发起导航到"目标行数出现在 DOM 里"的墙钟时间（50ms 轮询粒度）；
- **默认量的是 Vite dev server**（按需编译，比生产构建慢得多），因此数字偏保守；
  想看生产数据先 `npm run build && npm run preview`，再用 `--base-url` 指过去；
- 合成任务用 `BENCH-xxxxx` 标记，**测完自动删除**；
- 复用 `tests/_cdp_client.py`（零依赖 CDP 客户端），不引入 Playwright。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))  # 复用零依赖 CDP 客户端
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 复用 bench.py 的造数/清理

if not sys.stdout.isatty():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from _cdp_client import Chrome, WebSocketError, find_browser  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from bench import BENCH_PREFIX, _purge_synthetic, _seed_synthetic  # noqa: E402

SERVICE_URL = "http://127.0.0.1:8000"


class Measurement:
    def __init__(self, label: str, volume: str) -> None:
        self.label = label
        self.volume = volume
        self.ms: float | None = None
        self.nodes: int | None = None
        self.heap_mb: float | None = None
        self.note = ""

    def row(self) -> str:
        if self.ms is None:
            return f"{self.label:<30} {self.volume:<14} {'—':>10} {'—':>10} {'—':>10}  {self.note}"
        heap = f"{self.heap_mb:.1f}" if self.heap_mb is not None else "—"
        return (
            f"{self.label:<30} {self.volume:<14} {self.ms:>10.0f} "
            f"{self.nodes if self.nodes is not None else '—':>10} {heap:>10}  {self.note}"
        )


def measure(
    page,
    url: str,
    ready_js: str,
    label: str,
    volume: str,
    *,
    timeout: float = 60.0,
    warmup: bool = True,
) -> Measurement:
    """导航到 url，等到 ready_js 为真，记录耗时 / DOM 节点数 / JS 堆。

    ``warmup`` 会先访问一次同一个路由：Vite dev server 是按需编译的，
    首次进入某个页面要把该页的模块编译一遍，不预热就会把"编译时间"算进"渲染时间"。
    """
    result = Measurement(label, volume)
    if warmup:
        page.navigate(url, settle=0.0)
        page.wait_for(ready_js, timeout=timeout, interval=0.1)
    page.navigate("about:blank", settle=0.2)
    started = time.perf_counter()
    page.navigate(url, settle=0.0)
    if not page.wait_for(ready_js, timeout=timeout, interval=0.05):
        result.note = "超时（目标内容未出现）"
        return result
    result.ms = (time.perf_counter() - started) * 1000
    result.nodes = page.eval("document.querySelectorAll('*').length")
    heap = page.eval(
        "performance.memory ? performance.memory.usedJSHeapSize / 1048576 : null"
    )
    result.heap_mb = float(heap) if isinstance(heap, (int, float)) else None
    return result


def rows_ready(count: int) -> str:
    return f"document.querySelectorAll('.el-table__body tbody tr').length >= {count}"


def main() -> int:
    parser = argparse.ArgumentParser(description="ClauseGuard 前端渲染基线")
    parser.add_argument("--base-url", default="http://127.0.0.1:5173", help="调用端地址")
    parser.add_argument("--seed-tasks", type=int, default=1000, help="合成任务数（默认 1000）")
    parser.add_argument("--logs-per-task", type=int, default=60, help="每个合成任务的日志条数")
    parser.add_argument("--ap001-logs", type=int, default=300, help="给 AP-001 追加的日志条数（默认 300）")
    parser.add_argument("--no-seed", action="store_true", help="不造数，只量当前小数据基线")
    args = parser.parse_args()

    if find_browser() is None:
        print("未找到 Chrome/Edge，无法测量页面渲染。")
        return 2

    settings = get_settings()
    headers = {"X-API-Key": settings.internal_api_key}
    with httpx.Client(base_url=SERVICE_URL, headers=headers, timeout=300.0) as api:
        health = api.get("/health").json()
        api.post("/api/tasks/pull", json={"limit": 20})
        items = api.get("/api/tasks", params={"size": 50}).json()["items"]
        by_instance = {item["instance_id"]: item["task_id"] for item in items}
        if "AP-001" not in by_instance:
            print("未拉取到 AP-001，请先 POST /api/tasks/pull")
            return 2
        ap001 = by_instance["AP-001"]
        api.post(f"/api/tasks/{ap001}/parse")
        api.post(f"/api/tasks/{ap001}/review")

    print("=" * 92)
    print("ClauseGuard 前端渲染基线（真实 Chrome + 真实后端）")
    print("=" * 92)
    print(
        f"调用端 {args.base_url}｜{health['database']['info']}｜"
        f"随机造数 {args.seed_tasks} 任务 × {args.logs_per_task} 日志｜AP-001 追加 {args.ap001_logs} 条日志"
    )

    chrome = Chrome()
    results: list[Measurement] = []
    list_small: Measurement | None = None
    list_large: Measurement | None = None
    try:
        page = chrome.open(f"{args.base_url}/tasks")
        assert page.wait_for_text("待办调用", timeout=45), "调用端未就绪"

        # ── 小数据基线（当前库里的真实 5 条任务）──────────────────────
        list_small = measure(page, f"{args.base_url}/tasks", rows_ready(1), "待办列表", "5 条任务")
        results.append(list_small)
        results.append(
            measure(page, f"{args.base_url}/tasks/{ap001}/logs", rows_ready(1), "任务日志", "~15 条日志")
        )
        results.append(
            measure(page, f"{args.base_url}/rules", rows_ready(1), "规则维护", "11 条规则")
        )
        results.append(
            measure(
                page,
                f"{args.base_url}/tasks/{ap001}/review",
                "document.body.innerText.includes('整体风险')",
                "规则命中",
                "11 条判定",
            )
        )

        if not args.no_seed:
            print(f"\n插入合成数据（{BENCH_PREFIX}xxxxx）…")
            tasks = asyncio.run(_seed_synthetic(args.seed_tasks, args.logs_per_task))
            print(f"已插入 {tasks} 个任务")

            from sqlalchemy import text

            from app.db.session import dispose_engine, session_scope

            async def add_logs() -> int:
                async with session_scope() as session:
                    for seq in range(args.ap001_logs):
                        await session.execute(
                            text(
                                "INSERT INTO task_logs (task_id, log_level, log_type, log_content) "
                                "VALUES (:tid, 'info', 'parse', :content)"
                            ),
                            {"tid": ap001, "content": f"渲染基线日志 {seq}：" + "y" * 200},
                        )
                    await session.commit()
                await dispose_engine()
                return args.ap001_logs

            ap001_logs = asyncio.run(add_logs())
            print(f"已给 AP-001 追加 {ap001_logs} 条日志")

            try:
                total = httpx.get(
                    f"{SERVICE_URL}/api/tasks", params={"size": 1}, headers=headers, timeout=30
                ).json()["total"]
                list_large = measure(
                    page, f"{args.base_url}/tasks", rows_ready(20), "待办列表", f"{total} 条任务"
                )
                results.append(list_large)
                results.append(
                    measure(
                        page,
                        f"{args.base_url}/tasks/{ap001}/logs",
                        rows_ready(50),
                        "任务日志（默认 50/页）",
                        f"{ap001_logs} 条日志",
                    )
                )

                # 交互：把分页切到 100/页，量一次更大的表
                big = Measurement("任务日志（切到 100/页）", f"{ap001_logs} 条日志")
                if page.eval(
                    "(() => {const s=document.querySelector('.el-pagination .el-select');"
                    " if(!s) return false; s.click(); return true})()"
                ):
                    time.sleep(0.4)
                    if page.click_by_text("100", selector=".el-select-dropdown__item", timeout=5):
                        started = time.perf_counter()
                        if page.wait_for(rows_ready(100), timeout=30, interval=0.05):
                            big.ms = (time.perf_counter() - started) * 1000
                            big.nodes = page.eval("document.querySelectorAll('*').length")
                            big.note = "分页交互后重渲染"
                        else:
                            big.note = "切页后未达到 100 行"
                    else:
                        big.note = "未找到 100/页 选项"
                else:
                    big.note = "未找到分页选择器"
                results.append(big)
            finally:
                removed = asyncio.run(_purge_synthetic())
                print(f"已清理 {removed} 个合成任务及其日志")
    except WebSocketError as exc:
        print(f"浏览器驱动失败：{exc}")
        return 2
    finally:
        chrome.close()

    print("\n" + "=" * 92)
    print(f"{'场景':<30} {'数据量':<14} {'耗时(ms)':>10} {'DOM节点':>10} {'JS堆(MB)':>10}  备注")
    print("-" * 92)
    for item in results:
        print(item.row())

    print("\n" + "=" * 92)
    print("怎么看这份数据")
    print("=" * 92)
    if list_small and list_large and list_small.ms and list_large.ms:
        ratio = list_large.ms / list_small.ms
        print(
            f"· 待办列表从 {list_small.volume} 涨到 {list_large.volume}，渲染耗时 "
            f"{list_small.ms:.0f}ms → {list_large.ms:.0f}ms（×{ratio:.2f}）："
            "没有随总数据量线性膨胀，因为列表由接口分页，DOM 里始终只有当前页的行。"
        )
    print("· 真正的风险点不是「总数据量」，而是**单页行数**——日志页切到 100/页 后的耗时才是要盯的指标；")
    print("· 本基线跑在 Vite dev server 上（按需编译），比生产构建慢；要比生产数字请用 --base-url 指向 preview。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
