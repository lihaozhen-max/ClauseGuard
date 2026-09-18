"""M4 评论模板渲染用例（AC14、TS-11、SPEC §4.6.1）。

§4.6.1 是**规范文本**，因此这里用**逐字比对的黄金串**校验渲染结果——
措辞有任何改动都会被这组用例挡下来。
"""

from __future__ import annotations

import pytest

from app.core.enums import RISK_LEVEL_ZH
from app.modules.comment.service import build_idempotency_key, validate_comment_text
from app.modules.review.comment import DISCLAIMER, render_comment

DISCLAIMER_LINE = "以上结果由合同审查系统自动生成，仅供审批人员参考，最终审批结论由审批人员判断。"


def hit(
    code: str,
    level: str,
    *,
    position: str | None,
    suggestion: str,
    source: str = "rule",
) -> dict[str, object]:
    return {
        "rule_code": code,
        "rule_name": f"{code} 规则",
        "risk_level": level,
        "hit_status": "hit",
        "hit_source": source,
        "evidence_text": "合同原文片段" if position else None,
        "evidence_position": position,
        "suggestion": suggestion,
    }


HITS_WITH_EVIDENCE = [
    hit("R001", "high", position="第1页 第2段", suggestion="建议降低预付款比例或增加履约保障措施"),
    hit("R008", "medium", position=None, suggestion="建议补充保密条款"),
    hit("R002", "medium", position="第1页 第5段", suggestion="建议缩短付款周期"),
]


def test_comment_matches_spec_template_with_hits() -> None:
    """AC14：严格符合 §4.6.1 模板（含标题、等级、摘要、编号关注点、免责声明）。"""
    text = render_comment(
        overall_risk_level="high",
        summary_text="本合同存在较高预付款比例风险。",
        hits=HITS_WITH_EVIDENCE,
        focus_by_rule={"R001": "确认80%预付款是否符合公司付款政策", "R008": "补充保密条款", "R002": "核对付款账期"},
    )
    expected = "\n".join(
        [
            "【合同自动审查结果】",
            "",
            "整体风险等级：高",
            "",
            "风险摘要：",
            "本合同存在较高预付款比例风险。",
            "",
            "重点关注：",
            "",
            "1. 确认80%预付款是否符合公司付款政策",
            "证据：第1页 第2段。",
            "",
            "2. 补充保密条款",
            "建议：建议补充保密条款。",
            "",
            "3. 核对付款账期",
            "证据：第1页 第5段。",
            "",
            DISCLAIMER_LINE,
        ]
    )
    assert text == expected


def test_comment_without_hits_matches_spec_template() -> None:
    text = render_comment(overall_risk_level="low", summary_text="忽略我", hits=[], focus_by_rule={})
    expected = "\n".join(
        [
            "【合同自动审查结果】",
            "",
            "整体风险等级：低",
            "",
            "本次自动审查未发现明确的高风险或中风险条款。",
            "",
            DISCLAIMER_LINE,
        ]
    )
    assert text == expected


def test_low_level_with_hits_uses_no_hits_template() -> None:
    """§4.6.1：low 走"无命中"模板（即使存在 low 级命中）。"""
    text = render_comment(
        overall_risk_level="low",
        summary_text="x",
        hits=[hit("R001", "low", position="第1页 第1段", suggestion="提示")],
        focus_by_rule={"R001": "提示"},
    )
    assert "本次自动审查未发现明确的高风险或中风险条款。" in text
    assert "重点关注" not in text


def test_missing_position_item_uses_suggestion_line() -> None:
    """§4.6.1 硬约束：缺失项（无原文可定位）必须改用"建议"行。"""
    text = render_comment(
        overall_risk_level="high",
        summary_text="s",
        hits=[hit("R008", "medium", position=None, suggestion="建议补充保密条款")],
        focus_by_rule={"R008": "补充保密条款"},
    )
    assert "1. 补充保密条款\n建议：建议补充保密条款。" in text
    assert "证据：" not in text


def test_evidence_position_never_empty_when_rendered() -> None:
    text = render_comment(
        overall_risk_level="high",
        summary_text="s",
        hits=HITS_WITH_EVIDENCE,
        focus_by_rule={},
    )
    for line in text.splitlines():
        if line.startswith("证据："):
            assert line != "证据：。"


def test_at_most_five_items_and_numbered_from_one() -> None:
    hits = [
        hit(f"R{i:03d}", "high", position=f"第1页 第{i}段", suggestion=f"建议{i}") for i in range(1, 9)
    ]
    text = render_comment(
        overall_risk_level="high", summary_text="s", hits=hits, focus_by_rule={}
    )
    assert "6. " not in text
    assert "1. " in text and "5. " in text
    assert text.count("证据：") == 5


def test_risk_level_rendered_in_chinese() -> None:
    """SPEC §2.2：面向用户时等级必须中文。"""
    for level, zh in RISK_LEVEL_ZH.items():
        hits = [hit("R001", level, position="第1页 第1段", suggestion="s")] if level != "low" else []
        text = render_comment(
            overall_risk_level=level, summary_text="s", hits=hits, focus_by_rule={"R001": "x"}
        )
        assert f"整体风险等级：{zh}" in text


def test_comment_ends_with_disclaimer_and_has_no_decision_phrases() -> None:
    text = render_comment(
        overall_risk_level="high",
        summary_text="s",
        hits=HITS_WITH_EVIDENCE,
        focus_by_rule={"R001": "x"},
    )
    assert text.strip().endswith(DISCLAIMER)
    for word in ("审批通过", "审批不通过", "建议批准", "建议拒绝"):
        assert word not in text


# ── FR-COM-07 守卫与幂等键 ────────────────────────────────────────────────


def test_validate_comment_text_accepts_template_output() -> None:
    text = render_comment(
        overall_risk_level="high",
        summary_text="s",
        hits=HITS_WITH_EVIDENCE,
        focus_by_rule={"R001": "x"},
    )
    assert validate_comment_text(text) is None


@pytest.mark.parametrize(
    ("text", "keyword"),
    [
        ("", "为空"),
        ("【合同自动审查结果】没有免责声明", "免责声明"),
        (f"建议批准\n{DISCLAIMER}", "替代人工决策"),
    ],
)
def test_validate_comment_text_rejects_bad_content(text: str, keyword: str) -> None:
    reason = validate_comment_text(text)
    assert reason is not None and keyword in reason


def test_idempotency_key_is_sha256_of_instance_and_review() -> None:
    import hashlib

    key = build_idempotency_key("AP-001", 7)
    assert key == hashlib.sha256(b"AP-001:7").hexdigest()
    assert len(key) == 64  # DT-07 VARCHAR(64)
    assert build_idempotency_key("AP-001", 7) != build_idempotency_key("AP-002", 7)
    assert build_idempotency_key("AP-001", 7) != build_idempotency_key("AP-001", 8)
