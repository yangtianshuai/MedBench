"""
纵向 / 重复测量数据分析：线性混合效应模型（LMM）与广义估计方程（GEE）

适用场景：同一对象在不同时间点被反复测量（数据为长表：一行 = 一个对象的一个时间点）。
- 线性混合效应模型（MixedLM）：把「个体差异」作为随机效应，适合连续型结局，可给出组内相关系数 ICC
- 广义估计方程（GEE）：把个体内相关作为「讨厌参数」用稳健标准误处理，适合连续 / 二分类 / 计数结局，
  相关系数结构可选（可交换、独立、AR1、无序）

与「配对 t / Wilcoxon」的区别：配对检验只能处理两个时间点，且无法纳入协变量；
上述两种方法可处理 ≥3 个时间点、不平衡随访与协变量调整。
"""
import io
import warnings
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

# 结果判定用的显著水平
ALPHA = 0.05

GEE_CORR_LABELS = {
    'exchangeable': '可交换（同一对象任意两次测量相关相同）',
    'independence': '独立（相关性交由稳健标准误处理）',
    'ar1': 'AR(1)（相邻时间点相关更强）',
    'unstructured': '无序（每个时间点对单独估计，需时间点较少）',
}

GEE_FAMILY_LABELS = {
    'gaussian': '连续型（Gaussian，等同线性模型）',
    'binomial': '二分类（Binomial，给 OR）',
    'poisson': '计数型（Poisson，给率比 RR）',
}


