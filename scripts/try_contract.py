"""拿任意一份合同，在真实系统里跑一遍完整审查（给自己测着玩用的）。

它做四件事，全部可逆：

1. **借一个审批单的槽位**：读取目标审批单声明的附件名，把待测合同复制过去
   （原文件先备份，测完自动还原，不会毁掉随系统交付的样例）；
2. **重置该任务**：清掉它已下载的附件/解析/审查/回写痕迹，让新合同被真正重新下载解析；
3. **跑链路**：`IF-14 解析 → IF-16 审查`，打印整体风险、每条规则的判定、证据原文与位置；
4. **收尾还原**：把借用的槽位恢复原状，并告诉你去哪个页面看完整结果。

用法（先起好 MySQL / 模拟审批系统 8100 / 工具服务 8000）::

    uv run --project backend python scripts/try_contract.py sample_contracts/extra/T-01_设备租赁合同.pdf
    uv run --project backend python scripts/try_contract.py <你的合同.pdf> --instance AP-002
    uv run --project backend python scripts/try_contract.py <你的合同.docx> --keep-slot
        # --keep-slot：不还原借用的槽位（想连续多次解析同一份合同看稳定性时用）

支持的格式：PDF（文本型）、Word（.docx）、扫描件（PNG/JPEG，会走 OCR，单页约 60–150 秒）。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = PROJECT_ROOT / "sample_contracts"
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

if not sys.stdout.isatty():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings  # noqa: E402

SERVICE_URL = "http://127.0.0.1:8000"
#: 可借用的槽位（任一审批单都行；AP-004 的附件本来就缺，借它最"无损"）
DEFAULT_INSTANCE = "AP-004"
RESET_TABLES = (
    "comment_logs",
    "review_results",
    "rule_hits",
    "contract_parses",
    "approval_attachments",
    "task_logs",
)

HIT_MARK = {"hit": "命中", "miss": "未命中", "uncertain": "待确认"}


def api() -> httpx.Client:
    settings = get_settings()
    return httpx.Client(
        base_url=SERVICE_URL,
        headers={"X-API-Key": settings.internal_api_key},
        timeout=600.0,
    )


def approval_attachment_name(instance_id: str) -> str:
    """走 IF-02 问审批系统：这个审批单声明的附件叫什么名字。"""
    settings = get_settings()
    response = httpx.get(
        f"{settings.approval_base_url.rstrip('/')}/approvals/{instance_id}",
        headers={"X-API-Key": settings.approval_api_key},
        timeout=30.0,
    )
    response.raise_for_status()
    attachments = response.json().get("attachments") or []
    if not attachments:
        raise SystemExit(f"审批单 {instance_id} 没有声明任何附件，换一个槽位吧")
    return str(attachments[0]["file_name"])


def reset_task(instance_id: str) -> int | None:
    """清掉该任务的业务痕迹并把状态拨回 pending；返回 task_id（未拉取则 None）。"""
    from sqlalchemy import text

    from app.db.session import dispose_engine, session_scope

    async def run() -> int | None:
        async with session_scope() as session:
            task_id = await session.scalar(
                text("SELECT id FROM approval_tasks WHERE instance_id = :iid"),
                {"iid": instance_id},
            )
            if task_id is None:
                return None
            for table in RESET_TABLES:
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

    try:
        return asyncio.run(run())
    finally:
        pass


def print_report(body: dict) -> None:
    print("\n" + "=" * 86)
    print(
        f"整体风险：{body['overall_risk_level']}｜命中 {body['hit_count']}｜"
        f"待确认 {body['uncertain_count']}｜已评估 {body['evaluated_rules']} 条规则"
    )
    print("=" * 86)
    print(f"{'规则':<6}{'判定':<7}{'等级':<7}{'规则名':<22}证据位置")
    print("-" * 86)
    for hit in body["rule_hits"]:
        print(
            f"{hit['rule_code']:<6}{HIT_MARK.get(hit['hit_status'], hit['hit_status']):<7}"
            f"{hit['risk_level']:<7}{hit['rule_name'][:20]:<22}{hit['evidence_position'] or '—'}"
        )

    hits = [h for h in body["rule_hits"] if h["hit_status"] == "hit"]
    if hits:
        print("\n命中的证据原文：")
        for hit in hits:
            print(f"\n  【{hit['rule_code']} {hit['rule_name']}】（{hit['hit_source']}）")
            print(f"    证据：{(hit['evidence_text'] or '（缺失型命中，无原文可定位）')[:150]}")
            print(f"    建议：{hit['suggestion'][:150]}")

    print(f"\n摘要（{len(body['summary_text'])} 字）：{body['summary_text'][:160]}")
    print(f"关注点 {len(body['focus_points'])} 条：")
    for index, point in enumerate(body["focus_points"], start=1):
        print(f"  {index}. {point}")
    if body.get("summary_degraded"):
        print("\n（摘要走的是模板降级路径：LLM 未启用或调用失败，LM-16/LM-18）")


def main() -> int:
    parser = argparse.ArgumentParser(description="在自己的合同上跑一遍完整审查")
    parser.add_argument("contract", help="待测合同文件（PDF / DOCX / PNG / JPEG）")
    parser.add_argument("--instance", default=DEFAULT_INSTANCE, help=f"借用的审批单（默认 {DEFAULT_INSTANCE}）")
    parser.add_argument("--keep-slot", action="store_true", help="测完不还原借用的文件名")
    args = parser.parse_args()

    contract = Path(args.contract)
    if not contract.is_absolute():
        contract = (PROJECT_ROOT / contract).resolve()
    if not contract.is_file():
        print(f"找不到合同文件：{contract}")
        return 2

    instance_id = args.instance
    slot_name = approval_attachment_name(instance_id)
    slot = SAMPLES / slot_name
    backup = slot.with_suffix(slot.suffix + ".bak")

    print("=" * 86)
    print(f"待测合同：{contract.name}（{contract.stat().st_size} 字节）")
    print(f"借用槽位：{instance_id} → {slot_name}")
    print("=" * 86)

    if backup.exists():
        backup.unlink()
    if slot.exists():
        shutil.move(str(slot), str(backup))

    try:
        shutil.copyfile(contract, slot)

        with api() as client:
            client.post("/api/tasks/pull", json={"limit": 20})
            task_id = reset_task(instance_id)
            if task_id is None:
                print(f"审批单 {instance_id} 还没被拉取为任务，请先 POST /api/tasks/pull")
                return 2
            print(f"已重置任务 task_id={task_id}，开始解析…（扫描件需 60–150 秒，请稍等）")

            parsed = client.post(f"/api/tasks/{task_id}/parse")
            if parsed.status_code != 200:
                error = parsed.json().get("error", {})
                print(f"\n解析失败：HTTP {parsed.status_code}｜{error.get('code')}｜{error.get('message')}")
                return 1
            body = parsed.json()
            print(
                f"解析完成：parse_mode={body['parse_mode']}｜parse_status={body['parse_status']}｜"
                f"{body['page_count']} 页"
            )
            missing = [
                r["field_name"]
                for r in [*body["basic_info"], *body["clause_info"]]
                if r["extract_status"] != "success"
            ]
            print(f"未成功提取的字段：{'、'.join(missing) if missing else '无'}")

            reviewed = client.post(f"/api/tasks/{task_id}/review")
            if reviewed.status_code != 200:
                error = reviewed.json().get("error", {})
                print(f"\n审查失败：HTTP {reviewed.status_code}｜{error.get('code')}｜{error.get('message')}")
                return 1
            print_report(reviewed.json())

            print("\n" + "-" * 86)
            print(f"在界面上看完整结果：http://127.0.0.1:5173/tasks/{task_id}/review")
            print(f"任务详情：        http://127.0.0.1:5173/tasks/{task_id}/detail")
            return 0
    finally:
        if args.keep_slot:
            print(f"\n（--keep-slot：{slot_name} 保持为你的合同，未还原）")
        else:
            if slot.exists():
                slot.unlink()
            if backup.exists():
                shutil.move(str(backup), str(slot))
            print(f"（已还原槽位 {slot_name}）")


if __name__ == "__main__":
    raise SystemExit(main())
