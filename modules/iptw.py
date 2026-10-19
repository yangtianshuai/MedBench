"""
逆概率加权（IPTW）模块
负责：倾向评分估计、权重构造（ATE / ATT / ATM、稳定权重、分位数截断）、
      加权前后的平衡诊断（SMD、有效样本量）与加权后的效应估计

适用结构：0/1 处理变量 + 结局（连续或二分类）+ 若干协变量。
与倾向性评分匹配（PSM）互补：IPTW 保留全部样本，PSM 只能留下匹配上的样本。
"""
import io
import warnings
from typing import Dict, List, Optional, Sequence, Tuple

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

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

RANDOM_STATE = 20260101
WEIGHT_LABELS = {
    'ATE': 'ATE 权重（全人群平均处理效应）',
    'ATT': 'ATT 权重（处理组的平均处理效应）',
    'ATM': 'ATM 重叠权重（极端分数影响更小）',
}


class IPTWAnalysis:
    """逆概率加权分析"""

    # ---------- 数据准备 ----------
    @staticmethod
    def build_design(df: pd.DataFrame, covariates: Sequence[str],
                     treatment: str) -> Tuple[pd.DataFrame, Dict]:
        """协变量 → 设计矩阵：数值列保留，分类列转哑变量"""
        frame = df[[c for c in covariates if c in df.columns and c != treatment]].copy()
        if frame.empty:
            return pd.DataFrame(), {'error': '没有可用的协变量'}

        numeric_columns, categorical_columns = [], []
        for column in frame.columns:
            if pd.api.types.is_numeric_dtype(frame[column]) and frame[column].nunique() > 2:
                numeric_columns.append(column)
            else:
                categorical_columns.append(column)

        design = pd.DataFrame(index=frame.index)
        for column in numeric_columns:
            design[column] = pd.to_numeric(frame[column], errors='coerce')
        for column in categorical_columns:
            dummies = pd.get_dummies(frame[column].astype('category'), prefix=column,
                                     drop_first=True)
            for name in dummies.columns:
                design[name] = dummies[name].astype(float)

        return design, {'numeric': numeric_columns, 'categorical': categorical_columns}

    # ---------- 倾向评分与权重 ----------
    @staticmethod
    def fit_propensity(design: pd.DataFrame, treatment: np.ndarray) -> np.ndarray:
        """Logistic 回归估计倾向评分（协变量标准化后再拟合）"""
        scaler = StandardScaler()
        scaled = scaler.fit_transform(design.to_numpy(dtype=float))
        model = LogisticRegression(max_iter=5000, C=1.0, random_state=RANDOM_STATE)
        model.fit(scaled, treatment)
        return model.predict_proba(scaled)[:, 1]

    @staticmethod
    def estimate_weights(propensity: np.ndarray, treatment: np.ndarray,
                         weight_type: str = 'ATE', stabilize: bool = True,
                         trim: float = 0.0) -> np.ndarray:
        """
        由倾向评分构造权重。
        ATE: z/e + (1-z)/(1-e)；ATT: z + (1-z)·e/(1-e)；ATM: z·(1-e) + (1-z)·e
        """
        ps = np.clip(np.asarray(propensity, dtype=float), 1e-6, 1 - 1e-6)
        z = np.asarray(treatment, dtype=float)

        if weight_type == 'ATT':
            weights = z + (1 - z) * ps / (1 - ps)
        elif weight_type == 'ATM':
            weights = z * (1 - ps) + (1 - z) * ps
        else:
            weights = z / ps + (1 - z) / (1 - ps)

        if stabilize and weight_type == 'ATE':
            share = float(z.mean())
            weights = np.where(z == 1, share / ps, (1 - share) / (1 - ps))

        if trim and trim > 0:
            lower, upper = np.quantile(weights, [trim, 1 - trim])
            weights = np.clip(weights, lower, upper)
        return weights

    @staticmethod
    def effective_sample_size(weights: np.ndarray) -> float:
        """有效样本量 ESS = (Σw)² / Σw²"""
        weights = np.asarray(weights, dtype=float)
        denominator = float(np.sum(weights ** 2))
        return float(np.sum(weights) ** 2 / denominator) if denominator > 0 else 0.0

    # ---------- 平衡诊断 ----------
    @staticmethod
    def _weighted_mean_var(values: np.ndarray, weights: np.ndarray) -> Tuple[float, float]:
        mask = ~np.isnan(values) & ~np.isnan(weights)
        values, weights = values[mask], weights[mask]
        if len(values) == 0 or weights.sum() <= 0:
            return float('nan'), float('nan')
        mean = float(np.average(values, weights=weights))
        variance = float(np.average((values - mean) ** 2, weights=weights))
        return mean, variance

    @classmethod
    def balance_table(cls, design: pd.DataFrame, treatment: np.ndarray,
                      weights: Optional[np.ndarray] = None) -> pd.DataFrame:
        """加权前后各协变量的标准化均值差（SMD）"""
        z = np.asarray(treatment, dtype=float)
        w = np.ones_like(z) if weights is None else np.asarray(weights, dtype=float)
        rows = []
        for column in design.columns:
            values = design[column].to_numpy(dtype=float)
            mean1, var1 = cls._weighted_mean_var(values[z == 1], w[z == 1])
            mean0, var0 = cls._weighted_mean_var(values[z == 0], w[z == 0])
            pooled = np.sqrt((var1 + var0) / 2) if (var1 + var0) > 0 else 0.0
            smd = 0.0 if pooled == 0 else (mean1 - mean0) / pooled
            rows.append({
                '协变量': column,
                '处理组': round(mean1, 4),
                '对照组': round(mean0, 4),
                'SMD': round(float(smd), 4),
                '是否平衡(|SMD|<0.1)': '✅' if abs(smd) < 0.1 else '⚠️',
            })
        return pd.DataFrame(rows)

    @staticmethod
    def love_plot(before: pd.DataFrame, after: pd.DataFrame,
                  title: str = 'Love Plot - 加权前后平衡') -> bytes:
        """Love plot：加权前后 SMD 对比"""
        merged = before[['协变量', 'SMD']].merge(after[['协变量', 'SMD']],
                                                on='协变量', suffixes=('_加权前', '_加权后'))
        merged = merged.reindex(
            merged['SMD_加权前'].abs().sort_values(ascending=False).index)
        if merged.empty:
            return b''

        fig, ax = plt.subplots(figsize=(8, max(3, 0.35 * len(merged) + 1.6)), dpi=100)
        positions = np.arange(len(merged))
        ax.scatter(merged['SMD_加权前'].abs(), positions, color=DANGER, s=42,
                   label='加权前', zorder=3)
        ax.scatter(merged['SMD_加权后'].abs(), positions, color=SUCCESS, s=42,
                   label='加权后', zorder=3)
        for position, (before_value, after_value) in enumerate(
                zip(merged['SMD_加权前'].abs(), merged['SMD_加权后'].abs())):
            ax.plot([before_value, after_value], [position, position],
                    color='#CBD5E1', linewidth=1.2, zorder=1)
        ax.axvline(0.1, color=WARNING, linestyle='--', linewidth=1.4,
                   label='0.1 平衡阈值')
        ax.set_yticks(positions)
        ax.set_yticklabels([str(c)[:22] for c in merged['协变量']], fontsize=8)
        ax.invert_yaxis()
        ax.set_xlabel('|标准化均值差 SMD|')
        ax.set_title(title, fontsize=12, fontweight='bold')
        ax.grid(axis='x', alpha=0.3)
        ax.legend(fontsize=8, loc='lower right')
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()

    # ---------- 效应估计 ----------
    @staticmethod
    def _effect_once(outcome: np.ndarray, treatment: np.ndarray, weights: np.ndarray,
                     outcome_type: str) -> Dict:
        z = treatment.astype(bool)
        w = np.asarray(weights, dtype=float)
        if outcome_type == 'continuous':
            mean1 = float(np.average(outcome[z], weights=w[z]))
            mean0 = float(np.average(outcome[~z], weights=w[~z]))
            return {'estimate': mean1 - mean0, 'treated': mean1, 'control': mean0}

        risk1 = float(np.average(outcome[z], weights=w[z]))
        risk0 = float(np.average(outcome[~z], weights=w[~z]))
        result = {'estimate': risk1 - risk0, 'treated': risk1, 'control': risk0}
        if 0 < risk1 < 1 and 0 < risk0 < 1:
            result['risk_ratio'] = risk1 / risk0
            result['odds_ratio'] = (risk1 / (1 - risk1)) / (risk0 / (1 - risk0))
        return result

    @classmethod
    def run(cls, df: pd.DataFrame, treatment: str, outcome: str,
            covariates: Sequence[str], weight_type: str = 'ATE',
            trim: float = 0.01, n_bootstrap: int = 200,
            outcome_type: Optional[str] = None) -> Dict:
        """完整 IPTW 流程：倾向评分 → 权重 → 平衡诊断 → 加权效应 + Bootstrap CI"""
        for column in (treatment, outcome):
            if column not in df.columns:
                return {'error': f'字段不存在：{column}'}

        design, info = cls.build_design(df, covariates, treatment)
        if info.get('error'):
            return info

        target = pd.to_numeric(df[treatment], errors='coerce')
        y_raw = df[outcome]
        keep = target.isin([0, 1]) & design.notna().all(axis=1) & y_raw.notna()
        design, target, y_raw = design[keep], target[keep], y_raw[keep]
        if len(target) < 30:
            return {'error': f'有效样本仅 {len(target)} 例，不足以做加权分析'}
        if target.nunique() < 2:
            return {'error': '处理变量只有一种取值'}

        if outcome_type is None:
            unique_outcome = pd.to_numeric(y_raw, errors='coerce').dropna().unique()
            outcome_type = ('binary' if set(np.unique(unique_outcome)) <= {0, 1}
                            and len(unique_outcome) == 2 else 'continuous')
        if outcome_type == 'binary':
            outcome_values = pd.to_numeric(y_raw, errors='coerce').to_numpy(dtype=float)
        else:
            outcome_values = pd.to_numeric(y_raw, errors='coerce').to_numpy(dtype=float)

        treatment_values = target.to_numpy(dtype=float)
        propensity = cls.fit_propensity(design, treatment_values)
        weights = cls.estimate_weights(propensity, treatment_values, weight_type,
                                       stabilize=True, trim=float(trim))

        balance_before = cls.balance_table(design, treatment_values)
        balance_after = cls.balance_table(design, treatment_values, weights)
        max_smd_before = float(balance_before['SMD'].abs().max())
        max_smd_after = float(balance_after['SMD'].abs().max())
        balanced = int((balance_after['SMD'].abs() < 0.1).sum())

        point = cls._effect_once(outcome_values, treatment_values, weights, outcome_type)

        # Bootstrap：每次重抽后重新估计倾向评分与权重，纳入权重估计的不确定性
        generator = np.random.default_rng(RANDOM_STATE)
        total = len(target)
        design_matrix = design.to_numpy(dtype=float)
        samples = {'estimate': [], 'risk_ratio': [], 'odds_ratio': []}
        for _ in range(int(n_bootstrap)):
            index = generator.integers(0, total, total)
            if len(np.unique(treatment_values[index])) < 2:
                continue
            try:
                ps_boot = cls.fit_propensity(
                    pd.DataFrame(design_matrix[index], columns=design.columns),
                    treatment_values[index])
                weight_boot = cls.estimate_weights(ps_boot, treatment_values[index],
                                                   weight_type, stabilize=True,
                                                   trim=float(trim))
                effect = cls._effect_once(outcome_values[index], treatment_values[index],
                                          weight_boot, outcome_type)
            except Exception:
                continue
            samples['estimate'].append(effect['estimate'])
            if 'risk_ratio' in effect:
                samples['risk_ratio'].append(effect['risk_ratio'])
                samples['odds_ratio'].append(effect['odds_ratio'])

        def summarize(values: List[float], estimate: float) -> Dict:
            if len(values) < 30:
                return {'se': float('nan'), 'ci': (float('nan'), float('nan')),
                        'p': float('nan')}
            array = np.asarray(values, dtype=float)
            standard_error = float(np.std(array, ddof=1))
            lower, upper = np.percentile(array, [2.5, 97.5])
            p_value = float(2 * (1 - stats.norm.cdf(abs(estimate / standard_error)))) \
                if standard_error > 0 else float('nan')
            return {'se': standard_error, 'ci': (float(lower), float(upper)), 'p': p_value}

        summary = summarize(samples['estimate'], float(point['estimate']))
        extra_summary = {}
        if 'risk_ratio' in point:
            extra_summary['risk_ratio'] = summarize(samples['risk_ratio'],
                                                    float(point['risk_ratio']))
            extra_summary['odds_ratio'] = summarize(samples['odds_ratio'],
                                                    float(point['odds_ratio']))

        return {
            'n': int(total),
            'n_treated': int((treatment_values == 1).sum()),
            'n_control': int((treatment_values == 0).sum()),
            'n_covariates': int(design.shape[1]),
            'weight_type': weight_type,
            'weight_label': WEIGHT_LABELS.get(weight_type, weight_type),
            'trim': float(trim),
            'outcome_type': outcome_type,
            'propensity': propensity,
            'weights': weights,
            'ess': cls.effective_sample_size(weights),
            'ess_treated': cls.effective_sample_size(weights[treatment_values == 1]),
            'ess_control': cls.effective_sample_size(weights[treatment_values == 0]),
            'balance_before': balance_before,
            'balance_after': balance_after,
            'max_smd_before': max_smd_before,
            'max_smd_after': max_smd_after,
            'balanced_count': balanced,
            'love_plot': cls.love_plot(balance_before, balance_after),
            'effect': point,
            'summary': summary,
            'extra_summary': extra_summary,
            'n_bootstrap': len(samples['estimate']),
            'test': f'IPTW（{weight_type} 权重）',
        }
