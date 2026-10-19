"""
预测模型验证模块
负责：LASSO（L1 正则）变量筛选、交叉验证、Bootstrap 内部验证（乐观度校正 AUC）

适用结构：0/1 结局列 + 多个候选预测变量；样本量建议 ≥200 再谈内部验证。
"""
import warnings
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

from sklearn.linear_model import LogisticRegression, LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import StandardScaler

RANDOM_STATE = 20260101


class ModelValidation:
    """模型变量筛选与内部验证"""

    @staticmethod
    def prepare_matrix(df: pd.DataFrame, outcome: str,
                       predictors: Sequence[str]) -> Tuple[pd.DataFrame, pd.Series, Dict]:
        """
        构造建模矩阵：分类变量做哑变量，缺失行剔除。
        返回 (X, y, 说明信息)
        """
        if outcome not in df.columns:
            return pd.DataFrame(), pd.Series(dtype=float), {'error': f'结局列不存在：{outcome}'}

        columns = [c for c in predictors if c in df.columns and c != outcome]
        if not columns:
            return pd.DataFrame(), pd.Series(dtype=float), {'error': '请至少选择一个预测变量'}

        frame = df[columns].copy()
        numeric_columns, categorical_columns = [], []
        for column in frame.columns:
            if pd.api.types.is_numeric_dtype(frame[column]):
                numeric_columns.append(column)
            else:
                categorical_columns.append(column)

        design = pd.DataFrame(index=frame.index)
        for column in numeric_columns:
            design[column] = pd.to_numeric(frame[column], errors='coerce')

        encoded_labels = {}
        for column in categorical_columns:
            dummies = pd.get_dummies(frame[column].astype('category'), prefix=column, drop_first=True)
            if dummies.shape[1] == 0:
                continue
            design = pd.concat([design, dummies.astype(float)], axis=1)
            encoded_labels[column] = [str(c) for c in dummies.columns]

        target = pd.to_numeric(df[outcome], errors='coerce')
        keep = target.isin([0, 1]) & design.notna().all(axis=1)
        design, target = design[keep], target[keep].astype(int)

        info = {
            'numeric_columns': numeric_columns,
            'categorical_columns': categorical_columns,
            'dropped_rows': int(len(df) - len(target)),
            'encoded_labels': encoded_labels,
        }
        if len(target) < 30:
            return design, target, {**info, 'error': f'有效样本仅 {len(target)} 例，不足以建模'}
        if target.nunique() < 2:
            return design, target, {**info, 'error': '结局只有一种取值，无法建模'}
        return design, target, info

    @staticmethod
    def lasso_selection(df: pd.DataFrame, outcome: str, predictors: Sequence[str],
                        cv_folds: int = 5, penalty: str = 'l1') -> Dict:
        """
        L1 正则 Logistic（LASSO）变量筛选：按交叉验证选择正则强度，
        压缩到 0 的变量即被剔除。
        """
        design, target, info = ModelValidation.prepare_matrix(df, outcome, predictors)
        if info.get('error'):
            return info
        if design.shape[1] < 2:
            return {'error': '可用自变量不足 2 个'}

        folds = min(cv_folds, int(target.value_counts().min()))
        if folds < 3:
            return {'error': '某一类别样本过少，无法做交叉验证'}

        scaler = StandardScaler()
        scaled = scaler.fit_transform(design)
        model = LogisticRegressionCV(
            Cs=np.logspace(-3, 2, 25), cv=StratifiedKFold(n_splits=folds, shuffle=True,
                                                         random_state=RANDOM_STATE),
            penalty=penalty, solver='saga', scoring='roc_auc',
            max_iter=5000, random_state=RANDOM_STATE,
        )
        model.fit(scaled, target)

        coefficients = model.coef_[0]
        # 入选阈值取「最大系数绝对值的 1%」，避免把被 L1 压成近似 0 的变量算进来
        cutoff = max(1e-4, 0.01 * float(np.max(np.abs(coefficients))))
        table = []
        for name, coefficient in zip(design.columns, coefficients):
            table.append({
                '变量': name,
                '系数（标准化）': round(float(coefficient), 4),
                'OR（每 1 个标准差）': round(float(np.exp(coefficient)), 4),
                '是否入选': '✅' if abs(coefficient) > cutoff else '—',
            })
        table.sort(key=lambda row: -abs(row['系数（标准化）']))
        selected = [row['变量'] for row in table if row['是否入选'] == '✅']

        return {
            'n': int(len(target)),
            'n_candidates': int(design.shape[1]),
            'selected': selected,
            'table': table,
            'best_c': float(model.C_[0]),
            'cv_folds': folds,
            'info': info,
            'test': 'L1 正则 Logistic 回归（LASSO）',
        }

    @staticmethod
    def cross_validation(df: pd.DataFrame, outcome: str, predictors: Sequence[str],
                         folds: int = 5) -> Dict:
        """K 折交叉验证 AUC（模型泛化的第一层证据）"""
        design, target, info = ModelValidation.prepare_matrix(df, outcome, predictors)
        if info.get('error'):
            return info

        n_splits = min(folds, int(target.value_counts().min()))
        if n_splits < 3:
            return {'error': '某一类别样本过少，无法做交叉验证'}

        scaler = StandardScaler()
        scaled = scaler.fit_transform(design)
        scores = []
        splitter = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=RANDOM_STATE)
        for train_index, test_index in splitter.split(scaled, target):
            model = LogisticRegression(max_iter=5000, random_state=RANDOM_STATE)
            model.fit(scaled[train_index], target.iloc[train_index])
            probability = model.predict_proba(scaled[test_index])[:, 1]
            scores.append(roc_auc_score(target.iloc[test_index], probability))

        return {
            'n': int(len(target)), 'folds': n_splits,
            'mean_auc': float(np.mean(scores)),
            'sd_auc': float(np.std(scores, ddof=1)) if len(scores) > 1 else 0.0,
            'scores': [round(float(s), 4) for s in scores],
            'display': f"{np.mean(scores):.3f} ± {np.std(scores, ddof=1):.3f}",
            'test': f'{n_splits} 折交叉验证 AUC',
        }

    @staticmethod
    def bootstrap_validation(df: pd.DataFrame, outcome: str, predictors: Sequence[str],
                             n_bootstrap: int = 200, seed: int = RANDOM_STATE) -> Dict:
        """
        Bootstrap 内部验证（Harrell 乐观度校正）：
        校正 AUC = 全样本 AUC − 平均乐观度
        """
        design, target, info = ModelValidation.prepare_matrix(df, outcome, predictors)
        if info.get('error'):
            return info

        scaler = StandardScaler()
        scaled = scaler.fit_transform(design)
        model = LogisticRegression(max_iter=5000, random_state=seed)
        model.fit(scaled, target)
        apparent_auc = float(roc_auc_score(target, model.predict_proba(scaled)[:, 1]))

        rng = np.random.default_rng(seed)
        optimism = []
        bootstrap_auc = []
        total = len(target)
        target_values = target.to_numpy()
        for _ in range(int(n_bootstrap)):
            index = rng.integers(0, total, total)
            if len(np.unique(target_values[index])) < 2:
                continue
            boot_model = LogisticRegression(max_iter=5000, random_state=seed)
            boot_model.fit(scaled[index], target_values[index])
            boot_auc = roc_auc_score(target_values[index],
                                     boot_model.predict_proba(scaled[index])[:, 1])
            original_auc = roc_auc_score(target_values,
                                         boot_model.predict_proba(scaled)[:, 1])
            optimism.append(boot_auc - original_auc)
            bootstrap_auc.append(float(original_auc))

        if not optimism:
            return {'error': 'Bootstrap 未产生有效重抽样样本'}

        mean_optimism = float(np.mean(optimism))
        corrected = apparent_auc - mean_optimism
        lower, upper = np.percentile(bootstrap_auc, [2.5, 97.5])
        return {
            'n': int(total), 'n_bootstrap': len(optimism),
            'apparent_auc': apparent_auc,
            'optimism': mean_optimism,
            'corrected_auc': float(corrected),
            'ci': (float(lower), float(upper)),
            'display': (f'表观 AUC {apparent_auc:.3f}；平均乐观度 {mean_optimism:.3f}；'
                        f'校正 AUC {corrected:.3f}；泛化 AUC 的 Bootstrap 95%CI '
                        f'{lower:.3f}–{upper:.3f}'),
            'info': info,
            'test': 'Bootstrap 内部验证（乐观度校正）',
        }
