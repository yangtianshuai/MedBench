"""
科研论文辅助模块
负责：PSM倾向性评分匹配、漏斗图、亚组分析森林图、敏感性分析
"""
import pandas as pd
import numpy as np
from scipy import stats
import statsmodels.api as sm
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import NearestNeighbors
from typing import Dict, List, Optional, Tuple
import matplotlib.pyplot as plt
import matplotlib
matplotlib.rcParams['font.sans-serif'] = [
    'SimHei', 'Microsoft YaHei',
    'WenQuanYi Zen Hei', 'WenQuanYi Micro Hei',
    'Noto Sans CJK SC', 'Noto Sans SC', 'Source Han Sans SC',
    'Heiti SC', 'PingFang SC', 'Arial Unicode MS', 'DejaVu Sans'
]
matplotlib.rcParams['axes.unicode_minus'] = False
import io
import warnings
warnings.filterwarnings('ignore')

PRIMARY = '#3B82F6'
DANGER = '#EF4444'
SUCCESS = '#10B981'


class ResearchAssistant:
    """科研论文辅助分析"""

    # ---------- 倾向性评分匹配 (PSM) ----------

    @staticmethod
    def propensity_score_matching(df: pd.DataFrame, treatment: str, outcome: str,
                                   covariates: List[str], caliper: float = 0.2,
                                   ratio: int = 1, random_state: int = 42) -> Dict:
        """
        倾向性评分匹配
        使用1:k最近邻匹配 + 卡尺
        """
        cols = [treatment, outcome] + covariates
        sub = df[cols].copy()

        # 数值化（PSM内强制连续化）
        for c in covariates:
            if sub[c].dtype == 'object' or sub[c].dtype.name == 'category':
                # 哑变量展开
                dummies = pd.get_dummies(sub[c], prefix=c, drop_first=True, dummy_na=False)
                sub = pd.concat([sub.drop(columns=[c]), dummies.astype(float)], axis=1)
                cols = [treatment, outcome] + [c2 for c2 in sub.columns if c2 not in (treatment, outcome)]
            else:
                sub[c] = pd.to_numeric(sub[c], errors='coerce')

        sub = sub.dropna()
        covariates_now = [c for c in sub.columns if c not in (treatment, outcome)]

        treated = sub[sub[treatment] == 1]
        control = sub[sub[treatment] == 0]

        if len(treated) < 5 or len(control) < 5:
            return {'error': f'处理组或对照组样本不足 (T={len(treated)}, C={len(control)})'}

        # 估计PS
        X = sub[covariates_now]
        y = sub[treatment]

        lr = LogisticRegression(max_iter=1000, random_state=random_state)
        lr.fit(X, y)
        ps = lr.predict_proba(X)[:, 1]

        sub = sub.assign(ps=ps)

        # 评估PS模型
        try:
            auc_ps = statsmodels_roc_auc(y, ps)
        except Exception:
            auc_ps = None

        # 匹配
        treated_idx = sub[sub[treatment] == 1].index.values
        control_idx = sub[sub[treatment] == 0].index.values

        ps_treated = sub.loc[treated_idx, 'ps'].values.reshape(-1, 1)
        ps_control = sub.loc[control_idx, 'ps'].values.reshape(-1, 1)

        nn = NearestNeighbors(n_neighbors=ratio, radius=caliper)
        nn.fit(ps_control)
        distances, indices = nn.kneighbors(ps_treated)

        # 应用卡尺
        matched_pairs = []
        used_control = set()
        for i, t_idx in enumerate(treated_idx):
            for j in range(ratio):
                if distances[i, j] <= caliper:
                    c_idx = control_idx[indices[i, j]]
                    if c_idx not in used_control:
                        matched_pairs.append((t_idx, c_idx))
                        used_control.add(c_idx)
                        break

        if len(matched_pairs) < 5:
            return {'error': f'匹配后样本过少 (n={len(matched_pairs)}), 请放宽卡尺或减少协变量'}

        # 构建匹配后数据集
        matched_indices = []
        for t_idx, c_idx in matched_pairs:
            matched_indices.extend([t_idx, c_idx])
        matched_df = sub.loc[matched_indices].copy()

        # SMD 评估平衡
        covariates_now_list = [c for c in sub.columns if c not in (treatment, outcome, 'ps')]
        smd_before, smd_after = ResearchAssistant._smd_table(sub, matched_df, treatment, covariates_now_list)

        # 匹配后效应估计
        result = ResearchAssistant._outcome_analysis_matched(matched_df, treatment, outcome)

        # 可视化
        ps_plot = ResearchAssistant._ps_overlap_plot(sub, treatment, '匹配前PS分布')
        ps_after_plot = ResearchAssistant._ps_overlap_plot(matched_df, treatment, '匹配后PS分布')

        # Love plot
        love_plot = ResearchAssistant._love_plot(smd_before, smd_after)

        return {
            'n_treated_original': int(len(treated)),
            'n_control_original': int(len(control)),
            'n_matched_pairs': int(len(matched_pairs)),
            'n_matched_total': int(len(matched_df)),
            'ps_model_auc': round(float(auc_ps), 4) if auc_ps else None,
            'caliper': caliper,
            'ratio': ratio,
            'smd_before': smd_before,
            'smd_after': smd_after,
            'all_balanced_after': all(abs(s['smd_after']) < 0.1 for s in smd_before),
            'outcome_analysis': result,
            'ps_plot_before': ps_plot,
            'ps_plot_after': ps_after_plot,
            'love_plot': love_plot
        }

    @staticmethod
    def _smd_table(df_before: pd.DataFrame, df_after: pd.DataFrame,
                   treatment: str, covariates: List[str]) -> Tuple[List[Dict], List[Dict]]:
        """计算SMD（标准化均数差）"""
        smd_before, smd_after = [], []
        for cov in covariates:
            t_b = df_before[df_before[treatment] == 1][cov]
            c_b = df_before[df_before[treatment] == 0][cov]
            t_a = df_after[df_after[treatment] == 1][cov]
            c_a = df_after[df_after[treatment] == 0][cov]

            def smd(t, c):
                if len(t) < 2 or len(c) < 2:
                    return 0
                pooled_sd = np.sqrt((t.var(ddof=1) + c.var(ddof=1)) / 2)
                if pooled_sd == 0:
                    return 0
                return (t.mean() - c.mean()) / pooled_sd

            s_b = smd(t_b, c_b)
            s_a = smd(t_a, c_a)
            smd_before.append({'variable': cov, 'smd_before': round(float(s_b), 4), 'smd_after': round(float(s_a), 4)})
            smd_after.append(round(float(s_a), 4))
        return smd_before, smd_after

    @staticmethod
    def _outcome_analysis_matched(matched_df: pd.DataFrame, treatment: str, outcome: str) -> Dict:
        """匹配后效应估计"""
        # 配对检验
        if matched_df[outcome].nunique() == 2:
            # 二分类结局：McNemar + 条件Logistic
            t_out = matched_df[matched_df[treatment] == 1][outcome]
            c_out = matched_df[matched_df[treatment] == 0][outcome]
            n_t = len(t_out)

            # McNemar 配对卡方
            both_1 = ((t_out == 1) & (c_out == 1)).sum()
            t1_c0 = ((t_out == 1) & (c_out == 0)).sum()
            t0_c1 = ((t_out == 0) & (c_out == 1)).sum()
            both_0 = ((t_out == 0) & (c_out == 0)).sum()

            if t1_c0 + t0_c1 > 0:
                # 连续校正
                mcnemar = (abs(t1_c0 - t0_c1) - 1)**2 / (t1_c0 + t0_c1)
                p_val = 1 - stats.chi2.cdf(mcnemar, 1)
            else:
                mcnemar, p_val = 0, 1

            # 粗OR
            a, b = both_1, t1_c0
            c, d = t0_c1, both_0
            a2, b2, c2, d2 = a + 0.5, b + 0.5, c + 0.5, d + 0.5
            or_val = (a2 * d2) / (b2 * c2)
            se = np.sqrt(1/a + 1/b + 1/c + 1/d) if min(a, b, c, d) > 0 else 0
            ci = (np.exp(np.log(or_val) - 1.96*se), np.exp(np.log(or_val) + 1.96*se)) if se > 0 else (None, None)

            return {
                'type': 'binary',
                'mcnemar_chi2': round(float(mcnemar), 4),
                'p_value': round(float(p_val), 6),
                'OR': round(float(or_val), 4),
                'OR_CI': f"({ci[0]:.3f}, {ci[1]:.3f})" if ci[0] else None,
                'paired_table': {
                    '处理=1&结局=1': int(both_1),
                    '处理=1&结局=0': int(t1_c0),
                    '处理=0&结局=1': int(t0_c1),
                    '处理=0&结局=0': int(both_0)
                }
            }
        else:
            # 连续结局：配对t检验
            t_out = matched_df[matched_df[treatment] == 1][outcome]
            c_out = matched_df[matched_df[treatment] == 0][outcome]
            t_stat, p_val = stats.ttest_rel(t_out.values, c_out.values)
            return {
                'type': 'continuous',
                't': round(float(t_stat), 4),
                'p_value': round(float(p_val), 6),
                'mean_diff': round(float(t_out.mean() - c_out.mean()), 4)
            }

    @staticmethod
    def _ps_overlap_plot(df: pd.DataFrame, treatment: str, title: str) -> bytes:
        """PS分布重叠图"""
        fig, ax = plt.subplots(figsize=(8, 5), dpi=100)

        ps_t = df[df[treatment] == 1]['ps']
        ps_c = df[df[treatment] == 0]['ps']

        ax.hist(ps_c, bins=30, alpha=0.5, color=PRIMARY, label=f'对照 (n={len(ps_c)})', edgecolor='white')
        ax.hist(ps_t, bins=30, alpha=0.5, color=DANGER, label=f'处理 (n={len(ps_t)})', edgecolor='white')

        ax.set_xlabel('倾向性评分')
        ax.set_ylabel('频数')
        ax.set_title(title, fontsize=13, fontweight='bold')
        ax.legend()
        ax.grid(alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def _love_plot(smd_table: List[Dict], title: str = 'Love Plot - 协变量平衡') -> bytes:
        """Love plot：匹配前后协变量平衡"""
        variables = [s['variable'] for s in smd_table]
        before = [abs(s['smd_before']) for s in smd_table]
        after = [abs(s['smd_after']) for s in smd_table]

        fig, ax = plt.subplots(figsize=(8, max(3, len(variables)*0.4)), dpi=100)
        y = np.arange(len(variables))

        ax.scatter(before, y, color=DANGER, s=120, label='匹配前', marker='o', zorder=3)
        ax.scatter(after, y, color=PRIMARY, s=120, label='匹配后', marker='s', zorder=3)

        # 连接线
        for i in range(len(variables)):
            ax.plot([before[i], after[i]], [i, i], color='gray', alpha=0.4, linewidth=1)

        # 阈值线
        ax.axvline(0.1, color='green', linestyle='--', alpha=0.6, label='|SMD|=0.1')
        ax.axvline(-0.1, color='green', linestyle='--', alpha=0.6)

        ax.set_yticks(y)
        ax.set_yticklabels(variables)
        ax.set_xlabel('|SMD|')
        ax.set_title(title, fontsize=13, fontweight='bold')
        ax.legend(loc='lower right')
        ax.grid(axis='x', alpha=0.3)
        ax.invert_yaxis()
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    # ---------- 亚组分析（森林图） ----------

    @staticmethod
    def subgroup_analysis(df: pd.DataFrame, outcome: str, treatment: str,
                          subgroup_var: str, covariates: Optional[List[str]] = None) -> Dict:
        """
        亚组分析 + 森林图
        每个亚组内估计处理效应，并检验交互作用
        """
        if df[subgroup_var].nunique() > 10:
            return {'error': '亚组变量类别过多(>10)'}

        cols = [outcome, treatment, subgroup_var]
        if covariates:
            cols += covariates
        sub = df[cols].dropna().copy()

        results = []
        for grp in sorted(sub[subgroup_var].unique()):
            grp_df = sub[sub[subgroup_var] == grp]
            if len(grp_df) < 10 or grp_df[treatment].nunique() < 2:
                continue

            if covariates:
                # 调整Logistic
                try:
                    X = sm.add_constant(grp_df[[treatment] + covariates].astype(float))
                    y = grp_df[outcome].astype(int)
                    if y.value_counts().min() < 3:
                        continue
                    model = sm.Logit(y, X).fit(disp=0, maxiter=200)
                    coef = model.params[treatment]
                    se = model.bse[treatment]
                    p = model.pvalues[treatment]
                except Exception:
                    continue
            else:
                # 粗OR
                if grp_df[outcome].nunique() < 2:
                    continue
                ct = pd.crosstab(grp_df[treatment], grp_df[outcome])
                if ct.shape != (2, 2):
                    continue
                a, b = ct.iloc[1, 1], ct.iloc[1, 0]
                c, d = ct.iloc[0, 1], ct.iloc[0, 0]
                a2, b2, c2, d2 = a + 0.5, b + 0.5, c + 0.5, d + 0.5
                coef = np.log((a2 * d2) / (b2 * c2))
                se = np.sqrt(1/a + 1/b + 1/c + 1/d)
                p = 2 * (1 - stats.norm.cdf(abs(coef/se)))

            or_val = np.exp(coef)
            ci_low = np.exp(coef - 1.96 * se)
            ci_high = np.exp(coef + 1.96 * se)

            results.append({
                'subgroup': str(grp),
                'n': int(len(grp_df)),
                'n_treatment': int((grp_df[treatment] == 1).sum()),
                'OR': round(float(or_val), 4),
                'CI_low': round(float(ci_low), 4),
                'CI_high': round(float(ci_high), 4),
                'p_value': round(float(p), 4),
                'significant': p < 0.05
            })

        # 交互检验
        interaction_p = None
        if covariates is None and len(results) >= 2:
            try:
                # 在整体数据中加交互项
                X = sm.add_constant(sub[[treatment, subgroup_var]].astype(float))
                X['interaction'] = X[treatment] * X[subgroup_var]
                y = sub[outcome].astype(int)
                if y.value_counts().min() >= 3:
                    model = sm.Logit(y, X).fit(disp=0, maxiter=200)
                    interaction_p = float(model.pvalues.get('interaction', 1))
            except Exception:
                pass

        # 森林图
        forest_plot = ResearchAssistant._forest_plot(results, interaction_p, subgroup_var)

        return {
            'subgroup_var': subgroup_var,
            'results': results,
            'interaction_p': round(interaction_p, 4) if interaction_p is not None else None,
            'interaction_significant': interaction_p < 0.05 if interaction_p is not None else None,
            'forest_plot': forest_plot
        }

    @staticmethod
    def _forest_plot(results: List[Dict], interaction_p: Optional[float], subgroup_var: str) -> bytes:
        """森林图"""
        if not results:
            return b''

        fig, ax = plt.subplots(figsize=(8, max(4, len(results)*0.6)), dpi=100)

        y_pos = np.arange(len(results))
        for i, r in enumerate(results):
            x = r['OR']
            xerr_low = x - r['CI_low']
            xerr_high = r['CI_high'] - x
            ax.errorbar(x, i, xerr=[[xerr_low], [xerr_high]], fmt='s',
                       color=PRIMARY, ecolor='gray', elinewidth=2, capsize=4, markersize=10)

        ax.axvline(1, color='black', linestyle='--', alpha=0.5)
        ax.set_yticks(y_pos)
        ax.set_yticklabels([f"{r['subgroup']} (n={r['n']})" for r in results], fontsize=10)
        ax.set_xlabel(f'{subgroup_var} 各亚组的 OR (95% CI)')
        ax.set_title(f'亚组分析森林图{f" - 交互p={interaction_p:.3f}" if interaction_p else ""}',
                    fontsize=13, fontweight='bold')

        # 右侧标注
        for i, r in enumerate(results):
            ax.text(max(r['CI_high'] for r in results) * 1.1, i,
                    f"OR={r['OR']} ({r['CI_low']}-{r['CI_high']}) p={r['p_value']}",
                    va='center', fontsize=9, family='monospace')

        ax.grid(axis='x', alpha=0.3)
        ax.invert_yaxis()
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    # ---------- 发表偏倚漏斗图 ----------

    @staticmethod
    def funnel_plot(effect_sizes: List[float], standard_errors: List[float],
                    title: str = '漏斗图') -> bytes:
        """Egger漏斗图 + Egger检验"""
        es = np.array(effect_sizes)
        se = np.array(standard_errors)

        fig, ax = plt.subplots(figsize=(8, 6), dpi=100)
        ax.scatter(es, se, color=PRIMARY, s=60, alpha=0.6, edgecolor='white')

        # 中心线（合并效应）
        mean_es = es.mean()
        ax.axvline(mean_es, color=DANGER, linestyle='--', label=f'合并效应={mean_es:.3f}')

        # 95% CI线
        ax.plot([mean_es - 1.96*se, mean_es + 1.96*se], se, '--', color='gray', alpha=0.5)

        # 倒序
        ax.invert_yaxis()
        ax.set_xlabel('效应量')
        ax.set_ylabel('标准误')
        ax.set_title(title, fontsize=13, fontweight='bold')
        ax.legend()
        ax.grid(alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)

        # Egger检验
        try:
            slope, intercept, r, p, se_slope = stats.linregress(se, es)
            egger_result = {
                'intercept': round(float(intercept), 4),
                'p_value': round(float(p), 4),
                'significant': p < 0.05,
                'interpretation': '存在发表偏倚' if p < 0.05 else '无明显发表偏倚'
            }
        except Exception:
            egger_result = None

        return {'plot': buf.getvalue(), 'egger': egger_result}

    # ---------- 敏感性分析：E-value ----------

    @staticmethod
    def e_value(observed_rr: float) -> Dict:
        """
        E-value: 评估未测量混杂对观察到的RR/OR的解释能力
        E-value ≥ 1 + sqrt(RR × (RR-1)) 表明稳健
        """
        if observed_rr < 1:
            observed_rr = 1 / observed_rr
        e_val = observed_rr + np.sqrt(observed_rr * (observed_rr - 1))
        return {
            'observed_RR': round(float(observed_rr), 4),
            'E_value': round(float(e_val), 4),
            'interpretation': f"需要强度为 {e_val:.2f} 的未测量混杂才能完全解释观察到的关联"
        }


def statsmodels_roc_auc(y_true, y_score) -> float:
    """简单AUC计算"""
    from sklearn.metrics import roc_auc_score
    return roc_auc_score(y_true, y_score)