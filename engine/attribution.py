#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
结构-价格两因素分解器（Shift-Share Decomposition）—— docs/10 IN-04

解决的问题：
    「住院次均费用涨了 8%」到底是病例变难了（结构），还是单价涨了（价格）？
    两者的管理动作完全相反，但传统 BI 只能回答「哪个科室涨最多」。

数学形式（代数恒等式，不含任何模型假设）：
    设分组 g（DRG 组 / 科室），w_g = 该组例数占比，p_g = 该组例均水平（费用 / 床日）
    基期 0，报告期 1，整体水平 P = Σ w_g·p_g = 总量 / 总例数

    ΔP = 结构效应 + 价格效应 + 交互项              （分组集合一致时）
       = a0·(结构_C + 价格_C + 交互_C) + 新增组效应 + 退出组效应   （存在组别增减时）

    其中 a0 为共同组在基期的例数占比；_C 表示在两期共同存在的组内计算。
    四项之和恒等于 ΔP —— 这是本模块的自测基石。

严谨性门禁（任一不通过即拒绝出结论）：
    G1 DRG 分组器版本跨期必须一致，否则基期需按新版重算
    G2 分组内例数 < min_group_size 的组强制合并为「其他」，防患者识别
    G3 交互项 |占比| > 20% 时提示「解释力有限」，绝不并入主效应
    G4 结构效应方向须与 CMI 变化方向交叉验证，矛盾时标注「需复核」
    G5 输出严禁因果词（导致/因为/由于）—— 本模块只做分解，不做因果推断

用法:
    python attribution.py --selftest
    python attribution.py --input groups.jsonl --metric 住院次均费用 --unit 元
    python attribution.py --emit-sql --dimension drg --metric-num ip_revenue
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


class DecompositionError(ValueError):
    """分解前置条件不满足，拒绝出结论。"""


EPS = 1e-9

# 因果词黑名单（G5）：本模块只陈述"由…驱动"，不做因果断言
CAUSAL_WORDS = ("导致", "因为", "由于", "造成", "致使", "归因于")


# ────────────────────────────────────────────────────────────
# 数据结构
# ────────────────────────────────────────────────────────────
@dataclass
class GroupRow:
    """分组级两期数据。amount 为总量（费用合计 / 床日合计），count 为例数。"""
    group: str
    count_0: float = 0.0
    amount_0: float = 0.0
    count_1: float = 0.0
    amount_1: float = 0.0
    name: str = ""


@dataclass
class Decomposition:
    p0: float                      # 基期整体水平
    p1: float                      # 报告期整体水平
    delta: float                   # ΔP
    delta_pct: float               # 变化率
    structure: float = 0.0         # 结构效应（已按 a0 缩放）
    price: float = 0.0             # 价格/水平效应
    interaction: float = 0.0       # 交互项（单列，禁止并入主效应）
    new_group: float = 0.0         # 新增组效应
    exited_group: float = 0.0      # 退出组效应
    share_structure: float = 0.0   # 结构效应占 ΔP 比
    share_price: float = 0.0
    share_interaction: float = 0.0
    coverage: float = 1.0          # 共同组覆盖的基期例数占比
    contributors: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    identity_ok: bool = False
    identity_residual: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "p0": round(self.p0, 6), "p1": round(self.p1, 6),
            "delta": round(self.delta, 6),
            "delta_pct": round(self.delta_pct, 6),
            "structure": round(self.structure, 6),
            "price": round(self.price, 6),
            "interaction": round(self.interaction, 6),
            "new_group": round(self.new_group, 6),
            "exited_group": round(self.exited_group, 6),
            "share_structure": round(self.share_structure, 4),
            "share_price": round(self.share_price, 4),
            "share_interaction": round(self.share_interaction, 4),
            "coverage": round(self.coverage, 4),
            "identity_ok": self.identity_ok,
            "identity_residual": f"{self.identity_residual:.2e}",
            "warnings": self.warnings,
            "top_contributors": self.contributors[:10],
        }


