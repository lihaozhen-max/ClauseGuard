"""文档类型识别（SPEC PS-01）。

**必须**基于"扩展名 + 文件头魔数"双重判定；两者冲突时**以魔数为准**。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from app.core.errors import AppError, ErrorCode

#: 文件头魔数（按前缀匹配，长前缀优先）
_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"PK\x03\x04", "zip"),
)

_MAGIC_HEAD = 8

#: 扩展名映射（docx 归属 zip 容器，需进一步确认）
_EXTENSION_TYPES = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpeg",
}


class DocKind(StrEnum):
    """解析器可处理的文档类别。"""

    PDF = "pdf"
    DOCX = "docx"
    IMAGE = "image"
    UNKNOWN = "unknown"


def _extension_kind(file_name: str) -> str:
    return _EXTENSION_TYPES.get(Path(file_name).suffix.lower(), "unknown")


def _magic_kind(head: bytes) -> str:
    for prefix, kind in _MAGIC_PREFIXES:
        if head.startswith(prefix):
            return kind
    return "unknown"


def _is_docx_zip(path: Path) -> bool:
    """zip 容器是否为 Word 文档（含 ``word/document.xml``）。"""
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            return "word/document.xml" in archive.namelist()
    except Exception:  # noqa: BLE001 - 损坏的 zip 一律视为非 docx
        return False


def detect_document_kind(path: Path, file_name: str | None = None) -> tuple[DocKind, str]:
    """返回 ``(类别, 判定依据说明)``。

    规则（PS-01）：
    1. 魔数能定性时以魔数为准（扩展名与之冲突会记入说明）；
    2. 魔数只给出 ``zip`` 时，用容器内容区分 docx；
    3. 魔数无法定性时回退扩展名；两者都无法定性 → 抛 ``PARSE_FAILED``。
    """
    name = file_name or path.name
    extension_kind = _extension_kind(name)
    try:
        with path.open("rb") as handle:
            head = handle.read(_MAGIC_HEAD)
    except OSError as exc:
        raise AppError(
            ErrorCode.PARSE_FAILED,
            f"无法读取合同文件：{name}",
            detail={"file_name": name, "error": str(exc)},
        ) from exc

    magic = _magic_kind(head)

    if magic == "zip":
        if _is_docx_zip(path):
            return DocKind.DOCX, "魔数 zip + 容器含 word/document.xml → docx"
        raise AppError(
            ErrorCode.PARSE_FAILED,
            f"文件是 zip 容器但不是 Word 文档：{name}",
            detail={"file_name": name, "magic": "zip"},
        )
    if magic in {"pdf", "png", "jpeg"}:
        note = "魔数与扩展名一致"
        if extension_kind != magic and not (magic == "jpeg" and extension_kind in {"jpg", "jpeg"}):
            note = f"魔数({magic}) 与扩展名({extension_kind}) 冲突，以魔数为准"
        kind = DocKind.IMAGE if magic in {"png", "jpeg"} else DocKind(magic)
        return kind, note
    if extension_kind != "unknown":
        return (
            DocKind.IMAGE if extension_kind in {"png", "jpg", "jpeg"} else DocKind(extension_kind),
            f"魔数不可识别，回退扩展名({extension_kind})",
        )

    raise AppError(
        ErrorCode.PARSE_FAILED,
        f"无法识别的合同文件类型：{name}（魔数 {head!r}）",
        detail={"file_name": name, "magic": head.hex()},
    )
