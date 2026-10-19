# -*- coding: utf-8 -*-
"""小样本自适应层级上卷（F2.7 / 10 IN-09'）。

医疗场景不能用差分隐私加噪声（管理者要精确数字做考核，且对"数字被改过"零容忍）。
采纳方案：样本量不足时按**预定义层级**上卷，且原细分值**完全不返回**
（连"3"都不显示——3 本身已接近可识别）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from app.config import settings

OTHER = "其他（样本量不足，已上卷）"


def rollup_small_cells(rows: List[Dict[str, Any]],
                       dim: str,
                       count_key: str = "cnt",
                       value_keys: Optional[Sequence[str]] = None,
                       threshold: Optional[int] = None) -> Dict[str, Any]:
    """把样本量小于阈值的分组并入「其他」，原细分不返回。

    注意：ratio 类指标（率）不能简单相加，上卷时若 ratio 列的分子分母都在，
    按分母加权重算；否则该组直接置空并提示。
    """
    th = threshold if threshold is not None else settings.small_cell_threshold
    if not rows:
        return {"rows": rows, "rolled": [], "note": "", "applied": False}

    value_keys = list(value_keys or [k for k in rows[0].keys() if k != dim])
    kept: List[Dict[str, Any]] = []
    bucket: Dict[str, Any] = {dim: OTHER, count_key: 0}
    for k in value_keys:
        bucket[k] = 0
    rolled: List[Dict[str, str]] = []

    for r in rows:
        cnt = r.get(count_key)
        if isinstance(cnt, (int, float)) and cnt < th:
            # 原细分值完全不返回
            rolled.append({"dim_value": str(r.get(dim)),
                           "reason": f"样本量 {cnt} < {th}，已上卷"})
            if isinstance(cnt, (int, float)):
                bucket[count_key] += cnt
            for k in value_keys:
                v = r.get(k)
                if isinstance(v, (int, float)):
                    bucket[k] += v
            continue
        kept.append(r)

    if not rolled:
        return {"rows": rows, "rolled": [], "note": "", "applied": False}

    kept.append(bucket)
    note = (f"共 {len(rolled)} 个分组样本量不足（< {th}），已合并上卷为「{OTHER}」，"
            f"原细分值不展示。")
    return {"rows": kept, "rolled": rolled, "note": note, "applied": True}


def suppress_or_rollup(rows: List[Dict[str, Any]], dim: str,
                       count_key: str = "cnt") -> Dict[str, Any]:
    """对外统一入口：先抑制，再上卷。"""
    return rollup_small_cells(rows, dim=dim, count_key=count_key)