# ────────────────────────────────────────────────────────────
# 核心分解
# ────────────────────────────────────────────────────────────
def _merge_small_groups(rows: Sequence[GroupRow], min_size: int) -> Tuple[List[GroupRow], int]:
    """G2：小样本组合并为 __other__，防止下钻到可识别患者。"""
    keep, merged, n = [], [], 0
    agg = {"c0": 0.0, "a0": 0.0, "c1": 0.0, "a1": 0.0}
    for r in rows:
        if max(r.count_0, r.count_1) < min_size:
            agg["c0"] += r.count_0
            agg["a0"] += r.amount_0
            agg["c1"] += r.count_1
            agg["a1"] += r.amount_1
            n += 1
        else:
            keep.append(r)
    if n:
        merged.append(GroupRow(group="__other__", name="其他（小样本合并）",
                               count_0=agg["c0"], amount_0=agg["a0"],
                               count_1=agg["c1"], amount_1=agg["a1"]))
    return keep + merged, n


def decompose(rows: Sequence[GroupRow],
              min_group_size: int = 5,
              drg_version_0: Optional[str] = None,
              drg_version_1: Optional[str] = None,
              cmi_0: Optional[float] = None,
              cmi_1: Optional[float] = None,
              interaction_warn_ratio: float = 0.20) -> Decomposition:
    """结构-价格两因素分解。

    rows: 分组级两期 (count, amount)。amount/count 即该组的「例均水平」。
    """
    if not rows:
        raise DecompositionError("无分组数据，无法分解")

    # ── G1 分组器版本门禁 ──
    if drg_version_0 and drg_version_1 and drg_version_0 != drg_version_1:
        raise DecompositionError(
            f"DRG 分组器版本跨期不一致（{drg_version_0} → {drg_version_1}）："
            "基期必须按新版重算后才能分解，否则结构效应不可解释")

    warnings: List[str] = []

    # ── G2 小样本组合并 ──
    rows, n_merged = _merge_small_groups(rows, min_group_size)
    if n_merged:
        warnings.append(f"{n_merged} 个分组例数低于 {min_group_size}，"
                        "已合并为「其他」以防止患者识别")
        other = [r for r in rows if r.group == "__other__"]
        if other and max(other[0].count_0, other[0].count_1) < min_group_size:
            warnings.append("「其他」组合并后仍低于样本量阈值，"
                            "建议上卷到更高聚合层级（科室 → 院区 → 全院）")

    # 总量
    N0 = sum(r.count_0 for r in rows)
    N1 = sum(r.count_1 for r in rows)
    A0 = sum(r.amount_0 for r in rows)
    A1 = sum(r.amount_1 for r in rows)
    if N0 <= 0 or N1 <= 0:
        raise DecompositionError("基期或报告期总例数为 0，无法分解")

    p0, p1 = A0 / N0, A1 / N1
    delta = p1 - p0
    delta_pct = delta / p0 if p0 else 0.0

    # 分组分类：共同组 C / 新增组 N / 退出组 X
    common = [r for r in rows if r.count_0 > 0 and r.count_1 > 0]
    new_g = [r for r in rows if r.count_0 <= 0 and r.count_1 > 0]
    exit_g = [r for r in rows if r.count_1 <= 0 and r.count_0 > 0]

    if not common:
        raise DecompositionError("两期无共同分组，无法做结构-价格分解")

    N0_C = sum(r.count_0 for r in common)
    N1_C = sum(r.count_1 for r in common)
    N1_N = sum(r.count_1 for r in new_g)
    N0_X = sum(r.count_0 for r in exit_g)

    a0, a1 = N0_C / N0, N1_C / N1
    b1, b0 = N1_N / N1, N0_X / N0

    # 共同组内归一化权重与水平
    P0_C = sum((r.count_0 / N0_C) * (r.amount_0 / r.count_0) for r in common)
    P1_C = sum((r.count_1 / N1_C) * (r.amount_1 / r.count_1) for r in common)
    P1_N = sum((r.count_1 / N1_N) * (r.amount_1 / r.count_1) for r in new_g) if N1_N else 0.0
    P0_X = sum((r.count_0 / N0_X) * (r.amount_0 / r.count_0) for r in exit_g) if N0_X else 0.0

    # 共同组内三项分解
    struct_C = price_C = inter_C = 0.0
    contrib: List[Dict[str, Any]] = []
    for r in common:
        w0, w1 = r.count_0 / N0_C, r.count_1 / N1_C
        q0, q1 = r.amount_0 / r.count_0, r.amount_1 / r.count_1
        s = (w1 - w0) * q0
        p = w0 * (q1 - q0)
        i = (w1 - w0) * (q1 - q0)
        struct_C += s
        price_C += p
        inter_C += i
        contrib.append({
            "group": r.group, "name": r.name or r.group,
            "count_0": r.count_0, "count_1": r.count_1,
            "level_0": round(q0, 4), "level_1": round(q1, 4),
            "structure": s, "price": p, "interaction": i,
            "total": s + p + i,
        })

    structure = a0 * struct_C
    price = a0 * price_C
    interaction = a0 * inter_C
    new_effect = b1 * (P1_N - P1_C)
    exit_effect = b0 * (P1_C - P0_X)

    # ── 恒等式校验（自测基石）──
    total = structure + price + interaction + new_effect + exit_effect
    residual = abs(total - delta)
    tol = max(1e-9, abs(delta) * 1e-9)
    identity_ok = residual <= tol
    if not identity_ok:
        raise DecompositionError(
            f"恒等式校验失败：四项之和 {total:.6f} ≠ ΔP {delta:.6f}"
            f"（残差 {residual:.2e}），分解实现有 bug，拒绝出结论")

    result = Decomposition(
        p0=p0, p1=p1, delta=delta, delta_pct=delta_pct,
        structure=structure, price=price, interaction=interaction,
        new_group=new_effect, exited_group=exit_effect,
        coverage=a0,
        identity_ok=identity_ok, identity_residual=residual,
    )

    if delta:
        result.share_structure = structure / delta
        result.share_price = price / delta
        result.share_interaction = interaction / delta

    contrib.sort(key=lambda x: -abs(x["total"]))
    result.contributors = contrib

    # ── G3 交互项门禁 ──
    if delta and abs(interaction / delta) > interaction_warn_ratio:
        warnings.append(
            f"交互项占 ΔP 的 {abs(interaction / delta):.0%}（阈值 "
            f"{interaction_warn_ratio:.0%}）：结构与价格同时发生显著变化，"
            "分解解释力有限，不建议据此下管理结论")

    # ── G4 CMI 交叉验证 ──
    if cmi_0 is not None and cmi_1 is not None:
        d_cmi = cmi_1 - cmi_0
        if d_cmi != 0 and structure != 0 and (d_cmi > 0) != (structure > 0):
            warnings.append(
                f"交叉验证未通过：结构效应为 {structure:+.4f}（"
                f"{'推高' if structure > 0 else '拉低'}），"
                f"但 CMI 由 {cmi_0} 变为 {cmi_1}（{d_cmi:+.3f}），方向矛盾，分解需复核")
        elif d_cmi != 0 and structure != 0:
            warnings.append(f"[INFO] 交叉验证通过：结构效应方向与 CMI 变化一致"
                            f"（CMI {cmi_0} → {cmi_1}）")

    if result.coverage < 0.9:
        warnings.append(f"共同组仅覆盖基期 {result.coverage:.0%} 的病例，"
                        "组别增减对结果影响较大")

    result.warnings = warnings
    return result


