"""
统计推断模块
负责：t检验/方差分析/非参数检验/卡方/Fisher/相关/回归
含效应量、95%CI、自动正态性决策
"""
import pandas as pd
import numpy as np
from scipy import stats
from typing import Dict, List, Optional, Tuple
import warnings
warnings.filterwarnings('ignore')


class InferentialTests:
    """统计推断"""

    # ---------- 数值变量组间比较 ----------

    @staticmethod
    def two_groups_numeric(x: pd.Series, g: pd.Series) -> Dict:
        """
        两组数值比较：自动选择 t检验 或 Mann-Whitney
        """
        x1 = pd.to_numeric(x[g == g.unique()[0]], errors='coerce').dropna()
        x2 = pd.to_numeric(x[g == g.unique()[1]], errors='coerce').dropna()

        if len(x1) < 2 or len(x2) < 2:
            return {'error': '每组至少需2个有效样本'}

        # 正态性判断
        p1 = stats.shapiro(x1).pvalue if len(x1) >= 3 else 0
        p2 = stats.shapiro(x2).pvalue if len(x2) >= 3 else 0
        normal = (p1 > 0.05) and (p2 > 0.05)

        # 方差齐性
        if len(x1) >= 2 and len(x2) >= 2:
            levene_p = stats.levene(x1, x2).pvalue
            equal_var = levene_p > 0.05
        else:
            levene_p = None
            equal_var = True

        if normal and equal_var:
            t_stat, p_val = stats.ttest_ind(x1, x2, equal_var=True)
            test_name = '独立样本t检验'
            # Cohen's d
            pooled_sd = np.sqrt(((len(x1)-1)*x1.std()**2 + (len(x2)-1)*x2.std()**2) / (len(x1)+len(x2)-2))
            effect = (x1.mean() - x2.mean()) / pooled_sd if pooled_sd > 0 else 0
            effect_size = f"Cohen's d = {effect:.3f}"
            ci = InferentialTests._mean_diff_ci(x1, x2, equal_var=True)
        elif normal and not equal_var:
            t_stat, p_val = stats.ttest_ind(x1, x2, equal_var=False)
            test_name = "Welch's t检验"
            pooled_sd = np.sqrt(((len(x1)-1)*x1.std()**2 + (len(x2)-1)*x2.std()**2) / (len(x1)+len(x2)-2))
            effect = (x1.mean() - x2.mean()) / pooled_sd if pooled_sd > 0 else 0
            effect_size = f"Cohen's d = {effect:.3f}"
            ci = InferentialTests._mean_diff_ci(x1, x2, equal_var=False)
        else:
            u_stat, p_val = stats.mannwhitneyu(x1, x2, alternative='two-sided')
            t_stat = u_stat
            test_name = 'Mann-Whitney U检验'
            # r = Z/sqrt(N)
            n = len(x1) + len(x2)
            z = stats.norm.ppf(1 - p_val/2)
            r = z / np.sqrt(n)
            ci = None
            effect_size = f"r = {r:.3f}"
            ci = "95% CI: 不适用（非参数检验）"

        return {
            'test': test_name,
            'group1': {'n': len(x1), 'mean': round(float(x1.mean()), 4), 'sd': round(float(x1.std()), 4),
                       'median': round(float(x1.median()), 4)},
            'group2': {'n': len(x2), 'mean': round(float(x2.mean()), 4), 'sd': round(float(x2.std()), 4),
                       'median': round(float(x2.median()), 4)},
            'statistic': round(float(t_stat), 4),
            'p_value': round(float(p_val), 6),
            'significant': p_val < 0.05,
            'effect_size': effect_size,
            'ci_95': ci,
            'normality_p1': round(float(p1), 4),
            'normality_p2': round(float(p2), 4),
            'levene_p': round(float(levene_p), 4) if levene_p is not None else None,
            'note': f'选择{test_name}: 正态p1={p1:.3f}, p2={p2:.3f}; 方差齐性p={levene_p:.3f}' if levene_p else ''
        }

    @staticmethod
    def multi_groups_numeric(x: pd.Series, g: pd.Series) -> Dict:
        """
        多组(>=3)数值比较：ANOVA 或 Kruskal-Wallis + 事后检验
        """
        groups = sorted(g.unique())
        data = {grp: pd.to_numeric(x[g == grp], errors='coerce').dropna().values for grp in groups}
        data = {k: v for k, v in data.items() if len(v) >= 2}

        if len(data) < 2:
            return {'error': '至少需要2个有效分组'}

        # 正态性 + 方差齐性
        normal = all(stats.shapiro(v).pvalue > 0.05 for v in data.values() if len(v) >= 3)
        if len(data) >= 2:
            levene_p = stats.levene(*data.values()).pvalue
            equal_var = levene_p > 0.05
        else:
            levene_p = 1
            equal_var = True

        result = {
            'groups': {},
            'normality_all_normal': normal,
            'levene_p': round(float(levene_p), 4),
            'equal_var': equal_var
        }

        if normal and equal_var:
            f_stat, p_val = stats.f_oneway(*data.values())
            result.update({
                'test': '单因素方差分析(One-way ANOVA)',
                'statistic': round(float(f_stat), 4),
                'p_value': round(float(p_val), 6),
                'significant': p_val < 0.05,
                'effect_size': f"η² = {InferentialTests._eta_squared(data):.4f}"
            })
        else:
            h_stat, p_val = stats.kruskal(*data.values())
            result.update({
                'test': 'Kruskal-Wallis H检验',
                'statistic': round(float(h_stat), 4),
                'p_value': round(float(p_val), 6),
                'significant': p_val < 0.05,
                'effect_size': f"ε² = {InferentialTests._epsilon_squared(h_stat, data):.4f}"
            })

        for grp, vals in data.items():
            result['groups'][grp] = {
                'n': len(vals),
                'mean': round(float(np.mean(vals)), 4),
                'sd': round(float(np.std(vals, ddof=1)), 4),
                'median': round(float(np.median(vals)), 4),
                'q1': round(float(np.quantile(vals, 0.25)), 4),
                'q3': round(float(np.quantile(vals, 0.75)), 4),
            }

        # 事后检验
        if p_val < 0.05:
            result['post_hoc'] = InferentialTests._post_hoc(data, normal and equal_var)

        return result

    @staticmethod
    def _post_hoc(data: Dict, parametric: bool) -> List[Dict]:
        """事后检验：Tukey或Dunn"""
        rows = []
        groups = list(data.keys())
        for i in range(len(groups)):
            for j in range(i+1, len(groups)):
                g1, g2 = groups[i], groups[j]
                if parametric:
                    # Tukey HSD (简化)
                    t_stat, p_val = stats.ttest_ind(data[g1], data[g2], equal_var=False)
                    method = 'Bonferroni-t'
                else:
                    u_stat, p_val = stats.mannwhitneyu(data[g1], data[g2], alternative='two-sided')
                    method = 'Bonferroni-Mann-Whitney'
                # Bonferroni校正
                n_tests = len(groups) * (len(groups) - 1) / 2
                p_adj = min(p_val * n_tests, 1.0)
                rows.append({
                    'comparison': f'{g1} vs {g2}',
                    'method': method,
                    'p_value': round(float(p_val), 6),
                    'p_adjusted': round(float(p_adj), 6),
                    'significant': p_adj < 0.05
                })
        return rows

    @staticmethod
    def _eta_squared(data: Dict) -> float:
        """ANOVA效应量 η²"""
        all_vals = np.concatenate(list(data.values()))
        grand_mean = np.mean(all_vals)
        ss_between = sum(len(v) * (np.mean(v) - grand_mean)**2 for v in data.values())
        ss_total = np.sum((all_vals - grand_mean)**2)
        return ss_between / ss_total if ss_total > 0 else 0

    @staticmethod
    def _epsilon_squared(H: float, data: Dict) -> float:
        """Kruskal-Wallis效应量 ε²"""
        n = sum(len(v) for v in data.values())
        return H / (n - 1) if n > 1 else 0

    @staticmethod
    def _mean_diff_ci(x1: pd.Series, x2: pd.Series, equal_var: bool = True,
                      confidence: float = 0.95) -> str:
        """均数差 95%CI"""
        n1, n2 = len(x1), len(x2)
        m1, m2 = x1.mean(), x2.mean()
        diff = m1 - m2

        if equal_var:
            df = n1 + n2 - 2
            pooled_se = np.sqrt(((n1-1)*x1.var(ddof=1) + (n2-1)*x2.var(ddof=1)) / df *
                                (1/n1 + 1/n2))
        else:
            df = (x1.var(ddof=1)/n1 + x2.var(ddof=1)/n2)**2 / \
                 ((x1.var(ddof=1)/n1)**2/(n1-1) + (x2.var(ddof=1)/n2)**2/(n2-1))
            pooled_se = np.sqrt(x1.var(ddof=1)/n1 + x2.var(ddof=1)/n2)

        t_crit = stats.t.ppf((1 + confidence) / 2, df)
        ci_low = diff - t_crit * pooled_se
        ci_high = diff + t_crit * pooled_se
        return f"95% CI: ({ci_low:.3f}, {ci_high:.3f})"

    # ---------- 分类变量组间比较 ----------

    @staticmethod
    def categorical_vs_categorical(x: pd.Series, g: pd.Series) -> Dict:
        """
        两/多组分类变量比较：卡方 / Fisher精确
        # 任意单元格期望频数 <5 时使用Fisher
        """
        # 缺失值不能作为分类水平参与检验：
        # astype(str) 会把 NaN 变成 "nan" 字符串，凭空多出一个类别并污染 p 值
        pair = pd.DataFrame({'x': x, 'g': g}).dropna()
        if len(pair) < 4:
            return {'error': '有效样本不足（已剔除缺失值，至少需要 4 条完整记录）'}
        ct = pd.crosstab(pair['x'].astype(str), pair['g'].astype(str))
        if ct.shape[0] < 2 or ct.shape[1] < 2:
            return {'error': '需要至少2x2的列联表'}

        # 期望频数
        chi2, p_val, dof, expected = stats.chi2_contingency(ct)

        # 决定是否使用Fisher
        use_fisher = (expected < 5).any() and ct.shape == (2, 2) and (ct.values < 20).any()

        if use_fisher:
            odds_ratio, fisher_p = stats.fisher_exact(ct.values)
            test_name = 'Fisher精确检验'
            statistic = odds_ratio
            p_final = fisher_p
            ci = InferentialTests._or_ci(ct.values)
        else:
            test_name = 'Pearson卡方检验' if not (expected < 5).any() else '连续校正卡方'
            statistic = chi2
            p_final = p_val
            ci = None

        # Cramér's V (效应量)
        n = ct.values.sum()
        cramers_v = np.sqrt(chi2 / (n * (min(ct.shape) - 1))) if n > 0 and min(ct.shape) > 1 else 0

        # 标准化残差
        std_resid = (ct.values - expected) / np.sqrt(expected)
        cell_details = []
        for i, row_name in enumerate(ct.index):
            for j, col_name in enumerate(ct.columns):
                cell_details.append({
                    'row': row_name,
                    'col': col_name,
                    'observed': int(ct.values[i, j]),
                    'expected': round(float(expected[i, j]), 2),
                    'std_resid': round(float(std_resid[i, j]), 3)
                })

        return {
            'test': test_name,
            'statistic': round(float(statistic), 4),
            'p_value': round(float(p_final), 6),
            'dof': int(dof),
            'significant': p_final < 0.05,
            'effect_size': f"Cramér's V = {cramers_v:.3f}",
            'ci_95': ci,
            'contingency': ct,
            'cell_details': cell_details,
            'expected_min': float(expected.min()),
            'use_fisher': use_fisher
        }

    @staticmethod
    def _or_ci(table: np.ndarray, confidence: float = 0.95) -> Optional[str]:
        """2x2表OR的95%CI（Woolf法）"""
        try:
            a, b = table[0, 0], table[0, 1]
            c, d = table[1, 0], table[1, 1]
            if a == 0 or b == 0 or c == 0 or d == 0:
                # Haldane-Anscombe校正
                a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
            or_val = (a * d) / (b * c)
            se_ln_or = np.sqrt(1/a + 1/b + 1/c + 1/d)
            z = stats.norm.ppf((1 + confidence) / 2)
            ci_low = np.exp(np.log(or_val) - z * se_ln_or)
            ci_high = np.exp(np.log(or_val) + z * se_ln_or)
            return f"OR = {or_val:.3f}, 95% CI: ({ci_low:.3f}, {ci_high:.3f})"
        except Exception:
            return None

    # ---------- 相关分析 ----------

    @staticmethod
    def correlation(x: pd.Series, y: pd.Series, method: str = 'auto') -> Dict:
        """
        相关分析：Pearson / Spearman
        auto时根据正态性选择
        """
        df = pd.DataFrame({'x': pd.to_numeric(x, errors='coerce'),
                          'y': pd.to_numeric(y, errors='coerce')}).dropna()

        if len(df) < 3:
            return {'error': '有效样本不足'}

        if method == 'auto':
            p_x = stats.shapiro(df['x']).pvalue if len(df) >= 3 else 0
            p_y = stats.shapiro(df['y']).pvalue if len(df) >= 3 else 0
            method = 'pearson' if (p_x > 0.05 and p_y > 0.05) else 'spearman'

        if method == 'pearson':
            r, p = stats.pearsonr(df['x'], df['y'])
            test_name = 'Pearson相关'
        else:
            r, p = stats.spearmanr(df['x'], df['y'])
            test_name = 'Spearman秩相关'

        # Fisher变换CI
        if method == 'pearson':
            n = len(df)
            se = 1.0 / np.sqrt(n - 3) if n > 3 else None
            if se:
                z = np.arctanh(r)
                z_crit = stats.norm.ppf(0.975)
                ci_low = np.tanh(z - z_crit * se)
                ci_high = np.tanh(z + z_crit * se)
                ci = f"95% CI: ({ci_low:.3f}, {ci_high:.3f})"
            else:
                ci = None
        else:
            ci = None

        return {
            'test': test_name,
            'n': int(len(df)),
            'r': round(float(r), 4),
            'r_squared': round(float(r**2), 4),
            'p_value': round(float(p), 6),
            'significant': p < 0.05,
            'interpretation': InferentialTests._interpret_r(r),
            'ci_95': ci
        }

    @staticmethod
    def _interpret_r(r: float) -> str:
        """|r|效应量解释"""
        a = abs(r)
        if a < 0.1:
            return '极弱相关'
        elif a < 0.3:
            return '弱相关'
        elif a < 0.5:
            return '中等相关'
        elif a < 0.7:
            return '较强相关'
        else:
            return '强相关'

    # ---------- 配对样本 ----------

    @staticmethod
    def paired_ttest(before: pd.Series, after: pd.Series) -> Dict:
        """配对t检验 / Wilcoxon"""
        df = pd.DataFrame({'b': pd.to_numeric(before), 'a': pd.to_numeric(after)}).dropna()
        if len(df) < 3:
            return {'error': '配对样本不足'}

        diff = df['a'] - df['b']
        p_diff = stats.shapiro(diff).pvalue

        if p_diff > 0.05:
            t_stat, p_val = stats.ttest_rel(df['a'], df['b'])
            return {
                'test': '配对t检验',
                'mean_diff': round(float(diff.mean()), 4),
                'sd_diff': round(float(diff.std(ddof=1)), 4),
                't': round(float(t_stat), 4),
                'p_value': round(float(p_val), 6),
                'significant': p_val < 0.05,
                'effect_size': f"Cohen's d = {diff.mean() / diff.std(ddof=1):.3f}"
            }
        else:
            w_stat, p_val = stats.wilcoxon(df['a'], df['b'])
            return {
                'test': 'Wilcoxon符号秩检验',
                'median_diff': round(float(diff.median()), 4),
                'W': round(float(w_stat), 4),
                'p_value': round(float(p_val), 6),
                'significant': p_val < 0.05
            }

    # ---------- 危险因素 / 关联强度汇总 ----------

    @staticmethod
    def risk_factor_summary(outcome: pd.Series, exposure: pd.Series,
                           confounders: Optional[List[str]] = None,
                           data: Optional[pd.DataFrame] = None) -> Dict:
        """
        暴露-结局关联：粗OR + (可选) 调整OR via Logistic回归
        """
        ct = pd.crosstab(exposure.astype(str), outcome.astype(str))
        if ct.shape != (2, 2):
            return {'error': '需要2x2表（暴露×结局均为二分类）'}

        a, b = ct.iloc[0, 0], ct.iloc[0, 1]
        c, d = ct.iloc[1, 0], ct.iloc[1, 1]
        # 校正
        a2, b2, c2, d2 = a + 0.5, b + 0.5, c + 0.5, d + 0.5
        or_crude = (a2 * d2) / (b2 * c2)
        se = np.sqrt(1/a + 1/b + 1/c + 1/d)
        z = stats.norm.ppf(0.975)
        ci_low = np.exp(np.log(or_crude) - z * se)
        ci_high = np.exp(np.log(or_crude) + z * se)

        # 卡方
        chi2, p_chi, _, _ = stats.chi2_contingency(ct)

        result = {
            'crude_OR': round(float(or_crude), 4),
            'crude_OR_CI': f"({ci_low:.3f}, {ci_high:.3f})",
            'chi2_p': round(float(p_chi), 6),
            'significant': p_chi < 0.05,
            'table': ct.to_dict()
        }

        # 调整OR
        if data is not None and confounders and len(confounders) > 0:
            try:
                import statsmodels.api as sm
                cols = [exposure.name] + confounders
                sub = data[cols + [outcome.name]].dropna().copy()
                # 转换
                for c in [outcome.name, exposure.name] + confounders:
                    if sub[c].nunique() == 2:
                        sub[c] = (pd.to_numeric(sub[c], errors='coerce') == sub[c].dropna().unique()[1]).astype(int)
                    else:
                        sub[c] = pd.to_numeric(sub[c], errors='coerce')
                sub = sub.dropna()
                if len(sub) > len(cols) + 5:
                    X = sm.add_constant(sub[cols])
                    y = sub[outcome.name]
                    model = sm.Logit(y, X).fit(disp=0, maxiter=100)
                    coef = model.params[exposure.name]
                    se_adj = model.bse[exposure.name]
                    p_adj = model.pvalues[exposure.name]
                    or_adj = np.exp(coef)
                    ci_adj = (np.exp(coef - 1.96 * se_adj), np.exp(coef + 1.96 * se_adj))
                    result['adjusted_OR'] = round(float(or_adj), 4)
                    result['adjusted_OR_CI'] = f"({ci_adj[0]:.3f}, {ci_adj[1]:.3f})"
                    result['adjusted_p'] = round(float(p_adj), 6)
                    result['nobs'] = int(model.nobs)
            except Exception as e:
                result['adjusted_error'] = str(e)

        return result