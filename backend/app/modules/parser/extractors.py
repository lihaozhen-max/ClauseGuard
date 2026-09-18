"""文档文本抽取（SPEC FR-PARSE-01…FR-PARSE-04、PS-01…PS-05）。

流程严格按 §9.1 的顺序：**类型识别 → 文本提取 →（必要时）OCR → 文本清洗**
（清洗在 ``cleaning.py``，本模块只负责"拿到段落候选"）。

扫描页判定（PS-04）：单页可提取有效字符数低于 ``OCR_SCAN_PAGE_MIN_CHARS`` 即判为扫描页。
混合文档的 ``parse_mode`` 取 ``ocr``（PS-05，从严），以保证 ``position`` 格式唯一——
因此**文本页也会记录文本框坐标**，避免混合文档里出现"无坐标的 ocr 位置"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.enums import ParseMode
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.modules.parser.cleaning import RawBlock
from app.modules.parser.detect import DocKind, detect_document_kind
from app.modules.parser.ocr import OcrEngine, load_image_bgr, pixmap_to_bgr

logger = get_logger(__name__)

#: 扫描页转图分辨率（200 DPI：中文小字号也能稳定识别，实测可用）
OCR_RENDER_DPI = 200


@dataclass
class ExtractionResult:
    """抽取阶段的产物（尚未清洗、尚未组装全文）。"""

    blocks: list[RawBlock] = field(default_factory=list)
    page_count: int = 1
    used_ocr_pages: list[int] = field(default_factory=list)
    parse_mode: ParseMode = ParseMode.TEXT
    warnings: list[str] = field(default_factory=list)
    kind: DocKind = DocKind.UNKNOWN
    ocr_seconds: float = 0.0
    text_chars: int = 0


def _ocr_page_blocks(page_no: int, image) -> list[RawBlock]:
    """对一张图做 OCR，返回"整页一个 block、每行一条 line"的结构。

    整页放一个 block 是有意的：OCR 没有段落概念，段落切分交给
    ``cleaning.split_line_indices`` 用断句符/标题信号完成（与文本型共用一套逻辑）。
    """
    lines = OcrEngine.instance().recognize(image)
    if not lines:
        return []
    return [
        RawBlock(
            page_no=page_no,
            lines=[line.text for line in lines],
            bboxes=[line.bbox for line in lines],
        )
    ]


def _text_blocks_of_page(page, page_no: int) -> list[RawBlock]:
    """用 ``get_text("dict")`` 取文本块：每个块一组视觉行，**每行都带自己的坐标**。

    坐标在混合文档（PS-05 取 ocr 位置格式）与 M3 的证据定位里都会用到。
    """
    blocks: list[RawBlock] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:  # 0 = 文本块，1 = 图片块
            continue
        lines: list[str] = []
        bboxes: list[tuple[int, int] | None] = []
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if not text.strip():
                continue
            x0, y0 = line["bbox"][0], line["bbox"][1]
            lines.append(text)
            bboxes.append((int(round(x0)), int(round(y0))))
        if lines:
            blocks.append(RawBlock(page_no=page_no, lines=lines, bboxes=bboxes))
    # 阅读顺序：先上后下、先左后右
    blocks.sort(key=lambda b: (b.bboxes[0][1] if b.bboxes[0] else 0, b.bboxes[0][0] if b.bboxes[0] else 0))
    return blocks


def extract_pdf(path: Path, settings: Settings | None = None) -> ExtractionResult:
    """PDF：逐页判定文本型/扫描型，必要时逐页 OCR（PS-04）。"""
    import pymupdf  # noqa: PLC0415 - 重量级依赖，延迟导入

    settings = settings or get_settings()
    file_size = path.stat().st_size
    if file_size == 0:
        raise AppError(
            ErrorCode.EMPTY_CONTRACT_CONTENT,
            f"合同文件为空（0 字节）：{path.name}",
            detail={"file_name": path.name, "file_size": 0},
        )

    try:
        document = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            ErrorCode.PARSE_FAILED,
            f"PDF 打开失败（文件可能已损坏）：{path.name}",
            detail={"file_name": path.name, "error": str(exc)[:200]},
        ) from exc

    result = ExtractionResult(kind=DocKind.PDF)
    started = time.perf_counter()
    try:
        result.page_count = document.page_count
        text_chars = 0
        text_blocks: dict[int, list[RawBlock]] = {}

        for page_no, page in enumerate(document, start=1):
            raw_text = page.get_text("text")
            text_chars += len(raw_text.strip())
            if len(raw_text.strip()) < settings.ocr_scan_page_min_chars:
                # PS-04：有效字符数低于阈值 → 判为扫描页
                result.used_ocr_pages.append(page_no)
                continue
            text_blocks[page_no] = _text_blocks_of_page(page, page_no)

        result.text_chars = text_chars

        if result.used_ocr_pages:
            # PS-05：混合文档从严取 ocr，保证 position 格式唯一
            result.parse_mode = ParseMode.OCR
            for page_no in range(1, result.page_count + 1):
                if page_no in result.used_ocr_pages:
                    pixmap = document[page_no - 1].get_pixmap(dpi=OCR_RENDER_DPI)
                    result.blocks.extend(_ocr_page_blocks(page_no, pixmap_to_bgr(pixmap)))
                else:
                    result.blocks.extend(text_blocks.get(page_no, []))
        else:
            result.blocks = [b for page_no in sorted(text_blocks) for b in text_blocks[page_no]]
            if not result.blocks:
                raise AppError(
                    ErrorCode.EMPTY_CONTRACT_CONTENT,
                    f"合同内容为空：{path.name}",
                    detail={"file_name": path.name},
                )
    finally:
        document.close()
        result.ocr_seconds = time.perf_counter() - started
    return result


def extract_docx(path: Path) -> ExtractionResult:
    """Word：``python-docx`` 的段落天然就是"段"（无页码概念，统一记第 1 页）。"""
    import docx  # noqa: PLC0415

    if path.stat().st_size == 0:
        raise AppError(
            ErrorCode.EMPTY_CONTRACT_CONTENT,
            f"合同文件为空（0 字节）：{path.name}",
            detail={"file_name": path.name, "file_size": 0},
        )
    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            ErrorCode.PARSE_FAILED,
            f"Word 文档打开失败：{path.name}",
            detail={"file_name": path.name, "error": str(exc)[:200]},
        ) from exc

    blocks = [
        RawBlock(page_no=1, lines=[paragraph.text], bboxes=[None])
        for paragraph in document.paragraphs
        if paragraph.text.strip()
    ]
    if not blocks:
        raise AppError(
            ErrorCode.EMPTY_CONTRACT_CONTENT,
            f"Word 文档无可提取正文：{path.name}",
            detail={"file_name": path.name},
        )
    return ExtractionResult(
        blocks=blocks,
        page_count=1,
        parse_mode=ParseMode.TEXT,
        kind=DocKind.DOCX,
        text_chars=sum(len(b.lines[0]) for b in blocks),
    )


def extract_image(path: Path, settings: Settings | None = None) -> ExtractionResult:
    """图片扫描件：整体 OCR，``parse_mode=ocr``。"""
    if path.stat().st_size == 0:
        raise AppError(
            ErrorCode.EMPTY_CONTRACT_CONTENT,
            f"合同图片为空（0 字节）：{path.name}",
            detail={"file_name": path.name, "file_size": 0},
        )
    started = time.perf_counter()
    blocks = _ocr_page_blocks(1, load_image_bgr(path))
    if not blocks:
        raise AppError(
            ErrorCode.EMPTY_CONTRACT_CONTENT,
            f"图片 OCR 未识别出任何文本：{path.name}",
            detail={"file_name": path.name},
        )
    return ExtractionResult(
        blocks=blocks,
        page_count=1,
        used_ocr_pages=[1],
        parse_mode=ParseMode.OCR,
        kind=DocKind.IMAGE,
        ocr_seconds=time.perf_counter() - started,
        text_chars=sum(len(line) for line in blocks[0].lines),
    )


def extract_document(
    path: Path, file_name: str | None = None, settings: Settings | None = None
) -> ExtractionResult:
    """按类型识别结果分派到具体抽取器（PS-01 双重判定）。"""
    kind, reason = detect_document_kind(path, file_name)
    logger.info("文档类型识别：%s → %s（%s）", file_name or path.name, kind.value, reason)

    if kind is DocKind.PDF:
        result = extract_pdf(path, settings)
    elif kind is DocKind.DOCX:
        result = extract_docx(path)
    elif kind is DocKind.IMAGE:
        result = extract_image(path, settings)
    else:
        raise AppError(
            ErrorCode.PARSE_FAILED,
            f"不支持的合同文件类型：{file_name or path.name}",
            detail={"kind": kind.value},
        )
    result.warnings.append(f"类型识别：{reason}")
    return result
