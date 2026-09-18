"""Attachment Module — 附件下载 / 落盘消毒 / SHA-256 校验（M2）。

对应 SPEC：FR-ATT-01…FR-ATT-06、IF-03。
落库：``approval_attachments``（``UNIQUE(task_id, attachment_code)``）。
安全：落盘目录按 ``task_id`` 隔离，文件名必须做路径穿越消毒（FR-ATT-03），
附件禁止通过任何静态路由对外暴露（FR-SYS-02）。
"""
