"""
基线特征表（Table 1）自动化

规则：
- 数值变量：各组均近似正态 → mean ± SD，组间用 Welch t / 单因素 ANOVA；
  否则 → median [IQR]，组间用 Mann-Whitney U / Kruskal-Wallis
- 分类变量：n (%)，2×2 且存在期望频数 <5 → Fisher 精确检验，否则皮尔逊卡方
- 标准化均值差（SMD）：两组时计算；多组时取各组水平 SMD 的最大绝对值
- 缺失值按变量独立处理（pairwise），不因某一变量缺失而丢弃整行
"""
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
from scipy import stats

NORMAL_MAX_N = 5000
MAX_GROUPS = 10
CAT_MAX_LEVELS = 12


class BaselineTable:
    """生成论文用基线特征表。"""

    # ---------- 基础工具 ----------
    @staticmethod
    def _numeric(series) -> np.ndarray:
        values = pd.to_numeric(series, errors='coerce').dropna().to_numpy(dtype=float)
        return values[np.isfinite(values)]

    @staticmethod
    def _is_normal(values: np.ndarray) -> bool:
        values = np.asarray(values, dtype=float)
        values = values[np.isfinite(values)]
        if len(values) < 8:
            return False
        if len(values) > NORMAL_MAX_N:
            return abs(float(stats.skew(values))) < 2.0
        try:
            return float(stats.shapiro(values).pvalue) >= 0.05
        except Exception:
            return False

    @staticmethod
    def _fmt_p(value: Optional[float]) -> str:
        if value is None or not np.isfinite(value):
            return '—'
        if value < 0.001:
            return '<0.001'
        return f'{value:.3f}'

    @staticmethod
    def _fmt_num(value: float) -> str:
        return f'{value:.2f}'.rstrip('0').rstrip('.') if abs(value) >= 0.01 else f'{value:.4f}'

    @staticmethod
    def _numeric_cell(values: np.ndarray, normal: bool) -> str:
        if len(values) == 0:
            return '—'
        if normal:
            if len(values) == 1:
                return f'{values[0]:.2f}'
            return f'{np.mean(values):.2f} ± {np.std(values, ddof=1):.2f}'
        q1, q3 = np.quantile(values, [0.25, 0.75])
        return f'{np.median(values):.2f} [{q1:.2f}, {q3:.2f}]'

    @staticmethod
    def _smd_numeric(a: np.ndarray, b: np.ndarray) -> Optional[float]:
        a = np.asarray(a, dtype=float)
        b = np.asarray(b, dtype=float)
        a, b = a[np.isfinite(a)], b[np.isfinite(b)]
        if len(a) < 2 or len(b) < 2:
            return None
        pooled = np.sqrt(((len(a) - 1) * np.var(a, ddof=1)
                          + (len(b) - 1) * np.var(b, ddof=1)) / (len(a) + len(b) - 2))
        if pooled <= 0:
            return None
        return float((np.mean(a) - np.mean(b)) / pooled)

    @staticmethod
    def _smd_proportion(p1: float, p2: float) -> Optional[float]:
        denominator = np.sqrt((p1 * (1 - p1) + p2 * (1 - p2)) / 2)
        if denominator <= 0:
            return None
        return float((p1 - p2) / denominator)

    # ---------- 检验 ----------
    @staticmethod
    def _test_numeric(groups: List[np.ndarray], normal: bool):
        usable = [group for group in groups if len(group) >= 2]
        if len(usable) < 2:
            return None, '有效样本不足，未做检验'
        if normal and all(BaselineTable._is_normal(group) for group in usable):
            if len(usable) == 2:
                _, p_value = stats.ttest_ind(*usable, equal_var=False)
                return float(p_value), 'Welch t 检验'
            _, p_value = stats.f_oneway(*usable)
            return float(p_value), '单因素 ANOVA'
        if len(usable) == 2:
            _, p_value = stats.mannwhitneyu(*usable, alternative='two-sided')
            return float(p_value), 'Mann-Whitney U 检验'
        _, p_value = stats.kruskal(*usable)
        return float(p_value), 'Kruskal-Wallis 检验'

    @staticmethod
    def _test_categorical(table: np.ndarray):
        table = np.asarray(table, dtype=float)
        table = table[table.sum(axis=1) > 0]
        if table.shape[0] < 2 or table.shape[1] < 2:
            return None, '分组或水平不足，未做检验'
        if table.shape == (2, 2):
            if (table < 5).any():
                _, p_value = stats.fisher_exact(table.astype(int))
                return float(p_value), 'Fisher 精确检验'
            _, p_value, _, _ = stats.chi2_contingency(table, correction=True)
            return float(p_value), '卡方检验（Yates 连续校正）'
        try:
            _, p_value, _, expected = stats.chi2_contingency(table)
        except Exception:
            return None, '卡方检验无法计算'
        small = float((expected < 5).mean())
        if small > 0.2:
            return float(p_value), f'卡方检验（注意：{small * 100:.0f}% 单元格期望频数 < 5）'
        return float(p_value), '卡方检验'

    # ---------- 主流程 ----------
    @classmethod
    def build(cls, df: pd.DataFrame, variables: Sequence[str],
              group_col: Optional[str] = None, alpha: float = 0.05,
              include_smd: bool = True, cat_max_levels: int = CAT_MAX_LEVELS) -> Dict:
        """生成基线特征表。group_col 为空则只输出总体描述。"""
        variables = [name for name in (variables or []) if name in df.columns]
        if not variables:
            return {'error': '请至少选择一个变量'}

        groups: List = []
        group_series = None
        if group_col and group_col in df.columns:
            group_series = df[group_col]
            observed = group_series.dropna()
            counts = observed.value_counts()
            groups = [value for value in counts.index if counts[value] >= 2]
            if len(groups) > MAX_GROUPS:
                return {'error': f'分组数过多（{len(groups)} 组）：基线表建议不超过 {MAX_GROUPS} 组，'
                                 '请先合并稀有类别'}
            if len(groups) < 2:
                return {'error': '分组变量至少需要 2 个组，且每组不少于 2 例'}
            dropped = [str(value) for value in counts.index if value not in set(groups)]
        else:
            dropped = []

        two_group = len(groups) == 2
        group_label = str(group_col) if group_series is not None else '分组'

        def column_name(value) -> str:
            """表头带上分组变量名，避免只写「1 (n=211)」看不出是谁的分组。"""
            size = int((group_series == value).sum()) if group_series is not None else 0
            return f'{group_label}={value} (n={size})'

        columns = ['特征', f'总体 (n={len(df)})']
        columns += [column_name(value) for value in groups]
        columns.append('P 值')
        if include_smd and two_group:
            columns.append('SMD')
        columns += ['统计描述/检验']

        rows: List[Dict] = []
        notes: List[str] = []
        rows_plain: List[Dict] = []

        for name in variables:
            series = df[name]
            numeric_values = cls._numeric(series)
            non_null = series.dropna()
            n_levels = int(non_null.nunique())
            treat_numeric = (len(numeric_values) > 0
                             and len(numeric_values) >= max(3, int(0.6 * len(non_null)) if len(non_null) else 3)
                             and n_levels > 2)

            if treat_numeric:
                grouped_values = []
                if group_series is not None:
                    for value in groups:
                        grouped_values.append(cls._numeric(series[group_series == value]))
                normal_all = cls._is_normal(numeric_values)
                normal_groups = all(cls._is_normal(group) for group in grouped_values) if grouped_values else True
                use_normal = bool(normal_all and normal_groups)

                row = {'特征': f'{name}, {"mean ± SD" if use_normal else "median [IQR]"}',
                       f'总体 (n={len(df)})': cls._numeric_cell(numeric_values, use_normal)}
                for value, group_values in zip(groups, grouped_values):
                    row[column_name(value)] = cls._numeric_cell(group_values, use_normal)
                if grouped_values:
                    p_value, method = cls._test_numeric(grouped_values, use_normal)
                else:
                    p_value, method = None, '未分组，仅描述'
                row['P 值'] = cls._fmt_p(p_value)
                if include_smd and two_group:
                    smd = cls._smd_numeric(grouped_values[0], grouped_values[1]) if len(grouped_values) == 2 else None
                    row['SMD'] = '—' if smd is None else f'{abs(smd):.2f}'
                row['统计描述/检验'] = method
                rows.append(row)
                notes.append(f'{name}：{"近似正态" if use_normal else "非正态"}，{method}')
                rows_plain.append({'变量': name, '类型': '数值',
                                   '描述': 'mean ± SD' if use_normal else 'median [IQR]',
                                   '检验': method,
                                   'P 值': cls._fmt_p(p_value)})
                continue

            # 分类变量
            labels = [value for value in non_null.value_counts().index]
            if len(labels) > cat_max_levels:
                labels = labels[:cat_max_levels]
                notes.append(f'{name}：水平过多，仅列出前 {cat_max_levels} 个水平')
            is_binary = n_levels == 2
            head = {'特征': f'{name}, n (%)'}
            for column in columns:
                if column not in head:
                    head[column] = ''
            head[f'总体 (n={len(df)})'] = '—'

            if group_series is not None:
                table = []
                for value in groups:
                    subset = series[(group_series == value) & series.notna()]
                    table.append([int((subset == label).sum()) for label in labels])
                table = np.asarray(table, dtype=float)
                p_value, method = cls._test_categorical(table)
            else:
                p_value, method = None, '未分组，仅描述'
            head['P 值'] = cls._fmt_p(p_value)
            head['统计描述/检验'] = method
            rows.append(head)

            smd_max = None
            for index, label in enumerate(labels):
                row = {'特征': f'    {label}'}
                subset_all = series[series.notna()]
                total_n = len(subset_all)
                count_all = int((subset_all == label).sum())
                row[f'总体 (n={len(df)})'] = (f'{count_all} ({count_all / total_n * 100:.1f})'
                                             if total_n else '—')
                proportions = []
                for value in groups:
                    mask = (group_series == value) & series.notna()
                    group_values = series[mask]
                    count = int((group_values == label).sum())
                    denominator = int(len(group_values))
                    proportions.append(count / denominator if denominator else 0.0)
                    row[column_name(value)] = (
                        f'{count} ({count / denominator * 100:.1f})' if denominator else '—')
                row['P 值'] = ''
                if include_smd and two_group and len(proportions) == 2:
                    smd = cls._smd_proportion(proportions[0], proportions[1])
                    if smd is not None and (smd_max is None or abs(smd) > abs(smd_max)):
                        smd_max = smd
                row['统计描述/检验'] = ''
                rows.append(row)

            if include_smd and two_group:
                head['SMD'] = '—' if smd_max is None else f'{abs(smd_max):.2f}'
            notes.append(f'{name}：{n_levels} 个水平，{method}')
            rows_plain.append({'变量': name, '类型': '分类（%s 水平）' % n_levels,
                               '描述': 'n (%)', '检验': method, 'P 值': cls._fmt_p(p_value)})

        table = pd.DataFrame(rows)
        for column in columns:
            if column not in table.columns:
                table[column] = ''
        table = table[columns]

        summary = {
            'n_total': int(len(df)),
            'n_variables': len(variables),
            'groups': [str(value) for value in groups],
            'group_col': group_col or '',
            'table': table,
            'test_summary': pd.DataFrame(rows_plain),
            'notes': notes,
            'dropped_groups': dropped,
            'display': (f'共 {len(variables)} 个变量；' +
                        (f'按「{group_col}」分为 {len(groups)} 组；' if group_series is not None
                         else '未分组，仅输出总体描述；') +
                        ('含 SMD 列。' if (include_smd and two_group) else
                         ('多组情形不输出 SMD。' if include_smd else ''))),
        }
        if dropped:
            summary['display'] += '已忽略样本不足 2 例的组：' + '、'.join(dropped[:5])
        return summary
