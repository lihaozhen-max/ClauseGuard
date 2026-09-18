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
    # ── 自查样例 T-01…T-04（附件在 sample_contracts/extra/）──────────────────
    # 这四张单子是为了"能拿自己的合同测"而加的：**一份合同一张单子**，审查结果互不覆盖，
    # 不用再借 AP-004 的槽位。附件名带子目录，下载时会被 sanitize_filename 消毒成纯文件名。
    {
        "instance_id": "AP-006",
        "approval_code": "CG-2026-0006",
        "approval_title": "设备租赁合同审批（自查 T-01）",
        "applicant_name": "周八",
        "apply_time": "2026-09-15T01:20:00Z",
        "current_status": "pending",
        "contract_type": "租赁合同",
        "form_data": {
            "amount": "240000",
            "currency": "CNY",
            "supplier": "某某设备租赁有限公司",
            "department": "信息技术部",
            "lease_term": "12个月",
        },
        "attachments": [
            {
                "attachment_id": "ATT-006",
                "file_name": "extra/T-01_设备租赁合同.pdf",
                "file_type": "pdf",
                "file_size": 0,  # 运行时按磁盘实际大小回填
            }
        ],
        "expected": {
            "parse_mode": "text",
            "overall_risk_level": "high",
            "expected_hits": ["R001", "R002", "R003", "R005", "R008", "R010", "R011"],
            "expected_not_hit": ["R006", "R007", "R009"],
            "llm_dependent": ["R004"],
            "note": "故意写差的合同：预付款50%(R001)、尾款120天(R002)、期满自动续约(R003)、"
            "乙方所在地管辖(R005)、缺保密(R008)、缺知识产权(R010)、验收无标准(R011)；"
            "主体与金额齐全故 R006/R007 miss；不涉数据处理故 R009 miss；"
            "R004 由 LLM 判定违约责任是否对等（关闭 LLM 时为 uncertain）",
        },
    },
    {
        "instance_id": "AP-007",
        "approval_code": "CG-2026-0007",
        "approval_title": "技术服务合同审批（自查 T-02）",
        "applicant_name": "吴九",
        "apply_time": "2026-09-16T02:40:00Z",
        "current_status": "pending",
        "contract_type": "服务合同",
        "form_data": {
            "amount": "360000",
            "currency": "CNY",
            "supplier": "某某软件技术有限公司",
            "department": "数字化中心",
            "service_period": "12个月",
        },
        "attachments": [
            {
                "attachment_id": "ATT-007",
                "file_name": "extra/T-02_技术服务合同.pdf",
                "file_type": "pdf",
                "file_size": 0,
            }
        ],
        "expected": {
            "parse_mode": "text",
            "overall_risk_level": "low",
            "expected_hits": [],
            "expected_not_hit": [
                "R001", "R002", "R003", "R004", "R005", "R006",
                "R007", "R008", "R009", "R010", "R011",
            ],
            "llm_dependent": ["R004"],
            "note": "条款齐备的对照组：预付款20%(≤30%故 R001 miss)、尾款30天、无自动续约、"
            "违约责任对等、向甲方所在地起诉、有保密与知识产权条款、"
            "验收三要素齐备（时间+**组织验收**+标准）→ 期望 **0 命中**",
        },
    },
    {
        "instance_id": "AP-008",
        "approval_code": "CG-2026-0008",
        "approval_title": "数据处理服务协议审批（自查 T-03）",
        "applicant_name": "郑十",
        "apply_time": "2026-09-17T03:10:00Z",
        "current_status": "pending",
        "contract_type": "服务合同",
        "form_data": {
            "amount": "180000",
            "currency": "CNY",
            "supplier": "某某信息技术有限公司",
            "department": "数据治理部",
            "involves_personal_data": "是",
        },
        "attachments": [
            {
                "attachment_id": "ATT-008",
                "file_name": "extra/T-03_数据处理服务协议.docx",
                "file_type": "docx",
                "file_size": 0,
            }
        ],
        "expected": {
            "parse_mode": "text",
            "overall_risk_level": "medium",
            "expected_hits": ["R009"],
            "expected_not_hit": ["R001", "R002", "R003", "R004", "R005", "R006", "R007", "R010", "R011"],
            "llm_dependent": [],
            "note": "**唯一覆盖 Word(.docx) 路径的样例**；命中 R009 数据处理风险："
            "含关键词「个人信息」且约定了目的/范围/安全措施，但**缺删除义务** → hit；"
            "标题写作「交付与验收」用于回归「一标题多字段」；预付款30% 未超阈值故 R001 miss",
        },
    },
    {
        "instance_id": "AP-009",
        "approval_code": "CG-2026-0009",
        "approval_title": "框架采购协议审批（自查 T-04）",
        "applicant_name": "王十一",
        "apply_time": "2026-09-18T00:30:00Z",
        "current_status": "pending",
        "contract_type": "框架协议",
        "form_data": {
            "currency": "CNY",
            "department": "行政部",
            "purchase_mode": "框架协议+订单",
        },
        "attachments": [
            {
                "attachment_id": "ATT-009",
                "file_name": "extra/T-04_框架采购协议.pdf",
                "file_type": "pdf",
                "file_size": 0,
            }
        ],
        "expected": {
            "parse_mode": "text",
            "overall_risk_level": "high",
            "expected_hits": ["R006", "R007"],
            "expected_not_hit": ["R002", "R003", "R004", "R005", "R008", "R009", "R010", "R011"],
            "llm_dependent": [],
            "note": "主体名称与金额都空着 → 命中 R006 主体信息缺失、R007 合同金额缺失；"
            "**R001 应为 uncertain**（既无金额、又不约定预付款，算不出比例 → 交人工确认），"
            "故它既不在 expected_hits 也不在 expected_not_hit 里；"
            "解析页会出现 party_a/party_b/contract_amount/currency 四条 missing",
        },
    },
]

#: instance_id → 审批单
APPROVALS_BY_ID: dict[str, dict[str, Any]] = {item["instance_id"]: item for item in APPROVALS}
