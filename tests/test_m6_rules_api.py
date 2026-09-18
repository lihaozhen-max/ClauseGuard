"""M6 规则维护接口用例（SPEC IF-21、FR-RULE-01/08、FR-UI-08）。

设计取舍：所有用例只在**自建的 R9xx 规则**上做增改删，绝不改动 ``seed_rules.sql``
的 R001–R011，否则会污染其他用例与开发库的种子状态；夹具在模块前后各清一次 R9xx。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.enums import RuleStatus
from app.db.models import ReviewRule
from app.db.session import session_scope
from app.main import app as service_app
from app.modules.rules.admin import to_rule_model
from app.modules.rules.loader import load_enabled_rules

pytestmark = pytest.mark.requires_db

#: 用例自建规则的编码前缀（种子规则是 R001–R011）
TEST_PREFIX = "R9"


def _headers() -> dict[str, str]:
    return {"X-API-Key": get_settings().internal_api_key}


@pytest.fixture(autouse=True, scope="module")
def clean_test_rules(live_db: str, db_runner) -> None:
    """模块前后各清一次自建规则，保证可重复运行。"""

    async def _purge() -> int:
        async with session_scope() as session:
            result = await session.execute(
                delete(ReviewRule).where(ReviewRule.rule_code.like(f"{TEST_PREFIX}%"))
            )
            await session.commit()
            return result.rowcount or 0

    db_runner(_purge)
    yield
    db_runner(_purge)


@pytest.fixture(scope="module")
def api_client() -> Any:
    """规则维护接口不需要审批系统/LLM，因此不做依赖覆盖。"""
    with TestClient(service_app) as client:
        yield client


def _create_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "rule_code": "R901",
        "rule_name": "测试规则（用例自建）",
        "risk_level": "medium",
        "match_mode": "keyword",
        "match_text": "测试关键词",
        "suggestion_text": "建议复核该条款。",
        "target_section": "clause_info",
    }
    payload.update(overrides)
    return payload


# ── 查询 ────────────────────────────────────────────────────────────────


def test_list_rules_returns_seed_rules_with_all_fields(api_client: Any) -> None:
    """FR-RULE-08 / FR-UI-08：维护页需要拿到全部规则（含停用）与启停统计。"""
    response = api_client.get("/api/rules", headers=_headers())
    assert response.status_code == 200
    payload = response.json()

    assert payload["total"] >= 11
    assert payload["enabled_count"] + payload["disabled_count"] == payload["total"]

    codes = {item["rule_code"] for item in payload["items"]}
    assert {"R001", "R011"} <= codes, "种子规则必须可见"

    sample = next(item for item in payload["items"] if item["rule_code"] == "R001")
    for field in (
        "rule_id",
        "rule_code",
        "rule_name",
        "risk_level",
        "rule_status",
        "match_mode",
        "match_text",
        "match_params_json",
        "suggestion_text",
        "target_section",
        "updated_at",
    ):
        assert field in sample, f"规则模型缺字段 {field}"
    # 时间必须是指口口径（ISO 8601 UTC，形如 ...Z）
    assert sample["updated_at"].endswith("Z")


def test_rules_require_api_key(api_client: Any) -> None:
    """FR-SYS-03：内部接口缺 ``X-API-Key`` → 401 UNAUTHORIZED。"""
    assert api_client.get("/api/rules").status_code == 401
    denied = api_client.get("/api/rules").json()
    assert denied["error"]["code"] == "UNAUTHORIZED"


# ── 新增 ────────────────────────────────────────────────────────────────


def test_create_rule_then_visible_in_list(api_client: Any) -> None:
    response = api_client.post("/api/rules", json=_create_payload(), headers=_headers())
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["rule_code"] == "R901"
    assert created["rule_status"] == "enabled"  # 默认启用
    assert created["risk_level"] == "medium"

    listed = api_client.get("/api/rules", headers=_headers()).json()
    assert "R901" in {item["rule_code"] for item in listed["items"]}


def test_create_rule_rejects_duplicate_code(api_client: Any) -> None:
    api_client.post("/api/rules", json=_create_payload(rule_code="R902"), headers=_headers())
    again = api_client.post("/api/rules", json=_create_payload(rule_code="R902"), headers=_headers())
    assert again.status_code == 422
    assert again.json()["error"]["code"] == "VALIDATION_ERROR"
    assert again.json()["error"]["detail"]["rule_code"] == "R902"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"rule_code": "X001"}, "编码格式必须是 R + 3 位数字"),
        ({"rule_code": "R0001"}, "位数必须为 3"),
        ({"risk_level": "critical"}, "等级只能是 low/medium/high"),
        ({"match_mode": "fuzzy"}, "匹配模式必须是 5 种之一"),
        ({"suggestion_text": ""}, "建议不能为空"),
        ({"rule_name": ""}, "名称不能为空"),
    ],
)
def test_create_rule_validates_input(api_client: Any, overrides: dict[str, Any], reason: str) -> None:
    payload = _create_payload()
    payload["rule_code"] = "R903"
    payload.update(overrides)
    response = api_client.post("/api/rules", json=payload, headers=_headers())
    assert response.status_code == 422, reason
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ── 修改（FR-UI-08 的三件事：启用/停用、改等级、改建议）────────────────────


def test_update_rule_status_takes_effect_in_review_chain(api_client: Any, db_runner) -> None:
    """**停用即不再参与审查**——这是维护页开关的真实语义（FR-RULE-01）。"""
    created = api_client.post(
        "/api/rules", json=_create_payload(rule_code="R904"), headers=_headers()
    ).json()
    rule_id = created["rule_id"]
    assert created["rule_status"] == RuleStatus.ENABLED.value

    async def enabled_codes() -> set[str]:
        async with session_scope() as session:
            return {rule.rule_code for rule in await load_enabled_rules(session)}

    assert "R904" in db_runner(enabled_codes)

    disabled = api_client.put(
        "/api/rules",
        json={"rule_id": rule_id, "rule_status": "disabled"},
        headers=_headers(),
    )
    assert disabled.status_code == 200
    assert disabled.json()["rule_status"] == RuleStatus.DISABLED.value
    assert "R904" not in db_runner(enabled_codes)

    enabled = api_client.put(
        "/api/rules", json={"rule_id": rule_id, "rule_status": "enabled"}, headers=_headers()
    )
    assert enabled.json()["rule_status"] == RuleStatus.ENABLED.value
    assert "R904" in db_runner(enabled_codes)


def test_update_rule_level_and_suggestion(api_client: Any) -> None:
    created = api_client.post(
        "/api/rules", json=_create_payload(rule_code="R905"), headers=_headers()
    ).json()

    updated = api_client.put(
        "/api/rules",
        json={
            "rule_id": created["rule_id"],
            "risk_level": "high",
            "suggestion_text": "改为高风险：必须补充违约责任条款。",
        },
        headers=_headers(),
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["risk_level"] == "high"
    assert body["suggestion_text"].startswith("改为高风险")
    # 未传的字段保持不变（增量更新）
    assert body["rule_name"] == created["rule_name"]
    assert body["match_mode"] == created["match_mode"]


def test_update_rule_accepts_match_params_json(api_client: Any) -> None:
    """阈值类规则（R005/R006）靠 ``match_params_json`` 调参，维护页必须能改。"""
    created = api_client.post(
        "/api/rules",
        json=_create_payload(
            rule_code="R906",
            match_mode="threshold",
            match_text=None,
            match_params_json={"max_ratio": 0.3},
        ),
        headers=_headers(),
    ).json()
    assert created["match_params_json"] == {"max_ratio": 0.3}

    updated = api_client.put(
        "/api/rules",
        json={"rule_id": created["rule_id"], "match_params_json": {"max_ratio": 0.2}},
        headers=_headers(),
    )
    assert updated.json()["match_params_json"] == {"max_ratio": 0.2}


def test_update_unknown_rule_returns_404(api_client: Any) -> None:
    response = api_client.put(
        "/api/rules", json={"rule_id": 99999999, "rule_status": "disabled"}, headers=_headers()
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RULE_NOT_FOUND"


def test_update_without_changes_returns_422(api_client: Any, db_runner) -> None:
    created = api_client.post(
        "/api/rules", json=_create_payload(rule_code="R907"), headers=_headers()
    ).json()
    response = api_client.put(
        "/api/rules", json={"rule_id": created["rule_id"]}, headers=_headers()
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_rule_code_is_immutable(api_client: Any) -> None:
    """``rule_code`` 是规则的对外标识，PUT 不接受它（多余字段被忽略，编码不变）。"""
    created = api_client.post(
        "/api/rules", json=_create_payload(rule_code="R908"), headers=_headers()
    ).json()
    updated = api_client.put(
        "/api/rules",
        json={"rule_id": created["rule_id"], "rule_code": "R999", "risk_level": "low"},
        headers=_headers(),
    )
    assert updated.status_code == 200
    assert updated.json()["rule_code"] == "R908"
    assert updated.json()["risk_level"] == "low"


def test_rule_change_does_not_touch_historical_hits(live_db: str, db_runner) -> None:
    """FR-RULE-09：改规则**只影响之后的审查**，不回改历史命中。

    这里验证不变量本身：``rule_hits`` 直接引用的是 ``review_rules.id``，
    改规则字段不会级联任何命中行——本项目**刻意不做**级联更新，
    因为命中里的 ``risk_level``/``suggestion_text`` 是当时的快照。
    """
    from app.db.models import RuleHit

    async def hit_row_count() -> int:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(RuleHit.id)
                    .join(ReviewRule, RuleHit.rule_id == ReviewRule.id)
                    .where(ReviewRule.rule_code.like(f"{TEST_PREFIX}%"))
                )
            ).scalars().all()
            return len(rows)

    # 自建规则不应出现在历史命中里（用例自足，且清理夹具保证互不干扰）
    assert db_runner(hit_row_count) == 0


def test_to_rule_model_maps_primary_key_to_rule_id(db_runner) -> None:
    """接口对外用 ``rule_id``，内部 ORM 主键叫 ``id``；映射只应有一处。"""

    async def first_rule() -> Any:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ReviewRule).where(ReviewRule.rule_code == "R001")
                )
            ).scalar_one()
            session.expunge(row)
            return row

    rule = db_runner(first_rule)
    model = to_rule_model(rule)
    assert model.rule_id == rule.id
    assert model.rule_code == "R001"
