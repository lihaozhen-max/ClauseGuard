"""文本清洗与段落重建（SPEC PS-02 / PS-03）。

三个层次：

1. **段落来源**：文本型 PDF 直接用 PyMuPDF 的 block 作为段落候选（实测其切分与本项目的
   样例段落结构一致）；docx 用 ``python-docx`` 的段落；OCR 由文本框坐标按行/段聚类。
2. **清洗（PS-02）**：去除连续空白、页眉页脚重复行、孤行连字符；**禁止**改写正文语义。
3. **折行还原**：同一段落框内的多行按"上一行是否断句 + 下一行是否段落起始标志"合并，
   合并后段落文本在 ``full_text`` 中仍是连续子串——这是 PS-10 证据回验成立的前提。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.enums import ParseMode
from app.modules.parser.models import ParsedDocument, TextBlock

#: 断句符：段落最后一行以这些字符结尾时，说明段落已结束
PARAGRAPH_ENDERS = set("。！？!?；;：:）)】》」』\"'’")

#: 下一行"像新段落开头"的信号
_STRONG_START = re.compile(
    r"^(第[一二三四五六七八九十百零〇\d]+[条章节款项]"
    r"|[一二三四五六七八九十]+[、.]"
    r"|\d+[.、)）]"
    r"|[（(]\d+[)）]"
    r"|附[件录]"
    r"|[A-Za-z])"
)

#: "键：值"行（合同编号：xxx / 甲方：xxx）
_KEY_VALUE = re.compile(r"^[^：:\s]{1,24}[：:]")

#: 行尾孤行连字符（英文单词被断开）
_HYPHEN_BREAK = re.compile(r"(?<=[A-Za-z])-\s*$")

_WHITESPACE = re.compile(r"[ \t\u3000\xa0]+")

#: OCR 噪声：数字被空格切开（"8 0 %" → "80%"）
_OCR_DIGIT_SPACE_DIGIT = re.compile(r"(?<=\d) (?=\d)")
_OCR_DIGIT_SPACE_PERCENT = re.compile(r"(?<=\d) (?=[%％])")


def normalize_ocr_text(text: str) -> str:
    """OCR 专用后处理：修复数字被空格切开的常见识别噪声。

    实测 PP-OCRv6 会把 ``80%`` 识别成 ``8 0 %``、``20个`` 识别成 ``2 0个``；
    不修复会直接影响阈值类规则（R001/R002）与字段抽取。**只在 OCR 文本上使用**。
    """
    result = _OCR_DIGIT_SPACE_DIGIT.sub("", text)
    return _OCR_DIGIT_SPACE_PERCENT.sub("", result)


@dataclass
class RawBlock:
    """抽取阶段产出的段落候选（尚未清洗）。

    ``bboxes`` 与 ``lines`` 一一对应：OCR 模式下每个视觉行都有坐标，
    段落建立后取**首个视觉行**的坐标作为 ``position`` 中的 ``区域(x,y)``。
    """

    page_no: int
    lines: list[str] = field(default_factory=list)
    bboxes: list[tuple[int, int] | None] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.bboxes:
            self.bboxes = [None] * len(self.lines)


def collapse_whitespace(text: str) -> str:
    """连续空白折叠为单个空格并去首尾（PS-02）。"""
    return _WHITESPACE.sub(" ", text).strip()


def is_continuation(previous: str, following: str) -> bool:
    """判断 ``following`` 是否是 ``previous`` 的折行延续。"""
    if not previous:
        return False
    if previous[-1] in PARAGRAPH_ENDERS:
        return False
    if _STRONG_START.match(following) or _KEY_VALUE.match(following):
        return False
    return True


def _join_pair(previous: str, following: str) -> str:
    """连接两行：中文直接相接；拉丁字母/数字之间补一个空格。"""
    if previous and previous[-1].isascii() and previous[-1].isalnum():
        return f"{previous} {following}"
    return previous + following


def join_lines(lines: list[str]) -> str:
    """把段落框内的多行还原成一个段落（折行还原 + 孤行连字符处理）。"""
    if not lines:
        return ""
    merged = _HYPHEN_BREAK.sub("", lines[0])
    for line in lines[1:]:
        merged = _join_pair(merged, _HYPHEN_BREAK.sub("", line))
    return merged


def _drop_repeated_marginal_lines(pages: dict[int, list[list[str]]]) -> set[str]:
    """识别页眉/页脚：出现在 ≥2 页、且总在页首/页尾的短行。

    「总在页首/页尾」用"位于该页前两行或后两行"近似；只补充删除，不影响正文。
    """
    if len(pages) < 2:
        return set()
    edge_counter: dict[str, int] = {}
    for blocks in pages.values():
        flat = [line for block in blocks for line in block]
        edges = {collapse_whitespace(line) for line in flat[:2] + flat[-2:]}
        for text in edges:
            if text and len(text) <= 40:
                edge_counter[text] = edge_counter.get(text, 0) + 1
    return {text for text, count in edge_counter.items() if count >= 2}


def clean_blocks(raw_blocks: list[RawBlock]) -> list[RawBlock]:
    """按页清洗：折叠空白、丢弃空块、删除页眉页脚重复行。"""
    by_page: dict[int, list[RawBlock]] = {}
    for block in raw_blocks:
        kept: list[str] = []
        kept_boxes: list[tuple[int, int] | None] = []
        for index, line in enumerate(block.lines):
            cleaned = collapse_whitespace(line)
            if cleaned:
                kept.append(cleaned)
                kept_boxes.append(block.bboxes[index] if index < len(block.bboxes) else None)
        if kept:
            by_page.setdefault(block.page_no, []).append(
                RawBlock(block.page_no, kept, kept_boxes)
            )

    pages_lines = {page: [block.lines for block in blocks] for page, blocks in by_page.items()}
    marginal = _drop_repeated_marginal_lines(pages_lines)
    if not marginal:
        return [block for blocks in by_page.values() for block in blocks]

    result: list[RawBlock] = []
    for blocks in by_page.values():
        for block in blocks:
            lines: list[str] = []
            boxes: list[tuple[int, int] | None] = []
            for index, line in enumerate(block.lines):
                if line not in marginal:
                    lines.append(line)
                    boxes.append(block.bboxes[index] if index < len(block.bboxes) else None)
            if lines:
                result.append(RawBlock(block.page_no, lines, boxes))
    return result


def split_line_indices(lines: list[str]) -> list[list[int]]:
    """把段落框内的行切成若干段落的**下标分组**。

    正常情况下一个 block 就是一个段落（多行是折行）；若排版分析把整页并成一个 block
    （OCR 页就是这种情形），这里用三条信号兜底切分：断句符、段落起始标志、
    以及"明显短于该块最长行"（标题行通常明显短）。
    """
    if not lines:
        return []
    max_len = max(len(line) for line in lines)
    groups: list[list[int]] = []
    for index, line in enumerate(lines):
        if groups:
            previous = lines[groups[-1][-1]]
            looks_wrapped = not (max_len >= 24 and len(previous) < 0.6 * max_len)
            if looks_wrapped and is_continuation(previous, line):
                groups[-1].append(index)
                continue
        groups.append([index])
    return groups


def build_document(
    raw_blocks: list[RawBlock],
    parse_mode: ParseMode,
    page_count: int,
    used_ocr_pages: list[int] | None = None,
    warnings: list[str] | None = None,
) -> ParsedDocument:
    """清洗后组装 ``ParsedDocument``：段落编号、全文偏移、页码映射一次算清。"""
    cleaned = clean_blocks(raw_blocks)

    blocks: list[TextBlock] = []
    chunks: list[str] = []
    cursor = 0
    for page_no in sorted({block.page_no for block in cleaned}):
        para_no = 0
        for raw in (block for block in cleaned if block.page_no == page_no):
            for indices in split_line_indices(raw.lines):
                text = join_lines([raw.lines[index] for index in indices])
                if not text:
                    continue
                para_no += 1
                start = cursor
                chunks.append(text)
                cursor += len(text) + 1  # 段间以 "\n" 分隔
                blocks.append(
                    TextBlock(
                        page_no=page_no,
                        para_no=para_no,
                        text=text,
                        start=start,
                        end=start + len(text),
                        bbox=raw.bboxes[indices[0]] if indices[0] < len(raw.bboxes) else None,
                    )
                )

    return ParsedDocument(
        full_text="\n".join(chunks),
        blocks=blocks,
        parse_mode=parse_mode,
        page_count=page_count,
        used_ocr_pages=list(used_ocr_pages or []),
        warnings=list(warnings or []),
    )
