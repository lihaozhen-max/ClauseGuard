"""ClauseGuard 性能基线脚本（加固轮）。

目的不是"跑分"，而是给出一份**可复现的耗时基线**，回答三个问题：

1. 单个接口在自己机器上的正常量级是多少（便于判断"这次是不是变慢了"）；
2. 数据量增长时（列表、日志分页）响应是否仍然可接受；
3. 并发访问时是否出现错误率上升或明显的性能塌陷。

用法（先起好 MySQL / 模拟审批系统 8100 / 工具服务 8000）::

    uv run --project backend python scripts/bench.py                 # 默认：接口基线 + 规模 + 并发
    uv run --project backend python scripts/bench.py --repeat 20     # 每个测点重复 20 次
    uv run --project backend python scripts/bench.py --no-seed       # 不插入合成任务（不动数据库）
    uv run --project backend python scripts/bench.py --ocr           # 额外测一次 AP-003 的 OCR（约 1 分钟）

说明：

- 所有耗时都是**端到端 HTTP 往返**（含 FastAPI 序列化与数据库访问），不是纯函数耗时；
- 合成任务用 ``instance_id = BENCH-xxxx`` 标记，**测完自动删除**，不留痕迹；
- OCR 是 CPU 推理，单页 60–77s 属预期量级；本脚本只量化，不做优化。
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402

BASE_URL = "http://127.0.0.1:8000"
BENCH_PREFIX = "BENCH-"


# ── 统计 ───────────────────────────────────────────────────────────────────
class Samples:
    def __init__(self) -> None:
        self.values: list[float] = []
        self.errors: list[str] = []

    def add(self, seconds: float) -> None:
        self.values.append(seconds)

    def fail(self, detail: str) -> None:
        self.errors.append(detail)

    def row(self, label: str, note: str = "") -> str:
        if not self.values:
            return f"{label:<34} {'—':>9} {'—':>9} {'—':>9} {'—':>9}  {len(self.errors)} 失败 {note}"
        ordered = sorted(self.values)
        p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
        return (
            f"{label:<34} {statistics.median(self.values) * 1000:>8.1f} "
            f"{p95 * 1000:>8.1f} {min(self.values) * 1000:>8.1f} "
            f"{max(self.values) * 1000:>8.1f}  {len(self.errors):>4} {note}"
        )


def timed(samples: Samples, fn, *, repeat: int) -> None:
    for _ in range(repeat):
        started = time.perf_counter()
        try:
            response = fn()
        except httpx.HTTPError as exc:
            samples.fail(f"{type(exc).__name__}: {exc}")
            continue
        elapsed = time.perf_counter() - started
        if response.status_code >= 400:
            samples.fail(f"HTTP {response.status_code}")
            continue
        samples.add(elapsed)


# ── 合成数据（规模测点用）──────────────────────────────────────────────────
async def _seed_synthetic(tasks: int, logs_per_task: int) -> int:
    """插入 ``BENCH-xxxx`` 任务与附件日志，返回插入的任务数。"""
    from sqlalchemy import text

    from app.db.session import dispose_engine, session_scope

    async def run() -> int:
        async with session_scope() as session:
            for index in range(tasks):
                await session.execute(
                    text(
                        "INSERT INTO approval_tasks "
                        "(instance_id, approval_code, approval_title, applicant_name, "
                        " attachment_count, task_status, write_status) "
                        "VALUES (:iid, :code, :title, :who, 1, 'done', 'not_written')"
                    ),
                    {
                        "iid": f"{BENCH_PREFIX}{index:05d}",
                        "code": f"CG-BENCH-{index:05d}",
                        "title": f"性能基线合成合同 {index}",
                        "who": "基线",
                    },
                )
            await session.commit()
            task_ids = (
                await session.execute(
                    text("SELECT id FROM approval_tasks WHERE instance_id LIKE :p"),
                    {"p": f"{BENCH_PREFIX}%"},
                )
            ).scalars().all()
            for task_id in task_ids:
                for seq in range(logs_per_task):
                    await session.execute(
                        text(
                            "INSERT INTO task_logs (task_id, log_level, log_type, log_content) "
                            "VALUES (:tid, 'info', 'parse', :content)"
                        ),
                        {"tid": task_id, "content": f"性能基线日志 {seq}：" + "x" * 200},
                    )
            await session.commit()
            return len(task_ids)

    try:
        return await run()
    finally:
        await dispose_engine()


async def _purge_synthetic() -> int:
    from sqlalchemy import text

    from app.db.session import dispose_engine, session_scope

    async def run() -> int:
        async with session_scope() as session:
            await session.execute(
                text(
                    "DELETE FROM task_logs WHERE task_id IN "
                    "(SELECT id FROM approval_tasks WHERE instance_id LIKE :p)"
                ),
                {"p": f"{BENCH_PREFIX}%"},
            )
            result = await session.execute(
                text("DELETE FROM approval_tasks WHERE instance_id LIKE :p"),
                {"p": f"{BENCH_PREFIX}%"},
            )
            await session.commit()
            return result.rowcount or 0

    try:
        return await run()
    finally:
        await dispose_engine()


# ── 主流程 ─────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="ClauseGuard 性能基线")
    parser.add_argument("--repeat", type=int, default=10, help="每个测点重复次数（默认 10）")
    parser.add_argument("--concurrency", type=int, default=8, help="并发测点的并发度（默认 8）")
    parser.add_argument("--seed-tasks", type=int, default=1000, help="合成任务数（默认 1000）")
    parser.add_argument("--logs-per-task", type=int, default=5, help="每个合成任务的日志条数（默认 5）")
    parser.add_argument("--no-seed", action="store_true", help="不插入合成数据，只测接口基线")
    parser.add_argument("--ocr", action="store_true", help="额外测一次 AP-003 的 OCR 解析")
    args = parser.parse_args()

    settings = get_settings()
    headers = {"X-API-Key": settings.internal_api_key}
    client = httpx.Client(base_url=BASE_URL, headers=headers, timeout=600.0)

    health = client.get("/health").json()
    print("=" * 96)
    print("ClauseGuard 性能基线")
    print("=" * 96)
    print(
        f"工具服务 {health['version']}｜{health['database']['info']}｜"
        f"LLM={'on' if health['llm_enabled'] else 'off(降级)'}｜"
        f"repeat={args.repeat}｜concurrency={args.concurrency}"
    )

    # 准备：拉取 + 把 AP-001 推到可审查状态
    client.post("/api/tasks/pull", json={"limit": 20})
    tasks = {i["instance_id"]: i["task_id"] for i in client.get("/api/tasks", params={"size": 50}).json()["items"]}
    ap001 = tasks["AP-001"]
    client.post(f"/api/tasks/{ap001}/parse")
    client.post(f"/api/tasks/{ap001}/review")

    sections: list[tuple[str, list[tuple[str, Samples, str]]]] = []

    # ── 1. 接口基线 ────────────────────────────────────────────────────
    baseline: list[tuple[str, Samples, str]] = []
    for label, fn, note in [
        ("POST /api/tasks/pull (limit=5)", lambda: client.post("/api/tasks/pull", json={"limit": 5}), "写事务 + N+1 日志"),
        ("GET  /api/tasks?size=20", lambda: client.get("/api/tasks", params={"size": 20}), "任务列表"),
        (f"GET  /api/tasks/{{id}}", lambda: client.get(f"/api/tasks/{ap001}"), "任务详情"),
        ("GET  /api/tasks/{id}/parse", lambda: client.get(f"/api/tasks/{ap001}/parse"), "解析结果"),
        ("GET  /api/tasks/{id}/review", lambda: client.get(f"/api/tasks/{ap001}/review"), "审查结果"),
        ("POST /api/tasks/{id}/parse", lambda: client.post(f"/api/tasks/{ap001}/parse"), "复用已落盘附件"),
        ("POST /api/tasks/{id}/review", lambda: client.post(f"/api/tasks/{ap001}/review"), "规则引擎 + 落库"),
        ("POST /api/tasks/{id}/write-comment", lambda: client.post(f"/api/tasks/{ap001}/write-comment"), "幂等短路"),
        ("GET  /api/tasks/{id}/logs?size=200", lambda: client.get(f"/api/tasks/{ap001}/logs", params={"size": 200}), "任务日志"),
        ("GET  /api/rules", lambda: client.get("/api/rules"), "规则维护页数据"),
    ]:
        samples = Samples()
        timed(samples, fn, repeat=args.repeat)
        baseline.append((label, samples, note))
    sections.append(("1. 接口基线（小数据量）", baseline))

    # ── 2. 规模测点 ────────────────────────────────────────────────────
    scale: list[tuple[str, Samples, str]] = []
    if not args.no_seed:
        print(f"\n插入合成数据：{args.seed_tasks} 个任务 × {args.logs_per_task} 条日志 …")
        inserted = asyncio.run(_seed_synthetic(args.seed_tasks, args.logs_per_task))
        print(f"已插入 {inserted} 个 {BENCH_PREFIX}xxxxx 任务")

        try:
            total = client.get("/api/tasks", params={"size": 1}).json()["total"]
            for label, fn, note in [
                ("GET  /api/tasks?size=20", lambda: client.get("/api/tasks", params={"size": 20}), f"共 {total} 条任务"),
                ("GET  /api/tasks?size=200", lambda: client.get("/api/tasks", params={"size": 200}), f"共 {total} 条任务"),
                ("GET  /api/tasks?status=done", lambda: client.get("/api/tasks", params={"status": "done", "size": 20}), "带状态过滤"),
                (
                    "GET  /api/tasks/{id}/logs?size=200",
                    lambda: client.get(f"/api/tasks/{ap001}/logs", params={"size": 200}),
                    f"日志表约 {inserted * args.logs_per_task} 行",
                ),
            ]:
                samples = Samples()
                timed(samples, fn, repeat=args.repeat)
                scale.append((label, samples, note))
        finally:
            removed = asyncio.run(_purge_synthetic())
            print(f"已清理 {removed} 个合成任务及其日志")
    else:
        print("\n（--no-seed：跳过规模测点）")
    sections.append(("2. 数据量增长", scale))

    # ── 3. 并发测点 ────────────────────────────────────────────────────
    concurrent: list[tuple[str, Samples, str]] = []

    def parallel(fn, note: str) -> None:
        samples = Samples()

        async def run() -> None:
            async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=600.0) as ac:

                async def one() -> None:
                    started = time.perf_counter()
                    try:
                        response = await fn(ac)
                    except httpx.HTTPError as exc:
                        samples.fail(f"{type(exc).__name__}: {exc}")
                        return
                    elapsed = time.perf_counter() - started
                    if response.status_code >= 400:
                        samples.fail(f"HTTP {response.status_code}")
                        return
                    samples.add(elapsed)

                await asyncio.gather(*(one() for _ in range(args.concurrency)))

        wall = time.perf_counter()
        asyncio.run(run())
        wall = time.perf_counter() - wall
        concurrent.append((note, samples, f"{args.concurrency} 路并发，墙钟 {wall * 1000:.0f}ms"))

    parallel(lambda ac: ac.post("/api/tasks/pull", json={"limit": 20}), f"POST /api/tasks/pull ×{args.concurrency}")
    parallel(lambda ac: ac.get("/api/tasks", params={"size": 20}), f"GET  /api/tasks ×{args.concurrency}")
    parallel(lambda ac: ac.get(f"/api/tasks/{ap001}/logs", params={"size": 200}), f"GET  /api/tasks/{{id}}/logs ×{args.concurrency}")
    parallel(lambda ac: ac.post(f"/api/tasks/{ap001}/write-comment"), f"POST /api/tasks/{{id}}/write-comment ×{args.concurrency}")
    sections.append(("3. 并发", concurrent))

    # ── 4. OCR（可选）──────────────────────────────────────────────────
    if args.ocr and "AP-003" in tasks:
        print("\n测 OCR（扫描件 AP-003，单页 CPU 推理）…")
        started = time.perf_counter()
        response = client.post(f"/api/tasks/{tasks['AP-003']}/parse")
        elapsed = time.perf_counter() - started
        body = response.json() if response.status_code == 200 else {}
        print(f"  AP-003 解析：HTTP {response.status_code}｜{elapsed:.1f}s｜"
              f"parse_mode={body.get('parse_mode')}｜页数={body.get('page_count')}")

    # ── 输出 ───────────────────────────────────────────────────────────
    for title, rows in sections:
        print("\n" + "=" * 96)
        print(title)
        print("=" * 96)
        print(f"{'测点':<34} {'中位(ms)':>8} {'P95(ms)':>8} {'最小':>8} {'最大':>8}  失败 备注")
        print("-" * 96)
        for label, samples, note in rows:
            print(samples.row(label, note))

    print("\n" + "=" * 96)
    print("结论提示")
    print("=" * 96)
    print("· 写接口（pull/parse/review/write-comment）比读接口慢属正常：它们要落库并写任务日志；")
    print("· 并发测点看的是**失败数**与**墙钟**，不是单请求耗时——本系统并发度低（内网单机），")
    print("  MySQL 行锁会把同键冲突串行化，因此并发写同一任务时墙钟≈N×单次耗时，这是设计取舍；")
    print("· OCR 是 CPU 推理，量级在几十秒/页，与数据量无关；MVP 不做后台队列（设计 §12）。")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
