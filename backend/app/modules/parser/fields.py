"""字段提取与条款识别（SPEC FR-PARSE-05/06、PS-06/PS-09、§2.4/§2.5/§2.6）。

三层递进（PS-06，**禁止**跳过前两层直接用 LLM）：

1. **正则/模板**（本模块）：金额、编号、日期、币种、主体等强格式字段；
2. **条款定位**（本模块）：按 PS-09 的条款标题词表切分条款段落；
3. **LLM 兜底**（M3）：仅限 ``contract_amount``/``party_a``/``party_b``/``effective_date``/``expiry_date``，
   且返回的 ``source_text`` 必须经 ``full_text`` 子串校验（PS-07/PS-08）。
   本模块通过 ``llm_fallback`` 参数预留该层，M2 阶段不启用。

``success`` 判定（§2.4）：``field_value`` 非空 **且** ``source_text`` 是 ``full_text`` 连续子串
**且** ``position`` 非空；三者缺一即降级为 ``failed`` 并记录原因。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.core.enums import ExtractStatus, ParseMode
from app.core.logging import get_logger
from app.core.textutil import DEFAULT_SOURCE_TEXT_LIMIT, truncate
from app.modules.parser.models import FieldRecord, ParsedDocument, TextBlock

logger = get_logger(__name__)

#: §2.5 基本信息字段（8 个）
BASIC_FIELDS: tuple[str, ...] = (
    "contract_title",
    "contract_no",
    "party_a",
    "party_b",
    "contract_amount",
    "currency",
    "effective_date",
    "expiry_date",
)

#: §2.5 条款字段（8 个）
CLAUSE_FIELDS: tuple[str, ...] = (
    "payment_clause",
    "delivery_clause",
    "acceptance_clause",
    "breach_clause",
    "confidentiality_clause",
    "data_clause",
    "ip_clause",
    "dispute_clause",
)

ALL_FIELDS: tuple[str, ...] = BASIC_FIELDS + CLAUSE_FIELDS

#: PS-07：允许 LLM 兜底的关键字段
LLM_FALLBACK_FIELDS = frozenset(
    {"contract_amount", "party_a", "party_b", "effective_date", "expiry_date"}
)

#: PS-09：条款标题词表（含同义变体）
CLAUSE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "payment_clause": ("付款", "支付方式", "结算", "价款"),
    "delivery_clause": ("交付", "交货", "供货", "运输与交付"),
    "acceptance_clause": ("验收",),
    "breach_clause": ("违约责任", "违约"),
    "confidentiality_clause": ("保密", "商业秘密"),
    "data_clause": ("数据", "个人信息", "隐私"),
    "ip_clause": ("知识产权", "著作权", "专利", "商标"),
    "dispute_clause": ("争议解决", "争议", "管辖", "仲裁", "诉讼"),
}

#: 无需标题编号也能独立成条的"强关键词"（真实合同常不写"第 N 条"）
STRONG_SINGLE_KEYWORDS: dict[str, str] = {
    "confidentiality_clause": "保密",
    "ip_clause": "知识产权",
    "breach_clause": "违约",
    "dispute_clause": "争议解决",
}

#: 条款标题：**只有** 第X条 / 第X章 才算带编号的条款标题
_CLAUSE_HEADING_RE = re.compile(r"^第[一二三四五六七八九十百零〇\d]+[条章节]")

#: 列表项标记：付款条款里的 "1." "2." "（1）" "一、" 等**不是**条款标题
_LIST_ITEM_RE = re.compile(r"^(\d+[.、)）]|[（(]\d+[)）]|[一二三四五六七八九十]+[、.])")

#: 句末标点：以这些字符结尾的段落是**正文句子**，不是条款标题
_SENTENCE_TERMINATORS = "。！？；.!?;"

#: 主体条款里要排除的"盖章/签字/开户"等非主体行
_PARTY_EXCLUDE = ("盖章", "签字", "签章", "开户", "账号", "地址", "电话", "传真", "日期")

_TITLE_RE = re.compile(r"合同标题\s*[：:]\s*(\S.{0,60})")
_CONTRACT_NO_RE = re.compile(r"合同编号\s*[：:]\s*([A-Za-z0-9\-_/（）()]+)")
_PARTY_RE = re.compile(r"(甲|乙)方([^：:\n]{0,12})[：:]\s*([^\n，。；]{1,40})")
_AMOUNT_SYMBOL_RE = re.compile(r"[¥￥]\s*([\d,]+(?:\.\d+)?)")
_AMOUNT_PLAIN_RE = re.compile(r"人民币\s*([\d,]+(?:\.\d+)?)")
_AMOUNT_CN_RE = re.compile(
    r"人民币(["
    + "零一二三四五六七八九十百千万亿"
    + "壹贰叁肆伍陆柒捌玖拾佰仟"
    + "两"
    + "]{2,20}?)元"
)
_CURRENCY_CODE_RE = re.compile(r"币种[为是]?\s*[：:]?\s*([A-Za-z]{3})")
_CURRENCY_PAREN_RE = re.compile(r"[（(]\s*([A-Za-z]{3})\s*[)）]")
_CURRENCY_WORD_MAP = {
    "人民币": "CNY",
    "美元": "USD",
    "美金": "USD",
    "欧元": "EUR",
    "港币": "HKD",
    "港元": "HKD",
    "日元": "JPY",
    "英镑": "GBP",
}
_CN_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_EFFECTIVE_CONTEXT_RE = re.compile(r"自|生效|开始")
_EXPIRY_CONTEXT_RE = re.compile(r"至|止|届满|到期")

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "拾": 10, "佰": 100, "仟": 1000}
_CN_SECTIONS = {"万": 10_000, "亿": 100_000_000}

#: 与 ``_AMOUNT_CN_RE`` 的字符集保持一致的断言，防止词表漂移（大小写两套数字都要覆盖）
assert set(_AMOUNT_CN_RE.pattern.split("[")[1].split("]")[0]) >= (
    set(_CN_DIGITS) | set(_CN_UNITS) | set(_CN_SECTIONS)
)


def chinese_number_to_int(text: str) -> int | None:
    """中文数字 → 整数（"伍拾万" → 500000）。无法解析返回 None。"""
    total = section = number = 0
    matched = False
    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
            matched = True
        elif char in _CN_UNITS:
            section += (number or 1) * _CN_UNITS[char]
            number = 0
            matched = True
        elif char in _CN_SECTIONS:
            section = (section + number) * _CN_SECTIONS[char]
            total += section
            section = 0
            number = 0
            matched = True
        else:
            return None
    if not matched:
        return None
    return total + section + number


def _normalize_amount(raw: str) -> str | None:
    """金额归一化为**纯数字字符串**（SPEC §2.3：不含千分位与货币符号）。"""
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    if value <= 0:
        return None
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _normalize_date(year: str, month: str, day: str) -> str | None:
    try:
        y, m, d = int(year), int(month), int(day)
    except ValueError:
        return None
    if not (1900 <= y <= 2999 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


@dataclass
class FieldCandidate:
    """字段抽取的中间结果：值 + 命中的段落。"""

    value: str
    block: TextBlock


def _iter_blocks(document: ParsedDocument) -> Sequence[TextBlock]:
    return document.blocks


# ── 基本信息字段（第 1 层：正则/模板）─────────────────────────────────────


def _extract_title(document: ParsedDocument) -> FieldCandidate | None:
    for block in _iter_blocks(document):
        match = _TITLE_RE.search(block.text)
        if match:
            return FieldCandidate(match.group(1).strip().rstrip("。；;，,"), block)
    # 兜底：首个短段落且不含"："的标题行（排除条款标题与列表项）
    for block in _iter_blocks(document):
        text = block.text.strip()
        if (
            2 <= len(text) <= 20
            and "：" not in text
            and not _CLAUSE_HEADING_RE.match(text)
            and not _LIST_ITEM_RE.match(text)
        ):
            return FieldCandidate(text, block)
    return None


def _extract_contract_no(document: ParsedDocument) -> FieldCandidate | None:
    for block in _iter_blocks(document):
        match = _CONTRACT_NO_RE.search(block.text)
        if match:
            return FieldCandidate(match.group(1).strip(), block)
    return None


def _extract_party(document: ParsedDocument, side: str) -> FieldCandidate | None:
    for block in _iter_blocks(document):
        for match in _PARTY_RE.finditer(block.text):
            if match.group(1) != side:
                continue
            prefix = match.group(2) or ""
            if any(word in prefix for word in _PARTY_EXCLUDE):
                continue
            value = match.group(3).strip().rstrip("，。；;、")
            if len(value) < 2:
                continue
            # "甲乙双方各持一份" 这类不是主体名
            if value.startswith(("各持", "双方", "签字", "盖章")):
                continue
            return FieldCandidate(value, block)
    return None


def _extract_amount(document: ParsedDocument) -> FieldCandidate | None:
    for block in _iter_blocks(document):
        for pattern in (_AMOUNT_SYMBOL_RE, _AMOUNT_PLAIN_RE):
            match = pattern.search(block.text)
            if match:
                value = _normalize_amount(match.group(1))
                if value:
                    return FieldCandidate(value, block)
        match = _AMOUNT_CN_RE.search(block.text)
        if match:
            number = chinese_number_to_int(match.group(1))
            if number:
                return FieldCandidate(str(number), block)
    return None


def _extract_currency(document: ParsedDocument) -> FieldCandidate | None:
    for block in _iter_blocks(document):
        match = _CURRENCY_CODE_RE.search(block.text) or _CURRENCY_PAREN_RE.search(block.text)
        if match:
            return FieldCandidate(match.group(1).upper(), block)
    for block in _iter_blocks(document):
        for word, code in _CURRENCY_WORD_MAP.items():
            if word in block.text:
                return FieldCandidate(code, block)
    return None


def _date_candidates(block: TextBlock, context: re.Pattern[str], want_after: bool) -> list[str]:
    """在段落中找日期；优先取上下文匹配（自…起生效 / 有效期至…止）的那个。"""
    found: list[tuple[bool, str]] = []
    for pattern in (_CN_DATE_RE, _ISO_DATE_RE):
        for match in pattern.finditer(block.text):
            value = _normalize_date(match.group(1), match.group(2), match.group(3))
            if not value:
                continue
            window = block.text[max(match.start() - 8, 0) : match.end() + 8]
            if want_after:
                window = block.text[match.start() : match.end() + 8]
            found.append((bool(context.search(window)), value))
    found.sort(key=lambda item: (not item[0],))
    return [value for _, value in found]


def _extract_effective_date(document: ParsedDocument) -> FieldCandidate | None:
    for block in _iter_blocks(document):
        values = _date_candidates(block, _EFFECTIVE_CONTEXT_RE, want_after=True)
        if values:
            return FieldCandidate(values[0], block)
    return None


def _extract_expiry_date(document: ParsedDocument) -> FieldCandidate | None:
    for block in _iter_blocks(document):
        values = _date_candidates(block, _EXPIRY_CONTEXT_RE, want_after=False)
        if values:
            # 到期时间取最晚的那个日期（"自…起生效，有效期至…止"）
            return FieldCandidate(max(values), block)
    return None


_BASIC_EXTRACTORS: dict[str, Callable[[ParsedDocument], FieldCandidate | None]] = {
    "contract_title": _extract_title,
    "contract_no": _extract_contract_no,
    "party_a": lambda doc: _extract_party(doc, "甲"),
    "party_b": lambda doc: _extract_party(doc, "乙"),
    "contract_amount": _extract_amount,
    "currency": _extract_currency,
    "effective_date": _extract_effective_date,
    "expiry_date": _extract_expiry_date,
}


# ── 条款识别（PS-09）─────────────────────────────────────────────────────


def clause_heading_index(document: ParsedDocument) -> dict[int, str]:
    """返回 ``段落下标 → 命中该段的条款字段名``（一个段落至多算一个条款起点）。

    标题判定分两档（顺序敏感）：

    1. ``第X条 / 第X章`` —— 带编号的条款标题，最强信号；
    2. 短段落（≤24 字）、**不以列表项标记开头**、**不以句末标点结尾** ——
       覆盖"保密条款"这类无编号标题。

    两处刻意的排除（都是实测踩出来的）：

    - 排除 ``1.`` / ``（1）`` / ``一、`` 列表项：付款条款中的
      "2. 服务期满并经甲方验收合格后30日内支付剩余70%款项。" 含"验收"二字，
      按列表项当标题会把验收条款错误定位到付款条款里；
    - 排除以 ``。！？；`` 结尾的句子：正文短句
      "验收合格后120日内支付尾款。" 同样含"验收"，若当标题会把**上一条**（付款）
      的正文截断，使 R002 之类的规则读不到账期（实测踩过）。
    """

    headings: dict[int, str] = {}
    for index, block in enumerate(document.blocks):
        text = block.text.strip()
        is_clause_heading = bool(_CLAUSE_HEADING_RE.match(text))
        is_short_title = (
            len(text) <= 24
            and not _LIST_ITEM_RE.match(text)
            and text[-1] not in _SENTENCE_TERMINATORS
        )
        if not (is_clause_heading or is_short_title):
            continue
        for field, keywords in CLAUSE_KEYWORDS.items():
            if field in headings.values():
                continue
            if any(keyword in text for keyword in keywords):
                headings[index] = field
                break
    return headings


def _clause_span(document: ParsedDocument, start: int) -> list[TextBlock]:
    """条款正文 = 从标题段落起，到**下一个条款标题**之前的所有段落。

    断开条件有两条，缺一不可：

    1. 段落命中 ``第X条 / 第X章`` —— **任何**编号条款都算新条款的开头，
       哪怕它不含 §2.9 词表里的关键词（例如"第八条 合同期限与续约""第九条 其他"）。
       早期只按"关键词命中的标题"断开，导致 AP-001 的争议解决条款一路吞到文末，
       把"自动续约"也圈进争议解决部位，进而误伤了 R003 的关键词判定（实测踩过）。
    2. 段落被 ``clause_heading_index`` 判为无编号短标题（如"保密条款"单独成行）。
    """
    headings = clause_heading_index(document)
    end = len(document.blocks)
    for index in range(start + 1, len(document.blocks)):
        text = document.blocks[index].text.strip()
        if _CLAUSE_HEADING_RE.match(text) or index in headings:
            end = index
            break
    return list(document.blocks[start:end])


def extract_clauses(document: ParsedDocument) -> dict[str, FieldRecord]:
    """提取 8 个条款字段；未出现的条款记为 ``missing``（这是 R008/R010/R011 的判定依据）。"""
    headings = clause_heading_index(document)
    records: dict[str, FieldRecord] = {}
    for field in CLAUSE_FIELDS:
        start = next((index for index, name in headings.items() if name == field), None)
        if start is None:
            # 兜底：强关键词出现在正文中（真实合同常不写编号标题）
            keyword = STRONG_SINGLE_KEYWORDS.get(field)
            if keyword:
                fallback = next(
                    (index for index, block in enumerate(document.blocks) if keyword in block.text),
                    None,
                )
                start = fallback
        if start is None:
            records[field] = FieldRecord.missing(field)
            continue

        span = _clause_span(document, start)
        raw_text = "\n".join(block.text for block in span)
        retained = truncate(raw_text, DEFAULT_SOURCE_TEXT_LIMIT)  # FR-PARSE-06
        value = document.blocks[start].text.strip()
        records[field] = _make_record(
            field,
            value,
            retained,
            raw_text if retained.endswith("…") else None,
            document,
            document.blocks[start],
        )
    return records


# ── FieldRecord 组装与校验 ────────────────────────────────────────────────


def _make_record(
    field_name: str,
    value: str | None,
    source_text: str | None,
    verify_text: str | None,
    document: ParsedDocument,
    block: TextBlock | None,
) -> FieldRecord:
    """按 §2.4 组装并校验：值非空 + source_text 是全文连续子串 + position 非空。"""
    if not value:
        return FieldRecord.missing(field_name)

    position = block.position(document.parse_mode) if block else None
    if not source_text or not position:
        return FieldRecord.failed(field_name, f"{field_name} 缺少原文或位置")

    # PS-08 / PS-10：子串校验（截断过的条款用未截断前缀校验）
    basis = verify_text if verify_text is not None else source_text
    if basis not in document.full_text:
        return FieldRecord.failed(field_name, f"{field_name} 的 source_text 不是合同原文连续子串")
    if not re.fullmatch(r"第\d+页 (第\d+段|区域\(\d+,\d+\))", position):
        return FieldRecord.failed(field_name, f"{field_name} 的 position 不符合 SPEC §2.6")

    return FieldRecord(field_name, value, source_text, position, ExtractStatus.SUCCESS.value)


def extract_fields(
    document: ParsedDocument,
    llm_fallback: Callable[[str, ParsedDocument], FieldCandidate | None] | None = None,
) -> tuple[list[FieldRecord], list[FieldRecord], list[str]]:
    """提取全部 16 个字段。

    返回 ``(basic_info, clause_info, warnings)``；任一字段失败**禁止**中断整体解析（FR-PARSE-10）。
    """
    warnings: list[str] = []
    basic: list[FieldRecord] = []

    for field in BASIC_FIELDS:
        record: FieldRecord | None = None
        try:
            candidate = _BASIC_EXTRACTORS[field](document)
            if candidate is None and llm_fallback is not None and field in LLM_FALLBACK_FIELDS:
                # 第 3 层（PS-07）：仅关键字段，且必须过子串校验
                candidate = llm_fallback(field, document)
            if candidate is None:
                record = FieldRecord.missing(field)
            else:
                record = _make_record(
                    field, candidate.value, candidate.block.text, None, document, candidate.block
                )
        except Exception as exc:  # noqa: BLE001 - FR-PARSE-10：单字段异常不得中断整体解析
            warnings.append(f"字段 {field} 提取异常：{type(exc).__name__}: {exc}")
            logger.warning("字段 %s 提取异常：%s", field, exc)
            record = FieldRecord.failed(field, f"{type(exc).__name__}: {exc}")
        basic.append(record)

    try:
        clause_map = extract_clauses(document)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"条款识别异常：{type(exc).__name__}: {exc}")
        logger.warning("条款识别异常：%s", exc)
        clause_map = {field: FieldRecord.failed(field, str(exc)) for field in CLAUSE_FIELDS}
    clauses = [clause_map[field] for field in CLAUSE_FIELDS]

    return basic, clauses, warnings


def summarize_status(records: Sequence[FieldRecord]) -> str:
    """由字段状态推导 ``parse_status``。

    - 全部 ``success`` 或 ``missing``（条款确实不存在）→ ``success``；
    - 存在 ``failed`` → ``partial``（FR-PARSE-10）；
    - **全部字段都提取不到** → ``failed``。
    """
    if any(record.extract_status == ExtractStatus.FAILED.value for record in records):
        return "partial"
    if all(record.extract_status != ExtractStatus.SUCCESS.value for record in records):
        return "failed"
    return "success"


def position_regex_for(parse_mode: ParseMode) -> str:
    """§2.6 的位置正则（校验与测试共用）。"""
    return r"^第\d+页 区域\(\d+,\d+\)$" if parse_mode is ParseMode.OCR else r"^第\d+页 第\d+段$"
