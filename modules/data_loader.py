"""
数据加载与字段映射模块
负责：Excel读取、字段类型推断、缺失值/异常值诊断、变量元数据管理
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional, Iterable
from dataclasses import dataclass, asdict
import re
import warnings
warnings.filterwarnings('ignore')


# ===== 脏值处理：把"带单位的数字"和"各种缺失写法"归一到可计算的值 =====

# 这些写法应视为缺失，而不是 0
LOOSE_NA_TOKENS = {
    '', '-', '--', '---', '—', '——', '－', '/', '\\', 'n/a', 'na', 'null',
    'none', 'nan', '无', '缺失', '不详', '未查', '未做', '未记录', '空', '暂无',
}

# 全角数字/符号 → 半角
_FULLWIDTH_MAP = {ord(c): ord(h) for c, h in zip(
    '０１２３４５６７８９．％＋－（）：，', '0123456789.%+-():,')}

# 取字符串中第一个数字（"28人"→28，"总人数23"→23，"5个月"→5）
_NUMBER_PATTERN = re.compile(r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')

# 宽表值列的常见命名（1月/一月/Q1/第一季度/2024年 等）
WIDE_VALUE_PATTERNS = (
    r'^\d{1,2}\s*月(_\d+)?$', r'^[一二三四五六七八九十]{1,3}\s*月(_\d+)?$',
    r'^[Qq][1-4]$', r'^第[一二三四]季度$', r'^\d{4}\s*年$',
    r'^\d{4}[-/年]\d{1,2}', r'^\d{1,2}\s*季度$',
)


@dataclass
class FieldMeta:
    """字段元数据"""
    name: str
    dtype: str            # 'numeric' | 'categorical' | 'datetime' | 'text' | 'binary'
    original_dtype: str   # pandas推断类型
    n_total: int
    n_missing: int
    missing_pct: float
    n_unique: int
    # numeric
    mean: Optional[float] = None
    std: Optional[float] = None
    median: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None
    q1: Optional[float] = None
    q3: Optional[float] = None
    skew: Optional[float] = None
    kurt: Optional[float] = None
    # categorical
    top_value: Optional[str] = None
    top_count: Optional[int] = None
    # 异常
    outlier_count: Optional[int] = None
    outlier_method: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


class DataLoader:
    """Excel数据加载与字段推断"""

    # 推断为分类变量的常见医学字段（中文）
    CATEGORY_HINTS = {
        '性别', 'sex', 'gender', '民族', '血型', '分型', '分期', '分级',
        '组别', '分组', 'group', 'treatment', '治疗', '是否', '是否阳性',
        '是否死亡', '是否复发', '是否转移', '阳性', '阴性', '部位', '侧别',
        '左右', '病理类型', '转移', '复发', '并发症', '基础疾病'
    }

    # ===== 结构角色识别用的关键词（数据洞察） =====
    # 时间维度：日期型字段，或按时间点铺开的宽表列
    TIME_NAME_HINTS = ('日期', '时间', 'date', 'time', '年份', '年度', '季度', '月份', '年月', '随访', '入院', '出院')
    # 二分类结局候选
    OUTCOME_HINTS = ('死亡', '复发', '转移', '感染', '发生', '阳性', '不良', '事件', '结局',
                     '转归', '并发症', 'outcome', 'death', 'event', 'mortality')
    # 计数型指标（例数/次数/人次）
    COUNT_HINTS = ('例数', '次数', '人次', '发生数', '数量', '例次', 'count')
    # 率 / 强度型指标（发生率、感染密度、构成比）
    RATE_HINTS = ('率', '密度', '构成比', '占比', '百分比', '千分', '‰')
    # 强度型指标的分母
    DENOMINATOR_HINTS = ('总例数', '留置', '总日', '导管日', '床日', '分母', '合计', '总人数', '总数')
    # 可做趋势的时间点序列（年份 / 季度 / 月份）
    TIME_SERIES_HINTS = ('年份', '年度', '季度', '月份', '年月')
    # 生存/随访时间：数值列且全部 > 0
    DURATION_HINTS = ('天', '日', '周', '月', '生存', '随访', '时长', '病程', 'time', 'duration')
    # 这些名字含时间字眼但不是随访时长（避免把「年龄」当成生存时间）
    DURATION_EXCLUDE = ('年龄', '年份', '年度', '月份', '季度', '日期', '出生')
    # 常见配对列命名
    PAIR_HINTS = (('治疗前', '治疗后'), ('术前', '术后'), ('入院', '出院'),
                  ('干预前', '干预后'), ('前', '后'), ('左', '右'))
    # 标识列命名（唯一值占比高时不参与统计）
    IDENTIFIER_HINTS = ('id', '编号', '编码', '病历号', '住院号', '序号')

    @staticmethod
    def loose_to_numeric(series: pd.Series) -> pd.Series:
        """
        宽松数值解析：剥离单位与全角字符，把「28人」「95.2%」「5个月」还原为数值，
        并把「-」「--」「无」「N/A」等写法视为缺失（NaN）。
        """
        def parse(value):
            if value is None:
                return np.nan
            if isinstance(value, bool):
                return np.nan
            if isinstance(value, (int, float, np.number)):
                return np.nan if pd.isna(value) else float(value)
            if isinstance(value, (pd.Timestamp, pd.Timedelta)):
                return np.nan
            text = str(value).strip().translate(_FULLWIDTH_MAP)
            if text.lower() in LOOSE_NA_TOKENS:
                return np.nan
            match = _NUMBER_PATTERN.search(text.replace(',', ''))
            if not match:
                return np.nan
            try:
                return float(match.group())
            except ValueError:
                return np.nan

        return series.map(parse)

    @classmethod
    def _classify_numeric(cls, numeric_series: pd.Series, col_lower: str) -> Optional[str]:
        """把已数值化的列判为 binary / categorical / numeric。"""
        valid = numeric_series.dropna()
        if valid.empty:
            return None
        n_unique = valid.nunique()
        if n_unique == 2:
            return 'binary'
        if n_unique <= 10 and any(h in col_lower for h in cls.CATEGORY_HINTS):
            return 'categorical'
        return 'numeric'

    @staticmethod
    def open_workbook(file) -> pd.ExcelFile:
        """打开Excel（支持上传的文件对象，可重复读取）。"""
        if hasattr(file, 'seek'):
            try:
                file.seek(0)
            except Exception:
                pass
        try:
            return pd.ExcelFile(file)
        except ImportError as e:
            raise ValueError(
                "缺少 Excel 读取引擎，无法解析该文件。请安装 xlrd（读 .xls）"
                f"与 openpyxl（读 .xlsx）后重试。原始错误：{e}"
            )
        except Exception as e:
            raise ValueError(f"无法读取Excel文件：{e}")

    @classmethod
    def list_sheets(cls, file) -> List[str]:
        """只列出 Sheet 名称（不读取数据，速度快）。"""
        return list(cls.open_workbook(file).sheet_names)

    @staticmethod
    def _dedup_columns(columns: Iterable) -> List[str]:
        """列名去重并保证非空。"""
        seen: Dict[str, int] = {}
        result = []
        for index, column in enumerate(columns):
            label = str(column).strip()
            if label in ('', 'nan', 'None', 'NaT'):
                label = f'列{index + 1}'
            if label in seen:
                seen[label] += 1
                label = f'{label}_{seen[label]}'
            else:
                seen[label] = 0
            result.append(label)
        return result

    @classmethod
    def read_sheet(cls, file, sheet_name: Optional[str] = None, header_rows: int = 1,
                   skip_rows: int = 0, fill_header_merged: bool = True,
                   fill_data_merged: bool = False,
                   preview_rows: Optional[int] = None) -> pd.DataFrame:
        """
        按导入设置读取一个 Sheet。

        header_rows  用作表头的行数，1=首行是字段名，2=前两行合并成字段名（多级表头），0=无表头
        skip_rows    表头之前先跳过的行数（例如报表大标题行）
        fill_header_merged  表头合并单元格：空白列名沿用左侧
        fill_data_merged    数据区合并单元格：空白值沿用上方（纵向合并）
        preview_rows 只读取前若干行用于预览
        """
        xls = cls.open_workbook(file)
        names = list(xls.sheet_names)
        if not names:
            raise ValueError("Excel 中没有任何 Sheet")
        name = sheet_name if sheet_name in names else names[0]

        raw = pd.read_excel(xls, sheet_name=name, header=None)
        raw = raw.dropna(how='all').dropna(axis=1, how='all').reset_index(drop=True)
        if raw.empty:
            raise ValueError(f"Sheet「{name}」中没有有效数据")

        if fill_data_merged:
            raw = raw.ffill()

        skip_rows = max(0, int(skip_rows or 0))
        if skip_rows:
            raw = raw.iloc[skip_rows:].reset_index(drop=True)
        if raw.empty:
            raise ValueError(f"Sheet「{name}」在跳过前 {skip_rows} 行后没有数据")

        header_rows = max(0, int(header_rows or 0))
        if header_rows == 0:
            columns = [f'列{i + 1}' for i in range(raw.shape[1])]
            data = raw
        else:
            header_block = raw.iloc[:header_rows]
            if fill_header_merged:
                header_block = header_block.ffill(axis=1)
            columns = []
            for index in range(raw.shape[1]):
                parts: List[str] = []
                for value in header_block.iloc[:, index].tolist():
                    text = '' if pd.isna(value) else str(value).strip()
                    if text and text not in parts:
                        parts.append(text)
                columns.append(' | '.join(parts) if parts else f'列{index + 1}')
            data = raw.iloc[header_rows:]

        # 先落列名（列数与 raw 一致），再清理空行空列，避免列名与位置错位
        data = data.reset_index(drop=True)
        data.columns = cls._dedup_columns(columns)
        data = data.dropna(how='all').dropna(axis=1, how='all')
        # 表头行留在原始数据里会让整列退化成 object，这里做一次类型回填
        data = data.infer_objects()

        if preview_rows:
            data = data.head(int(preview_rows))
        data = data.reset_index(drop=True)
        data.attrs['sheet_name'] = name
        return data

    @classmethod
    def load_excel(cls, file, sheet_name: Optional[str] = None,
                   **kwargs) -> Tuple[Dict[str, pd.DataFrame], str]:
        """
        读取Excel全部Sheet（兼容旧接口）
        返回: (sheet字典, 默认sheet名)
        """
        names = cls.list_sheets(file)
        sheets = {}
        for name in names:
            try:
                df = cls.read_sheet(file, name, **kwargs)
            except Exception:
                continue
            if len(df) > 0:
                sheets[name] = df

        if not sheets:
            raise ValueError("Excel中无有效数据")

        default = sheet_name if sheet_name and sheet_name in sheets else list(sheets.keys())[0]
        return sheets, default

    @classmethod
    def infer_field_type(cls, series: pd.Series, col_name: str) -> str:
        """
        推断字段业务类型
        返回: numeric | categorical | binary | datetime | text
        """
        col_lower = str(col_name).lower().strip()

        # 0. 标识列（ID/编号/病历号/序号）：唯一值占比高时不参与统计分析
        if any(k in col_lower for k in ['id', '编号', '编码', '病历号', '住院号', '序号']):
            non_null = series.dropna()
            if len(non_null) > 0 and non_null.nunique() / len(non_null) > 0.8:
                return 'text'

        # 1. 日期字段
        # 排除明显是ID的字段
        if any(k in col_lower for k in ['id', '编号', '编码', '病历号', '住院号']):
            pass  # ID不当作日期
        elif any(k in col_lower for k in ['日期', '时间', 'date', 'time', '入院', '出院', '手术', '随访', 'birth']):
            # 纯数值列不是日期：pandas 会把整数解释成纳秒时间戳而不报错，
            # 使「随访月数=3」「入院天数=7」这类列被误判为日期。
            # 唯一例外是 8 位数字型日期（如 20240101）。
            if pd.api.types.is_numeric_dtype(series):
                numeric_values = pd.to_numeric(series, errors='coerce').dropna()
                looks_like_yyyymmdd = (
                    len(numeric_values) > 0
                    and (numeric_values % 1 == 0).all()
                    and bool(numeric_values.between(19000101, 21001231).all())
                )
                if not looks_like_yyyymmdd:
                    return cls._classify_numeric(numeric_values, col_lower) or 'numeric'
            try:
                pd.to_datetime(series.dropna().head(20), errors='raise')
                return 'datetime'
            except Exception:
                pass

        # 1b. pandas 已确认是数值类型的列：缺失再多也仍然是数值列
        #（稀疏的「1月…12月」列曾因此被误判为分类）
        if pd.api.types.is_numeric_dtype(series):
            return cls._classify_numeric(series, col_lower) or 'numeric'

        # 2. 数值字段（原值本身即可转数值，最可信）
        numeric_series = pd.to_numeric(series, errors='coerce')
        valid_ratio = numeric_series.notna().sum() / max(len(series), 1)
        if valid_ratio > 0.9:
            classified = cls._classify_numeric(numeric_series, col_lower)
            if classified:
                return classified

        # 2b. 宽松数值：剥离单位/全角字符后以数值为主（如「28人」「95.2%」「5个月」）
        loose_series = cls.loose_to_numeric(series)
        loose_ratio = loose_series.notna().sum() / max(len(series), 1)
        if loose_ratio > 0.9:
            classified = cls._classify_numeric(loose_series, col_lower)
            if classified:
                return classified

        # 3. 字段名暗示分类
        if any(h in col_lower for h in DataLoader.CATEGORY_HINTS):
            return 'categorical'

        # 4. 唯一值数判断
        n_unique = series.nunique()
        total = len(series.dropna())
        if total > 0:
            unique_ratio = n_unique / total
            # 占比低视为分类
            if n_unique <= 20 or unique_ratio < 0.05:
                # 进一步判断：是否像分类
                if n_unique <= 50:
                    return 'categorical'

        # 5. 默认
        return 'text'

    @classmethod
    def build_metadata(cls, df: pd.DataFrame,
                       overrides: Optional[Dict[str, str]] = None) -> Dict[str, FieldMeta]:
        """构建所有字段的元数据（overrides 为人工指定的字段类型）"""
        overrides = overrides or {}
        metas = {}
        for col in df.columns:
            meta = cls._build_one_field(df[col], col, forced_type=overrides.get(col))
            metas[col] = meta
        return metas

    @classmethod
    def _build_one_field(cls, series: pd.Series, col_name: str,
                         forced_type: Optional[str] = None) -> FieldMeta:
        """构建单字段元数据"""
        original_dtype = str(series.dtype)
        business_type = forced_type or cls.infer_field_type(series, col_name)
        n_total = len(series)
        n_missing = series.isna().sum()
        missing_pct = round(n_missing / max(n_total, 1) * 100, 2)
        n_unique = series.nunique()

        meta = FieldMeta(
            name=col_name,
            dtype=business_type,
            original_dtype=original_dtype,
            n_total=int(n_total),
            n_missing=int(n_missing),
            missing_pct=missing_pct,
            n_unique=int(n_unique),
        )

        if business_type == 'numeric':
            num = pd.to_numeric(series, errors='coerce')
            # 带单位/全角字符的列（如「28人」）用宽松解析兜底
            if num.notna().sum() < 3:
                num = cls.loose_to_numeric(series)
            num = num.dropna()
            if len(num) > 0:
                meta.mean = float(num.mean())
                meta.std = float(num.std())
                meta.median = float(num.median())
                meta.min = float(num.min())
                meta.max = float(num.max())
                meta.q1 = float(num.quantile(0.25))
                meta.q3 = float(num.quantile(0.75))
                meta.skew = float(num.skew()) if len(num) > 2 else None
                meta.kurt = float(num.kurt()) if len(num) > 3 else None
                # IQR异常值
                iqr = meta.q3 - meta.q1
                if iqr > 0:
                    lower = meta.q1 - 1.5 * iqr
                    upper = meta.q3 + 1.5 * iqr
                    outliers = num[(num < lower) | (num > upper)]
                    meta.outlier_count = int(len(outliers))
                    meta.outlier_method = 'IQR(1.5x)'

        elif business_type in ('categorical', 'binary'):
            vc = series.dropna().astype(str).value_counts()
            if len(vc) > 0:
                meta.top_value = str(vc.index[0])
                meta.top_count = int(vc.iloc[0])

        return meta

    @classmethod
    def apply_overrides(cls, df: pd.DataFrame,
                        overrides: Optional[Dict[str, str]] = None) -> pd.DataFrame:
        """
        按人工指定的类型实际改写数据列：
        numeric → 宽松数值解析；datetime → 日期解析；其余保持文本。
        """
        result = df.copy()
        for col, target in (overrides or {}).items():
            if col not in result.columns:
                continue
            if target == 'numeric':
                result[col] = cls.loose_to_numeric(result[col])
            elif target == 'datetime':
                result[col] = pd.to_datetime(result[col], errors='coerce')
            elif target in ('categorical', 'binary', 'text'):
                result[col] = result[col].astype(object)
        return result

    @classmethod
    def get_quality_report(cls, df: pd.DataFrame,
                           overrides: Optional[Dict[str, str]] = None) -> Dict:
        """数据质量报告（overrides 为人工指定的字段类型）"""
        overrides = {k: v for k, v in (overrides or {}).items() if k in df.columns}
        if overrides:
            df = cls.apply_overrides(df, overrides)
        metas = cls.build_metadata(df, overrides=overrides)
        report = {
            'n_rows': len(df),
            'n_cols': len(df.columns),
            'total_cells': len(df) * len(df.columns),
            'missing_cells': int(df.isna().sum().sum()),
            'missing_pct': round(df.isna().sum().sum() / max(len(df) * len(df.columns), 1) * 100, 2),
            'duplicate_rows': int(df.duplicated().sum()),
            'fields': {name: m.to_dict() for name, m in metas.items()},
            'summary': {
                'numeric_count': sum(1 for m in metas.values() if m.dtype == 'numeric'),
                'categorical_count': sum(1 for m in metas.values() if m.dtype == 'categorical'),
                'binary_count': sum(1 for m in metas.values() if m.dtype == 'binary'),
                'datetime_count': sum(1 for m in metas.values() if m.dtype == 'datetime'),
                'text_count': sum(1 for m in metas.values() if m.dtype == 'text'),
            },
            'type_overrides': dict(overrides),
            'sheet_name': df.attrs.get('sheet_name'),
        }
        return report

    @staticmethod
    def detect_outliers_iqr(series: pd.Series) -> np.ndarray:
        """IQR法检测异常值下标"""
        num = pd.to_numeric(series, errors='coerce').dropna()
        if len(num) < 4:
            return np.array([])
        q1, q3 = num.quantile(0.25), num.quantile(0.75)
        iqr = q3 - q1
        lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        mask = (num < lower) | (num > upper)
        return num[mask].index.values

    @staticmethod
    def clean_data(df: pd.DataFrame, drop_duplicates: bool = True,
                   outlier_strategy: str = 'keep') -> pd.DataFrame:
        """
        数据清洗
        outlier_strategy: 'keep' | 'winsorize' | 'drop'
        """
        cleaned = df.copy()
        if drop_duplicates:
            cleaned = cleaned.drop_duplicates()

        if outlier_strategy != 'keep':
            for col in cleaned.select_dtypes(include=[np.number]).columns:
                num = pd.to_numeric(cleaned[col], errors='coerce')
                q1, q3 = num.quantile(0.25), num.quantile(0.75)
                iqr = q3 - q1
                lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                if outlier_strategy == 'winsorize':
                    cleaned[col] = num.clip(lower, upper)
                elif outlier_strategy == 'drop':
                    mask = (num < lower) | (num > upper) | num.isna()
                    cleaned = cleaned[~mask]

        return cleaned.reset_index(drop=True)

    @staticmethod
    def guess_wide_value_columns(df: pd.DataFrame,
                                 exclude: Optional[Iterable] = None) -> List[str]:
        """猜出宽表中的「值列」，例如 1月…12月、Q1…Q4、2021年…2025年。"""
        exclude = set(exclude or [])
        guessed = []
        for col in df.columns:
            if col in exclude:
                continue
            label = str(col).strip()
            if any(re.match(pattern, label) for pattern in WIDE_VALUE_PATTERNS):
                guessed.append(col)
        return guessed

    @classmethod
    def melt_wide_to_long(cls, df: pd.DataFrame, id_cols: Iterable, value_cols: Iterable,
                          var_name: str = '指标名称', value_name: str = '数值',
                          drop_missing: bool = True,
                          numeric_convert: bool = True) -> pd.DataFrame:
        """
        宽表转长表（逆透视）：把「1月…12月」这类横向铺开的列，折叠成
        「指标名称 + 数值」两列，转换后每个组合占一行，才能做趋势和组间比较。
        """
        id_cols = list(id_cols or [])
        value_cols = list(value_cols or [])
        if not value_cols:
            raise ValueError('请至少选择一个需要折叠的值列')
        if not id_cols:
            raise ValueError('请至少选择一个标识列（用来区分每条记录）')
        missing = [c for c in id_cols + value_cols if c not in df.columns]
        if missing:
            raise ValueError('字段不存在：' + '、'.join(map(str, missing)))

        long_df = df.melt(id_vars=id_cols, value_vars=value_cols,
                          var_name=var_name, value_name=value_name)
        if numeric_convert:
            long_df[value_name] = cls.loose_to_numeric(long_df[value_name])
        if drop_missing:
            long_df = long_df.dropna(subset=[value_name])
        return long_df.reset_index(drop=True)

    # ===== 结构角色识别与「数据洞察」推荐 =====

    @classmethod
    def as_field_metas(cls, df: pd.DataFrame,
                       metas: Optional[Dict] = None) -> Dict[str, 'FieldMeta']:
        """把「FieldMeta 或普通 dict」形式的元数据统一成 FieldMeta。"""
        result: Dict[str, FieldMeta] = {}
        for name, meta in (metas or {}).items():
            if isinstance(meta, FieldMeta):
                result[name] = meta
            else:
                fields = {key: meta.get(key) for key in FieldMeta.__dataclass_fields__}
                result[name] = FieldMeta(**fields)
        return result or cls.build_metadata(df)

    @classmethod
    def is_identifier_column(cls, name, series: pd.Series) -> bool:
        """是否为标识列（名称含 ID/编号/序号，且唯一值占比高）。"""
        lower = str(name).lower()
        if not any(hint in lower for hint in cls.IDENTIFIER_HINTS):
            return False
        non_null = series.dropna()
        return len(non_null) > 0 and non_null.nunique() / len(non_null) > 0.8

    @classmethod
    def profile_structure(cls, df: pd.DataFrame,
                          metas: Optional[Dict[str, FieldMeta]] = None) -> Dict:
        """
        识别数据的结构角色，并给出「能做哪些分析 / 为什么做不了」。
        判据只来自数据结构与字段语义，不预设科室或业务场景。
        """
        metas = cls.as_field_metas(df, metas)
        roles: Dict[str, list] = {
            'identifier': [], 'numeric': [], 'categorical': [], 'binary': [],
            'time_fields': [], 'duration': [], 'grouping': [], 'outcome_binary': [],
            'count': [], 'rate': [], 'denominator': [], 'paired': [], 'wide_value_columns': [],
        }
        reasons: Dict[str, list] = {}

        def note(role: str, text: str) -> None:
            reasons.setdefault(role, []).append(text)

        for name in df.columns:
            meta = metas.get(name)
            if meta is None:
                continue
            series = df[name]
            lower = str(name).lower()

            if cls.is_identifier_column(name, series):
                roles['identifier'].append(name)
                note('identifier', f'{name}：名称含 ID/编号/序号，唯一值占比 > 80%')
                continue

            if meta.dtype == 'numeric':
                roles['numeric'].append(name)
            elif meta.dtype in ('categorical', 'binary'):
                roles['categorical'].append(name)
            if meta.dtype == 'binary':
                roles['binary'].append(name)

            if meta.dtype == 'datetime':
                roles['time_fields'].append(name)
                note('time_fields', f'{name}：日期型字段')
            elif any(hint in lower for hint in cls.TIME_NAME_HINTS) and 2 <= meta.n_unique <= 60:
                roles['time_fields'].append(name)
                note('time_fields', f'{name}：名称含时间语义，取值 {meta.n_unique} 个')

            if meta.dtype in ('categorical', 'binary') and 2 <= meta.n_unique <= 10:
                roles['grouping'].append(name)
                note('grouping', f'{name}：{meta.n_unique} 个取值，可作分组')

            if meta.dtype == 'binary' and any(hint in lower for hint in cls.OUTCOME_HINTS):
                roles['outcome_binary'].append(name)
                note('outcome_binary', f'{name}：0/1 且名称含结局语义')

            if meta.dtype == 'numeric':
                values = pd.to_numeric(series, errors='coerce').dropna()
                if len(values) > 0:
                    if (values >= 0).all() and (values % 1 == 0).all() and any(
                            hint in lower for hint in cls.COUNT_HINTS):
                        roles['count'].append(name)
                        note('count', f'{name}：非负整数且名称含例数/次数语义')
                    if any(hint in lower for hint in cls.RATE_HINTS):
                        roles['rate'].append(name)
                        note('rate', f'{name}：名称含率 / 密度语义')
                    if ((values > 0).all() and values.max() > 3
                            and any(hint in lower for hint in cls.DURATION_HINTS)
                            and not any(hint in lower for hint in cls.DURATION_EXCLUDE)
                            and not any(re.match(pattern, str(name).strip())
                                        for pattern in WIDE_VALUE_PATTERNS)):
                        roles['duration'].append(name)
                        note('duration', f'{name}：正值且名称含随访/时长语义')
                    if any(hint in lower for hint in cls.DENOMINATOR_HINTS):
                        roles['denominator'].append(name)
                        note('denominator', f'{name}：名称含总数/留置日等分母语义')

        # 报表型结构：指标名集中在某个分类列的取值里（如「指标」列含 VAP发生率 / 例次数）
        metric_columns = []
        rate_words = '|'.join(cls.RATE_HINTS + cls.COUNT_HINTS)
        candidate_columns = [name for name, meta in metas.items()
                             if meta.dtype in ('categorical', 'text') and name in df.columns
                             and 3 <= meta.n_unique <= 500]
        for name in candidate_columns:
            values = df[name].dropna().astype(str)
            if len(values) < 5:
                continue
            hit = int(values.str.contains(rate_words, regex=True).sum())
            if hit >= 3 and hit / len(values) >= 0.3:
                metric_columns.append(name)
        roles['metric_column'] = metric_columns
        if metric_columns:
            note('metric_column', '指标名集中在列：' + '、'.join(str(c) for c in metric_columns[:3])
                 + '（取值中含 发生率 / 例数 等）')

        roles['wide_value_columns'] = cls.guess_wide_value_columns(df)
        if len(roles['wide_value_columns']) >= 3:
            note('wide_value_columns', '列名可按时间解析：'
                 + '、'.join(str(c) for c in roles['wide_value_columns'][:8]) + ' …')

        for before_hint, after_hint in cls.PAIR_HINTS:
            afters = [c for c in df.columns if after_hint in str(c)]
            for before in [c for c in df.columns if before_hint in str(c)]:
                suffix = str(before).replace(before_hint, '')
                for after in afters:
                    if suffix and str(after).replace(after_hint, '') == suffix:
                        pair = (before, after)
                        if pair not in roles['paired']:
                            roles['paired'].append(pair)

        time_series_columns = [c for c in roles['time_fields']
                               if any(hint in str(c).lower() for hint in cls.TIME_SERIES_HINTS)]
        has_time_series = bool(len(roles['wide_value_columns']) >= 3 or time_series_columns)
        return {
            'sample_size': len(df),
            'roles': roles,
            'reasons': reasons,
            'has_date_field': bool(roles['time_fields']),
            'has_time_series': has_time_series,
            'time_series_columns': time_series_columns,
            'has_time_dimension': has_time_series,
            'recommendations': cls._build_recommendations(df, roles, has_time_series),
            'gaps': cls._build_gaps(df, roles),
        }

    @classmethod
    def _build_recommendations(cls, df: pd.DataFrame, roles: Dict,
                               has_time_series: bool = False) -> List[Dict]:
        """按数据支持度给出可做分析；blockers 说明为什么不满足。"""
        n_rows = len(df)
        predictors = len(roles['numeric']) + len(roles['categorical'])

        def make(rid: str, title: str, page: str, blockers: List[str],
                 evidence: str, hint: str = '') -> Dict:
            return {'id': rid, 'title': title, 'page': page, 'ready': not blockers,
                    'blockers': blockers, 'evidence': evidence, 'hint': hint}

        items = [
            make('describe', '描述性统计与分布图', '📈 描述性分析',
                 [] if (roles['numeric'] or roles['categorical']) else ['没有任何可用变量'],
                 f"数值变量 {len(roles['numeric'])} 个 / 分类变量 {len(roles['categorical'])} 个"),
            make('compare', '组间比较（t 检验 / Mann-Whitney / ANOVA / Kruskal-Wallis / 卡方）', '🔬 统计推断',
                 ([] if roles['grouping'] else ['没有取值在 2–10 类之间的分组变量'])
                 + ([] if (roles['numeric'] or roles['categorical']) else ['没有可比较的变量']),
                 '分组变量 ' + (('、'.join(str(c) for c in roles['grouping'][:3]))
                             if roles['grouping'] else '无')),
            make('correlation', '相关分析（Pearson / Spearman）', '📈 描述性分析',
                 [] if len(roles['numeric']) >= 2 else ['数值变量不足 2 个'],
                 f"数值变量 {len(roles['numeric'])} 个"),
            make('model', '风险预测（Logistic / ROC / 校准曲线 / 列线图）', '🤖 预测建模',
                 ([] if roles['outcome_binary'] else
                  ['未识别到 0/1 结局列（列名需含 死亡 / 复发 / 感染 / 发生 / 阳性 / 事件 等）'])
                 + ([] if predictors >= 2 else ['候选预测变量不足 2 个'])
                 + ([] if n_rows >= 100 else [f'样本量 {n_rows} 行，建议 ≥100 再建模']),
                 f"结局候选 {len(roles['outcome_binary'])} 个，候选预测变量 {predictors} 个，样本量 {n_rows} 行"),
            make('survival', '生存分析（KM 曲线 / Cox 回归）', '🔬 统计推断',
                 ([] if roles['binary'] else ['没有 0/1 事件列'])
                 + ([] if roles['duration'] else ['没有可识别的随访时间列（正值且名称含 天 / 月 / 年）']),
                 f"时间列 {len(roles['duration'])} 个，0/1 变量 {len(roles['binary'])} 个"),
            make('psm', '倾向性评分匹配与亚组分析', '📚 论文辅助',
                 ([] if len(roles['binary']) >= 2 else ['需要 0/1 处理变量与 0/1 结局变量'])
                 + ([] if roles['numeric'] else ['缺少可用于匹配的数值协变量']),
                 f"0/1 变量 {len(roles['binary'])} 个，数值协变量 {len(roles['numeric'])} 个"),
        ]

        if roles['paired']:
            items.append(make(
                'paired', '配对比较（治疗前后 / 左右 / 入院出院）', '🔬 统计推断', [],
                '配对列：' + '、'.join(f'{before}–{after}' for before, after in roles['paired'][:3])))

        if roles['rate'] or roles['count'] or roles.get('metric_column'):
            if roles['rate']:
                rate_evidence = f"识别到率 / 密度型指标 {len(roles['rate'])} 个"
            elif roles['count']:
                rate_evidence = f"识别到计数型指标 {len(roles['count'])} 个（需指定分母）"
            else:
                rate_evidence = ('指标名在列「'
                                 + '、'.join(str(c) for c in roles['metric_column'][:2])
                                 + '」的取值中，按指标取行后即可计算')
            items.append(make('rate', '率的 95%CI、两组率比较、感染密度（‰）', '⏱️ 时间与质控',
                              [], rate_evidence,
                              hint='指定分子与分母列即可计算'))

        if has_time_series:
            time_evidence = (f"识别到时间点序列：宽表时间列 {len(roles['wide_value_columns'])} 个"
                             if len(roles['wide_value_columns']) >= 3
                             else '识别到年份 / 季度 / 月份字段')
            items.append(make('trend', '时间趋势、同比环比与 SPC 控制图（UCL / CL / LCL）',
                              '⏱️ 时间与质控', [], time_evidence,
                              hint='宽表需先在「🧹 数据准备 → 宽表转长表」折叠成 时间–数值 两列'))

        if len(roles['numeric']) >= 2:
            items.append(make('agreement', '诊断一致性（Kappa / ICC / Bland-Altman）', '🔬 统计推断',
                              [], f"数值变量 {len(roles['numeric'])} 个，可选两列作为两次测量",
                              hint='需要同一指标的两次测量、两名评估者或两种方法'))
        if roles['binary'] and n_rows >= 200:
            items.append(make('validation', 'LASSO 变量筛选 + Bootstrap 内部验证', '🤖 预测建模',
                              [], f"0/1 变量 {len(roles['binary'])} 个，样本量 {n_rows} 行"))
        if n_rows > 0 and df.isna().sum().sum() > 0:
            items.append(make('missing', '缺失模式可视化与多重插补（MICE）',
                              '📊 总览与洞察',
                              [], f"共 {int(df.isna().sum().sum())} 个缺失单元格",
                              hint='在「🧹 数据准备 → 🩹 缺失模式与多重插补」中处理'))
        if roles['binary'] and roles['numeric']:
            items.append(make('iptw', 'IPTW 逆概率加权（ATE / ATT / ATM）', '📚 论文辅助', [],
                              f"0/1 处理变量 {len(roles['binary'])} 个，"
                              f"数值协变量 {len(roles['numeric'])} 个",
                              hint='按倾向评分倒数加权，保留全部样本；与 PSM 互补'))
        if roles['numeric']:
            items.append(make('dose_response', '剂量-反应关系（限制性立方样条）', '🤖 预测建模',
                              [], f"连续暴露变量 {len(roles['numeric'])} 个",
                              hint='检验暴露与结局是否线性、有无阈值或 J 型'))
        return items

    @classmethod
    def _build_gaps(cls, df: pd.DataFrame, roles: Dict) -> List[Dict]:
        """更进阶的方法：当前版本尚未内置（如实标注能力边界）。"""
        gaps: List[Dict] = []

        def add(title: str, satisfied: bool, why: str, action: str) -> None:
            gaps.append({'title': title, 'structure_ready': bool(satisfied),
                         'why': why, 'action': action})

        add('竞争风险模型（Fine-Gray / 累积发生函数）', bool(roles['duration']),
            '识别到随访时间列' if roles['duration'] else '需要随访时间列（正值）与事件类型列',
            '当前可用 Cox 回归替代（🔬 统计推断），但存在竞争事件时会有偏')
        add('时间依赖 AUC / NRI / IDI（生存模型区分度）', bool(roles['duration']),
            '识别到随访时间列' if roles['duration'] else '需要随访时间与事件列',
            '当前可用 C-index 与 ROC（🔬 统计推断、🤖 预测建模）')
        add('模型外部验证（独立数据集）', False,
            '需要来自另一中心或另一时间段的独立数据',
            '当前提供 Bootstrap 内部验证（🤖 预测建模）')
        return gaps


def to_numeric_safe(series: pd.Series) -> pd.Series:
    """安全转换为数值"""
    return pd.to_numeric(series, errors='coerce')


def is_binary(series: pd.Series) -> bool:
    """判断是否为二分类"""
    return series.dropna().nunique() == 2