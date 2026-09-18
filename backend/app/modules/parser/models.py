"""解析阶段的内部数据结构。

设计要点：``full_text`` 是**证据回验的唯一基准**（PS-10），因此每个段落都记录它在
``full_text`` 中的精确偏移；段落同时携带页码与页内段号，用以生成 SPEC §2.6 的 ``position``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.enums import ParseMode


@dataclass
class TextBlock:
    """一个**逻辑段落**（不是视觉行）。

    ``text`` 是脱去折行后的段落原文；段落文本在 ``full_text`` 中占据 ``[start, end)``。
    """

    page_no: int
    para_no: int
    text: str
    start: int
    end: int
    bbox: tuple[int, int] | None = None  # OCR 区域左上角坐标（text 模式为 None）

    def position(self, parse_mode: ParseMode) -> str:
        """SPEC §2.6 的两种格式之一。"""
        if parse_mode is ParseMode.OCR:
            x, y = self.bbox if self.bbox else (0, 0)
            return f"第{self.page_no}页 区域({x},{y})"
        return f"第{self.page_no}页 第{self.para_no}段"


@dataclass
class ParsedDocument:
    """一份合同解析后的统一结构（PS-04 的产物）。"""

    full_text: str
    blocks: list[TextBlock]
    parse_mode: ParseMode
    page_count: int
    used_ocr_pages: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def page_map_json(self) -> dict[str, Any]:
        """DT-03 ``page_map_json``：页码 → 文本偏移，以及页内段落偏移。

        M3 的 RL-00-03 需要"由证据文本反查位置"，因此段落级偏移必须落库。
        """
        pages: dict[int, dict[str, Any]] = {}
        for block in self.blocks:
            page = pages.setdefault(
                block.page_no,
                {"page": block.page_no, "start": block.start, "end": block.end, "paragraphs": []},
            )
            page["end"] = max(page["end"], block.end)
            page["paragraphs"].append(
                {
                    "no": block.para_no,
                    "start": block.start,
                    "end": block.end,
                    **({"bbox": list(block.bbox)} if block.bbox else {}),
                }
            )
        return {
            "parse_mode": self.parse_mode.value,
            "page_count": self.page_count,
            "used_ocr_pages": self.used_ocr_pages,
            "pages": [pages[key] for key in sorted(pages)],
        }

    def locate(self, offset: int) -> TextBlock | None:
        """按偏移定位段落（M3 证据定位用）。"""
        for block in self.blocks:
            if block.start <= offset < block.end:
                return block
        return self.blocks[-1] if self.blocks else None

    def find_block_of(self, text: str) -> TextBlock | None:
        """按原文片段定位段落（要求 ``text`` 是 ``full_text`` 的子串，PS-10）。"""
        index = self.full_text.find(text)
        return self.locate(index) if index >= 0 else None

    @classmethod
    def from_stored(
        cls,
        full_text: str,
        page_map: dict[str, Any] | None,
        parse_mode: str | ParseMode = ParseMode.TEXT,
    ) -> ParsedDocument:
        """从库中的 ``full_text`` + ``page_map_json`` 重建可定位的文档（M3 证据定位用）。

        ``page_map_json`` 里存了每页/每段的字符偏移，因此重建出的 ``TextBlock``
        与解析时完全一致，位置反查（RL-00-03）无需重跑解析。
        """
        mode = parse_mode if isinstance(parse_mode, ParseMode) else ParseMode(parse_mode)
        page_map = page_map or {}
        blocks: list[TextBlock] = []
        for page in page_map.get("pages") or []:
            page_no = int(page.get("page") or 1)
            for index, paragraph in enumerate(page.get("paragraphs") or [], start=1):
                start = int(paragraph.get("start") or 0)
                end = int(paragraph.get("end") or start)
                bbox = paragraph.get("bbox")
                blocks.append(
                    TextBlock(
                        page_no=page_no,
                        para_no=int(paragraph.get("no") or index),
                        text=full_text[start:end],
                        start=start,
                        end=end,
                        bbox=(int(bbox[0]), int(bbox[1])) if bbox else None,
                    )
                )
        blocks.sort(key=lambda block: block.start)
        return cls(
            full_text=full_text,
            blocks=blocks,
            parse_mode=mode,
            page_count=int(page_map.get("page_count") or (blocks[-1].page_no if blocks else 1)),
            used_ocr_pages=list(page_map.get("used_ocr_pages") or []),
        )


@dataclass
class FieldRecord:
    """SPEC §2.4 的 5 键字段记录。"""

    field_name: str
    field_value: str | None
    source_text: str | None
    position: str | None
    extract_status: str

    def to_dict(self) -> dict[str, Any]:
        """固定 5 键，顺序与 SPEC §2.4 一致。"""
        return {
            "field_name": self.field_name,
            "field_value": self.field_value,
            "source_text": self.source_text,
            "position": self.position,
            "extract_status": self.extract_status,
        }

    @staticmethod
    def missing(field_name: str) -> FieldRecord:
        return FieldRecord(field_name, None, None, None, "missing")

    @staticmethod
    def failed(field_name: str, reason: str = "") -> FieldRecord:
        return FieldRecord(field_name, None, reason or None, None, "failed")
