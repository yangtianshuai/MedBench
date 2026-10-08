"""
诊断一致性分析模块
负责：Kappa（未加权 / 线性加权 / 平方加权）、ICC（组内相关系数）、Bland-Altman

适用结构：同一指标有两列测量（两次测量、两名评估者、两种方法）。
"""
import io
import warnings
from typing import Dict, List, Optional, Sequence, Tuple

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings('ignore')

matplotlib.rcParams['font.sans-serif'] = [
    'SimHei', 'Microsoft YaHei',
    'WenQuanYi Zen Hei', 'WenQuanYi Micro Hei',
    'Noto Sans CJK SC', 'Noto Sans SC',
    'Source Han Sans SC',
    'Heiti SC', 'PingFang SC',
    'Arial Unicode MS', 'DejaVu Sans',
]
matplotlib.rcParams['axes.unicode_minus'] = False

PRIMARY = '#3B82F6'
SUCCESS = '#10B981'
WARNING = '#F59E0B'
DANGER = '#EF4444'

# Landis & Koch 一致性强度（阈值从高到低）
KAPPA_INTERPRETATION = (
    (0.81, '几乎完全一致（>0.80）'),
    (0.61, '高度一致（0.61–0.80）'),
    (0.41, '中等一致（0.41–0.60）'),
    (0.21, '一般一致（0.21–0.40）'),
    (0.0, '轻微一致（0.00–0.20）'),
    (float('-inf'), '一致性差（<0）'),
)
ICC_INTERPRETATION = (
    (0.9, '优秀（≥0.90）'),
    (0.75, '良好（0.75–0.90）'),
    (0.5, '中等（0.50–0.75）'),
    (float('-inf'), '较差（<0.50）'),
)


def _interpret(value: float, table) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return '无法判断'
    for threshold, label in table:
        if value >= threshold:
            return label
    return '无法判断'


def _icc21(matrix: np.ndarray) -> Optional[float]:
    """ICC(2,1) 绝对一致性（双向随机效应），供点估计与 Bootstrap 复用"""
    n, k = matrix.shape
    grand = matrix.mean()
    row_means = matrix.mean(axis=1)
    column_means = matrix.mean(axis=0)
    ss_rows = k * float(np.sum((row_means - grand) ** 2))
    ss_columns = n * float(np.sum((column_means - grand) ** 2))
    ss_total = float(np.sum((matrix - grand) ** 2))
    ss_error = ss_total - ss_rows - ss_columns
    ms_rows = ss_rows / (n - 1)
    ms_columns = ss_columns / (k - 1)
    ms_error = ss_error / ((n - 1) * (k - 1))
    if ms_error <= 1e-12:
        return 1.0
    denominator = ms_rows + (k - 1) * ms_error + k * (ms_columns - ms_error) / n
    if abs(denominator) < 1e-12:
        return None
    return float((ms_rows - ms_error) / denominator)