# ────────────────────────────────────────────────────────────
# 解读渲染（G5：禁止因果词）
# ────────────────────────────────────────────────────────────
def render(d: Decomposition, metric_name: str = "指标",
           unit: str = "", dim_name: str = "分组",
           level_name: str = "水平") -> str:
    """渲染解读文本。

    level_name 必须按指标语义传入（费用 / 住院日 / 例均…）。
    ★ 不可写死为「费用」——用于平均住院日时会产生错误表述。
    """
    u = unit or ""
    pct = f"{d.delta_pct:+.1%}"
    lines = [
        f"{metric_name}：{d.p0:,.2f}{u} → {d.p1:,.2f}{u}（{pct}，{d.delta:+,.2f}{u}）",
        "",
        f"  · 结构效应     {d.structure:+10.4f}{u}   占 {d.share_structure:6.1%}"
        f"   {dim_name}构成变化（按基期{level_name}计价）",
        f"  · {level_name}效应  {d.price:+10.4f}{u}   占 {d.share_price:6.1%}"
        f"   各组{level_name}变化（按基期结构加权）",
        f"  · 交互项       {d.interaction:+10.4f}{u}   占 {d.share_interaction:6.1%}"
        f"   （单列，不并入主效应）",
    ]
    if abs(d.new_group) > EPS or abs(d.exited_group) > EPS:
        lines += [
            f"  · 新增{dim_name}效应 {d.new_group:+8.4f}{u}",
            f"  · 退出{dim_name}效应 {d.exited_group:+8.4f}{u}",
        ]
    lines.append("")

    if abs(d.structure) >= abs(d.price):
        lines.append(f"结论方向：变化主要由【病例结构变化】驱动"
                     f"——{dim_name}构成改变，各组{level_name}按基期计价。")
    else:
        lines.append(f"结论方向：变化主要由【各{dim_name}{level_name}变化】驱动"
                     f"——病例结构影响很小。")

    if d.contributors:
        top = d.contributors[0]
        lines.append(
            f"贡献最大的{dim_name}：{top['name']}"
            f"（结构 {top['structure']:+.4f} / {level_name} {top['price']:+.4f}）")

    lines.append("")
    lines.append("※ 本结果为会计式分解（恒等式），非因果推断；"
                 "不做显著性检验，不构成因果结论。")
    for w in d.warnings:
        lines.append(f"⚠ {w}" if not w.startswith("[INFO]") else f"{w}")

    text = "\n".join(lines)
    assert_no_causal(text)
    return text


