"""Logging Module — 8 类核心操作落 task_logs（M5）。

对应 SPEC：FR-LOG-01…FR-LOG-05。
落库：``task_logs``（8 类 log_type：pull/download/parse/ocr/extract/rule/save/write_comment，外加 retry）。

约束：log_content 必须已脱敏（禁止 API Key/Token/密码/连接串，FR-LOG-03）；
合同原文应当截断至 ≤ 500 字符（FR-LOG-04）。
依赖方向铁律：本模块禁止反向依赖任何业务模块（架构图 FIG-03）。
"""
