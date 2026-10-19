"""
期刊模板：让导出的表格符合目标期刊的排版惯例。

中文期刊与 SCI 期刊对三线表的要求并不相同（线宽、字体、字号、小数位），
这些差异此前只能由作者在 Word 里手工调整。本模块把它们做成可切换的模板。

数字格式按**列名语义**处理，而不是全局一刀切：
    - p 值列 → 固定小数位，极小值写作 <0.001
    - 百分比 / 构成比列 → 1 位小数
    - 计数列（n / 例数 / 频数 / 样本量） → 整数
    - 其余数值 → 模板设定的小数位，并去掉多余零（1.080 → 1.08）
"""
import math
from dataclasses import asdict, dataclass
from typing import Dict, Optional


@dataclass
class TableStyle:
    """表格样式与数字格式规则。线宽单位为 pt。"""
    name: str = '中文期刊（默认）'
    font: str = '宋体'
    top_pt: float = 1.5            # 顶线
    header_pt: float = 0.75        # 表头下方细线
    bottom_pt: float = 1.5         # 底线
    header_bold: bool = True
    header_size_pt: float = 10.0
    body_size_pt: float = 9.0
    decimals: int = 3              # 一般数值小数位
    p_decimals: int = 3            # p 值小数位
    percent_decimals: int = 1      # 百分比小数位
    table_center: bool = True
    # ---- 以下在「导出报告时」生效 ----
    number_captions: bool = True   # 自动编号：表 1 / 图 1，并写上标题
    caption_size_pt: float = 9.0
    caption_bold: bool = False
    image_width_cm: float = 0.0    # 图片宽度（cm）；0 = 按宽高比自适应
    language: str = 'zh'           # 报告语言：zh / en（英文时章节名与题注按术语表翻译）

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data) -> 'TableStyle':
        data = dict(data or {})
        allowed = set(cls().__dict__)
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def with_style(self, **changes) -> 'TableStyle':
        data = self.to_dict()
        data.update(changes)
        return TableStyle.from_dict(data)


TEMPLATES: Dict[str, TableStyle] = {
    '中文期刊（默认）': TableStyle(),
    'SCI 英文期刊': TableStyle(
        name='SCI 英文期刊', font='Times New Roman',
        top_pt=1.0, header_pt=0.5, bottom_pt=1.0,
        header_size_pt=9.0, body_size_pt=9.0),
    '更细线条（部分期刊要求）': TableStyle(
        name='更细线条（部分期刊要求）', top_pt=1.0, header_pt=0.5, bottom_pt=1.0),
    '表头不加粗': TableStyle(
        name='表头不加粗', header_bold=False),
}

DEFAULT_TEMPLATE_NAME = '中文期刊（默认）'

_P_COLUMNS = {'p', 'p值', 'pvalue', 'p_value', 'p-value', 'sig', '显著性', 'p值(双侧)'}
_PERCENT_HINTS = ('%', '构成比', '百分比', '比例')
_COUNT_HINTS = ('例数', '频数', '样本量', '计数', '人数')


def pt_to_eighth(pt) -> str:
    """OOXML 的线宽单位是 1/8 pt。"""
    return str(max(0, int(round(float(pt or 0) * 8))))


def is_p_column(column) -> bool:
    """
    是否 p 值列。

    注意不能只看首字母：`PaO2/FiO2 (mmHg)` 也以 P 开头，但它不是 p 值列。
    这里的判据是「p 之后不再出现其他字母」。
    """
    text = str(column or '').strip().lower().replace(' ', '')
    if not text:
        return False
    if text in _P_COLUMNS:
        return True
    return (text.startswith('p') and 1 < len(text) <= 10
            and not any(ch.isalpha() for ch in text[1:]))


def is_percent_column(column) -> bool:
    text = str(column or '')
    return any(hint in text for hint in _PERCENT_HINTS)


def is_count_column(column) -> bool:
    text = str(column or '').strip().lower()
    if text == 'n':
        return True
    return any(hint in text for hint in _COUNT_HINTS)


