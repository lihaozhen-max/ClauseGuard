-- ============================================================================
-- ClauseGuard 审查规则种子数据（SPEC §8 RL-001…RL-011）
--
-- 依据：
--   * DT-04 review_rules 表结构（M0 已建）
--   * §2.2 match_mode 只允许 regex/keyword/threshold/presence/llm_semantic 五个值
--   * SPEC §8 中形如 "presence + llm_semantic" 的多阶段规则，本文件存**确定性主模式**，
--     LLM 阶段与要素清单放在 match_params_json 里（列语义见 §2.2，禁止扩展枚举）
--
-- 幂等：按 rule_code 唯一键 upsert，可重复执行（管理员改过建议/等级后重跑会覆盖为基线）。
-- 阈值默认值也可由 .env 的 RULE_PREPAY_MAX_RATIO / RULE_PAYMENT_MAX_DAYS 回落（FR-RULE-08）。
-- ============================================================================

USE `clauseguard`;

INSERT INTO `review_rules`
    (`rule_code`, `rule_name`, `risk_level`, `rule_status`, `match_mode`,
     `match_text`, `match_params_json`, `suggestion_text`, `target_section`)
VALUES
-- RL-001 预付款比例风险（threshold，严格大于阈值才命中 RL-00-05）
('R001', '预付款比例风险', 'high', 'enabled', 'threshold',
 NULL,
 JSON_OBJECT('max_ratio', 0.30),
 '建议降低预付款比例或增加履约保障措施（如预付款保函）',
 'payment'),

-- RL-002 付款周期风险（threshold）
('R002', '付款周期风险', 'medium', 'enabled', 'threshold',
 NULL,
 JSON_OBJECT('max_days', 90),
 '建议缩短付款周期或增加分期付款节点',
 'payment'),

-- RL-003 自动续约风险（keyword 主 + llm_semantic 变体兜底）
('R003', '自动续约风险', 'medium', 'enabled', 'keyword',
 '自动续约,自动顺延,期满自动,默认续期,自动延长,无异议则续期,自动展期',
 JSON_OBJECT('llm_stage', TRUE,
             'llm_question', '合同是否存在"无需双方再次确认即可自动续期/顺延"的默认续约安排？'
                             '约定"须经双方书面确认后方可续约"的**不算**自动续约。'),
 '建议取消默认自动续约，改为到期前书面确认续约',
 NULL),

-- RL-004 违约责任风险（presence 主 + llm_semantic 判是否明显不对等）
('R004', '违约责任风险', 'high', 'enabled', 'presence',
 NULL,
 JSON_OBJECT('llm_stage', TRUE,
             'unequal_signals', JSON_ARRAY('单方', '不承担任何责任', '全部由乙方承担', '甲方不承担', '乙方承担全部'),
             'llm_question', '违约责任是否明显不对等（一方承担全部责任、另一方免责）？'
                             '双方对等赔偿应判定为不对等=false。'),
 '建议调整为双方对等的违约责任条款',
 'breach'),

-- RL-005 管辖地风险（regex）
('R005', '管辖地风险', 'medium', 'enabled', 'regex',
 '(乙方|对方|供应商|卖方|供方)所在地',
 JSON_OBJECT('adverse_terms',
             JSON_ARRAY('乙方所在地', '对方所在地', '供应商所在地', '卖方所在地', '供方所在地'),
             'favourable_terms',
             JSON_ARRAY('甲方所在地', '我司所在地', '合同签订地即甲方所在地')),
 '建议改为甲方所在地法院管辖或约定明确仲裁机构',
 'dispute'),

-- RL-006 主体信息缺失（presence）
('R006', '主体信息缺失', 'high', 'enabled', 'presence',
 NULL,
 JSON_OBJECT('fields', JSON_ARRAY('party_a', 'party_b')),
 '建议补充完整的签约主体与对方主体信息',
 NULL),

-- RL-007 合同金额缺失（presence）
('R007', '合同金额缺失', 'high', 'enabled', 'presence',
 NULL,
 JSON_OBJECT('fields', JSON_ARRAY('contract_amount', 'currency')),
 '建议明确合同金额与币种，并保持大小写金额一致',
 NULL),

-- RL-008 保密条款缺失（presence）
('R008', '保密条款缺失', 'medium', 'enabled', 'presence',
 NULL,
 JSON_OBJECT('fields', JSON_ARRAY('confidentiality_clause')),
 '建议补充保密条款，明确保密范围、期限与违约责任',
 NULL),

-- RL-009 数据处理风险（keyword 主 + llm_semantic）
('R009', '数据处理风险', 'medium', 'enabled', 'keyword',
 '个人信息,数据采集,数据共享,数据处理,数据存储,数据传输,用户数据,数据安全',
 JSON_OBJECT('llm_stage', TRUE,
             'required_elements', JSON_OBJECT(
                 'purpose', JSON_ARRAY('目的', '用于'),
                 'scope', JSON_ARRAY('范围'),
                 'security', JSON_ARRAY('安全措施', '加密', '访问控制', '脱敏'),
                 'deletion', JSON_ARRAY('删除', '销毁', '清除')),
             'llm_question', '合同是否实际涉及个人信息的采集、共享、处理或存储？'),
 '建议补充数据处理的目的、范围、安全措施与数据删除义务',
 NULL),

-- RL-010 知识产权风险（presence 主 + llm_semantic 判归属是否明确）
('R010', '知识产权风险', 'high', 'enabled', 'presence',
 NULL,
 JSON_OBJECT('llm_stage', TRUE,
             'clear_signals', JSON_ARRAY('归甲方所有', '归甲方', '甲方享有', '归属甲方', '甲方独立享有'),
             'llm_question', '知识产权归属是否不明确（存在风险）？'
                             '已明确写清归属方（如"成果归甲方所有"）应判定为 hit=false。'),
 '建议明确知识产权归属方、使用许可范围与后续改进成果归属',
 'ip'),

-- RL-011 验收标准缺失（presence 主 + keyword 判三要素）
('R011', '验收标准缺失', 'high', 'enabled', 'presence',
 NULL,
 JSON_OBJECT('llm_stage', FALSE,
             'required_elements', JSON_OBJECT(
                 'acceptance_time', JSON_ARRAY('\\d+\\s*(?:个)?\\s*(?:工作日|日|天|月|年)内', '期限', '时间'),
                 'acceptance_method', JSON_ARRAY('方式', '流程', '程序', '书面', '报告', '检测', '抽检',
                                                 '进行验收', '组织验收', '验收合格', '验收通过', '核查',
                                                 '由[^\\n]{0,10}验收'),
                 'acceptance_criteria', JSON_ARRAY('标准', '指标', '规范', '技术规格', '合格标准'))),
 '建议补充验收时间、验收方式及验收标准',
 'acceptance')

ON DUPLICATE KEY UPDATE
    `rule_name`         = VALUES(`rule_name`),
    `risk_level`        = VALUES(`risk_level`),
    `rule_status`       = VALUES(`rule_status`),
    `match_mode`        = VALUES(`match_mode`),
    `match_text`        = VALUES(`match_text`),
    `match_params_json` = VALUES(`match_params_json`),
    `suggestion_text`   = VALUES(`suggestion_text`),
    `target_section`    = VALUES(`target_section`);
