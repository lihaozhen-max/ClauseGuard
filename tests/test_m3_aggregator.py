"""M3 风险汇总与摘要模板用例（AC10、TS-10、SPEC §8.13 RL-AGG、§10.4）。

纯函数，不依赖数据库，也不依赖 LLM——**整体风险等级禁止由 LLM 决定**（RL-AGG-03）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.enums import HitStatus, RiskLevel
from app.modules.review.summary import (
    EMPTY_SUMMARY_TEXT,
    FOCUS_POINT_LIMIT,
    build_template_focus_points,
    build_template_summary,
)
from app.modules.rules.aggregator import (
    active_hits,
    aggregate_overall_risk,
    count_by_level,
    count_by_status,
)


def hit_row(code: str, level: str) -> dict[str, str]:
    return {"rule_code": code, "rule_name": f"规则{code}", "hit_status": "hit", "risk_level": level}


def row(code: str, status: str, level: str) -> dict[str, str]:
    return {"rule_code": code, "rule_name": f"规则{code}", "hit_status": status, "risk_level": level}


# ── RL-AGG ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hits", "expected"),
    [
        # 存在 high → high
        ([hit_row("R001", "high"), hit_row("R005", "medium")], RiskLevel.HIGH.value),
        # 无 high 但存在 medium → medium
        ([hit_row("R003", "medium"), hit_row("R005", "medium")], RiskLevel.MEDIUM.value),
        # 只有 low → low
        ([hit_row("R001", "low")], RiskLevel.LOW.value),
        # RL-AGG-02：空集 → low
        ([], RiskLevel.LOW.value),
        # RL-AGG-01：uncertain / miss 不参与计算
        (
            [
                row("R001", "uncertain", "high"),
                row("R002", "miss", "medium"),
                row("R003", "uncertain", "medium"),
            ],
            RiskLevel.LOW.value,
        ),
        # 全 uncertain → low（不是 high）
        ([row(f"R{i:03d}", "uncertain", "high") for i in range(1, 12)], RiskLevel.LOW.value),
        # hit 的 high 与 uncertain 的 high 混在一起，只认 hit
        ([row("R001", "uncertain", "high"), hit_row("R008", "medium")], RiskLevel.MEDIUM.value),
    ],
)
def test_aggregate_overall_risk(hits: list[dict[str, str]], expected: str) -> None:
    assert aggregate_overall_risk(hits) == expected


def test_aggregate_accepts_rule_outcome_objects() -> None:
    """真实调用传的是 ``RuleOutcome`` 对象，不是 dict。"""
    outcomes = [
        SimpleNamespace(hit_status=HitStatus.HIT, risk_level="medium"),
        SimpleNamespace(hit_status=HitStatus.UNCERTAIN, risk_level="high"),
    ]
    assert aggregate_overall_risk(outcomes) == RiskLevel.MEDIUM.value


def test_counts() -> None:
    hits = [
        hit_row("R001", "high"),
        hit_row("R003", "medium"),
        hit_row("R005", "medium"),
        row("R004", "uncertain", "high"),
        row("R002", "miss", "medium"),
    ]
    assert count_by_status(hits) == {"hit": 3, "miss": 1, "uncertain": 1}
    assert count_by_level(hits) == {"high": 1, "medium": 2, "low": 0}
    assert [item["rule_code"] for item in active_hits(hits)] == ["R001", "R003", "R005"]
    assert all(item["hit_status"] == "hit" for item in active_hits(hits))


# ── §10.4 模板摘要与关注点 ────────────────────────────────────────────────


def test_summary_template_with_hits() -> None:
    hits = [hit_row("R001", "high"), hit_row("R003", "medium"), hit_row("R005", "medium")]
    text = build_template_summary(hits)
    assert text == "本合同共命中3项风险，其中高风险1项、中风险2项，主要涉及：规则R001、规则R003、规则R005。"


def test_summary_template_without_hits() -> None:
    hits = [row("R004", "uncertain", "high"), row("R002", "miss", "medium")]
    assert build_template_summary(hits) == EMPTY_SUMMARY_TEXT


def test_focus_points_sorted_by_level_and_capped() -> None:
    hits = [
        hit_row("R005", "medium"),
        hit_row("R001", "high"),
        hit_row("R011", "high"),
        hit_row("R008", "medium"),
        hit_row("R010", "high"),
        hit_row("R003", "medium"),
    ]
    for index, item in enumerate(hits):
        item["suggestion_text"] = f"建议{index}"
    points = build_template_focus_points(hits)
    assert len(points) == FOCUS_POINT_LIMIT
    # high 排在 medium 之前
    assert points[:3] == ["建议1", "建议2", "建议4"]


def test_focus_points_truncated_to_40_chars() -> None:
    long_suggestion = "建议" * 40
    hits = [hit_row("R001", "high")]
    hits[0]["suggestion_text"] = long_suggestion
    points = build_template_focus_points(hits)
    assert len(points[0]) == 40
    assert points[0].endswith("…")


def test_focus_points_empty_without_hits() -> None:
    assert build_template_focus_points([row("R004", "uncertain", "high")]) == []
