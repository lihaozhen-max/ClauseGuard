"""M2 OCR 用例（AC05、TS-04、TS-05）。

标注 ``slow``：真实 PaddleOCR 推理（整页约 60s），迭代时用
``uv run pytest -m "not slow"`` 跳过，里程碑验收时跑全量。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.enums import ParseMode
from app.core.errors import AppError
from app.db.models import ApprovalTask, ContractParse
from app.db.session import session_scope
from app.modules.parser.cleaning import build_document, normalize_ocr_text
from app.modules.parser.extractors import extract_document
from app.modules.parser.fields import extract_fields, summarize_status
from app.modules.parser.models import ParsedDocument
from app.tools.parser import parse_task

POSITION_OCR_RE = re.compile(r"^第\d+页 区域\(\d+,\d+\)$")


# ── OCR 文本后处理（不需要引擎）──────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("合同签订后三日内支付合同总额8 0 %作为预付款。", "合同签订后三日内支付合同总额80%作为预付款。"),
        ("乙方应于合同生效之日起2 0个日历日内交付。", "乙方应于合同生效之日起20个日历日内交付。"),
        ("金额(￥2 0 0 0 0 0.0 0)", "金额(￥200000.00)"),
        ("服务器 20 台", "服务器 20 台"),  # 数字与中文之间的空格保留
    ],
)
def test_ocr_text_normalization(raw: str, expected: str) -> None:
    """实测 PP-OCRv6 会把 80% 识别成 "8 0 %"，不修复会直接影响阈值类规则。"""
    assert normalize_ocr_text(raw) == expected


def test_ocr_cache_dir_is_ascii() -> None:
    """Paddle 无法读取非 ASCII 模型路径（M2 记录 R-01），配置层必须保证 ASCII。"""
    settings = get_settings()
    str(settings.ocr_cache_dir).encode("ascii")  # 非 ASCII 会抛 UnicodeEncodeError
    assert settings.ocr_enable_mkldnn is False


# ── AC05：扫描件必须走 OCR ────────────────────────────────────────────────


async def _task_of(instance_id: str) -> ApprovalTask | None:
    async with session_scope() as session:
        task = await session.scalar(
            select(ApprovalTask).where(ApprovalTask.instance_id == instance_id)
        )
        if task is not None:
            session.expunge(task)
    return task


@pytest.fixture(scope="module", autouse=True)
def _pulled(db_runner, mock_app):
    """确保样例审批单已拉取为任务。

    ``-m slow`` / ``-k test_scan_image_goes_through_ocr_ac05`` 单点运行时不经过别的文件，
    必须自己把前置数据建起来（实测单跑会因 AP-003 任务不存在而失败）。
    """
    import httpx

    from app.clients.approval_client import ApprovalSystemClient
    from app.tools.approval import list_pending_contract_approvals

    client = ApprovalSystemClient(transport=httpx.ASGITransport(app=mock_app))
    db_runner(lambda: list_pending_contract_approvals(20, client=client))


@pytest.mark.requires_db
@pytest.mark.slow
def test_scan_image_goes_through_ocr_ac05(live_db: str, db_runner, approval_client) -> None:
    """AC05：AP-003（扫描件）→ parse_mode=ocr，full_text 非空且含合同关键要素。"""
    task = db_runner(lambda: _task_of("AP-003"))
    assert task is not None
    outcome = db_runner(lambda: parse_task(task.id, client=approval_client))

    assert outcome.parse_mode == ParseMode.OCR.value
    assert outcome.used_ocr_pages == [1]
    assert outcome.parse_status == "success"

    async def fetch():
        async with session_scope() as session:
            row = await session.scalar(
                select(ContractParse).where(ContractParse.task_id == task.id)
            )
            if row is not None:
                session.expunge(row)
            return row

    row = db_runner(fetch)
    assert row is not None and row.full_text
    for keyword in ("采购合同", "合同金额", "付款", "预付款"):
        assert keyword in row.full_text, f"OCR 全文缺少关键要素：{keyword}"

    by_name = {r["field_name"]: r for r in [*row.basic_info_json, *row.clause_info_json]}
    # 与 AP-001 同类的风险特征应在扫描件里同样可提取
    assert by_name["contract_amount"]["field_value"] == "200000"
    assert by_name["currency"]["field_value"] == "CNY"
    assert by_name["payment_clause"]["extract_status"] == "success"
    assert by_name["dispute_clause"]["extract_status"] == "success"
    # 缺失条款必须如实记为 missing（R008/R010 的判定依据）
    assert by_name["confidentiality_clause"]["extract_status"] == "missing"
    assert by_name["ip_clause"]["extract_status"] == "missing"

    for record in by_name.values():
        if record["extract_status"] == "success":
            assert POSITION_OCR_RE.match(record["position"]), record["position"]
            assert record["source_text"] in row.full_text


# ── TS-05：混合文档（部分页扫描）→ parse_mode 取 ocr ──────────────────────


def _write_mixed_pdf(path: Path) -> None:
    """第 1 页有文本层，第 2 页是纯图片（模拟部分页扫描的合同）。"""
    import pymupdf

    document = pymupdf.open()
    page1 = document.new_page(width=595, height=420)
    page1.insert_text((72, 90), "采购合同", fontname="china-s", fontsize=13)
    page1.insert_text((72, 130), "合同标题：混合文档测试合同", fontname="china-s", fontsize=12)
    page1.insert_text((72, 170), "第三条  付款方式", fontname="china-s", fontsize=12)
    page1.insert_text(
        (72, 205), "合同签订后三日内支付合同总额80%作为预付款。", fontname="china-s", fontsize=11
    )

    scanned = pymupdf.open()
    scan_page = scanned.new_page(width=595, height=200)
    scan_page.insert_text((40, 60), "第七条  保密条款", fontname="china-s", fontsize=12)
    scan_page.insert_text((40, 100), "双方对商业秘密承担保密义务。", fontname="china-s", fontsize=12)
    pixmap = scan_page.get_pixmap(dpi=150)

    page2 = document.new_page(width=595, height=420)
    page2.insert_image(pymupdf.Rect(0, 0, 595, 200), stream=pixmap.tobytes("png"))
    document.save(path)
    document.close()
    scanned.close()


@pytest.mark.slow
def test_mixed_document_uses_ocr_mode_ts05(tmp_path: Path) -> None:
    """PS-05：混合文档 parse_mode 必须取 ocr（从严），position 格式唯一。"""
    pdf_path = tmp_path / "mixed.pdf"
    _write_mixed_pdf(pdf_path)

    extraction = extract_document(pdf_path, "mixed.pdf")
    assert extraction.parse_mode is ParseMode.OCR
    assert extraction.used_ocr_pages == [2]

    document: ParsedDocument = build_document(
        extraction.blocks,
        parse_mode=extraction.parse_mode,
        page_count=extraction.page_count,
        used_ocr_pages=extraction.used_ocr_pages,
    )
    assert document.page_count == 2
    # 文本页的内容与 OCR 页的内容都要在全文里
    assert "合同标题：混合文档测试合同" in document.full_text
    assert "保密" in document.full_text
    assert "预付款" in document.full_text

    basic, clauses, _ = extract_fields(document)
    assert summarize_status([*basic, *clauses]) in {"success", "partial"}
    by_name = {r.field_name: r for r in [*basic, *clauses]}
    # 文本页的条款与 OCR 页的条款都要能被定位
    assert by_name["payment_clause"].extract_status == "success"
    assert by_name["confidentiality_clause"].extract_status == "success"
    for record in [*basic, *clauses]:
        if record.extract_status == "success":
            assert POSITION_OCR_RE.match(record.position or ""), record.position


# ── OCR 失败路径 ──────────────────────────────────────────────────────────


def test_corrupt_image_raises_parse_failed(tmp_path: Path) -> None:
    """损坏的图片 → PARSE_FAILED（不得静默返回空结果，FR-PARSE-07）。"""
    fake = tmp_path / "broken.png"
    fake.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not-a-real-png" * 4)
    with pytest.raises(AppError) as excinfo:
        extract_document(fake, "broken.png")
    assert excinfo.value.code.value in {"PARSE_FAILED", "OCR_FAILED", "EMPTY_CONTRACT_CONTENT"}


def test_zero_byte_pdf_raises_empty_content(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(AppError) as excinfo:
        extract_document(empty, "empty.pdf")
    assert excinfo.value.code.value == "EMPTY_CONTRACT_CONTENT"
