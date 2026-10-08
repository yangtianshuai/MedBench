"""
缺失数据处理模块
负责：缺失率汇总、缺失模式识别与可视化、简单填补、多重插补（MICE + Rubin 合并）

适用结构：任何存在缺失的数据；插补结果会作为新数据集写回工作区，供敏感性分析对比。
"""
import io
import warnings
from typing import Dict, List, Optional, Sequence

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
SLATE_200 = '#E2E8F0'


class MissingAnalysis:
    """缺失模式与插补"""

    @staticmethod
    def summary(df: pd.DataFrame, only_missing: bool = False) -> pd.DataFrame:
        """逐字段缺失情况"""
        rows = []
        total = len(df)
        for column in df.columns:
            missing = int(df[column].isna().sum())
            if only_missing and missing == 0:
                continue
            rows.append({
                '字段': column,
                '缺失数': missing,
                '缺失率%': round(missing / max(total, 1) * 100, 2),
                '有效数': total - missing,
                '唯一值': int(df[column].nunique()),
            })
        table = pd.DataFrame(rows)
        if not table.empty:
            table = table.sort_values('缺失率%', ascending=False).reset_index(drop=True)
        return table

    @staticmethod
    def pattern_table(df: pd.DataFrame, max_patterns: int = 15) -> pd.DataFrame:
        """缺失模式（哪些字段同时缺失）"""
        missing_flags = df.isna()
        columns = list(df.columns)
        patterns = {}
        for row in missing_flags.itertuples(index=False):
            key = tuple(i for i, flag in enumerate(row) if flag)
            patterns[key] = patterns.get(key, 0) + 1

        total = len(df)
        rows = []
        for key, count in sorted(patterns.items(), key=lambda item: -item[1])[:max_patterns]:
            label = '完整' if not key else '、'.join(str(columns[i]) for i in key[:4])
            if len(key) > 4:
                label += f' 等 {len(key)} 个字段'
            rows.append({
                '缺失模式': label,
                '缺失字段数': len(key),
                '样本数': count,
                '占比%': round(count / max(total, 1) * 100, 2),
            })
        return pd.DataFrame(rows)

    @staticmethod
    def missing_matrix_plot(df: pd.DataFrame, max_columns: int = 60,
                            max_rows: int = 400, title: str = '缺失矩阵（红=缺失）') -> bytes:
        """缺失矩阵图：横轴字段、纵轴样本"""
        subset = df.iloc[:, :max_columns]
        if len(subset) > max_rows:
            subset = subset.sample(max_rows, random_state=20260101).sort_index()
        if subset.empty or not df.isna().any().any():
            return b''

        matrix = subset.isna().to_numpy(dtype=float).T
        fig, ax = plt.subplots(figsize=(11, 5), dpi=100)
        ax.imshow(matrix, aspect='auto', cmap='RdYlGn_r', vmin=0, vmax=1, interpolation='nearest')
        ax.set_yticks(range(len(subset.columns)))
        ax.set_yticklabels([str(c)[:18] for c in subset.columns], fontsize=7)
        ax.set_xlabel(f'样本序号（共 {len(subset)} 行）')
        ax.set_title(title, fontsize=12, fontweight='bold')
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def missing_bar_plot(df: pd.DataFrame, max_columns: int = 40,
                         title: str = '字段缺失率') -> bytes:
        """缺失率条形图"""
        table = MissingAnalysis.summary(df, only_missing=True).head(max_columns)
        if table.empty:
            return b''

        fig, ax = plt.subplots(figsize=(9, max(3, 0.35 * len(table) + 1.5)), dpi=100)
        positions = np.arange(len(table))
        ax.barh(positions, table['缺失率%'], color=PRIMARY, alpha=0.85)
        ax.set_yticks(positions)
        ax.set_yticklabels([str(c)[:20] for c in table['字段']], fontsize=8)
        ax.invert_yaxis()
        ax.set_xlabel('缺失率 %')
        ax.set_title(title, fontsize=12, fontweight='bold')
        for position, value in zip(positions, table['缺失率%']):
            ax.text(value + 0.5, position, f'{value:g}%', va='center', fontsize=7)
        ax.grid(axis='x', alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def simple_impute(df: pd.DataFrame, strategy: str = 'median',
                      columns: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """单值填补：median / mean（数值列）+ 众数（其余列）"""
        result = df.copy()
        targets = list(columns) if columns else [c for c in df.columns if df[c].isna().any()]
        for column in targets:
            if column not in result.columns:
                continue
            series = result[column]
            if pd.api.types.is_numeric_dtype(series):
                numeric = pd.to_numeric(series, errors='coerce')
                fill_value = numeric.median() if strategy == 'median' else numeric.mean()
                if pd.isna(fill_value):
                    continue
                result[column] = numeric.fillna(fill_value)
            else:
                mode = series.dropna().astype(str).mode()
                if len(mode) == 0:
                    continue
                result[column] = series.fillna(mode.iloc[0])
        return result

    @staticmethod
    def _mice_once(matrix: np.ndarray, max_iter: int = 10, seed: int = 20260101) -> np.ndarray:
        """
        单次链式方程插补（MICE，normal-based）：
        以列均值为起始值，逐列用其余列做线性回归，
        缺失值用「预测值 + 残差扰动」填充，多轮迭代至收敛。
        只用 numpy 实现，避免依赖打包环境中可能缺失的可选模块。
        """
        generator = np.random.default_rng(seed)
        data = np.array(matrix, dtype=float, copy=True)
        missing = np.isnan(data)
        if not missing.any():
            return data

        # 起始值：列均值，保证回归自变量完整
        column_means = np.nanmean(data, axis=0)
        column_means = np.where(np.isnan(column_means), 0.0, column_means)
        for column in range(data.shape[1]):
            if missing[:, column].any():
                data[missing[:, column], column] = column_means[column]

        # 缺失较少的列先插补，作为后续列的自变量更可靠
        order = list(np.argsort(missing.sum(axis=0)))
        for _ in range(int(max_iter)):
            for column in order:
                missing_rows = missing[:, column]
                if not missing_rows.any():
                    continue
                observed_rows = ~missing_rows
                if observed_rows.sum() < 3:
                    continue

                others = [index for index in range(data.shape[1]) if index != column]
                design = np.column_stack([np.ones(observed_rows.sum()),
                                          data[observed_rows][:, others]])
                target = data[observed_rows, column]
                coefficients, *_ = np.linalg.lstsq(design, target, rcond=None)

                residuals = target - design @ coefficients
                spread = float(np.std(residuals, ddof=1)) if len(target) > 1 else 0.0
                spread = spread if spread > 1e-9 else 1e-9

                missing_design = np.column_stack([np.ones(missing_rows.sum()),
                                                  data[missing_rows][:, others]])
                predicted = missing_design @ coefficients
                # 残差扰动保留插补的不确定性，这也是「多重」插补的根据
                data[missing_rows, column] = predicted + generator.normal(
                    0.0, spread, size=int(missing_rows.sum()))
        return data

    @staticmethod
    def mice_impute(df: pd.DataFrame, columns: Optional[Sequence[str]] = None,
                    n_imputations: int = 5, max_iter: int = 10) -> Dict:
        """
        多重插补（MICE，链式方程）：数值列做链式方程插补，
        分类列用众数；并用 Rubin 规则合并各次插补的均值估计。
        """
        targets = list(columns) if columns else [c for c in df.columns if df[c].isna().any()]
        if not targets:
            return {'error': '所选数据没有缺失值'}

        numeric_columns = [c for c in targets
                           if pd.api.types.is_numeric_dtype(df[c])
                           and pd.to_numeric(df[c], errors='coerce').notna().any()]
        if not numeric_columns:
            return {'error': '没有可用于多重插补的数值列（分类列请用众数填补）'}

        matrix = df[numeric_columns].apply(pd.to_numeric, errors='coerce').to_numpy(dtype=float)
        if not np.isnan(matrix).any():
            return {'error': '所选数值列没有缺失值，无需插补'}

        # 全部缺失的数值列无法参与插补，需要明确告知用户
        unusable_columns = [column for column in targets
                            if pd.api.types.is_numeric_dtype(df[column])
                            and not pd.to_numeric(df[column], errors='coerce').notna().any()]

        datasets = []
        for index in range(int(n_imputations)):
            datasets.append(MissingAnalysis._mice_once(matrix, max_iter=int(max_iter),
                                                       seed=20260101 + index))

        rows = []
        for position, column in enumerate(numeric_columns):
            estimates = [dataset[:, position].mean() for dataset in datasets]
            variances = [dataset[:, position].var(ddof=1) / len(dataset) for dataset in datasets]
            mean_estimate = float(np.mean(estimates))
            within = float(np.mean(variances))
            between = float(np.var(estimates, ddof=1)) if len(estimates) > 1 else 0.0
            total_variance = within + (1 + 1 / len(estimates)) * between
            standard_error = float(np.sqrt(total_variance))
            if between > 0:
                degrees = (len(estimates) - 1) * (1 + within / ((1 + 1 / len(estimates)) * between)) ** 2
            else:
                degrees = 1e6
            t_critical = stats.t.ppf(0.975, max(1.0, degrees))
            missing_info = (between + between / len(estimates)) / total_variance if total_variance > 0 else 0.0
            rows.append({
                '变量': column,
                '插补均值': round(mean_estimate, 4),
                '标准误': round(standard_error, 4),
                '95%CI': f'{mean_estimate - t_critical * standard_error:.3f} ~ '
                         f'{mean_estimate + t_critical * standard_error:.3f}',
                '缺失信息比例': round(missing_info, 3),
            })

        filled = df.copy()
        first = datasets[0]
        for position, column in enumerate(numeric_columns):
            filled[column] = first[:, position]
        others = [c for c in targets if c not in numeric_columns]
        if others:
            filled = MissingAnalysis.simple_impute(filled, strategy='median', columns=others)

        remaining = int(filled[targets].isna().sum().sum())
        return {
            'n_imputations': int(n_imputations),
            'max_iter': int(max_iter),
            'columns': numeric_columns,
            'categorical_columns': others,
            'rubin_table': rows,
            'dataset': filled,
            'unusable_columns': unusable_columns,
            'remaining_missing': remaining,
            'display': (f'已用 MICE 生成 {len(datasets)} 份插补数据（迭代 {max_iter} 次）；'
                        f'下方表格为 Rubin 规则合并后的均值估计'),
            'test': '多重插补（MICE）',
        }