def assert_no_causal(text: str) -> None:
    """G5：输出中不得出现因果词。出现即视为实现缺陷。"""
    hits = [w for w in CAUSAL_WORDS if w in text]
    if hits:
        raise DecompositionError(f"输出包含因果词 {hits}：本模块只做分解，不做因果断言")


# ────────────────────────────────────────────────────────────
# SQL 生成（两期分组聚合）
# ────────────────────────────────────────────────────────────
def build_two_period_sql(*, table: str, date_col: str, group_col: str,
                         count_expr: str, amount_expr: str,
                         span0: Tuple[dt.date, dt.date],
                         span1: Tuple[dt.date, dt.date],
                         extra_where: str = "",
                         dept_scope: Optional[List[str]] = None,
                         min_group_size: int = 5,
                         rls_dept_col: str = "belong_dept_code") -> str:
    """生成两期分组聚合 SQL（UNION ALL）。

    ★ 与 sql_builder 保持一致的正确性约束：半开区间、行级权限 SQL 层注入、
      分组例数下限（HAVING）。

    ★ `rls_dept_col` 由调用方给出**该数据源上真实的列名**（语义层的 belong 口径
      经物理映射后的结果）。默认值只是让单测能直接调用；调用方写死逻辑名会让
      SQL 引用一个物理上不存在的列（详见 sql_builder._rls_dept_col 的注释）。
    """
    def one(period: str, span):
        conds = [f"{date_col} >= '{span[0].isoformat()}'",
                 f"{date_col} <  '{span[1].isoformat()}'"]
        if extra_where:
            conds.append(extra_where)
        if dept_scope and "*" not in dept_scope:
            codes = ", ".join(f"'{c}'" for c in dept_scope)
            conds.append(f"{table}.{rls_dept_col} IN ({codes})")
        return (
            f"SELECT '{period}' AS period, {table}.{group_col} AS grp, "
            f"{count_expr} AS cnt, {amount_expr} AS amt\n"
            f"FROM {table}\nWHERE " + "\n  AND ".join(conds) +
            f"\nGROUP BY {table}.{group_col}"
            f"\nHAVING {count_expr} >= {min_group_size}"
        )

    return (one("0", span0) + "\nUNION ALL\n" + one("1", span1)
            + "\nORDER BY grp, period")


def rows_from_sql_result(records: Sequence[Tuple]) -> List[GroupRow]:
    """把 SQL 结果（period, grp, cnt, amt）转换为 GroupRow 列表。"""
    acc: Dict[str, GroupRow] = {}
    for period, grp, cnt, amt in records:
        r = acc.setdefault(str(grp), GroupRow(group=str(grp)))
        cnt = float(cnt or 0)
        amt = float(amt or 0)
        if str(period) == "0":
            r.count_0, r.amount_0 = cnt, amt
        else:
            r.count_1, r.amount_1 = cnt, amt
    return list(acc.values())


# ────────────────────────────────────────────────────────────
# 自测
# ────────────────────────────────────────────────────────────
def _rand_rows(k: int = 8, seed: int = 42) -> List[GroupRow]:
    rng = random.Random(seed)
    rows = []
    for i in range(k):
        c0 = rng.randint(20, 300)
        c1 = rng.randint(20, 300)
        q0 = rng.uniform(500, 30000)
        q1 = q0 * rng.uniform(0.7, 1.4)
        rows.append(GroupRow(group=f"G{i}", name=f"组{i}",
                             count_0=c0, amount_0=c0 * q0,
                             count_1=c1, amount_1=c1 * q1))
    return rows


