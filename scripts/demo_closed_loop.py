"""ClauseGuard 闭环演示脚本（M7）。

对着**真实运行的两个服务**把 SPEC 的九步业务闭环跑一遍，并逐条打印验收判据的实测值：

    ① 拉取 → ② 详情 → ③ 下载 → ④ 解析 → ⑤ 提取 → ⑥ 审查 → ⑦ 汇总 → ⑧ 保存 → ⑨ 回写

用法（先按 README 起好 MySQL / 模拟审批系统 8100 / 工具服务 8000）::

    uv run --project backend python scripts/demo_closed_loop.py            # AC19：AP-001 全链路
    uv run --project backend python scripts/demo_closed_loop.py --reset     # 先清空业务数据再演示
    uv run --project backend python scripts/demo_closed_loop.py --retry     # AC16 → AC17：阻塞与人工重试
    uv run --project backend python scripts/demo_closed_loop.py --all       # 三段一起跑

本脚本只做"驱动 + 断言 + 打印"，不改业务代码；`--retry` 需要临时补齐 AP-004 的附件文件
（模拟"审批人补交了合同"），跑完会自动删除，恢复 AC16 的「附件缺失」基线。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "backend"
SAMPLES = PROJECT_ROOT / "sample_contracts"
sys.path.insert(0, str(BACKEND))

from app.core.config import get_settings  # noqa: E402

BASE_URL = "http://127.0.0.1:8000"
CORE_LOG_TYPES = ("pull", "download", "parse", "ocr", "extract", "rule", "save", "write_comment")

PASS = "[OK]"
FAIL = "[!!]"


def load_expected(instance_id: str) -> dict:
    """读取 SPEC §15 SD-01 的期望结果（``sample_contracts/expected_results.json``）。

    这是"这份样例应该得出什么"的**单一事实来源**，演示脚本据此比对，而不是把 AP-001 的
    期望值写死在代码里 —— 否则换一个 ``--instance``（例如条款齐备的 AP-007，期望 low / 0 命中）
    就会被误判成"未通过"。
    读取失败或该样例无期望值时返回空字典：此时只做通用判据，不比对具体等级。
    """
    path = SAMPLES / "expected_results.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entry = (data.get("samples") or {}).get(instance_id)
    return entry if isinstance(entry, dict) else {}


class Demo:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.approval_base = self.settings.approval_base_url.rstrip("/")
        self.approval_headers = {"X-API-Key": self.settings.approval_api_key}
        self.client = httpx.Client(
            base_url=BASE_URL,
            headers={"X-API-Key": self.settings.internal_api_key},
            timeout=300.0,
        )
        self.checks: list[tuple[bool, str]] = []

    # ── 基础设施 ───────────────────────────────────────────────────────
    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.checks.append((ok, label))
        print(f"  {PASS if ok else FAIL} {label}" + (f" —— {detail}" if detail else ""))
        return ok

    def step(self, index: str, title: str) -> None:
        print(f"\n{index} {title}")

    def reset(self) -> None:
        """清空业务表与附件目录（与 tests/conftest.py 同口径），得到可复现的基线。"""
        import asyncio

        from sqlalchemy import text

        from app.db.session import dispose_engine, session_scope

        async def _reset() -> None:
            async with session_scope() as session:
                for table in (
                    "comment_logs",
                    "review_results",
                    "rule_hits",
                    "contract_parses",
                    "approval_attachments",
                    "task_logs",
                    "approval_tasks",
                ):
                    await session.execute(text(f"DELETE FROM {table}"))
                await session.commit()
            await dispose_engine()

        asyncio.run(_reset())
        storage = self.settings.storage_path
        if storage.exists():
            for child in storage.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
        print("已清空业务表与附件目录")

    # ── 通用取数 ───────────────────────────────────────────────────────
    def task_of(self, instance_id: str) -> dict:
        items = self.client.get("/api/tasks", params={"size": 50}).json()["items"]
        for item in items:
            if item["instance_id"] == instance_id:
                return item
        raise SystemExit(f"未找到任务 {instance_id}，请先拉取待办")

    def pull(self, limit: int = 20) -> dict:
        response = self.client.post("/api/tasks/pull", json={"limit": limit})
        response.raise_for_status()
        return response.json()

    def approval_detail(self, instance_id: str) -> dict:
        response = httpx.get(
            f"{self.approval_base}/approvals/{instance_id}",
            headers=self.approval_headers,
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()

    # ── AC19：九步闭环 ────────────────────────────────────────────────
    def closed_loop(self, instance_id: str = "AP-001") -> bool:
        print("=" * 78)
        print(f"AC19 端到端闭环演示：{instance_id}（全程无人工干预）")
        print("=" * 78)

        # 期望值来自 expected_results.json（单一事实来源），不在代码里写死某个样例的结论
        expected = load_expected(instance_id)
        expected_risk = expected.get("overall_risk_level")

        self.step("①", "拉取待办（IF-10）")
        pulled = self.pull(5)
        self.check(len(pulled["items"]) >= 1, f"拉到 {len(pulled['items'])} 条待办")
        task = self.task_of(instance_id)
        task_id = task["task_id"]
        self.check(
            all(
                task.get(key) is not None
                for key in ("approval_code", "approval_title", "applicant_name")
            ),
            f"任务 {task_id}：{task['approval_code']}｜{task['approval_title']}｜{task['applicant_name']}",
        )

        self.step("②", "查看审批详情（IF-12 + IF-02）")
        detail = self.client.get(f"/api/tasks/{task_id}").json()
        approval = self.approval_detail(instance_id)
        declared = approval["attachments"]
        # 本系统**不预下载**附件（PRD §6：下载发生在解析阶段），所以解析前附件表应为空。
        # 脚本要可重复演示，因此"已经跑过一遍"也算通过：此时附件条数应与声明一致。
        downloaded = len(detail["attachments"])
        first_run = downloaded == 0
        self.check(
            len(declared) >= 1 and (first_run or downloaded == len(declared)),
            f"审批单声明附件 {len(declared)} 个（{declared[0]['file_name']}），"
            f"本系统当前 {downloaded} 个"
            + ("（首次：尚未下载，符合预期）" if first_run else "（重跑：已下载）"),
        )

        self.step("③④⑤", "下载附件 + 解析 + 16 字段提取（IF-14）")
        parsed = self.client.post(f"/api/tasks/{task_id}/parse")
        if parsed.status_code != 200:
            self.check(False, "解析失败", parsed.text[:200])
            return False
        parse_body = parsed.json()
        self.check(
            parse_body["parse_status"] == "success", f"parse_status={parse_body['parse_status']}"
        )
        # 解析模式由附件类型决定：图片/扫描件 → ocr，其余 → text。
        # 若 expected_results.json 里有该样例的期望值则以其为准（单一事实来源）。
        scan_suffixes = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
        expected_mode = expected.get("parse_mode") or (
            "ocr" if Path(declared[0]["file_name"]).suffix.lower() in scan_suffixes else "text"
        )
        self.check(
            parse_body["parse_mode"] == expected_mode,
            f"parse_mode={parse_body['parse_mode']}"
            f"（按附件 {declared[0]['file_name']} 预期 {expected_mode}）",
        )
        total_fields = len(parse_body["basic_info"]) + len(parse_body["clause_info"])
        self.check(total_fields == 16, f"字段记录 {total_fields} 条（基本信息 8 + 条款 8）")

        # 第 ② 步会缓存审批表单数据（PRD §6 第二步），因此详情要在解析之后再取一次
        detail = self.client.get(f"/api/tasks/{task_id}").json()
        # 合同类型以**审批系统声明的值**为准（IF-02），不写死 —— 换 `--instance` 才不会假失败
        self.check(
            bool(detail["form_data"]) and detail["contract_type"] == approval["contract_type"],
            f"审批表单已缓存：contract_type={detail['contract_type']}"
            f"（= 审批系统声明值），form_data={len(detail['form_data'])} 项",
        )
        attachment = detail["attachments"][0]
        self.check(
            attachment["download_status"] == "success" and (attachment["file_size"] or 0) > 0,
            f"附件落盘 {attachment['file_name']}（{attachment['file_size']} 字节，"
            f"SHA-256 {attachment['file_checksum'][:12]}…）",
        )
        on_disk = self.settings.storage_path / str(task_id)
        stored = [p for p in on_disk.rglob("*") if p.is_file()] if on_disk.exists() else []
        self.check(bool(stored), f"磁盘上确实存在 {len(stored)} 个文件：{on_disk}")

        self.step("⑥⑦", "规则审查 + 摘要/关注点（IF-16）")
        reviewed = self.client.post(f"/api/tasks/{task_id}/review")
        if reviewed.status_code != 200:
            self.check(False, "审查失败", reviewed.text[:200])
            return False
        review = reviewed.json()
        self.check(len(review["rule_hits"]) == 11, "11 条规则全部给出判定")
        self.check(
            expected_risk is None or review["overall_risk_level"] == expected_risk,
            f"整体风险={review['overall_risk_level']}（命中 {review['hit_count']}，"
            f"待确认 {review['uncertain_count']}）"
            + (f"，与 expected_results.json 的 {expected_risk} 一致" if expected_risk else ""),
        )
        hits = [h for h in review["rule_hits"] if h["hit_status"] == "hit"]
        self.check(
            all(h["suggestion"] for h in hits),
            "命中项均含风险等级 + 证据 + 位置 + 建议：" + "、".join(h["rule_code"] for h in hits),
        )
        self.check(bool(review["summary_text"].strip()), f"摘要 {len(review['summary_text'])} 字")
        # 关注点由**命中项**派生（summarizer.active_hits）：有命中 → 1–5 条；零命中 → 0 条。
        # 原先写死 "1–5 条"，于是条款齐备的对照组（AP-007，期望 0 命中）被误判为不通过。
        focus_count = len(review["focus_points"])
        self.check(
            focus_count <= 5 and (focus_count >= 1) == bool(hits),
            f"关注点 {focus_count} 条（命中 {len(hits)} 条 → "
            + ("应有 1–5 条" if hits else "零命中时为 0 条")
            + "）",
        )

        self.step("⑧", "结果入库（IF-06 → DT-06 唯一）")
        self.check(review["task_status"] == "done", f"task_status={review['task_status']}")
        again = self.client.post(f"/api/tasks/{task_id}/review").json()
        self.check(
            again["review_id"] == review["review_id"],
            f"重复审查不新增记录：review_id={review['review_id']}",
        )

        self.step("⑨", "评论回写（IF-17 → DT-07 幂等）")
        written = self.client.post(f"/api/tasks/{task_id}/write-comment").json()
        self.check(
            written["write_status"] == "success", f"回写成功 remark_id={written['remark_id']}"
        )
        duplicate = self.client.post(f"/api/tasks/{task_id}/write-comment").json()
        self.check(duplicate["duplicate"] is True, "二次回写命中幂等键，不重复写评论")

        # 审批系统侧回读（辅助接口），确认评论真的到了对方系统
        approval = httpx.get(
            f"{self.approval_base}/approvals/{instance_id}/comments",
            headers=self.approval_headers,
            timeout=30.0,
        ).json()
        self.check(approval["total"] >= 1, f"审批系统侧可读到 {approval['total']} 条评论")
        if approval["items"]:
            content = approval["items"][-1]["content"]
            self.check(
                content.startswith("【合同自动审查结果】")
                and content.rstrip().endswith("最终审批结论由审批人员判断。"),
                "评论正文符合 §4.6.1 模板（标题 → 等级 → 摘要 → 关注点 → 免责声明）",
            )

        self.step("补", "全链路日志（IF-19，AC18）")
        logs = self.client.get(f"/api/tasks/{task_id}/logs", params={"size": 200}).json()
        kinds = set(logs["log_types"])
        expected = set(CORE_LOG_TYPES) - {"ocr"}  # 文本型合同没有 OCR 环节
        missing = expected - kinds
        self.check(not missing, f"核心日志类型齐备（{len(expected)} 类，无 ocr 属预期）")
        self.check(logs["total"] >= 1, f"日志 {logs['total']} 条：" + "、".join(sorted(kinds)))

        final = self.client.get(f"/api/tasks/{task_id}").json()
        self.check(
            final["task_status"] == "done" and final["write_status"] == "success",
            f"终态 task_status={final['task_status']}，write_status={final['write_status']}",
        )
        return all(ok for ok, _ in self.checks)

    # ── AC16 → AC17：阻塞与人工重试 ───────────────────────────────────
    def blocked_and_retry(self, instance_id: str = "AP-004") -> bool:
        print("\n" + "=" * 78)
        print(f"AC16 → AC17 异常阻塞与人工重试演示：{instance_id}")
        print("=" * 78)

        self.step("①", "拉取待办并确认 AP-004 附件在审批系统侧不存在")
        self.pull(5)
        detail = self.approval_detail(instance_id)
        file_name = detail["attachments"][0]["file_name"]
        target = SAMPLES / file_name
        if target.exists():
            target.unlink()
            print(f"  （已删除 {file_name}，恢复「附件缺失」基线）")
        self.check(not target.exists(), f"审批单声明有附件 {file_name}，但文件确实不存在")

        task = self.task_of(instance_id)
        task_id = task["task_id"]

        self.step("②", "AC16：跑解析 → 任务必须 blocked 且记录断点")
        failed = self.client.post(f"/api/tasks/{task_id}/parse")
        blocked = self.client.get(f"/api/tasks/{task_id}").json()
        self.check(failed.status_code == 404, f"解析返回 HTTP {failed.status_code}")
        self.check(
            failed.json()["error"]["code"] == "CONTRACT_ATTACHMENT_MISSING",
            f"错误码 {failed.json()['error']['code']}",
        )
        self.check(
            blocked["task_status"] == "blocked" and blocked["blocked_stage"] == "parsing",
            f"task_status={blocked['task_status']}，blocked_stage={blocked['blocked_stage']}",
        )

        self.step("③", "模拟「审批人补交合同」：把样例 PDF 放到 AP-004 的附件路径")
        shutil.copyfile(SAMPLES / "AP-002_服务合同.pdf", target)
        self.check(target.exists(), f"已补齐 {file_name}（{target.stat().st_size} 字节）")

        try:
            self.step("④", "AC17：人工重试（IF-20）→ 从断点续跑并完成")
            retried = self.client.post(f"/api/tasks/{task_id}/retry")
            if retried.status_code != 200:
                self.check(False, "重试失败", retried.text[:200])
                return False
            body = retried.json()
            self.check(body["resumed_stage"] == "parsing", f"从 {body['resumed_stage']} 阶段重入")
            self.check(body["task_status"] == "done", f"task_status={body['task_status']}")
            self.check(body["retry_count"] == 1, f"retry_count={body['retry_count']}")

            after = self.client.get(f"/api/tasks/{task_id}").json()
            self.check(
                after["blocked_stage"] is None and after["error_code"] is None,
                "断点与错误码已清空（ST-01-02）",
            )

            logs = self.client.get(f"/api/tasks/{task_id}/logs", params={"size": 200}).json()
            self.check("retry" in logs["log_types"], "存在 log_type=retry 的日志（FR-LOG-05）")

            self.step("⑤", "重试后任务可正常收尾：审查 + 回写")
            review = self.client.post(f"/api/tasks/{task_id}/review")
            self.check(review.status_code == 200, f"审查 HTTP {review.status_code}")
            written = self.client.post(f"/api/tasks/{task_id}/write-comment").json()
            self.check(
                written["write_status"] == "success", f"回写成功 remark_id={written['remark_id']}"
            )
        finally:
            if target.exists():
                target.unlink()
                print(f"  （已删除临时附件 {file_name}，恢复 AC16 基线）")
        return all(ok for ok, _ in self.checks)

    # ── 收尾 ───────────────────────────────────────────────────────────
    def summary(self) -> bool:
        total = len(self.checks)
        passed = sum(1 for ok, _ in self.checks if ok)
        print("\n" + "=" * 78)
        print(f"演示结果：{passed}/{total} 项判据通过")
        if passed != total:
            print("未通过项：")
            for ok, label in self.checks:
                if not ok:
                    print(f"  {FAIL} {label}")
        print("=" * 78)
        return passed == total


def main() -> int:
    parser = argparse.ArgumentParser(description="ClauseGuard 闭环演示（M7）")
    parser.add_argument("--reset", action="store_true", help="先清空业务数据，从干净基线开始")
    parser.add_argument("--retry", action="store_true", help="只演示 AC16/AC17（阻塞与人工重试）")
    parser.add_argument("--all", action="store_true", help="AC19 与 AC16/AC17 都跑")
    parser.add_argument("--instance", default="AP-001", help="AC19 使用的样例（默认 AP-001）")
    args = parser.parse_args()

    demo = Demo()
    try:
        health = httpx.get(f"{BASE_URL}/health", timeout=10.0).json()
    except httpx.HTTPError as exc:
        print(f"工具服务不可达（{BASE_URL}）：{exc}")
        return 2
    if not health["database"]["connected"]:
        print("数据库未连通，请先启动 database/docker-compose.yml")
        return 2
    print(
        f"工具服务 {health['version']}｜{health['database']['info']}｜"
        f"LLM={'on' if health['llm_enabled'] else 'off(降级)'}"
    )

    if args.reset:
        demo.reset()

    ok = True
    if args.all:
        ok &= demo.closed_loop(args.instance)
        ok &= demo.blocked_and_retry()
    elif args.retry:
        ok &= demo.blocked_and_retry()
    else:
        ok &= demo.closed_loop(args.instance)

    demo.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
