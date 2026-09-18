"""解析结果模型（SPEC IF-04 / IF-13 / IF-14、§2.4 FieldRecord）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FieldRecordModel(BaseModel):
    """§2.4 的 5 键对象，一个字段一条，缺一不可。"""

    field_name: str
    field_value: str | None = None
    source_text: str | None = None
    position: str | None = None
    extract_status: str = Field(description="success / missing / failed")


class ParseResultResponse(BaseModel):
    """IF-04 / IF-13 / IF-14 的返回结构。

    ``basic_info`` 与 ``clause_info`` **必须各含 §2.5 全部条目**（各 8 条），缺失者以
    ``extract_status=missing`` 呈现，禁止缺项（SPEC IF-04 约束）。
    """

    document_id: int
    task_id: int
    parse_mode: str = Field(description="text / ocr，决定 position 格式（§2.6）")
    parse_status: str = Field(description="success / partial / failed")
    basic_info: list[FieldRecordModel] = Field(default_factory=list)
    clause_info: list[FieldRecordModel] = Field(default_factory=list)
    parse_error: str | None = None
    page_count: int = 0
    used_ocr_pages: list[int] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