def selftest() -> bool:
    passed = True

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal passed
        tag = "[OK]  " if cond else "[FAIL]"
        if not cond:
            passed = False
        print(f"{tag} {name}" + (f"  {detail}" if detail else ""))

    # 1. 恒等式（含随机数据）
    for seed in range(20):
        rows = _rand_rows(seed=seed)
        d = decompose(rows)
        s = d.structure + d.price + d.interaction + d.new_group + d.exited_group
        if abs(s - d.delta) > max(1e-9, abs(d.delta) * 1e-9):
            check("恒等式（随机 20 组）", False, f"seed={seed} 残差={s - d.delta:.2e}")
            break
    else:
        check("恒等式（随机 20 组）", True, "结构+价格+交互+组别增减 = ΔP")

    # 2. 纯结构变化：各组水平不变，仅构成变化 → 价格效应 ≈ 0
    rows = [GroupRow("A", 100, 100 * 1000, 200, 200 * 1000),
            GroupRow("B", 100, 100 * 5000, 50, 50 * 5000)]
    d = decompose(rows)
    check("纯结构变化 → 价格效应 ≈ 0", abs(d.price) < 1e-6,
          f"price={d.price:.2e}, structure={d.structure:+.2f}")

    # 3. 纯价格变化：构成不变，各组水平等比上涨 → 结构效应 ≈ 0
    rows = [GroupRow("A", 100, 100 * 1000, 100, 100 * 1200),
            GroupRow("B", 100, 100 * 5000, 100, 100 * 6000)]
    d = decompose(rows)
    check("纯价格变化 → 结构效应 ≈ 0", abs(d.structure) < 1e-6,
          f"structure={d.structure:.2e}, price={d.price:+.2f}")
    check("纯价格变化 → ΔP = 20%", abs(d.delta_pct - 0.2) < 1e-9,
          f"{d.delta_pct:.4f}")

    # 4. 组别增减：新增/退出组存在时恒等式仍成立
    rows = [GroupRow("A", 100, 100 * 1000, 120, 120 * 1100),
            GroupRow("B", 100, 100 * 5000, 0, 0),          # 退出组
            GroupRow("C", 0, 0, 80, 80 * 20000)]           # 新增组
    d = decompose(rows)
    s = d.structure + d.price + d.interaction + d.new_group + d.exited_group
    check("组别增减时恒等式仍成立", abs(s - d.delta) < 1e-6,
          f"残差={s - d.delta:.2e}, 新增组效应={d.new_group:+.2f}")

    # 5. G1 分组器版本门禁
    try:
        decompose(_rand_rows(), drg_version_0="V1.0", drg_version_1="V1.1")
        check("G1 分组器版本门禁", False, "未被拦截")
    except DecompositionError:
        check("G1 分组器版本门禁", True, "版本不一致已拒绝")

    # 6. G2 小样本组合并
    rows = [GroupRow("A", 100, 100 * 1000, 100, 100 * 1100),
            GroupRow("tiny", 2, 2 * 9999, 3, 3 * 9999)]
    d = decompose(rows, min_group_size=5)
    check("G2 小样本组合并", any("合并" in w for w in d.warnings),
          f"warnings={d.warnings}")

    # 7. G3 交互项告警
    rows = [GroupRow("A", 100, 100 * 1000, 300, 300 * 2000),
            GroupRow("B", 300, 300 * 3000, 100, 100 * 1500)]
    d = decompose(rows)
    check("G3 交互项超阈值告警",
          any("交互项占" in w for w in d.warnings), f"{d.share_interaction:.1%}")

    # 8. G4 CMI 交叉验证
    #    数据须让「高费用组占比上升」→ 结构效应为正 → 与 CMI 上升方向一致
    rows = [GroupRow("A", 200, 200 * 1000, 100, 100 * 1300),   # 低费用组占比下降
            GroupRow("B", 100, 100 * 5000, 200, 200 * 5200)]   # 高费用组占比上升
    d = decompose(rows, cmi_0=1.10, cmi_1=1.25)
    check("G4 CMI 方向一致 → 通过",
          any("交叉验证通过" in w for w in d.warnings),
          f"structure={d.structure:+.2f}")
    d2 = decompose(rows, cmi_0=1.25, cmi_1=1.10)
    check("G4 CMI 方向矛盾 → 告警",
          any("交叉验证未通过" in w for w in d2.warnings),
          f"structure={d2.structure:+.2f}")

    # 9. G5 输出无因果词
    d = decompose(_rand_rows())
    txt = render(d, metric_name="住院次均费用", unit="元", dim_name="DRG组")
    check("G5 输出无因果词", not any(w in txt for w in CAUSAL_WORDS))

    # 10. 空数据与无共同组
    for bad, label in ((_rand_rows()[:0], "空数据"),
                       ([GroupRow("A", 100, 1000, 0, 0),
                         GroupRow("B", 0, 0, 100, 1000)], "无共同组")):
        try:
            decompose(bad)
            check(f"拒绝分解：{label}", False)
        except DecompositionError:
            check(f"拒绝分解：{label}", True)

    # 11. Golden 归因用例
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "eval", "golden_attribution.jsonl")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cases = [json.loads(l) for l in f if l.strip()]
        ok = True
        for c in cases:
            try:
                rows = [GroupRow(**g) for g in c["groups"]]
                d = decompose(rows, drg_version_0=c.get("drg_version_0"),
                              drg_version_1=c.get("drg_version_1"),
                              cmi_0=c.get("cmi_0"), cmi_1=c.get("cmi_1"))
                expect = c.get("expect", {})
                if expect.get("reject"):
                    ok = False
                    print(f"       {c['id']} 应被拒绝但通过")
                    continue
                good = True
                if "dominant" in expect:
                    actual = "structure" if abs(d.structure) >= abs(d.price) else "price"
                    good &= (actual == expect["dominant"])
                if "delta_pct_gt" in expect:
                    good &= d.delta_pct > expect["delta_pct_gt"]
                if "delta_pct_lt" in expect:
                    good &= d.delta_pct < expect["delta_pct_lt"]
                if "warn_contains" in expect:
                    good &= any(expect["warn_contains"] in w for w in d.warnings)
                if not good:
                    ok = False
                    print(f"       {c['id']} 不符预期: {d.as_dict()}")
            except DecompositionError as e:
                if not c.get("expect", {}).get("reject"):
                    ok = False
                    print(f"       {c['id']} 意外被拒绝: {e}")
        check(f"Golden 归因用例（{len(cases)} 条）", ok)
    else:
        print("[SKIP] 未找到 eval/golden_attribution.jsonl")

    return passed


