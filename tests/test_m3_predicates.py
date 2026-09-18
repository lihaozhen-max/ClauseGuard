"""M3 单条规则判定用例（TS-09）。

对 R001–R011 各给正例与反例，输入是**合成合同文本**（不依赖数据库、不依赖真实 LLM），
符合 SPEC RL-00-06「所有规则必须可独立测试」。
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import get_settings
from app.core.enums import HitSource, HitStatus, ParseMode
from app.llm.null import NullLLMClient
from app.modules.parser.fields import extract_fields
from app.modules.parser.models import ParsedDocument, TextBlock
from app.modules.rules.context import RuleContext
from app.modules.rules.evidence import MAX_EVIDENCE_LENGTH
from app.modules.rules.predicates import (
    HANDLERS,
    rule_acceptance_missing,
    rule_amount_missing,
    rule_auto_renewal,
    rule_breach_liability,
    rule_confidentiality_missing,
    rule_data_processing,
    rule_intellectual_property,
    rule_jurisdiction,
    rule_payment_period,
    rule_prepay_ratio,
    rule_subject_missing,
)

# ── 与 seed_rules.sql 一致的规则参数（单元测试只验逻辑，seed 本身由集成用例回归）──


def make_rule(code: str, **overrides: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "rule_code": code,
        "rule_name": f"{code} 测试规则",
        "risk_level": "high",
        "match_mode": "presence",
        "match_text": None,
        "match_params_json": {},
        "suggestion_text": f"{code} 的处理建议",
        "target_section": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


ACCEPTANCE_ELEMENTS = {
    "acceptance_time": [r"\d+\s*(?:个)?\s*(?:工作日|日|天|月|年)内", "期限", "时间"],
    # 与 database/seed_rules.sql 的 R011 保持一致：早期只认"进行验收"，导致
    # "甲方组织验收""由甲方验收"这类同样常见的写法被误判为"缺验收方式"
    "acceptance_method": [
        "方式", "流程", "程序", "书面", "报告", "检测", "抽检",
        "进行验收", "组织验收", "验收合格", "验收通过", "核查", r"由[^\n]{0,10}验收",
    ],
    "acceptance_criteria": ["标准", "指标", "规范", "技术规格", "合格标准"],
}
DATA_ELEMENTS = {
    "purpose": ["目的", "用于"],
    "scope": ["范围"],
    "security": ["安全措施", "加密", "访问控制", "脱敏"],
    "deletion": ["删除", "销毁", "清除"],
}

RULES = {
    "R001": make_rule("R001", match_mode="threshold", match_params_json={"max_ratio": 0.30},
                      suggestion_text="建议降低预付款比例或增加履约保障措施（如预付款保函）"),
    "R002": make_rule("R002", match_mode="threshold", risk_level="medium",
                      match_params_json={"max_days": 90}, suggestion_text="建议缩短付款周期"),
    "R003": make_rule(
        "R003", match_mode="keyword", risk_level="medium",
        match_text="自动续约,自动顺延,期满自动,默认续期,自动延长,无异议则续期,自动展期",
        match_params_json={"llm_stage": True, "llm_question": "是否存在默认自动续约安排？"},
        suggestion_text="建议取消默认自动续约，改为到期前书面确认续约",
    ),
    "R004": make_rule(
        "R004", match_mode="presence", target_section="breach",
        match_params_json={
            "llm_stage": True,
            "unequal_signals": ["单方", "不承担任何责任", "全部由乙方承担", "甲方不承担", "乙方承担全部"],
            "llm_question": "违约责任是否明显不对等？",
        },
        suggestion_text="建议调整为双方对等的违约责任条款",
    ),
    "R005": make_rule(
        "R005", match_mode="regex", risk_level="medium", target_section="dispute",
        match_text="(乙方|对方|供应商|卖方|供方)所在地",
        match_params_json={"adverse_terms": ["乙方所在地", "对方所在地", "供应商所在地", "卖方所在地", "供方所在地"]},
        suggestion_text="建议改为甲方所在地法院管辖或约定明确仲裁机构",
    ),
    "R006": make_rule("R006", match_params_json={"fields": ["party_a", "party_b"]},
                      suggestion_text="建议补充完整的签约主体与对方主体信息"),
    "R007": make_rule("R007", match_params_json={"fields": ["contract_amount", "currency"]},
                      suggestion_text="建议明确合同金额与币种"),
    "R008": make_rule("R008", risk_level="medium", match_params_json={"fields": ["confidentiality_clause"]},
                      suggestion_text="建议补充保密条款"),
    "R009": make_rule(
        "R009", match_mode="keyword", risk_level="medium",
        match_text="个人信息,数据采集,数据共享,数据处理,数据存储,数据传输,用户数据,数据安全",
        match_params_json={"llm_stage": True, "required_elements": DATA_ELEMENTS, "llm_question": "是否涉及数据处理？"},
        suggestion_text="建议补充数据处理的目的、范围、安全措施与数据删除义务",
    ),
    "R010": make_rule("R010", match_params_json={
        "llm_stage": True,
        "clear_signals": ["归甲方所有", "归甲方", "甲方享有", "归属甲方", "甲方独立享有"],
        "llm_question": "知识产权归属是否不明确？",
    }, suggestion_text="建议明确知识产权归属方"),
    "R011": make_rule("R011", match_params_json={
        "llm_stage": False, "required_elements": ACCEPTANCE_ELEMENTS,
    }, suggestion_text="建议补充验收时间、验收方式及验收标准"),
}


def evaluate(handler: Any, ctx: Any, code: str) -> Any:
    return asyncio.run(handler(ctx, RULES[code]))


# ── 合成合同 ──────────────────────────────────────────────────────────────

PAYMENT_80 = {
    1: [
        "第三条  付款方式",
        "1. 合同签订后三日内支付合同总额80%作为预付款。",
        "2. 剩余20%款项于设备全部到货后三十日内支付。",
    ]
}
PAYMENT_30 = {1: ["第三条  付款方式", "1. 合同签订后十日内支付合同总额30%作为预付款。"]}
PAYMENT_120 = {1: ["第三条  付款方式", "验收合格后120日内支付尾款。"]}
PAYMENT_NO_RATIO = {1: ["第三条  付款方式", "合同签订后支付部分款项，具体比例另行约定。"]}

SUBJECT_OK = {
    1: [
        "甲方（签约主体）：某某集团有限公司",
        "乙方（对方名称）：某某科技有限公司",
        "本合同总金额为人民币伍拾万元整（¥500000.00）。",
        "币种为人民币（CNY）。",
    ]
}
SUBJECT_MISSING = {1: ["本合同总金额为人民币伍拾万元整（¥500000.00）。", "币种为人民币（CNY）。"]}
SUBJECT_PARTIAL = {1: ["甲方（签约主体）：某某集团有限公司。", "本合同一式两份。"]}

BREACH_EQUAL = {1: ["第六条  违约责任", "任何一方违反本合同约定给对方造成损失的，均应承担赔偿责任，双方责任对等。"]}
BREACH_UNEQUAL = {
    1: ["第六条  违约责任", "因乙方原因造成甲方损失的，乙方承担全部赔偿责任；甲方不承担任何责任。"]
}

DISPUTE_ADVERSE = {1: ["第七条  争议解决", "协商不成的，向合同签订地即乙方所在地人民法院提起诉讼。"]}
DISPUTE_FAVOURABLE = {1: ["第七条  争议解决", "协商不成的，向甲方所在地人民法院提起诉讼。"]}

CONFIDENTIAL_MISSING = {1: ["第七条  争议解决", "协商不成的，向甲方所在地人民法院提起诉讼。"]}
CONFIDENTIAL_PRESENT = {1: ["第七条  保密条款", "双方对商业秘密承担保密义务，保密期限为三年。"]}

DATA_COMPLETE = {
    1: [
        "第八条  数据条款",
        "乙方仅可为实现本合同服务目的处理甲方数据。",
        "数据处理范围为甲方业务系统运行日志。",
        "乙方应采取加密存储、访问控制等安全措施。",
        "服务终止后乙方应在三十日内删除全部甲方数据。",
    ]
}
DATA_INCOMPLETE = {1: ["第八条  数据条款", "乙方可处理甲方数据，数据处理的具体安排另行约定。"]}

IP_MISSING = {1: ["第七条  争议解决", "协商不成的，向甲方所在地人民法院提起诉讼。"]}
IP_CLEAR = {1: ["第九条  知识产权", "本合同履行过程中形成的全部成果的知识产权归甲方所有。"]}
IP_UNCLEAR = {1: ["第九条  知识产权", "双方就知识产权归属另行协商确定。"]}

ACCEPTANCE_INCOMPLETE = {1: ["第五条  验收条款", "甲方验收合格后支付剩余款项。"]}
ACCEPTANCE_ORGANIZED = {
    1: [
        "第五条  验收条款",
        "甲方应在交付后10个工作日内组织验收。",
        "验收标准为附件二《验收标准与测试用例》所列指标。",
    ]
}
ACCEPTANCE_COMPLETE = {
    1: [
        "第五条  验收条款",
        "乙方应于每月5日前提交上月服务报告。",
        "甲方应在收到报告后10个工作日内进行验收。",
        "验收标准为附件三《服务验收标准》所列全部指标。",
        "甲方验收后应出具书面验收意见。",
    ]
}

RENEWAL_AUTO = {1: ["第六条  合同期限与续约", "本合同期满自动续约一年。"]}
RENEWAL_EXPLICIT = {1: ["第六条  合同期限与续约", "期满如需续约，须经双方书面确认后另行签署续约协议。"]}
RENEWAL_SILENT = {1: ["第六条  合同期限与续约", "本合同自2026年10月1日起生效，有效期一年。"]}


# ── RL-001 预付款比例 ────────────────────────────────────────────────────


def test_r001_hit_when_ratio_exceeds_threshold(rule_context_factory) -> None:
    outcome = evaluate(rule_prepay_ratio, rule_context_factory(PAYMENT_80), "R001")
    assert outcome.hit_status is HitStatus.HIT
    assert outcome.hit_source is HitSource.RULE
    assert outcome.evidence.text and "80%" in outcome.evidence.text
    assert outcome.evidence.position == "第1页 第2段"
    assert "80%" in outcome.reason


def test_r001_miss_when_ratio_equals_threshold(rule_context_factory) -> None:
    """RL-00-05：严格大于才命中，等于阈值不命中。"""
    outcome = evaluate(rule_prepay_ratio, rule_context_factory(PAYMENT_30), "R001")
    assert outcome.hit_status is HitStatus.MISS


def test_r001_uncertain_without_ratio(rule_context_factory) -> None:
    outcome = evaluate(rule_prepay_ratio, rule_context_factory(PAYMENT_NO_RATIO), "R001")
    assert outcome.hit_status is HitStatus.UNCERTAIN


def test_r001_threshold_falls_back_to_settings(rule_context_factory) -> None:
    """FR-RULE-08：``match_params_json`` 缺失时回落 CF 配置默认值。"""
    rule = make_rule("R001", match_params_json={})
    ctx = rule_context_factory(PAYMENT_80)
    outcome = asyncio.run(rule_prepay_ratio(ctx, rule))
    assert outcome.hit_status is HitStatus.HIT  # CF-09 默认 0.30 < 80%


# ── RL-002 付款周期 ──────────────────────────────────────────────────────


def test_r002_hit_and_miss(rule_context_factory) -> None:
    hit_outcome = evaluate(rule_payment_period, rule_context_factory(PAYMENT_120), "R002")
    assert hit_outcome.hit_status is HitStatus.HIT
    assert "120" in hit_outcome.reason

    miss_outcome = evaluate(rule_payment_period, rule_context_factory(PAYMENT_80), "R002")
    assert miss_outcome.hit_status is HitStatus.MISS  # 中文数字"三十日" → 30 天


# ── RL-003 自动续约 ──────────────────────────────────────────────────────


def test_r003_keyword_hit(rule_context_factory) -> None:
    outcome = evaluate(rule_auto_renewal, rule_context_factory(RENEWAL_AUTO), "R003")
    assert outcome.hit_status is HitStatus.HIT
    assert outcome.hit_source is HitSource.RULE
    assert "自动续约" in (outcome.evidence.text or "")


def test_r003_miss_when_explicit_confirmation_required(rule_context_factory) -> None:
    """RL-003 边界：需双方书面确认才续约 → miss（无 LLM 也可确定性判定）。"""
    outcome = evaluate(rule_auto_renewal, rule_context_factory(RENEWAL_EXPLICIT), "R003")
    assert outcome.hit_status is HitStatus.MISS


def test_r003_uncertain_without_llm(rule_context_factory) -> None:
    outcome = evaluate(rule_auto_renewal, rule_context_factory(RENEWAL_SILENT), "R003")
    assert outcome.hit_status is HitStatus.UNCERTAIN


def test_r003_llm_variant_hit(rule_context_factory, stub_llm) -> None:
    """无关键词时由 LLM 判语义变体，证据须回验通过。"""
    llm = stub_llm({"hit": True, "reason": "存在默认续期安排", "evidence_text": "本合同自2026年10月1日起生效，有效期一年。"})
    outcome = evaluate(rule_auto_renewal, rule_context_factory(RENEWAL_SILENT, llm=llm), "R003")
    assert outcome.hit_status is HitStatus.HIT
    assert outcome.hit_source is HitSource.LLM
    assert outcome.evidence.text in "本合同自2026年10月1日起生效，有效期一年。"


def test_r003_llm_judged_miss(rule_context_factory, stub_llm) -> None:
    llm = stub_llm({"hit": False, "reason": "没有默认续约"})
    outcome = evaluate(rule_auto_renewal, rule_context_factory(RENEWAL_SILENT, llm=llm), "R003")
    assert outcome.hit_status is HitStatus.MISS


# ── RL-004 违约责任 ──────────────────────────────────────────────────────


def test_r004_hit_when_breach_clause_missing(rule_context_factory) -> None:
    outcome = evaluate(rule_breach_liability, rule_context_factory(SUBJECT_OK), "R004")
    assert outcome.hit_status is HitStatus.HIT
    assert outcome.hit_source is HitSource.RULE
    assert outcome.evidence.text is None  # 缺失型命中无原文可引


def test_r004_miss_when_balanced(rule_context_factory, stub_llm) -> None:
    llm = stub_llm({"hit": False, "reason": "双方对等"})
    outcome = evaluate(rule_breach_liability, rule_context_factory(BREACH_EQUAL, llm=llm), "R004")
    assert outcome.hit_status is HitStatus.MISS


def test_r004_hit_when_llm_finds_imbalance(rule_context_factory, stub_llm) -> None:
    llm = stub_llm({"hit": True, "reason": "明显不对等", "evidence_text": "因乙方原因造成甲方损失的，乙方承担全部赔偿责任"})
    outcome = evaluate(rule_breach_liability, rule_context_factory(BREACH_UNEQUAL, llm=llm), "R004")
    assert outcome.hit_status is HitStatus.HIT
    assert outcome.hit_source is HitSource.LLM
    assert outcome.evidence.text
    assert outcome.evidence.position


def test_r004_rule_side_signal_when_llm_unavailable(rule_context_factory) -> None:
    """RL-004 边界：LLM 不可用但命中不对等信号 → 仍可按规则侧判命中。"""
    outcome = evaluate(rule_breach_liability, rule_context_factory(BREACH_UNEQUAL), "R004")
    assert outcome.hit_status is HitStatus.HIT
    assert outcome.hit_source is HitSource.RULE
    assert outcome.evidence.text
    signals = RULES["R004"].match_params_json["unequal_signals"]
    assert any(signal in outcome.evidence.text for signal in signals)
    assert "信号" in outcome.reason


def test_r004_uncertain_when_llm_unavailable_and_no_signal(rule_context_factory) -> None:
    outcome = evaluate(rule_breach_liability, rule_context_factory(BREACH_EQUAL), "R004")
    assert outcome.hit_status is HitStatus.UNCERTAIN


# ── RL-005 管辖地 ────────────────────────────────────────────────────────


def test_r005_hit_and_miss(rule_context_factory) -> None:
    hit_outcome = evaluate(rule_jurisdiction, rule_context_factory(DISPUTE_ADVERSE), "R005")
    assert hit_outcome.hit_status is HitStatus.HIT
    assert "乙方所在地" in hit_outcome.reason
    assert hit_outcome.evidence.position == "第1页 第2段"

    miss_outcome = evaluate(rule_jurisdiction, rule_context_factory(DISPUTE_FAVOURABLE), "R005")
    assert miss_outcome.hit_status is HitStatus.MISS


# ── RL-006 / RL-007 / RL-008 缺失型 ──────────────────────────────────────


def test_r006_hit_and_miss(rule_context_factory) -> None:
    hit_outcome = evaluate(rule_subject_missing, rule_context_factory(SUBJECT_MISSING), "R006")
    assert hit_outcome.hit_status is HitStatus.HIT
    assert hit_outcome.evidence.text is None  # 全缺 → 无原文

    partial = evaluate(rule_subject_missing, rule_context_factory(SUBJECT_PARTIAL), "R006")
    assert partial.hit_status is HitStatus.HIT
    assert partial.evidence.text  # 部分存在 → 取其原文

    ok = evaluate(rule_subject_missing, rule_context_factory(SUBJECT_OK), "R006")
    assert ok.hit_status is HitStatus.MISS


def test_r007_hit_and_miss(rule_context_factory) -> None:
    missing = {1: ["甲方（签约主体）：某某集团有限公司", "乙方（对方名称）：某某科技有限公司"]}
    assert evaluate(rule_amount_missing, rule_context_factory(missing), "R007").hit_status is HitStatus.HIT
    assert evaluate(rule_amount_missing, rule_context_factory(SUBJECT_OK), "R007").hit_status is HitStatus.MISS


def test_r008_hit_and_miss(rule_context_factory) -> None:
    assert evaluate(rule_confidentiality_missing, rule_context_factory(CONFIDENTIAL_MISSING), "R008").hit_status is HitStatus.HIT
    assert evaluate(rule_confidentiality_missing, rule_context_factory(CONFIDENTIAL_PRESENT), "R008").hit_status is HitStatus.MISS


# ── RL-009 数据处理 ──────────────────────────────────────────────────────


def test_r009_miss_when_all_four_elements_present(rule_context_factory) -> None:
    outcome = evaluate(rule_data_processing, rule_context_factory(DATA_COMPLETE), "R009")
    assert outcome.hit_status is HitStatus.MISS


def test_r009_hit_when_elements_incomplete(rule_context_factory) -> None:
    outcome = evaluate(rule_data_processing, rule_context_factory(DATA_INCOMPLETE), "R009")
    assert outcome.hit_status is HitStatus.HIT
    assert "scope" in outcome.reason or "范围" in outcome.reason
    assert outcome.evidence.text


def test_r009_uncertain_without_keyword_and_llm(rule_context_factory) -> None:
    outcome = evaluate(rule_data_processing, rule_context_factory(SUBJECT_OK), "R009")
    assert outcome.hit_status is HitStatus.UNCERTAIN


def test_r009_llm_detects_semantic_variant(rule_context_factory, stub_llm) -> None:
    llm = stub_llm(
        {
            "hit": True,
            "reason": "涉及用户行为数据采集",
            "evidence_text": "本合同总金额为人民币伍拾万元整（¥500000.00）。",
        }
    )
    outcome = evaluate(rule_data_processing, rule_context_factory(SUBJECT_OK, llm=llm), "R009")
    assert outcome.hit_status is HitStatus.HIT
    assert outcome.hit_source is HitSource.LLM


# ── RL-010 知识产权 ──────────────────────────────────────────────────────


def test_r010_hit_when_clause_missing(rule_context_factory) -> None:
    outcome = evaluate(rule_intellectual_property, rule_context_factory(IP_MISSING), "R010")
    assert outcome.hit_status is HitStatus.HIT
    assert outcome.hit_source is HitSource.RULE
    assert outcome.evidence.text is None


def test_r010_miss_when_ownership_clear(rule_context_factory) -> None:
    """RL-010 边界：明确"成果归甲方所有" → miss（无 LLM 也可判定）。"""
    outcome = evaluate(rule_intellectual_property, rule_context_factory(IP_CLEAR), "R010")
    assert outcome.hit_status is HitStatus.MISS


def test_r010_uncertain_when_unclear_and_no_llm(rule_context_factory) -> None:
    outcome = evaluate(rule_intellectual_property, rule_context_factory(IP_UNCLEAR), "R010")
    assert outcome.hit_status is HitStatus.UNCERTAIN


def test_r010_llm_hit_when_unclear(rule_context_factory, stub_llm) -> None:
    llm = stub_llm({"hit": True, "reason": "归属未明确", "evidence_text": "双方就知识产权归属另行协商确定。"})
    outcome = evaluate(rule_intellectual_property, rule_context_factory(IP_UNCLEAR, llm=llm), "R010")
    assert outcome.hit_status is HitStatus.HIT
    assert outcome.hit_source is HitSource.LLM


# ── RL-011 验收标准 ──────────────────────────────────────────────────────


def test_r011_hit_when_elements_missing(rule_context_factory) -> None:
    outcome = evaluate(rule_acceptance_missing, rule_context_factory(ACCEPTANCE_INCOMPLETE), "R011")
    assert outcome.hit_status is HitStatus.HIT
    assert "验收时间" in outcome.suggestion  # RL-011：建议必须指明缺失要素
    assert outcome.evidence.text


def test_r011_miss_when_all_three_elements_present(rule_context_factory) -> None:
    outcome = evaluate(rule_acceptance_missing, rule_context_factory(ACCEPTANCE_COMPLETE), "R011")
    assert outcome.hit_status is HitStatus.MISS


def test_r011_miss_when_acceptance_is_organized(rule_context_factory) -> None:
    """回归：「甲方组织验收」也算写明了验收方式。

    早期词表只认"进行验收"，于是"甲方应在交付后10个工作日内**组织验收**"被误判为
    缺验收方式 → 一份验收条款写得很清楚的合同被误报高风险（自查合同 T-02 实测踩到）。
    """
    outcome = evaluate(rule_acceptance_missing, rule_context_factory(ACCEPTANCE_ORGANIZED), "R011")
    assert outcome.hit_status is HitStatus.MISS


def test_r011_hit_when_clause_missing(rule_context_factory) -> None:
    outcome = evaluate(rule_acceptance_missing, rule_context_factory(SUBJECT_OK), "R011")
    assert outcome.hit_status is HitStatus.HIT
    assert outcome.evidence.text is None


# ── 证据规范（RL-00-02、PS-10/11）────────────────────────────────────────


def test_evidence_is_contiguous_substring_and_located(rule_context_factory) -> None:
    ctx = rule_context_factory(PAYMENT_80)
    outcome = evaluate(rule_prepay_ratio, ctx, "R001")
    assert outcome.evidence.text in ctx.document.full_text
    assert re.match(r"^第\d+页 第\d+段$", outcome.evidence.position)


def test_evidence_truncated_to_300_chars(rule_context_factory) -> None:
    """RL-00-02：证据超过 300 字符必须截断并加省略号。"""
    long_sentence = "合同签订后支付预付款80%，" + "并在验收合格后支付其余款项，" * 30 + "双方另行确认"
    doc = {1: ["第三条  付款方式", long_sentence]}
    ctx = rule_context_factory(doc)
    outcome = evaluate(rule_prepay_ratio, ctx, "R001")
    assert outcome.hit_status is HitStatus.HIT
    assert outcome.evidence.text is not None
    assert len(outcome.evidence.text) == MAX_EVIDENCE_LENGTH
    assert outcome.evidence.text.endswith("…")
    # 截断前的前缀仍必须是原文连续子串（与 FR-PARSE-06 同一口径）
    assert outcome.evidence.text[:-1] in ctx.document.full_text


def test_llm_evidence_failing_verification_downgrades_to_uncertain(rule_context_factory, stub_llm) -> None:
    """PS-11：LLM 命中的证据回验失败 → 整条降级 uncertain（禁止采信模型生成的文本）。"""
    llm = stub_llm({"hit": True, "reason": "模型自己编的依据", "evidence_text": "这段文字在合同里根本不存在。"})
    outcome = evaluate(
        rule_auto_renewal, rule_context_factory(RENEWAL_SILENT, llm=llm), "R003"
    )
    assert outcome.hit_status is HitStatus.UNCERTAIN
    assert outcome.hit_source is HitSource.LLM
    assert "回验失败" in outcome.reason


def test_llm_crash_is_contained(rule_context_factory, stub_llm) -> None:
    """LM-14：LLM 抛异常也必须能给出结论（降级 uncertain），不能把异常抛给上层。"""
    llm = stub_llm(None, raise_error=True)
    outcome = evaluate(rule_auto_renewal, rule_context_factory(RENEWAL_SILENT, llm=llm), "R003")
    assert outcome.hit_status is HitStatus.UNCERTAIN


# ── 证据位置：OCR 模式 ───────────────────────────────────────────────────


def test_evidence_position_in_ocr_mode() -> None:
    """§2.6：OCR 模式的 position 必须是 ``第N页 区域(x,y)``。"""
    text = "第二条付款方式\n合同签订后三日内支付合同总额80%作为预付款。"
    blocks = [
        TextBlock(1, 1, "第二条付款方式", 0, 8, bbox=(173, 877)),
        TextBlock(1, 2, "合同签订后三日内支付合同总额80%作为预付款。", 9, len(text), bbox=(179, 943)),
    ]
    document = ParsedDocument(text, blocks, ParseMode.OCR, 1)
    basic, clauses, _ = extract_fields(document)
    ctx = RuleContext.build(
        document,
        [record.to_dict() for record in basic],
        [record.to_dict() for record in clauses],
        get_settings(),
        NullLLMClient(),
    )
    outcome = evaluate(rule_prepay_ratio, ctx, "R001")
    assert outcome.hit_status is HitStatus.HIT
    assert re.match(r"^第1页 区域\(\d+,\d+\)$", outcome.evidence.position or "")
    assert outcome.evidence.position == "第1页 区域(179,943)"


# ── 处理器注册表 ─────────────────────────────────────────────────────────


def test_all_eleven_rules_have_handlers() -> None:
    for index in range(1, 12):
        assert f"R{index:03d}" in HANDLERS, f"R{index:03d} 缺少专用处理器"
