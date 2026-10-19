# -*- coding: utf-8 -*-
"""物理映射的可视化编辑后端（F5.3）：读取 / 校验 / 覆盖度诊断 / 保存 / 回滚。

★ 为什么值得做界面：
    「接一家医院」的边际成本几乎全在物理映射上：表名、列名、表达式各不相同。
    如果每次都要工程师登上跳板机改 YAML，第二家医院就开始亏 —— 这是规模化的卡点，
    不是"体验优化"。

★ 为什么保存必须先校验、且必须留备份：
    映射改错不会报错，只会**查不出数或查错数**。而且它是全院共享的一份配置，
    一个人改错，所有人跟着错。所以：
      1) 结构校验（类型/取值/危险键）不过就不让保存
      2) 保存前自动备份，回滚是一条命令
      3) 保存后立即热加载并重建管线，避免"改了没生效"的静默失效

★ 不做的事：
    不在服务端做 SQL 试跑（那要走限流、要连库，属于"验证"而不是"保存"）。
    覆盖度诊断只回答"配置上还差什么"，不回答"数据库里到底有没有这一列"。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ALLOWED_DIALECTS = {"oracle", "sqlite", "doris", "mysql"}
_MAP_KEYS = {"hospital", "dialect", "tables", "columns", "exprs", "measures",
             "dimensions", "relations", "fail_if_unmapped", "version", "route",
             "backend", "notes", "note",
             # 口径级物理列覆盖（按 date_basis / dept_basis 分档），实际存在于
             # physical_map_his.yaml —— 白名单漏了它们会把现有合法配置判成"拼错"
             "date_basis", "dept_basis"}
_DICT_SECTIONS = ("tables", "columns", "exprs", "measures", "dimensions",
                  "date_basis", "dept_basis")
_BACKUP_RE = r"\.bak\.\d{8}-\d{6}$"
_FORBIDDEN_KEY = re.compile(r"(password|passwd|pwd|secret|token|api[_-]?key)", re.I)


class MapValidationError(ValueError):
    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _path() -> Path:
    from app.config import settings
    return Path(settings.physical_map_path)


# ────────────────────────────────────────────────────────────
# 读取
# ────────────────────────────────────────────────────────────
def read() -> Dict[str, Any]:
    p = _path()
    if not p.exists():
        return {"path": str(p), "exists": False, "text": "", "sha": None,
                "mtime": None, "size": 0}
    text = p.read_text(encoding="utf-8")
    st = p.stat()
    import hashlib
    return {
        "path": str(p), "exists": True, "text": text,
        "sha": hashlib.sha1(text.encode("utf-8")).hexdigest()[:12],
        "mtime": st.st_mtime, "size": st.st_size,
    }


def parse(text: str) -> Dict[str, Any]:
    import yaml
    try:
        data = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        raise MapValidationError([f"YAML 解析失败：{e}"]) from e
    if not isinstance(data, dict):
        raise MapValidationError(["顶层必须是映射（key: value），当前不是"])
    return data


# ────────────────────────────────────────────────────────────
# 校验
# ────────────────────────────────────────────────────────────
def validate(data: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    unknown = set(data) - _MAP_KEYS
    if unknown:
        errs.append(f"未知顶层字段：{', '.join(sorted(unknown))}"
                    f"（拼错不会报错、只会静默不生效，所以这里直接拦）")

    dialect = data.get("dialect")
    if dialect is not None and str(dialect).lower() not in _ALLOWED_DIALECTS:
        errs.append(f"dialect={dialect!r} 不支持（可选：{', '.join(sorted(_ALLOWED_DIALECTS))}）")

    for sec in _DICT_SECTIONS:
        if sec not in data or data[sec] is None:
            continue
        v = data[sec]
        if not isinstance(v, dict):
            errs.append(f"{sec} 必须是映射（key: value）")
            continue
        for k, val in v.items():
            if isinstance(val, dict):          # measures.<code> 是嵌套映射，合法
                if sec != "measures":
                    errs.append(f"{sec}.{k} 不应是嵌套结构")
                continue
            if val is None:
                continue
            if isinstance(val, bool):
                errs.append(f"{sec}.{k} 是布尔值：如果本意是 YAML 键 `on`，"
                            f"请写成 join_on（YAML 1.1 会把裸 on 解析成 True）")
                continue
            if not isinstance(val, str):
                errs.append(f"{sec}.{k} 必须是字符串，当前是 {type(val).__name__}")
            elif not val.strip():
                errs.append(f"{sec}.{k} 是空字符串：若要表示「该列不存在」请写 null")

    # 关系声明的结构检查（写错会导致 JOIN 条件缺失，且要到查数时才暴露）
    rels = data.get("relations")
    if rels is not None:
        if not isinstance(rels, list):
            errs.append("relations 必须是列表")
        else:
            for i, r in enumerate(rels):
                if not isinstance(r, dict):
                    errs.append(f"relations[{i}] 必须是映射")
                    continue
                for k in ("from", "to", "type"):
                    if not r.get(k):
                        errs.append(f"relations[{i}] 缺少 {k}")
                if True in r or "on" in r:
                    errs.append(f"relations[{i}] 使用了 `on` 键：YAML 会把它解析成布尔 True，"
                                f"请改名 join_on")
                if str(r.get("type")) not in ("many_to_one", "one_to_one"):
                    errs.append(f"relations[{i}].type={r.get('type')!r} 不允许"
                                f"（只接受 many_to_one / one_to_one）")

    # 凭据绝不进映射文件：这份文件会随项目交付给下一位实施同事
    if _FORBIDDEN_KEY.search(str(list(data.keys()))):
        errs.append("映射文件里出现了疑似凭据字段：连接信息请走环境变量 / sources.yaml")

    return errs


# ────────────────────────────────────────────────────────────
# 覆盖度诊断（配置层面，不连库）
# ────────────────────────────────────────────────────────────
def diagnose(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """回答两个问题：① 哪些指标在这份映射下"接不上" ② 哪些列被显式标记为不可用。"""
    import yaml
    from app.config import settings

    data = data if data is not None else parse(read()["text"] or "{}")
    try:
        raw_reg = yaml.safe_load(Path(settings.registry_path).read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"语义层读取失败：{e}"}

    tables = data.get("tables") or {}
    cols = data.get("columns") or {}
    measure_tbl = {k: (v or {}).get("table") for k, v in (data.get("measures") or {}).items()
                   if isinstance(v, dict)}
    fail_unmapped = bool(data.get("fail_if_unmapped"))

    measures = {m.get("code"): m for m in (raw_reg.get("measures") or [])}

    def _measure_codes(met: Dict[str, Any]) -> List[str]:
        """指标 → 参与的 measure 码。ratio 指标没有 measure，要走分子/分母。"""
        out: List[str] = []
        if met.get("measure"):
            out.append(str(met["measure"]))
        for key in ("numerator", "denominator"):
            v = met.get(key)
            out.extend(str(x) for x in (v if isinstance(v, list) else [v]) if x)
        return list(dict.fromkeys(out))

    mapped, unmapped = [], []
    for met in raw_reg.get("metrics") or []:
        mc = met.get("code")
        codes = _measure_codes(met)
        logical = None
        for c in codes:
            logical = measure_tbl.get(c) or (measures.get(c) or {}).get("table")
            if logical:
                break
        if not logical:
            unmapped.append({"metric": mc, "name": met.get("name"), "table": None,
                             "why": "该指标未关联到任何 measure"
                                    "（语义层定义不完整，先修本体再谈映射）"})
            continue
        row = {"metric": mc, "name": met.get("name"), "table": logical,
               "physical": tables.get(logical)}
        if logical in tables:
            mapped.append(row)
        else:
            row["why"] = ("映射未声明该表，SQL 将直接使用逻辑表名"
                          + ("；fail_if_unmapped=true，这类指标会被拒绝出数"
                             if fail_unmapped else ""))
            unmapped.append(row)

    # 显式不可用列（值为 null）：这是"故意不接"，必须让实施人员看见影响
    # ★ 影响范围要按"用到它的功能"说，而不是只列指标 —— 例如 campus_code 影响的是
    #   院区围栏（RLS），它根本不出现在任何 measure 里，只搜 measure 会得出"无影响"。
    null_cols = []
    for k, v in cols.items():
        if v is None:
            affected = [met.get("code") for met in (raw_reg.get("metrics") or [])
                        if any(k in str(measures.get(c) or {}) for c in _measure_codes(met))]
            impact = []
            if k.startswith("campus"):
                impact.append("院区围栏（F6.2）不可启用：启用后会被明确拒绝，而不是拼错 SQL")
            if k.startswith(("dept", "belong")):
                impact.append("科室下钻与行级权限（RLS）依赖该列")
            if not impact and not affected:
                impact.append("未发现直接依赖；确认是否已废弃")
            null_cols.append({"column": k, "affected_metrics": affected,
                              "impact": impact})

    # 关系目标表
    rel_issues = []
    for r in (data.get("relations") or []):
        to = (r or {}).get("to")
        if to and to not in tables:
            rel_issues.append({"relation": r.get("name"), "to": to,
                               "note": "维表未在 tables 中映射，切到 resolve: join 时需补"})

    total = len(mapped) + len(unmapped)
    return {
        "ok": True,
        "coverage": round(len(mapped) / total, 4) if total else 0.0,
        "mapped_count": len(mapped), "total": total,
        "mapped": mapped,
        "unmapped": unmapped,
        "null_columns": null_cols,
        "relation_issues": rel_issues,
        "fail_if_unmapped": fail_unmapped,
        "note": "只做配置层诊断：『已映射』不等于数据库里一定有这张表/这一列，"
                "上线前仍需在目标库跑一次自测查询。",
    }


# ────────────────────────────────────────────────────────────
# 备份 / 保存 / 回滚
# ────────────────────────────────────────────────────────────
def backups(limit: int = 20) -> List[Dict[str, Any]]:
    p = _path()
    if not p.parent.exists():
        return []
    out = []
    for f in p.parent.iterdir():
        if f.is_file() and re.search(rf"^{re.escape(p.name)}{_BACKUP_RE}", f.name):
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:limit]


def _backup_current() -> Optional[str]:
    p = _path()
    if not p.exists():
        return None
    name = f"{p.name}.bak.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(p, p.with_name(name))
    return name


def save(text: str, actor: str = "", note: str = "") -> Dict[str, Any]:
    """校验 → 备份 → 原子写入 → 热加载。失败时**原文件不动**。"""
    p = _path()
    data = parse(text)
    errs = validate(data)
    if errs:
        raise MapValidationError(errs)

    backup = _backup_current()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)                      # 原子替换：不会留下半截文件

    reload_info: Dict[str, Any] = {}
    try:
        from app.core import engine_bridge
        from app.core import physical_map as pm
        from app.core import registry as reg
        pm.load(force=True)
        reg.load_registry(force=True)
        engine_bridge.get_pipeline(force=True)
        reload_info = {"ok": True, "version": reg.registry_version()}
    except Exception as e:  # noqa: BLE001 —— 配置已落盘，热加载失败要如实报，便于回滚
        reload_info = {"ok": False, "error": str(e),
                       "hint": "配置已写入但热加载失败，可回滚到上一版本"}

    return {"ok": True, "path": str(p), "backup": backup,
            "reload": reload_info, "diagnose": diagnose(data),
            "saved_by": actor, "note": note}


def restore(name: str, actor: str = "") -> Dict[str, Any]:
    p = _path()
    if not re.search(rf"^{re.escape(p.name)}{_BACKUP_RE}", name or ""):
        raise MapValidationError([f"备份文件名不合法：{name}"])
    src = p.with_name(name)
    if not src.exists() or src.parent != p.parent:
        raise MapValidationError([f"备份不存在：{name}"])
    text = src.read_text(encoding="utf-8")
    data = parse(text)
    errs = validate(data)
    if errs:                                # 备份本身坏掉也要拦住，别把坏档写回去
        raise MapValidationError([f"备份文件本身校验不通过：{e}" for e in errs])
    _backup_current()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    from app.core import engine_bridge
    from app.core import physical_map as pm
    from app.core import registry as reg
    pm.load(force=True)
    reg.load_registry(force=True)
    engine_bridge.get_pipeline(force=True)
    return {"ok": True, "restored_from": name, "restored_by": actor,
            "diagnose": diagnose(data)}


# ────────────────────────────────────────────────────────────
# 自测
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    good = {"dialect": "oracle", "tables": {"t": "MEDREC.T"}, "columns": {"c": "C"},
            "exprs": {"e": "1"}, "measures": {"m": {"table": "t"}},
            "fail_if_unmapped": True}
    check("合法映射无报错", validate(good) == [])

    check("未知顶层字段被拦（拼错会静默不生效）",
          any("未知顶层字段" in e for e in validate({**good, "tabel": {}})))
    check("空字符串列名被拦",
          any("空字符串" in e for e in validate({**good, "columns": {"c": ""}})))
    check("布尔值列名被拦（YAML on → True 的坑）",
          any("布尔值" in e for e in validate({**good, "columns": {"on": True}})))
    check("relations 里的 on 键被拦",
          any("join_on" in e for e in validate(
              {**good, "relations": [{"from": "a", "to": "b", "type": "many_to_one",
                                      "on": {"x": {"left": "l", "right": "r"}}}]})))
    check("一对多关系被拦",
          any("many_to_one" in e for e in validate(
              {**good, "relations": [{"from": "a", "to": "b", "type": "one_to_many"}]})))
    check("凭据字段被拦",
          any("凭据" in e for e in validate({**good, "password": "x"})))

    diag = diagnose(good)
    check("诊断能算出覆盖度", diag["ok"] and "coverage" in diag,
          f"coverage={diag.get('coverage')}")
    check("诊断列出未映射指标", isinstance(diag.get("unmapped"), list))

    p = _path()
    check("备份文件名校验会拒绝路径穿越",
          _safe_backup_name(p.name + ".bak.20260101-000000")
          and not _safe_backup_name("../evil.bak.20260101-000000"))

    # ⑥ 真实映射文件必须能过校验 —— 否则"打开界面就报错"，功能等于不可用。
    #    这条同时防止：将来往 yaml 里加了新顶层字段，却忘了同步白名单。
    for f in (p, p.with_name(p.name.replace("_his", "_ads"))):
        if not f.exists():
            continue
        errs = validate(parse(f.read_text(encoding="utf-8")))
        check(f"{f.name} 通过校验", errs == [], "; ".join(errs)[:120])
    return passed


def _safe_backup_name(name: str) -> bool:
    p = _path()
    return bool(re.fullmatch(rf"{re.escape(p.name)}{_BACKUP_RE}", name or ""))


if __name__ == "__main__":
    import sys
    ok = selftest()
    print("物理映射自测: " + ("全部通过" if ok else "存在失败"))
    sys.exit(0 if ok else 1)
