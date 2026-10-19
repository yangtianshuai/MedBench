#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
HIS 测试库只读盘点脚本  (docs/08 P0-1 数据源盘点 / P0-3 字典现状 / P0-6 质量初评)

实测环境: Oracle 11g 11.2.0.4 / 账号 HIS 自有 0 张表，业务数据在其它 schema，
          经同义词与授权访问 -> 因此本脚本全部走 ALL_* 视图，跨 schema 盘点。

设计红线：
  1. 只执行 SELECT / WITH 开头的语句，其他一律拒绝（代码级守门，不依赖调用方自觉）
  2. 不建表、不写库、不改会话状态；凭据只从环境变量读取，绝不落盘
  3. COUNT(*) 默认不执行，需显式 --count（大表可能很慢）

环境依赖：
  Oracle 11g 服务端 + 本机 Instant Client 11.2，故驱动锁 oracledb==1.4.2（thick mode）。
  新版 oracledb 要求客户端 >= 19.1，会报 DPI-1050。

用法：
  $env:ASK_HIS_PASSWORD = "***"
  $env:ASK_HIS_THICK    = "1"      # 走本机 Instant Client
  python scripts/probe_his.py --stage meta                       # 元数据盘点（快）
  python scripts/probe_his.py --stage profile --tables BA1,BA2 --count
  python scripts/probe_his.py --stage dict                       # 字典/编码表专项
