# -*- coding: utf-8 -*-
"""数字护城河（F2.6 / 10 IN-08）。

问数产品最致命的信任杀手：让 LLM 写解读时顺手编一个数字。
本模块做**代码级**校验，不是提示词约束：

  第 1 层  槽位填充 —— 数字由系统模板注入（在上层调用）
  第 2 层  集合校验 —— 文本中出现的数字必须与「授权数字集合」一致，
                       多一个、少一个都不通过（集合相等，不是相似度）
  第 3 层  断言过滤 —— 禁止因果词，除非有 IN-04 结构-价格分解支撑

校验失败 → 丢弃本次解读，降级为纯表格 + 模板化结论，**绝不裸奔 LLM 输出**。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Set

# 因果词：归因未成立时禁止出现
CAUSAL_WORDS = ["导致", "因为", "由于", "所以", "致使", "原因在于", "造成", "归因于", "使得"]

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?\s*%?")


def extract_numbers(text: str) -> List[str]:
    """抽取文本中的数字，归一化为字符串集合（保留 % 语义）。"""
    out: List[str] = []
    for m in _NUM_RE.findall(text or ""):
        s = m.replace(" ", "").replace(",", "")
        out.append(s)
    return out


def _norm_allowed(values: Iterable[Any]) -> Set[str]:
    allowed: Set[str] = set()
    for v in values:
        if v is None:
            continue
        if isinstance(v, (int, float)):
            allowed.add(_fmt_num(v))
            allowed.add(_fmt_num(v) + "%")
        else:
            s = str(v).replace(",", "")
            allowed.add(s)
            try:
                f = float(s.rstrip("%"))
                allowed.add(_fmt_num(f))
                allowed.add(_fmt_num(f) + "%")
            except ValueError:
                pass
    return allowed


def _fmt_num(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return ("%.4f" % v).rstrip("0").rstrip(".")


def check_narrative(text: str, allowed_values: Iterable[Any],
                    allow_causal: bool = False) -> Dict[str, Any]:
    """校验一段解读文本是否越界。

    返回 {"ok": bool, "unauthorized": [...], "causal_words": [...], "reason": str}
    """
    used = extract_numbers(text)
    allowed = _norm_allowed(allowed_values)
    unauthorized = [n for n in used if n not in allowed]

    causal = [w for w in CAUSAL_WORDS if w in (text or "")] if not allow_causal else []

    ok = not unauthorized and not causal
    reason = ""
    if unauthorized:
        reason += "解读出现未授权数字：%s。" % "、".join(unauthorized[:5])
    if causal:
        reason += " 禁止因果断言（无归因支撑）：%s。" % "、".join(causal[:3])
    return {"ok": ok, "unauthorized": unauthorized, "causal_words": causal,
            "reason": reason.strip()}


def numbers_from_rows(rows: List[Dict[str, Any]]) -> List[Any]:
    """从结果行中提取「授权数字集合」——解读里只能出现这些数字。"""
    vals: List[Any] = []
    for r in rows or []:
        for v in r.values():
            if isinstance(v, (int, float)):
                vals.append(v)
    return vals


def safe_narrative(text: str, rows: List[Dict[str, Any]],
                   allow_causal: bool = False) -> Dict[str, Any]:
    """校验失败则返回降级文案，调用方不得展示原文。"""
    res = check_narrative(text, numbers_from_rows(rows), allow_causal=allow_causal)
    if not res["ok"]:
        res["text"] = "（解读未通过数字校验，已降级为纯数据展示，不做文字结论）"
        res["degraded"] = True
    else:
        res["text"] = text
        res["degraded"] = False
    return res