class LongitudinalAnalysis:
    """重复测量数据的建模与描述。"""

    # ---------- 数据准备 ----------
    @staticmethod
    def _prepare(df: pd.DataFrame, outcome: str, time_col: str, subject_col: str,
                 covariates: Optional[List[str]] = None) -> Dict:
        covariates = [c for c in (covariates or []) if c in df.columns
                      and c not in (outcome, time_col, subject_col)]
        # 取值恒定的协变量会与截距完全共线，导致「Singular matrix」，提前剔除
        constant_dropped = []
        for name in list(covariates):
            values = pd.to_numeric(df[name], errors='coerce').dropna()
            if values.nunique() <= 1:
                covariates.remove(name)
                constant_dropped.append(name)
        columns = [subject_col, time_col, outcome] + covariates
        data = df[columns].copy()
        data[outcome] = pd.to_numeric(data[outcome], errors='coerce')
        data[time_col] = pd.to_numeric(data[time_col], errors='coerce')
        for name in covariates:
            data[name] = pd.to_numeric(data[name], errors='coerce')
        data = data.dropna(subset=[subject_col, time_col, outcome])
        if data.empty:
            return {'error': '清洗后没有可用记录，请检查结局与时间列是否为数值型'}

        counts = data.groupby(subject_col).size()
        repeated = counts[counts >= 2]
        if len(repeated) < 5:
            return {'error': f'只有 {len(repeated)} 个对象有 ≥2 次测量，重复测量分析至少需要 5 个；'
                             '若每人只有一次记录，请改用普通回归或组间比较'}
        data = data[data[subject_col].isin(repeated.index)]
        n_time_points = int(data[time_col].nunique())
        if n_time_points < 2:
            return {'error': '时间变量只有 1 个取值，无法分析随时间的变化'}
        return {'data': data, 'covariates': covariates,
                'constant_dropped': constant_dropped,
                'n_subjects': int(data[subject_col].nunique()),
                'n_records': int(len(data)),
                'n_time_points': n_time_points,
                'time_values': sorted(data[time_col].unique().tolist())[:12],
                'missing_dropped': int(len(df) - len(data))}

    @staticmethod
    def _trajectory_plot(data: pd.DataFrame, outcome: str, time_col: str,
                         subject_col: str, title: str, max_subjects: int = 25) -> bytes:
        """个体轨迹（抽样若干条）+ 各时间点均值线与 95%CI。"""
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(7.6, 5.2), dpi=110)
        subjects = data[subject_col].drop_duplicates().tolist()
        rng = np.random.default_rng(42)
        if len(subjects) > max_subjects:
            subjects = list(rng.choice(subjects, size=max_subjects, replace=False))
        for subject in subjects:
            chunk = data[data[subject_col] == subject].sort_values(time_col)
            ax.plot(chunk[time_col], chunk[outcome], color='#94A3B8',
                    alpha=0.45, linewidth=1)

        summary = data.groupby(time_col)[outcome].agg(['mean', 'std', 'count'])
        summary['se'] = summary['std'] / np.sqrt(summary['count'].clip(lower=1))
        summary['ci'] = 1.96 * summary['se']
        ax.errorbar(summary.index, summary['mean'], yerr=summary['ci'],
                    fmt='o-', color='#2563EB', linewidth=2.5, capsize=4,
                    markersize=6, label='各时间点均值（95%CI）')
        ax.set_xlabel(str(time_col))
        ax.set_ylabel(str(outcome))
        ax.set_title(title, fontsize=12, fontweight='bold')
        ax.legend(fontsize=9)
        ax.grid(alpha=0.3)
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    # ---------- 线性混合效应模型 ----------
    @classmethod
    def linear_mixed(cls, df: pd.DataFrame, outcome: str, time_col: str,
                     subject_col: str, covariates: Optional[List[str]] = None) -> Dict:
        """线性混合效应模型（随机截距）。"""
        import statsmodels.formula.api as smf

        prepared = cls._prepare(df, outcome, time_col, subject_col, covariates)
        if 'error' in prepared:
            return prepared
        data = prepared['data']
        covars = prepared['covariates']

        fixed = [time_col] + covars
        # statsmodels 的公式接口对中文列名不友好（参数名会变成 Q("随访月")），
        # 因此先临时改名为 v0/v1…，拟合后再映射回原始列名。
        rename = {name: f'v{index}' for index, name in enumerate([outcome] + fixed)}
        reverse = {safe: name for name, safe in rename.items()}
        working = data.rename(columns=rename)
        formula = f'{rename[outcome]} ~ ' + ' + '.join(rename[name] for name in fixed)
        model = smf.mixedlm(formula, working, groups=working[subject_col])
        try:
            fit = model.fit(reml=True, method='lbfgs')
        except Exception:
            # lbfgs 在一部分数据上不收敛（例如时间变量含基线 0 时），
            # 先回退到默认优化器重试一次，仍然失败才报错
            try:
                fit = model.fit(reml=True)
            except Exception as error:
                return {'error': f'模型拟合失败：{error}。'
                                 '可尝试减少协变量，或改用 GEE'}

        rows = []
        for name in fit.params.index:
            if name == 'Group Var':
                continue
            display = '截距' if name == 'Intercept' else reverse.get(name, str(name))
            coefficient = float(fit.params[name])
            se = float(fit.bse.get(name, np.nan))
            p_value = float(fit.pvalues.get(name, np.nan))
            low = coefficient - 1.96 * se
            high = coefficient + 1.96 * se
            rows.append({
                '变量': display,
                '系数 β': round(coefficient, 4),
                '标准误': round(se, 4),
                '95%CI 下限': round(low, 4),
                '95%CI 上限': round(high, 4),
                'p 值': '<0.001' if p_value < 0.001 else f'{p_value:.4f}',
                '显著': '是' if p_value < ALPHA else '否',
            })
        table = pd.DataFrame(rows)

        random_var = float(fit.cov_re.iloc[0, 0]) if fit.cov_re.size else float('nan')
        residual_var = float(fit.scale)
        denominator = random_var + residual_var
        icc = random_var / denominator if denominator > 0 else float('nan')
        time_row = table[table['变量'] == time_col]
        time_effect = (f"{time_row.iloc[0]['系数 β']}（95%CI {time_row.iloc[0]['95%CI 下限']} ~ "
                       f"{time_row.iloc[0]['95%CI 上限']}，p {time_row.iloc[0]['p 值']}）"
                       if len(time_row) else '—')

        return {
            'table': table,
            'model': '线性混合效应模型（随机截距）',
            'n_subjects': prepared['n_subjects'],
            'n_records': prepared['n_records'],
            'n_time_points': prepared['n_time_points'],
            'time_values': prepared['time_values'],
            'missing_dropped': prepared['missing_dropped'],
            'covariates': covars,
            'random_var': round(random_var, 4),
            'residual_var': round(residual_var, 4),
            'icc': round(icc, 4) if np.isfinite(icc) else None,
            'aic': round(float(fit.aic), 2),
            'bic': round(float(fit.bic), 2),
            'time_effect': time_effect,
            'converged': bool(getattr(fit, 'converged', True)),
            'dropped_covariates': prepared.get('constant_dropped') or [],
            'plot': cls._trajectory_plot(
                data, outcome, time_col, subject_col,
                f'{outcome} 随 {time_col} 的变化轨迹'),
            'display': (f'线性混合效应模型：{prepared["n_subjects"]} 名对象、'
                        f'{prepared["n_records"]} 条记录、{prepared["n_time_points"]} 个时间点；'
                        f'时间效应 {time_effect}；'
                        + (f'ICC={icc:.3f}（个体差异解释约 {icc * 100:.0f}% 的变异）'
                           if np.isfinite(icc) else 'ICC 无法计算')
                        + (f'；已剔除取值恒定的协变量：{"、".join(prepared["constant_dropped"])}'
                           if prepared.get('constant_dropped') else '')),
        }

    # ---------- 广义估计方程 ----------
    @classmethod
    def gee(cls, df: pd.DataFrame, outcome: str, time_col: str, subject_col: str,
            covariates: Optional[List[str]] = None, family: str = 'gaussian',
            correlation: str = 'exchangeable') -> Dict:
        """广义估计方程（稳健标准误）。"""
        import statsmodels.api as sm

        prepared = cls._prepare(df, outcome, time_col, subject_col, covariates)
        if 'error' in prepared:
            return prepared
        data = prepared['data']
        covars = prepared['covariates']

        family = family if family in GEE_FAMILY_LABELS else 'gaussian'
        correlation = correlation if correlation in GEE_CORR_LABELS else 'exchangeable'
        if family == 'binomial' and data[outcome].nunique() != 2:
            return {'error': '二分类族要求结局变量只有两个取值（0/1）'}
        if family == 'poisson' and (data[outcome] < 0).any():
            return {'error': 'Poisson 族要求结局为非负计数'}

        families = {'gaussian': sm.families.Gaussian(),
                    'binomial': sm.families.Binomial(),
                    'poisson': sm.families.Poisson()}
        cov_structs = {'exchangeable': sm.cov_struct.Exchangeable(),
                       'independence': sm.cov_struct.Independence(),
                       'ar1': sm.cov_struct.Autoregressive(),
                       'unstructured': sm.cov_struct.Unstructured()}

        design = sm.add_constant(data[[time_col] + covars])
        column_names = list(design.columns)
        cov_struct = cov_structs[correlation]
        if correlation == 'ar1':
            # AR(1) 的自相关系数搜索容易「找不到右括号」，给一个合理初值更稳
            try:
                cov_struct.dep_params = np.array([0.3])
            except Exception:
                pass
        try:
            # statsmodels 0.15 的 GEE 不接受 pandas 对象（会触发多维索引报错），
            # 这里统一传 numpy 数组，再按列名映射回结果。
            model = sm.GEE(
                data[outcome].to_numpy(dtype=float),
                design.to_numpy(dtype=float),
                groups=data[subject_col].to_numpy(),
                time=data[time_col].to_numpy(dtype=float),
                family=families[family],
                cov_struct=cov_struct,
            )
            fit = model.fit()
        except Exception as error:
            hint = ('（AR(1) 的相关参数估计有时不收敛，可改用「可交换」结构，'
                    '结论通常一致）' if correlation == 'ar1' else '')
            return {'error': f'GEE 拟合失败：{error}。'
                             f'可尝试简化相关结构或协变量{hint}'}

        use_or = family in ('binomial', 'poisson')
        effect_label = 'OR' if family == 'binomial' else ('RR' if family == 'poisson' else '系数 β')
        params = np.asarray(fit.params, dtype=float).ravel()
        std_errors = np.asarray(fit.bse, dtype=float).ravel()
        pvalues = np.asarray(fit.pvalues, dtype=float).ravel()
        rows = []
        for index, name in enumerate(column_names):
            coefficient = float(params[index])
            se = float(std_errors[index])
            p_value = float(pvalues[index])
            if use_or:
                estimate = float(np.exp(coefficient))
                low = float(np.exp(coefficient - 1.96 * se))
                high = float(np.exp(coefficient + 1.96 * se))
            else:
                estimate, low, high = coefficient, coefficient - 1.96 * se, coefficient + 1.96 * se
            rows.append({
                '变量': '截距' if name == 'const' else name,
                effect_label: round(estimate, 4),
                '95%CI 下限': round(low, 4),
                '95%CI 上限': round(high, 4),
                '稳健标准误': round(se, 4),
                'p 值': '<0.001' if p_value < 0.001 else f'{p_value:.4f}',
                '显著': '是' if p_value < ALPHA else '否',
            })
        table = pd.DataFrame(rows)

        time_row = table[table['变量'] == time_col]
        time_effect = (f"{effect_label}={time_row.iloc[0][effect_label]}（95%CI "
                       f"{time_row.iloc[0]['95%CI 下限']} ~ {time_row.iloc[0]['95%CI 上限']}，"
                       f"p {time_row.iloc[0]['p 值']}）" if len(time_row) else '—')

        return {
            'table': table,
            'model': f'GEE（{GEE_FAMILY_LABELS[family]}，相关结构：{GEE_CORR_LABELS[correlation]}）',
            'effect_label': effect_label,
            'family': family,
            'correlation': correlation,
            'n_subjects': prepared['n_subjects'],
            'n_records': prepared['n_records'],
            'n_time_points': prepared['n_time_points'],
            'time_values': prepared['time_values'],
            'missing_dropped': prepared['missing_dropped'],
            'covariates': covars,
            'time_effect': time_effect,
            'dropped_covariates': prepared.get('constant_dropped') or [],
            'plot': cls._trajectory_plot(
                data, outcome, time_col, subject_col,
                f'{outcome} 随 {time_col} 的变化轨迹'),
            'display': (f'GEE（{GEE_FAMILY_LABELS[family]}，'
                        f'{GEE_CORR_LABELS[correlation].split("（")[0]}相关结构）：'
                        f'{prepared["n_subjects"]} 名对象、{prepared["n_records"]} 条记录；'
                        f'时间效应 {time_effect}'
                        + (f'；已剔除取值恒定的协变量：{"、".join(prepared["constant_dropped"])}'
                           if prepared.get('constant_dropped') else '')),
        }

    # ---------- 辅助：识别长表结构 ----------
    @staticmethod
    def suggest_columns(df: pd.DataFrame, fields: Dict) -> Dict:
        """按字段语义给出「个体 / 时间 / 结局」的候选列，供界面预选。"""
        numeric = [name for name, meta in fields.items()
                   if (meta.get('dtype') if isinstance(meta, dict) else meta.dtype) == 'numeric']
        identifier_like, time_like, outcome_like = [], [], []
        n_rows = max(len(df), 1)
        for name in df.columns:
            lower = str(name).lower()
            non_null = df[name].dropna()
            if not len(non_null):
                continue
            named_like_id = any(key in lower for key in
                                ('id', '编号', '患者', '对象', '病案', '病人', '个体'))
            if named_like_id:
                # 长表的个体标识列：同一取值反复出现（平均重复次数 ≥2）
                repeats = len(non_null) / max(int(non_null.nunique()), 1)
                identifier_like.append((repeats, name))
            if any(key in lower for key in ('时间', '时点', '随访', '月份', '周', 'day', 'time', 'visit')):
                time_like.append(name)
            if any(key in lower for key in ('评分', '得分', '值', '指标', '结局', '血压', '血糖')):
                outcome_like.append(name)

        # 重复出现次数多的列更可能是「个体」而非普通 ID，排在前面
        identifier_like.sort(key=lambda item: -item[0])
        subject_columns = [name for _, name in identifier_like]
        return {
            'subject': subject_columns,
            'time': [name for name in time_like if name in numeric] or time_like,
            'outcome': [name for name in outcome_like if name in numeric] or numeric,
            'numeric': numeric,
        }
