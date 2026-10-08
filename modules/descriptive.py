"""
描述性分析与可视化模块
负责：分布描述、正态性检验、自动图表生成
"""
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib
import seaborn as sns
from scipy import stats
from typing import Dict, List, Optional, Tuple
import io
import re
import warnings
warnings.filterwarnings('ignore')

_LOOSE_NA = {'', '-', '--', '---', '—', '——', '－', '/', '\\', 'n/a', 'na',
             'nan', 'none', 'null', '无', '缺失', '不详', '未查', '空'}


def loose_numeric(value):
    """把「28人」「95.2%」「５个月」解析为数值，缺失写法返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, np.number)):
        return None if pd.isna(value) else float(value)
    text = str(value).strip()
    if text.lower() in _LOOSE_NA:
        return None
    match = re.search(r'[-+]?\d+(?:\.\d+)?', text.replace(',', ''))
    return float(match.group()) if match else None

# 中文字体设置（跨平台：Windows / Linux 服务器 / macOS）
# 顺序很重要：先写本机可能有的中文字体，最后回退 DejaVu Sans
matplotlib.rcParams['font.sans-serif'] = [
    'SimHei', 'Microsoft YaHei',        # Windows
    'WenQuanYi Zen Hei',                # Linux（Debian 系 fonts-wqy-zenhei）
    'WenQuanYi Micro Hei',
    'Noto Sans CJK SC', 'Noto Sans SC', # Linux（RedHat 系 / 通用）
    'Source Han Sans SC',
    'Heiti SC', 'PingFang SC',          # macOS
    'Arial Unicode MS',
    'DejaVu Sans'                       # 最终回退
]
matplotlib.rcParams['axes.unicode_minus'] = False  # 解决负号显示为方块

# 颜色调色板（tech-blue主题）
PRIMARY = '#3B82F6'
SECONDARY = '#6366F1'
SUCCESS = '#10B981'
WARNING = '#F59E0B'
DANGER = '#EF4444'
COLORS = ['#3B82F6', '#6366F1', '#8B5CF6', '#06B6D4', '#10B981', '#F59E0B', '#EF4444', '#EC4899']


class DescriptiveAnalysis:
    """描述性统计分析"""

    @staticmethod
    def numeric_summary(series: pd.Series) -> Dict:
        """数值变量描述：均数±标准差 vs 中位数IQR + 正态性检验"""
        num = pd.to_numeric(series, errors='coerce')
        if num.notna().sum() < 3:
            # 兼容「28人」「95.2%」这类带单位写法
            num = pd.to_numeric(series.map(loose_numeric), errors='coerce')
        num = num.dropna()
        if len(num) < 3:
            return {
                'n': int(len(num)),
                'mean_std': '—',
                'median_iqr': '—',
                'is_normal': False,
                'normality_p_display': '—',
                'recommended_desc': '有效样本不足',
                'recommended_test': '有效样本不足',
                'error': '有效样本不足（至少需要 3 个有效数值）',
            }

        mean = float(num.mean())
        std = float(num.std(ddof=1))
        median = float(num.median())
        q1, q3 = float(num.quantile(0.25)), float(num.quantile(0.75))
        iqr = q3 - q1
        skew = float(num.skew())
        kurt = float(num.kurt())

        # 正态性检验
        n = len(num)
        if n <= 5000:
            shapiro_stat, shapiro_p = stats.shapiro(num)
            test_name = 'Shapiro-Wilk'
        else:
            # 大样本用Anderson-Darling
            ad_result = stats.anderson(num, dist='norm')
            shapiro_stat = float(ad_result.statistic)
            # 5%水平对应临界值
            crit_idx = list(ad_result.critical_values).index(
                [v for v in ad_result.critical_values if abs(v - 5) < 0.1][0]
            ) if 5 in [int(v) for v in ad_result.significance_level] else 2
            # 简化：p值近似
            shapiro_p = 0.5 if shapiro_stat < ad_result.critical_values[2] else 0.001
            test_name = 'Anderson-Darling'

        is_normal = shapiro_p > 0.05

        # p值友好显示
        p_display = round(float(shapiro_p), 4)
        p_label = '>0.05' if p_display > 0.05 else '<0.001'

        return {
            'n': int(n),
            'mean': round(mean, 4),
            'std': round(std, 4),
            'mean_std': f"{mean:.2f} ± {std:.2f}",
            'median': round(median, 4),
            'q1': round(q1, 4),
            'q3': round(q3, 4),
            'iqr': round(iqr, 4),
            'median_iqr': f"{median:.2f} ({q1:.2f}, {q3:.2f})",
            'min': round(float(num.min()), 4),
            'max': round(float(num.max()), 4),
            'skewness': round(skew, 4),
            'kurtosis': round(kurt, 4),
            'normality_test': test_name,
            'normality_stat': round(float(shapiro_stat), 4),
            'normality_p': float(shapiro_p),
            'normality_p_display': p_label,
            'is_normal': is_normal,
            'recommended_desc': 'mean±sd' if is_normal else 'median(IQR)',
            'recommended_test': 't/ANOVA' if is_normal else 'Mann-Whitney/Kruskal-Wallis'
        }

    @staticmethod
    def categorical_summary(series: pd.Series, max_categories: int = 20) -> Dict:
        """分类变量描述：频数/构成比"""
        s = series.dropna().astype(str)
        if len(s) == 0:
            return {'error': '无有效数据'}

        vc = s.value_counts()
        total = len(s)

        rows = []
        for val, cnt in vc.items():
            rows.append({
                'value': val,
                'count': int(cnt),
                'percent': round(cnt / total * 100, 2)
            })

        # 截断过多类别
        if len(rows) > max_categories:
            top = rows[:max_categories]
            other_cnt = sum(r['count'] for r in rows[max_categories:])
            if other_cnt > 0:
                top.append({
                    'value': f'其他({len(rows) - max_categories}类)',
                    'count': other_cnt,
                    'percent': round(other_cnt / total * 100, 2)
                })
            rows = top

        return {
            'n': int(total),
            'n_categories': int(vc.count()),
            'table': rows,
            'top': str(vc.index[0]),
            'top_pct': round(vc.iloc[0] / total * 100, 2)
        }

    @staticmethod
    def hist_plot(series: pd.Series, title: str = '', bins: int = 30) -> bytes:
        """直方图 + 核密度"""
        num = pd.to_numeric(series, errors='coerce').dropna()
        if len(num) < 2:
            return b''

        fig, ax = plt.subplots(figsize=(8, 5), dpi=100)
        ax.hist(num, bins=bins, color=PRIMARY, alpha=0.7, edgecolor='white', density=True)
        # KDE
        try:
            num.plot.kde(ax=ax, color=DANGER, linewidth=2)
        except Exception:
            pass

        # 标注均数/中位数
        ax.axvline(num.mean(), color=DANGER, linestyle='--', linewidth=1.5, label=f'均数={num.mean():.2f}')
        ax.axvline(num.median(), color=SUCCESS, linestyle='--', linewidth=1.5, label=f'中位数={num.median():.2f}')

        ax.set_title(title or '分布直方图', fontsize=13, fontweight='bold')
        ax.set_xlabel('取值')
        ax.set_ylabel('密度')
        ax.legend(loc='upper right', fontsize=9)
        ax.grid(axis='y', alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def box_plot(series: pd.Series, title: str = '', group_by: Optional[pd.Series] = None) -> bytes:
        """箱线图"""
        fig, ax = plt.subplots(figsize=(8, 5), dpi=100)

        if group_by is not None and group_by.nunique() > 1:
            data = pd.DataFrame({'x': pd.to_numeric(series, errors='coerce'), 'g': group_by.astype(str)})
            data = data.dropna()
            groups = sorted(data['g'].unique())
            plot_data = [data.loc[data['g'] == g, 'x'].values for g in groups]

            bp = ax.boxplot(plot_data, patch_artist=True, widths=0.6,
                            medianprops=dict(color=DANGER, linewidth=2))
            ax.set_xticks(range(1, len(groups) + 1))
            ax.set_xticklabels(groups)
            for i, patch in enumerate(bp['boxes']):
                patch.set_facecolor(COLORS[i % len(COLORS)])
                patch.set_alpha(0.7)
        else:
            num = pd.to_numeric(series, errors='coerce').dropna().values
            bp = ax.boxplot([num], patch_artist=True, widths=0.5,
                       medianprops=dict(color=DANGER, linewidth=2))
            bp['boxes'][0].set_facecolor(PRIMARY)
            bp['boxes'][0].set_alpha(0.7)

        ax.set_title(title or '箱线图', fontsize=13, fontweight='bold')
        ax.set_ylabel('取值')
        ax.grid(axis='y', alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def bar_plot(series: pd.Series, title: str = '', top_n: int = 15) -> bytes:
        """分类变量柱状图"""
        s = series.dropna().astype(str)
        if len(s) == 0:
            return b''

        vc = s.value_counts().head(top_n)
        fig, ax = plt.subplots(figsize=(8, 5), dpi=100)

        bars = ax.bar(range(len(vc)), vc.values, color=PRIMARY, alpha=0.85, edgecolor='white')
        # 渐变着色
        for i, bar in enumerate(bars):
            bar.set_color(COLORS[i % len(COLORS)])

        # 百分比标注
        total = vc.sum()
        for i, (val, cnt) in enumerate(vc.items()):
            pct = cnt / total * 100
            ax.text(i, cnt + max(vc.values) * 0.01, f'{pct:.1f}%',
                    ha='center', fontsize=9, color='#374151')

        ax.set_xticks(range(len(vc)))
        ax.set_xticklabels(vc.index, rotation=30 if max(len(str(v)) for v in vc.index) > 4 else 0, ha='right')
        ax.set_title(title or '频数分布', fontsize=13, fontweight='bold')
        ax.set_ylabel('频数')
        ax.grid(axis='y', alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def pie_plot(series: pd.Series, title: str = '', top_n: int = 8) -> bytes:
        """饼图"""
        s = series.dropna().astype(str)
        if len(s) == 0:
            return b''

        vc = s.value_counts().head(top_n)
        if len(vc) < len(s.value_counts()):
            other = s.value_counts().iloc[top_n:].sum()
            vc[f'其他({len(s.value_counts()) - top_n})'] = other

        fig, ax = plt.subplots(figsize=(7, 7), dpi=100)
        colors = [COLORS[i % len(COLORS)] for i in range(len(vc))]
        wedges, texts, autotexts = ax.pie(
            vc.values, labels=vc.index, autopct='%1.1f%%',
            colors=colors, startangle=90, wedgeprops={'edgecolor': 'white', 'linewidth': 2}
        )
        for t in autotexts:
            t.set_color('white')
            t.set_fontweight('bold')

        ax.set_title(title or '构成比', fontsize=13, fontweight='bold')
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def qq_plot(series: pd.Series, title: str = '') -> bytes:
        """QQ图（正态性诊断）"""
        num = pd.to_numeric(series, errors='coerce').dropna()
        if len(num) < 3:
            return b''

        fig, ax = plt.subplots(figsize=(6, 6), dpi=100)
        stats.probplot(num, dist='norm', plot=ax)
        ax.get_lines()[0].set_markerfacecolor(PRIMARY)
        ax.get_lines()[0].set_markeredgecolor(PRIMARY)
        ax.get_lines()[0].set_markersize(5)
        ax.get_lines()[1].set_color(DANGER)
        ax.get_lines()[1].set_linewidth(2)

        ax.set_title(f'{title} Q-Q图', fontsize=13, fontweight='bold')
        ax.grid(alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def correlation_heatmap(df_numeric: pd.DataFrame, title: str = '') -> bytes:
        """相关系数热图"""
        if df_numeric.shape[1] < 2:
            return b''

        fig, ax = plt.subplots(figsize=(max(7, df_numeric.shape[1] * 0.6), max(6, df_numeric.shape[1] * 0.6)), dpi=100)
        corr = df_numeric.corr(method='pearson')

        sns.heatmap(corr, annot=True, fmt='.2f', cmap='RdBu_r', center=0, vmin=-1, vmax=1,
                    square=True, linewidths=0.5, cbar_kws={'shrink': 0.8}, ax=ax)
        ax.set_title(title or '变量相关系数热图', fontsize=13, fontweight='bold')
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def scatter_plot(x: pd.Series, y: pd.Series, title: str = '',
                     group_by: Optional[pd.Series] = None) -> bytes:
        """散点图 + 回归线"""
        data = pd.DataFrame({
            'x': pd.to_numeric(x, errors='coerce'),
            'y': pd.to_numeric(y, errors='coerce')
        }).dropna()

        if len(data) < 3:
            return b''

        fig, ax = plt.subplots(figsize=(7.5, 5), dpi=100)

        if group_by is not None and group_by.nunique() > 1 and group_by.nunique() <= 6:
            data['g'] = group_by.loc[data.index].astype(str).values
            for i, (g, sub) in enumerate(data.groupby('g')):
                ax.scatter(sub['x'], sub['y'], color=COLORS[i % len(COLORS)], alpha=0.6,
                           label=g, edgecolor='white', s=40)
            ax.legend(title=group_by.name or '分组')
        else:
            ax.scatter(data['x'], data['y'], color=PRIMARY, alpha=0.6,
                       edgecolor='white', s=40)

        # 回归线
        try:
            slope, intercept, r, p, se = stats.linregress(data['x'], data['y'])
            xs = np.linspace(data['x'].min(), data['x'].max(), 100)
            ax.plot(xs, slope * xs + intercept, color=DANGER, linewidth=2,
                    label=f'r={r:.3f}, p={p:.3g}')
            ax.legend()
        except Exception:
            pass

        ax.set_xlabel(x.name or 'X')
        ax.set_ylabel(y.name or 'Y')
        ax.set_title(title or '散点图', fontsize=13, fontweight='bold')
        ax.grid(alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()