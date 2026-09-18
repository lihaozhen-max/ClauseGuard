"""模拟审批系统内置样例数据（SPEC §15 SD-01/SD-02）。

SD-02：样例**必须**为自造文本，**禁止**使用真实企业合同数据。
SD-01：每份样例**必须**随附其期望结果（命中哪些规则、整体等级），供 M3 回归比对；
       机器可读版本同时写入 ``sample_contracts/expected_results.json``。

附件文件位于 ``ClauseGuard/sample_contracts/``；AP-004 声明有附件但**文件不存在**，
用于覆盖"附件不存在 → CONTRACT_ATTACHMENT_MISSING"（FR-ATT-04 / AC16）。
"""

from __future__ import annotations

from typing import Any

#: 5 个样例审批单（FR-MOCK-03）
APPROVALS: list[dict[str, Any]] = [
    {
        "instance_id": "AP-001",
        "approval_code": "CG-2026-0001",
        "approval_title": "服务器采购合同审批",
        "applicant_name": "张三",
        "apply_time": "2026-09-10T02:30:00Z",
        "current_status": "pending",
        "contract_type": "采购合同",
        "form_data": {
            "amount": "500000",
            "currency": "CNY",
            "supplier": "某某科技有限公司",
            "department": "信息技术部",
            "purchase_type": "固定资产采购",
        },
        "attachments": [
            {
                "attachment_id": "ATT-001",
                "file_name": "AP-001_采购合同.pdf",
                "file_type": "pdf",
                "file_size": 0,  # 运行时按磁盘实际大小回填
            }
        ],
        # SD-01 期望结果（M3 实测校准）
        "expected": {
            "parse_mode": "text",
            "overall_risk_level": "high",
            "expected_hits": ["R001", "R003", "R005", "R008", "R010", "R011"],
            "expected_not_hit": ["R002", "R004", "R006", "R007", "R009"],
            "llm_dependent": ["R004", "R009"],
            "note": "80% 预付款(hit)、期满自动续约(hit)、验收无标准(hit)、乙方所在地管辖(hit)、"
            "缺保密/知识产权条款(hit)；R002 账期 30 天、R006/R007 主体金额齐全 → miss；"
            "R004（对等违约）与 R009（无数据处理）由 LLM 判定：LLM 开启时 miss，关闭时 uncertain"
            "——两者都不是 hit，故整体等级恒为 high",
        },
    },
    {
        "instance_id": "AP-002",
        "approval_code": "CG-2026-0002",
        "approval_title": "技术服务合同审批",
        "applicant_name": "李四",
        "apply_time": "2026-09-11T01:15:00Z",
        "current_status": "pending",
        "contract_type": "服务合同",
        "form_data": {
            "amount": "300000",
            "currency": "CNY",
            "supplier": "某某软件服务有限公司",
            "department": "数字化中心",
            "service_period": "12个月",
        },
        "attachments": [
            {
                "attachment_id": "ATT-002",
                "file_name": "AP-002_服务合同.pdf",
                "file_type": "pdf",
                "file_size": 0,
            }
        ],
        "expected": {
            "parse_mode": "text",
            "overall_risk_level": "low",
            "expected_hits": [],
            "expected_not_hit": [f"R{i:03d}" for i in range(1, 12)],
            "llm_dependent": ["R004"],
            "note": "条款完备，**必须零命中**：30% 预付款(=阈值不命中，RL-00-05 严格大于)、"
            "30 天账期、验收三要素齐、甲方所在地管辖、对等违约、保密/数据(四要素齐)/"
            "知识产权(归甲方)条款齐备 → R009 亦为 miss；R004 由 LLM 判定，关闭时为 uncertain",
        },
    },
    {
        "instance_id": "AP-003",
        "approval_code": "CG-2026-0003",
        "approval_title": "设备采购合同审批（扫描件）",
        "applicant_name": "王五",
        "apply_time": "2026-09-12T03:40:00Z",
        "current_status": "pending",
        "contract_type": "采购合同",
        "form_data": {
            "amount": "200000",
            "currency": "CNY",
            "supplier": "某某科技有限公司",
            "department": "生产运营部",
        },
        "attachments": [
            {
                "attachment_id": "ATT-003",
                "file_name": "AP-003_扫描件.png",
                "file_type": "png",
                "file_size": 0,
            }
        ],
        "expected": {
            "parse_mode": "ocr",
            "overall_risk_level": "high",
            "expected_hits": ["R001", "R003", "R004", "R005", "R008", "R010", "R011"],
            "expected_not_hit": ["R002", "R006", "R007", "R009"],
            "llm_dependent": ["R009"],
            "note": "扫描图片必须走 OCR（AC05），关键特征与 AP-001 同类但文面简化；"
            "文面未写违约责任条款 → R004① 命中（hit_source=rule）；"
            "缺保密/知识产权条款 → R008/R010① 命中；支付账期仅 3 天、主体金额齐全 → R002/R006/R007 miss",
        },
    },
    {
        "instance_id": "AP-004",
        "approval_code": "CG-2026-0004",
        "approval_title": "办公用品采购合同审批",
        "applicant_name": "赵六",
        "apply_time": "2026-09-13T05:20:00Z",
        "current_status": "pending",
        "contract_type": "采购合同",
        "form_data": {
            "amount": "15000",
            "currency": "CNY",
            "supplier": "某某办公用品有限公司",
            "department": "行政部",
        },
        "attachments": [
            {
                # 审批单声称有附件，但 sample_contracts/ 下没有该文件 → 下载返回 404
                "attachment_id": "ATT-004",
                "file_name": "AP-004_办公用品采购合同.pdf",
                "file_type": "pdf",
                "file_size": 102400,
            }
        ],
        "expected": {
            "parse_mode": None,
            "overall_risk_level": None,
            "expected_hits": [],
            "expected_miss": [],
            "note": "附件不存在 → 任务 blocked / blocked_stage=parsing / "
            "error_code=CONTRACT_ATTACHMENT_MISSING（AC16）",
        },
    },
    {
        "instance_id": "AP-005",
        "approval_code": "CG-2026-0005",
        "approval_title": "框架协议审批（空文件）",
        "applicant_name": "孙七",
        "apply_time": "2026-09-14T06:05:00Z",
        "current_status": "pending",
        "contract_type": "框架协议",
        "form_data": {
            "amount": "0",
            "currency": "CNY",
            "supplier": "某某咨询有限公司",
            "department": "法务部",
        },
        "attachments": [
            {
                "attachment_id": "ATT-005",
                "file_name": "AP-005_空文件.pdf",
                "file_type": "pdf",
                "file_size": 0,
            }
        ],
        "expected": {
            "parse_mode": None,
            "overall_risk_level": None,
            "expected_hits": [],
            "expected_miss": [],
            "note": "0 字节文件 → 下载成功但解析为空 → blocked / EMPTY_CONTRACT_CONTENT"
            "（或 PARSE_FAILED）",
        },
    },
]

#: instance_id → 审批单
APPROVALS_BY_ID: dict[str, dict[str, Any]] = {item["instance_id"]: item for item in APPROVALS}
