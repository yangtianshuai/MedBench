"""
敏感字段脱敏

场景：把数据摘录、数据集导出给别人（或存档）之前，去掉身份信息。

策略：
  keep      不处理
  drop      删除整列
  mask      掩码（保留首尾字符，如「张三丰」→「张*丰」）
  hash      假名（稳定的哈希代号，如 P3F2A91C40，同一人始终对应同一代号）
  age_band  年龄段（<18 / 18–40 / 41–65 / 66–80 / >80）
  year_only 仅保留年份（日期 → 年份）

敏感字段识别分两步：字段名命中身份信息词表，或字段内容形如身份证号/手机号/邮箱。
"""
import hashlib
import re
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

# 与「发送给 AI 的摘要」共用同一套敏感词表，避免两处口径不一致
from modules.ai_context import SENSITIVE_HINTS, is_sensitive_name

CONTENT_PATTERNS = (
    r'^\d{17}[\dXx]$',                      # 18 位身份证
    r'^\d{15}$',                            # 15 位身份证
    r'^1[3-9]\d{9}$',                       # 手机号
    r'^[\w.\-]+@[\w\-]+\.[\w.]+$',          # 邮箱
    r'^\d{15,19}$',                         # 长卡号
)


class Desensitizer:
    """按策略批量改写敏感字段。"""

    STRATEGIES = {
        'keep': '不处理',
        'drop': '删除整列',
        'mask': '掩码（保留首尾）',
        'hash': '假名（稳定哈希）',
        'age_band': '年龄段',
        'year_only': '仅保留年份',
    }

    STRATEGY_HELP = {
        'keep': '原样保留（确认不含身份信息时使用）',
        'drop': '整列从导出结果中移除，最彻底',
        'mask': '「张三丰」→「张*丰」，保留可读性但无法反查',
        'hash': '生成稳定假名「P3F2A91C40」，同一人始终同一代号，可做个体追踪',
        'age_band': '把精确年龄转为年龄段，避免准标识信息',
        'year_only': '把出生/入院日期保留到年份',
    }

    # ---------- 识别 ----------
    @staticmethod
    def _looks_sensitive_content(series: pd.Series, sample_size: int = 60) -> bool:
        if pd.api.types.is_numeric_dtype(series):
            return False
        sample = series.dropna().astype(str).head(sample_size)
        if sample.empty:
            return False
        hits = 0
        for value in sample:
            text = value.strip()
            if any(re.match(pattern, text) for pattern in CONTENT_PATTERNS):
                hits += 1
        return hits >= max(3, int(0.5 * len(sample)))

    @classmethod
    def detect_sensitive_columns(cls, df: pd.DataFrame) -> List[str]:
        """按字段名与内容特征识别敏感列。"""
        found: List[str] = []
        for name in df.columns:
            if is_sensitive_name(name) or cls._looks_sensitive_content(df[name]):
                found.append(name)
        return found

    @classmethod
    def suggest_strategy(cls, name, series: pd.Series) -> str:
        """按列名语义与数据类型推荐策略。"""
        lower = str(name).lower()
        if '年龄' in lower or lower in ('age', 'age_years'):
            return 'age_band'
        if any(key in lower for key in ('日期', '时间', 'date', 'time', '出生', '入院', '出院')):
            if pd.api.types.is_datetime64_any_dtype(series):
                return 'year_only'
            sample = series.dropna().head(5).astype(str)
            if not sample.empty and sample.str.contains(r'\d{4}[-/年]', regex=True).any():
                return 'year_only'
        if any(key in lower for key in ('姓名', '名字', '住址', '地址', '备注', '原因',
                                        '诊断', '手术', '症状', '主诉')):
            return 'mask'
        if any(key in lower for key in ('身份证', '证件', 'id', '编号', '序号', '病历号',
                                        '住院号', '门诊号', '病案号', '社保', '医保',
                                        '银行卡', '电话', '手机', '邮箱', '微信', '账号',
                                        '帐号')):
            return 'hash'
        return 'mask'

    @classmethod
    def plan_for_all(cls, df: pd.DataFrame) -> Dict[str, str]:
        """为所有列生成一份建议方案（非敏感列默认不处理）。"""
        sensitive = set(cls.detect_sensitive_columns(df))
        return {name: (cls.suggest_strategy(name, df[name]) if name in sensitive else 'keep')
                for name in df.columns}

    @classmethod
    def detect_plan(cls, df: pd.DataFrame) -> Dict[str, str]:
        """只为识别出的敏感列生成方案。"""
        return {name: cls.suggest_strategy(name, df[name])
                for name in cls.detect_sensitive_columns(df)}

    # ---------- 单值改写 ----------
    @staticmethod
    def _mask_text(value) -> str:
        text = str(value)
        if len(text) <= 1:
            return '*'
        if len(text) == 2:
            return text[0] + '*'
        return text[0] + '*' * (len(text) - 2) + text[-1]

    @staticmethod
    def _hash_text(value, salt: str = 'medbench') -> str:
        digest = hashlib.sha256(f'{salt}:{value}'.encode('utf-8')).hexdigest()
        return f'P{digest[:10].upper()}'

    @staticmethod
    def _age_band(value) -> str:
        try:
            age = float(value)
        except (TypeError, ValueError):
            return '—'
        if not np.isfinite(age) or age < 0 or age > 130:
            return '—'
        if age < 18:
            return '<18'
        if age < 41:
            return '18–40'
        if age < 66:
            return '41–65'
        if age < 81:
            return '66–80'
        return '>80'

    @staticmethod
    def _year_only(value) -> str:
        if isinstance(value, (int, float)) and not pd.isna(value):
            number = float(value)
            if 1900 <= number <= 2100:
                return str(int(number))
        parsed = pd.to_datetime(value, errors='coerce')
        if pd.isna(parsed):
            return '—'
        return str(int(parsed.year))

    # ---------- 批量应用 ----------
    @classmethod
    def apply(cls, df: pd.DataFrame, plan: Optional[Dict[str, str]] = None,
              keep_empty_as_na: bool = True) -> pd.DataFrame:
        """按方案改写数据。未知策略按「不处理」对待。"""
        result = df.copy()
        drop_columns: List[str] = []
        for name, strategy in (plan or {}).items():
            if name not in result.columns:
                continue
            strategy = strategy if strategy in cls.STRATEGIES else 'keep'
            if strategy == 'keep':
                continue
            if strategy == 'drop':
                drop_columns.append(name)
                continue
            series = result[name]

            def handle(value):
                if keep_empty_as_na and pd.isna(value):
                    return ''
                if strategy == 'mask':
                    return cls._mask_text(value)
                if strategy == 'hash':
                    return cls._hash_text(value)
                if strategy == 'age_band':
                    return cls._age_band(value)
                return cls._year_only(value)

            result[name] = series.map(handle)

        if drop_columns:
            result = result.drop(columns=drop_columns)
        return result

    @classmethod
    def summary(cls, plan: Optional[Dict[str, str]]) -> str:
        """一句话说明本次脱敏动作。"""
        plan = plan or {}
        counts: Dict[str, int] = {}
        for strategy in plan.values():
            if strategy == 'keep':
                continue
            counts[strategy] = counts.get(strategy, 0) + 1
        if not counts:
            return '未启用脱敏：导出内容与原始数据一致。'
        parts = [f'{cls.STRATEGIES.get(key, key)} × {value} 列' for key, value in counts.items()]
        return '将执行脱敏：' + '、'.join(parts) + '。'
