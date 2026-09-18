"""生成样例合同文件（PDF / 扫描件 PNG / 异常文件）。

规范依据：SPEC §15 SD-01（每份样例随附期望结果）、SD-02（**必须**为自造文本，
禁止真实企业合同数据）。文本源在 ``sources/*.txt``，以 ``<<<PAGE>>>`` 分隔页。

用法（在 ClauseGuard 根目录）：

```powershell
uv run --project backend python sample_contracts/generate_samples.py
```

产物：

| 文件 | 对应审批单 | 说明 |
|---|---|---|
| ``AP-001_采购合同.pdf`` | AP-001 | 文本型 PDF，2 页，含 80% 预付款/自动续约/验收无标准/乙方所在地管辖 |
| ``AP-002_服务合同.pdf`` | AP-002 | 文本型 PDF，2 页，条款完备，期望 0 命中 |
| ``AP-003_扫描件.png`` | AP-003 | 由文本渲染后栅格化的**扫描件**，必须走 OCR |
| ``AP-005_空文件.pdf`` | AP-005 | 0 字节文件，覆盖 EMPTY_CONTRACT_CONTENT |
| ``expected_results.json`` | 全部 | SD-01 要求的期望结果（源自 ``mock-approval-system/seed.py``，单一来源） |
| AP-004 | AP-004 | **故意不生成**：审批单声明有附件但文件缺失，用于 AC16 |
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pymupdf

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
SOURCES_DIR = BASE_DIR / "sources"

#: PyMuPDF 内置简体中文字体；用系统字体文件会绑死 Windows，故用内置的
CJK_FONT = "china-s"
PAGE_WIDTH, PAGE_HEIGHT = pymupdf.paper_size("a4")
MARGIN = 64.0
FONT_SIZE = 11.0
LINE_HEIGHT = 18.0
SCAN_FONT_SIZE = 12.0
SCAN_DPI = 200

#: 渲染期收集的问题（超长行会被折行，破坏"一行一段"的确定性）
_WARNINGS: list[str] = []

#: 文本源 → 产物（kind: pdf / scan / empty）
ARTIFACTS = [
    ("AP-001.txt", "AP-001_采购合同.pdf", "pdf"),
    ("AP-002.txt", "AP-002_服务合同.pdf", "pdf"),
    ("AP-003.txt", "AP-003_扫描件.png", "scan"),
]


def _char_width(char: str, font_size: float) -> float:
    """粗略字宽估计：CJK 全角按字号，其余按 0.55 字号。"""
    return font_size if ord(char) > 0x2E80 else font_size * 0.55


def _content_width(text: str, font_size: float) -> float:
    return sum(_char_width(char, font_size) for char in text)


def _wrap(text: str, font_size: float, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    width = 0.0
    for char in text:
        char_width = _char_width(char, font_size)
        if current and width + char_width > max_width:
            lines.append(current)
            current, width = char, char_width
        else:
            current += char
            width += char_width
    lines.append(current)
    return lines


def render_pdf(
    pages: list[str],
    out_path: Path,
    font_size: float = FONT_SIZE,
    label: str = "",
) -> None:
    """把若干页文本渲染成 PDF。

    **设计约束**：文本源中"一行 = 一个段落"，且每行都**不应触发折行**。
    若某行超长被折行，PDF 抽取出来的一句话会被换行符切断，
    进而破坏 M2 的段落定位（``position``）与 PS-10 证据子串校验——
    因此这里显式记录告警，而不是静默折行。
    """
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    y = MARGIN
    max_width = PAGE_WIDTH - 2 * MARGIN

    for index, page_text in enumerate(pages):
        for paragraph in page_text.splitlines():
            line_text = paragraph.strip()
            if line_text and _content_width(line_text, font_size) > max_width:
                _WARNINGS.append(
                    f"{label}：超长行将被折行（{len(line_text)} 字）→ {line_text[:24]}…"
                )
            for line in _wrap(line_text, font_size, max_width):
                if y > PAGE_HEIGHT - MARGIN:
                    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
                    y = MARGIN
                if line:
                    page.insert_text(
                        (MARGIN, y), line, fontname=CJK_FONT, fontsize=font_size
                    )
                y += LINE_HEIGHT
            y += 4  # 段间距
        # 文本源中每一段 ``<<<PAGE>>>`` 分隔的内容渲染到独立页
        if index != len(pages) - 1:
            page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            y = MARGIN

    doc.save(out_path)
    doc.close()


def render_scan(source: Path, out_path: Path) -> None:
    """渲染 PDF 后栅格化为 PNG，模拟扫描件。"""
    pages = source.read_text(encoding="utf-8").split("<<<PAGE>>>")
    temp_pdf = out_path.with_suffix(".tmp.pdf")
    render_pdf(pages, temp_pdf, font_size=SCAN_FONT_SIZE, label=out_path.name)
    with pymupdf.open(temp_pdf) as doc:
        doc[0].get_pixmap(dpi=SCAN_DPI).save(out_path)
    temp_pdf.unlink()


def dump_expected_results() -> Path:
    """把 seed.py 里的期望结果导出为 JSON（SD-01，单一来源不复制）。"""
    sys.path.insert(0, str(PROJECT_ROOT / "mock-approval-system"))
    import seed  # noqa: PLC0415 - 需要先注入 sys.path

    payload = {
        "note": "SPEC §15 SD-01：每份样例的期望结果，供 M3 规则引擎回归比对",
        "samples": {
            item["instance_id"]: {
                "approval_code": item["approval_code"],
                "contract_type": item["contract_type"],
                "file_name": item["attachments"][0]["file_name"] if item["attachments"] else None,
                **item["expected"],
            }
            for item in seed.APPROVALS
        },
    }
    out_path = BASE_DIR / "expected_results.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out_path


def main() -> int:
    print(f"文本源：{SOURCES_DIR}")
    print(f"输出：{BASE_DIR}\n")

    for source_name, out_name, kind in ARTIFACTS:
        source = SOURCES_DIR / source_name
        out_path = BASE_DIR / out_name
        if not source.is_file():
            print(f"[跳过] 缺少文本源 {source}")
            continue
        if kind == "pdf":
            render_pdf(
                source.read_text(encoding="utf-8").split("<<<PAGE>>>"),
                out_path,
                label=out_name,
            )
        elif kind == "scan":
            render_scan(source, out_path)
        size = out_path.stat().st_size
        print(f"[生成] {out_name:<28} {size:>9,} 字节")

    # AP-005：0 字节文件（覆盖 EMPTY_CONTRACT_CONTENT）
    empty_path = BASE_DIR / "AP-005_空文件.pdf"
    empty_path.write_bytes(b"")
    print(f"[生成] {empty_path.name:<28} {0:>9,} 字节  ← 0 字节，覆盖空内容分支")

    # AP-004：故意不生成（附件缺失 → CONTRACT_ATTACHMENT_MISSING）
    missing = BASE_DIR / "AP-004_办公用品采购合同.pdf"
    if missing.exists():
        missing.unlink()
        print(f"[清理] {missing.name}（AP-004 必须保持附件缺失）")
    print(f"[保留] {missing.name:<28} 不存在  ← 故意的，用于 AC16")

    expected = dump_expected_results()
    print(f"\n[生成] {expected.name}（SD-01 期望结果）")

    if _WARNINGS:
        print("\n[告警] 以下行会被折行，请把 sources 里的长句拆成多行：")
        for warning in _WARNINGS:
            print(f"  - {warning}")
        return 1
    print("\n[校验] 全部文本行均在单行宽度内（一行 = 一段，不折行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
