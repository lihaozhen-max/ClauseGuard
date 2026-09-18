"""风险汇总（SPEC §8.13 RL-AGG，**规范算法**）。

```
令 H = { hit_status = 'hit' 的命中集合 }
若 ∃ h ∈ H 且 h.risk_level = 'high'   → overall = 'high'
否则若 ∃ h ∈ H 且 h.risk_level = 'medium' → overall = 'medium'
否则                                   → overall = 'low'
```

- **RL-AGG-01**：``uncertain`` 与 ``miss`` **禁止**参与等级计算；
- **RL-AGG-02**：``H`` 为空 → ``low``（禁止因数据缺失判为更高等级）；
- **RL-AGG-03**：整体等级**禁止**由 LLM 决定——本模块是纯函数，不依赖任何模型。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.core.enums import HitStatus, RiskLevel

#: 风险等级排序（高 → 低），用于关注点排序与展示
RISK_ORDER = {RiskLevel.HIGH.value: 0, RiskLevel.MEDIUM.value: 1, RiskLevel.LOW.value: 2}


def aggregate_overall_risk(hits: Iterable[Any]) -> str:
    """按 RL-AGG 计算整体风险等级。

    ``hits`` 中的元素可以是 ``RuleOutcome``，也可以是含 ``hit_status`` / ``risk_level`` 的字典。
    """
    levels: set[str] = set()
    for item in hits:
        status = _get(item, "hit_status")
        if status != HitStatus.HIT.value:
            continue  # RL-AGG-01
        levels.add(str(_get(item, "risk_level") or RiskLevel.LOW.value))

    if RiskLevel.HIGH.value in levels:
        return RiskLevel.HIGH.value
    if RiskLevel.MEDIUM.value in levels:
        return RiskLevel.MEDIUM.value
    return RiskLevel.LOW.value  # RL-AGG-02


def _get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def count_by_status(hits: Iterable[Any]) -> dict[str, int]:
    """按 ``hit_status`` 统计（供 IF-05 的 hit_count / uncertain_count）。"""
    counts = {status.value: 0 for status in HitStatus}
    for item in hits:
        status = str(_get(item, "hit_status") or "")
        if status in counts:
            counts[status] += 1
    return counts


def count_by_level(hits: Iterable[Any]) -> dict[str, int]:
    """统计**命中**（``hit_status=hit``）按风险等级的分布（供摘要模板使用）。"""
    counts = {level.value: 0 for level in RiskLevel}
    for item in hits:
        if _get(item, "hit_status") != HitStatus.HIT.value:
            continue
        level = str(_get(item, "risk_level") or "")
        if level in counts:
            counts[level] += 1
    return counts


def active_hits(hits: Iterable[Any]) -> list[Any]:
    """只保留 ``hit_status=hit`` 的项，并按风险等级降序排序。"""
    selected = [item for item in hits if _get(item, "hit_status") == HitStatus.HIT.value]
    return sorted(selected, key=lambda item: RISK_ORDER.get(str(_get(item, "risk_level")), 9))
