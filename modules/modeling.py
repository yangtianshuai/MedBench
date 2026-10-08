"""
预测建模模块
负责：Logistic回归 / Cox回归 / 列线图 / 决策曲线(DCA) / 校准曲线 / ROC
"""
import pandas as pd
import numpy as np
from scipy import stats
import statsmodels.api as sm
from sklearn.metrics import roc_auc_score, roc_curve, confusion_matrix, classification_report
from sklearn.model_selection import train_test_split
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


class PredictiveModeling:
    """预测建模"""

    # ---------- Logistic回归 ----------

    @staticmethod
    def logistic_regression(df: pd.DataFrame, outcome: str,
                           predictors: List[str], test_size: float = 0.3,
                           random_state: int = 42) -> Dict:
        """
        二分类Logistic回归：自动产生OR、95%CI、p值、VIF、ROC、Calibration
        """
        cols = [outcome] + predictors
        sub = df[cols].copy()

        # 类型转换
        for col in cols:
            if sub[col].dtype == 'object' or sub[col].dtype.name == 'category':
                # 哑变量
                pass
            else:
                sub[col] = pd.to_numeric(sub[col], errors='coerce')

        # 结局二值化
        if sub[outcome].nunique() != 2:
            return {'error': '结局变量必须是二分类'}

        # 哑变量展开
        cat_cols = [c for c in predictors if sub[c].nunique() <= 5 and sub[c].dtype == 'object']
        if cat_cols:
            sub = pd.get_dummies(sub, columns=cat_cols, drop_first=True, dummy_na=False)
            predictors_dum = [c for c in sub.columns if c != outcome]
        else:
            predictors_dum = predictors

        sub = sub.dropna()
        if len(sub) < len(predictors_dum) + 10:
            return {'error': f'有效样本不足 (n={len(sub)}, 变量数={len(predictors_dum)})'}

        X = sub[predictors_dum].astype(float)
        y = sub[outcome].astype(int)

        # 单变量min做因变量为1类
        if y.value_counts().min() < 5:
            return {'error': '某一类别样本数<5，无法建模'}

        X_const = sm.add_constant(X)

        # 拟合
        try:
            model = sm.Logit(y, X_const).fit(disp=0, maxiter=200)
        except Exception as e:
            return {'error': f'模型拟合失败: {str(e)}'}

        # 提取系数
        params = model.params
        bse = model.bse
        pvals = model.pvalues
        conf = model.conf_int()

        coef_table = []
        for var in X_const.columns:
            coef_table.append({
                'variable': var,
                'coef': round(float(params[var]), 4),
                'std_err': round(float(bse[var]), 4),
                'z': round(float(params[var] / bse[var]) if bse[var] > 0 else 0, 4),
                'OR': round(float(np.exp(params[var])), 4),
                'OR_CI_low': round(float(np.exp(conf.loc[var, 0])), 4),
                'OR_CI_high': round(float(np.exp(conf.loc[var, 1])), 4),
                'p_value': float(pvals[var]),
                'significant': pvals[var] < 0.05
            })

        # 训练/测试划分
        if test_size > 0 and len(sub) > 30:
            try:
                X_train, X_test, y_train, y_test = train_test_split(
                    X_const, y, test_size=test_size, random_state=random_state,
                    stratify=y if y.value_counts().min() >= 5 else None
                )
                model_train = sm.Logit(y_train, X_train).fit(disp=0, maxiter=200)
                y_pred_train = model_train.predict(X_train)
                y_pred_test = model_train.predict(X_test)

                auc_train = roc_auc_score(y_train, y_pred_train)
                auc_test = roc_auc_score(y_test, y_pred_test) if len(y_test.unique()) > 1 else None

                # 混淆矩阵
                y_pred_class = (y_pred_test >= 0.5).astype(int)
                cm = confusion_matrix(y_test, y_pred_class)

                train_metrics = {
                    'auc': round(float(auc_train), 4),
                    'n': int(len(y_train))
                }
                test_metrics = {
                    'auc': round(float(auc_test), 4) if auc_test is not None else None,
                    'n': int(len(y_test)),
                    'confusion_matrix': cm.tolist(),
                    'classification_report': classification_report(y_test, y_pred_class, output_dict=True, zero_division=0)
                }
            except Exception as e:
                train_metrics = None
                test_metrics = {'error': str(e)}
        else:
            train_metrics = None
            test_metrics = None

        # VIF
        try:
            from statsmodels.stats.outliers_influence import variance_inflation_factor
            vif_data = []
            for i, col in enumerate(X.columns):
                vif_val = variance_inflation_factor(X_const.values, i + 1)  # +1 跳过常数项
                vif_data.append({'variable': col, 'VIF': round(float(vif_val), 2)})
        except Exception:
            vif_data = []

        # 整体评价
        ll = model.llf
        aic = model.aic
        bic = model.bic
        pseudo_r2 = model.prsquared

        # Hosmer-Lemeshow
        try:
            hl = PredictiveModeling._hosmer_lemeshow_test(y, model.predict(X_const))
        except Exception:
            hl = None

        # ROC曲线
        y_pred_full = model.predict(X_const)
        roc_plot = PredictiveModeling._roc_plot(y.values, y_pred_full.values, 'ROC（全样本预测）')

        # 校准曲线
        cal_plot = PredictiveModeling._calibration_plot(y.values, y_pred_full.values, '校准曲线 (训练集)')

        # DCA
        dca_plot = PredictiveModeling._dca_curve(y.values, y_pred_full.values, '决策曲线分析')

        # 完全分离检测：某个自变量能完美区分结局时，系数与标准误会异常膨胀，
        # OR 会变成 0 或 +inf 且 p 值失真，必须显式告知使用者而不是照常输出
        separation_vars = [
            row['variable'] for row in coef_table
            if row['variable'] != 'const' and (abs(row['coef']) > 10 or row['std_err'] > 10)
        ]

        return {
            'n': int(len(sub)),
            'n_dropped': int(len(df) - len(sub)),
            'separation_vars': separation_vars,
            'predictors': predictors_dum,
            'coef_table': coef_table,
            'pseudo_r2': round(float(pseudo_r2), 4),
            'log_likelihood': round(float(ll), 2),
            'AIC': round(float(aic), 2),
            'BIC': round(float(bic), 2),
            'hosmer_lemeshow': hl,
            'vif': vif_data,
            'train_metrics': train_metrics,
            'test_metrics': test_metrics,
            'roc_plot': roc_plot,
            'calibration_plot': cal_plot,
            'dca_plot': dca_plot,
            'cutoff': PredictiveModeling.cutoff_analysis(y, y_pred_full),
            'model_summary': str(model.summary())
        }

    @staticmethod
    def _hosmer_lemeshow_test(y_true: pd.Series, y_pred: pd.Series, g: int = 10) -> Dict:
        """Hosmer-Lemeshow拟合优度"""
        data = pd.DataFrame({'y': y_true.values, 'p': y_pred.values})
        data['decile'] = pd.qcut(data['p'], g, duplicates='drop')
        obs = data.groupby('decile')['y'].sum()
        exp = data.groupby('decile')['p'].sum()
        n_g = data.groupby('decile')['y'].count()
        chi2 = (((obs - exp)**2 / (exp * (1 - exp/n_g))).sum())
        dof = len(obs) - 2
        p = 1 - stats.chi2.cdf(chi2, dof)
        return {
            'chi2': round(float(chi2), 4),
            'dof': int(dof),
            'p_value': round(float(p), 4),
            'good_fit': p > 0.05
        }

    @staticmethod
    def _roc_plot(y_true: np.ndarray, y_score: np.ndarray, title: str = 'ROC') -> bytes:
        """ROC曲线"""
        if len(np.unique(y_true)) < 2:
            return b''
        fpr, tpr, _ = roc_curve(y_true, y_score)
        auc = roc_auc_score(y_true, y_score)

        fig, ax = plt.subplots(figsize=(7, 6), dpi=100)
        ax.plot(fpr, tpr, color=PRIMARY, linewidth=2.5, label=f'AUC = {auc:.3f}')
        ax.plot([0, 1], [0, 1], 'k--', alpha=0.4, linewidth=1)
        ax.fill_between(fpr, tpr, alpha=0.1, color=PRIMARY)

        # 最佳截点（Youden）
        youden = tpr - fpr
        best_idx = np.argmax(youden)
        ax.scatter(fpr[best_idx], tpr[best_idx], color=DANGER, s=120, zorder=5,
                   label=f'最佳截点 ({fpr[best_idx]:.2f}, {tpr[best_idx]:.2f})')

        ax.set_xlim([-0.02, 1.02])
        ax.set_ylim([-0.02, 1.02])
        ax.set_xlabel('1 - 特异度')
        ax.set_ylabel('灵敏度')
        ax.set_title(title, fontsize=13, fontweight='bold')
        ax.legend(loc='lower right', fontsize=10)
        ax.grid(alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def cutoff_analysis(y_true, y_score, max_table_rows: int = 80) -> Dict:
        """
        诊断试验截断值分析。

        给出三种常用「最佳截断值」判据（约登指数最大 / 灵敏度≈特异度 / 离左上角最近），
        以及每个阈值对应的灵敏度、特异度、预测值、似然比等完整指标表。
        """
        labels = pd.Series(y_true)
        if labels.dtype == object:
            uniques = labels.dropna().unique()
            if len(uniques) == 2:
                labels = (labels == uniques[1]).astype(int)
        labels = pd.to_numeric(labels, errors='coerce')
        scores = pd.to_numeric(pd.Series(y_score), errors='coerce')
        frame = pd.DataFrame({'y': labels, 'p': scores}).dropna()
        if frame['y'].nunique() < 2:
            return {'error': '结局变量只有一类，无法做诊断分析'}
        y = frame['y'].to_numpy().astype(int)
        probability = frame['p'].to_numpy(dtype=float)

        fpr, tpr, thresholds = roc_curve(y, probability)
        youden = tpr - fpr
        distance = np.sqrt((1 - tpr) ** 2 + fpr ** 2)
        balance = np.abs(tpr - (1 - fpr))

        def metrics_at(index: int) -> Dict:
            threshold = float(thresholds[index])
            predicted = (probability >= threshold).astype(int)
            tp = int(((predicted == 1) & (y == 1)).sum())
            fp = int(((predicted == 1) & (y == 0)).sum())
            fn = int(((predicted == 0) & (y == 1)).sum())
            tn = int(((predicted == 0) & (y == 0)).sum())
            sensitivity = tp / (tp + fn) if (tp + fn) else float('nan')
            specificity = tn / (tn + fp) if (tn + fp) else float('nan')
            ppv = tp / (tp + fp) if (tp + fp) else float('nan')
            npv = tn / (tn + fn) if (tn + fn) else float('nan')
            accuracy = (tp + tn) / len(y)
            plr = sensitivity / (1 - specificity) if specificity < 1 else float('inf')
            nlr = (1 - sensitivity) / specificity if specificity > 0 else float('inf')
            f1 = (2 * ppv * sensitivity / (ppv + sensitivity)
                  if (ppv + sensitivity) > 0 else float('nan'))
            return {
                '阈值': round(threshold, 4),
                '灵敏度': round(float(sensitivity), 4),
                '特异度': round(float(specificity), 4),
                '约登指数': round(float(sensitivity + specificity - 1), 4),
                '阳性预测值': round(float(ppv), 4),
                '阴性预测值': round(float(npv), 4),
                '准确率': round(float(accuracy), 4),
                '阳性似然比': (round(float(plr), 3) if np.isfinite(plr) else '∞'),
                '阴性似然比': round(float(nlr), 3),
                'F1': round(float(f1), 4),
                'TP': tp, 'FP': fp, 'FN': fn, 'TN': tn,
            }

        candidates = {
            '约登指数最大': int(np.argmax(youden)),
            '灵敏度≈特异度（平衡点）': int(np.argmin(balance)),
            '离左上角最近': int(np.argmin(distance)),
        }

        rows = []
        for label, index in candidates.items():
            rows.append({'判据': label, **metrics_at(index)})
        summary_table = pd.DataFrame(rows)

        all_rows = [metrics_at(index) for index in range(len(thresholds))]
        threshold_table = pd.DataFrame(all_rows)
        sampled = False
        if len(threshold_table) > max_table_rows:
            step = max(1, len(threshold_table) // max_table_rows)
            keep_index = set(range(0, len(threshold_table), step)) | set(candidates.values())
            threshold_table = threshold_table.iloc[sorted(keep_index)].reset_index(drop=True)
            sampled = True

        auc = float(roc_auc_score(y, probability))
        best = metrics_at(candidates['约登指数最大'])
        return {
            'summary_table': summary_table,
            'threshold_table': threshold_table,
            'best': dict(best),
            'auc': auc,
            'n': int(len(y)),
            'n_events': int(y.sum()),
            'sampled': sampled,
            'plot': PredictiveModeling._cutoff_plot(y, probability, fpr, tpr, thresholds, candidates),
            'display': (f'AUC={auc:.3f}（n={len(y)}，阳性 {int(y.sum())} 例）；'
                        f'约登指数最大处：阈值 {best["阈值"]}，'
                        f'灵敏度 {best["灵敏度"]:.3f}、特异度 {best["特异度"]:.3f}、'
                        f'约登指数 {best["约登指数"]:.3f}')
        }

    @staticmethod
    def _cutoff_plot(y_true, probability, fpr, tpr, thresholds, candidates) -> bytes:
        """左图 ROC 上标注三种候选截断点，右图阈值—灵敏度/特异度/约登曲线。"""
        fig, axes = plt.subplots(1, 2, figsize=(13, 5.6), dpi=110)

        ax = axes[0]
        ax.plot(fpr, tpr, color=PRIMARY, linewidth=2.4, label='ROC 曲线')
        ax.plot([0, 1], [0, 1], 'k--', alpha=0.4, linewidth=1)
        colors = {'约登指数最大': DANGER, '灵敏度≈特异度（平衡点）': '#2563EB',
                  '离左上角最近': '#16A34A'}
        for label, index in candidates.items():
            ax.scatter(fpr[index], tpr[index], s=110, zorder=5,
                       color=colors.get(label, '#7C3AED'), label=label)
        ax.set_xlabel('1 - 特异度')
        ax.set_ylabel('灵敏度')
        ax.set_title('ROC 与候选截断点', fontsize=12, fontweight='bold')
        ax.legend(loc='lower right', fontsize=9)
        ax.grid(alpha=0.3)

        ax2 = axes[1]
        ax2.plot(thresholds, tpr, color=PRIMARY, linewidth=2, label='灵敏度')
        ax2.plot(thresholds, 1 - fpr, color='#16A34A', linewidth=2, label='特异度')
        ax2.plot(thresholds, tpr - fpr, color=DANGER, linewidth=1.6, linestyle='--',
                 label='约登指数')
        for label, index in candidates.items():
            ax2.axvline(thresholds[index], color=colors.get(label, '#7C3AED'),
                        linestyle=':', alpha=0.7, linewidth=1.2)
        ax2.set_xlabel('判定阈值')
        ax2.set_ylabel('指标值')
        ax2.set_ylim(-0.05, 1.05)
        ax2.set_title('阈值—灵敏度/特异度/约登指数', fontsize=12, fontweight='bold')
        ax2.legend(fontsize=9)
        ax2.grid(alpha=0.3)

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def _calibration_plot(y_true: np.ndarray, y_pred: np.ndarray, title: str = '校准曲线', n_bins: int = 10) -> bytes:
        """校准曲线"""
        data = pd.DataFrame({'y': y_true, 'p': y_pred})
        data['bin'] = pd.qcut(data['p'], n_bins, duplicates='drop')

        grouped = data.groupby('bin').agg({'y': 'mean', 'p': 'mean'}).reset_index()

        fig, ax = plt.subplots(figsize=(7, 6), dpi=100)
        ax.plot([0, 1], [0, 1], 'k--', alpha=0.5, label='理想曲线')
        ax.plot(grouped['p'], grouped['y'], 'o-', color=PRIMARY, linewidth=2.5,
                markersize=8, label='实际模型')

        ax.set_xlabel('预测概率')
        ax.set_ylabel('实际发生率')
        ax.set_title(title, fontsize=13, fontweight='bold')
        ax.legend(loc='upper left')
        ax.grid(alpha=0.3)
        ax.set_xlim([-0.02, 1.02])
        ax.set_ylim([-0.02, 1.02])
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    @staticmethod
    def _dca_curve(y_true: np.ndarray, y_pred: np.ndarray, title: str = '决策曲线') -> bytes:
        """
        Decision Curve Analysis (DCA)
        净收益 = TP/N - FP/N × (pt/(1-pt))
        """
        thresholds = np.linspace(0.01, 0.99, 99)
        net_benefits = []

        for pt in thresholds:
            y_pred_class = (y_pred >= pt).astype(int)
            tp = np.sum((y_pred_class == 1) & (y_true == 1))
            fp = np.sum((y_pred_class == 1) & (y_true == 0))
            n = len(y_true)
            nb = tp/n - fp/n * (pt / (1 - pt))
            net_benefits.append(nb)

        # 全部治疗基准
        all_treat = y_true.mean() - (1 - y_true.mean()) * (thresholds / (1 - thresholds))
        # 全不治疗基准
        none_treat = np.zeros_like(thresholds)

        fig, ax = plt.subplots(figsize=(8, 5.5), dpi=100)
        ax.plot(thresholds, net_benefits, color=PRIMARY, linewidth=2.5, label='预测模型')
        ax.plot(thresholds, all_treat, color=DANGER, linewidth=1.5, linestyle='--', label='全部治疗')
        ax.plot(thresholds, none_treat, color='gray', linewidth=1.5, linestyle=':', label='全不治疗')

        ax.axhline(0, color='black', linewidth=0.5)
        ax.set_xlabel('阈值概率')
        ax.set_ylabel('净收益')
        ax.set_title(title, fontsize=13, fontweight='bold')
        ax.legend(loc='upper right')
        ax.grid(alpha=0.3)
        ax.set_xlim([0, 1])
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    # ---------- 列线图（Nomogram） ----------

    @staticmethod
    def nomogram_points(coef_table: List[Dict], var_ranges: Dict[str, Tuple[float, float]]) -> bytes:
        """
        简化列线图：变量贡献可视化
        """
        # 过滤掉常数项
        items = [c for c in coef_table if c['variable'] != 'const' and not c['variable'].startswith('const')]

        if not items:
            return b''

        # 按|coef|排序
        items = sorted(items, key=lambda x: abs(x['coef']), reverse=True)

        # 计算Points（0-100）
        max_abs = max(abs(it['coef']) for it in items)
        for it in items:
            it['points'] = round(abs(it['coef']) / max_abs * 100, 1)

        fig, ax = plt.subplots(figsize=(10, max(4, len(items) * 0.5)), dpi=100)
        y_pos = np.arange(len(items))

        colors = [DANGER if it['coef'] > 0 else PRIMARY for it in items]
        bars = ax.barh(y_pos, [it['points'] for it in items], color=colors, alpha=0.8)

        for i, (bar, it) in enumerate(zip(bars, items)):
            ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height()/2,
                    f"  {it['points']} (OR={it['OR']}, p={it['p_value']:.3f})",
                    va='center', fontsize=9)

        ax.set_yticks(y_pos)
        ax.set_yticklabels([it['variable'] for it in items], fontsize=10)
        ax.set_xlabel('贡献得分 (Points)')
        ax.set_title('列线图 - 各变量贡献（基于Logistic回归系数）', fontsize=13, fontweight='bold')
        ax.invert_yaxis()
        ax.grid(axis='x', alpha=0.3)
        ax.set_xlim(0, 120)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    # ---------- Cox 生存分析（基础） ----------

    @staticmethod
    def cox_regression(df: pd.DataFrame, time_col: str, event_col: str,
                       predictors: List[str]) -> Dict:
        """
        Cox比例风险回归
        """
        try:
            from lifelines import CoxPHFitter
        except ImportError:
            return {'error': '需要安装lifelines: pip install lifelines'}

        cols = [time_col, event_col] + predictors
        sub = df[cols].copy().dropna()

        if len(sub) < len(predictors) + 10:
            return {'error': '样本不足'}

        for c in cols:
            sub[c] = pd.to_numeric(sub[c], errors='coerce')
        sub = sub.dropna()

        if (sub[time_col] <= 0).any():
            return {'error': '生存时间必须>0'}

        cph = CoxPHFitter()
        try:
            cph.fit(sub, duration_col=time_col, event_col=event_col)
        except Exception as e:
            return {'error': f'拟合失败: {str(e)}'}

        summary = cph.summary.reset_index()
        variable_col = cph.summary.index.name or 'index'
        if variable_col not in summary.columns:
            variable_col = summary.columns[0]
        summary = summary.rename(columns={variable_col: 'variable'})
        rows = []
        for _, r in summary.iterrows():
            rows.append({
                'variable': r['variable'],
                'coef': round(float(r['coef']), 4),
                'HR': round(float(r['exp(coef)']), 4),
                'HR_CI_low': round(float(r['exp(coef) lower 95%']), 4),
                'HR_CI_high': round(float(r['exp(coef) upper 95%']), 4),
                'p_value': float(r['p']),
                'significant': r['p'] < 0.05
            })

        # KM曲线（按主要预测因子分组）
        km_plot = PredictiveModeling._km_plot(sub, time_col, event_col, predictors[0] if predictors else None)

        return {
            'n': int(len(sub)),
            'events': int(sub[event_col].sum()),
            'coef_table': rows,
            'concordance_index': round(float(cph.concordance_index_), 4),
            'AIC': round(float(cph.AIC_partial_), 2),
            'log_likelihood': round(float(cph.log_likelihood_), 2),
            'km_plot': km_plot,
            'summary_text': str(cph.summary)
        }

    @staticmethod
    def _km_plot(df: pd.DataFrame, time_col: str, event_col: str,
                 group_col: Optional[str] = None) -> bytes:
        """KM生存曲线"""
        try:
            from lifelines import KaplanMeierFitter
            from lifelines.statistics import logrank_test
        except ImportError:
            return b''

        fig, ax = plt.subplots(figsize=(8, 5.5), dpi=100)
        kmf = KaplanMeierFitter()

        colors = ['#3B82F6', '#EF4444', '#10B981', '#F59E0B', '#8B5CF6', '#EC4899']

        if group_col and group_col in df.columns and df[group_col].nunique() <= 6:
            groups = sorted(df[group_col].unique())
            for i, g in enumerate(groups):
                sub = df[df[group_col] == g]
                kmf.fit(sub[time_col], sub[event_col], label=f'{group_col}={g}')
                kmf.plot_survival_function(ax=ax, color=colors[i % len(colors)], linewidth=2)

            # log-rank
            if len(groups) == 2:
                g1, g2 = df[df[group_col] == groups[0]], df[df[group_col] == groups[1]]
                lr = logrank_test(g1[time_col], g2[time_col], g1[event_col], g2[event_col])
                ax.text(0.6, 0.95, f'Log-rank p = {lr.p_value:.4f}',
                        transform=ax.transAxes, fontsize=10,
                        bbox=dict(facecolor='white', edgecolor='gray', alpha=0.8))
        else:
            kmf.fit(df[time_col], df[event_col])
            kmf.plot_survival_function(ax=ax, color=PRIMARY, linewidth=2)

        ax.set_xlabel('时间')
        ax.set_ylabel('生存概率')
        ax.set_title('Kaplan-Meier 生存曲线', fontsize=13, fontweight='bold')
        ax.grid(alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()