"""

import argparse
import os
import re
import sys
from datetime import datetime

import oracledb

# HIS 常见命名习惯（拼音首字母 + 英文），用于给表做粗分类
CATEGORY_KEYWORDS = [
    ("门诊", ["OUTP", "MZ", "MENZHEN", "CLINIC", "OP_", "_OP", "REG_", "VISIT", "GH_", "GUAHAO"]),
    ("住院", ["INP", "ZY", "ZHUYUAN", "IP_", "_IP", "ADMIT", "DISCH", "BED", "INPAT", "RY_"]),
    ("费用", ["FEE", "COST", "CHARGE", "PAY", "SETTLE", "INVOICE", "SF_", "FY_", "PRICE", "TARIFF"]),
    ("手术", ["OPER", "SURG", "SS_", "SHOUSHU", "ANESTH", "OPERATION"]),
    ("诊断", ["DIAG", "ICD", "ZD_", "ZHENDUAN", "DISEASE"]),
    ("检验检查", ["LIS", "LAB", "EXAM", "RIS", "PACS", "JY_", "JC_", "RESULT"]),
    ("药品", ["DRUG", "PHARM", "MED_", "YP_", "YAOPIN", "PRES", "RECIPE"]),
    ("医嘱", ["ORDER", "YZ_", "YIZHU", "ADVICE"]),
    ("科室/人员", ["DEPT", "KS_", "KESHI", "EMP_", "STAFF", "DOCTOR", "WARD", "BQ_"]),
    ("字典/编码", ["DICT", "_DIC", "CODE", "BASE", "GY_", "XT_", "CATA", "STD_"]),
    ("病案/质控", ["MR_", "MEDICAL", "BA_", "BINGAN", "QUALITY", "ZK_"]),
    ("医保/DRG", ["YIBAO", "YB_", "INSUR", "DRG", "DIP", "MEDICARE"]),
]

# 系统 schema，盘点时排除
SYS_OWNERS = [
    "SYS", "SYSTEM", "SYSAUX", "XDB", "OLAPSYS", "MDSYS", "CTXSYS", "WMSYS", "DBSNMP",
    "ORDSYS", "EXFSYS", "LBACSYS", "OUTLN", "AUDSYS", "GSMADMIN_INTERNAL", "APPQOSSYS",
    "ORDDATA", "ORACLE_OCM", "OJVMSYS", "FLOWS_FILES", "SQLTXPLAIN", "PERFSTAT",
    "PUBLIC", "DBSFWUSER", "GSMUSER", "GSMCATUSER", "SPATIAL_CSW_ADMIN_USR", "SI_INFORMTN_SCHEMA",
]

IN_CLAUSE = ",".join("'%s'" % o for o in SYS_OWNERS)


def check_readonly(sql: str) -> None:
    s = sql.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        raise RuntimeError("只读脚本拒绝执行非 SELECT 语句: %s" % sql[:80])


def connect():
    host = os.getenv("ASK_HIS_HOST", "10.1.33.46")
    port = int(os.getenv("ASK_HIS_PORT", "1521"))
    service = os.getenv("ASK_HIS_SERVICE", "oracle10")
    user = os.getenv("ASK_HIS_USER", "his")
    pwd = os.getenv("ASK_HIS_PASSWORD")
    if not pwd:
        sys.exit("缺少环境变量 ASK_HIS_PASSWORD（不要把密码写进脚本或命令行历史文件）")

    dsn = oracledb.makedsn(host, port, service_name=service)
    if os.getenv("ASK_HIS_THICK") == "1":
        oracledb.init_oracle_client()
    return oracledb.connect(user=user, password=pwd, dsn=dsn), user, "%s:%s/%s" % (host, port, service)


def q(cur, sql, params=None):
    """所有查询的唯一入口，强制只读守门。返回 (列名, 行列表)。"""
    check_readonly(sql)
    cur.execute(sql, params or {})
    cols = [d[0] for d in cur.description] if cur.description else []
    return cols, cur.fetchall()


def classify(name: str) -> str:
    n = name.upper()
    for cat, kws in CATEGORY_KEYWORDS:
        for kw in kws:
            if kw in n:
                return cat
    return "未分类"


def fmt_num(v):
    if v is None:
        return "-"
    try:
        return "{:,}".format(int(v))
    except (TypeError, ValueError):
        return str(v)


def split_table(spec: str, default_owner: str):
    """支持 OWNER.TABLE 与 TABLE 两种写法。"""
    spec = spec.strip()
    if "." in spec:
        o, t = spec.split(".", 1)
        return o.strip().upper(), t.strip().upper()
    return (default_owner or "").upper(), spec.upper()


def stage_meta(cur, top, max_tables):
    print("=" * 78)
    print("HIS 测试库 · 只读元数据盘点（跨 schema）")
    print("生成时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78)

    cols, rows = q(cur, "SELECT banner FROM v$version WHERE ROWNUM <= 2")
    print("\n[1] 数据库版本")
    for r in rows:
        print("    " + str(r[0]).strip())

    print("\n[2] 当前身份与权限")
    cols, rows = q(cur, "SELECT USER FROM dual")
    print("    当前用户: %s" % rows[0][0])
    try:
        cols, rows = q(cur, "SELECT granted_role FROM user_role_privs ORDER BY 1")
        print("    角色: %s" % (", ".join(r[0] for r in rows) or "(无)"))
    except Exception as e:
        print("    角色: 读取失败 (%s)" % e)
    try:
        cols, rows = q(cur, "SELECT privilege FROM user_sys_privs ORDER BY 1")
        privs = [r[0] for r in rows]
        print("    系统权限(%d): %s" % (len(privs), ", ".join(privs)))
        danger = [p for p in privs if "DROP" in p or "DELETE" in p or "UPDATE" in p or "INSERT" in p]
        if danger:
            print("    ⚠️  含写类权限: %s —— MVP 阶段应改用只读账号" % ", ".join(danger))
    except Exception as e:
        print("    系统权限: 读取失败 (%s)" % e)

    # 同义词：HIS 应用访问业务表的主路径，指向哪个 schema 最有价值
    try:
        cols, rows = q(cur, """
            SELECT table_owner, COUNT(*) FROM user_synonyms GROUP BY table_owner ORDER BY 2 DESC
        """)
        print("\n[3] 同义词指向的 schema（HIS 应用的实际访问路径）")
        for owner, n in rows:
            print("    %-16s %6d 个同义词" % (owner, n))
        main_owner = rows[0][0] if rows else None
        if main_owner:
            print("    主业务 schema 判定: %s" % main_owner)
    except Exception as e:
        print("\n[3] 同义词: 读取失败 (%s)" % e)
        main_owner = None

    # 按 OWNER 汇总表规模
    cols, rows = q(cur, """
        SELECT owner, COUNT(*) n, SUM(num_rows) tot
        FROM all_tables
        WHERE owner NOT IN (%s)
        GROUP BY owner
        ORDER BY tot DESC NULLS LAST
    """ % IN_CLAUSE)
    print("\n[4] 业务 schema 规模（按估算总行数排序）")
    print("    %-16s %8s %18s" % ("OWNER", "表数", "估算总行数"))
    for owner, n, tot in rows[:15]:
        print("    %-16s %8d %18s" % (owner, n, fmt_num(tot)))
    if not main_owner and rows:
        main_owner = rows[0][0]

    # 表清单（取估算行数最大的 N 张，11g 不支持 FETCH FIRST，用 ROWNUM）
    cols, rows = q(cur, """
        SELECT owner, table_name, num_rows, last_analyzed, comments FROM (
            SELECT t.owner, t.table_name, t.num_rows, t.last_analyzed, c.comments
            FROM all_tables t
            LEFT JOIN all_tab_comments c
                   ON c.owner = t.owner AND c.table_name = t.table_name
            WHERE t.owner NOT IN (%s) AND t.num_rows IS NOT NULL
            ORDER BY t.num_rows DESC
        ) WHERE ROWNUM <= :1
    """ % IN_CLAUSE, {"1": max_tables})

    tables = []
    for owner, name, num_rows, analyzed, comment in rows:
        tables.append({
            "owner": owner,
            "name": name,
            "full": "%s.%s" % (owner, name),
            "rows": num_rows,
            "analyzed": analyzed,
            "comment": (comment or "").strip(),
            "cat": classify(name),
        })

    print("\n[5] 表分类（按命名习惯粗分，仅供人工复核）")
    by_cat = {}
    for t in tables:
        by_cat.setdefault(t["cat"], []).append(t)
    print("    %-12s %8s   %s" % ("类别", "表数", "示例"))
    for cat, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        print("    %-12s %8d   %s" % (cat, len(items), ", ".join(i["name"] for i in items[:4])))

    print("\n[6] 估算行数 Top %d" % top)
    print("    %-28s %14s %-11s %s" % ("表", "估算行数", "最后分析", "注释"))
    for t in tables[:top]:
        an = t["analyzed"].strftime("%Y-%m-%d") if t["analyzed"] else "-"
        print("    %-28s %14s %-11s %s" % (t["full"], fmt_num(t["rows"]), an, t["comment"][:36]))

    with_cn = [t for t in tables if re.search(r"[\u4e00-\u9fa5]", t["comment"])]
    print("\n[7] 带中文注释的表: %d / %d" % (len(with_cn), len(tables)))
    for t in with_cn[:top]:
        print("    %-28s %s" % (t["full"], t["comment"][:56]))

    # 按中文注释猜业务域 —— 比命名习惯可靠
    print("\n[8] 疑似核心业务表（按中文注释归类，供 profile 阶段使用）")
    guess = []
    for t in tables:
        cn = t["comment"]
        hit = None
        if re.search(r"病人|患者|就诊|挂号|登记|入院|出院|病案", cn):
            hit = "就诊/出入院主记录"
        elif re.search(r"费用|收费|结算|发票|明细|支付|医保", cn):
            hit = "费用/结算"
        elif re.search(r"科室|病区|病床|床位|部门", cn):
            hit = "科室/床位"
        elif re.search(r"诊断|手术|医嘱|药品|处方|检验|检查|治疗", cn):
            hit = "临床业务"
        elif re.search(r"字典|编码|目录|对照|分类|参数", cn):
            hit = "字典/编码"
        if hit:
            guess.append((hit, t))
    by_hit = {}
    for hit, t in guess:
        by_hit.setdefault(hit, []).append(t)
    for hit, items in sorted(by_hit.items(), key=lambda kv: -len(kv[1])):
        items.sort(key=lambda x: -(x["rows"] or 0))
        print("    【%s】%d 张" % (hit, len(items)))
        for t in items[:10]:
            print("        %-28s %14s  %s" % (t["full"], fmt_num(t["rows"]), t["comment"][:40]))

    print("\n下一步:")
    print("  python scripts/probe_his.py --stage dict                       # 字典/编码表专项")
    print("  python scripts/probe_his.py --stage profile --tables %s --count # 表级画像"
          % (tables[0]["full"] if tables else "OWNER.TABLE"))
    return main_owner


def stage_dict(cur, top):
    """字典/编码表专项：P0-3 主数据现状调研的输入。"""
    print("=" * 78)
    print("字典 / 编码表专项盘点")
    print("=" * 78)

    cols, rows = q(cur, """
        SELECT owner, table_name, num_rows, comments FROM (
            SELECT t.owner, t.table_name, t.num_rows, c.comments
            FROM all_tables t
            LEFT JOIN all_tab_comments c ON c.owner = t.owner AND c.table_name = t.table_name
            WHERE t.owner NOT IN (%s)
              AND (UPPER(t.table_name) LIKE '%%DICT%%' OR UPPER(t.table_name) LIKE '%%DIC%%'
                   OR UPPER(t.table_name) LIKE '%%CODE%%' OR UPPER(t.table_name) LIKE '%%CATA%%'
                   OR UPPER(t.table_name) LIKE '%%BASE%%' OR UPPER(t.table_name) LIKE '%%GY_%%'
                   OR c.comments LIKE '%%字典%%' OR c.comments LIKE '%%编码%%'
                   OR c.comments LIKE '%%目录%%' OR c.comments LIKE '%%对照%%')
            ORDER BY t.num_rows DESC
        ) WHERE ROWNUM <= :1
    """ % IN_CLAUSE, {"1": top})

    print("\n命中 %d 张（按估算行数排序）" % len(rows))
    print("    %-30s %12s  %s" % ("表", "估算行数", "注释"))
    for owner, name, n, comment in rows:
        print("    %-30s %12s  %s" % ("%s.%s" % (owner, name), fmt_num(n), (comment or "")[:44]))
    return rows


def stage_find(cur, pattern, top):
    """按表名/注释关键字找表（表名未知时先用它定位）。"""
    like = "%%%s%%" % pattern.upper()
    cols, rows = q(cur, """
        SELECT owner, table_name, num_rows, comments FROM (
            SELECT t.owner, t.table_name, t.num_rows, c.comments
            FROM all_tables t
            LEFT JOIN all_tab_comments c ON c.owner = t.owner AND c.table_name = t.table_name
            WHERE t.owner NOT IN (%s)
              AND (UPPER(t.table_name) LIKE :1 OR UPPER(c.comments) LIKE :2)
            ORDER BY t.num_rows DESC NULLS LAST
        ) WHERE ROWNUM <= :3
    """ % IN_CLAUSE, {"1": like, "2": like, "3": top})

    print("=" * 78)
    print("按关键字 '%s' 定位表（num_rows 为估算，实测请用 profile --count）" % pattern)
    print("=" * 78)
    print("    %-32s %13s  %s" % ("表", "估算行数", "注释"))
    for owner, name, n, comment in rows:
        print("    %-32s %13s  %s" % ("%s.%s" % (owner, name), fmt_num(n), (comment or "")[:40]))
    print("\n命中 %d 张" % len(rows))


def stage_profile(cur, specs, do_count, sample_rows, default_owner, brief=False):
    print("=" * 78)
    print("表级画像（列 / 行数 / 时间跨度 / 空值率）")
    print("=" * 78)

    for spec in specs:
        owner, name = split_table(spec, default_owner)
        print("\n" + "-" * 78)
        print("表: %s.%s" % (owner, name))

        cols, rows = q(cur, """
            SELECT c.column_id, c.column_name, c.data_type, c.data_length,
                   c.nullable, cc.comments
            FROM all_tab_columns c
            LEFT JOIN all_col_comments cc
                   ON cc.owner = c.owner AND cc.table_name = c.table_name
                      AND cc.column_name = c.column_name
            WHERE c.owner = :1 AND c.table_name = :2
            ORDER BY c.column_id
        """, {"1": owner, "2": name})
        if not rows:
            print("    ❌ 表不存在或当前用户无访问权限")
            continue

        print("    列数: %d" % len(rows))
        if brief:
            key = [r[1] for r in rows if re.search(
                r"DATE|TIME|DEPT|COST|FEE|AMOUNT|TOTAL|MONEY|PATIENT|VISIT|ADMIT|DISCH|"
                r"PATIENT_ID|_ID$|INP_NO|CODE", r[1])]
            print("    关键列候选(%d): %s" % (len(key), ", ".join(key[:24])))
            date_cols = [r[1] for r in rows if r[2].startswith("DATE") or r[2].startswith("TIMESTAMP")]
            print("    日期列(%d): %s" % (len(date_cols), ", ".join(date_cols[:12])))
            if not do_count:
                continue
            _, rows = q(cur, "SELECT COUNT(*) FROM %s.%s" % (owner, name))
            print("    实际行数: %s" % fmt_num(rows[0][0]))
            total = rows[0][0]
            for dc in date_cols[:6]:
                try:
                    _, rows = q(cur, """
                        SELECT MIN(%s), MAX(%s), SUM(CASE WHEN %s IS NULL THEN 1 ELSE 0 END)
                        FROM %s.%s
                    """ % (dc, dc, dc, owner, name))
                    mn, mx, nulls = rows[0]
                    print("      %-22s 跨度 %s ~ %s   空值 %s (%.1f%%)" % (
                        dc,
                        mn.strftime("%Y-%m-%d") if mn else "-",
                        mx.strftime("%Y-%m-%d") if mx else "-",
                        fmt_num(nulls),
                        (nulls / total * 100) if total else 0.0,
                    ))
                except Exception as e:
                    print("      %-22s 探查失败: %s" % (dc, str(e)[:56]))
            continue

        print("    %-26s %-16s %-9s %s" % ("列名", "类型", "可空", "注释"))
        for cid, cname, dtype, dlen, nullable, comment in rows[:45]:
            print("    %-26s %-16s %-9s %s" % (
                cname,
                "%s(%s)" % (dtype, dlen) if dtype in ("VARCHAR2", "CHAR", "NVARCHAR2") else dtype,
                "NULL" if nullable == "Y" else "NOT NULL",
                (comment or "")[:38],
            ))
        if len(rows) > 45:
            print("    ... 其余 %d 列省略" % (len(rows) - 45))

        date_cols = [r[1] for r in rows if r[2].startswith("DATE") or r[2].startswith("TIMESTAMP")]
        if date_cols:
            print("    日期列(%d): %s" % (len(date_cols), ", ".join(date_cols[:12])))

        if not do_count:
            continue

        _, rows = q(cur, "SELECT COUNT(*) FROM %s.%s" % (owner, name))
        total = rows[0][0]
        print("    实际行数: %s" % fmt_num(total))

        for dc in date_cols[:6]:
            try:
                _, rows = q(cur, """
                    SELECT MIN(%s), MAX(%s), SUM(CASE WHEN %s IS NULL THEN 1 ELSE 0 END)
                    FROM %s.%s
                """ % (dc, dc, dc, owner, name))
                mn, mx, nulls = rows[0]
                print("      %-22s 跨度 %s ~ %s   空值 %s (%.1f%%)" % (
                    dc,
                    mn.strftime("%Y-%m-%d") if mn else "-",
                    mx.strftime("%Y-%m-%d") if mx else "-",
                    fmt_num(nulls),
                    (nulls / total * 100) if total else 0.0,
                ))
            except Exception as e:
                print("      %-22s 探查失败: %s" % (dc, str(e)[:56]))

        if sample_rows:
            try:
                _, rows = q(cur, "SELECT * FROM %s.%s WHERE ROWNUM <= %d" % (owner, name, sample_rows))
                print("    采样 %d 行:" % len(rows))
                for r in rows:
                    print("      " + " | ".join(str(v)[:16] if v is not None else "NULL" for v in r[:12]))
            except Exception as e:
                print("    采样失败: %s" % str(e)[:56])


def main():
    ap = argparse.ArgumentParser(description="HIS 测试库只读盘点")
    ap.add_argument("--stage", choices=["meta", "dict", "profile", "find"], default="meta")
    ap.add_argument("--pattern", default="", help="find 阶段：表名/注释关键字")
    ap.add_argument("--tables", default="", help="profile 阶段：逗号分隔，支持 OWNER.TABLE")
    ap.add_argument("--owner", default=os.getenv("ASK_HIS_SCHEMA", ""), help="默认 schema")
    ap.add_argument("--count", action="store_true", help="profile 阶段：执行 COUNT(*)/跨度（慢）")
    ap.add_argument("--sample", type=int, default=0, help="采样行数（0=不采样）")
    ap.add_argument("--brief", action="store_true", help="profile 精简模式：只看关键列/行数/时间跨度")
    ap.add_argument("--top", type=int, default=30, help="展示条数")
    ap.add_argument("--max-tables", type=int, default=400, help="meta 阶段纳入分类的最大表数")
    args = ap.parse_args()

    try:
        cnx, user, dsn = connect()
    except Exception as e:
        print("连接失败: %s" % e)
        print("\n排错提示:")
        print("  DPY-3010 -> 服务端 < 12.1，thin mode 不支持，需 $env:ASK_HIS_THICK=1")
        print("  DPI-1050 -> 本机 Instant Client 版本过低，oracledb 新版要求 >= 19.1；")
        print("              本库为 11g，请用 pip install oracledb==1.4.2 + Instant Client 11.2")
        sys.exit(1)

    print("已连接: %s@%s (只读盘点，不执行任何写操作)" % (user, dsn))
    cur = cnx.cursor()
    try:
        if args.stage == "meta":
            stage_meta(cur, args.top, args.max_tables)
        elif args.stage == "dict":
            stage_dict(cur, args.top)
        elif args.stage == "find":
            if not args.pattern:
                sys.exit("find 阶段需要 --pattern 参数")
            stage_find(cur, args.pattern, args.top)
        else:
            names = [t.strip() for t in args.tables.split(",") if t.strip()]
            if not names:
                sys.exit("profile 阶段需要 --tables 参数")
            stage_profile(cur, names, args.count, args.sample, args.owner, args.brief)
    finally:
        cur.close()
        cnx.close()
        print("\n连接已关闭。")


if __name__ == "__main__":
    main()
