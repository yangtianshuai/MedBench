"""
质控与时间序列分析模块
负责：率的置信区间与组间比较、感染密度（‰）、SPC 控制图、趋势检验与同比环比

判据来自数据结构本身（分子/分母、时间点序列），不预设科室或业务场景。
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

# 中文字体（与 descriptive 模块保持一致）
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
SECONDARY = '#6366F1'
SUCCESS = '#10B981'
WARNING = '#F59E0B'
DANGER = '#EF4444'


def _finite(values: Sequence) -> np.ndarray:
    numbers = pd.to_numeric(pd.Series(list(values)), errors='coerce').to_numpy(dtype=float)
    return numbers[~np.isnan(numbers)]


class RateAnalysis:
    """率与强度指标：点估计、置信区间、两组比较"""

    @staticmethod
    def proportion_ci(events: float, total: float, method: str = 'wilson',
                      confidence: float = 0.95) -> Dict:
        """单组率的 95%CI。method: wilson（默认）| wald | exact（Clopper-Pearson）"""
        events = float(events)
        total = float(total)
        if total <= 0:
            return {'error': '分母为 0，无法计算率'}
        if events < 0 or events > total:
            return {'error': f'分子（{events:g}）应介于 0 与分母（{total:g}）之间'}

        rate = events / total
        alpha = 1 - confidence
        if method == 'wald':
            se = np.sqrt(rate * (1 - rate) / total)
            z = stats.norm.ppf(1 - alpha / 2)
            low, high = max(0.0, rate - z * se), min(1.0, rate + z * se)
            label = 'Wald 法'
        elif method == 'exact':
            low = 0.0 if events == 0 else float(stats.beta.ppf(alpha / 2, events, total - events + 1))
            high = 1.0 if events == total else float(
                stats.beta.ppf(1 - alpha / 2, events + 1, total - events))
            label = 'Clopper-Pearson 精确法'
        else:
            z = stats.norm.ppf(1 - alpha / 2)
            denominator = 1 + z ** 2 / total
            centre = (rate + z ** 2 / (2 * total)) / denominator
            half = z * np.sqrt(rate * (1 - rate) / total + z ** 2 / (4 * total ** 2)) / denominator
            low, high = max(0.0, centre - half), min(1.0, centre + half)
            label = 'Wilson 得分法'

        return {
            'events': events, 'total': total, 'rate': float(rate),
            'ci_low': float(low), 'ci_high': float(high),
            'method': label, 'confidence': confidence,
            'display': f'{rate * 100:.2f}%（{low * 100:.2f}–{high * 100:.2f}%）',
        }

    @staticmethod
    def compare_proportions(events1: float, total1: float, events2: float, total2: float,
                            confidence: float = 0.95) -> Dict:
        """两组率比较：率差 / 率比及其 95%CI、卡方或 Fisher 检验"""
        group1 = RateAnalysis.proportion_ci(events1, total1)
        group2 = RateAnalysis.proportion_ci(events2, total2)
        if 'error' in group1:
            return group1
        if 'error' in group2:
            return group2

        rate1, rate2 = group1['rate'], group2['rate']
        z = stats.norm.ppf(1 - (1 - confidence) / 2)

        # 率差（Wald）
        se_diff = np.sqrt(rate1 * (1 - rate1) / total1 + rate2 * (1 - rate2) / total2)
        diff = rate1 - rate2
        diff_low, diff_high = diff - z * se_diff, diff + z * se_diff

        # 率比（对数法，含 0 单元格时不计算）
        table = np.array([[events1, total1 - events1], [events2, total2 - events2]], dtype=float)
        if events1 > 0 and events2 > 0 and events1 < total1 and events2 < total2:
            ratio = rate1 / rate2
            se_log = np.sqrt(1 / events1 - 1 / total1 + 1 / events2 - 1 / total2)
            ratio_low = float(np.exp(np.log(ratio) - z * se_log))
            ratio_high = float(np.exp(np.log(ratio) + z * se_log))
            ratio_display = f'{ratio:.3f}（{ratio_low:.3f}–{ratio_high:.3f}）'
        else:
            ratio = ratio_low = ratio_high = float('nan')
            ratio_display = '存在 0 单元格，率比不可估计'

        chi2, p_chi, _, expected = stats.chi2_contingency(table, correction=False)
        if expected.min() < 5:
            _, p_value = stats.fisher_exact(table)
            test_name = 'Fisher 精确检验（存在期望频数 < 5 的格子）'
        else:
            p_value, test_name = float(p_chi), 'Pearson 卡方检验'

        return {
            'group1': group1, 'group2': group2,
            'diff': float(diff), 'diff_ci': (float(diff_low), float(diff_high)),
            'ratio': float(ratio), 'ratio_ci': (ratio_low, ratio_high),
            'ratio_display': ratio_display,
            'p': float(p_value), 'test': test_name,
            'confidence': confidence,
        }

    @staticmethod
    def poisson_rate_ci(events: float, exposure: float, multiplier: float = 1000.0,
                        confidence: float = 0.95) -> Dict:
        """强度（密度）指标：如 感染例次 / 导管日 × 1000‰，Poisson 精确 CI"""
        events = float(events)
        exposure = float(exposure)
        if exposure <= 0:
            return {'error': '暴露量（分母，如导管日）必须大于 0'}
        if events < 0:
            return {'error': '事件数不能为负'}

        rate = events / exposure * multiplier
        alpha = 1 - confidence
        low = 0.0 if events == 0 else float(
            stats.chi2.ppf(alpha / 2, 2 * events) / 2 / exposure * multiplier)
        high = float(stats.chi2.ppf(1 - alpha / 2, 2 * (events + 1)) / 2 / exposure * multiplier)
        unit = '‰' if abs(multiplier - 1000) < 1e-9 else f'×{multiplier:g}'
        return {
            'events': events, 'exposure': exposure, 'multiplier': multiplier,
            'rate': float(rate), 'ci_low': low, 'ci_high': high,
            'confidence': confidence,
            'display': f'{rate:.2f}{unit}（{low:.2f}–{high:.2f}）',
        }

    @staticmethod
    def compare_poisson_rates(events1: float, exposure1: float, events2: float,
                              exposure2: float, multiplier: float = 1000.0,
                              confidence: float = 0.95) -> Dict:
        """两组强度比较：密度比及其 95%CI（Poisson 对数法）"""
        group1 = RateAnalysis.poisson_rate_ci(events1, exposure1, multiplier)
        group2 = RateAnalysis.poisson_rate_ci(events2, exposure2, multiplier)
        if 'error' in group1:
            return group1
        if 'error' in group2:
            return group2

        z = stats.norm.ppf(1 - (1 - confidence) / 2)
        if events1 > 0 and events2 > 0:
            ratio = group1['rate'] / group2['rate']
            se_log = np.sqrt(1 / events1 + 1 / events2)
            ratio_low = float(np.exp(np.log(ratio) - z * se_log))
            ratio_high = float(np.exp(np.log(ratio) + z * se_log))
            z_stat = float(np.log(ratio) / se_log)
            p_value = float(2 * (1 - stats.norm.cdf(abs(z_stat))))
        else:
            ratio = ratio_low = ratio_high = z_stat = p_value = float('nan')

        return {
            'group1': group1, 'group2': group2,
            'ratio': ratio, 'ratio_ci': (ratio_low, ratio_high),
            'ratio_display': ('—' if np.isnan(ratio) else
                              f'{ratio:.3f}（{ratio_low:.3f}–{ratio_high:.3f}）'),
            'z': z_stat, 'p': p_value, 'confidence': confidence,
            'test': 'Poisson 对数比检验',
        }


class SpcChart:
    """SPC 控制图：P 图（率）、U 图（单位数密度）、I-MR 图（个体值）"""

    @staticmethod
    def detect_signals(values: Sequence[float], centre: Sequence[float],
                       sigma: Sequence[float], labels: Optional[Sequence] = None) -> List[Dict]:
        """判异：单点超 3σ、连续 8 点同侧、连续 6 点递增/递减、连续 3 点中 2 点超 2σ"""
        values = np.asarray(values, dtype=float)
        centre = np.asarray(centre, dtype=float)
        sigma = np.asarray(sigma, dtype=float)
        count = len(values)
        signals: List[Dict] = []

        def label_of(index: int) -> str:
            if labels is not None and index < len(labels):
                return str(labels[index])
            return f'第{index + 1}点'

        # 规则 1：单点超出 3σ
        for index in range(count):
            if sigma[index] <= 0:
                continue
            upper = centre[index] + 3 * sigma[index]
            lower = centre[index] - 3 * sigma[index]
            if values[index] > upper or values[index] < lower:
                signals.append({
                    '点位': label_of(index),
                    '规则': '1 点超出 3σ',
                    '取值': round(float(values[index]), 4),
                    '控制限': f'{lower:.4f} ~ {upper:.4f}',
                })

        # 规则 2：连续 8 点在中心线同侧
        side = []
        for value, line in zip(values, centre):
            side.append(1 if value > line else (-1 if value < line else 0))
        run = 1
        for index in range(1, count):
            if side[index] != 0 and side[index] == side[index - 1]:
                run += 1
                if run >= 8:
                    signals.append({
                        '点位': f'{label_of(index - 7)} ~ {label_of(index)}',
                        '规则': '连续 8 点在中心线同侧',
                        '取值': '—',
                        '控制限': '—',
                    })
                    run = 1
            else:
                run = 1

        # 规则 3：连续 6 点递增或递减
        run_up = run_down = 1
        for index in range(1, count):
            if values[index] > values[index - 1]:
                run_up, run_down = run_up + 1, 1
            elif values[index] < values[index - 1]:
                run_down, run_up = run_down + 1, 1
            else:
                run_up = run_down = 1
            if run_up >= 6:
                signals.append({
                    '点位': f'{label_of(index - 5)} ~ {label_of(index)}',
                    '规则': '连续 6 点递增', '取值': '—', '控制限': '—',
                })
                run_up = 1
            if run_down >= 6:
                signals.append({
                    '点位': f'{label_of(index - 5)} ~ {label_of(index)}',
                    '规则': '连续 6 点递减', '取值': '—', '控制限': '—',
                })
                run_down = 1

        # 规则 4：连续 3 点中有 2 点超出同侧 2σ
        for index in range(2, count):
            window = range(index - 2, index + 1)
            above = [i for i in window if sigma[i] > 0 and values[i] > centre[i] + 2 * sigma[i]]
            below = [i for i in window if sigma[i] > 0 and values[i] < centre[i] - 2 * sigma[i]]
            if len(above) >= 2:
                signals.append({
                    '点位': f'{label_of(index - 2)} ~ {label_of(index)}',
                    '规则': '连续 3 点中 2 点超出 +2σ', '取值': '—', '控制限': '—',
                })
            elif len(below) >= 2:
                signals.append({
                    '点位': f'{label_of(index - 2)} ~ {label_of(index)}',
                    '规则': '连续 3 点中 2 点超出 -2σ', '取值': '—', '控制限': '—',
                })

        return signals

    @staticmethod
    def _plot(values: np.ndarray, centre: np.ndarray, upper: np.ndarray, lower: np.ndarray,
              labels: Optional[Sequence], title: str, ylabel: str) -> bytes:
        fig, ax = plt.subplots(figsize=(10, 5), dpi=100)
        x = np.arange(len(values))
        ax.plot(x, values, marker='o', color=PRIMARY, linewidth=1.8, label='实测值')
        ax.plot(x, centre, color=SUCCESS, linewidth=1.6, linestyle='-', label='中心线 CL')
        ax.plot(x, upper, color=DANGER, linewidth=1.4, linestyle='--', label='UCL / LCL')
        ax.plot(x, lower, color=DANGER, linewidth=1.4, linestyle='--')

        outlier = (values > upper) | (values < lower)
        if outlier.any():
            ax.scatter(x[outlier], values[outlier], color=DANGER, s=70, zorder=5,
                       label='越界点')

        if labels is not None and len(labels) == len(values) and len(values) <= 30:
            ax.set_xticks(x)
            ax.set_xticklabels([str(v) for v in labels], rotation=45, ha='right', fontsize=8)

        ax.set_title(title, fontsize=13, fontweight='bold')
        ax.set_ylabel(ylabel)
        ax.grid(alpha=0.3)
        ax.legend(loc='best', fontsize=9)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def p_chart(events: Sequence[float], totals: Sequence[float],
                labels: Optional[Sequence] = None, title: str = 'P 控制图（率）') -> Dict:
        """P 图：适用于「分子/分母」型指标的率监控"""
        events = np.asarray(events, dtype=float)
        totals = np.asarray(totals, dtype=float)
        if len(events) != len(totals):
            return {'error': '分子与分母的观测数不一致'}
        if len(events) < 3 or np.nansum(totals) <= 0:
            return {'error': '至少需要 3 个时间点，且分母之和大于 0'}

        p_bar = float(np.nansum(events) / np.nansum(totals))
        with np.errstate(divide='ignore', invalid='ignore'):
            sigma = np.sqrt(p_bar * (1 - p_bar) / totals)
            values = events / totals
        sigma = np.where(np.isfinite(sigma), sigma, 0.0)
        centre = np.full(len(values), p_bar)
        upper = np.minimum(1.0, centre + 3 * sigma)
        lower = np.maximum(0.0, centre - 3 * sigma)

        signals = SpcChart.detect_signals(values, centre, sigma, labels)
        image = SpcChart._plot(values, centre, upper, lower, labels, title, '率')
        return {
            'chart': image, 'values': values, 'centre': p_bar, 'sigma': sigma,
            'upper': upper, 'lower': lower, 'signals': signals,
            'display': f'中心线 {p_bar * 100:.2f}%；平均上限 {np.nanmean(upper) * 100:.2f}%',
        }

    @staticmethod
    def u_chart(events: Sequence[float], exposure: Sequence[float],
                labels: Optional[Sequence] = None, multiplier: float = 1000.0,
                title: str = 'U 控制图（单位数密度）') -> Dict:
        """U 图：适用于「例次 / 导管日」型密度指标（如 ‰导管日）"""
        events = np.asarray(events, dtype=float)
        exposure = np.asarray(exposure, dtype=float)
        if len(events) != len(exposure):
            return {'error': '事件数与暴露量的观测数不一致'}
        valid = (exposure > 0) & np.isfinite(events) & np.isfinite(exposure)
        if valid.sum() < 3:
            return {'error': '至少需要 3 个暴露量大于 0 的时间点'}

        events, exposure = events[valid], exposure[valid]
        if labels is not None:
            labels = [label for label, keep in zip(labels, valid) if keep]

        u_bar = float(np.sum(events) / np.sum(exposure))
        values = events / exposure * multiplier
        with np.errstate(divide='ignore', invalid='ignore'):
            sigma = np.sqrt(u_bar / exposure) * multiplier
        centre = np.full(len(values), u_bar * multiplier)
        upper = centre + 3 * sigma
        lower = np.maximum(0.0, centre - 3 * sigma)

        signals = SpcChart.detect_signals(values, centre, sigma, labels)
        image = SpcChart._plot(values, centre, upper, lower, labels, title, '单位数密度')
        return {
            'chart': image, 'values': values, 'centre': u_bar * multiplier, 'sigma': sigma,
            'upper': upper, 'lower': lower, 'signals': signals,
            'display': f'中心线 {u_bar * multiplier:.2f}',
        }

    @staticmethod
    def i_mr_chart(values: Sequence[float], labels: Optional[Sequence] = None,
                   title: str = 'I-MR 控制图（个体值）') -> Dict:
        """I-MR 图：适用于单个连续指标随时间变化（无分母）"""
        numbers = _finite(values)
        if len(numbers) < 3:
            return {'error': '至少需要 3 个有效数值'}
        if labels is not None:
            numeric = pd.to_numeric(pd.Series(list(values)), errors='coerce')
            labels = [label for label, keep in zip(labels, numeric.notna()) if keep]

        moving_range = np.abs(np.diff(numbers))
        mr_bar = float(np.mean(moving_range)) if len(moving_range) > 0 else 0.0
        if mr_bar <= 0:
            return {'error': '该指标在所选时间点上没有波动（或有效点不足），无法建立控制限'}

        centre_value = float(np.mean(numbers))
        sigma_value = mr_bar / 1.128

        centre = np.full(len(numbers), centre_value)
        sigma = np.full(len(numbers), sigma_value)
        upper = centre + 3 * sigma
        lower = centre - 3 * sigma

        signals = SpcChart.detect_signals(numbers, centre, sigma, labels)
        image = SpcChart._plot(numbers, centre, upper, lower, labels, title, '个体值')
        return {
            'chart': image, 'values': numbers, 'centre': centre_value, 'sigma': sigma_value,
            'mr_bar': mr_bar, 'upper': float(upper[0]), 'lower': float(lower[0]),
            'signals': signals,
            'display': f'中心线 {centre_value:.4f}；σ ≈ {sigma_value:.4f}（由平均移动极差估计）',
        }


class TrendTest:
    """趋势检验与同比环比"""

    @staticmethod
    def cochran_armitage(events: Sequence[float], totals: Sequence[float],
                         scores: Optional[Sequence[float]] = None) -> Dict:
        """Cochran-Armitage 趋势检验：各时间点的率是否存在单调趋势"""
        events = np.asarray(events, dtype=float)
        totals = np.asarray(totals, dtype=float)
        valid = (totals > 0) & np.isfinite(events) & np.isfinite(totals)
        events, totals = events[valid], totals[valid]
        if len(events) < 3:
            return {'error': '至少需要 3 个时间点'}

        if scores is None:
            scores = np.arange(len(events), dtype=float)
        scores = np.asarray(scores, dtype=float)[:len(events)]

        total_n = float(np.sum(totals))
        total_k = float(np.sum(events))
        p_bar = total_k / total_n
        statistic = float(np.sum(scores * (events - totals * p_bar)))
        variance = p_bar * (1 - p_bar) * (
            np.sum(totals * scores ** 2) - (np.sum(totals * scores) ** 2) / total_n)
        if variance <= 0:
            return {'error': '无法计算趋势（时间得分无变异）'}

        z = statistic / np.sqrt(variance)
        p_value = float(2 * (1 - stats.norm.cdf(abs(z))))
        return {
            'z': float(z), 'p': p_value,
            'direction': '上升' if z > 0 else '下降',
            'rates': (events / totals).tolist(),
            'test': 'Cochran-Armitage 趋势检验',
        }

    @staticmethod
    def linear_trend(values: Sequence[float],
                     scores: Optional[Sequence[float]] = None) -> Dict:
        """线性趋势：斜率及 95%CI、R²、p 值（时间得分缺省为 0,1,2,…）"""
        numbers = pd.to_numeric(pd.Series(list(values)), errors='coerce')
        keep = numbers.notna()
        numbers = numbers[keep].to_numpy(dtype=float)
        if scores is None:
            scores = np.arange(len(numbers), dtype=float)
        else:
            scores = np.asarray(scores, dtype=float)[keep.to_numpy()]
        if len(numbers) < 3:
            return {'error': '至少需要 3 个有效数值'}
        if np.ptp(numbers) == 0:
            return {'error': '指标取值没有变化，无法估计趋势'}

        result = stats.linregress(scores, numbers)
        degrees = len(numbers) - 2
        t_critical = stats.t.ppf(0.975, degrees)
        slope_ci = (float(result.slope - t_critical * result.stderr),
                    float(result.slope + t_critical * result.stderr))
        return {
            'slope': float(result.slope), 'slope_ci': slope_ci,
            'intercept': float(result.intercept),
            'r_squared': float(result.rvalue ** 2), 'p': float(result.pvalue),
            'direction': '上升' if result.slope > 0 else '下降',
            'per_step': f'每个时间点变化 {result.slope:+.4f}',
            'test': '线性回归趋势（最小二乘）',
        }

    @staticmethod
    def mann_kendall(values: Sequence[float]) -> Dict:
        """Mann-Kendall 单调趋势检验（不要求线性、对异常值稳健）"""
        numbers = _finite(values)
        if len(numbers) < 4:
            return {'error': '至少需要 4 个有效数值'}
        tau, p_value = stats.kendalltau(np.arange(len(numbers)), numbers)
        return {
            'tau': float(tau), 'p': float(p_value),
            'direction': '上升' if tau > 0 else ('下降' if tau < 0 else '无方向'),
            'test': 'Mann-Kendall 趋势检验',
        }

    @staticmethod
    def period_over_period(values: Sequence[float], labels: Optional[Sequence] = None,
                           period: int = 12, value_name: str = '指标值') -> pd.DataFrame:
        """
        环比 / 同比变化：period 为一个周期包含的观测数
        （月度数据 period=12 即为同比；period=1 为环比）
        """
        numbers = pd.to_numeric(pd.Series(list(values)), errors='coerce')
        table = pd.DataFrame({
            '时间点': list(labels) if labels is not None else np.arange(1, len(numbers) + 1),
            value_name: numbers,
        })
        table['环比变化'] = numbers.diff(1)
        table['环比变化率%'] = (numbers.pct_change(1) * 100).round(2)
        if period and period > 1 and len(numbers) > period:
            table['同比变化'] = numbers.diff(period)
            table['同比变化率%'] = (numbers.pct_change(period) * 100).round(2)
        return table
