"""
多重比较校正

两类需求：
1. 手里已有一组 p 值（多个指标、多个亚组、多次比较）→ 做 FDR / Bonferroni / Holm / Šidák 校正
2. 一个数值变量 + 一个多分类分组变量 → 做 Tukey HSD 两两比较（含 95%CI 图）

依赖 statsmodels.stats.multitest 与 statsmodels.stats.multicomp。
"""
import io
import re
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd


def parse_pvalues(text: str) -> List[float]:
    """
    从粘贴文本里提取 p 值。

    支持换行、逗号、分号、空格分隔；支持 0.03、3e-4、<0.001、=.05 等写法；
    自动忽略非数值内容。
    """
    if not text:
        return []
    cleaned = (str(text).replace('<', ' ').replace('>', ' ')
               .replace('＝', '=').replace('，', ',').replace('；', ';'))
    values: List[float] = []
    for token in re.split(r'[,;\s]+', cleaned):
        token = token.strip().rstrip('.')
        if not token:
            continue
        try:
            value = float(token)
        except ValueError:
            continue
        if 0.0 <= value <= 1.0:
            values.append(value)
    return values


class MultiComparison:
    """多重比较校正与 Tukey HSD。"""

    METHOD_LABELS = {
        'fdr_bh': 'FDR（Benjamini–Hochberg）',
        'fdr_by': 'FDR（Benjamini–Yekutieli）',
        'bonferroni': 'Bonferroni',
        'holm': 'Holm–Bonferroni',
        'sidak': 'Šidák',
    }

    METHOD_HELP = {
        'fdr_bh': '控制错误发现率，探索性研究常用；比 Bonferroni 温和，不易漏掉真实差异',
        'fdr_by': '控制 FDR 且不依赖检验间独立性假设，是 FDR 家族里最保守的变体',
        'bonferroni': '控制族错误率（FWER），最严格；检验数一多，几乎都失去显著性',
        'holm': '逐步法控制 FWER，比 Bonferroni 略有力，是 FWER 场景的推荐做法',
        'sidak': 'Bonferroni 的近似修正，适合检验数不多的情形',
    }

    @staticmethod
    def adjust_pvalues(p_values: Sequence[float], method: str = 'fdr_bh',
                       alpha: float = 0.05,
                       labels: Optional[Sequence[str]] = None) -> Dict:
        """对一组 p 值做多重比较校正，返回校正前后对照表。"""
        from statsmodels.stats.multitest import multipletests

        values = [float(value) for value in (p_values or []) if value is not None]
        if len(values) < 2:
            return {'error': '至少需要 2 个 p 值（单个检验无需校正）'}
        if any(not (0.0 <= value <= 1.0) for value in values):
            return {'error': 'p 值必须落在 0 与 1 之间'}

        method = method if method in MultiComparison.METHOD_LABELS else 'fdr_bh'
        reject, adjusted, _, _ = multipletests(values, alpha=alpha, method=method)

        names = list(labels or [f'检验 {index + 1}' for index in range(len(values))])
        if len(names) != len(values):
            names = [f'检验 {index + 1}' for index in range(len(values))]

        def fmt(value: float) -> str:
            value = min(float(value), 1.0)
            return f'{value:.4f}' if value >= 0.0001 else '<0.0001'

        rows: List[Dict] = []
        for name, raw, adj, keep in zip(names, values, adjusted, reject):
            raw_significant = raw < alpha
            if raw_significant and not keep:
                change = '由显著变为不显著'
            elif (not raw_significant) and keep:
                change = '由不显著变为显著'
            else:
                change = '—'
            rows.append({
                '检验项': name,
                '原始 p': fmt(raw),
                '校正后 p': fmt(float(adj)),
                '原始显著': '是' if raw_significant else '否',
                '校正后显著': '是' if keep else '否',
                '结论变化': change,
            })

        n_raw = int(sum(1 for value in values if value < alpha))
        n_adjusted = int(sum(1 for value in reject if value))
        n_changed = int(sum(1 for row in rows if row['结论变化'] != '—'))

        return {
            'table': pd.DataFrame(rows),
            'n_tests': len(values),
            'method': MultiComparison.METHOD_LABELS[method],
            'method_code': method,
            'alpha': alpha,
            'n_significant_raw': n_raw,
            'n_significant_adjusted': n_adjusted,
            'n_changed': n_changed,
            'display': (f"{MultiComparison.METHOD_LABELS[method]}：共 {len(values)} 个检验，"
                        f"α={alpha:g}；校正前显著 {n_raw} 个，校正后 {n_adjusted} 个，"
                        f"结论发生改变 {n_changed} 个。"),
        }

    @staticmethod
    def pairwise_tukey(values: pd.Series, groups: pd.Series, alpha: float = 0.05,
                       max_groups: int = 20) -> Dict:
        """Tukey HSD 两两比较（要求各组方差齐性，适合 ANOVA 之后的成对比较）。"""
        from statsmodels.stats.multicomp import pairwise_tukeyhsd

        frame = pd.DataFrame({
            'value': pd.to_numeric(values, errors='coerce'),
            'group': groups,
        }).dropna()
        if frame.empty:
            return {'error': '没有有效数据'}

        counts = frame.groupby('group').size()
        usable = counts[counts >= 2].index
        dropped = [str(name) for name in counts.index if name not in set(usable)]
        frame = frame[frame['group'].isin(usable)]

        n_groups = int(frame['group'].nunique())
        if n_groups < 2:
            return {'error': '至少需要 2 个分组（且每组至少 2 例有效数据）'}
        if n_groups > max_groups:
            return {'error': f'分组数过多（{n_groups} 组）：Tukey HSD 建议不超过 {max_groups} 组，'
                             '请先合并稀有类别'}

        result = pairwise_tukeyhsd(frame['value'], frame['group'], alpha=alpha)
        raw = result.summary().data
        table = pd.DataFrame(raw[1:], columns=raw[0])
        rename = {'group1': '组1', 'group2': '组2', 'meandiff': '均值差',
                  'p-adj': '校正后 p', 'lower': 'CI 下限', 'upper': 'CI 上限',
                  'reject': '显著'}
        table = table.rename(columns=rename)

        for column in ('均值差', 'CI 下限', 'CI 上限'):
            if column in table.columns:
                table[column] = pd.to_numeric(table[column], errors='coerce').round(4)
        if '校正后 p' in table.columns:
            table['校正后 p'] = pd.to_numeric(table['校正后 p'], errors='coerce').map(
                lambda value: ('<0.0001' if value < 0.0001 else f'{value:.4f}')
                if pd.notna(value) else '—')
        if '显著' in table.columns:
            table['显著'] = table['显著'].map({True: '是', False: '否'})

        n_significant = int(np.sum(result.reject))
        display = (f'Tukey HSD：{n_groups} 组、{len(table)} 对比较，α={alpha:g}，'
                   f'其中 {n_significant} 对差异有统计学意义。')
        if dropped:
            display += '已忽略有效样本不足 2 例的分组：' + '、'.join(dropped[:5])

        return {
            'table': table,
            'n_groups': n_groups,
            'n_comparisons': int(len(table)),
            'n_significant': n_significant,
            'alpha': alpha,
            'dropped_groups': dropped,
            'plot': MultiComparison._tukey_plot(result),
            'display': display,
        }

    @staticmethod
    def _tukey_plot(result) -> bytes:
        """两两比较的均值差与 95%CI 区间图。"""
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        rows = result.summary().data[1:]
        if not rows:
            return b''
        pairs = [f'{row[0]} vs {row[1]}' for row in rows]
        diff = np.array([float(row[2]) for row in rows], dtype=float)
        lower = np.array([float(row[4]) for row in rows], dtype=float)
        upper = np.array([float(row[5]) for row in rows], dtype=float)

        height = max(3.0, 0.55 * len(pairs) + 1.2)
        fig, ax = plt.subplots(figsize=(7.2, height), dpi=110)
        positions = np.arange(len(pairs))
        ax.errorbar(diff, positions,
                    xerr=[diff - lower, upper - diff],
                    fmt='o', color='#2563EB', ecolor='#94A3B8',
                    elinewidth=2, capsize=5, markersize=7)
        ax.axvline(0, color='#EF4444', linestyle='--', alpha=0.7, linewidth=1.2)
        ax.set_yticks(positions)
        ax.set_yticklabels(pairs)
        ax.invert_yaxis()
        ax.set_xlabel('均值差（95% CI）')
        ax.set_title('Tukey HSD 两两比较', fontsize=12, fontweight='bold')
        ax.grid(alpha=0.3, axis='x')
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()
