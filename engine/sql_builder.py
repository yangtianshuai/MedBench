"""语义层 → SQL 生成器（问数引擎的「正确性地基」）。

严谨性约束（每一条都对应一个真实的事故场景）：
  1. ratio 指标必须 SUM(分子)/NULLIF(SUM(分母),0)，禁止 AVG(ratio)
     —— 反例：全院平均住院日 ≠ 各科室平均住院日的算术平均
  2. 日期一律半开区间 [start, end)，禁止 BETWEEN
     —— 反例：BETWEEN '2026-09-01' AND '2026-09-30' 会漏掉 9/30 有时分秒的记录
  3. 除法一律 NULLIF(...,0)，禁止裸除
  4. 时间解析由代码完成，绝不接受模型算出的日期字符串
  5. 行级权限(RLS)在 SQL 层强制注入，应用层过滤无效（下钻会绕过）
  6. 小样本（< min_cell_size）用 HAVING 抑制，不做前端过滤
  7. forbidden_fields 永不出 SELECT
  8. 维度**默认长在事实表上**（不 JOIN）；只有显式声明 `resolve: join` 才连维表。
     切到 JOIN 模式后，事实与维表一律用别名限定，不留裸列 —— 两表同名列（dept_code /
     visit_id）在 JOIN 后不加限定会直接报 ambiguous，而这类报错只在"有人点了下钻"
     时才出现，最难在上线前发现。
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, List, Optional, Tuple

from dsl import ALLOWED_FILTER_OPS, QueryDSL, validate_dsl, DSLError


# ────────────────────────────────────────────────────────────
# 方言差异（默认 Doris/MySQL，可切换）
# ────────────────────────────────────────────────────────────
DIALECTS = {
    "doris": {
        "month": "DATE_FORMAT({c}, '%Y-%m')",
        "quarter": "CONCAT(YEAR({c}), '-Q', QUARTER({c}))",
        "year": "YEAR({c})",
        "day": "DATE({c})",
        "week": "DATE_FORMAT({c}, '%Y-%u')",
    },
    "postgres": {
        "month": "TO_CHAR({c}, 'YYYY-MM')",
        "quarter": "TO_CHAR({c}, 'YYYY-\"Q\"Q')",
        "year": "EXTRACT(YEAR FROM {c})",
        "day": "DATE_TRUNC('day', {c})",
        "week": "TO_CHAR({c}, 'IYYY-IW')",
    },
    # SQLite（本地 ADS 验证环境；Python 内置，无需装包）
    "sqlite": {
        "month": "strftime('%Y-%m', {c})",
        "quarter": "strftime('%Y', {c}) || '-Q' || " \
                   "CAST((CAST(strftime('%m', {c}) AS INTEGER) + 2) / 3 AS TEXT)",
        "year": "CAST(strftime('%Y', {c}) AS INTEGER)",
        "day": "DATE({c})",
        "week": "strftime('%Y-%W', {c})",
    },
    # Oracle 11g（HIS 镜像库）：
    #   - 无 DATE_FORMAT / QUARTER，用 TO_CHAR
    #   - 无 LIMIT（12c 才有 FETCH FIRST），TOP-N 必须靠 ROWNUM 包一层
    "oracle": {
        "month": "TO_CHAR({c}, 'YYYY-MM')",
        "quarter": "TO_CHAR({c}, 'YYYY') || '-Q' || TO_CHAR({c}, 'Q')",
        "year": "EXTRACT(YEAR FROM {c})",
        "day": "TRUNC({c})",
        "week": "TO_CHAR({c}, 'IYYY-IW')",
        "limit": "rownum",     # 标记：由 build() 用 ROWNUM 包一层
    },
}


# ────────────────────────────────────────────────────────────
# 列限定（JOIN 模式专用）
# ────────────────────────────────────────────────────────────
_SQL_WORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "HAVING", "LIMIT", "AS",
    "AND", "OR", "NOT", "IN", "IS", "NULL", "LIKE", "BETWEEN", "CASE", "WHEN",
    "THEN", "ELSE", "END", "DISTINCT", "ASC", "DESC", "ON", "JOIN", "LEFT",
    "RIGHT", "INNER", "OUTER", "UNION", "ALL", "EXISTS", "CAST", "INTERVAL",
    "CURRENT_DATE", "TRUE", "FALSE", "TIMESTAMP", "DATE", "TIME", "OVER",
    "PARTITION", "WINDOW",
}


def qualify(expr: Any, alias: Optional[str]) -> Any:
    """给表达式里的裸列名加上表别名前缀。

    只加在「确实是列名」的标识符上：
      - 函数名（后面紧跟 `(`）不动               → SUM(...) / NULLIF(...)
      - SQL 关键字不动                            → CASE WHEN ... END
      - 字符串字面量整段跳过                      → LIKE 'F%'
      - 已限定的列（前面是 `.`）不动               → MEDREC.PAT_VISIT.VISIT_ID
      - 数字常量不动                              → ... / NULLIF(..., 0)

    ★ 为什么必须有这一层：measure.expr 是用户可配的 SQL 表达式
      （`(CASE WHEN CLASS_ON_MR LIKE 'F%' THEN COSTS ELSE 0 END)`），
      JOIN 之后这些裸列会变成 ambiguous；而"只在特定医院、特定口径才炸"的
      问题是最难排查的一类。
    """
    if alias is None or not expr:
        return expr
    s = str(expr)
    out: List[str] = []
    i, n = 0, len(s)
    prev = ""          # 上一个有意义的字符
    while i < n:
        ch = s[i]
        # 字符串字面量整段透传（含 '' 转义）
        if ch == "'":
            j = i + 1
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(s[i:j])
            prev, i = "'", j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            word = s[i:j]
            k = j
            while k < n and s[k].isspace():
                k += 1
            nxt = s[k] if k < n else ""
            if prev == "." or nxt == "." or nxt == "(" or word.upper() in _SQL_WORDS:
                out.append(word)          # 已限定 / 表名 / 函数名 / 关键字
            else:
                out.append(f"{alias}.{word}")
            prev, i = "w", j
            continue
        out.append(ch)
        if not ch.isspace():
            prev = ch
        i += 1
    return "".join(out)


# ────────────────────────────────────────────────────────────
# 时间解析：唯一可信的时间计算入口
# ────────────────────────────────────────────────────────────
def resolve_time_range(spec: Dict[str, Any], today: dt.date) -> Tuple[dt.date, dt.date]:
    """把 DSL 的时间表达解析为 [start, end) 半开区间（end 为开区间）。

    这是「时间解析」的唯一实现。模型只负责给出 type 与参数，绝不传日期字符串。

    ★★ 边界语义（唯一转换点，别处不要再加减一天）★
      DSL / 对外表达 = **闭区间**（人类直觉）：{"type":"absolute","start":"2023-01-01",
      "end":"2023-12-31"} 表示「2023 一整年，含 12-31」。
      SQL = **半开区间**：`>= start AND < end`（见本文件开头的严谨性约束 2）。
      转换只发生在本函数的 absolute 分支（end + 1 天）。

    为什么要统一成"对外闭区间"：此前 `norm_time_range`（标注侧）按闭区间 +1，
    而提示词没告诉模型这件事，模型按直觉给闭区间 → **整年少算最后一天**
    （2023 年出院人次 59,653 被算成 59,356），且不报错、答案看着正常。
    两处各转一次、还都不写明契约，就是这类"差一天"的温床；现在只留一处。

    归一化：last_<k>_months → last_n_months；last_two_weeks → week_before_last。
    （标注模板与文档中出现的是口语化 type 名，此处统一收敛，避免命名分裂。）
    """
    t = str(spec.get("type", "last_month"))

    # 周口径：统一按自然周（周一至周日）处理
    if t in ("week_before_last", "last_two_weeks", "last_2_weeks"):
        monday = today - dt.timedelta(days=today.weekday())
        return monday - dt.timedelta(days=14), monday - dt.timedelta(days=7)

    m = re.fullmatch(r"last_(\d+)_months", t)
    if m:
        return _resolve_months(int(m.group(1)), today)
    if t == "last_12_months":
        return _resolve_months(12, today)

    def _month_start(d: dt.date) -> dt.date:
        return d.replace(day=1)

    def _add_months(d: dt.date, n: int) -> dt.date:
        y = d.year + (d.month - 1 + n) // 12
        m = (d.month - 1 + n) % 12 + 1
        return dt.date(y, m, 1)

    if t == "absolute":
        s = dt.date.fromisoformat(str(spec["start"])[:10])
        # ★ 对外闭区间 → SQL 半开区间：唯一的一次 +1 天。
        #   不再"猜"模型写的是开区间还是闭区间 —— 契约写死为闭区间，如实采纳。
        e = dt.date.fromisoformat(str(spec["end"])[:10]) + dt.timedelta(days=1)
        return s, e

    if t == "last_month":
        return _add_months(_month_start(today), -1), _month_start(today)

    if t == "current_month":
        return _month_start(today), _add_months(_month_start(today), 1)

    if t == "last_n_months":
        n = int(spec.get("n", 6))
        start = _add_months(_month_start(today), -(n - 1))
        return start, _add_months(_month_start(today), 1)

    if t == "year_to_date":
        return dt.date(today.year, 1, 1), today + dt.timedelta(days=1)

    if t == "last_quarter":
        q = (today.month - 1) // 3 + 1
        start_m = 3 * ((q - 2) % 4) + 1
        year = today.year if q > 1 else today.year - 1
        return dt.date(year, start_m, 1), dt.date(year, start_m + 3, 1)

    if t == "first_three_quarters":
        return dt.date(today.year, 1, 1), dt.date(today.year, 10, 1)

    if t == "last_year":
        return dt.date(today.year - 1, 1, 1), dt.date(today.year, 1, 1)

    if t == "last_3_years":
        return dt.date(today.year - 3, 1, 1), dt.date(today.year, 1, 1)

    if t == "same_period_last_year":
        # 与 last_month 同窗口，整体前移一年
        s, e = resolve_time_range({"type": "last_month"}, today)
        return dt.date(s.year - 1, s.month, s.day), dt.date(e.year - 1, e.month, e.day)

    if t == "week_before_last":
        monday = today - dt.timedelta(days=today.weekday())
        return monday - dt.timedelta(days=14), monday - dt.timedelta(days=7)

    raise DSLError(f"不支持的时间范围类型: {t}")


def _resolve_months(n: int, today: dt.date) -> Tuple[dt.date, dt.date]:
    """最近 n 个自然月（含当月），返回 [start, end)。"""
    def month_start(d: dt.date) -> dt.date:
        return d.replace(day=1)

    def add_months(d: dt.date, k: int) -> dt.date:
        total = d.month - 1 + k
        return dt.date(d.year + total // 12, total % 12 + 1, 1)

    return add_months(month_start(today), -(n - 1)), add_months(month_start(today), 1)


def _shift_years(span: Tuple[dt.date, dt.date], n: int) -> Tuple[dt.date, dt.date]:
    s, e = span
    return dt.date(s.year + n, s.month, s.day), dt.date(e.year + n, e.month, e.day)


def _shift_months(span: Tuple[dt.date, dt.date], n: int) -> Tuple[dt.date, dt.date]:
    def add(d: dt.date, k: int) -> dt.date:
        y = d.year + (d.month - 1 + k) // 12
        m = (d.month - 1 + k) % 12 + 1
        return dt.date(y, m, min(d.day, [31, 29 if y % 4 == 0 else 28,
                                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]))
    return add(span[0], n), add(span[1], n)


# ────────────────────────────────────────────────────────────
# 生成器
# ────────────────────────────────────────────────────────────
class SQLBuilder:

    def __init__(self, registry: Dict[str, Any], dialect: str = "doris"):
        self.registry = registry
        self.dialect_name = dialect
        self.dialect = DIALECTS[dialect]
        self.metrics = {m["code"]: m for m in registry.get("metrics", [])}
        self.measures = {m["code"]: m for m in registry.get("measures", [])}
        self.dims = {d["code"]: d for d in registry.get("dimensions", [])}
        self.defaults = registry.get("defaults", {}) or {}
        self.permission = registry.get("permission", {}) or {}
        # 事实 → 维表关系（语义层声明，编译器只按声明拼，模型无权新增）
        self.relations = registry.get("relations", []) or []

    # ── 口径解析 ──────────────────────────────────────────
    def _measure_of(self, metric_code: str):
        m = self.metrics[metric_code]
        if m.get("type") == "ratio":
            num = m.get("numerator")
            code = num[0] if isinstance(num, list) else num
            return self.measures.get(code), m
        return self.measures.get(m.get("measure")), m

    def _date_col(self, metric_code: str, basis: str) -> str:
        ref, _ = self._measure_of(metric_code)
        emap = (ref.get("date_basis", {}) or {}).get("expr_map", {})
        if basis not in emap:
            raise DSLError(f"时间口径 '{basis}' 无对应字段（可用：{list(emap)}）")
        return emap[basis]

    def _dept_col(self, metric_code: str, basis: str) -> str:
        ref, _ = self._measure_of(metric_code)
        emap = (ref.get("dept_basis", {}) or {}).get("expr_map", {})
        if basis not in emap:
            raise DSLError(f"科室口径 '{basis}' 无对应字段（可用：{list(emap)}）")
        return emap[basis]

    def _resolve_basis(self, metric_code: str, dsl: QueryDSL) -> Tuple[str, str, str]:
        """返回 (date_basis, dept_basis, dedup)，未指定则用指标默认值。"""
        ref, metric = self._measure_of(metric_code)
        db = dsl.time.date_basis or (ref.get("date_basis", {}) or {}).get("default")
        ob = dsl.options.dept_basis or (ref.get("dept_basis", {}) or {}).get("default")
        dd = dsl.options.dedup or metric.get("dedup") or ref.get("dedup") or "visits"
        return db, ob, dd

    # ── JOIN 规划（事实 → 维表） ──────────────────────────
    def _find_relation(self, fact_table: str, dim_code: str,
                       dim_table: Optional[str]) -> Optional[Dict[str, Any]]:
        """按 (事实表, 维度) 找关系声明；没有 `dim` 字段时退回按目标表匹配。"""
        cands = [r for r in self.relations
                 if r.get("from") == fact_table and r.get("dim") == dim_code]
        if not cands and dim_table:
            cands = [r for r in self.relations
                     if r.get("from") == fact_table and r.get("to") == dim_table]
        if len(cands) > 1:
            names = ", ".join(str(r.get("name")) for r in cands)
            raise DSLError(f"事实表 {fact_table} 与维度 '{dim_code}' 之间存在多条关系"
                           f"（{names}），语义层必须用 dim 字段消歧")
        return cands[0] if cands else None

    @staticmethod
    def _relation_when(rel: Dict[str, Any]) -> Any:
        """取按口径分档的连接条件。字段名是 `join_on`，不是 `on`。

        ★ YAML 1.1 会把裸写的 `on:` 解析成布尔 True（键名变成 True 而不是 "on"），
          这种 config 读出来"看着有、代码读不到"，排查成本极高。为兼容手写的旧文件，
          这里同时接受 "on" 与布尔键。
        """
        if "join_on" in rel:
            return rel["join_on"]
        if "on" in rel:
            return rel["on"]
        return rel.get(True)

    @classmethod
    def _relation_on(cls, rel: Dict[str, Any], basis: Optional[str]) -> Tuple[str, str]:
        """取连接条件。按口径分档时，口径对不上必须报错，不许猜。"""
        on_map = cls._relation_when(rel)
        if on_map:
            key = basis if basis in on_map else None
            if key is None and "default" in on_map:
                key = "default"
            if key is None:
                raise DSLError(
                    f"关系 '{rel.get('name')}' 未声明「{basis}」口径的连接条件"
                    f"（已声明：{', '.join(map(str, on_map))}）——"
                    f"这会让科室口径与 JOIN 条件不一致，宁可拒绝出数")
            cond = on_map[key] or {}
        else:
            cond = rel.get("on_default") or {}
        left, right = cond.get("left"), cond.get("right")
        if not left or not right:
            raise DSLError(f"关系 '{rel.get('name')}' 的连接条件缺少 left/right")
        return left, right

    def _plan_joins(self, fact_table: str, dims: List[str],
                    basis: Optional[str]) -> Dict[str, Dict[str, Any]]:
        """把「声明了 resolve: join 的维度」翻译成 JOIN 计划。

        返回 {dim_code: {alias, table, left, right, col}}，按维度顺序分配 d1/d2…
        同一目标表 + 同一连接列只 JOIN 一次（复用别名），避免重复连接。
        """
        plan: Dict[str, Dict[str, Any]] = {}
        by_key: Dict[Tuple[str, str, str], str] = {}
        used_alias: set = set()
        for d in dims:
            dim = self.dims.get(d) or {}
            if (dim.get("resolve") or "fact") != "join":
                continue
            dim_table = dim.get("table")
            if not dim_table:
                raise DSLError(f"维度 '{d}' 声明为 join 解析，但未声明 table")
            if dim_table == fact_table:
                raise DSLError(f"维度 '{d}' 的 table 与事实表相同，不应声明 resolve: join")
            rel = self._find_relation(fact_table, d, dim_table)
            if not rel:
                raise DSLError(
                    f"维度 '{d}' 声明为 join 解析，但语义层没有声明 "
                    f"{fact_table} → {dim_table} 的关系（relations）")
            rtype = str(rel.get("type") or "many_to_one")
            if rtype not in ("many_to_one", "one_to_one"):
                raise DSLError(
                    f"关系 '{rel.get('name')}' 的基数为 {rtype}：只允许事实→维表的 "
                    f"many_to_one/one_to_one（一对多会把一行拉成多行，指标被放大）")
            left, right = self._relation_on(rel, basis)
            key = (dim_table, left, right)
            alias = by_key.get(key)
            if alias is None:
                alias = f"d{len(used_alias) + 1}"
                used_alias.add(alias)
                by_key[key] = alias
            plan[d] = {
                "alias": alias, "table": dim_table,
                "left": left, "right": right,
                # 输出用中文名（label_expr），过滤仍用码值列（enum_map 产出的是码）
                "label_col": dim.get("label_expr") or dim.get("expr") or right,
                "code_col": dim.get("expr") or right,
                "key": key,
            }
        return plan

    # ── 聚合表达式 ────────────────────────────────────────
    def _agg_expr(self, measure_code: str, dedup: str,
                  alias: Optional[str] = None) -> str:
        m = self.measures[measure_code]
        agg, expr = m.get("agg"), m.get("expr")
        if dedup == "patients" and agg in ("count", "count_distinct", "sum"):
            key = qualify(m.get("dedup_key") or "patient_id", alias)
            return f"COUNT(DISTINCT {key})"
        q = qualify(expr, alias)
        if agg == "count":
            return f"COUNT({q})"
        if agg == "count_distinct":
            return f"COUNT(DISTINCT {q})"
        if agg == "sum":
            return f"SUM({q})"
        if agg == "avg":
            # 仅当该 measure 语义上确为「对行求平均」时可用，指标层不允许用 AVG 算 ratio
            return f"AVG({q})"
        raise DSLError(f"不支持的聚合方式: {agg}")

    @staticmethod
    def _scale_of(formula) -> str:
        """从 formula 尾部解析常数系数（如 '* 100'）。

        为什么不能按 unit 猜：material_rate 的 unit 是「元/百元」，
        公式同样是 *100，按 `unit == '%'` 判断会静默把结果缩小 100 倍
        （实测出 0.1705 而正确值是 17.05）。系数以 formula 为准。
        """
        if not formula:
            return ""
        import re as _re
        mm = _re.search(r"([*/])\s*(\d+(?:\.\d+)?)\s*$", str(formula))
        return f" {mm.group(1)} {mm.group(2)}" if mm else ""

    def _metric_expr(self, metric_code: str, dedup: str,
                     alias: Optional[str] = None) -> str:
        m = self.metrics[metric_code]
        if m.get("type") == "ratio":
            num, den = m.get("numerator"), m.get("denominator")
            num_e = self._combine(num, dedup, alias)
            den_e = self._combine(den, dedup, alias)
            scale = self._scale_of(m.get("formula"))
            # ★ 先分别聚合，再相除
            return f"({num_e}) / NULLIF(({den_e}), 0){scale}"
        return self._agg_expr(m["measure"], dedup, alias)

    def _combine(self, code_or_list, dedup: str,
                 alias: Optional[str] = None) -> str:
        codes = code_or_list if isinstance(code_or_list, list) else [code_or_list]
        parts = [self._agg_expr(c, dedup, alias) for c in codes]
        return " + ".join(parts) if len(parts) > 1 else parts[0]

    # ── WHERE ─────────────────────────────────────────────
    def _where(self, metric_code: str, dsl: QueryDSL, spans: List[Tuple[dt.date, dt.date]],
               ctx, warnings: Optional[List[str]] = None,
               fact_alias: Optional[str] = None,
               joins: Optional[Dict[str, Dict[str, Any]]] = None) -> List[str]:
        ref, _ = self._measure_of(metric_code)
        table = ref["table"]
        db, ob, _ = self._resolve_basis(metric_code, dsl)
        date_col = self._date_col(metric_code, db)
        warnings = warnings if warnings is not None else []
        joins = joins or {}
        # JOIN 模式下所有裸列都要加别名；非 JOIN 模式保持历史写法（逐字不变）
        ft = fact_alias or table
        dref = f"{fact_alias}.{date_col}" if fact_alias else date_col

        # ★ 多个区间（当期 + 对比期）用 OR 并集，保证一条 SQL 取回两期
        if len(spans) == 1:
            s, e = spans[0]
            date_cond = (f"{dref} >= '{s.isoformat()}' "
                         f"AND {dref} < '{e.isoformat()}'")
        else:
            parts = [f"({dref} >= '{s.isoformat()}' AND {dref} < '{e.isoformat()}')"
                     for s, e in spans]
            date_cond = "(" + " OR ".join(parts) + ")"

        conds = [date_cond]

        # 数据成熟度
        if not dsl.options.include_provisional:
            conds.append(f"{ft}.data_maturity = 'settled'")

        # measure 固有过滤（如 is_valid_visit = 1、operation_level = '4'）
        for f in ref.get("filters", []) or []:
            v = f["value"]
            lit = f"'{v}'" if isinstance(v, str) else str(v)
            conds.append(f"{ft}.{f['field']} {f['op']} {lit}")

        # 院方口径规则（如出院人次是否计入新生儿）：由 hospital_profile.yaml 开关控制。
        # 「哪些算、哪些不算」是院方口径，系统不替他决定，只负责按配置执行。
        for w in (ref.get("_caliber_where") or []):
            conds.append(qualify(w, fact_alias) if fact_alias else w)

        # DSL 过滤
        dept_col = self._dept_col(metric_code, ob)
        for f in dsl.filters:
            if f.field in joins:
                # 该维度已 JOIN 到维表：过滤要走维表的码值列，不能再用事实表列
                jp = joins[f.field]
                col = f"{jp['alias']}.{jp['code_col']}"
            elif f.field == "dept":
                col = f"{fact_alias}.{dept_col}" if fact_alias else dept_col
            else:
                col = f"{fact_alias}.{f.field}" if fact_alias else f.field
            conds.append(self._filter_sql(col, f))

        # ★ RLS：按「患者归属科室」判定数据主权，与查询的 dept_basis 解耦
        # ★ 显式空范围 = 无授权 → 直接拒绝生成 SQL（v0.9 修正）。
        #   不得因为空列表是 falsy 就跳过注入 —— 那会让「无授权」变成「无围栏」。
        dept_scope = (ctx or {}).get("dept_scope")
        if dept_scope is not None and not dept_scope:
            raise DSLError("无数据范围授权，拒绝生成 SQL")
        if dept_scope and "*" not in dept_scope:
            col = self._rls_dept_col(metric_code, warnings, table)
            codes = ", ".join(f"'{c}'" for c in dept_scope)
            conds.append(f"{ft}.{col} IN ({codes})")
        campus_scope = (ctx or {}).get("campus_scope")
        if campus_scope and "*" not in campus_scope:
            col = self._rls_campus_col()
            codes = ", ".join(f"'{c}'" for c in campus_scope)
            conds.append(f"{ft}.{col} IN ({codes})")

        return conds

    # ── 行级权限用的列 ────────────────────────────────────
    def _rls_dept_col(self, metric_code: str, warnings: List[str], table: str) -> str:
        """数据主权按哪一列判定：语义层 `permission.rls.dept_basis`（默认 belong）。

        ★ 这里**必须走 `_dept_col()` 解析**（即取已物理映射的列名），不能写死字面量。
          写死 `belong_dept_code` 的老写法会产生一条引用"物理上不存在的列"的 SQL：
          HIS 上这一列叫 `DEPT_DISCHARGE_FROM`、ADS 宽表上叫 `discharge_dept_code`。
          而单元测试只断言字符串里出现过 `belong_dept_code`，所以它一直是"通过"的；
          真实库上的表现是：**任何科室级授权的用户一条数都问不出来**，
          全院范围的人则毫无感觉（RLS 对 `["*"]` 不注入）—— 最难被发现的一类故障。
        """
        rls = self.permission.get("rls") or {}
        basis = rls.get("dept_basis") or "belong"
        try:
            return self._dept_col(metric_code, basis)
        except DSLError:
            ref, _ = self._measure_of(metric_code)
            fallback = (ref.get("dept_basis", {}) or {}).get("default")
            if not fallback:
                raise DSLError(
                    f"指标 {metric_code} 未声明「{basis}」科室口径，且没有默认科室口径，"
                    f"无法注入行级权限围栏（宁可拒绝出数，也不能没有围栏）")
            col = self._dept_col(metric_code, fallback)
            warnings.append(
                f"该指标未声明「{basis}」科室口径，行级围栏按「{fallback}」口径"
                f"（{col}）注入，口径与标准判定不同，请知悉")
            return col

    def _rls_campus_col(self) -> str:
        """院区围栏用哪一列。**数据源没物化院区列时明确拒绝**，不拼坏 SQL。"""
        rls = self.permission.get("rls") or {}
        col = rls.get("campus_column") or "campus_code"
        absent = set(self.registry.get("_absent_columns") or [])
        if col in absent:
            raise DSLError(
                f"院区围栏未接线：当前数据源没有物化院区列「{col}」"
                f"（物理映射中已声明为不可用）。启用院区范围前需先给该数据源补这一列，"
                f"见 semantic-layer/physical_map_*.yaml")
        return col

    def _filter_sql(self, col: str, f) -> str:
        # ★ 最后一道防线：即使上层校验被绕过，也不允许把未知算子拼进 SQL
        if f.op not in ALLOWED_FILTER_OPS:
            raise DSLError(f"不支持的过滤算子 '{f.op}'（可用：{', '.join(ALLOWED_FILTER_OPS)}）")
        if f.op == "in":
            vals = ", ".join(f"'{v}'" for v in f.value)
            return f"{col} IN ({vals})"
        if f.op == "not_in":
            vals = ", ".join(f"'{v}'" for v in f.value)
            return f"{col} NOT IN ({vals})"
        if f.op == "between":
            return f"{col} >= '{f.value[0]}' AND {col} < '{f.value[1]}'"
        v = f.value
        lit = f"'{v}'" if isinstance(v, str) else str(v)
        return f"{col} {f.op} {lit}"

    # ── 主入口 ────────────────────────────────────────────
    def build(self, dsl: QueryDSL, ctx: Optional[Dict[str, Any]] = None,
              today: Optional[dt.date] = None) -> Dict[str, Any]:
        """返回 {sql, caliber, spans, warnings}。

        caliber 用于前端口径回显（指标名 + 时间口径 + 科室口径 + 去重 + 数据成熟度）。
        """
        ctx = ctx or {}
        today = today or dt.date.today()

        errs = validate_dsl(dsl, self.registry)
        if errs:
            raise DSLError("; ".join(errs))

        metric_code = dsl.metrics[0]
        metric = self.metrics[metric_code]
        ref, _ = self._measure_of(metric_code)
        table = ref["table"]
        db, ob, dd = self._resolve_basis(metric_code, dsl)

        # 权限门禁（restricted 指标）
        warnings: List[str] = []
        restricted = self.permission.get("restricted_metrics_roles", [])
        if metric.get("sensitivity") == "restricted" and restricted:
            if ctx.get("role") not in restricted:
                raise DSLError(
                    f"指标 '{metric['name']}' 为受限指标，当前角色无权访问")

        # 对比区间（yoy / mom）先算，因为会同时影响 WHERE 与 GROUP BY
        span = resolve_time_range(dsl.time.range, today)
        spans = [span]
        if dsl.time.compare == "yoy":
            spans.append(_shift_years(span, -1))
        elif dsl.time.compare == "mom":
            spans.append(_shift_months(span, -1))

        # 维度列
        date_col = self._date_col(metric_code, db)
        dept_col = self._dept_col(metric_code, ob)
        gran = dsl.time.granularity or "month"

        # ★ JOIN 只在维度显式声明 `resolve: join` 时才发生（默认不 JOIN，历史 SQL 逐字不变）。
        #   开启后事实表拿别名 f、维表拿 d1/d2…，所有列一律限定 —— 不留裸列。
        joins = self._plan_joins(table, list(dsl.dimensions or []), ob)
        fat = "f" if joins else None       # 事实表别名；None = 沿用历史写法
        ft = fat or table                  # 表名前缀
        dref = f"{fat}.{date_col}" if fat else date_col

        # ★ 有对比期时必须按时间粒度分组，否则两期会被合并成一个数
        need_time_group = bool(spans) and len(spans) > 1

        select_cols: List[str] = []
        group_cols: List[str] = []

        has_time_group = False
        for d in (dsl.dimensions or []):
            dim = self.dims.get(d)
            if not dim:
                continue
            if d in joins:
                # 维表已 JOIN：输出中文名，过滤仍走维表码值列（见 _where）
                jp = joins[d]
                if jp["label_col"] in (self.permission.get("forbidden_fields") or []):
                    raise DSLError(f"字段 '{jp['label_col']}' 禁止在问数场景出现")
                col_expr = f"{jp['alias']}.{jp['label_col']}"
                select_cols.append(f"{col_expr} AS {d}")
                group_cols.append(col_expr)
            elif dim.get("type") == "time":
                has_time_group = True
                expr = self.dialect[gran].format(c=dref)
                select_cols.append(f"{expr} AS stat_period")
                group_cols.append(expr)
            elif d == "dept":
                select_cols.append(f"{ft}.{dept_col} AS dept_code")
                group_cols.append(f"{ft}.{dept_col}")
            elif dim.get("case_expr"):
                # 科室分组：值由院方配置的 CASE 表达式算出。
                # 注意取的是「分组声明口径」的列（如 discharge），而不是当前查询口径的列
                # —— 否则按入院科查询时，分组会被错算成入院科口径。
                basis_key = dim.get("case_basis") or "discharge"
                gcol = ((ref.get("dept_basis") or {}).get("expr_map") or {}).get(basis_key)
                if not gcol:
                    raise DSLError(
                        f"科室分组维度 '{d}' 需要 '{basis_key}' 口径的科室列，"
                        f"但当前指标未提供")
                ge = dim["case_expr"].replace("{" + basis_key + "}",
                                              f"{ft}.{gcol}")
                select_cols.append(f"{ge} AS {d}")
                group_cols.append(ge)
            else:
                col = dim.get("expr") or d
                if col in (self.permission.get("forbidden_fields") or []):
                    raise DSLError(f"字段 '{col}' 禁止在问数场景出现")
                select_cols.append(f"{ft}.{col} AS {d}")
                group_cols.append(f"{ft}.{col}")

        if not dsl.dimensions or (need_time_group and not has_time_group):
            expr = self.dialect[gran].format(c=dref)
            select_cols.append(f"{expr} AS stat_period")
            group_cols.append(expr)

        # 度量列
        for mc in dsl.metrics:
            select_cols.append(f"{self._metric_expr(mc, dd, fat)} AS {mc}")

        # ★ 样本量必须按「病例数」计，不能用度量列
        #   反例：COUNT(occupy_bed_days) 会漏掉 NULL 行，使小样本抑制失效
        sample_key = self.defaults.get("sample_key") or "visit_id"
        dedup_key = self.defaults.get("dedup_key") or "patient_id"
        sample_expr = (f"COUNT(DISTINCT {qualify(dedup_key, fat)})" if dd == "patients"
                       else f"COUNT({qualify(sample_key, fat)})")
        select_cols.append(f"{sample_expr} AS _sample_size")

        where = self._where(metric_code, dsl, spans, ctx, warnings,
                            fact_alias=fat, joins=joins)

        sql = "SELECT\n  " + ",\n  ".join(select_cols)
        sql += f"\nFROM {table}" + (f" {fat}" if fat else "")
        if joins:
            seen: set = set()
            for jp in joins.values():
                if jp["alias"] in seen:
                    continue
                seen.add(jp["alias"])
                sql += (f"\nLEFT JOIN {jp['table']} {jp['alias']}"
                        f" ON {fat}.{jp['left']} = {jp['alias']}.{jp['right']}")
        sql += "\nWHERE " + "\n  AND ".join(where)
        if group_cols:
            sql += "\nGROUP BY " + ", ".join(group_cols)

        # 小样本抑制
        min_cell = max(dsl.options.min_cell_size,
                       int(self.defaults.get("min_cell_size", 5)))
        if dsl.dimensions:
            sql += f"\nHAVING {sample_expr} >= {min_cell}"

        # 排序 / TopN
        if dsl.options.order_by:
            ob_m = dsl.options.order_by.get("metric")
            direction = dsl.options.order_by.get("dir", "desc").upper()
            if direction not in ("ASC", "DESC"):
                direction = "DESC"
            sql += f"\nORDER BY {ob_m} {direction}"
        if dsl.options.top_n:
            n = int(dsl.options.top_n)
            if getattr(self, "dialect_name", "doris") == "oracle":
                # Oracle 11g 无 LIMIT，标准分页写法：内联视图 + ROWNUM
                sql = f"SELECT * FROM (\n{sql}\n) WHERE ROWNUM <= {n}"
            else:
                sql += f"\nLIMIT {n}"

        # ── Oracle 方言后处理：包装时间字面量、修正小写引用 ──
        if getattr(self, "dialect_name", "doris") == "oracle":
            import re as _re
            sql = _re.sub(r">=\s*'(\d{4}-\d{2}-\d{2})'",
                          r">= TO_DATE('\1','YYYY-MM-DD')", sql)
            sql = _re.sub(r"<\s*'(\d{4}-\d{2}-\d{2})'",
                          r"< TO_DATE('\1','YYYY-MM-DD')", sql)
            # 小写 visit_id / patient_id 引用统一改为大写，
            # 规避 Oracle 11g 在 ROWNUM/别名场景下偶发的 ORA-00911
            sql = (sql.replace("COUNT(visit_id) AS _sample_size",
                               "COUNT(VISIT_ID) AS sample_size_cnt")
                      .replace("COUNT(patient_id)", "COUNT(PATIENT_ID)")
                      .replace(" AS _sample_size", " AS sample_size_cnt"))

        # 对比区间（yoy / mom）
        spans_out = {"current": [spans[0][0].isoformat(), spans[0][1].isoformat()]}
        if len(spans) > 1:
            spans_out["baseline"] = [spans[1][0].isoformat(), spans[1][1].isoformat()]

        # 数据成熟度提示
        if span[1] > today:
            warnings.append("区间包含未结束周期，结果含未结算数据（provisional）")

        return {
            "sql": sql,
            "caliber": {
                "metric": metric["name"],
                "metric_code": metric_code,
                "unit": metric.get("unit", ""),
                "date_basis": db,
                "dept_basis": ob,
                "dedup": dd,
                "granularity": gran,
                "formula": metric.get("formula") or f"{ref.get('agg')}({ref.get('expr')})",
                "definition": metric.get("business_definition", ""),
                "notes": metric.get("notes", ""),
                "owner": metric.get("owner", ""),
                "version": metric.get("version", ""),
            },
            "spans": spans_out,
            "warnings": warnings,
            "table": table,
        }


# ────────────────────────────────────────────────────────────
# 自测
# ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    import os

    try:
        import yaml
    except ImportError:
        raise SystemExit("需要 PyYAML: pip install pyyaml")

    base = os.path.join(os.path.dirname(__file__), "..", "semantic-layer")
    with open(os.path.join(base, "metric_registry.yaml"), encoding="utf-8") as f:
        registry = yaml.safe_load(f)

    from dsl import dsl_from_dict

    builder = SQLBuilder(registry)
    TODAY = dt.date(2026, 10, 14)

    cases = [
        ("常规：上月出院人次",
         {"intent": "query_metric", "metrics": ["ip_discharge_cnt"], "dimensions": [],
          "time": {"granularity": "month", "range": {"type": "last_month"}}}),
        ("★ratio：各科室平均住院日 Top10",
         {"intent": "rank", "metrics": ["ip_los_avg"], "dimensions": ["dept"],
          "time": {"granularity": "month", "range": {"type": "last_month"}},
          "options": {"order_by": {"metric": "ip_los_avg", "dir": "desc"}, "top_n": 10}}),
        ("★口径切换：按结算日期 + 开单科室的门诊收入",
         {"intent": "query_metric", "metrics": ["op_revenue"], "dimensions": ["dept"],
          "time": {"granularity": "month", "range": {"type": "last_month"},
                   "date_basis": "settle", "compare": "yoy"},
          "options": {"dept_basis": "order"}}),
        ("★去重：门诊人数（patients）",
         {"intent": "query_metric", "metrics": ["op_patient_cnt"], "dimensions": [],
          "time": {"granularity": "month", "range": {"type": "year_to_date"}}}),
        ("★RLS：呼吸科主任查出院人次（应自动注入 belong_dept 过滤）",
         {"intent": "query_metric", "metrics": ["ip_discharge_cnt"], "dimensions": ["dept"],
          "time": {"granularity": "month", "range": {"type": "last_month"}}}),
    ]

    for name, data in cases:
        print("=" * 72)
        print(f"用例: {name}")
        dsl = dsl_from_dict(data)
        ctx = {"role": "科室主任", "dept_scope": ["RESP"]} if "RLS" in name else {"role": "院长"}
        res = builder.build(dsl, ctx=ctx, today=TODAY)
        print(f"口径: {json.dumps(res['caliber'], ensure_ascii=False)}")
        print(f"区间: {res['spans']}")
        if res["warnings"]:
            print(f"告警: {res['warnings']}")
        print(res["sql"])

    # 反向测试：非法口径必须被拦截
    print("=" * 72)
    print("反向测试: 编造时间口径 'operation' 用于出院人次（应报错）")
    try:
        builder.build(dsl_from_dict(
            {"intent": "query_metric", "metrics": ["ip_discharge_cnt"],
             "time": {"range": {"type": "last_month"}, "date_basis": "operation"}}),
            ctx={"role": "院长"}, today=TODAY)
        print("[FAIL] 未被拦截，存在口径编造风险")
    except DSLError as e:
        print(f"[OK] 已拦截: {e}")

    print("反向测试: 无权限角色访问受限指标（应报错）")
    try:
        builder.build(dsl_from_dict(
            {"intent": "query_metric", "metrics": ["ip_revenue"],
             "time": {"range": {"type": "last_month"}}}),
            ctx={"role": "医生"}, today=TODAY)
        print("[FAIL] 未被拦截")
    except DSLError as e:
        print(f"[OK] 已拦截: {e}")

    # ── RLS 列解析（v0.19）────────────────────────────────
    # ★ 这一组回归的存在理由：RLS 的列名曾写死为 `belong_dept_code`，而它在
    #   HIS 上叫 DEPT_DISCHARGE_FROM、在 ADS 宽表上叫 discharge_dept_code ——
    #   也就是说**科室级授权的人在任何一条路线上都查不出数**，而全院范围的人无感。
    #   当时的单测只断言"SQL 里出现了 belong_dept_code"，所以一路绿灯。
    print("=" * 72)
    print("RLS 列解析：注入的列必须来自语义层（物理映射后的真实列），不是写死的逻辑名")

    def _rls_sql(reg, ctx, metric="ip_discharge_cnt"):
        b = SQLBuilder(reg)
        return b.build(dsl_from_dict(
            {"intent": "query_metric", "metrics": [metric],
             "time": {"granularity": "month", "range": {"type": "last_month"}}}),
            ctx=ctx, today=TODAY)

    raw = json.loads(json.dumps(builder.registry)) if hasattr(builder, "registry") else None
    if raw is None:                       # 兼容构造方式差异：从文件重新读一份
        with open(str(REGISTRY), "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

    # ① 逻辑名（未映射）：注入归属性口径列
    res = _rls_sql(raw, {"role": "科室主任", "dept_scope": ["0101"]})
    ok = "belong_dept_code IN ('0101')" in res["sql"]
    print(f"[{'OK' if ok else 'FAIL'}] 未映射时用逻辑名 belong_dept_code")

    # ② 物理映射后（等价于 HIS/ADS 的 registry）：必须换成真实列名
    mapped = json.loads(json.dumps(raw))
    for m in mapped.get("measures") or []:
        dm = (m.get("dept_basis") or {}).get("expr_map") or {}
        for k, v in list(dm.items()):
            if v == "belong_dept_code":
                dm[k] = "DEPT_DISCHARGE_FROM"
    res2 = _rls_sql(mapped, {"role": "科室主任", "dept_scope": ["0101"]})
    ok2 = ("DEPT_DISCHARGE_FROM IN ('0101')" in res2["sql"]
           and "belong_dept_code" not in res2["sql"])
    print(f"[{'OK' if ok2 else 'FAIL'}] ★ 物理映射后注入的是真实列 DEPT_DISCHARGE_FROM"
          f"（写死逻辑名会让真实库报无效标识符）")

    # ③ 显式空范围 = 无授权 → 拒绝（不得因为 [] 是 falsy 就跳过围栏注入）
    try:
        _rls_sql(raw, {"role": "科室主任", "dept_scope": []})
        print("[FAIL] 空范围未被拦截")
    except DSLError as e:
        print(f"[OK] 空范围被拦截: {e}")

    # ④ 院区围栏：数据源没物化院区列时必须**明确拒绝**，而不是拼一条必然失败的 SQL
    absent = json.loads(json.dumps(raw))
    absent["_absent_columns"] = ["campus_code"]
    try:
        _rls_sql(absent, {"role": "院长", "dept_scope": ["*"], "campus_scope": ["01"]})
        print("[FAIL] 院区列不可用却仍生成了 SQL")
    except DSLError as e:
        print(f"[OK] 院区围栏未接线被拦截: {str(e)[:70]}")

    # ── JOIN 关系（v0.21）────────────────────────────────
    # ★ 这一组回归的存在理由：语义层写了 `table: dim_doctor` 却从不 JOIN，
    #   生成的是 `dwd_fact_operation.doctor_id` —— 一张没有这一列的表。
    #   加了 JOIN 之后又必须保证：别名限定、按科室口径选连接键、缺声明就报错。
    print("=" * 72)
    print("JOIN 关系：resolve: join 才连维表；连接键随口径变；缺声明直接报错")

    def _join_reg(mutator=None):
        reg = json.loads(json.dumps(raw))
        if mutator:
            mutator(reg)
        return reg

    def _mark_doctor_join(reg):
        for d in reg.get("dimensions") or []:
            if d.get("code") == "doctor":
                d["resolve"] = "join"

    def _build(reg, data, ctx=None):
        return SQLBuilder(reg).build(dsl_from_dict(data), ctx=ctx or {"role": "院长"},
                                     today=TODAY)["sql"]

    op_rank = {"intent": "rank", "metrics": ["surgery_cnt"], "dimensions": ["doctor"],
               "time": {"granularity": "month", "range": {"type": "last_month"}}}

    sql_j = _build(_join_reg(_mark_doctor_join), op_rank)
    ok_j1 = "LEFT JOIN dim_doctor d1 ON f.doctor_id = d1.doctor_id" in sql_j
    ok_j2 = "d1.doctor_name AS doctor" in sql_j
    ok_j3 = "COUNT(f.operation_id)" in sql_j
    ok_j4 = "GROUP BY d1.doctor_name" in sql_j
    print(f"[{'OK' if ok_j1 else 'FAIL'}] 生成 LEFT JOIN 且连接键带别名")
    print(f"[{'OK' if ok_j2 else 'FAIL'}] 维度输出中文名（label_expr），不是码值")
    print(f"[{'OK' if ok_j3 and ok_j4 else 'FAIL'}] ★ JOIN 后度量列与 GROUP BY 全部限定别名"
          f"（裸列在多表查询里会 ambiguous）")

    # 默认 fact 模式必须仍然不 JOIN（历史 SQL 逐字不变的前提）
    sql_fact = _build(raw, op_rank)
    ok_f = "JOIN" not in sql_fact and "dwd_fact_operation.doctor_id AS doctor" in sql_fact
    print(f"[{'OK' if ok_f else 'FAIL'}] 未声明 resolve: join 时不产生 JOIN（兼容既有 SQL）")

    # 连接键必须跟着科室口径走：住院 dept 关系按 admit/discharge/belong 分档
    ip_dept = {"intent": "query_metric", "metrics": ["ip_discharge_cnt"],
               "dimensions": ["dept"],
               "time": {"granularity": "month", "range": {"type": "last_month"},
                        "date_basis": "discharge"},
               "options": {"dept_basis": "admit"}}

    def _mark_dept_join(reg):
        for d in reg.get("dimensions") or []:
            if d.get("code") == "dept":
                d["resolve"] = "join"

    sql_basis = _build(_join_reg(_mark_dept_join), ip_dept)
    ok_b = ("ON f.admit_dept_code = d1.dept_code" in sql_basis
            and "d1.dept_name AS dept" in sql_basis)
    print(f"[{'OK' if ok_b else 'FAIL'}] ★ 换科室口径时连接键跟着换（admit → admit_dept_code）")

    # 反向①：维度声明 join 但语义层没有关系声明 → 报错，不许猜
    def _drop_relations(reg):
        reg["relations"] = []

    try:
        _build(_join_reg(lambda r: (_mark_doctor_join(r), _drop_relations(r))), op_rank)
        print("[FAIL] 缺关系声明却仍生成了 JOIN")
    except DSLError as e:
        print(f"[OK] 缺关系声明被拦截: {str(e)[:70]}")

    # 反向②：一对多关系 → 报错（会把一行拉成多行，指标被放大）
    def _bad_cardinality(reg):
        _mark_doctor_join(reg)
        for r in reg.get("relations") or []:
            if r.get("dim") == "doctor":
                r["type"] = "one_to_many"

    try:
        _build(_join_reg(_bad_cardinality), op_rank)
        print("[FAIL] 一对多关系未被拦截")
    except DSLError as e:
        print(f"[OK] 一对多关系被拦截: {str(e)[:70]}")

    # 反向③：当前科室口径没有声明连接条件 → 报错，绝不拿别的口径凑
    def _drop_belong(reg):
        _mark_dept_join(reg)
        for r in reg.get("relations") or []:
            if r.get("name") == "fact_inpatient__dept":
                (r.get("join_on") or {}).pop("belong", None)

    ip_belong = json.loads(json.dumps(ip_dept))
    ip_belong["options"]["dept_basis"] = "belong"
    try:
        _build(_join_reg(_drop_belong), ip_belong)
        print("[FAIL] 口径连接条件缺失却仍生成了 SQL")
    except DSLError as e:
        print(f"[OK] 口径连接条件缺失被拦截: {str(e)[:70]}")

    # 列限定器：只给真列名加前缀，函数/关键字/字符串/已限定列都不动
    q_cases = [
        ("is_valid_visit", "f.is_valid_visit"),
        ("(DISCHARGE_DATE_TIME - ADMISSION_DATE_TIME)",
         "(f.DISCHARGE_DATE_TIME - f.ADMISSION_DATE_TIME)"),
        ("(CASE WHEN CLASS_ON_MR LIKE 'F%' THEN COSTS ELSE 0 END)",
         "(CASE WHEN f.CLASS_ON_MR LIKE 'F%' THEN f.COSTS ELSE 0 END)"),
        ("COUNT(DISTINCT patient_id)", "COUNT(DISTINCT f.patient_id)"),
        ("MEDREC.PAT_VISIT.VISIT_ID", "MEDREC.PAT_VISIT.VISIT_ID"),
        ("1", "1"),
    ]
    q_bad = [(a, b, qualify(a, "f")) for a, b in q_cases if qualify(a, "f") != b]
    print(f"[{'OK' if not q_bad else 'FAIL'}] 列限定器：函数/关键字/字符串/已限定列不被误改")
    for a, b, got in q_bad:
        print(f"       期望 {b}，实得 {got}")

