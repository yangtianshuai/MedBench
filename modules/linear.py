"""
多重线性回归（OLS）

补的是「常规数据分析」里最常见的一类问题：以**连续变量**为结局的回归。
在此之前程序只有 Logistic（二分类结局）与 Cox（时间-事件），
「哪些因素与住院天数独立相关」这类问题做不了，只能把连续结局硬切成二分类，
白白损失信息。

顺带覆盖 ANCOVA：把分组变量作为其中一个预测变量，就是「校正基线后比较组间差异」。

输出：系数表（β、SE、t、p、95%CI、标准化 β）、R² / 调整 R²、F 检验、
      VIF 共线性、Durbin-Watson、Breusch-Pagan 异方差检验、
      残差诊断图（Q-Q / 残差-拟合 / 直方图）、异常点识别（标准化残差 / Cook's D / 杠杆值）。
"""
import io
import warnings
from typing import Dict, List, Optional

import matplotlib
import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats
from statsmodels.stats.diagnostic import het_breuschpagan
from statsmodels.stats.outliers_influence import variance_inflation_factor

matplotlib.rcParams['font.sans-serif'] = [
    'SimHei', 'Microsoft YaHei',
    'WenQuanYi Zen Hei', 'WenQuanYi Micro Hei',
    'Noto Sans CJK SC', 'Noto Sans SC', 'Source Han Sans SC',
    'Heiti SC', 'PingFang SC', 'Arial Unicode MS', 'DejaVu Sans'
]
matplotlib.rcParams['axes.unicode_minus'] = False
import matplotlib.pyplot as plt  # noqa: E402

warnings.filterwarnings('ignore')

# 每预测变量建议的最少样本量（经验法则）
MIN_N_PER_PREDICTOR = 10
VIF_WARN = 5.0
VIF_SERIOUS = 10.0
# 异常点判定：这三个阈值是「真正需要核对」的标准。
# 注意不能用 Cook's D > 4/n 作为门槛 —— 它在干净数据上也会标出一成左右的样本，
# 提示一多就没人看了；高杠杆本身也不是异常，只有杠杆大且残差也大才值得核对。
OUTLIER_STD_RESID = 3.0
COOKS_D_THRESHOLD = 1.0
LEVERAGE_FACTOR = 3.0
LEVERAGE_RESID_GATE = 2.0


