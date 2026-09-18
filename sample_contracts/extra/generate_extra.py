"""生成「额外测试合同」T-01…T-04（PDF / Word / 扫描件）。

与上层 ``sample_contracts/generate_samples.py``（AP-001…AP-005，随系统交付的固定样例）分开：
这里的是**给你自己动手测着玩的**合同，专门设计成能打到不同规则上。

用法（在 ClauseGuard 根目录）::

    uv run --project backend python sample_contracts/extra/generate_extra.py
    uv run --project backend python sample_contracts/extra/generate_extra.py --scan T-02
        # 额外把 T-02 渲染成扫描件 PNG（测 OCR 路径；单页识别约 60–150 秒）

产物（生成在 sources 同级）：

| 文件 | 形态 | 设计意图 |
|---|---|---|
| ``T-01_设备租赁合同.pdf`` | 文本型 PDF | 故意写"差"：打 R001/R002/R003/R005/R008/R010/R011 |
| ``T-02_技术服务合同.pdf`` | 文本型 PDF | 条款齐备的"好"合同：期望 0 命中（对照组） |
| ``T-03_数据处理服务协议.docx`` | Word | 测 docx 解析 + 打 R009（涉及个人信息但缺"删除义务"） |
| ``T-04_框架采购协议.pdf`` | 文本型 PDF | 主体名称与金额均缺失：打 R006/R007，并让解析页出现 missing 行 |

文本源在 ``sources/*.txt``，以 ``<<<PAGE>>>`` 分隔页；改完重跑本脚本即可。
内容全部为虚构主体（"某某…"），符合 SPEC §15 SD-02「禁止真实企业合同数据」。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymupdf

if not sys.stdout.isatty():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
SOURCES_DIR = BASE_DIR / "sources"

CJK_FONT = "china-s"  # PyMuPDF 内置简中字体，避免绑死系统字体
PAGE_WIDTH, PAGE_HEIGHT = pymupdf.paper_size("a4")
MARGIN = 64.0
FONT_SIZE = 11.0
LINE_HEIGHT = 18.0
SCAN_DPI = 200
PAGE_BREAK = "<<<PAGE>>>"

ARTIFACTS: list[tuple[str, str, str]] = [
    ("T-01_设备租赁合同.txt", "T-01_设备租赁合同.pdf", "pdf"),
    ("T-02_技术服务合同.txt", "T-02_技术服务合同.pdf", "pdf"),
    ("T-03_数据处理服务协议.txt", "T-03_数据处理服务协议.docx", "docx"),
    ("T-04_框架采购协议.txt", "T-04_框架采购协议.pdf", "pdf"),
]


def _char_width(char: str, font_size: float) -> float:
    """粗略字宽：CJK 全角按字号，其余按 0.55 字号。"""
    return font_size if ord(char) > 0x2E80 else font_size * 0.55


def _wrap(text: str, font_size: float, max_width: float) -> list[str]:
    lines: list[str] = []
    current, width = "", 0.0
    for char in text:
        step = _char_width(char, font_size)
        if width + step > max_width and current:
            lines.append(current)
            current, width = "", 0.0
        current += char
        width += step
    if current:
        lines.append(current)
    return lines or [""]


def build_pdf(source: Path, target: Path) -> int:
    """文本源 → **带文本层**的 PDF（可被直接抽取，不需要 OCR）。"""
    document = pymupdf.open()
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    y = MARGIN
    max_width = PAGE_WIDTH - 2 * MARGIN
    pages = 1

    for raw in source.read_text(encoding="utf-8").splitlines():
        if raw.strip() == PAGE_BREAK:
            page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            y, pages = MARGIN, pages + 1
            continue
        text = raw.strip()
        if not text:
            y += LINE_HEIGHT * 0.5
            continue
        for line in _wrap(text, FONT_SIZE, max_width):
            if y > PAGE_HEIGHT - MARGIN:
                page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
                y, pages = MARGIN, pages + 1
            page.insert_text((MARGIN, y), line, fontname=CJK_FONT, fontsize=FONT_SIZE)
            y += LINE_HEIGHT

    document.save(target)
    document.close()
    return pages


def build_docx(source: Path, target: Path) -> int:
    """文本源 → Word（docx）。解析器目前只读段落（M2-D），因此逐行成段即可。"""
    from docx import Document

    document = Document()
    for raw in source.read_text(encoding="utf-8").splitlines():
        text = raw.strip()
        if text == PAGE_BREAK:
            document.add_page_break()
            continue
        document.add_paragraph(text)
    document.save(target)
    return 1


def build_scan(pdf: Path, target: Path) -> int:
    """把已生成的 PDF 渲染成 **PNG 扫描件**（没有文本层，逼出 OCR 路径）。"""
    document = pymupdf.open(pdf)
    pages = 0
    for index, page in enumerate(document):
        pixmap = page.get_pixmap(dpi=SCAN_DPI)
        if index == 0:
            pixmap.save(target)
        pages += 1
    document.close()
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(description="生成额外测试合同 T-01…T-04")
    parser.add_argument(
        "--scan",
        metavar="T-0X",
        help="额外把指定合同渲染成扫描件 PNG（如 --scan T-02），用于测 OCR",
    )
    args = parser.parse_args()

    if not SOURCES_DIR.is_dir():
        print(f"找不到文本源目录：{SOURCES_DIR}")
        return 2

    for source_name, target_name, kind in ARTIFACTS:
        source = SOURCES_DIR / source_name
        target = BASE_DIR / target_name
        if not source.is_file():
            print(f"  [跳过] 缺少文本源 {source_name}")
            continue
        if kind == "pdf":
            pages = build_pdf(source, target)
        else:
            pages = build_docx(source, target)
        print(f"  [OK] {target_name}（{kind}，{pages} 页，{target.stat().st_size} 字节）")

    if args.scan:
        prefix = args.scan.strip()
        candidates = [name for name in ARTIFACTS if name[1].startswith(prefix)]
        if not candidates:
            print(f"  [X] --scan {prefix} 没有匹配的产物")
            return 2
        source_name, target_name, _ = candidates[0]
        pdf = BASE_DIR / target_name
        if pdf.suffix.lower() != ".pdf":
            # docx 需要先转 PDF 才能渲染扫描件：这里直接从文本源生成一个临时 PDF
            pdf = BASE_DIR / f"_tmp_{prefix}.pdf"
            build_pdf(SOURCES_DIR / source_name, pdf)
        scan_target = BASE_DIR / f"{pdf.stem}_扫描件.png"
        pages = build_scan(pdf, scan_target)
        print(f"  [OK] {scan_target.name}（扫描件，{pages} 页，{scan_target.stat().st_size} 字节）")
        if pdf.name.startswith("_tmp_"):
            pdf.unlink()

    print("\n完成。测试方法见 sample_contracts/extra/README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
