"""M3 规则引擎集成用例（AC08、AC09、AC10 + SD-01 回归）。

跑真实的"库中规则 + 库中解析结果"链路；LLM 用 ``NullLLMClient`` 以获得**确定性**结果
（语义型规则退化为 uncertain，见 LM-15），真实 LLM 的行为另由 ``test_m3_llm.py`` 覆盖。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.api.deps import get_approval_client, get_llm_client
from app.clients.approval_client import ApprovalSystemClient
from app.core.config import PROJECT_ROOT, get_settings
from app.core.enums import HitSource, HitStatus
from app.core.errors import AppError, ErrorCode
from app.db.models import ApprovalTask, ContractParse, RuleHit
from app.db.session import session_scope
from app.llm.null import NullLLMClient
from app.main import app as service_app
from app.modules.parser.service import get_parse_row  # noqa: F401  （保留：便于本地调试）
from app.modules.rules.aggregator import aggregate_overall_risk
from app.modules.rules.service import run_rules_for_task
from app.tools.approval import list_pending_contract_approvals
from app.tools.parser import parse_task
from app.tools.rules import run_contract_rules

pytestmark = pytest.mark.requires_db

EXPECTED = json.loads(
    (PROJECT_ROOT / "sample_contracts" / "expected_results.json").read_text(encoding="utf-8")
)["samples"]

POSITION_TEXT_RE = re.compile(r"^第\d+页 第\d+段$")
POSITION_OCR_RE = re.compile(r"^第\d+页 区域\(\d+,\d+\)$")


async def _task_of(instance_id: str) -> ApprovalTask | None:
    async with session_scope() as session:
        task = await session.scalar(
            select(ApprovalTask).where(ApprovalTask.instance_id == instance_id)
        )
        if task is not None:
            session.expunge(task)
    return task


@pytest.fixture(scope="module", autouse=True)
def prepared_tasks(db_runner, mock_app: Any) -> None:
    """确保 AP-001 / AP-002 已拉取并**按当前实现重新解析**。

    用例自足：不依赖其它模块的执行顺序，也不依赖库中可能过期的解析结果
    （条款切分逻辑一旦调整，旧解析记录就会与规则判定口径不一致）。
    """
    client = ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))

    async def _prepare() -> None:
        await list_pending_contract_approvals(20, client=client)
        for instance_id in ("AP-001", "AP-002"):
            task = await _task_of(instance_id)
            if task is None:
                continue
            await parse_task(task.id, client=client)

    db_runner(_prepare)


async def _run(task_id: int, *, llm: Any = None):
    async with session_scope() as session:
        result = await run_rules_for_task(session, task_id, settings=get_settings(), llm=llm or NullLLMClient())
        await session.commit()
    return result


async def _full_text(task_id: int) -> str:
    async with session_scope() as session:
        row = await session.scalar(
            select(ContractParse)
            .where(ContractParse.task_id == task_id)
            .order_by(ContractParse.id.desc())
        )
        return row.full_text or ""


# ── AC08：返回全部 11 条规则的判定结果 ────────────────────────────────────


def test_if05_evaluates_all_eleven_rules(live_db: str, db_runner) -> None:
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None
    result = db_runner(lambda: _run(task.id))

    assert result.evaluated_rules == 11
    assert len(result.rule_hits) == 11
    codes = [row["rule_code"] for row in result.rule_hits]
    assert codes == [f"R{i:03d}" for i in range(1, 12)]

    valid_status = {status.value for status in HitStatus}
    valid_source = {source.value for source in HitSource}
    for row in result.rule_hits:
        assert row["hit_status"] in valid_status
        assert row["hit_source"] in valid_source
        assert row["risk_level"] in {"low", "medium", "high"}
        assert row["rule_name"]


# ── AC09：每条命中含等级 + 证据 + 位置 + 建议 ─────────────────────────────


def test_every_hit_carries_level_evidence_position_suggestion(live_db: str, db_runner) -> None:
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None
    result = db_runner(lambda: _run(task.id))
    full_text = db_runner(lambda: _full_text(task.id))

    hits = [row for row in result.rule_hits if row["hit_status"] == HitStatus.HIT.value]
    assert hits, "AP-001 应当有命中"

    for row in hits:
        assert row["risk_level"] in {"low", "medium", "high"}
        assert row["suggestion"], f"{row['rule_code']} 命中必须带处理建议"
        if row["evidence_text"] is None:
            # 缺失型命中（条款不存在）没有原文可引，§4.6.1 规定改用建议行
            assert row["rule_code"] in {"R004", "R008", "R010", "R011"}
            assert row["evidence_position"] is None
            continue
        assert row["evidence_text"] in full_text, f"{row['rule_code']} 证据不是合同原文连续子串"
        assert POSITION_TEXT_RE.match(row["evidence_position"] or ""), row["evidence_position"]


def test_hit_rows_are_persisted_with_snapshot(live_db: str, db_runner) -> None:
    """FR-RULE-09：命中记录必须快照当时的 risk_level 与 suggestion_text。"""
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None
    db_runner(lambda: _run(task.id))

    async def fetch() -> list[dict[str, Any]]:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(RuleHit.risk_level, RuleHit.suggestion_text, RuleHit.hit_status).where(
                        RuleHit.task_id == task.id
                    )
                )
            ).all()
        return [dict(row._mapping) for row in rows]

    rows = db_runner(fetch)
    assert len(rows) == 11
    for row in rows:
        assert row["risk_level"] in {"low", "medium", "high"}
        assert row["suggestion_text"], "快照建议不得为空"
        assert row["hit_status"] in {"hit", "miss", "uncertain"}


# ── AC10：整体风险等级 = 手算 RL-AGG ──────────────────────────────────────


def test_overall_risk_equals_hand_computed_rl_agg(live_db: str, db_runner) -> None:
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None
    result = db_runner(lambda: _run(task.id))

    # 手算：H = hit 集合；存在 high → high；否则 medium → medium；否则 low
    hand = aggregate_overall_risk(
        [{"hit_status": row["hit_status"], "risk_level": row["risk_level"]} for row in result.rule_hits]
    )
    assert result.overall_risk_level == hand
    assert result.overall_risk_level == "high"  # AP-001 期望整体 high（SD-01）

    # uncertain 与 miss 一律不得参与等级计算（RL-AGG-01）
    uncertain_or_miss = [
        row for row in result.rule_hits if row["hit_status"] != HitStatus.HIT.value
    ]
    assert uncertain_or_miss, "AP-001 在 LLM 关闭时应存在 uncertain"
    assert aggregate_overall_risk(uncertain_or_miss) == "low"


def test_hit_and_uncertain_counts(live_db: str, db_runner) -> None:
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None
    result = db_runner(lambda: _run(task.id))
    hits = [row for row in result.rule_hits if row["hit_status"] == "hit"]
    uncertain = [row for row in result.rule_hits if row["hit_status"] == "uncertain"]
    assert result.hit_count == len(hits)
    assert result.uncertain_count == len(uncertain)


# ── SD-01 回归：与样例期望结果比对 ────────────────────────────────────────


def test_ap001_matches_expected_baseline(live_db: str, db_runner) -> None:
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None
    result = db_runner(lambda: _run(task.id))
    expected = EXPECTED["AP-001"]

    actual_hits = {row["rule_code"] for row in result.rule_hits if row["hit_status"] == "hit"}
    assert actual_hits == set(expected["expected_hits"])
    for code in expected["expected_not_hit"]:
        assert code not in actual_hits, f"{code} 不应命中（SD-01 期望）"
    assert result.overall_risk_level == expected["overall_risk_level"]


def test_ap002_has_zero_hits_and_low_risk(live_db: str, db_runner) -> None:
    """条款完备的样例**必须零命中**（SD-01 / 无误报）。"""
    task = db_runner(lambda: _task_of("AP-002"))
    assert task is not None
    result = db_runner(lambda: _run(task.id))
    expected = EXPECTED["AP-002"]

    assert result.hit_count == 0
    assert result.overall_risk_level == "low"
    actual_hits = {row["rule_code"] for row in result.rule_hits if row["hit_status"] == "hit"}
    assert actual_hits == set(expected["expected_hits"]) == set()
    assert result.focus_points == []
    assert result.summary_text == "本次自动审查未发现明确的高风险或中风险条款。"


# ── FR-RULE-07：重复执行覆盖旧命中，不产生重复行 ──────────────────────────


def test_rerun_overwrites_hits(live_db: str, db_runner) -> None:
    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None
    first = db_runner(lambda: _run(task.id))
    second = db_runner(lambda: _run(task.id))

    async def count() -> int:
        async with session_scope() as session:
            return int(
                await session.scalar(
                    select(func.count()).select_from(RuleHit).where(RuleHit.task_id == task.id)
                )
                or 0
            )

    assert db_runner(count) == 11  # 不是 22
    assert [row["rule_code"] for row in first.rule_hits] == [
        row["rule_code"] for row in second.rule_hits
    ]


# ── FR-RULE-06：单条规则异常禁止中断其他规则 ──────────────────────────────


def test_single_rule_failure_does_not_abort_others(
    live_db: str, db_runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.modules.rules.service as service_module
    from app.modules.rules.predicates import resolve_handler as real_resolve

    def exploding_resolver(rule: Any) -> Any:
        if getattr(rule, "rule_code", "") == "R001":
            async def _boom(ctx: Any, _rule: Any) -> Any:
                raise RuntimeError("模拟规则实现缺陷")

            return _boom
        return real_resolve(rule)

    monkeypatch.setattr(service_module, "resolve_handler", exploding_resolver)

    task = db_runner(lambda: _task_of("AP-001"))
    assert task is not None
    result = db_runner(lambda: _run(task.id))

    assert result.evaluated_rules == 11  # 其余 10 条照常执行
    by_code = {row["rule_code"]: row for row in result.rule_hits}
    assert by_code["R001"]["hit_status"] == "uncertain"
    assert "异常" in by_code["R001"]["reason"]
    assert result.warnings and "R001" in result.warnings[0]

    # 修好之后重跑应恢复命中（覆盖旧结果）
    monkeypatch.undo()
    recovered = db_runner(lambda: _run(task.id))
    assert {row["rule_code"]: row for row in recovered.rule_hits}["R001"]["hit_status"] == "hit"


# ── IF-05 约束：404 / 409 ────────────────────────────────────────────────


def test_run_rules_requires_parsed_document(live_db: str, db_runner) -> None:
    """AP-004（附件缺失、无解析记录）→ 409 PARSE_REQUIRED（SPEC IF-05）。"""
    task = db_runner(lambda: _task_of("AP-004"))
    assert task is not None
    with pytest.raises(AppError) as excinfo:
        db_runner(lambda: run_contract_rules(task.id, llm=NullLLMClient()))
    assert excinfo.value.code is ErrorCode.PARSE_REQUIRED
    assert excinfo.value.http_status == 409


def test_run_rules_unknown_case_raises_not_found(live_db: str, db_runner) -> None:
    with pytest.raises(AppError) as excinfo:
        db_runner(lambda: run_contract_rules(99_999_999, llm=NullLLMClient()))
    assert excinfo.value.code is ErrorCode.APPROVAL_NOT_FOUND
    assert excinfo.value.http_status == 404


# ── 内部 REST：IF-15 / IF-16 ─────────────────────────────────────────────


@pytest.fixture
def api_client(mock_app: Any) -> Any:
    """带鉴权的 TestClient；审批系统走内存 ASGI，**LLM 注入空实现**（不产生真实调用）。"""
    client = ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))
    service_app.dependency_overrides[get_approval_client] = lambda: client
    service_app.dependency_overrides[get_llm_client] = lambda: NullLLMClient()
    with TestClient(service_app) as test_client:
        yield test_client
    service_app.dependency_overrides.clear()


@pytest.fixture
def ap002_task_id(db_runner) -> int:
    """清掉 AP-002 的既有命中，并返回其 task_id。

    注意：所有 ``db_runner`` 调用都在 **TestClient 创建之前**完成——
    engine 是进程级单例，跨事件循环复用在测试里会炸
    （"Future attached to a different loop"）。
    """
    task = db_runner(lambda: _task_of("AP-002"))
    assert task is not None

    async def clear_hits() -> None:
        async with session_scope() as session:
            await session.execute(delete(RuleHit).where(RuleHit.task_id == task.id))
            await session.commit()

    db_runner(clear_hits)
    return task.id


def test_if15_returns_409_before_any_run(ap002_task_id: int, mock_app: Any) -> None:
    """未执行过审查 → 409（消息说明应先触发审查）。"""
    headers = {"X-API-Key": get_settings().internal_api_key}
    with TestClient(service_app) as client:
        response = client.get(f"/api/tasks/{ap002_task_id}/review", headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PARSE_REQUIRED"
    assert "尚未执行规则审查" in response.json()["error"]["message"]


def test_if16_and_if15_via_rest(api_client: Any, ap002_task_id: int) -> None:
    headers = {"X-API-Key": get_settings().internal_api_key}

    triggered = api_client.post(f"/api/tasks/{ap002_task_id}/review", headers=headers)
    assert triggered.status_code == 200
    body = triggered.json()
    assert body["case_id"] == ap002_task_id
    assert body["evaluated_rules"] == 11
    assert body["overall_risk_level"] == "low"

    fetched = api_client.get(f"/api/tasks/{ap002_task_id}/review", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["overall_risk_level"] == "low"
    assert len(fetched.json()["rule_hits"]) == 11

    assert api_client.get(f"/api/tasks/{ap002_task_id}/review").status_code == 401