def format_value(value, column: str = '', style: Optional[TableStyle] = None) -> str:
    """按列名语义与模板规则格式化一个单元格。"""
    style = style or TableStyle()
    if value is None:
        return '—'
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, str):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(number) or math.isinf(number):
        return '—'

    # p 值列：固定小数位，极小值写 <0.001（或模板设定的更粗档位）
    if is_p_column(column):
        threshold = 10 ** (-int(style.p_decimals))
        if number == 0:
            return '0'
        if abs(number) < threshold:
            return f'<{threshold:g}'
        return f'{number:.{int(style.p_decimals)}f}'.rstrip('0').rstrip('.') or '0'

    if is_percent_column(column) and abs(number) <= 100:
        return f'{number:.{int(style.percent_decimals)}f}'

    if is_count_column(column):
        return str(int(round(number))) if abs(number - round(number)) < 1e-9 else f'{number:g}'

    if number == 0:
        return '0'
    if abs(number) >= 1e5:                          # 大数加千分位更易读（如 123,456）
        return f'{number:,.0f}'
    if float(number).is_integer() and abs(number) < 1e15:
        return str(int(number))
    threshold = 10 ** (-int(style.decimals))
    if abs(number) < threshold:
        return f'<{threshold:g}'
    return f'{number:.{int(style.decimals)}f}'.rstrip('0').rstrip('.') or '0'


def describe(style: TableStyle) -> str:
    """给界面用的一句话说明（表格部分）。"""
    width = (f'图片宽度 {style.image_width_cm:g} cm'
             if style.image_width_cm else '图片宽度按宽高比自适应')
    return (f'线宽：顶/底 {style.top_pt:g} pt、表头 {style.header_pt:g} pt；'
            f'字体 {style.font}；表头 {style.header_size_pt:g} pt'
            f'{"（加粗）" if style.header_bold else "（不加粗）"}、'
            f'表体 {style.body_size_pt:g} pt；小数位 {style.decimals} 位'
            f'（p 值 {style.p_decimals} 位、百分比 {style.percent_decimals} 位）；'
            f'{"自动编号图题/表题" if style.number_captions else "不加编号"}；{width}')


# ==================== 图片样式（生成图时生效） ====================

@dataclass
class FigureStyle:
    """
    图片内文字的字体与字号。

    这些参数在**绘图时**写入 matplotlib，因此修改后需要重新运行分析页才能出新图；
    而图注编号与图片宽度在**导出报告时**生效，改完立刻有用。
    """
    font: str = 'Microsoft YaHei'
    base_size: float = 10.0
    title_size: float = 11.0
    label_size: float = 10.0
    tick_size: float = 9.0
    legend_size: float = 9.0
    figure_dpi: float = 100.0
    savefig_dpi: float = 300.0

    def to_dict(self) -> Dict:
        return asdict(self)


FIGURE_TEMPLATES: Dict[str, FigureStyle] = {
    '中文期刊（默认）': FigureStyle(),
    'SCI 英文期刊': FigureStyle(font='Arial', base_size=9.0, title_size=10.0,
                                label_size=9.0, tick_size=8.0, legend_size=8.0),
}

# 中文字体回退链：首选字体缺失时依次尝试，避免中文显示成方块
CJK_FALLBACK = ['Microsoft YaHei', 'SimHei', 'SimSun', 'PingFang SC', 'Arial Unicode MS']


def apply_figure_style(style: Optional[FigureStyle] = None) -> None:
    """
    把图片样式写入 matplotlib 全局参数。

    必须在各分析模块加载**之后**调用：模块顶部会设置自己的字体列表，
    只有后设置才能覆盖。rcParams 只影响之后新绘制的图。
    """
    import matplotlib

    style = style or FigureStyle()
    fonts = [style.font] + [name for name in CJK_FALLBACK if name != style.font]
    matplotlib.rcParams.update({
        'font.family': 'sans-serif',
        'font.sans-serif': fonts,
        'axes.unicode_minus': False,
        'font.size': float(style.base_size),
        'axes.titlesize': float(style.title_size),
        'axes.labelsize': float(style.label_size),
        'xtick.labelsize': float(style.tick_size),
        'ytick.labelsize': float(style.tick_size),
        'legend.fontsize': float(style.legend_size),
        'figure.dpi': float(style.figure_dpi),
        'savefig.dpi': float(style.savefig_dpi),
    })


def describe_figures(style: Optional[FigureStyle] = None) -> str:
    style = style or FigureStyle()
    return (f'图片内字体 {style.font}，基础字号 {style.base_size:g} pt'
            f'（标题 {style.title_size:g}、坐标轴 {style.label_size:g}、'
            f'刻度 {style.tick_size:g}）；导出分辨率 {style.savefig_dpi:g} dpi')
