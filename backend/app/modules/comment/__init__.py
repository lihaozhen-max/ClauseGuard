"""Comment Module — 评论回写 / 回写日志 / 失败重试（M4）。

对应 SPEC：FR-COM-01…FR-COM-07、IF-07、ST-02。
落库：``comment_logs``（``UNIQUE(idempotency_key)``；每次状态变化追加一条，不覆盖）。

约束：回写失败禁止删除已产生的审查结果，仅置 `write_status=failed`，任务保持 `done`
（FR-COM-04 / ST-01-04）；已 `success` 再调用必须返回既有结果并标 `duplicate=true`（FR-COM-06）。
"""
