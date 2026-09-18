"""Parser Module — PDF/Word/OCR 解析、16 字段提取、条款识别、证据定位（M2）。

对应 SPEC：FR-PARSE-01…FR-PARSE-10、PS-01…PS-11、IF-04。
落库：``contract_parses``（``UNIQUE(task_id, attachment_id)``，``full_text`` 为证据回验基准）。
流水线（顺序固定，PS §9.1）：类型识别 → 文本提取 →（必要时）OCR → 清洗 → 字段提取 → 条款识别。
"""
