"""
剂量-反应关系模块（限制性立方样条 RCS）
负责：样条基函数构造、整体关联检验与非线性检验、相对参考点的效应曲线与置信区间、作图

适用结构：一个连续暴露变量 + 结局（连续或二分类）+ 可选协变量。
用于回答「暴露与结局是不是直线关系、有没有阈值或 J 型」这类问题。
"""
import io
import warnings
from typing import Dict, List, Optional, Sequence, Tuple

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm
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
SURFACE = '#E2E8F0'


class DoseResponse:
    """限制性立方样条（RCS）剂量-反应分析"""

    # ---------- 样条基 ----------
    @staticmethod
    def choose_knots(x: Sequence[float], n_knots: int = 4) -> List[float]:
        """按分位数取节点（与 rms 包默认做法一致）"""
        values = pd.to_numeric(pd.Series(list(x)), errors='coerce').dropna().to_numpy()
        if len(values) < n_knots * 2:
            raise ValueError(f'有效样本 {len(values)} 个，不足以放置 {n_knots} 个节点')
        probabilities = np.linspace(0, 1, n_knots)
        return [float(v) for v in np.quantile(values, probabilities)]

    @staticmethod
    def rcs_basis(x: Sequence[float], knots: Sequence[float]) -> np.ndarray:
        """
        限制性立方样条基（Harrell / rms 写法）：
        返回「线性项 + n-2 个非线性项」共 n-1 列，n 为节点数。
        """
        values = np.asarray(x, dtype=float)
        ordered = sorted(float(v) for v in knots)
        n_knots = len(ordered)
        if n_knots < 3:
            raise ValueError('至少需要 3 个节点')
        if ordered[-1] == ordered[-2]:
            raise ValueError('节点取值重复，请调整节点数或参考区间')

        columns = [values]
        for index in range(n_knots - 2):
            term = (np.maximum(values - ordered[index], 0.0) ** 3
                    - np.maximum(values - ordered[n_knots - 2], 0.0) ** 3
                    * (ordered[n_knots - 1] - ordered[index])
                    / (ordered[n_knots - 1] - ordered[n_knots - 2]))
            columns.append(term)
        return np.column_stack(columns)

    # ---------- 分析 ----------
    @classmethod
    def analyze(cls, df: pd.DataFrame, exposure: str, outcome: str,
                covariates: Optional[Sequence[str]] = None,
                outcome_type: str = 'continuous', n_knots: int = 4,
                reference: Optional[float] = None, grid_size: int = 120) -> Dict:
        """RCS 分析：整体关联、非线性检验与相对参考点的效应曲线"""
        for column in (exposure, outcome):
            if column not in df.columns:
                return {'error': f'字段不存在：{column}'}

        covariates = [c for c in (covariates or []) if c in df.columns
                      and c not in (exposure, outcome)]

        frame = pd.DataFrame({
            'exposure': pd.to_numeric(df[exposure], errors='coerce'),
            'outcome': pd.to_numeric(df[outcome], errors='coerce'),
        })
        covariate_columns, categorical_columns = [], []
        for column in covariates:
            if pd.api.types.is_numeric_dtype(df[column]) and df[column].nunique() > 2:
                frame[column] = pd.to_numeric(df[column], errors='coerce')
                covariate_columns.append(column)
            else:
                categorical_columns.append(column)

        for column in categorical_columns:
            dummies = pd.get_dummies(df[column].astype('category'), prefix=column,
                                     drop_first=True)
            for name in dummies.columns:
                frame[name] = dummies[name].astype(float)
                covariate_columns.append(name)

        frame = frame.dropna()
        if len(frame) < 40:
            return {'error': f'有效样本仅 {len(frame)} 例，不足以做样条分析'}
        if frame['exposure'].nunique() < 10:
            return {'error': '暴露变量取值过少，无法拟合样条（请改用分组比较）'}
        if outcome_type == 'binary' and set(frame['outcome'].unique()) - {0, 1}:
            return {'error': '二分类结局只能是 0 与 1'}

        exposure_values = frame['exposure'].to_numpy(dtype=float)
        outcome_values = frame['outcome'].to_numpy(dtype=float)
        try:
            knots = cls.choose_knots(exposure_values, int(n_knots))
            basis = cls.rcs_basis(exposure_values, knots)
        except ValueError as error:
            return {'error': str(error)}

        covariate_block = frame[covariate_columns].to_numpy(dtype=float) \
            if covariate_columns else np.empty((len(frame), 0))

        def design(block: np.ndarray) -> np.ndarray:
            return sm.add_constant(np.column_stack([block, covariate_block]),
                                   has_constant='add')

        spline_design = design(basis)
        linear_design = design(exposure_values.reshape(-1, 1))
        null_design = design(np.empty((len(frame), 0)))

        family = sm.families.Binomial() if outcome_type == 'binary' else sm.families.Gaussian()
        try:
            spline_model = sm.GLM(outcome_values, spline_design, family=family).fit()
            linear_model = sm.GLM(outcome_values, linear_design, family=family).fit()
            null_model = sm.GLM(outcome_values, null_design, family=family).fit()
        except Exception as error:  # noqa: BLE001
            return {'error': f'模型拟合失败：{error}'}

        def likelihood_ratio(full, reduced, df_difference: int) -> Tuple[float, float]:
            statistic = 2 * (full.llf - reduced.llf)
            statistic = max(0.0, float(statistic))
            return statistic, float(1 - stats.chi2.cdf(statistic, max(df_difference, 1)))

        spline_columns = basis.shape[1]
        overall_stat, overall_p = likelihood_ratio(spline_model, null_model, spline_columns)
        nonlinear_stat, nonlinear_p = likelihood_ratio(spline_model, linear_model,
                                                       spline_columns - 1)

        # 参考点
        if reference is None:
            reference = float(np.median(exposure_values))
        reference = float(np.clip(reference, float(np.min(exposure_values)),
                                  float(np.max(exposure_values))))

        def design_row(value: float) -> np.ndarray:
            row = [1.0]
            row.extend(cls.rcs_basis([value], knots)[0].tolist())
            row.extend(np.zeros(len(covariate_columns)).tolist())   # 协变量固定在参照水平
            return np.asarray(row, dtype=float)

        reference_row = design_row(reference)
        parameters = spline_model.params
        covariance = spline_model.cov_params()

        grid = np.linspace(float(np.min(exposure_values)), float(np.max(exposure_values)),
                           int(grid_size))
        rows = []
        for value in grid:
            delta = design_row(float(value)) - reference_row
            estimate = float(delta @ parameters)
            variance = float(delta @ covariance @ delta.T)
            standard_error = float(np.sqrt(max(variance, 0.0)))
            if outcome_type == 'binary':
                rows.append({
                    '暴露值': round(float(value), 4),
                    'OR': round(float(np.exp(estimate)), 4),
                    '下限': round(float(np.exp(estimate - 1.96 * standard_error)), 4),
                    '上限': round(float(np.exp(estimate + 1.96 * standard_error)), 4),
                })
            else:
                rows.append({
                    '暴露值': round(float(value), 4),
                    '效应差值': round(estimate, 4),
                    '下限': round(estimate - 1.96 * standard_error, 4),
                    '上限': round(estimate + 1.96 * standard_error, 4),
                })
        curve = pd.DataFrame(rows)

        plot = cls._plot(curve, exposure_values, reference, outcome_type, exposure,
                         outcome, knots)
        extremes = curve.iloc[[0, -1]]

        return {
            'n': int(len(frame)),
            'outcome_type': outcome_type,
            'knots': knots,
            'reference': reference,
            'reference_label': (f'{exposure} = {reference:.4g}'),
            'overall': {'statistic': overall_stat, 'df': spline_columns, 'p': overall_p},
            'nonlinear': {'statistic': nonlinear_stat, 'df': spline_columns - 1,
                          'p': nonlinear_p},
            'curve': curve,
            'extremes': extremes,
            'plot': plot,
            'covariates': covariate_columns,
            'n_knots': int(n_knots),
            'test': f'限制性立方样条（{len(knots)} 节点）',
            'pseudo_r2': float(1 - spline_model.deviance / null_model.deviance)
            if null_model.deviance > 0 else float('nan'),
        }

    # ---------- 作图 ----------
    @staticmethod
    def _plot(curve: pd.DataFrame, exposure_values: np.ndarray, reference: float,
              outcome_type: str, exposure: str, outcome: str,
              knots: Sequence[float]) -> bytes:
        value_column = 'OR' if outcome_type == 'binary' else '效应差值'
        fig, ax = plt.subplots(figsize=(8.5, 5.4), dpi=100)
        ax.plot(curve['暴露值'], curve[value_column], color=PRIMARY, linewidth=2,
                label=('OR' if outcome_type == 'binary' else '效应差值'))
        ax.fill_between(curve['暴露值'], curve['下限'], curve['上限'],
                        color=PRIMARY, alpha=0.18, label='95%CI')
        reference_line = 1.0 if outcome_type == 'binary' else 0.0
        ax.axhline(reference_line, color=SUCCESS, linewidth=1.4, linestyle='-')
        ax.axvline(reference, color=DANGER, linewidth=1.3, linestyle='--',
                   label=f'参考点 {reference:.4g}')

        # 节点与样本分布
        for knot in knots:
            ax.axvline(knot, color=SURFACE, linewidth=0.9, zorder=0)
        ax.plot(exposure_values,
                np.full(len(exposure_values), ax.get_ylim()[0]),
                '|', color='#94A3B8', markersize=6, alpha=0.5, label='样本分布')

        ax.set_xlabel(exposure)
        ax.set_ylabel(f'{outcome} 的 {"OR" if outcome_type == "binary" else "效应差值"}'
                      f'（相对 {reference:.4g}）')
        ax.set_title(f'{exposure} 与 {outcome} 的剂量-反应关系（RCS）',
                     fontsize=12, fontweight='bold')
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc='best')
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()
