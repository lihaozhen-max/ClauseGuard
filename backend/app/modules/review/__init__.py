"""Review Module — 风险汇总 / 中文摘要 / 审批关注点 / 评论文本生成（M4）。

对应 SPEC：FR-REV-01…FR-REV-07、IF-06、§4.6.1 评论模板、§10.4 模板降级文案。
落库：``review_results``（``UNIQUE(task_id)``，按 task_id upsert）。

约束：`uncertain` 命中禁止计入整体风险等级（FR-REV-06）；
摘要/关注点生成失败必须降级为模板，任务禁止因此转 blocked（FR-REV-07、LM-16）。
"""