# ────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="结构-价格两因素分解器")
    ap.add_argument("--selftest", action="store_true", help="运行自测")
    ap.add_argument("--input", help="分组数据 JSONL：[{group,count_0,amount_0,count_1,amount_1}]")
    ap.add_argument("--metric", default="指标")
    ap.add_argument("--unit", default="")
    ap.add_argument("--dim-name", default="分组")
    ap.add_argument("--level-name", default="水平",
                    help="被分解的水平语义：费用 / 住院日 / 例均（不可写死为费用）")
    ap.add_argument("--drg-version-0")
    ap.add_argument("--drg-version-1")
    ap.add_argument("--cmi-0", type=float)
    ap.add_argument("--cmi-1", type=float)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if args.selftest:
        print("=" * 66)
        print("结构-价格分解器 自测")
        print("=" * 66)
        ok = selftest()
        print("=" * 66)
        print("自测结果: " + ("全部通过" if ok else "存在失败"))
        return 0 if ok else 1

    if not args.input:
        ap.error("需要 --input 或 --selftest")

    with open(args.input, encoding="utf-8") as f:
        rows = [GroupRow(**g) for g in json.load(f)] \
            if args.input.endswith(".json") else \
            [GroupRow(**json.loads(l)) for l in f if l.strip()]

    try:
        d = decompose(rows, drg_version_0=args.drg_version_0,
                      drg_version_1=args.drg_version_1,
                      cmi_0=args.cmi_0, cmi_1=args.cmi_1)
    except DecompositionError as e:
        print(f"[拒绝出结论] {e}")
        return 2

    if args.json:
        print(json.dumps(d.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(d, args.metric, args.unit, args.dim_name, args.level_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
