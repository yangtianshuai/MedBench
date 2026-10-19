"""
模型外部验证

做法：在当前数据上拟合 Logistic 模型，把同一套系数与编码规则应用到**另一个独立数据集**，
比较判别力（AUC）与校准（预测概率 vs 实际发生率），并给出决策曲线净获益。

限制：为保证编码规则可移植，预测变量只支持「数值型」与「二分类」；
多分类变量请先自行转成哑变量（0/1）后使用。
"""
import io
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy.special import expit
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score

MIN_SAMPLES = 20
BINS = 5


class ExternalValidation:
    """训练 / 外部验证一体化的 Logistic 流程。"""

    # ---------- 编码与整理 ----------
    @staticmethod
    def _encode(frame: pd.DataFrame, outcome: str, predictors: List[str],
                positive: Optional[str] = None,
                binary_map: Optional[Dict[str, object]] = None) -> Dict:
        """把预测变量统一转成数值矩阵，二分类变量按映射编码为 0/1。"""
        missing = [name for name in [outcome] + predictors if name not in frame.columns]
        if missing:
            return {'error': '数据缺少字段：' + '、'.join(missing)}

        labels = frame[outcome].dropna()
        uniques = list(labels.unique())
        numeric_uniques = pd.to_numeric(pd.Series(uniques), errors='coerce').dropna().tolist()
        if positive is None:
            if len(uniques) != 2:
                return {'error': f'结局变量「{outcome}」必须是二分类（当前 {len(uniques)} 类）'}
            if set(numeric_uniques) == {0.0, 1.0}:
                positive = 1  # 0/1 结局统一把 1 作为阳性事件，与临床惯例一致
            else:
                positive = uniques[-1]
        elif positive not in uniques and len(uniques) == 2:
            positive = uniques[-1]

        X = pd.DataFrame(index=frame.index)
        mapping: Dict[str, object] = {}
        for name in predictors:
            column = frame[name]
            non_null = column.dropna()
            if non_null.empty:
                X[name] = np.nan
                continue
            numeric_values = pd.to_numeric(non_null, errors='coerce')
            is_numeric = numeric_values.notna().mean() > 0.9
            if is_numeric and numeric_values.nunique() > 2:
                X[name] = pd.to_numeric(column, errors='coerce')
                continue
            # 二分类（含 0/1 与文本两类）
            defined = binary_map.get(name) if binary_map else None
            if defined is None:
                categories = list(non_null.unique())
                if len(categories) != 2:
                    return {'error': f'预测变量「{name}」有 {len(categories)} 个取值，'
                                     '外部验证只支持数值型与二分类变量'}
                distinct = set(numeric_values.dropna().unique())
                # 注意：空集是任何集合的子集，必须先判非空，否则文本变量会被误判成 0/1
                if distinct and distinct <= {0, 1}:
                    defined = 1  # 本来就是 0/1 数值
                else:
                    defined = categories[-1]
                mapping[name] = defined
            else:
                mapping[name] = defined
            if defined == 1:
                X[name] = pd.to_numeric(column, errors='coerce')
            else:
                X[name] = (column == defined).astype(float)

        y = (frame[outcome] == positive).astype(float)
        combined = pd.concat([X, y.rename('__y__')], axis=1).dropna()
        if len(combined) < MIN_SAMPLES:
            return {'error': f'有效样本不足 {MIN_SAMPLES} 例（当前 {len(combined)} 例）'}
        if combined['__y__'].nunique() < 2:
            return {'error': '结局只有一类，无法拟合或评估模型'}
        return {
            'X': combined[predictors],
            'y': combined['__y__'].astype(int),
            'positive': positive,
            'binary_map': mapping,
            'n': int(len(combined)),
            'n_events': int(combined['__y__'].sum()),
        }

    # ---------- 训练 ----------
    @classmethod
    def train(cls, frame: pd.DataFrame, outcome: str, predictors: List[str],
              positive: Optional[str] = None) -> Dict:
        prepared = cls._encode(frame, outcome, predictors, positive=positive)
        if 'error' in prepared:
            return prepared
        X, y = prepared['X'], prepared['y']
        model = LogisticRegression(max_iter=2000, solver='lbfgs')
        model.fit(X.to_numpy(dtype=float), y.to_numpy(dtype=int))
        probability = model.predict_proba(X.to_numpy(dtype=float))[:, 1]
        return {
            'model': model,
            'coefficients': {name: float(value) for name, value in zip(predictors, model.coef_[0])},
            'intercept': float(model.intercept_[0]),
            'features': list(predictors),
            'outcome': outcome,
            'positive': prepared['positive'],
            'binary_map': prepared['binary_map'],
            'train_probability': probability,
            'train_y': y.to_numpy(dtype=int),
            'train_auc': float(roc_auc_score(y, probability)),
            'n_train': prepared['n'],
            'n_events_train': prepared['n_events'],
        }

    # ---------- 外部评估 ----------
    @classmethod
    def evaluate(cls, trained: Dict, frame_new: pd.DataFrame,
                 new_outcome: Optional[str] = None) -> Dict:
        """在独立数据集上评估：判别力、校准、决策曲线。"""
        if 'error' in trained:
            return trained
        outcome = new_outcome or trained['outcome']
        prepared = cls._encode(frame_new, outcome, trained['features'],
                               positive=trained['positive'],
                               binary_map=trained['binary_map'])
        if 'error' in prepared:
            return prepared
        X_new, y_new = prepared['X'], prepared['y']
        linear = trained['intercept'] + X_new.to_numpy(dtype=float) @ np.array(
            [trained['coefficients'][name] for name in trained['features']], dtype=float)
        probability = expit(linear)
        external_auc = float(roc_auc_score(y_new, probability))

        predicted = (probability >= 0.5).astype(int)
        tp = int(((predicted == 1) & (y_new.to_numpy() == 1)).sum())
        fp = int(((predicted == 1) & (y_new.to_numpy() == 0)).sum())
        fn = int(((predicted == 0) & (y_new.to_numpy() == 1)).sum())
        tn = int(((predicted == 0) & (y_new.to_numpy() == 0)).sum())
        sensitivity = tp / (tp + fn) if (tp + fn) else float('nan')
        specificity = tn / (tn + fp) if (tn + fp) else float('nan')
        ppv = tp / (tp + fp) if (tp + fp) else float('nan')
        npv = tn / (tn + fn) if (tn + fn) else float('nan')
        accuracy = (tp + tn) / max(len(y_new), 1)
        brier = float(brier_score_loss(y_new, probability))

        metrics = pd.DataFrame([
            {'指标': '样本量', '内部（训练集）': trained['n_train'], '外部（验证集）': prepared['n']},
            {'指标': '阳性事件数', '内部（训练集）': trained['n_events_train'],
             '外部（验证集）': prepared['n_events']},
            {'指标': 'AUC', '内部（训练集）': round(trained['train_auc'], 4),
             '外部（验证集）': round(external_auc, 4)},
            {'指标': 'AUC 变化', '内部（训练集）': '—',
             '外部（验证集）': round(external_auc - trained['train_auc'], 4)},
            {'指标': '准确率（阈值 0.5）', '内部（训练集）': '—', '外部（验证集）': round(accuracy, 4)},
            {'指标': '灵敏度', '内部（训练集）': '—', '外部（验证集）': round(float(sensitivity), 4)},
            {'指标': '特异度', '内部（训练集）': '—', '外部（验证集）': round(float(specificity), 4)},
            {'指标': '阳性预测值', '内部（训练集）': '—', '外部（验证集）': round(float(ppv), 4)},
            {'指标': '阴性预测值', '内部（训练集）': '—', '外部（验证集）': round(float(npv), 4)},
            {'指标': 'Brier 分数（越小越好）', '内部（训练集）': '—',
             '外部（验证集）': round(brier, 4)},
        ])

        calibration = pd.DataFrame([
            cls._calibration_row(trained['train_probability'], trained['train_y'], '内部（训练集）'),
            cls._calibration_row(probability, y_new.to_numpy(), '外部（验证集）'),
        ], index=None)
        calibration_long = []
        for _, row in calibration.iterrows():
            for level, (predicted_mean, observed, count) in row['分组'].items():
                calibration_long.append({
                    '数据来源': row['数据来源'],
                    '预测概率区间': level,
                    '平均预测概率': predicted_mean,
                    '实际发生率': observed,
                    '例数': count,
                })
        calibration_table = pd.DataFrame(calibration_long)

        drop = external_auc - trained['train_auc']
        return {
            'metrics': metrics,
            'calibration': calibration_table,
            'external_auc': external_auc,
            'train_auc': trained['train_auc'],
            'auc_drop': drop,
            'external_probability': probability,
            'external_y': y_new.to_numpy(dtype=int),
            'n_external': prepared['n'],
            'plot': cls._plot(trained, probability, y_new.to_numpy(dtype=int), calibration_table),
            'display': (f'外部验证：验证集 {prepared["n"]} 例（阳性 {prepared["n_events"]} 例）；'
                        f'AUC 从内部 {trained["train_auc"]:.3f} 变为外部 {external_auc:.3f}'
                        f'（{drop:+.3f}）；Brier={brier:.3f}'),
        }

    @staticmethod
    def _calibration_row(probability: np.ndarray, y: np.ndarray, label: str) -> Dict:
        """按预测概率等宽分组，给出平均预测概率与实际发生率。"""
        probability = np.asarray(probability, dtype=float)
        y = np.asarray(y, dtype=int)
        edges = np.linspace(0, 1, BINS + 1)
        groups = {}
        for index in range(BINS):
            low, high = edges[index], edges[index + 1]
            mask = ((probability >= low) & (probability < high)) if index < BINS - 1 \
                else ((probability >= low) & (probability <= high))
            if not mask.any():
                continue
            groups[f'{low:.1f}–{high:.1f}'] = (
                round(float(probability[mask].mean()), 3),
                round(float(y[mask].mean()), 3),
                int(mask.sum()),
            )
        return {'数据来源': label, '分组': groups}

    @staticmethod
    def _plot(trained: Dict, external_probability: np.ndarray, external_y: np.ndarray,
              calibration_table: pd.DataFrame) -> bytes:
        """左：校准曲线（内部 vs 外部）；右：外部预测概率分布（按实际结局分层）。"""
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(1, 2, figsize=(12.4, 5.0), dpi=110)

        ax = axes[0]
        ax.plot([0, 1], [0, 1], 'k--', alpha=0.4, linewidth=1, label='理想校准')
        colors = {'内部（训练集）': '#2563EB', '外部（验证集）': '#EF4444'}
        for label, chunk in calibration_table.groupby('数据来源'):
            ax.plot(chunk['平均预测概率'], chunk['实际发生率'], marker='o', linewidth=2,
                    color=colors.get(label, '#7C3AED'), label=label)
        ax.set_xlabel('平均预测概率')
        ax.set_ylabel('实际发生率')
        ax.set_title('校准曲线对比', fontsize=12, fontweight='bold')
        ax.set_xlim(-0.02, 1.02)
        ax.set_ylim(-0.02, 1.02)
        ax.legend(fontsize=9)
        ax.grid(alpha=0.3)

        ax2 = axes[1]
        bins = np.linspace(0, 1, 21)
        ax2.hist(external_probability[external_y == 0], bins=bins, alpha=0.65,
                 color='#94A3B8', label='实际阴性')
        ax2.hist(external_probability[external_y == 1], bins=bins, alpha=0.65,
                 color='#EF4444', label='实际阳性')
        ax2.set_xlabel('预测概率')
        ax2.set_ylabel('例数')
        ax2.set_title('外部验证集预测概率分布', fontsize=12, fontweight='bold')
        ax2.legend(fontsize=9)
        ax2.grid(alpha=0.3)

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()
