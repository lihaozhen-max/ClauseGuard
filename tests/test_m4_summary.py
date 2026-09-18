"""M4 摘要与关注点生成用例（AC11、AC12、TS-16 的摘要侧、SPEC §10.3/§10.4）。

不依赖数据库：直接对 ``generate_summary_and_focus`` 喂命中列表与 LLM 桩。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.llm.null import NullLLMClient
from app.modules.review.summarizer import (
    EMPTY_SUMMARY_TEXT,
    FOCUS_POINT_MAX_LENGTH,
    generate_summary_and_focus,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def hit(
    code: str,
    level: str,
    *,
    status: str = "hit",
    suggestion: str = "建议处理",
    position: str | None = "第1页 第1段",
) -> dict[str, Any]:
    return {
        "rule_code": code,
        "rule_name": f"{code} 规则",
        "risk_level": level,
        "hit_status": status,
        "hit_source": "rule",
        "evidence_text": "原文" if position else None,
        "evidence_position": position,
        "suggestion": suggestion,
    }


HITS = [
    hit("R001", "high", suggestion="建议降低预付款比例或增加履约保障措施（如预付款保函）"),
    hit("R008", "medium", position=None, suggestion="建议补充保密条款"),
    hit("R002", "medium", suggestion="建议缩短付款周期或增加分期付款节点"),
    hit("R004", "high", status="uncertain", suggestion="建议调整为双方对等的违约责任条款"),
]


# ── 无命中 ────────────────────────────────────────────────────────────────


def test_no_hits_uses_spec_template_without_calling_llm(stub_llm) -> None:
    llm = stub_llm({"summary_text": "不该被调用", "focus_points": []})
    result = run(
        generate_summary_and_focus(llm, [hit("R002", "medium", status="miss")], "low")
    )
    assert result.summary_text == EMPTY_SUMMARY_TEXT
    assert result.focus_points == []
    assert result.degraded is True
    assert llm.calls == []  # 省一次调用


# ── 模板降级（LM-16 / LM-18）─────────────────────────────────────────────


def test_null_client_falls_back_to_template() -> None:
    result = run(generate_summary_and_focus(NullLLMClient(), HITS, "high"))
    assert result.degraded is True
    assert "本合同共命中3项风险" in result.summary_text
    assert "高风险1项、中风险2项" in result.summary_text
    assert len(result.focus_points) == 3
    # uncertain 不计入摘要
    assert "违约责任" not in result.summary_text


def test_template_focus_aligned_with_active_hits() -> None:
    """回归：模板关注点必须按"仅命中项"对齐，否则会把 A 的文字配到 B 的证据行。"""
    result = run(generate_summary_and_focus(NullLLMClient(), HITS, "high"))
    assert result.focus_by_rule["R001"].startswith("建议降低预付款比例")
    assert result.focus_by_rule["R008"] == "建议补充保密条款"
    assert result.focus_by_rule["R002"].startswith("建议缩短付款周期")
    assert "R004" not in result.focus_by_rule  # uncertain 不参与


@pytest.mark.parametrize(
    "payload",
    [
        None,  # 调用失败
        {},  # 空对象
        {"summary_text": ""},  # 空摘要
        {"summary_text": "   "},
        {"summary_text": "建议批准本合同", "focus_points": []},  # 含审批结论
        {"focus_points": [{"rule_code": "R001", "text": "x"}]},  # 缺摘要
    ],
)
def test_degrade_when_llm_output_unusable(payload, stub_llm) -> None:
    result = run(generate_summary_and_focus(stub_llm(payload), HITS, "high"))
    assert result.degraded is True
    assert result.summary_text  # 模板摘要非空
    assert result.focus_by_rule  # 模板关注点齐备


def test_degrade_when_llm_raises(stub_llm) -> None:
    result = run(generate_summary_and_focus(stub_llm(None, raise_error=True), HITS, "high"))
    assert result.degraded is True
    assert "stub" in (result.error or "") or result.error is not None


# ── LLM 正常路径 ─────────────────────────────────────────────────────────


def test_llm_summary_and_focus_are_used(stub_llm) -> None:
    payload = {
        "summary_text": "本合同预付款比例偏高且缺少保密条款，需重点确认。",
        "focus_points": [
            {"rule_code": "R001", "text": "确认80%预付款是否符合公司付款政策"},
            {"rule_code": "R008", "text": "补充保密条款并明确期限"},
        ],
    }
    result = run(generate_summary_and_focus(stub_llm(payload), HITS, "high"))
    assert result.degraded is False
    assert result.summary_text == payload["summary_text"]
    assert result.focus_by_rule["R001"] == "确认80%预付款是否符合公司付款政策"
    assert result.focus_by_rule["R008"] == "补充保密条款并明确期限"
    # 未给出关注点的命中回落到模板文案，保证评论每个条目都有附件行
    assert result.focus_by_rule["R002"].startswith("建议缩短付款周期")
    assert len(result.focus_points) == 3


def test_llm_focus_with_unknown_rule_code_is_dropped(stub_llm) -> None:
    payload = {
        "summary_text": "摘要",
        "focus_points": [
            {"rule_code": "R999", "text": "凭空捏造的风险"},
            {"rule_code": "R001", "text": "确认预付款政策"},
        ],
    }
    result = run(generate_summary_and_focus(stub_llm(payload), HITS, "high"))
    assert "R999" not in result.focus_by_rule
    assert result.focus_by_rule["R001"] == "确认预付款政策"
    assert all("捏造" not in point for point in result.focus_points)


def test_llm_focus_is_truncated_to_40_chars(stub_llm) -> None:
    payload = {
        "summary_text": "摘要",
        "focus_points": [{"rule_code": "R001", "text": "请" * 80}],
    }
    result = run(generate_summary_and_focus(stub_llm(payload), HITS, "high"))
    assert len(result.focus_by_rule["R001"]) == FOCUS_POINT_MAX_LENGTH
    assert result.focus_by_rule["R001"].endswith("…")


def test_llm_focus_limited_to_five(stub_llm) -> None:
    hits = [hit(f"R{i:03d}", "high", suggestion=f"建议{i}") for i in range(1, 9)]
    payload = {
        "summary_text": "摘要",
        "focus_points": [{"rule_code": f"R{i:03d}", "text": f"关注{i}"} for i in range(1, 9)],
    }
    result = run(generate_summary_and_focus(stub_llm(payload), hits, "high"))
    assert len(result.focus_points) == 5
    assert len(result.focus_by_rule) == 5


def test_llm_prompt_contains_only_hits_and_no_evidence_text(stub_llm) -> None:
    """LM-05：证据原文不进提示词（避免模型复述/改编证据）。"""
    llm = stub_llm({"summary_text": "摘要", "focus_points": [{"rule_code": "R001", "text": "x"}]})
    run(generate_summary_and_focus(llm, HITS, "high"))
    user_prompt = llm.calls[0]["user"]
    assert "R001" in user_prompt
    assert "合同原文片段" not in user_prompt
    assert "第1页 第1段" not in user_prompt
    assert "R004" not in user_prompt  # uncertain 不进提示词
