"""
时间序列预测：ARIMA（自动选阶）、Holt 线性趋势、Holt-Winters（含季节性），
并始终与「朴素法（下期 = 本期）」比较。

设计要点：
1. 先做留出验证（最后 20% 时间点不参与训练），用真实值计算 RMSE / MAE / MAPE，
   避免只看拟合优度造成的乐观误判；
2. 只有模型优于朴素法才值得采用——否则说明序列没有可利用的模式；
3. 结论用全部数据重新拟合后给出未来若干期的预测值与区间。
"""
import io
import warnings
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

METHOD_LABELS = {
    'auto': '自动选择（比较各方法后取最优）',
    'arima': 'ARIMA（自回归积分移动平均，自动选阶）',
    'holt': 'Holt 线性趋势（无季节性）',
    'holt_winters': 'Holt-Winters（含季节性）',
    'naive': '朴素法（下期 = 本期，作基准）',
}

MIN_POINTS = 8


class TimeSeriesForecast:
    """时间序列建模与预测。"""

    # ---------- 数据与检验 ----------
    @staticmethod
    def _prepare(values) -> np.ndarray:
        series = pd.to_numeric(pd.Series(values), errors='coerce').dropna()
        return series.to_numpy(dtype=float)

    @staticmethod
    def stationarity(values) -> Dict:
        """ADF 单位根检验：判断序列是否平稳（决定 ARIMA 是否需要差分）。"""
        from statsmodels.tsa.stattools import adfuller

        data = TimeSeriesForecast._prepare(values)
        if len(data) < MIN_POINTS:
            return {'error': f'至少需要 {MIN_POINTS} 个时间点才能做平稳性检验'}
        try:
            statistic, p_value, used_lag, nobs, critical, _ = adfuller(data, autolag='AIC')
        except Exception as error:
            return {'error': f'ADF 检验失败：{error}'}
        stationary = bool(p_value < 0.05)
        return {
            'statistic': round(float(statistic), 4),
            'p_value': round(float(p_value), 4),
            'used_lag': int(used_lag),
            'nobs': int(nobs),
            'stationary': stationary,
            'critical': {key: round(float(value), 4) for key, value in critical.items()},
            'display': (f'ADF 检验：统计量 {statistic:.4f}，p = {p_value:.4f} → '
                        + ('序列平稳（无需差分）' if stationary
                           else '存在单位根（不平稳，ARIMA 会自动差分）')),
        }

    # ---------- 各方法拟合 ----------
    @staticmethod
    def _fit_arima(train: np.ndarray, order: Optional[tuple] = None):
        from statsmodels.tsa.arima.model import ARIMA

        if order is not None:
            return order, ARIMA(train, order=order).fit()
        best = None
        for p in range(0, 3):
            for d in range(0, 2):
                for q in range(0, 3):
                    if len(train) < 15 and (p + q) > 2:
                        continue
                    try:
                        fitted = ARIMA(train, order=(p, d, q)).fit()
                    except Exception:
                        continue
                    score = float(fitted.aic)
                    if np.isfinite(score) and (best is None or score < best[2]):
                        best = ((p, d, q), fitted, score)
        if best is None:
            raise ValueError('ARIMA 的所有候选阶数都拟合失败（序列可能过短或方差为 0）')
        return best[0], best[1]

    @staticmethod
    def _fit_exponential(train: np.ndarray, method: str, seasonal_periods: int):
        from statsmodels.tsa.holtwinters import ExponentialSmoothing

        if method == 'holt':
            model = ExponentialSmoothing(train, trend='add', seasonal=None,
                                         initialization_method='estimated')
        else:
            if len(train) < 2 * seasonal_periods:
                raise ValueError(f'Holt-Winters 需要至少 {2 * seasonal_periods} 个时间点'
                                 f'（当前 {len(train)} 个），可调小季节周期')
            model = ExponentialSmoothing(train, trend='add', seasonal='add',
                                         seasonal_periods=seasonal_periods,
                                         initialization_method='estimated')
        return model.fit()

    # ---------- 误差指标 ----------
    @staticmethod
    def _errors(actual: np.ndarray, predicted: np.ndarray) -> Optional[Dict]:
        actual = np.asarray(actual, dtype=float)
        predicted = np.asarray(predicted, dtype=float)
        mask = np.isfinite(actual) & np.isfinite(predicted)
        if not mask.any():
            return None
        a, p = actual[mask], predicted[mask]
        rmse = float(np.sqrt(np.mean((a - p) ** 2)))
        mae = float(np.mean(np.abs(a - p)))
        nonzero = np.abs(a) > 1e-9
        mape = (float(np.mean(np.abs((a[nonzero] - p[nonzero]) / a[nonzero])) * 100)
                if nonzero.any() else None)
        denominator = float(np.sum((a - a.mean()) ** 2))
        r_squared = (1 - float(np.sum((a - p) ** 2)) / denominator) if denominator > 0 else None
        return {
            'RMSE': round(rmse, 4),
            'MAE': round(mae, 4),
            'MAPE': round(mape, 2) if mape is not None else None,
            'R²': round(r_squared, 4) if r_squared is not None else None,
        }

    # ---------- 主流程 ----------
    @classmethod
    def forecast(cls, values, periods: int = 6, method: str = 'auto',
                 seasonal_periods: int = 12, holdout_ratio: float = 0.2,
                 labels: Optional[List] = None) -> Dict:
        """留出验证 → 选出最优方法 → 用全部数据拟合 → 预测未来若干期。"""
        data = cls._prepare(values)
        if len(data) < MIN_POINTS:
            return {'error': f'至少需要 {MIN_POINTS} 个时间点才能做预测（当前 {len(data)} 个）'}
        if not 1 <= periods <= 60:
            return {'error': '预测期数需在 1 到 60 之间'}
        if np.ptp(data) == 0:
            return {'error': '序列取值没有变化，无法预测'}

        method = method if method in METHOD_LABELS else 'auto'
        holdout = max(3, int(round(len(data) * holdout_ratio))) if len(data) >= 12 else 0
        if holdout and holdout >= len(data) - 3:
            holdout = 0
        train = data[:-holdout] if holdout else data
        actual = data[-holdout:] if holdout else np.array([])

        candidates: List[str] = []
        if method == 'auto':
            candidates = ['arima', 'holt']
            if len(train) >= 2 * seasonal_periods and seasonal_periods >= 2:
                candidates.append('holt_winters')
            candidates.append('naive')
        else:
            candidates = [method]

        evaluations: List[Dict] = []
        fitted_cache: Dict[str, Dict] = {}
        for name in candidates:
            try:
                if name == 'arima':
                    order, fitted = cls._fit_arima(train)
                    prediction = np.asarray(fitted.forecast(holdout or periods), dtype=float)
                    detail = f'ARIMA{order}'
                    aic = float(fitted.aic)
                elif name in ('holt', 'holt_winters'):
                    fitted = cls._fit_exponential(train, name, seasonal_periods)
                    prediction = np.asarray(fitted.forecast(holdout or periods), dtype=float)
                    detail = 'Holt 线性趋势' if name == 'holt' else f'Holt-Winters（周期 {seasonal_periods}）'
                    aic = float(getattr(fitted, 'aic', np.nan))
                elif name == 'naive':
                    fitted = None
                    prediction = np.repeat(train[-1], holdout or periods)
                    detail = '朴素法（下期 = 本期）'
                    aic = float('nan')
                else:
                    continue
            except Exception as error:
                evaluations.append({'方法': METHOD_LABELS.get(name, name), '说明': detail if 'detail' in dir() else '',
                                    'RMSE': '—', 'MAE': '—', 'MAPE': '—', 'R²': '—',
                                    '备注': f'拟合失败：{error}'})
                continue

            metrics = cls._errors(actual, prediction) if holdout else None
            evaluations.append({
                '方法': METHOD_LABELS.get(name, name),
                '说明': detail,
                'RMSE': metrics['RMSE'] if metrics else '—',
                'MAE': metrics['MAE'] if metrics else '—',
                'MAPE': metrics['MAPE'] if metrics else '—',
                'R²': metrics['R²'] if metrics else '—',
                'AIC': round(aic, 2) if np.isfinite(aic) else '—',
                '备注': '' if holdout else '未做留出验证（时间点过少）',
            })
            fitted_cache[name] = {'fitted': fitted, 'detail': detail}

        usable = [item for item in evaluations if item['RMSE'] != '—' and not item['备注'].startswith('拟合失败')]
        if not usable:
            return {'error': '所有方法都未能完成留出验证，请检查序列长度或调整方法'}

        if holdout:
            best = min(usable, key=lambda item: float(item['RMSE']))
        else:
            ranked = [item for item in evaluations if item.get('AIC') != '—' and item['AIC'] != '—']
            best = (min(ranked, key=lambda item: float(item['AIC'])) if ranked else evaluations[0])
        best_label = best['方法']
        best_name = next((name for name, label in METHOD_LABELS.items() if label == best_label), 'arima')

        naive_row = next((item for item in evaluations if item['方法'] == METHOD_LABELS['naive']), None)
        beats_naive = None
        if naive_row and naive_row['RMSE'] != '—' and best['RMSE'] != '—':
            beats_naive = float(best['RMSE']) < float(naive_row['RMSE'])

        # 用全部数据重新拟合最优方法，预测未来
        try:
            if best_name == 'arima':
                cached = fitted_cache.get('arima', {}).get('detail', 'ARIMA')
                order = None
                if cached.startswith('ARIMA') and len(cached) > 5:
                    order = tuple(int(part) for part in cached[5:].strip('()').split(','))
                order, fitted_full = cls._fit_arima(data, order=order)
                forecast_result = fitted_full.get_forecast(periods)
                mean = np.asarray(forecast_result.predicted_mean, dtype=float)
                interval = np.asarray(forecast_result.conf_int(alpha=0.05), dtype=float)
                lower, upper = interval[:, 0], interval[:, 1]
                interval_note = 'ARIMA 预测区间（95%）'
            elif best_name in ('holt', 'holt_winters'):
                fitted_full = cls._fit_exponential(data, best_name, seasonal_periods)
                mean = np.asarray(fitted_full.forecast(periods), dtype=float)
                residual_sd = float(np.std(np.asarray(fitted_full.resid, dtype=float), ddof=1))
                lower = mean - 1.96 * residual_sd
                upper = mean + 1.96 * residual_sd
                interval_note = '近似 95% 区间（按残差标准差估计）'
            else:  # naive
                mean = np.repeat(data[-1], periods)
                lower = upper = mean
                interval_note = '朴素法不提供区间'
        except Exception as error:
            return {'error': f'用全部数据重新拟合失败：{error}'}

        history_labels = [str(value) for value in (labels or [])]
        if len(history_labels) == len(data):
            future_labels = cls._next_labels(history_labels, periods)
        else:
            future_labels = [f'第 {len(data) + index + 1} 期' for index in range(periods)]

        table = pd.DataFrame({
            '预测期': future_labels,
            '预测值': [round(float(value), 4) for value in mean],
            '95%CI 下限': [round(float(value), 4) for value in lower],
            '95%CI 上限': [round(float(value), 4) for value in upper],
        })

        return {
            'table': table,
            'evaluation': pd.DataFrame(evaluations),
            'best_method': best_label,
            'best_method_code': best_name,
            'beats_naive': beats_naive,
            'holdout_size': holdout,
            'n_points': int(len(data)),
            'periods': periods,
            'interval_note': interval_note,
            'forecast_mean': [round(float(value), 4) for value in mean],
            'stationarity': cls.stationarity(data),
            'plot': cls._plot(data, mean, lower, upper, history_labels, future_labels,
                              best_label, holdout, actual),
            'display': (f'{best_label}：基于 {len(data)} 个时间点预测未来 {periods} 期；'
                        + (f'留出验证 RMSE={best["RMSE"]}、MAE={best["MAE"]}'
                           + (f'、MAPE={best["MAPE"]}%' if best["MAPE"] else '')
                           if holdout else '未做留出验证')
                        + ('；已优于朴素法基准' if beats_naive
                           else ('；未优于朴素法，说明该序列缺乏可利用的趋势/周期，'
                                 '建议只作参考' if beats_naive is False else ''))),
        }

    @staticmethod
    def _next_labels(history_labels: List[str], periods: int) -> List[str]:
        """尽量按历史标签的规律生成未来标签（如 2024-10 → 2024-11）。"""
        last = history_labels[-1]
        parsed = pd.to_datetime(last, errors='coerce')
        if pd.notna(parsed):
            frequency = 'M'
            if len(history_labels) >= 2:
                previous = pd.to_datetime(history_labels[-2], errors='coerce')
                if pd.notna(previous):
                    delta_days = (parsed - previous).days
                    frequency = 'Y' if delta_days > 300 else ('W' if 5 <= delta_days <= 9 else 'M')
            return [str((parsed + index * pd.tseries.frequencies.to_offset(frequency)).date())
                    for index in range(1, periods + 1)]
        return [f'第 {len(history_labels) + index + 1} 期' for index in range(periods)]

    @staticmethod
    def _plot(history: np.ndarray, mean: np.ndarray, lower: np.ndarray, upper: np.ndarray,
              history_labels: List[str], future_labels: List[str], method_label: str,
              holdout: int, actual_holdout: np.ndarray) -> bytes:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(8.4, 5.0), dpi=110)
        use_labels = len(history_labels) == len(history)
        positions = np.arange(len(history))
        ax.plot(positions, history, marker='o', markersize=4, color='#2563EB',
                linewidth=2, label='历史值')
        if holdout and len(actual_holdout):
            ax.plot(positions[-holdout:], actual_holdout, marker='s', markersize=4,
                    color='#16A34A', linewidth=1.6, linestyle='--', label='留出期实际值')
        future_positions = np.arange(len(history), len(history) + len(mean))
        ax.plot(future_positions, mean, marker='D', markersize=5, color='#EF4444',
                linewidth=2.2, label=f'预测（{method_label}）')
        ax.fill_between(future_positions, lower, upper, color='#EF4444', alpha=0.15,
                        label='95% 预测区间')
        ax.axvline(len(history) - 0.5, color='#94A3B8', linestyle=':', linewidth=1.2)
        ticks = list(positions) + list(future_positions)
        labels_all = (history_labels if use_labels else [str(index + 1) for index in positions]) + \
                     list(future_labels)
        if len(ticks) <= 24:
            ax.set_xticks(ticks)
            ax.set_xticklabels(labels_all, rotation=45, ha='right', fontsize=8)
        ax.set_ylabel('数值')
        ax.set_title('时间序列预测', fontsize=12, fontweight='bold')
        ax.legend(fontsize=9)
        ax.grid(alpha=0.3)
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()
