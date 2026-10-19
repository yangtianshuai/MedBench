# -*- coding: utf-8 -*-
"""可信与口径透明（F2）。

产出四件事：
  1. 口径条 caliber_bar —— 用户一眼看到"这个数字是按什么口径算的"
  2. 数据成熟度 maturity —— settled / provisional / restated / source_stale
  3. 数据截止时间 data_as_of —— 全量同步镜像必须明示，否则用户以为看到的是实时
  4. 结果指纹 result_hash —— 供快照复现与审计比对

原则：系统只**标记**不可信，绝不**修正**数字（10 IN-02 / IN-03）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from app.config import settings
from app.core import meta_db

# 财务/结算类指标的域，结算截止日前一律 provisional
SETTLE_SENSITIVE_DOMAINS = {"财务", "费用", "revenue", "finance"}


def build_trust(res, metric_code: Optional[str] = None) -> Dict[str, Any]:
    """从管线结果构建可信信息块。"""
    caliber = res.caliber or {}
    dsl = res.raw_dsl or {}
    rows = res.rows or []

    source_state = _source_state_for(metric_code, caliber)
    maturity, maturity_reasons = _judge_maturity(dsl, caliber, source_state)

    return {
        "caliber_bar": _caliber_bar(caliber, dsl),
        "maturity": maturity,
        "maturity_reasons": maturity_reasons,
        "maturity_text": _maturity_text(maturity),
        "data_as_of": (source_state or {}).get("last_data_date"),
        "source_domain": caliber.get("domain") or _guess_domain(metric_code),
        "freshness": (source_state or {}).get("freshness", "unknown"),
        "row_count": len(rows),
        "result_hash": _hash(rows),
        "caliber_raw": caliber,
    }


# 相对时间范围 → 人话（只做展示，不改判定；解析的唯一实现仍是 sql_builder.resolve_time_range）
_TIME_RANGE_LABEL = {
    "last_month": "上月", "current_month": "本月",
    "last_12_months": "最近 12 个月", "year_to_date": "本年至今",
    "last_quarter": "上季度", "first_three_quarters": "前三季度",
    "last_year": "上一年", "last_3_years": "近 3 年",
    "same_period_last_year": "去年同期",
    "week_before_last": "上一周", "current_week": "本周",
}


def _fmt_range(rng: Any) -> str:
    """时间范围 → 人话。

    ★ 这里**不再减一天**（v0.17 修正）：DSL 里的绝对区间 end 是**含当天**的闭区间
      （契约见 engine/sql_builder.resolve_time_range），直接展示即为用户预期。
      此前 DSL 存开区间、这里减一天，而模型给的是闭区间 → 界面显示成
      「2023-01-01 ~ 2023-12-30」（问的是 2023 年，看着就像漏了一天）。
    """
    if not isinstance(rng, dict):
        return "" if rng in (None, "") else str(rng)
    t = str(rng.get("type") or "")
    if t == "absolute":
        s = str(rng.get("start") or "")[:10]
        e = str(rng.get("end") or "")[:10]
        if s and e:
            return f"{s} ~ {e}"
        return s or e or "未指定"
    m = re.fullmatch(r"last_(\d+)_months", t)
    if m:
        return f"最近 {m.group(1)} 个月"
    if t == "last_n_months":
        return f"最近 {rng.get('n', 6)} 个月"
    return _TIME_RANGE_LABEL.get(t) or (t or "未指定")


def _caliber_bar(caliber: Dict[str, Any], dsl: Dict[str, Any]) -> List[Dict[str, str]]:
    """口径条：每项都要能追溯到语义层，UI 不得凭空造口径。"""
    bar: List[Dict[str, str]] = []

    def add(k, v, src="语义层"):
        if v not in (None, "", []):
            bar.append({"key": k, "value": str(v), "source": src})

    tr = dsl.get("time") or {}
    if isinstance(tr, dict):
        add("时间范围", _fmt_range(tr.get("range")) or tr.get("start"), "提问")
        add("时间口径", tr.get("date_basis") or tr.get("basis"))
    add("指标口径", caliber.get("metric_name") or caliber.get("metric"))
    add("时间基准", caliber.get("date_basis"))
    add("科室归属口径", caliber.get("dept_basis"))
    add("统计口径", caliber.get("dedup"))
    add("指标版本", caliber.get("metric_version"))
    add("包含范围", caliber.get("scope"))
    return bar


def _judge_maturity(dsl: Dict[str, Any], caliber: Dict[str, Any],
                    source_state: Optional[Dict[str, Any]]) -> (str, List[str]):
    reasons: List[str] = []
    today = date.today()

    # ① 最严重：源域已停更
    if source_state and source_state.get("freshness") == "stale":
        reasons.append(
            f"源数据域「{source_state.get('domain')}」最新数据为 "
            f"{source_state.get('last_data_date')}，已停止更新")

    # ② 财务类未结算
    domain = str(caliber.get("domain") or "")
    if domain in SETTLE_SENSITIVE_DOMAINS and today.day <= settings.settle_cutoff_day:
        reasons.append(f"财务类指标在每月 {settings.settle_cutoff_day} 日前尚未结算完成")

    # ③ 时间范围包含未完成周期
    tr = dsl.get("time") or {}
    rng = str(tr.get("range") or "")
    if rng in ("current_month", "current_quarter", "current_year", "year_to_date", "本月", "本季"):
        reasons.append(f"统计周期「{rng}」尚未结束")

    # ④ 源域为 T+1 且数据日期早于昨天
    if source_state and source_state.get("freshness") == "t_plus_1":
        reasons.append("数据源为 T+1 同步，不含当日数据")

    if not reasons:
        return "settled", []
    if source_state and source_state.get("freshness") == "stale":
        return "source_stale", reasons
    return "provisional", reasons


def _maturity_text(m: str) -> str:
    return {
        "settled": "数据已结算落定",
        "provisional": "未结算，数值可能变化",
        "restated": "本数据已修正，可查看原值",
        "source_stale": "源数据已停止更新，不可作为现状使用",
    }.get(m, "")


def _source_state_for(metric_code: Optional[str], caliber: Dict[str, Any]):
    domain = caliber.get("domain") or _guess_domain(metric_code)
    if not domain:
        return None
    return meta_db.get_source_state(str(domain))


def _guess_domain(metric_code: Optional[str]) -> Optional[str]:
    """按指标前缀粗判数据域（MVP；正式应由语义层显式声明 domain）。

    ip_ → 住院，op_ → 门诊，drug/material → 药品耗材，drg → DRG
    """
    if not metric_code:
        return None
    c = str(metric_code)
    if c.startswith("ip_"):
        return "住院"
    if c.startswith("op_"):
        return "门诊"
    if c.startswith("drg") or c.startswith("dip"):
        return "DRG"
    if "drug" in c or "material" in c:
        return "药品耗材"
    if "surgery" in c:
        return "手术"
    return None


def _hash(rows: List[Dict[str, Any]]) -> Optional[str]:
    if not rows:
        return None
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