class LinearModel:
    """多重线性回归（连续结局）。"""

    # ---------- 数据准备 ----------

    @staticmethod
    def _is_categorical(series: pd.Series) -> bool:
        """是否按分类变量处理（文本 / 类别 / 取值很少的整数）。"""
        if series.dtype == 'object' or str(series.dtype) == 'category':
            return True
        unique = series.dropna().nunique()
        return unique <= 5 and not pd.api.types.is_float_dtype(series)

    @staticmethod
    def prepare(df: pd.DataFrame, outcome: str, predictors: List[str],
                categorical: Optional[List[str]] = None) -> Dict:
        """
        构造建模矩阵：数值列直接进模型，分类列展开为哑变量（第一水平作参照）。

        参照水平按取值排序后的第一项确定，并在结果里写明 —— 否则
        「通气策略 的系数」到底是相对谁，读表的人无从判断。
        """
        categorical = list(categorical or [])
        sub = df[[outcome] + predictors].copy()
        notes: List[str] = []
        design_parts: List[pd.DataFrame] = []
        continuous: List[str] = []
        total_dummies = 0

        for name in predictors:
            series = sub[name]
            is_cat = name in categorical or (
                name not in categorical and LinearModel._is_categorical(series))
            if is_cat:
                non_null = series.dropna().astype(str)
                levels = sorted(non_null.unique().tolist())
                if len(levels) < 2:
                    return {'error': f'「{name}」只有一个取值，无法作为预测变量'}
                if len(levels) > 20:
                    return {'error': f'「{name}」有 {len(levels)} 个水平，'
                                     '水平过多不适合直接做哑变量'}
                reference = levels[0]
                dummies = pd.get_dummies(
                    series.astype(str), prefix=name, drop_first=True, dtype=float)
                dummies.index = sub.index
                design_parts.append(dummies)
                total_dummies += dummies.shape[1]
                notes.append(f'{name}：分类变量，参照水平 = {reference}')
            else:
                numeric = pd.to_numeric(series, errors='coerce')
                design_parts.append(numeric.rename(name).to_frame())
                continuous.append(name)

        design = pd.concat(design_parts, axis=1)
        frame = pd.concat([sub[[outcome]].apply(pd.to_numeric, errors='coerce'),
                           design], axis=1)
        frame = frame.dropna()
        return {
            'ok': True, 'frame': frame, 'outcome': outcome,
            'design_columns': list(design.columns),
            'continuous': continuous, 'dummy_count': total_dummies,
            'notes': notes, 'n_input': int(len(df)), 'n_used': int(len(frame)),
            'n_dropped': int(len(df) - len(frame)),
        }

    # ---------- 拟合 ----------

    @staticmethod
    def fit(df: pd.DataFrame, outcome: str, predictors: List[str],
            categorical: Optional[List[str]] = None) -> Dict:
        """拟合多重线性回归并做完整诊断。"""
        if not predictors:
            return {'error': '请至少选择一个自变量'}
        if outcome in predictors:
            return {'error': '结局变量不能同时作为自变量'}
        if len(df[[outcome] + predictors].dropna(how='all')) < 5:
            return {'error': '有效数据过少，无法拟合'}

        prepared = LinearModel.prepare(df, outcome, predictors, categorical)
        if 'error' in prepared:
            return prepared

        frame = prepared['frame']
        design_columns = prepared['design_columns']
        n, p = len(frame), len(design_columns)
        if n <= p + 1:
            return {'error': f'有效样本（{n}）不足以估计 {p} 个变量，'
                             '请减少自变量或补充数据'}

        y = frame[outcome].astype(float)
        X = frame[design_columns].astype(float)
        if float(y.std(ddof=1)) == 0:
            return {'error': f'结局变量「{outcome}」没有变异，无法回归'}

        X_const = sm.add_constant(X, has_constant='add')
        try:
            model = sm.OLS(y, X_const).fit()
        except Exception as error:  # noqa: BLE001
            return {'error': f'模型拟合失败：{error}'}

        params, bse = model.params, model.bse
        tvals, pvals = model.tvalues, model.pvalues
        conf = model.conf_int()
        ci_low = np.asarray(conf)[:, 0]
        ci_high = np.asarray(conf)[:, 1]
        ci_index = {name: position for position, name in enumerate(X_const.columns)}

        # 标准化 β：只有连续变量有明确意义，哑变量仍给出但会标注
        y_sd = float(y.std(ddof=1))
        std_beta: Dict[str, Optional[float]] = {}
        for name in design_columns:
            sd = float(X[name].std(ddof=1))
            std_beta[name] = (float(params[name]) * sd / y_sd) if sd > 0 else None

        coef_rows = []
        for name in X_const.columns:
            position = ci_index[name]
            is_dummy = name not in prepared['continuous']
            coef_rows.append({
                '变量': name,
                '回归系数 β': round(float(params[name]), 4),
                '标准误': round(float(bse[name]), 4),
                't 值': round(float(tvals[name]), 4),
                'p 值': float(pvals[name]),
                '95% CI 下限': round(float(ci_low[position]), 4),
                '95% CI 上限': round(float(ci_high[position]), 4),
                '标准化 β': (round(std_beta[name], 4)
                          if std_beta.get(name) is not None else None),
            })

        # ---------- 共线性 ----------
        vif_rows = []
        for position, name in enumerate(X_const.columns):
            if name == 'const':
                continue
            try:
                value = float(variance_inflation_factor(X_const.values, position))
            except Exception:  # noqa: BLE001
                value = float('nan')
            if np.isnan(value):
                label = '无法计算'
            elif value >= VIF_SERIOUS:
                label = '严重共线（建议剔除或合并）'
            elif value >= VIF_WARN:
                label = '存在共线（需留意）'
            else:
                label = '可接受'
            vif_rows.append({'变量': name, 'VIF': round(value, 3), '判读': label})

        # ---------- 残差与异常点 ----------
        residuals = np.asarray(model.resid, dtype=float)
        fitted = np.asarray(model.fittedvalues, dtype=float)
        influence = model.get_influence()
        std_resid = np.asarray(influence.resid_studentized_internal, dtype=float)
        cooks = np.asarray(influence.cooks_distance[0], dtype=float)
        leverage = np.asarray(influence.hat_matrix_diag, dtype=float)

        outlier_rows = []
        for position in range(n):
            flags = []
            if abs(std_resid[position]) > OUTLIER_STD_RESID:
                flags.append(f'标准化残差 {std_resid[position]:.2f}')
            if cooks[position] > COOKS_D_THRESHOLD:
                flags.append(f"Cook's D {cooks[position]:.3f}（强影响点）")
            if (leverage[position] > LEVERAGE_FACTOR * (p + 1) / n
                    and abs(std_resid[position]) > LEVERAGE_RESID_GATE):
                flags.append(f'高杠杆值 {leverage[position]:.3f} 且残差偏大')
            if flags:
                outlier_rows.append({
                    '行号': int(frame.index[position]) + 1,
                    '实测值': round(float(y.iloc[position]), 3),
                    '预测值': round(float(fitted[position]), 3),
                    '残差': round(float(residuals[position]), 3),
                    '判定依据': '；'.join(flags),
                })
        outlier_rows.sort(key=lambda item: abs(item['残差']), reverse=True)

        # ---------- 假设检验 ----------
        try:
            bp_stat, bp_p, _, _ = het_breuschpagan(residuals, X_const)
            bp = {'stat': round(float(bp_stat), 4), 'p': float(bp_p)}
        except Exception:  # noqa: BLE001
            bp = None
        try:
            dw = float(sm.stats.durbin_watson(residuals))
        except Exception:  # noqa: BLE001
            dw = float('nan')
        skew = float(stats.skew(y, bias=False)) if n > 2 else 0.0

        # ---------- 文字警示 ----------
        alerts: List[str] = []
        if n < MIN_N_PER_PREDICTOR * (p + 1):
            alerts.append(f'样本量不足：有效 n={n}，而变量数为 {p}。'
                          f'经验法则要求每个预测变量至少 {MIN_N_PER_PREDICTOR} 例'
                          f'（约需 {MIN_N_PER_PREDICTOR * (p + 1)} 例），'
                          '当前结果容易过拟合，系数与 p 值都不可靠，建议谨慎解读。')
        elif n < 2 * MIN_N_PER_PREDICTOR * (p + 1):
            alerts.append(f'样本量偏少：有效 n={n}，变量 {p} 个。虽已达到'
                          f'「每变量 {MIN_N_PER_PREDICTOR} 例」的最低要求，'
                          '但建模通常建议每变量 15–20 例；若用于建立预测模型'
                          '（而非仅校正混杂），应做内部验证或扩大样本。')
        serious = [row['变量'] for row in vif_rows if row['VIF'] >= VIF_SERIOUS]
        if serious:
            alerts.append('存在严重共线性（VIF ≥ 10）：' + '、'.join(serious) +
                          '。这些变量的系数与 p 值不稳定，建议只保留其中一个，'
                          '或改用主成分 / 岭回归。')
        elif any(row['VIF'] >= VIF_WARN for row in vif_rows):
            alerts.append('部分变量 VIF ≥ 5，存在中度共线性，解读系数时需留意。')
        if bp and bp['p'] < 0.05:
            alerts.append(f"Breusch-Pagan 检验 p={bp['p']:.4f} < 0.05："
                          '残差方差不齐（异方差），系数估计仍无偏但标准误可能偏小，'
                          'p 值可能偏乐观。可尝试对结局做对数转换或使用稳健标准误。')
        if not np.isnan(dw) and (dw < 1.5 or dw > 2.5):
            alerts.append(f'Durbin-Watson = {dw:.2f}（理想约 2）：'
                          '残差可能存在自相关。若数据按时间顺序排列（如同一患者的多次测量），'
                          '应改用重复测量模型（LMM / GEE）。')
        if abs(skew) > 1:
            alerts.append(f'结局变量偏度 = {skew:.2f}（|偏度| > 1 属明显偏态）：'
                          '线性回归假设残差正态，偏态结局可考虑对数转换后重做。')
        if outlier_rows:
            alerts.append(f'检出 {len(outlier_rows)} 个可疑观测点（见异常点表）：'
                          '请先核对是否为录入错误；确认无误时可做「剔除后重跑」的敏感性分析。')
        if n < 30:
            alerts.append('有效样本不足 30，所有推断均不稳定，仅建议作探索性参考。')

        if abs(skew) > 1:
            alerts.append('提示：结局偏态时可用「结局取对数」后再回归，'
                          '结果解释为「自变量每增加 1 单位，结局的对数平均变化 β」。')

        return {
            'ok': True,
            'n': n, 'n_input': prepared['n_input'],
            'n_dropped': prepared['n_dropped'],
            'n_predictors': p,
            'outcome': outcome,
            'r2': round(float(model.rsquared), 4),
            'adj_r2': round(float(model.rsquared_adj), 4),
            'f_stat': round(float(model.fvalue), 4),
            'f_p': float(model.f_pvalue),
            'aic': round(float(model.aic), 2),
            'sigma': round(float(np.sqrt(model.mse_resid)), 4),
            'dw': round(dw, 3) if not np.isnan(dw) else None,
            'bp': bp,
            'skew': round(skew, 3),
            'coef_table': coef_rows,
            'vif': vif_rows,
            'outliers': outlier_rows[:20],
            'outlier_count': len(outlier_rows),
            'notes': prepared['notes'],
            'alerts': alerts,
            'formula': LinearModel._describe(outcome, predictors, prepared),
            'y': y, 'fitted': fitted, 'residuals': residuals,
            'std_resid': std_resid,
        }

    @staticmethod
    def _describe(outcome: str, predictors: List[str], prepared: Dict) -> str:
        parts = []
        for name in predictors:
            if name in prepared['continuous']:
                parts.append(name)
            else:
                reference = next((note.split('参照水平 = ')[-1]
                                  for note in prepared['notes']
                                  if note.startswith(f'{name}：')), '')
                parts.append(f'{name}（参照：{reference}）' if reference else name)
        return f"{outcome} ~ " + ' + '.join(parts)

    # ---------- 诊断图 ----------

    @staticmethod
    def residual_figure(result: Dict) -> Optional[bytes]:
        """残差诊断三联图：Q-Q、残差-拟合、残差直方图。"""
        if not result.get('ok'):
            return None
        residuals = result['residuals']
        fitted = result['fitted']
        std_resid = result['std_resid']

        figure, axes = plt.subplots(1, 3, figsize=(13.5, 4.0))
        stats.probplot(residuals, dist='norm', plot=axes[0])
        axes[0].set_title('正态 Q-Q 图（点贴近直线 = 残差近正态）', fontsize=11)
        axes[0].set_xlabel('理论分位数')
        axes[0].set_ylabel('样本分位数')

        axes[1].scatter(fitted, std_resid, s=18, alpha=0.6, color='#3B82F6',
                        edgecolors='none')
        axes[1].axhline(0, color='#6B7280', linewidth=1)
        axes[1].axhline(2, color='#EF4444', linewidth=0.8, linestyle='--')
        axes[1].axhline(-2, color='#EF4444', linewidth=0.8, linestyle='--')
        axes[1].set_title('残差 vs 预测值（应无喇叭形 / 弯曲）', fontsize=11)
        axes[1].set_xlabel('预测值')
        axes[1].set_ylabel('标准化残差')

        axes[2].hist(residuals, bins=min(20, max(5, len(residuals) // 5)),
                     color='#10B981', alpha=0.75, edgecolor='white')
        axes[2].set_title('残差分布', fontsize=11)
        axes[2].set_xlabel('残差')
        axes[2].set_ylabel('频数')

        figure.tight_layout()
        buffer = io.BytesIO()
        figure.savefig(buffer, format='png', dpi=300, bbox_inches='tight')
        plt.close(figure)
        return buffer.getvalue()

    # ---------- 导出 ----------

    @staticmethod
    def coef_frame(result: Dict) -> pd.DataFrame:
        return pd.DataFrame(result.get('coef_table') or [])

    @staticmethod
    def vif_frame(result: Dict) -> pd.DataFrame:
        return pd.DataFrame(result.get('vif') or [])

    @staticmethod
    def outlier_frame(result: Dict) -> pd.DataFrame:
        return pd.DataFrame(result.get('outliers') or [])

    @staticmethod
    def coef_csv(result: Dict) -> bytes:
        frame = LinearModel.coef_frame(result)
        return frame.to_csv(index=False).encode('utf-8-sig')
