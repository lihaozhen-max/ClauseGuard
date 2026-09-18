"""Approval Module — 待办拉取 / 审批详情 / 任务去重（M1）。

对应 SPEC：FR-APP-01…FR-APP-06、IF-01、IF-02。
落库：``approval_tasks``（``UNIQUE(instance_id)`` 是去重的最终保障，FR-APP-03）。
"""
