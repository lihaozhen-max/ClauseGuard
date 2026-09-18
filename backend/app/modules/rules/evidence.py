"""证据截取、位置反查与回验（SPEC RL-00-02/RL-00-03、PS-10/PS-11）。

三条硬约束：

- **RL-00-02**：证据必须是 ``full_text`` 的**连续子串**，长度 ≤ 300 字符，超出截断并加 ``…``；
- **RL-00-03**：证据位置必须由证据文本在 ``page_map_json`` 中的偏移反查得到（§2.6 格式）；
- **PS-10/PS-11**：任何写入 ``rule_hits.evidence_text`` 的文本都必须通过子串校验；
  回验失败时——LLM 命中整条降级为 ``uncertain``，规则命中则视为实现缺陷并报警。

截断与子串校验的折中口径：截断后的 ``…`` 后缀不参与校验，即校验
"去掉尾部省略号后的那段前缀"仍是原文子串（与 FR-PARSE-06 的处理一致）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.modules.parser.models import ParsedDocument

#: RL-00-02：证据长度上限
MAX_EVIDENCE_LENGTH = 300

#: 句子边界（含换行）：证据取"包含命中的完整句子"，读起来才有意义
_SENTENCE_BOUNDARY = "。！？；!?;\n"

_ELLIPSIS = "…"


@dataclass
class Evidence:
    """一条证据（可能为空——缺失型命中本就没有原文可引）。"""

    text: str | None = None
    position: str | None = None
    verified: bool = True
    note: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.text

    def to_dict(self) -> dict[str, str | None]:
        return {"evidence_text": self.text, "evidence_position": self.position}


#: 缺失型证据：R004①/R008/R010①/R011① 等"条款不存在"的命中没有原文可引
NULL_EVIDENCE = Evidence(text=None, position=None, verified=True, note="条款缺失，无原文可引")


class EvidenceLocator:
    """在 ``full_text`` 上做"偏移 ↔ 句子 ↔ 位置"的换算。"""

    def __init__(self, document: ParsedDocument) -> None:
        self.document = document
        self.full_text = document.full_text

    # ── 位置 ──────────────────────────────────────────────────────────────

    def position_of_offset(self, offset: int) -> str | None:
        block = self.document.locate(offset)
        if block is None:
            return None
        return block.position(self.document.parse_mode)

    def position_of_text(self, text: str | None) -> str | None:
        """RL-00-03：由证据文本反查位置。"""
        if not text:
            return None
        probe = strip_ellipsis(text)
        index = self.full_text.find(probe)
        if index < 0:
            return None
        return self.position_of_offset(index)

    # ── 句子 ──────────────────────────────────────────────────────────────

    def sentence_span(self, start: int, end: int) -> tuple[int, int]:
        """把 ``[start, end)`` 扩展到包含它的完整句子。"""
        left = start
        while left > 0 and self.full_text[left - 1] not in _SENTENCE_BOUNDARY:
            left -= 1
        right = end
        length = len(self.full_text)
        while right < length and self.full_text[right] not in _SENTENCE_BOUNDARY:
            right += 1
        if right < length:  # 把句末标点一并纳入
            right += 1
        return left, right

    def sentence_at(self, offset: int) -> str:
        left, right = self.sentence_span(offset, offset + 1)
        return self.full_text[left:right].strip()

    # ── 证据构造与校验 ────────────────────────────────────────────────────

    def make(self, start: int, end: int, *, note: str | None = None) -> Evidence:
        """由原文偏移区间构造证据（扩展成句 + 截断 + 位置反查 + 回验）。"""
        left, right = self.sentence_span(start, end)
        raw = self.full_text[left:right].strip()
        if not raw:
            return Evidence(text=None, position=None, verified=False, note="证据区间为空")
        text = truncate_evidence(raw)
        return self.finalize(text, note=note)

    def make_from_text(self, text: str | None, *, note: str | None = None) -> Evidence:
        """由**候选**证据文本构造证据（LLM 返回的片段走这条路，必须回验）。"""
        if not text:
            return Evidence(text=None, position=None, verified=False, note=note or "证据为空")
        normalized = text.strip()
        index = self.full_text.find(strip_ellipsis(normalized))
        if index < 0:
            return Evidence(
                text=None,
                position=None,
                verified=False,
                note=note or "候选证据不是合同原文连续子串（PS-10 回验失败）",
            )
        left, right = self.sentence_span(index, index + len(strip_ellipsis(normalized)))
        return self.finalize(truncate_evidence(self.full_text[left:right].strip()), note=note)

    def finalize(self, text: str, *, note: str | None = None) -> Evidence:
        probe = strip_ellipsis(text)
        if probe not in self.full_text:
            return Evidence(
                text=None,
                position=None,
                verified=False,
                note=note or "证据子串校验失败（PS-10）",
            )
        return Evidence(text=text, position=self.position_of_text(probe), verified=True, note=note)

    def contains(self, text: str) -> bool:
        return text in self.full_text

    def find_span(
        self, pattern: str, *, start: int = 0, end: int | None = None
    ) -> tuple[int, int] | None:
        """在 ``[start, end)`` 范围内做正则搜索，返回**全文坐标**下的区间。

        用于"限定部位"匹配（RL-00-01）：先把部位范围算出来，再在其中匹配，
        得到的区间直接可用于构造证据。
        """
        region = self.full_text[start:end] if end is not None else self.full_text[start:]
        match = re.search(pattern, region)
        if match is None:
            return None
        return start + match.start(), start + match.end()


def strip_ellipsis(text: str) -> str:
    """去掉尾部省略号——截断产生的 ``…`` 不参与子串校验。"""
    return text[:-1] if text.endswith(_ELLIPSIS) else text


def truncate_evidence(text: str, limit: int = MAX_EVIDENCE_LENGTH) -> str:
    """RL-00-02：超过 300 字符则截断并加 ``…``。"""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + _ELLIPSIS


def find_all_matches(text: str, keywords: list[str]) -> list[tuple[str, int, int]]:
    """返回 ``[(关键词, 起始偏移, 结束偏移)]``，按出现顺序。"""
    matches: list[tuple[str, int, int]] = []
    for keyword in keywords:
        if not keyword:
            continue
        start = text.find(keyword)
        while start >= 0:
            matches.append((keyword, start, start + len(keyword)))
            start = text.find(keyword, start + len(keyword))
    matches.sort(key=lambda item: item[1])
    return matches