class AgreementAnalysis:
    """诊断一致性：Kappa / ICC / Bland-Altman"""

    # ---------- Kappa ----------
    @staticmethod
    def cohens_kappa(rater_a: Sequence, rater_b: Sequence,
                     weights: Optional[str] = None) -> Dict:
        """
        Cohen's Kappa。weights: None（未加权）| 'linear' | 'quadratic'
        加权 Kappa 要求两个评估者的类别是可排序的。
        """
        first = pd.Series(list(rater_a))
        second = pd.Series(list(rater_b))
        keep = first.notna() & second.notna()
        first, second = first[keep], second[keep]
        if len(first) < 5:
            return {'error': '有效配对样本不足 5 对'}

        raw_labels = sorted(set(first.astype(str)) | set(second.astype(str)))
        if len(raw_labels) < 2:
            return {'error': '两个评估者的取值只有一种，无法计算 Kappa'}

        index = {label: position for position, label in enumerate(raw_labels)}
        size = len(raw_labels)
        matrix = np.zeros((size, size), dtype=float)
        for left, right in zip(first.astype(str), second.astype(str)):
            matrix[index[left], index[right]] += 1

        total = matrix.sum()
        observed = float(np.trace(matrix) / total)
        row = matrix.sum(axis=1) / total
        column = matrix.sum(axis=0) / total
        expected = float(np.dot(row, column))

        if weights in ('linear', 'quadratic'):
            positions = np.arange(size)
            distance = np.abs(positions[:, None] - positions[None, :]) / max(size - 1, 1)
            weight_matrix = 1 - distance if weights == 'linear' else 1 - distance ** 2
            observed = float((weight_matrix * matrix).sum() / total)
            expected = float(np.sum(weight_matrix * np.outer(row, column)))
            weight_label = '线性加权' if weights == 'linear' else '平方加权'
        else:
            weight_label = '未加权'

        denominator = 1 - expected
        if abs(denominator) < 1e-12:
            kappa = 1.0
            standard_error = 0.0
        else:
            kappa = (observed - expected) / denominator
            standard_error = float(np.sqrt(observed * (1 - observed) / (total * denominator ** 2)))

        z_value = stats.norm.ppf(0.975)
        return {
            'n': int(total), 'kappa': float(kappa),
            'observed_agreement': float(observed), 'expected_agreement': float(expected),
            'se': standard_error,
            'ci': (float(kappa - z_value * standard_error),
                   float(kappa + z_value * standard_error)),
            'weights': weight_label,
            'categories': raw_labels,
            'interpretation': _interpret(kappa, KAPPA_INTERPRETATION),
            'test': f"Cohen's Kappa（{weight_label}）",
        }

    @staticmethod
    def fleiss_kappa(table: Sequence[Sequence[float]]) -> Dict:
        """Fleiss' Kappa：多名评估者、每名受试者被评次数相同的情形"""
        counts = np.asarray(table, dtype=float)
        if counts.ndim != 2 or counts.shape[0] < 2 or counts.shape[1] < 2:
            return {'error': '需要「受试者 × 类别」的计数矩阵'}
        ratings = counts.sum(axis=1)
        if len(set(ratings.tolist())) != 1:
            return {'error': 'Fleiss Kappa 要求每个受试者的评估次数相同'}
        n_subjects, n_categories = counts.shape
        n_ratings = ratings[0]
        if n_ratings < 2:
            return {'error': '每个受试者至少需要 2 次评估'}

        proportion = counts / n_ratings
        # P_i = (Σ_j p_ij² · n − 1) / (n − 1)
        p_subject = (np.square(proportion).sum(axis=1) * n_ratings - 1) / (n_ratings - 1)
        p_bar = float(p_subject.mean())
        p_category = counts.sum(axis=0) / (n_subjects * n_ratings)
        p_expected = float(np.square(p_category).sum())
        if abs(1 - p_expected) < 1e-12:
            return {'error': '类别分布过于集中，无法计算'}
        kappa = (p_bar - p_expected) / (1 - p_expected)
        return {
            'n': int(n_subjects), 'ratings': int(n_ratings),
            'kappa': float(kappa), 'observed_agreement': p_bar,
            'expected_agreement': p_expected,
            'interpretation': _interpret(kappa, KAPPA_INTERPRETATION),
            'test': "Fleiss' Kappa",
        }

    # ---------- ICC ----------
    @staticmethod
    def icc(measure1: Sequence, measure2: Sequence, n_bootstrap: int = 1000) -> Dict:
        """
        组内相关系数（两名评估者 / 两次测量）。
        同时给出 ICC(2,1) 绝对一致性、ICC(3,1) 一致性、ICC(1,1) 单因素。
        """
        values = pd.DataFrame({
            'measure1': pd.to_numeric(pd.Series(list(measure1)), errors='coerce'),
            'measure2': pd.to_numeric(pd.Series(list(measure2)), errors='coerce'),
        }).dropna()
        n = len(values)
        if n < 5:
            return {'error': '有效配对样本不足 5 对'}
        k = 2

        grand_mean = values.to_numpy().mean()
        row_means = values.mean(axis=1)
        column_means = values.mean(axis=0)

        ss_rows = k * float(np.sum((row_means - grand_mean) ** 2))
        ss_columns = n * float(np.sum((column_means - grand_mean) ** 2))
        ss_total = float(np.sum((values.to_numpy() - grand_mean) ** 2))
        ss_error = ss_total - ss_rows - ss_columns

        ms_rows = ss_rows / (n - 1)
        ms_columns = ss_columns / (k - 1)
        ms_error = ss_error / ((n - 1) * (k - 1))
        if ms_error <= 1e-12:
            return {
                'n': n, 'icc_21': 1.0, 'icc_31': 1.0, 'icc_11': 1.0,
                'ci': (1.0, 1.0), 'f': float('inf'), 'p': 0.0,
                'interpretation': _interpret(1.0, ICC_INTERPRETATION),
                'test': 'ICC(2,1) 绝对一致性',
                'note': '两次测量完全一致（残差为 0）',
            }

        icc_absolute = (ms_rows - ms_error) / (
            ms_rows + (k - 1) * ms_error + k * (ms_columns - ms_error) / n)
        icc_consistency = (ms_rows - ms_error) / (ms_rows + (k - 1) * ms_error)
        icc_single = icc_consistency

        f_value = ms_rows / ms_error
        p_value = float(1 - stats.f.cdf(f_value, n - 1, (n - 1) * (k - 1)))

        # 置信区间用 Bootstrap：k=2 时解析公式的两组自由度相等，界限会退化成一个点
        matrix = values.to_numpy()
        generator = np.random.default_rng(20260101)
        replicates = []
        for _ in range(int(n_bootstrap)):
            sample = matrix[generator.integers(0, n, n)]
            value = _icc21(sample)
            if value is not None:
                replicates.append(value)
        if len(replicates) >= 30:
            icc_lower, icc_upper = np.percentile(replicates, [2.5, 97.5])
        else:
            icc_lower = icc_upper = icc_absolute

        return {
            'n': n,
            'icc_21': float(icc_absolute),
            'icc_31': float(icc_consistency),
            'icc_11': float(icc_single),
            'ci': (float(max(-1.0, icc_lower)), float(min(1.0, icc_upper))),
            'n_bootstrap': len(replicates),
            'f': float(f_value), 'p': p_value,
            'interpretation': _interpret(icc_absolute, ICC_INTERPRETATION),
            'test': 'ICC(2,1) 绝对一致性',
        }

    # ---------- Bland-Altman ----------
    @staticmethod
    def bland_altman(measure1: Sequence, measure2: Sequence,
                     title: str = 'Bland-Altman 一致性分析',
                     label1: str = '测量1', label2: str = '测量2') -> Tuple[bytes, Dict]:
        """Bland-Altman：偏倚、一致性界限（LoA）、比例偏倚与差值分布"""
        values = pd.DataFrame({
            'first': pd.to_numeric(pd.Series(list(measure1)), errors='coerce'),
            'second': pd.to_numeric(pd.Series(list(measure2)), errors='coerce'),
        }).dropna()
        if len(values) < 5:
            return b'', {'error': '有效配对样本不足 5 对'}

        differences = values['first'] - values['second']
        averages = (values['first'] + values['second']) / 2
        bias = float(differences.mean())
        sd = float(differences.std(ddof=1))
        upper = bias + 1.96 * sd
        lower = bias - 1.96 * sd

        # 一致性界限的置信区间（Bland & Altman 1986 近似）
        standard_error_loa = np.sqrt(3 * sd ** 2 / len(values))
        t_critical = stats.t.ppf(0.975, len(values) - 1)
        loa_ci = ((float(lower - t_critical * standard_error_loa),
                   float(lower + t_critical * standard_error_loa)),
                  (float(upper - t_critical * standard_error_loa),
                   float(upper + t_critical * standard_error_loa)))

        outside = int(((differences > upper) | (differences < lower)).sum())
        slope, intercept, r_value, p_value, std_error = stats.linregress(
            averages.to_numpy(), differences.to_numpy())

        fig, axes = plt.subplots(1, 2, figsize=(12, 5), dpi=100,
                                 gridspec_kw={'width_ratios': [2, 1]})
        ax = axes[0]
        ax.scatter(averages, differences, color=PRIMARY, alpha=0.7, s=28, label='配对差值')
        ax.axhline(bias, color=SUCCESS, linewidth=1.8, label=f'偏倚 {bias:.3f}')
        ax.axhline(upper, color=DANGER, linewidth=1.5, linestyle='--',
                   label=f'+1.96SD {upper:.3f}')
        ax.axhline(lower, color=DANGER, linewidth=1.5, linestyle='--',
                   label=f'-1.96SD {lower:.3f}')
        ax.set_xlabel(f'两次测量均值（{label1} 与 {label2}）')
        ax.set_ylabel(f'差值（{label1} − {label2}）')
        ax.set_title(title, fontsize=12, fontweight='bold')
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc='best')

        ax2 = axes[1]
        ax2.hist(differences, bins=max(5, min(20, len(values) // 3)),
                 color=PRIMARY, alpha=0.75, edgecolor='white')
        ax2.axvline(bias, color=SUCCESS, linewidth=1.8)
        ax2.axvline(upper, color=DANGER, linewidth=1.4, linestyle='--')
        ax2.axvline(lower, color=DANGER, linewidth=1.4, linestyle='--')
        ax2.set_title('差值分布', fontsize=11)
        ax2.set_yticks([])
        ax2.grid(axis='x', alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)

        return buf.getvalue(), {
            'n': len(values),
            'bias': bias, 'sd': sd,
            'upper_loa': float(upper), 'lower_loa': float(lower),
            'loa_ci': loa_ci,
            'outside': outside,
            'outside_pct': round(outside / len(values) * 100, 2),
            'proportional_slope': float(slope),
            'proportional_p': float(p_value),
            'bias_display': f'{bias:.3f}（95%LoA {lower:.3f} ~ {upper:.3f}）',
            'test': 'Bland-Altman 一致性分析',
        }
