"""M7 端到端闭环用例（AC19 / TS-20；同时把 AC01–AC15、AC18 串成一条链复核）。

与前面各里程碑的用例不同，本文件的目的是**把整条链一次跑通并逐条卡验收判据**：
`IF-01 → IF-02 → IF-03 → IF-04 → IF-05 → IF-06 → IF-07 → IF-18 → IF-19`，
且**全程无人工干预**（AC19 的原文要求）。

为了让"各阶段数据齐全"可核对，这里不只看接口返回值，还回查了：
落盘文件、`contract_parses.full_text`、`page_map` 反查出的 `position`、
审批系统侧的评论读回、任务日志的类型覆盖。

每个用例都通过 :func:`_ensure_loop` 自行把链路跑到目标阶段——链路里每一步都是幂等的，
因此用例之间**没有顺序依赖**，单独挑一条跑也能通过。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.deps import get_approval_client, get_llm_client
from app.clients.approval_client import ApprovalSystemClient
from app.core.config import get_settings
from app.core.enums import CORE_LOG_TYPES, RiskLevel
from app.db.models import ApprovalTask, CommentLog, ContractParse, ReviewResult
from app.db.session import session_scope
from app.llm.null import NullLLMClient
from app.main import app as service_app
from app.modules.review.comment import DISCLAIMER, NO_HITS_SUMMARY, TITLE
from app.modules.review.summary import FOCUS_POINT_LIMIT, FOCUS_POINT_MAX_LENGTH

pytestmark = pytest.mark.requires_db

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: SPEC §2.6
POSITION_PATTERNS = {
    "text": re.compile(r"^第\d+页 第\d+段$"),
    "ocr": re.compile(r"^第\d+页 区域\(\d+,\d+\)$"),
}

CORE_TYPE_VALUES = {str(item) for item in CORE_LOG_TYPES}


@pytest.fixture(scope="module")
def loop_client(mock_app: Any) -> Any:
    """闭环用的 REST 客户端：审批系统走内存 ASGI，LLM 走 Null（保证结论可复现）。

    AC19 验证的是**闭环本身**，不是在某个特定 LLM 输出下的表现；用 ``NullLLMClient``
    让语义规则走降级路径，结果稳定。真实 LLM 的通路由标 ``llm`` 的用例覆盖。
    """
    client = ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))
    service_app.dependency_overrides[get_approval_client] = lambda: client
    service_app.dependency_overrides[get_llm_client] = lambda: NullLLMClient()
    with TestClient(service_app) as test_client:
        yield test_client
    service_app.dependency_overrides.clear()


def _headers() -> dict[str, str]:
    return {"X-API-Key": get_settings().internal_api_key}


def _ensure_loop(client: Any) -> dict[str, Any]:
    """把 AP-001 推进到"已回写"这一步，并返回本次链路的各阶段产物。

    每一步都幂等（去重 / 覆盖式解析 / DT-06 唯一 / 回写幂等键），重复调用安全。
    """
    headers = _headers()
    pulled = client.post("/api/tasks/pull", json={"limit": 5}, headers=headers)
    assert pulled.status_code == 200, pulled.text
    item = next(i for i in pulled.json()["items"] if i["instance_id"] == "AP-001")
    task_id = item["task_id"]

    parsed = client.post(f"/api/tasks/{task_id}/parse", headers=headers)
    assert parsed.status_code == 200, parsed.text
    reviewed = client.post(f"/api/tasks/{task_id}/review", headers=headers)
    assert reviewed.status_code == 200, reviewed.text
    written = client.post(f"/api/tasks/{task_id}/write-comment", headers=headers)
    assert written.status_code == 200, written.text
    # 第 ② 步「详情」会在解析流程里缓存审批表单数据，因此详情要在解析之后再取
    detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()

    return {
        "task_id": task_id,
        "pull_item": item,
        "detail": detail,
        "parse": parsed.json(),
        "review": reviewed.json(),
        "write": written.json(),
    }


def _scalar(db_runner: Any, stmt: Any) -> Any:
    async def _run() -> Any:
        async with session_scope() as session:
            return await session.scalar(stmt)

    return db_runner(_run)


def _task_row(db_runner: Any, instance_id: str) -> ApprovalTask:
    async def _get() -> ApprovalTask:
        async with session_scope() as session:
            row = await session.scalar(
                select(ApprovalTask).where(ApprovalTask.instance_id == instance_id)
            )
            assert row is not None, f"任务 {instance_id} 不存在"
            session.expunge(row)
            return row

    return db_runner(_get)


# ── AC19：主链路 ────────────────────────────────────────────────────────


def test_ac19_full_closed_loop_on_ap001(loop_client: Any, db_runner, mock_module: Any) -> None:
    """AC19：从待办拉取到评论回写完整跑一遍 AP-001，各阶段数据齐全、无人工干预。"""
    loop = _ensure_loop(loop_client)
    task_id = loop["task_id"]
    parse_body = loop["parse"]
    review = loop["review"]
    write_body = loop["write"]

    # ── ① 待办拉取（AC01）：字段齐全 ──────────────────────────────────
    item = loop["pull_item"]
    for field in (
        "instance_id",
        "approval_code",
        "approval_title",
        "applicant_name",
        "apply_time",
        "attachment_count",
        "task_status",
    ):
        assert item.get(field) is not None, f"待办项缺字段 {field}"

    # ── ② 详情（IF-12）：审批基本信息 + 表单数据 + 附件 ────────────────
    detail = loop["detail"]
    assert detail["approval_code"] == item["approval_code"]
    assert detail["contract_type"] == "采购合同", "第 ② 步应把合同类型缓存进任务行"
    assert detail["form_data"], "审批表单数据不应为空（PRD §6 第二步：详情查看要读表单）"
    assert detail["form_data"].get("amount") == "500000"
    assert detail["attachments"], "合同附件不应为空"

    # ── ③ 下载落盘（AC03）────────────────────────────────────────────
    assert parse_body["parse_status"] == "success"  # AC04
    assert parse_body["parse_mode"] == "text"
    attachment = detail["attachments"][0]
    assert attachment["download_status"] == "success"
    assert (attachment["file_size"] or 0) > 0
    assert attachment["file_checksum"], "必须有 SHA-256"

    on_disk = PROJECT_ROOT / "storage" / "contracts" / str(task_id)
    assert [p for p in on_disk.rglob("*") if p.is_file()], f"附件未落盘：{on_disk}"

    # ── ④⑤ 字段与条款提取（AC04/AC06）+ 证据可回溯（AC07）────────────
    assert len(parse_body["basic_info"]) == 8
    assert len(parse_body["clause_info"]) == 8
    for record in parse_body["basic_info"] + parse_body["clause_info"]:
        assert len(record) == 5, "FieldRecord 必须是 §2.4 的 5 键对象"
        assert record["extract_status"] in {"success", "missing", "failed"}
        if record["extract_status"] == "missing":
            assert record["position"] is None, "missing 时 position 必须为 null"

    full_text = _scalar(
        db_runner, select(ContractParse.full_text).where(ContractParse.task_id == task_id)
    )
    assert full_text, "contract_parses.full_text 不应为空"

    sampled = [r for r in parse_body["basic_info"] if r["extract_status"] == "success"][:3]
    assert len(sampled) == 3, "至少应有 3 个成功提取的字段可供抽查"
    for record in sampled:
        assert record["source_text"] in full_text, f"{record['field_name']} 的 source_text 不是原文子串"
        assert POSITION_PATTERNS[parse_body["parse_mode"]].match(record["position"] or ""), (
            f"{record['field_name']} 的 position 不符合 §2.6：{record['position']}"
        )

    # ── ⑥⑦ 规则审查 + 摘要/关注点（AC08–AC12）────────────────────────
    assert len(review["rule_hits"]) == 11, "§9 的 11 条规则必须全部给出判定（AC08）"
    assert review["overall_risk_level"] == RiskLevel.HIGH.value  # AC10
    assert review["hit_count"] >= 1

    levels = {item.value for item in RiskLevel}
    for hit in review["rule_hits"]:
        assert hit["risk_level"] in levels
        assert hit["hit_status"] in {"hit", "miss", "uncertain"}
        assert hit["hit_source"] in {"rule", "llm"}
        if hit["hit_status"] == "hit":
            assert hit["suggestion"], f"{hit['rule_code']} 命中却没有建议（AC09）"
            if hit["evidence_text"]:
                # FR-RULE-04 / PS-10：证据必须是合同原文连续子串
                assert hit["evidence_text"] in full_text, f"{hit['rule_code']} 证据不是原文子串"
                assert POSITION_PATTERNS[parse_body["parse_mode"]].match(
                    hit["evidence_position"] or ""
                ), f"{hit['rule_code']} 的证据位置不符合 §2.6"

    assert review["summary_text"].strip(), "摘要不能为空（AC11）"
    assert any("\u4e00" <= ch <= "\u9fff" for ch in review["summary_text"]), "摘要必须是中文"

    focus = review["focus_points"]
    assert 1 <= len(focus) <= FOCUS_POINT_LIMIT, f"关注点 1–5 条，实际 {len(focus)}（AC12）"
    for point in focus:
        assert len(point) <= FOCUS_POINT_MAX_LENGTH, f"关注点超长：{point}"

    # ── ⑧ 结果入库（AC13）：DT-06 每任务唯一 ─────────────────────────
    assert review["review_id"]
    assert review["task_status"] == "done"
    repeat = loop_client.post(f"/api/tasks/{task_id}/review", headers=_headers()).json()
    assert repeat["review_id"] == review["review_id"], "重复审查不应新增 DT-06 记录"
    assert (
        _scalar(
            db_runner,
            select(func.count()).select_from(ReviewResult).where(ReviewResult.task_id == task_id),
        )
        == 1
    )

    # ── AC14：评论正文严格符合 §4.6.1 模板 ───────────────────────────
    comment_text = review["comment_text"]
    assert comment_text.startswith(TITLE), "缺少规范标题"
    assert "整体风险等级：高" in comment_text, "风险等级必须输出中文"
    assert "风险摘要：" in comment_text and "重点关注：" in comment_text
    assert comment_text.rstrip().endswith(DISCLAIMER), "缺少免责声明"
    numbered = re.findall(r"^(\d+)\. ", comment_text, flags=re.MULTILINE)
    assert numbered == [str(i) for i in range(1, len(numbered) + 1)], "关注点必须从 1 连续编号"

    # ── ⑨ 评论回写（AC15）────────────────────────────────────────────
    assert write_body["write_status"] == "success"
    assert (write_body["remark_id"] or "").startswith("RMK-")

    logs = loop_client.get(f"/api/tasks/{task_id}/comment-logs", headers=_headers()).json()
    assert logs["total"] == 1
    assert logs["items"][0]["write_status"] == "success"

    # AC15 的另一半：**审批系统侧**确实出现了这条评论，且内容与本地一致
    keys = mock_module._comments_by_instance.get("AP-001", [])
    assert keys, "审批系统侧没有收到评论"
    latest = mock_module._comments[keys[-1]]
    assert latest["content"] == comment_text, "回写内容与本地渲染的评论不一致"
    assert latest["instance_id"] == "AP-001"
    assert latest["remark_id"] == write_body["remark_id"]
    assert keys[-1], "幂等键不能为空"  # 评论以幂等键为字典键存储

    # 幂等：再回写一次不重复（FR-COM-06 / TS-12）
    again = loop_client.post(f"/api/tasks/{task_id}/write-comment", headers=_headers()).json()
    assert again["duplicate"] is True
    assert again["remark_id"] == write_body["remark_id"]
    assert len(mock_module._comments_by_instance.get("AP-001", [])) == len(keys)

    # ── 终态（ST-01 / ST-02）────────────────────────────────────────
    final = loop_client.get(f"/api/tasks/{task_id}", headers=_headers()).json()
    assert final["task_status"] == "done"
    assert final["write_status"] == "success"
    assert final["blocked_stage"] is None and final["error_code"] is None


def test_ac19_loop_leaves_full_task_log_trail(loop_client: Any, db_runner) -> None:
    """AC19 的"各阶段数据齐全"落到日志上：文本型合同应有 7 类核心日志（不含 `ocr`）。"""
    from app.modules.logging.service import collect_log_types

    task_id = _ensure_loop(loop_client)["task_id"]

    async def kinds() -> set[str]:
        async with session_scope() as session:
            return await collect_log_types(session, task_id)

    found = db_runner(kinds)
    missing = (CORE_TYPE_VALUES - {"ocr"}) - found
    assert not missing, f"闭环缺少日志类型：{sorted(missing)}"
    assert "ocr" not in found, "文本型合同不应出现 OCR 日志"


def test_ac02_repull_is_idempotent(loop_client: Any, db_runner) -> None:
    """AC02：连续两次拉取，第二次只更新不新建，`task_id` 不变，库中仅 1 条。"""
    headers = _headers()
    first = loop_client.post("/api/tasks/pull", json={"limit": 5}, headers=headers).json()
    before = next(i for i in first["items"] if i["instance_id"] == "AP-001")

    second = loop_client.post("/api/tasks/pull", json={"limit": 5}, headers=headers).json()
    after = next(i for i in second["items"] if i["instance_id"] == "AP-001")

    assert second["created_count"] == 0
    assert after["dedup"] == "updated"
    assert after["task_id"] == before["task_id"]

    assert (
        _scalar(
            db_runner,
            select(func.count())
            .select_from(ApprovalTask)
            .where(ApprovalTask.instance_id == "AP-001"),
        )
        == 1
    )


def test_chain_is_traceable_task_to_comment(loop_client: Any, db_runner) -> None:
    """可追踪性（PRD 19.1）：任务 → 附件 → 解析 → 审查 → 评论，外键链完整。"""
    task_id = _ensure_loop(loop_client)["task_id"]

    async def chain() -> dict[str, Any]:
        async with session_scope() as session:
            parse_row = await session.scalar(
                select(ContractParse).where(ContractParse.task_id == task_id)
            )
            review_row = await session.scalar(
                select(ReviewResult).where(ReviewResult.task_id == task_id)
            )
            comment_row = await session.scalar(
                select(CommentLog).where(CommentLog.task_id == task_id)
            )
            assert parse_row and review_row and comment_row
            return {
                "attachment": parse_row.attachment_id,
                "review": review_row.id,
                "overall": review_row.overall_risk_level,
                "idempotency_key": comment_row.idempotency_key,
                "remark_id": comment_row.remark_id,
            }

    info = db_runner(chain)
    assert info["attachment"], "解析记录必须挂到具体附件（DT-02 外键）"
    assert info["review"]
    assert info["overall"] == RiskLevel.HIGH.value
    assert info["idempotency_key"] and info["remark_id"].startswith("RMK-")


def test_task_row_matches_final_api_view(loop_client: Any, db_runner) -> None:
    """接口视图与库中真值必须一致（避免"页面好看、库里没落"的假闭环）。

    只比对**视图与库是否一致**，不假设绝对取值——`retry_count` 等字段会受其它用例
    的历史操作影响，硬编码会制造顺序依赖。
    """
    loop = _ensure_loop(loop_client)
    row = _task_row(db_runner, "AP-001")
    view = loop_client.get(f"/api/tasks/{loop['task_id']}", headers=_headers()).json()

    assert row.id == view["task_id"] == loop["task_id"]
    assert row.task_status == view["task_status"] == "done"
    assert row.write_status == view["write_status"] == "success"
    assert row.retry_count == view["retry_count"]
    assert row.blocked_stage == view["blocked_stage"] is None
    assert row.error_code == view["error_code"] is None
    assert row.contract_type == view["contract_type"] == "采购合同"
    assert row.form_data_json == view["form_data"]


def test_if12_form_data_is_cached_after_parse(loop_client: Any, db_runner) -> None:
    """回归：`form_data_json`/`contract_type` 必须被真正写入（M7 发现的缺陷）。

    缺陷原文：这两个列在 M1–M6 期间**从未被写入过**，导致 FR-UI-02 的"表单数据"永远为空。
    根因是"第 ② 步 详情"只被实现为"顺便取一下附件"，没有把详情缓存回任务行。
    """
    task_id = _ensure_loop(loop_client)["task_id"]

    async def snapshot() -> tuple[Any, Any]:
        async with session_scope() as session:
            row = await session.get(ApprovalTask, task_id)
            assert row is not None
            return row.contract_type, dict(row.form_data_json or {})

    contract_type, form_data = db_runner(snapshot)
    assert contract_type == "采购合同"
    assert form_data.get("amount") == "500000"
    assert form_data.get("supplier")


def test_approval_snapshot_is_best_effort(
    loop_client: Any, db_runner, unreachable_client: ApprovalSystemClient
) -> None:
    """审批系统不可达时，**不能**因为"缓存详情失败"就把任务判失败（降级同 LM-14）。"""
    from app.modules.approval.service import sync_approval_snapshot

    task_id = _ensure_loop(loop_client)["task_id"]

    async def try_sync() -> list[str]:
        async with session_scope() as session:
            task = await session.get(ApprovalTask, task_id)
            assert task is not None
            # 清空缓存以逼出真实请求；客户端故意指向"打不通"的地址
            task.form_data_json = None
            task.contract_type = None
            await session.flush()
            return await sync_approval_snapshot(session, task, client=unreachable_client)

    assert db_runner(try_sync) == []  # 返回空表示"没缓存成功"，而非抛异常


def test_no_hits_template_used_for_clean_contract(loop_client: Any) -> None:
    """§4.6.1 的另一支模板：AP-002 零命中时走"无命中"文案（AC14 的反例覆盖）。"""
    headers = _headers()
    loop_client.post("/api/tasks/pull", json={"limit": 5}, headers=headers)
    tasks = loop_client.get("/api/tasks", params={"size": 20}, headers=headers).json()["items"]
    task_id = next(i["task_id"] for i in tasks if i["instance_id"] == "AP-002")

    loop_client.post(f"/api/tasks/{task_id}/parse", headers=headers)
    review = loop_client.post(f"/api/tasks/{task_id}/review", headers=headers).json()

    assert review["overall_risk_level"] == RiskLevel.LOW.value
    assert review["hit_count"] == 0
    assert review["comment_text"].startswith(TITLE)
    assert NO_HITS_SUMMARY in review["comment_text"]
    assert review["comment_text"].rstrip().endswith(DISCLAIMER)
