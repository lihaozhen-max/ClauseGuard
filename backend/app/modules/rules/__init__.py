"""Rule Engine — 规则加载 / 5 种匹配 / 命中保存 / 风险汇总（M3）。

对应 SPEC：FR-RULE-01…FR-RULE-11、RL-001…RL-011、RL-AGG、IF-05。
落库：``rule_hits``（``UNIQUE(task_id, rule_id)``，命中时快照 risk_level/suggestion，FR-RULE-09）。

铁律：整体风险等级由 RL-AGG 算法决定，**禁止**由 LLM 决定（RL-AGG-03）；
证据必须由系统从 ``full_text`` 截取，**禁止**采用 LLM 生成的文本（FR-RULE-05、PS-10）。
"""
