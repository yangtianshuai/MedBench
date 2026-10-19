"""
医析台 MedBench - 主入口（美化版）

本地优先的临床数据分析工作台：统计计算全部在本机完成，数据不出本机；
可选接入 OpenAI 兼容的 AI 服务做结果解读（仅发送结构摘要）。
界面风格：tech-blue + slate | 临床级 UI
"""
import importlib.util
import re
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

MODULES_DIR = Path(__file__).parent / 'modules'
sys.path.insert(0, str(MODULES_DIR))


def _load_local_modules() -> None:
    """
    从文件系统重新加载 modules 目录，覆盖打包时被冻结进 PYZ 的旧版本。

    PyInstaller 的模块归档优先于 _internal/modules 里的源码文件，升级程序时
    只会替换源码，所以这里显式按文件路径加载一遍，保证运行的是当前源码。
    """
    if not MODULES_DIR.is_dir():
        return

    package = types.ModuleType('modules')
    package.__path__ = [str(MODULES_DIR)]
    package.__file__ = str(MODULES_DIR / '__init__.py')
    sys.modules['modules'] = package

    pending = {}
    for path in sorted(MODULES_DIR.glob('*.py')):
        if path.name.startswith('_'):
            continue
        name = f'modules.{path.stem}'
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module          # 先注册，避免重复导入
        pending[name] = (spec, module)

    for name, (spec, module) in pending.items():
        try:
            spec.loader.exec_module(module)
        except Exception as error:          # 单个模块失败不影响其它模块
            sys.modules.pop(name, None)
            print(f'[模块加载失败] {name}: {error}')


_load_local_modules()


def apply_session_figure_style() -> None:
    """
    按会话设置统一图片样式（字体、字号、分辨率）。

    必须在各分析模块加载**之后**调用：模块顶部会设置自己的字体列表，
    只有后设置才能覆盖。rcParams 只影响之后新绘制的图，
    因此改动样式后需要重新运行分析页才会体现在图里。
    """
    try:
        from modules.journal import FIGURE_TEMPLATES, FigureStyle, apply_figure_style

        name = st.session_state.get('journal_template') or '中文期刊（默认）'
        if name == '自定义（手动设置）':
            style = FigureStyle(
                font=st.session_state.get('fig_font') or 'Microsoft YaHei',
                base_size=float(st.session_state.get('fig_base') or 10.0),
                title_size=float(st.session_state.get('fig_title') or 11.0),
                label_size=float(st.session_state.get('fig_label') or 10.0),
                tick_size=float(st.session_state.get('fig_tick') or 9.0),
                legend_size=float(st.session_state.get('fig_legend') or 9.0),
                savefig_dpi=float(st.session_state.get('fig_dpi') or 300),
            )
        else:
            style = FIGURE_TEMPLATES.get(name) or FigureStyle()
        apply_figure_style(style)
    except Exception as error:      # 样式失败不应影响任何分析功能
        print(f'[图片样式] 应用失败（已忽略）：{error}')


from modules.data_loader import DataLoader, FieldMeta


def render_table(data, hide_index=False):
    """Render a table without Streamlit's optional PyArrow dependency."""
    if not isinstance(data, pd.DataFrame):
        data = pd.DataFrame(data)
    html = data.to_html(
        index=not hide_index,
        classes='medical-table',
        border=0,
        na_rep='',
    )
    st.markdown(f'<div class="table-wrapper">{html}</div>', unsafe_allow_html=True)


# ========== 版本 ==========
# 界面侧边栏、About 菜单与使用说明统一引用此常量，避免各处版本号不一致。
APP_VERSION = 'v1.3'

# ========== 页面配置 ==========
st.set_page_config(
    page_title="医析台 MedBench",
    page_icon="🩺",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        'About': f'🩺 医析台 MedBench {APP_VERSION}\n'
                 '本地统计计算（临床级精度）+ 可选 AI 解读（仅发送结构摘要）。'
    }
)

# ========== 设计系统（Design Tokens） ==========
# 主色：tech-blue + indigo（slate中性）
PRIMARY = '#3B82F6'
PRIMARY_DARK = '#2563EB'
PRIMARY_LIGHT = '#DBEAFE'
INDIGO = '#6366F1'
INDIGO_DARK = '#4F46E5'

# 中性色（Slate）
SLATE_50 = '#F8FAFC'
SLATE_100 = '#F1F5F9'
SLATE_200 = '#E2E8F0'
SLATE_300 = '#CBD5E1'
SLATE_400 = '#94A3B8'
SLATE_500 = '#64748B'
SLATE_600 = '#475569'
SLATE_700 = '#334155'
SLATE_800 = '#1E293B'
SLATE_900 = '#0F172A'

# 语义色
SUCCESS = '#10B981'
SUCCESS_LIGHT = '#D1FAE5'
WARNING = '#F59E0B'
WARNING_LIGHT = '#FEF3C7'
DANGER = '#EF4444'
DANGER_LIGHT = '#FEE2E2'

# 统计方法说明：通过结果卡片的 help 参数悬停查看
METHOD_INFO = {
    '独立样本t检验': (
        '比较两独立组均值。公式：t = (x̄₁ − x̄₂) / SE，通常假设两组近似正态且方差齐。\n\n'
        '参考：Student (1908), *Biometrika*, 6(1), 1–25.'
    ),
    "Welch's t检验": (
        '比较两独立组均值，不要求方差齐。公式：t = (x̄₁ − x̄₂) / √(s₁²/n₁ + s₂²/n₂)。\n\n'
        '参考：Welch (1947), *Biometrika*, 34(1–2), 28–35.'
    ),
    'Mann-Whitney U检验': (
        '比较两独立组的秩分布，适用于非正态或有序数据。U = R₁ − n₁(n₁+1)/2。\n\n'
        '参考：Mann & Whitney (1947), *The Annals of Mathematical Statistics*, 18(1), 50–60.'
    ),
    '单因素方差分析(One-way ANOVA)': (
        '比较三个及以上独立组均值。F = MS_between / MS_within；显著后需进行事后比较。\n\n'
        '参考：Fisher (1925), *Statistical Methods for Research Workers*.'
    ),
    'Kruskal-Wallis H检验': (
        '比较三个及以上独立组的秩分布。H = [12/(N(N+1))]Σ(Rᵢ²/nᵢ) − 3(N+1)。\n\n'
        '参考：Kruskal & Wallis (1952), *Journal of the American Statistical Association*, 47(260), 583–621.'
    ),
    'Pearson卡方检验': (
        '检验分类变量是否独立。χ² = Σ(Oᵢⱼ − Eᵢⱼ)²/Eᵢⱼ。期望频数过小时应考虑 Fisher 检验。\n\n'
        '参考：Pearson (1900), *The London, Edinburgh, and Dublin Philosophical Magazine*, 50(302), 157–175.'
    ),
    '连续校正卡方': (
        '用于分类变量列联表，并对 2×2 表采用连续性校正。公式：χ²ᵧ = Σ(|O−E|−0.5)²/E。\n\n'
        '参考：Yates (1934), *Supplement to the Journal of the Royal Statistical Society*, 1(2), 217–235.'
    ),
    'Fisher精确检验': (
        '用于小样本 2×2 列联表，直接计算在固定边际下观察到当前或更极端表格的概率。\n\n'
        '参考：Fisher (1922), *Journal of the Royal Statistical Society*, 85(1), 87–94.'
    ),
    '配对t检验': (
        '比较配对观测的平均差异。公式：t = d̄ / (sᵈ/√n)，其中 d 为每对差值。\n\n'
        '参考：Student (1908), *Biometrika*, 6(1), 1–25.'
    ),
    'Wilcoxon符号秩检验': (
        '比较配对观测的差值秩分布，适用于差值不满足正态性时。统计量为正负差值秩和的较小者。\n\n'
        '参考：Wilcoxon (1945), *Biometrics Bulletin*, 1(6), 80–83.'
    ),
    'Pearson相关': (
        '衡量两个连续变量的线性相关。公式：r = cov(X,Y)/(sₓsᵧ)，取值范围为 −1 至 1。\n\n'
        '参考：Pearson (1895), *Proceedings of the Royal Society of London*, 58, 240–242.'
    ),
    'Spearman秩相关': (
        '基于秩次衡量单调相关。无并列秩时，ρ = 1 − 6Σdᵢ²/[n(n²−1)]。\n\n'
        '参考：Spearman (1904), *The American Journal of Psychology*, 15(1), 72–101.'
    ),
}


def method_help(method: str) -> str:
    """返回统计方法的公式与参考文献悬停说明。"""
    return METHOD_INFO.get(method, '该方法暂无详细说明。')

# ========== 全局样式 ==========
st.markdown(f"""
<style>
    /* ===== 全局字体与背景 ===== */
    html, body, [data-testid="stAppViewContainer"] {{
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', sans-serif;
        color: {SLATE_800};
    }}

    [data-testid="stAppViewContainer"] {{
        background: linear-gradient(180deg, {SLATE_50} 0%, #FFFFFF 100%);
    }}

    /* ===== 顶部 Header 渐变Banner ===== */
    .hero-banner {{
        background: linear-gradient(135deg, {PRIMARY} 0%, {INDIGO} 50%, {INDIGO_DARK} 100%);
        padding: 2rem 2.5rem 1.6rem;
        border-radius: 16px;
        color: white;
        margin-bottom: 1.5rem;
        position: relative;
        overflow: hidden;
        box-shadow: 0 10px 25px -5px rgba(59, 130, 246, 0.25);
    }}
    .hero-banner::before {{
        content: '';
        position: absolute;
        top: -50%;
        right: -10%;
        width: 400px;
        height: 400px;
        background: radial-gradient(circle, rgba(255,255,255,0.1) 0%, transparent 70%);
        pointer-events: none;
    }}
    .hero-banner::after {{
        content: '';
        position: absolute;
        bottom: -30%;
        left: -5%;
        width: 300px;
        height: 300px;
        background: radial-gradient(circle, rgba(255,255,255,0.08) 0%, transparent 70%);
        pointer-events: none;
    }}
    .hero-title {{
        font-size: 1.85rem;
        font-weight: 700;
        margin: 0;
        letter-spacing: -0.02em;
        position: relative;
        z-index: 1;
    }}
    .hero-subtitle {{
        font-size: 0.95rem;
        opacity: 0.92;
        margin: 0.5rem 0 0;
        font-weight: 400;
        position: relative;
        z-index: 1;
    }}
    .hero-meta {{
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        background: rgba(255,255,255,0.18);
        padding: 0.25rem 0.7rem;
        border-radius: 999px;
        font-size: 0.75rem;
        margin-top: 0.85rem;
        backdrop-filter: blur(10px);
        position: relative;
        z-index: 1;
    }}
    .hero-pulse {{
        display: inline-block;
        width: 8px;
        height: 8px;
        background: #34D399;
        border-radius: 50%;
        animation: pulse 2s infinite;
        box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.6);
    }}
    @keyframes pulse {{
        0% {{ box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.6); }}
        70% {{ box-shadow: 0 0 0 8px rgba(52, 211, 153, 0); }}
        100% {{ box-shadow: 0 0 0 0 rgba(52, 211, 153, 0); }}
    }}

    /* ===== Metric 指标卡 ===== */
    [data-testid="stMetric"] {{
        background: white;
        padding: 1.1rem 1.2rem !important;
        border-radius: 12px !important;
        border: 1px solid {SLATE_200};
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04);
        transition: all 0.2s;
    }}
    [data-testid="stMetric"]:hover {{
        box-shadow: 0 4px 12px rgba(15, 23, 42, 0.08);
        border-color: {PRIMARY_LIGHT};
    }}
    [data-testid="stMetricLabel"] {{
        color: {SLATE_500} !important;
        font-size: 0.8rem !important;
        font-weight: 500 !important;
    }}
    [data-testid="stMetricValue"] {{
        color: {SLATE_900} !important;
        font-size: 1.7rem !important;
        font-weight: 700 !important;
        letter-spacing: -0.02em;
    }}
    [data-testid="stMetricDelta"] {{
        font-size: 0.78rem !important;
    }}

    /* ===== Tabs 页签 ===== */
    .stTabs [data-baseweb="tab-list"] {{
        gap: 4px;
        background: transparent;
        padding: 0;
        border-bottom: 2px solid {SLATE_200};
    }}
    .stTabs [data-baseweb="tab"] {{
        height: 44px;
        padding: 0 20px;
        background: transparent;
        border-radius: 8px 8px 0 0;
        color: {SLATE_500};
        font-weight: 500;
        font-size: 0.92rem;
        border: none;
        border-bottom: 2px solid transparent;
        margin-bottom: -2px;
        transition: all 0.2s;
    }}
    .stTabs [data-baseweb="tab"]:hover {{
        color: {PRIMARY};
        background: {SLATE_50};
    }}
    .stTabs [aria-selected="true"] {{
        background: transparent;
        color: {PRIMARY};
        border-bottom: 2px solid {PRIMARY};
        font-weight: 600;
    }}
    .stTabs [data-baseweb="tab-panel"] {{
        padding-top: 1.5rem;
    }}

    /* ===== Sidebar 侧边栏 ===== */
    [data-testid="stSidebar"] {{
        background: {SLATE_50};
        border-right: 1px solid {SLATE_200};
    }}
    [data-testid="stSidebar"] .block-container {{
        padding-top: 2rem;
        padding-left: 1.2rem;
        padding-right: 1.2rem;
    }}

    /* ===== Section 区块标题 ===== */
    .section-header {{
        display: flex;
        align-items: center;
        gap: 0.6rem;
        margin-bottom: 1.2rem;
        padding-bottom: 0.6rem;
        border-bottom: 2px solid {SLATE_200};
    }}
    .section-header h2 {{
        font-size: 1.4rem;
        font-weight: 700;
        color: {SLATE_900};
        margin: 0;
        letter-spacing: -0.01em;
    }}
    .section-header .badge {{
        background: {PRIMARY_LIGHT};
        color: {PRIMARY_DARK};
        padding: 0.2rem 0.6rem;
        border-radius: 6px;
        font-size: 0.72rem;
        font-weight: 600;
    }}

    /* ===== Subsubsection 子标题 ===== */
    .subsection-header {{
        display: flex;
        align-items: center;
        gap: 0.5rem;
        margin: 1.5rem 0 1rem;
        font-size: 1rem;
        font-weight: 600;
        color: {SLATE_700};
    }}
    .subsection-header::before {{
        content: '';
        width: 4px;
        height: 18px;
        background: linear-gradient(180deg, {PRIMARY}, {INDIGO});
        border-radius: 2px;
    }}

    /* ===== Info Card 提示卡 ===== */
    .stAlert {{
        border-radius: 10px !important;
        border-left-width: 4px !important;
        padding: 0.8rem 1rem !important;
    }}
    [data-baseweb="notification"] {{
        border-radius: 10px !important;
    }}

    /* ===== Buttons 按钮 ===== */
    .stButton > button {{
        border-radius: 10px !important;
        font-weight: 500 !important;
        transition: all 0.2s !important;
        border: 1px solid {SLATE_300} !important;
        color: {SLATE_700} !important;
        background: white !important;
    }}
    .stButton > button:hover {{
        border-color: {PRIMARY} !important;
        color: {PRIMARY} !important;
        transform: translateY(-1px);
        box-shadow: 0 4px 8px rgba(59, 130, 246, 0.12);
    }}
    .stButton > button[kind="primary"] {{
        background: linear-gradient(135deg, {PRIMARY}, {INDIGO}) !important;
        color: white !important;
        border: none !important;
        box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3);
    }}
    .stButton > button[kind="primary"]:hover {{
        box-shadow: 0 6px 18px rgba(59, 130, 246, 0.45);
        transform: translateY(-1px);
    }}

    /* ===== Selectbox / Multiselect 输入控件 ===== */
    [data-baseweb="select"] > div, [data-baseweb="input"] > div {{
        border-radius: 10px !important;
        border-color: {SLATE_300} !important;
        transition: all 0.2s !important;
    }}
    [data-baseweb="select"] > div:hover, [data-baseweb="input"] > div:hover {{
        border-color: {PRIMARY} !important;
    }}

    /* ===== DataFrame 表格 ===== */
    [data-testid="stDataFrame"] {{
        border-radius: 10px;
        overflow: hidden;
        border: 1px solid {SLATE_200};
    }}

    /* ===== File Uploader 上传区 ===== */
    [data-testid="stFileUploader"] {{
        background: white;
        padding: 1rem;
        border-radius: 10px;
        border: 1.5px dashed {SLATE_300};
        transition: all 0.2s;
    }}
    [data-testid="stFileUploader"]:hover {{
        border-color: {PRIMARY};
        background: {PRIMARY_LIGHT}10;
    }}

    /* ===== Expander 折叠面板 ===== */
    .streamlit-expanderHeader, [data-testid="stExpander"] details summary {{
        background: {SLATE_50};
        border-radius: 10px;
        font-weight: 500;
        color: {SLATE_700};
        border: 1px solid {SLATE_200};
    }}

    /* ===== Radio Pills 单选标签 ===== */
    [data-baseweb="radio"] [role="radiogroup"] {{
        gap: 0.4rem;
        background: {SLATE_100};
        padding: 4px;
        border-radius: 10px;
    }}
    [data-baseweb="radio"] label {{
        border-radius: 8px;
        padding: 0.4rem 1rem;
        transition: all 0.2s;
    }}
    [data-baseweb="radio"] label:hover {{
        background: white;
    }}

    /* ===== Image 图表 ===== */
    [data-testid="stImage"] img {{
        border-radius: 10px;
        border: 1px solid {SLATE_200};
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04);
    }}

    /* ===== Divider 分隔线 ===== */
    hr {{
        margin: 1.2rem 0 !important;
        border-color: {SLATE_200} !important;
    }}

    /* ===== 全局链接 ===== */
    a {{
        color: {PRIMARY};
        text-decoration: none;
    }}
    a:hover {{
        text-decoration: underline;
    }}

    /* ===== 隐藏Streamlit水印 ===== */
    #MainMenu {{ visibility: hidden; }}
    footer {{ visibility: hidden; }}
    [data-testid="stToolbar"] {{ visibility: hidden; }}

    /* ===== 顶部 Header 去白条 =====
       注意：绝不能用 display:none 隐藏 stHeader，
       Streamlit 的侧边栏折叠/展开按钮就挂在这个区域里，隐藏后折叠功能会失效。
       这里只把背景改为透明、去掉边框，保留其高度与交互能力。 */
    [data-testid="stHeader"] {{
        background: transparent !important;
        border-bottom: none !important;
        box-shadow: none !important;
    }}
    [data-testid="stHeader"]::before {{
        background: transparent !important;
    }}
    /* 主内容区顶部间距补偿 */
    .main .block-container {{
        padding: 0.5rem 1.5rem 0.5rem 1.5rem !important;
    }}
    .st-emotion-cache-zy6yx3 {{
        padding: 3rem 4rem 5rem !important;
    }}

    /* ===== 侧边栏折叠/展开按钮常驻显示 =====
       【关键修复】Streamlit 默认把折叠按钮设为 visibility:hidden，
       只有鼠标悬停到侧边栏时才显现——用户以为按钮不存在、点了没反应。
       这里强制常驻可见，并做成明显的蓝色按钮。 */
    /* 收起按钮：stSidebarCollapseButton
       展开按钮：stExpandSidebarButton（Streamlit 1.4x+ 改名，旧版叫 stSidebarCollapsedControl）
       三者都写上，兼容不同版本 */
    [data-testid="stSidebarCollapseButton"],
    [data-testid="stExpandSidebarButton"],
    [data-testid="stSidebarCollapsedControl"] {{
        visibility: visible !important;
        opacity: 1 !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        background: rgba(59, 130, 246, 0.16) !important;
        border-radius: 8px !important;
        transition: background 0.2s !important;
    }}
    [data-testid="stSidebarCollapseButton"]:hover,
    [data-testid="stExpandSidebarButton"]:hover,
    [data-testid="stSidebarCollapsedControl"]:hover {{
        background: rgba(59, 130, 246, 0.32) !important;
    }}
    /* 折叠后的展开按钮固定在左上角，保证随时能找回来 */
    [data-testid="stExpandSidebarButton"],
    [data-testid="stSidebarCollapsedControl"] {{
        position: fixed !important;
        left: 10px !important;
        top: 12px !important;
        z-index: 999997 !important;
        box-shadow: 0 4px 14px rgba(59, 130, 246, 0.35) !important;
    }}

    /* ===== 滚动条美化 ===== */
    ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
    ::-webkit-scrollbar-track {{ background: {SLATE_100}; }}
    ::-webkit-scrollbar-thumb {{ background: {SLATE_300}; border-radius: 4px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: {SLATE_400}; }}

    /* ===== 卡片样式（用HTML注入时使用） ===== */
    .info-card {{
        background: white;
        padding: 1rem 1.2rem;
        border-radius: 10px;
        border-left: 3px solid {PRIMARY};
        margin: 0.5rem 0;
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.05);
    }}
    .warning-card {{
        background: {WARNING_LIGHT};
        padding: 0.8rem 1rem;
        border-radius: 8px;
        border-left: 3px solid {WARNING};
        margin: 0.5rem 0;
        color: {SLATE_800};
    }}
    .success-card {{
        background: {SUCCESS_LIGHT};
        padding: 0.8rem 1rem;
        border-radius: 8px;
        border-left: 3px solid {SUCCESS};
        margin: 0.5rem 0;
        color: {SLATE_800};
    }}
</style>
""", unsafe_allow_html=True)


# ========== 顶部Banner ==========
def render_hero():
    st.markdown("""
    <div class="hero-banner">
        <h1 class="hero-title">🩺 医析台 MedBench</h1>
        <p class="hero-subtitle">面向临床科研场景的一站式数据分析平台 · 数据全程本地处理，零外传</p>
    </div>
    """, unsafe_allow_html=True)


# ========== 引导页（空状态） ==========
def render_hero_empty():
    """未上传数据时的引导页"""
    # 注意：不再调用 render_hero()，由 main() 统一调用
    st.markdown(f"""
    <div style="text-align: center; padding: 2.5rem 1rem 1.5rem;">
        <div style="font-size: 3.5rem; margin-bottom: 0.5rem;">📊</div>
        <h2 style="color: {SLATE_800}; font-weight: 700; margin-bottom: 0.5rem;">开始你的临床数据分析</h2>
        <p style="color: {SLATE_500}; font-size: 1rem;">从左侧上传一个 Excel 文件开始分析</p>
    </div>
    """, unsafe_allow_html=True)

    # 功能卡片网格
    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown(f"""
        <div style="background: white; padding: 1.3rem; border-radius: 12px; border: 1px solid {SLATE_200};
                    height: 100%; box-shadow: 0 1px 3px rgba(15,23,42,0.04);">
            <div style="display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.8rem;">
                <div style="width: 36px; height: 36px; border-radius: 10px;
                            background: linear-gradient(135deg, {PRIMARY}, {INDIGO});
                            display: flex; align-items: center; justify-content: center; font-size: 1.1rem;">📊</div>
                <strong style="color: {SLATE_800}; font-size: 1rem;">总览与洞察</strong>
            </div>
            <p style="color: {SLATE_600}; font-size: 0.88rem; line-height: 1.5; margin: 0;">
                数据概况、类型调整与缺失处理，并推荐可做的分析。<br>
                <span style="color: {SLATE_400};">适合初次接触陌生数据</span>
            </p>
        </div>
        """, unsafe_allow_html=True)

    with col2:
        st.markdown(f"""
        <div style="background: white; padding: 1.3rem; border-radius: 12px; border: 1px solid {SLATE_200};
                    height: 100%; box-shadow: 0 1px 3px rgba(15,23,42,0.04);">
            <div style="display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.8rem;">
                <div style="width: 36px; height: 36px; border-radius: 10px;
                            background: linear-gradient(135deg, {SUCCESS}, #059669);
                            display: flex; align-items: center; justify-content: center; font-size: 1.1rem;">🔬</div>
                <strong style="color: {SLATE_800}; font-size: 1rem;">统计推断</strong>
            </div>
            <p style="color: {SLATE_600}; font-size: 0.88rem; line-height: 1.5; margin: 0;">
                自动选择 t/ANOVA/Mann-Whitney/卡方/Fisher。<br>
                <span style="color: {SLATE_400};">含效应量与 95% 置信区间</span>
            </p>
        </div>
        """, unsafe_allow_html=True)

    with col3:
        st.markdown(f"""
        <div style="background: white; padding: 1.3rem; border-radius: 12px; border: 1px solid {SLATE_200};
                    height: 100%; box-shadow: 0 1px 3px rgba(15,23,42,0.04);">
            <div style="display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.8rem;">
                <div style="width: 36px; height: 36px; border-radius: 10px;
                            background: linear-gradient(135deg, {INDIGO}, {INDIGO_DARK});
                            display: flex; align-items: center; justify-content: center; font-size: 1.1rem;">🤖</div>
                <strong style="color: {SLATE_800}; font-size: 1rem;">预测建模</strong>
            </div>
            <p style="color: {SLATE_600}; font-size: 0.88rem; line-height: 1.5; margin: 0;">
                Logistic/Cox 回归 + ROC + 校准 + 决策曲线。<br>
                <span style="color: {SLATE_400};">论文级可视化</span>
            </p>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    col4, col5, col6 = st.columns(3)

    with col4:
        st.markdown(f"""
        <div style="background: white; padding: 1.3rem; border-radius: 12px; border: 1px solid {SLATE_200};
                    height: 100%; box-shadow: 0 1px 3px rgba(15,23,42,0.04);">
            <div style="display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.8rem;">
                <div style="width: 36px; height: 36px; border-radius: 10px;
                            background: linear-gradient(135deg, {WARNING}, #D97706);
                            display: flex; align-items: center; justify-content: center; font-size: 1.1rem;">📚</div>
                <strong style="color: {SLATE_800}; font-size: 1rem;">论文辅助</strong>
            </div>
            <p style="color: {SLATE_600}; font-size: 0.88rem; line-height: 1.5; margin: 0;">
                PSM 匹配、亚组森林图、E-value。<br>
                <span style="color: {SLATE_400};">让审稿人挑不出方法学硬伤</span>
            </p>
        </div>
        """, unsafe_allow_html=True)

    with col5:
        st.markdown(f"""
        <div style="background: white; padding: 1.3rem; border-radius: 12px; border: 1px solid {SLATE_200};
                    height: 100%; box-shadow: 0 1px 3px rgba(15,23,42,0.04);">
            <div style="display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.8rem;">
                <div style="width: 36px; height: 36px; border-radius: 10px;
                            background: linear-gradient(135deg, #06B6D4, #0891B2);
                            display: flex; align-items: center; justify-content: center; font-size: 1.1rem;">📄</div>
                <strong style="color: {SLATE_800}; font-size: 1rem;">报告导出</strong>
            </div>
            <p style="color: {SLATE_600}; font-size: 0.88rem; line-height: 1.5; margin: 0;">
                一键导出 Word 报告，含方法说明、图表。<br>
                <span style="color: {SLATE_400};">符合学术写作规范</span>
            </p>
        </div>
        """, unsafe_allow_html=True)

    with col6:
        st.markdown(f"""
        <div style="background: linear-gradient(135deg, {PRIMARY}10, {INDIGO}10);
                    padding: 1.3rem; border-radius: 12px; border: 1.5px solid {PRIMARY_LIGHT};
                    height: 100%;">
            <div style="display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.8rem;">
                <div style="width: 36px; height: 36px; border-radius: 10px;
                            background: linear-gradient(135deg, {PRIMARY}, {INDIGO});
                            display: flex; align-items: center; justify-content: center; font-size: 1.1rem;">🩺</div>
                <strong style="color: {SLATE_800}; font-size: 1rem;">医疗安全优先</strong>
            </div>
            <p style="color: {SLATE_600}; font-size: 0.88rem; line-height: 1.5; margin: 0;">
                数据全程本地处理，零上传。<br>
                <span style="color: {PRIMARY}; font-weight: 500;">符合患者隐私合规要求</span>
            </p>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # 快速开始指南
    st.markdown(f"""
    <div style="background: white; padding: 1.5rem; border-radius: 12px; border: 1px solid {SLATE_200};
                margin-top: 1rem;">
        <h3 style="color: {SLATE_800}; margin-top: 0; font-size: 1.05rem;">🚀 三步开始</h3>
        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-top: 1rem;">
            <div style="text-align: center;">
                <div style="width: 32px; height: 32px; border-radius: 50%; background: {PRIMARY}; color: white;
                            display: inline-flex; align-items: center; justify-content: center; font-weight: 700; margin-bottom: 0.5rem;">1</div>
                <div style="font-weight: 500; color: {SLATE_700}; font-size: 0.9rem;">上传 Excel</div>
                <div style="color: {SLATE_400}; font-size: 0.78rem;">.xlsx / .xls 格式</div>
            </div>
            <div style="text-align: center;">
                <div style="width: 32px; height: 32px; border-radius: 50%; background: {PRIMARY}; color: white;
                            display: inline-flex; align-items: center; justify-content: center; font-weight: 700; margin-bottom: 0.5rem;">2</div>
                <div style="font-weight: 500; color: {SLATE_700}; font-size: 0.9rem;">选择分析模块</div>
                <div style="color: {SLATE_400}; font-size: 0.78rem;">六大功能自由切换</div>
            </div>
            <div style="text-align: center;">
                <div style="width: 32px; height: 32px; border-radius: 50%; background: {PRIMARY}; color: white;
                            display: inline-flex; align-items: center; justify-content: center; font-weight: 700; margin-bottom: 0.5rem;">3</div>
                <div style="font-weight: 500; color: {SLATE_700}; font-size: 0.9rem;">导出报告</div>
                <div style="color: {SLATE_400}; font-size: 0.78rem;">Word 文档即下即用</div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)


# ========== 数据接入：解析设置 / 类型指定 / 宽转长 ==========
TYPE_LABELS = {
    'auto': '自动识别',
    'numeric': '🔢 数值',
    'categorical': '📋 分类',
    'binary': '⚖️ 二分类',
    'datetime': '📅 日期',
    'text': '📝 文本',
}
TYPE_CODES = {label: code for code, label in TYPE_LABELS.items()}
TYPE_OPTIONS = [TYPE_LABELS[key] for key in ('auto', 'numeric', 'categorical', 'binary', 'datetime', 'text')]

# 主导航：总览与洞察合并为首页（数据概况 → 数据准备 → 结构画像 → 推荐分析）
NAV_OVERVIEW = '📊 总览与洞察'
NAV_PREP = '🧹 数据准备'
# 导航按「看数据 → 改数据 → 分析 → 成文」的顺序排：
#   总览与洞察（看结论、拿建议）→ 数据准备（动手改数据）→ 质控 / 描述 / 推断 / 建模（分析）
#   → 论文辅助 → 报告导出
# 「📌 数据摘录」已并入「🧹 数据准备」（它就是查看当前这份数据）；
# 「重复测量 LMM/GEE」与「时间序列预测」从质控页移到了预测建模页；
# 「相关分析」从描述性分析移到了统计推断页。
NAV_PAGES = [
    NAV_OVERVIEW,
    NAV_PREP,
    '⏱️ 时间与质控',
    '📈 描述性分析',
    '🔬 统计推断',
    '🤖 预测建模',
    '📚 论文辅助',
    '📄 报告导出',
]


def default_import_settings() -> dict:
    return {'header_rows': 1, 'skip_rows': 0, 'fill_data_merged': False}


def current_sheet_label() -> str:
    return st.session_state.get('current_sheet') or '(当前数据)'


@st.cache_data(show_spinner=False, max_entries=8)
def cached_list_sheets(file_bytes: bytes):
    import io

    return DataLoader.list_sheets(io.BytesIO(file_bytes))


@st.cache_data(show_spinner=False, max_entries=8)
def cached_read_sheet(file_bytes: bytes, sheet_name: str, header_rows: int,
                      skip_rows: int, fill_data_merged: bool):
    import io

    return DataLoader.read_sheet(
        io.BytesIO(file_bytes),
        sheet_name=sheet_name,
        header_rows=header_rows,
        skip_rows=skip_rows,
        fill_data_merged=fill_data_merged,
    )


def prune_stale_column_state(next_columns) -> int:
    """
    换数据后清掉指向「已不存在的列」的会话状态。

    必须做这一步的原因：Streamlit 的 selectbox / multiselect 在 session_state 里
    已经有值时，**不会校验它是否还在选项里**。换一份数据（或做了多表关联、宽表转长表）
    之后，这些陈旧值仍会被原样返回，紧接着 df[该列] 就抛 KeyError ——
    而报错位置离真正的原因很远（例如时间与质控页的分母列），很难查。

    只处理「值看起来像列名」的情况（字符串、或全是字符串的列表），不碰其他会话状态。
    """
    previous = st.session_state.get('df')
    old_columns = set(map(str, previous.columns)) if previous is not None else set()
    new_columns = set(map(str, next_columns))
    if not old_columns or old_columns == new_columns:
        return 0

    removed = 0
    for key in list(st.session_state.keys()):
        if key == 'df':
            continue
        value = st.session_state.get(key)
        if isinstance(value, str):
            if value in old_columns and value not in new_columns:
                del st.session_state[key]
                removed += 1
        elif isinstance(value, (list, tuple)) and value and all(
                isinstance(item, str) for item in value):
            kept = [item for item in value
                    if item not in old_columns or item in new_columns]
            if len(kept) != len(value):
                st.session_state[key] = kept
                removed += 1
    return removed


def load_dataframe_into_session(df: pd.DataFrame, sheet_name: str = '(当前数据)',
                                source: str = 'Excel') -> None:
    """把一份数据设为当前工作区数据，并重建质量报告（自动应用人工类型指定）。"""
    if df is None or len(df) == 0:
        st.warning('数据集为空，未加载')
        return

    # 必须在替换数据**之前**清理：控件带着旧列名不放，后面各分析页就会拿到不存在的列
    prune_stale_column_state(df.columns)

    overrides = {key: value for key, value in (st.session_state.get('type_overrides') or {}).items()
                 if key in df.columns}
    if overrides:
        df = DataLoader.apply_overrides(df, overrides)
    df = df.reset_index(drop=True)
    # 识别为数值但仍是文本的列（带单位/全角写法）统一转成数值，保证各分析页可直接使用
    provisional = DataLoader.build_metadata(df, overrides=overrides)
    for column, meta in provisional.items():
        if (meta.dtype == 'numeric' and column in df.columns
                and not pd.api.types.is_numeric_dtype(df[column])):
            df[column] = DataLoader.loose_to_numeric(df[column])
    df.attrs['sheet_name'] = sheet_name

    report = DataLoader.get_quality_report(df, overrides=overrides)
    st.session_state.df = df
    st.session_state.current_sheet = sheet_name
    st.session_state.type_overrides = overrides
    st.session_state.quality_report = report
    st.session_state.report_fields = report['fields']
    st.session_state.cache.clear()
    # 台账只增不减：数据变了就插入分隔标记，而不是把旧记录一起丢掉，
    # 这样事后能看出哪一段分析是基于哪一版数据做的
    if st.session_state.get('analysis_ledger'):
        from datetime import datetime

        from modules.ledger import make_data_change

        ledger = list(st.session_state.analysis_ledger)
        if ledger and ledger[-1].get('kind') != 'data':
            ledger.append(make_data_change(source,
                                           at=datetime.now().strftime('%m-%d %H:%M')))
        st.session_state.analysis_ledger = ledger[-300:]
    st.session_state.dataset_source = source
    st.session_state.structure_profile = None
    # 换数据后旧的分析建议、整理记录与结构诊断不再适用
    st.session_state.ai_result = None
    st.session_state.ai_payload_sent = None
    st.session_state.ai_advice_question = ''
    st.session_state.schema_result = None
    # 数据变了，整理后的评估结论不再适用（整理历史本身在换数据时另行清空）
    st.session_state.closure_result = None


def push_data_snapshot(label: str) -> None:
    """
    在改动数据或导入设置之前保存当前状态，供「↩️ 回到上一步」使用。

    快照必须同时包含导入设置：只回退数据不回退设置的话，下一次 rerun 会立刻
    按旧设置重新解析一遍，撤销等于没做。
    """
    from datetime import datetime

    from modules.history import STACK_LIMIT, make_snapshot, push

    frame = st.session_state.get('df')
    if not isinstance(frame, pd.DataFrame):
        return
    st.session_state.data_history = push(
        st.session_state.get('data_history') or [],
        make_snapshot(
            df=frame,
            overrides=st.session_state.get('type_overrides'),
            import_settings=st.session_state.get('import_settings'),
            source=st.session_state.get('dataset_source') or '',
            label=label,
            at=datetime.now().strftime('%m-%d %H:%M')),
        limit=STACK_LIMIT)


def restore_data_snapshot() -> None:
    """回退到上一个数据状态（数据、类型指定、导入设置一起回退）。"""
    from modules.history import pop

    snapshot, remaining = pop(st.session_state.get('data_history') or [])
    if snapshot is None:
        st.warning('没有可回退的历史。')
        return
    frame = snapshot.get('df')
    if frame is None:
        st.warning('这条历史里没有数据，已跳过。')
        return
    st.session_state.type_overrides = dict(snapshot.get('overrides') or {})
    st.session_state.import_settings = dict(
        snapshot.get('import_settings') or default_import_settings())
    st.session_state.data_history = remaining
    load_dataframe_into_session(
        frame.copy(deep=True), sheet_name=current_sheet_label(),
        source=f"回退：{snapshot.get('label')}")
    st.success(f"已回退到执行「{snapshot.get('label')}」之前的状态")
    st.rerun()


def render_history_panel() -> None:
    """数据回退：撤销类型指定 / 整理 / 清洗 / 插补等改动过数据的操作。"""
    from modules.history import describe_stack, estimate_memory_mb, to_frame

    st.caption('改了变量类型、整理了表格、清洗或插补之后，可以在这里回退。'
               '分析类操作是只读的，不需要回退。')
    stack = st.session_state.get('data_history') or []
    if not stack:
        st.info('暂无可回退的历史。执行一次整理 / 清洗 / 类型指定后，这里会出现回退入口。')
        return

    st.markdown(f'**{describe_stack(stack)}**')
    with st.expander('📜 回退步骤明细', expanded=False):
        render_table(to_frame(stack), hide_index=True)
        memory = estimate_memory_mb(stack)
        if memory > 200:
            st.warning(f'快照共占用约 {memory} MB 内存。数据较大时建议及时清空历史，'
                       '以免程序变慢。')
        else:
            st.caption(f'快照占用约 {memory} MB 内存（最多保留 8 层）。')
        if st.button('🗑️ 清空回退历史', key='history_clear'):
            st.session_state.data_history = []
            st.rerun()

    if st.button('↩️ 回到上一步', type='primary', use_container_width=True,
                 key='history_undo'):
        restore_data_snapshot()


def detect_header_warning(df) -> str:
    """
    检测「表头错位」：报表顶部的大标题行被当成了字段名。

    典型表现是列名大量重复（标题被横向填充到每一列，再被去重成「标题_1、标题_2…」）。
    这会让后续所有分析都建立在错误的字段名上，因此需要明确提示用户去设置跳过行数。
    """
    columns = [str(name) for name in df.columns]
    if len(columns) < 3:
        return ''
    base = [re.sub(r'_\d+$', '', name) for name in columns]
    if len(set(base)) <= max(1, len(base) // 5):
        return ('⚠️ 检测到列名大量重复，很可能是报表顶部的大标题行被当成了字段名。'
                '请在侧边栏「⚙️ 导入设置 → 跳过开头行数」中填入大标题占用的行数，'
                '再点「🔄 重新解析该 Sheet」。')
    return ''


def build_project_bytes() -> bytes:
    """把当前工作区（数据 + 类型指定 + 报告章节 + AI 结果）打包成 .mda 项目文件。"""
    from modules.project import save_project

    cache = st.session_state.get('cache')
    ai_results = {}
    value = st.session_state.get('ai_result')
    if value:
        ai_results['分析建议'] = value

    return save_project(
        st.session_state.get('df'),
        type_overrides=st.session_state.get('type_overrides') or {},
        dataset_source=st.session_state.get('dataset_source') or '',
        report_title=st.session_state.get('report_title_input') or '',
        sections=(cache.sections if cache is not None else []),
        ai_results=ai_results)


def restore_project_state(state) -> None:
    """
    把项目文件的状态恢复到当前会话。

    顺序很关键：load_dataframe_into_session 会读取 type_overrides 并清空报告缓存与
    AI 结果，因此必须先设类型指定、再加载数据、最后恢复章节与 AI 结果。
    """
    st.session_state.type_overrides = dict(state.get('type_overrides') or {})
    load_dataframe_into_session(state['df'], sheet_name='项目文件',
                                source=state.get('dataset_source') or '项目文件')

    cache = st.session_state.cache
    cache.clear()
    for section in state.get('sections') or []:
        cache.add_section(section.get('title') or '',
                          section.get('content') or '',
                          table=section.get('table'),
                          image=section.get('image'),
                          level=int(section.get('level') or 1))

    ai_results = state.get('ai_results') or {}
    # 兼容合并前保存的旧项目：当时解读与方案分别存为「解读」「方案」两个键
    st.session_state.ai_result = (ai_results.get('分析建议')
                                  or ai_results.get('方案')
                                  or ai_results.get('解读'))

    if state.get('report_title'):
        st.session_state.report_title_input = state['report_title']

    parts = [f"已打开项目：{state['row_count']} 行 × {state['column_count']} 列"]
    parts.append(f"恢复 {len(state.get('sections') or [])} 个报告章节")
    if state.get('image_count'):
        parts.append(f"含 {state['image_count']} 张图")
    if state.get('created_at'):
        parts.append(f"生成于 {state['created_at']}")
    st.session_state.project_loaded_notice = '，'.join(parts)
    st.rerun()


def parse_uploaded_sheet(uploaded, sheet_name: str, settings: dict) -> None:
    """按导入设置解析指定 Sheet，并设为当前数据。"""
    file_bytes = uploaded.getvalue()
    df = cached_read_sheet(
        file_bytes,
        sheet_name,
        int(settings.get('header_rows', 1)),
        int(settings.get('skip_rows', 0)),
        bool(settings.get('fill_data_merged', False)),
    )
    # 换数据前先存回退点，误传文件或选错 Sheet 时可以退回
    push_data_snapshot('重新上传数据 / 切换 Sheet')
    st.session_state.parsed_df = df
    st.session_state.type_overrides = {}
    st.session_state.schema_action_log = []      # 换了一份数据，整理历史不再适用
    load_dataframe_into_session(df, sheet_name=sheet_name or '(未命名)', source='Excel')


def render_type_editor() -> None:
    """在数据总览页提供字段类型的人工指定。"""
    report = st.session_state.quality_report
    overrides = st.session_state.get('type_overrides') or {}
    editor_df = pd.DataFrame([
        {
            '字段': name,
            '自动识别': TYPE_LABELS.get(meta['dtype'], meta['dtype']),
            '指定类型': TYPE_LABELS.get(overrides.get(name, 'auto'), TYPE_LABELS['auto']),
        }
        for name, meta in report['fields'].items()
    ])
    st.caption('把「指定类型」改为数值后点应用，程序会剥离单位（人 / % / 个月）并按数值列处理。')
    edited = st.data_editor(
        editor_df,
        hide_index=True,
        use_container_width=True,
        height=380,
        disabled=['字段', '自动识别'],
        column_config={
            '指定类型': st.column_config.SelectboxColumn(
                '指定类型', options=TYPE_OPTIONS, required=True),
        },
        key='type_override_editor',
    )

    col_apply, col_reset = st.columns(2)
    if col_apply.button('✅ 应用类型指定', use_container_width=True):
        push_data_snapshot('应用变量类型指定')
        new_overrides = {}
        for _, row in edited.iterrows():
            code = TYPE_CODES.get(row['指定类型'])
            if code and code != 'auto':
                new_overrides[row['字段']] = code
        st.session_state.type_overrides = new_overrides
        base = st.session_state.get('parsed_df')
        if base is None:
            base = st.session_state.df
        load_dataframe_into_session(base, sheet_name=current_sheet_label(),
                                    source='Excel/人工指定类型')
        st.success(f'已应用 {len(new_overrides)} 个字段的类型指定')
        st.rerun()

    if col_reset.button('♻️ 恢复自动识别', use_container_width=True):
        base = st.session_state.get('parsed_df')
        if base is None:
            st.info('当前数据不是从 Excel 直接解析的，无法恢复自动识别')
        else:
            push_data_snapshot('恢复自动识别')
            st.session_state.type_overrides = {}
            st.session_state.ai_type_fixes_log = []
            load_dataframe_into_session(base, sheet_name=current_sheet_label(), source='Excel')
            st.rerun()

    # AI 建议的类型修正应用记录（可追溯：避免类型被改过却不知道）
    fix_log = st.session_state.get('ai_type_fixes_log') or []
    if fix_log:
        st.markdown('---')
        st.caption(f'🤖 已应用 AI 建议的类型修正（共 {len(fix_log)} 项，'
                   '如需还原可在上方「指定类型」改回后重新应用）：')
        for item in fix_log[-6:]:
            detail = (f"「{item['column']}」{item.get('from_label') or '?'} → "
                      f"{item['to_label']}（{item['at']}）")
            reason = item.get('reason') or ''
            if reason:
                detail += '　理由：' + reason
            st.markdown(f'- {detail}')
        if st.button('↩️ 清除该记录（不影响已应用的设置）', key='ai_fix_log_clear'):
            st.session_state.ai_type_fixes_log = []
            st.rerun()


def render_reshape_panel() -> None:
    """宽表转长表：把 1月…12月 这类横向铺开的列折叠成行。"""
    df = st.session_state.get('parsed_df')
    if df is None:
        df = st.session_state.df
    all_columns = list(df.columns)

    guessed = DataLoader.guess_wide_value_columns(df)
    default_ids = [column for column in all_columns if column not in guessed][:4]

    st.caption('当「月份 / 季度 / 年份」被放成列时，先折叠成「名称 + 数值」两列，才能做趋势和组间比较。')
    id_cols = st.multiselect(
        '标识列（保持不动的列，如 年份 / 指标 / 科室）',
        all_columns,
        default=default_ids,
        key='reshape_id_cols',
    )
    value_cols = st.multiselect(
        '要折叠的值列（如 1月 … 12月，已尝试自动识别）',
        [column for column in all_columns if column not in id_cols],
        default=[column for column in guessed if column not in id_cols],
        key='reshape_value_cols',
    )

    col_name, col_value = st.columns(2)
    var_name = col_name.text_input('折叠后的名称列', value='指标名称', key='reshape_var_name')
    value_name = col_value.text_input('折叠后的数值列', value='数值', key='reshape_value_name')

    if st.button('🔀 生成长表并设为当前数据', use_container_width=True):
        push_data_snapshot('宽表转长表')
        try:
            long_df = DataLoader.melt_wide_to_long(
                df, id_cols, value_cols, var_name=var_name, value_name=value_name)
        except Exception as error:
            st.error(f'❌ 转换失败：{error}')
            return
        if long_df.empty:
            st.warning('转换后没有有效数据，请检查选择的值列与标识列')
            return
        st.session_state.parsed_df = long_df
        st.session_state.type_overrides = {}
        load_dataframe_into_session(long_df, sheet_name='宽转长结果', source='宽表转长表')
        st.success(f'✅ 已生成 {len(long_df)} 行 × {len(long_df.columns)} 列，可直接用于各分析页')
        st.rerun()


def render_missing_panel() -> None:
    """缺失模式可视化与插补"""
    from modules.missing import MissingAnalysis

    df = st.session_state.df
    missing_cells = int(df.isna().sum().sum())
    if missing_cells == 0:
        st.success('✅ 当前数据没有缺失值，无需处理')
        return

    st.caption(f'当前共 {missing_cells} 个缺失单元格'
               f'（占全部单元格 {missing_cells / max(df.size, 1) * 100:.2f}%）。'
               '插补只作用于当前会话，不会改写原始 Excel。')

    col1, col2 = st.columns(2)
    with col1:
        st.markdown('**缺失率**')
        render_table(MissingAnalysis.summary(df, only_missing=True), hide_index=True)
    with col2:
        st.markdown('**缺失模式（哪些字段同时缺失）**')
        render_table(MissingAnalysis.pattern_table(df), hide_index=True)

    chart_left, chart_right = st.columns(2)
    bar_chart = MissingAnalysis.missing_bar_plot(df)
    if bar_chart:
        chart_left.image(bar_chart, use_container_width=True)
    matrix_chart = MissingAnalysis.missing_matrix_plot(df)
    if matrix_chart:
        chart_right.image(matrix_chart, use_container_width=True)

    st.markdown('**处理方式**')
    mode = st.radio('选择处理方式',
                    ['不处理（仅查看）', '单值填补（中位数 / 众数）', '多重插补（MICE）'],
                    horizontal=True, key='missing_mode')

    if mode.startswith('单值'):
        strategy = st.radio('数值列填补值', ['中位数', '均值'], horizontal=True,
                            key='missing_strategy')
        if st.button('应用单值填补', use_container_width=True):
            push_data_snapshot(f'{strategy}填补')
            filled = MissingAnalysis.simple_impute(
                df, strategy='median' if strategy == '中位数' else 'mean')
            st.session_state.parsed_df = filled
            load_dataframe_into_session(filled, sheet_name='填补后数据',
                                        source=f'{strategy}填补')
            st.success(f'✅ 已填补，剩余缺失 {int(filled.isna().sum().sum())} 个单元格')
            st.rerun()

    elif mode.startswith('多重'):
        col1, col2 = st.columns(2)
        n_imputations = col1.slider('插补份数 m', 2, 10, 5, 1, key='missing_m',
                                    help='一般 5–10 份即可')
        max_iter = col2.slider('迭代次数', 5, 30, 10, 1, key='missing_iter')
        if st.button('运行 MICE 多重插补', use_container_width=True):
            push_data_snapshot('MICE 多重插补')
            with st.spinner('正在进行多重插补...'):
                result = MissingAnalysis.mice_impute(df, n_imputations=int(n_imputations),
                                                     max_iter=int(max_iter))
            if result.get('error'):
                st.error(f"❌ {result['error']}")
                return
            st.session_state.missing_result = result
            st.rerun()

        stored = st.session_state.get('missing_result')
        if stored:
            st.success(stored['display'])
            if stored.get('unusable_columns'):
                st.warning('⚠️ 以下数值列全部缺失，无法插补，已保持原样：'
                           + '、'.join(stored['unusable_columns']))
            st.markdown('**Rubin 规则合并后的均值估计**')
            render_table(pd.DataFrame(stored['rubin_table']), hide_index=True)
            offer_report_section(
                'mice', '多重插补（MICE）', stored['display'],
                table=pd.DataFrame(stored['rubin_table']))
            if st.button('✅ 把第 1 份插补结果设为当前数据', use_container_width=True):
                filled = stored['dataset']
                st.session_state.parsed_df = filled
                st.session_state.missing_result = None
                load_dataframe_into_session(filled, sheet_name='插补后数据', source='多重插补')
                st.success('已切换为插补后数据；建议与原始数据分别分析，做敏感性对比')
                st.rerun()


# ========== AI 解读（可选功能） ==========
def current_ai_config():
    """
    从会话中保存的 AI 配置构造对象。

    配置真源是普通会话键 ai_config（不是控件键）：控件键在 st.rerun() 之后会被清空
    （例如「加载示例数据」「重新解析 Sheet」都会触发 rerun），
    若直接依赖控件键，用户刚填好的服务地址与 Key 会在加载数据后凭空消失。

    会话里没有可用配置时，退回**服务端统一配置**（环境变量 / 共享 JSON）——
    多人部署场景由管理员配一次，使用者打开就能用，不必各自填 Key。
    优先级：用户在本会话手填的 > 服务端统一配置。
    """
    from modules.ai_client import AIConfig, load_server_config

    data = dict(st.session_state.get('ai_config') or {})
    if not (str(data.get('base_url') or '').strip()
            and str(data.get('model') or '').strip()):
        server = load_server_config()
        for key in ('base_url', 'api_key', 'model', 'timeout', 'redact'):
            if server.get(key) not in (None, ''):
                data[key] = server[key]
    return AIConfig(
        base_url=data.get('base_url') or '',
        api_key=data.get('api_key') or '',
        model=data.get('model') or '',
        redact=bool(data.get('redact', True)),
        timeout=int(data.get('timeout') or 60),
    )


def run_ai(config, messages, *, label: str = '正在生成',
           temperature: float = 0.2):
    """
    统一发起一次 AI 调用：流式显示生成过程，返回完整结果。

    好处不只是「看得见进度」—— 流式下 requests 的超时是**两次数据之间的间隔**，
    本地大模型长时间生成不会被总时长掐断。
    异常不在此处吞掉，由调用方决定如何提示。
    """
    from modules.ai_client import AIClient

    box = st.empty()
    chunks: list = []

    def on_delta(piece: str) -> None:
        chunks.append(piece)
        text = ''.join(chunks)
        if len(text) > 3000:
            text = '…（前文已省略）\n' + text[-3000:]
        box.markdown(f'_⏳ {label}…_\n\n{text}▌')

    box.markdown(f'_⏳ {label}…（正在连接）_')
    try:
        return AIClient(config).chat(messages, temperature=temperature,
                                     on_delta=on_delta)
    finally:
        # 清掉流式预览：完整结果会由调用方解析后正式渲染，避免同一段内容出现两次
        box.empty()


def ai_is_enabled() -> bool:
    return current_ai_config().is_ready()


def render_ai_config_sidebar() -> None:
    """侧边栏：AI 服务配置、连通性测试、本机保存与清除。"""
    from modules.ai_client import (CONFIG_PATH, MAX_TIMEOUT, AIClient, AIError,
                                   clear_saved_config, is_local_endpoint, list_models,
                                   load_saved_config, load_server_config, save_config,
                                   summarize_server_config)

    if 'ai_config' not in st.session_state:
        saved = load_saved_config()
        st.session_state.ai_config = saved.to_dict()
        st.session_state.ai_saved_on_disk = bool(
            saved.base_url or saved.api_key or saved.model)
    cfg = dict(st.session_state.ai_config)

    st.markdown("---")
    with st.expander('🤖 AI 解读（可选）', expanded=False):
        st.caption('把「数据结构摘要」交给 AI，由它解释这份数据能做什么、为什么可行、有哪些前提与坑。')

        # 服务端统一配置（管理员部署时配一次）：使用者打开即可用，不必各自填 Key。
        # 这里只显摘要、不回显 Key；想自己改的话勾选「临时覆盖」，仅影响本会话。
        server_cfg = load_server_config()
        server_ready = bool(str(server_cfg.get('base_url') or '').strip()
                            and str(server_cfg.get('model') or '').strip())
        if server_cfg:
            st.success(f"🔧 **AI 由服务端统一配置**（无需填写，可直接使用）\n\n"
                       f"{summarize_server_config(server_cfg)}")
            if not server_ready:
                st.warning('服务端配置不完整（缺服务地址或模型名），请联系管理员，'
                           '或勾选下方选项自行填写。')
        if server_cfg:
            override = st.checkbox('临时覆盖服务端配置（仅本次会话，不影响他人）',
                                   value=not server_ready, key='ai_override_server')
            if not override:
                st.caption('如需临时改用其他服务或模型，勾选上方选项。'
                           '「记住到本机」的配置在有服务端配置时不会生效。')
                return

        st.caption('结构摘要只会发送到下方填写的 AI 服务；摘要内容可在洞察页逐字预览。')

        base_url = st.text_input(
            '服务地址', value=cfg.get('base_url') or '', key='ai_base_url_input',
            placeholder='https://api.deepseek.com 或 http://127.0.0.1:8000/v1',
            help='任何 OpenAI 兼容服务均可：DeepSeek、通义、智谱、月之暗面、'
                 'Azure OpenAI，以及本地 vLLM / Ollama（本地服务可留空 Key）')
        # 隐私结论随地址变化：指向本机 / 内网服务时数据不出内网，
        # 继续提示「不再满足零外传」会造成误导
        if is_local_endpoint(base_url):
            st.success('🔒 指向本机 / 内网服务（如自建 vLLM / Ollama）：'
                       '结构摘要只会发送到该服务，**不会离开内网**。')
        elif (base_url or '').strip():
            st.warning('⚠️ 启用后结构摘要会发送到外部 AI 服务，此时不再满足「数据零外传」。'
                       '摘要只含字段名、类型、唯一值数、缺失率与结构角色等元信息，'
                       '不含任何个体数据行，也不含均值/频数等数值统计。')
        else:
            st.caption('未填写地址前不会发送任何内容。')
        api_key = st.text_input(
            'API Key', value=cfg.get('api_key') or '', key='ai_api_key_input',
            type='password',
            help='默认只存在当前会话；勾选下方「记住到本机」才会写入文件')
        # 模型名不用 value= 而用 session_state 预填：这样「列出可用模型」才能把选中的
        # 模型写回来（on_change 回调在下一次脚本执行的开头运行，此时控件尚未实例化）
        if 'ai_model_input' not in st.session_state:
            st.session_state.ai_model_input = cfg.get('model') or ''

        def _apply_probed_model() -> None:
            picked = st.session_state.get('ai_model_pick')
            if picked and not picked.startswith('（'):
                st.session_state.ai_model_input = picked

        model = st.text_input(
            '模型名', key='ai_model_input',
            placeholder='deepseek-chat / qwen-plus / qwen2.5:14b',
            help='必须与所选服务提供的模型名一致。本地服务可用下方「📋 列出可用模型」直接选')

        col_probe, _probe_blank = st.columns([2, 3])
        if col_probe.button('📋 列出可用模型', use_container_width=True, key='ai_probe'):
            with st.spinner('正在向服务索取模型列表…'):
                try:
                    names = list_models(base_url, api_key)
                except AIError as error:
                    st.session_state.ai_model_options = []
                    st.session_state.ai_probe_result = ('error', str(error))
                else:
                    st.session_state.ai_model_options = names
                    st.session_state.ai_probe_result = (
                        'ok', f'服务返回 {len(names)} 个模型，可在下方选择')
        probe_state = st.session_state.get('ai_probe_result')
        if probe_state:
            (st.success if probe_state[0] == 'ok' else st.error)(probe_state[1])

        probed_models = st.session_state.get('ai_model_options') or []
        if probed_models:
            st.selectbox('服务返回的模型（选中即填入上方模型名）',
                         ['（保持手填）'] + probed_models, key='ai_model_pick',
                         on_change=_apply_probed_model)
        redact = st.checkbox(
            '脱敏疑似身份字段名', value=bool(cfg.get('redact', True)), key='ai_redact_input',
            help='姓名、证件号、住址、联系方式、各类编号会替换为「字段A、字段B…」；'
                 '年龄、性别等分析必需变量保留，以免削弱 AI 对方法的判断')
        remember = st.checkbox(
            '记住到本机（明文保存）', value=bool(cfg.get('remember', False)),
            key='ai_remember_input',
            help=f'写入 {CONFIG_PATH}，内容为明文，可随时一键清除')
        timeout = st.number_input(
            '超时（秒）', min_value=10, max_value=MAX_TIMEOUT, step=10,
            value=int(cfg.get('timeout') or 120), key='ai_timeout_input',
            help='流式输出下这是「两条新内容之间的最长间隔」，不是总耗时：'
                 '本地大模型建议 300 秒以上；云端推理型模型（如 deepseek-reasoner）建议 180 秒以上')

        # 每次运行都把控件值同步回配置真源：即使控件状态被 rerun 清空，
        # 下一轮也会用这里的值重新填充，用户输入不会丢失。
        st.session_state.ai_config = {
            'base_url': (base_url or '').strip(),
            'api_key': (api_key or '').strip(),
            'model': (model or '').strip(),
            'redact': bool(redact),
            'remember': bool(remember),
            'timeout': int(timeout or 120),
        }

        col_test, col_clear = st.columns(2)
        if col_test.button('🔌 测试连接', use_container_width=True, key='ai_test'):
            with st.spinner('正在测试连接…'):
                try:
                    result = AIClient(current_ai_config()).test_connection()
                except AIError as error:
                    st.session_state.ai_test_result = ('error', str(error))
                except Exception as error:
                    st.session_state.ai_test_result = ('error', f'未预期错误：{error}')
                else:
                    st.session_state.ai_test_result = (
                        'ok',
                        f"连接成功 · 模型 {result['model']} · 耗时 {result['elapsed']:.1f}s")
            if remember:
                save_config(current_ai_config())
                st.session_state.ai_saved_on_disk = True

        if col_clear.button('🗑️ 清除本机配置', use_container_width=True, key='ai_clear'):
            removed = clear_saved_config()
            st.session_state.ai_saved_on_disk = False
            st.session_state.ai_test_result = (
                'ok' if removed else 'error',
                '已清除本机保存的配置文件' if removed else '本机没有已保存的配置文件')

        test_state = st.session_state.get('ai_test_result')
        if test_state:
            kind, message = test_state
            (st.success if kind == 'ok' else st.error)(message)

        if st.button('💾 保存当前配置到本机', use_container_width=True, key='ai_save'):
            save_config(current_ai_config())
            st.session_state.ai_saved_on_disk = True
            st.success(f'已保存到 {CONFIG_PATH}（明文）')

        if st.session_state.get('ai_saved_on_disk'):
            st.caption(f'本机已保存配置文件：{CONFIG_PATH.name}（明文，可一键清除）')
        else:
            st.caption('当前配置仅存在于本次会话，关闭程序后失效。')


@st.cache_data(show_spinner=False, max_entries=8)
def cached_workbook_brief(file_bytes: bytes, settings_key: str, skip_key: str):
    """读全部 Sheet 的结构与关系。按「文件 + 读取设置 + 排除项」缓存，只算一次。"""
    import json

    from modules.multi_sheet import analyze_workbook

    return analyze_workbook(file_bytes,
                            settings_map=json.loads(settings_key),
                            skip=json.loads(skip_key))


def _run_multi_join(file_bytes, joins) -> None:
    """
    执行 AI 建议的多表关联。

    只在**全部步骤都通过**时才替换当前数据：任何一步被拦下（缺聚合方式、
    挂接后行数被放大）都保留原数据不动，由界面列出原因。
    关联结果的行数一旦被放大，后续所有统计结论都会失真，且从表面看不出来。
    """
    from modules.multi_sheet import apply_join_plan

    try:
        merged, reports = apply_join_plan(file_bytes, joins)
    except Exception as error:                              # noqa: BLE001
        st.session_state.multi_join_reports = [
            {'base_sheet': '', 'extra_sheet': '', 'key': '', 'ok': False,
             'problems': [f'关联失败：{error}']}]
        return

    st.session_state.multi_join_reports = reports
    failed = [item for item in reports if not item.get('ok')]
    if merged is None or len(merged) == 0 or failed:
        return                       # 有问题就不动数据，让界面把原因列清楚

    push_data_snapshot('多表关联（AI 建议方案）')
    load_dataframe_into_session(merged, sheet_name='多表关联结果',
                                source='多表关联')
    st.session_state.multi_join_applied = True
    st.rerun()


def render_multi_sheet_panel() -> None:
    """
    多表（每个维度一张表）分析面板。

    临床数据常见的形态是一个 Excel 里每个维度一张表，而使用者往往并不清楚
    这批数据能做什么。这里先用本地规则给出每张表的量级、主键与表间关系，
    再把结构清单交给 AI，判断「这批数据能研究什么、该怎么关联、先做哪一步」。

    关键前提：一个对象多行的表**不能直接挂接**，否则行数成倍膨胀、样本量虚增。
    所以关系判定与聚合方式都由程序把关，AI 只能给建议。
    """
    import json

    from modules.ai_client import AIError
    from modules.ai_context import (build_multisheet_payload, payload_to_json,
                                    summarize_multisheet_payload)
    from modules.ai_prompts import (MULTISHEET_SYSTEM_PROMPT,
                                    MULTISHEET_USER_TEMPLATE)
    from modules.ai_actions import AGG_CHOICES, parse_ai_output
    from modules.multi_sheet import summarize_brief

    file_bytes = st.session_state.get('excel_bytes')
    if not file_bytes:
        st.info('这块功能针对**上传的 Excel**。当前数据来自示例数据 / 项目文件 / '
                '表格整理结果，找不到原始工作簿。「每个维度一个工作表」的文件'
                '请在侧边栏上传。')
        return

    names = st.session_state.get('sheet_names') or []
    if len(names) < 2:
        st.caption('当前工作簿只有 1 个工作表，无需多表分析。'
                   '当文件里是「每个维度一个工作表」时，这里会自动给出关联建议。')
        return

    # 上一轮点了「按此方案关联」：在这里执行，不成功就保留原数据
    request = st.session_state.pop('multi_apply_request', None)
    if request:
        _run_multi_join(file_bytes, request)

    reports = st.session_state.get('multi_join_reports')
    if reports:
        st.markdown('<div class="subsection-header">最近一次关联执行</div>',
                    unsafe_allow_html=True)
        for item in reports:
            head = (f"{item.get('base_sheet') or '主表'} ◀ "
                    f"{item.get('extra_sheet') or '待挂表'}"
                    + (f"（按 {item['key']}）" if item.get('key') else ''))
            if item.get('ok'):
                st.success(f"✅ {head}：{item['rows_before']} 行 → "
                           f"{item['rows_after']} 行，匹配 {item['matched']} 个对象，"
                           f"新增 {len(item['added_columns'])} 列")
            else:
                st.error(f"❌ {head}：未执行")
            for problem in item.get('problems') or []:
                st.caption('　· ' + problem)
        if any(item.get('ok') for item in reports):
            st.caption('关联已应用到当前数据，可直接到各分析页使用。')
        if st.button('知道了', key='multi_join_notice_clear'):
            st.session_state.multi_join_reports = None
            st.rerun()

    st.caption('多个工作表**不能直接拼在一起** —— 一个对象多行的那张表会让行数成倍膨胀、'
               '样本量虚增，而且拼完从表面看不出来。这里先判断每张表是什么量级、'
               '能不能关联、该怎么聚合，再让 AI 说明这批数据能研究什么。')

    settings_key = json.dumps({}, sort_keys=True)
    skip_key = json.dumps([], sort_keys=True)
    try:
        brief = cached_workbook_brief(file_bytes, settings_key, skip_key)
    except Exception as error:                              # noqa: BLE001
        st.error(f'❌ 读取工作簿失败：{error}')
        return

    st.session_state.multi_sheet_brief = brief
    st.caption(summarize_brief(brief))

    sheets = brief.get('sheets') or []
    if not sheets:
        st.warning('没有读到可用的工作表。')
        return

    def _key_text(item):
        keys = item.get('key_candidates') or []
        return '、'.join(f"{k['column']}（{k['unique']}个唯一值）" for k in keys[:2]) or '—'

    rows = [{'工作表': item['sheet'],
             '规模': f"{item['rows']} 行 × {item['columns']} 列",
             '量级': item['level'],
             '疑似主键': _key_text(item),
             '疑似时间列': '、'.join(item.get('time_candidates') or []) or '—'}
            for item in sheets]
    st.markdown('**各表结构**')
    render_table(pd.DataFrame(rows), hide_index=True)

    relations = brief.get('relations') or []
    if relations:
        st.markdown('**表与表的关系**')
        render_table(pd.DataFrame([
            {'表 A': item['left'], '表 B': item['right'],
             '关联列': '、'.join(item['columns']),
             '关系': item['relation'],
             '能否直接挂接': '✅ 可以' if item['safe_to_join'] else '⚠️ 需先聚合',
             'A / B 唯一值数': f"{item['left_unique']} / {item['right_unique']}"}
            for item in relations]), hide_index=True)
        unsafe = [item for item in relations if not item['safe_to_join']]
        if unsafe:
            st.warning('⚠️ 上表中「需先聚合」的关系：右侧那张表一个对象有多行，'
                       '直接挂接会让行数成倍膨胀、样本量虚增。必须先按临床意义聚合'
                       '（如检验取峰值、用药取累计天数），或保留原样走重复测量分析。')
    else:
        st.info('没有发现可关联的表 —— 各表之间没有共同的主键列。'
                '若确实需要合在一起分析，检查各表的患者标识列是否用了同一个列名。')

    if brief.get('failed'):
        st.caption('未能读取的工作表：'
                   + '、'.join(f"{item['sheet']}（{item['reason']}）"
                             for item in brief['failed']))

    st.markdown('---')
    config = current_ai_config()
    if not config.is_ready():
        st.caption('未配置 AI：配置后可由 AI 判断这批数据能研究什么、'
                   '该怎么关联、先做哪一步。上方的结构识别与关系判定全部由本地规则给出。')
        return

    question = st.text_area(
        '研究问题（可留空）', key='multi_question', height=70,
        placeholder='例如：这批数据能做什么分析？或：通气策略是否影响患者预后？',
        help='留空时 AI 会按「这批数据本身能研究什么」来判断。')
    design = st.selectbox(
        '研究设计（可不填）',
        ['未指定', '回顾性队列', '前瞻性队列', '横断面调查', '病例对照',
         '随机对照试验的二次分析', '其他'],
        key='multi_design')

    payload = build_multisheet_payload(brief, question=question, design=design)
    st.caption(summarize_multisheet_payload(payload))
    with st.expander('👁️ 预览将发送的全部内容（所见即所发）', expanded=False):
        st.code(payload_to_json(payload), language='json')

    if st.button('🤖 让 AI 判断这批数据能做什么', type='primary',
                 use_container_width=True, key='multi_run'):
        messages = [
            {'role': 'system', 'content': MULTISHEET_SYSTEM_PROMPT},
            {'role': 'user', 'content': MULTISHEET_USER_TEMPLATE.format(
                payload=payload_to_json(payload))},
        ]
        with st.spinner(f'正在判断…（流式，最长间隔 {config.timeout} 秒）'):
            try:
                output = run_ai(config, messages, label='正在判断这批数据')
            except AIError as error:
                st.session_state.multi_sheet_result = None
                st.error(f'❌ AI 调用失败：{error}')
            except Exception as error:                      # noqa: BLE001
                st.session_state.multi_sheet_result = None
                st.error(f'❌ 未预期错误：{error}')
            else:
                saved = dict(output)
                saved['question'] = (question or '').strip()
                st.session_state.multi_sheet_result = saved

    result = st.session_state.get('multi_sheet_result')
    if not result:
        return

    # 多表解读里的 methods 是「用哪几张表」，字段名来自不同工作表，
    # 因此校验列的合法性要用所有表字段名的并集
    all_columns = sorted({field['name'] for item in sheets
                          for field in item['fields']})
    parsed = parse_ai_output(result.get('content') or '', columns=all_columns,
                             profile=None)

    st.markdown(parsed['body'] or '（AI 未返回内容）')
    st.caption(f"来源：{result.get('model')} · "
               f"耗时 {float(result.get('elapsed') or 0):.1f}s")
    if parsed.get('scenario'):
        st.info(f"**AI 的判断**：{parsed['scenario']}")

    joins = parsed.get('joins') or []
    if joins:
        st.markdown('<div class="subsection-header">建议的关联方案</div>',
                    unsafe_allow_html=True)
        st.caption('一对一的表可直接挂接；一对多的表必须先聚合 —— '
                   '下面的聚合方式由 AI 建议，是否合适需要你按临床意义确认。')
        rows = []
        for item in joins:
            aggs = '；'.join(f"{column}→{AGG_CHOICES.get(how, how)}"
                            for column, how in (item['aggregations'] or {}).items())
            rows.append({'主表': item['base'], '挂接': item['extra'],
                         '关联列': item['key'], '关系': item['relation'],
                         '聚合方式': aggs or '—（一对一，直接挂）',
                         '理由': item.get('why') or '—'})
        render_table(pd.DataFrame(rows), hide_index=True)
        if st.button('▶️ 按此方案关联数据', type='primary',
                     use_container_width=True, key='multi_apply'):
            st.session_state.multi_apply_request = joins
            st.rerun()

    long_format = parsed.get('long_format') or []
    if long_format:
        st.markdown('<div class="subsection-header">建议保留多次测量的表</div>',
                    unsafe_allow_html=True)
        st.caption('这些表若要研究「随时间的变化」，应转成长表走重复测量分析'
                   '（LMM / GEE），而不是聚合掉。')
        render_table(pd.DataFrame([
            {'工作表': item['sheet'], '个体标识': item['key'],
             '时间列': item.get('time') or '—',
             '指标': '、'.join(item.get('values') or []) or '—'}
            for item in long_format]), hide_index=True)

    step = parsed.get('next_step')
    if step:
        lines = [f"**建议的第一步：{step['action']}**"]
        if step.get('page'):
            lines.append(f"页签：{step['page']}")
        if step.get('needs'):
            lines.append(f"需要选择：{step['needs']}")
        if step.get('why'):
            lines.append(f"理由：{step['why']}")
        st.success('\n\n'.join(lines))

    if parsed.get('unavailable'):
        st.info('ℹ️ AI 提到程序尚未内置的方法：' + '、'.join(parsed['unavailable']))

    if parsed.get('rejected'):
        st.warning('⚠️ 以下内容未通过校验，**不会被应用**：')
        for item in parsed['rejected']:
            st.markdown(f"- {item['reason']}")

    col_report, col_note = st.columns([1, 3])
    with col_report:
        if st.button('📥 加入报告', use_container_width=True, key='multi_to_report'):
            st.session_state.cache.add_section(
                '多表数据解读与关联方案', parsed['body'] or '', level=1)
            st.success('已加入报告，可到「📄 报告导出」导出 Word')
    with col_note:
        st.caption('AI 只给建议，不会自动关联任何数据；下方点了「按此方案关联」'
                   '才会执行，且执行前会校验行数是否被意外放大。')


def build_explore_question(method, original_question: str = '') -> str:
    """把一条方法建议转成探索分析的研究问题（目标 + 涉及变量）。"""
    method = method or {}
    name = str(method.get('name') or '').strip()
    variables = method.get('variables') or {}
    parts = []
    if (original_question or '').strip():
        parts.append(str(original_question).strip())
    if name:
        parts.append(f'围绕「{name}」展开')
    if variables:
        pairs = '；'.join(f'{key}={value}' for key, value in variables.items())
        parts.append(f'涉及变量：{pairs}')
    return '；'.join(parts)[:400]


def render_explore_handoff(parsed, *, key_prefix: str,
                           original_question: str = '') -> None:
    """
    把 AI 建议的方法直接接到探索分析上（建议 → 探索）。

    这里只**预填研究问题**并展开探索面板，绝不代用户执行 —— 探索分析会逐轮把
    统计结果发送给 AI，属于需要用户明示确认的动作（见探索面板的确认项）。
    """
    methods = list(parsed.get('methods') or [])
    if not methods:
        return

    st.markdown('<div class="subsection-header">🧪 继续往下做（交给探索分析）</div>',
                unsafe_allow_html=True)
    st.caption('「一键配置」适合路线已经明确时（预填参数，由你点运行）；'
               '**探索分析**适合还不知道该往哪走时 —— AI 会自己选下一步、看结果、再决定，'
               '多轮之后给结论。点下面的按钮会把该条建议转成研究问题并填进探索面板。')

    for index, item in enumerate(methods[:3], 1):
        col_info, col_button = st.columns([5, 1])
        with col_info:
            st.markdown(f"**{index}. {item.get('name') or '分析'}**"
                        + (f" → {item['page']}" if item.get('page') else ''))
            variables = item.get('variables') or {}
            if variables:
                st.caption('涉及变量：'
                           + '；'.join(f'{key}={value}' for key, value in variables.items()))
        with col_button:
            if st.button('🧪 探索', key=f'{key_prefix}_explore_{index}',
                         use_container_width=True):
                st.session_state.agent_question = build_explore_question(
                    item, original_question)
                st.session_state.explore_handoff = {
                    'label': item.get('name') or '', 'page': item.get('page') or ''}
                st.rerun()


def render_ai_insight_block(df, profile, report) -> None:
    """
    洞察页的 AI 分析建议区块：预览 → 调用 → 结果 → 加入报告。

    「解读」与「设计方案」原是两个按钮，现合并为一个按输入分流：
    留空研究问题 → 数据概览（能做什么 / 做不了什么 / 建议的第一步）；
    填了研究问题 → 方案（能否做 / 路线 / 前提清单 / 解读口径 / 方法学段落）。
    """
    from datetime import datetime

    from modules.ai_client import AIError
    from modules.ai_context import (build_advice_payload, build_structure_payload,
                                    payload_to_json, summarize_advice_payload,
                                    summarize_payload)
    from modules.ai_prompts import ADVICE_USER_TEMPLATE, build_advice_prompt

    st.markdown('<div class="subsection-header">AI 分析建议（可选）</div>',
                unsafe_allow_html=True)
    config = current_ai_config()
    if not config.is_ready():
        st.info(
            '未配置 AI：在左侧「🤖 AI 解读（可选）」中填入服务地址与模型名即可使用。\n\n'
            '未启用不影响任何功能——上方的结构识别与推荐全部由本地规则引擎给出。'
        )
        return

    payload = build_structure_payload(
        df, profile, st.session_state.get('report_fields'), redact=bool(config.redact))
    st.caption(summarize_payload(payload))

    with st.expander('👁️ 预览将发送的结构摘要（所见即所发）', expanded=False):
        preview_text = payload_to_json(payload)
        st.code(preview_text, language='json')
        st.download_button('⬇️ 下载摘要 JSON', data=preview_text.encode('utf-8'),
                           file_name='结构摘要.json', mime='application/json',
                           key='ai_payload_download')

    question = st.text_area(
        '研究问题（可留空）', key='ai_question', height=90,
        placeholder='例如：比较 2023 年与 2024 年的 VAP 发生率是否下降？'
                    '或：哪些因素与 ICU 患者死亡独立相关？',
        help='越具体越好：写清比较对象、结局指标与时间范围。'
             '留空也能获取建议，只是 AI 会改为回答「这份数据本身能做什么」。')
    design = st.selectbox(
        '研究设计（可不填）',
        ['未指定', '回顾性队列', '前瞻性队列', '横断面调查', '病例对照',
         '随机对照试验的二次分析', '其他'],
        key='ai_design')

    ask = bool((question or '').strip())
    if ask:
        st.caption('AI 将针对你的研究问题作答：能否做、分析路线、前提检查清单、'
                   '变量与数据准备、解读口径、可写入论文的方法学表述。')
    else:
        st.caption('未填研究问题 → AI 将回答：这份数据是什么、现在能做什么、'
                   '暂时做不了什么、数据质量与改造建议、建议的第一步。')

    advice_payload = build_advice_payload(payload, question, design)
    st.caption(summarize_advice_payload(advice_payload))
    with st.expander('👁️ 预览将发送的内容（所见即所发）', expanded=False):
        advice_text = payload_to_json(advice_payload)
        st.code(advice_text, language='json')
        st.download_button('⬇️ 下载请求 JSON', data=advice_text.encode('utf-8'),
                           file_name='分析建议请求.json', mime='application/json',
                           key='ai_advice_download')

    if st.button('▶️ 获取分析建议', type='primary', use_container_width=True,
                 key='ai_advice'):
        messages = [
            {'role': 'system', 'content': build_advice_prompt(question)},
            {'role': 'user', 'content': ADVICE_USER_TEMPLATE.format(
                payload=payload_to_json(advice_payload))},
        ]
        st.session_state.ai_payload_sent = messages[1]['content']
        with st.spinner(f'正在请 AI 分析…（流式，最长间隔 {config.timeout} 秒）'):
            try:
                result = run_ai(config, messages, label='正在请 AI 分析')
            except AIError as error:
                st.session_state.ai_result = None
                st.error(f'❌ AI 调用失败：{error}')
            except Exception as error:
                st.session_state.ai_result = None
                st.error(f'❌ 未预期错误：{error}')
            else:
                # 模式随结果一起保存：用户可能改了研究问题却不重新调用，
                # 若从当前输入推断模式，会把旧结果标成新模式
                saved = dict(result)
                saved['advice_asked'] = ask
                saved['advice_question'] = (question or '').strip()
                saved['advice_design'] = design
                st.session_state.ai_result = saved
                st.session_state.ai_result_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    result = st.session_state.get('ai_result')
    if not result:
        return

    asked = bool(result.get('advice_asked'))
    parsed = parsed_ai_output(result.get('content') or '', profile)
    st.markdown(parsed['body'] or '（AI 未返回内容）')
    st.caption(f"来源：{result.get('model')} · "
               f"耗时 {float(result.get('elapsed') or 0):.1f}s · "
               f"模式：{'研究方案（针对研究问题）' if asked else '数据概览'} · "
               f"{st.session_state.get('ai_result_at', '')}")

    col_report, col_note = st.columns([1, 3])
    with col_report:
        if st.button('📥 加入报告', use_container_width=True, key='ai_to_report'):
            st.session_state.cache.add_section(
                'AI 分析方案' if asked else 'AI 数据解读',
                parsed['body'] or '', level=1)
            st.success('已加入报告，可到「📄 报告导出」导出 Word')
    with col_note:
        st.caption('AI 输出仅供参考：方法前提请自行核对，所有数值计算均由本地模块完成。'
                   'AI 只给建议，不会自动执行任何分析。')
    render_ai_review_panels(parsed, key_prefix='ai_read')
    render_explore_handoff(parsed, key_prefix='ai_read',
                           original_question=result.get('advice_question') or '')


def parsed_ai_output(content, profile):
    """解析 AI 输出（含脱敏字段名反查），正文显示与对照面板共用同一份结果。"""
    from modules.ai_actions import parse_ai_output
    from modules.ai_context import build_alias_map

    df = st.session_state.df
    config = current_ai_config()
    return parse_ai_output(
        content, columns=list(df.columns), profile=profile,
        alias_map=build_alias_map(list(df.columns), redact=bool(config.redact)))


def apply_ai_type_fixes(fixes) -> None:
    """
    把 AI 建议的字段类型写入「人工指定」，与「🏷️ 变量类型调整」走完全相同的路径，
    保证画像、推荐与各分析页的变量清单同步重算。
    """
    from datetime import datetime

    fields = st.session_state.get('report_fields') or {}
    # 只应用「确实会改变类型」的条目：AI 有时会把已经是该类型的字段也列出来，
    # 若照单全收会触发一次无意义的数据重载，并在记录里留下误导性的痕迹。
    effective = [item for item in fixes
                 if (fields.get(item['column']) or {}).get('dtype') != item['to']]
    if not effective:
        st.info('AI 建议的类型与当前识别结果一致，无需应用。')
        return

    push_data_snapshot('应用 AI 建议的字段类型修正')
    overrides = dict(st.session_state.get('type_overrides') or {})
    log = list(st.session_state.get('ai_type_fixes_log') or [])
    stamp = datetime.now().strftime('%m-%d %H:%M')
    for item in effective:
        overrides[item['column']] = item['to']
        log.append({
            'column': item['column'],
            'from_label': item.get('from_label') or '',
            'to_label': item.get('to_label'),
            'reason': item.get('reason') or '',
            'at': stamp,
        })
    st.session_state.type_overrides = overrides
    st.session_state.ai_type_fixes_log = log[-30:]
    base = st.session_state.get('parsed_df')
    if base is None:
        base = st.session_state.df
    load_dataframe_into_session(base, sheet_name=current_sheet_label(),
                                source='Excel/AI 建议类型修正')
    st.rerun()


def record_schema_action(action, *, rows=None, columns=None) -> None:
    """
    记录一次已执行的整理操作。

    闭环评估时 AI 只看到「整理后的摘要」，不知道数据是怎么变成这样的
    （某字段消失是因为删列，还是因为改了名？）。这份记录补上因果链，
    同时也让用户能回看自己到底改过什么。

    rows / columns 应传入「执行前」的形状：本函数通常在数据已被替换之后才调用，
    不显式传入就会把整理后的形状误记成整理前。
    """
    from datetime import datetime

    from modules.ai_schema import make_log_entry

    frame = st.session_state.get('df')
    if rows is None:
        rows = len(frame) if frame is not None else 0
    if columns is None:
        columns = len(frame.columns) if frame is not None else 0
    log = list(st.session_state.get('schema_action_log') or [])
    log.append(make_log_entry(action, rows=rows, columns=columns,
                              at=datetime.now().strftime('%m-%d %H:%M')))
    st.session_state.schema_action_log = log[-20:]
    # 勾选了「整理后自动继续」时，标记一次待执行的闭环评估
    if st.session_state.get('schema_auto_closure'):
        st.session_state.schema_pending_closure = True


def render_action_list(actions, *, key_prefix: str) -> None:
    """
    渲染整理动作清单：**勾选若干项，再点一次按钮按顺序应用**。

    不给每行配「单独执行」按钮：那样一行两个按钮看着像"必须逐条执行"，
    而且列表一长就要在"勾"和"点"之间来回跳。渲染与执行分开之后，
    交互只剩两步 —— 勾选、点应用。

    批量应用时顺序有讲究 —— 「读取设置」类必须先跑（它要重新解析原始文件），
    否则「数据变换」类会作用在旧数据上（见 apply_schema_actions）。这一点在界面上
    明确写出来，避免用户以为顺序无所谓。
    """
    from modules.ai_schema import describe_actions

    if not actions:
        return
    st.info(f'💡 **勾选要执行的步骤，再点下方的「应用」按钮** —— 可一次勾选多项，'
            f'也可以只勾一条。{describe_actions(actions)}。'
            '执行顺序按列表顺序，且「读取设置」类一定先于「数据变换」类 —— '
            '前者要重新解析原始文件，不然后者会作用在旧数据上。')

    head_pick, head_info = st.columns([0.6, 5.4])
    head_pick.markdown('<div style="font-size:0.78rem;color:#64748b;">勾选</div>',
                       unsafe_allow_html=True)
    head_info.markdown('<div style="font-size:0.78rem;color:#64748b;">整理步骤</div>',
                       unsafe_allow_html=True)

    picked = []
    for index, action in enumerate(actions, 1):
        is_reparse = action.get('scope') == 'reparse'
        col_pick, col_info = st.columns([0.6, 5.4])
        with col_pick:
            chosen = st.checkbox(
                f'选择第 {index} 项', key=f'{key_prefix}_pick_{index}',
                label_visibility='collapsed',
                help='勾选这一条；勾好后点下方按钮应用')
        with col_info:
            detail = '、'.join(f'{key}={value}'
                              for key, value in (action['params'] or {}).items())
            tag = '读取设置' if is_reparse else '数据变换'
            st.markdown(f"**{index}. {action['label']}**　`{detail}`　"
                        f"<span style='color:#64748b;font-size:0.78rem;'>[{tag}]</span>",
                        unsafe_allow_html=True)
            if action.get('why'):
                st.caption('理由：' + action['why'])
        if chosen:
            picked.append(action)

    head = (f'▶️ 应用选中的 {len(picked)} 项整理步骤' if picked
            else '▶️ 应用选中的整理步骤')
    reparse_n = sum(1 for item in picked if item.get('scope') == 'reparse')
    if reparse_n and reparse_n < len(picked):
        head += f'（先 {reparse_n} 项读取设置并重新解析，再执行其余数据变换）'
    if st.button(head, type='primary', use_container_width=True,
                 disabled=not picked,
                 help='勾选上方的步骤后此按钮可用；勾了几条就应用几条',
                 key=f'{key_prefix}_batch'):
        apply_schema_actions(picked)
    if not picked:
        st.caption('勾选左侧的方框（可多选），然后点上面的按钮一次性应用。')


def _apply_transform(frame, action, overrides):
    """
    在当前数据上执行一条**数据变换**操作，返回 (新数据, 是否成功, 说明)。

    单独抽出来是为了让「单条应用」与「按顺序批量应用」共用同一份逻辑：
    批量执行需要在一个 run 内连续跑完多条，不能每条都 st.rerun()。

    overrides 是「变量类型指定」的字典（可变）：set_types 改的是它而不是数据本身，
    所以按引用传入，由调用方累积，最后统一重载数据让画像与推荐重算。
    """
    kind = str(action.get('type') or '')
    params = dict(action.get('params') or {})
    try:
        if kind == 'wide_to_long':
            result = DataLoader.melt_wide_to_long(
                frame, id_cols=params.get('id_columns') or [],
                value_cols=params.get('value_columns') or [])
        elif kind == 'drop_rows':
            column = str(params.get('column') or '')
            if column not in frame.columns:
                return frame, False, f'列「{column}」不在当前数据中'
            match = str(params.get('match') or '').strip()
            mask = frame[column].astype(str).str.strip() == match
            result = frame[~mask].reset_index(drop=True)
        elif kind == 'drop_columns':
            columns = [name for name in (params.get('columns') or [])
                       if name in frame.columns]
            if not columns:
                return frame, False, '待删除的列都不在当前数据中'
            result = frame.drop(columns=columns)
        elif kind == 'rename_columns':
            mapping = dict(params.get('mapping') or {})
            missing = [name for name in mapping if name not in frame.columns]
            if missing:
                # 不校验的话 rename 会静默忽略不存在的键，用户会以为改成功了。
                # 而这正是最常见的情形：前一条「表头行数」重新解析后列名全都变了，
                # 基于旧列名的重命名自然对不上，必须明说而不是悄悄放过。
                return frame, False, ('要重命名的字段不在当前数据中：'
                                      + '、'.join(missing[:4]))
            result = frame.rename(columns=mapping)
        elif kind == 'set_types':
            mapping = {str(key): str(value)
                       for key, value in (params.get('mapping') or {}).items()}
            missing = [name for name in mapping if name not in frame.columns]
            if missing:
                return frame, False, '字段不在当前数据中：' + '、'.join(missing[:4])
            overrides.update(mapping)
            # 类型指定不改数据本身，只影响画像与各分析页的变量清单 —— 由调用方统一重载
            return frame, True, '已指定 ' + '、'.join(
                f'{name}={value}' for name, value in list(mapping.items())[:3])
        else:
            return frame, False, f'暂不支持的操作：{kind}'
    except Exception as error:                              # noqa: BLE001
        return frame, False, f'执行出错：{error}'

    if result is None or len(result) == 0:
        return frame, False, '整理后没有数据了'
    return result, True, ''


def apply_schema_actions(actions) -> None:
    """
    按顺序应用多个整理操作。

    **分两阶段执行**，因为两类操作的作用时机不同：
      · 读取设置类（跳过行 / 表头行数 / 合并填充）改的是导入配置，必须合并成
        一次重新解析 —— 若和数据变换挤在同一轮里跑，后者会作用在**尚未重解析的
        旧数据**上，删错列、改错表头，而且用户看不出来。
      · 数据变换类则在解析完成之后，在当前数据上依次执行。

    所以读取设置类先落地、并把数据变换类挂起（存进会话）；重新解析完成后的下一轮
    再执行它们（见 apply_pending_transforms）。每一步的结果都会显示，
    失败的会说明原因，不会静默跳过。
    """
    actions = list(actions or [])
    if not actions:
        return
    reparse = [item for item in actions if item.get('scope') == 'reparse']
    transforms = [item for item in actions if item.get('scope') != 'reparse']
    frame = st.session_state.get('df')
    before_rows = len(frame) if frame is not None else 0
    before_columns = len(frame.columns) if frame is not None else 0

    if reparse:
        if st.session_state.get('upload_id') is None:
            st.warning('当前数据不是从 Excel 上传的（可能是项目文件或示例数据），'
                       '无法应用「读取设置」类操作，已跳过这些项；'
                       '下面的数据变换仍会执行。')
            reparse = []
        else:
            push_data_snapshot(f'整理：{len(reparse)} 项读取设置')
            settings = dict(st.session_state.get('import_settings')
                            or default_import_settings())
            labels = []
            for action in reparse:
                kind = str(action.get('type') or '')
                params = dict(action.get('params') or {})
                if kind == 'skip_rows':
                    settings['skip_rows'] = max(0, int(params.get('value') or 0))
                elif kind == 'header_rows':
                    settings['header_rows'] = max(0, int(params.get('value') or 1))
                elif kind == 'fill_merged':
                    settings['fill_data_merged'] = bool(params.get('value'))
                labels.append(str(action.get('label') or kind))
            st.session_state.import_settings = settings
            st.session_state.excel_state = None          # 触发侧边栏重新解析
            for action in reparse:
                record_schema_action(action, rows=before_rows, columns=before_columns)
            if transforms:
                # 挂起：等重新解析完成，下一轮再在新数据上执行
                st.session_state.pending_transform_actions = transforms
            st.success(f'已更新 {len(reparse)} 项读取设置（' + '、'.join(labels)
                       + '），正在按新设置重新解析…'
                       + (f'解析完成后会自动继续执行 {len(transforms)} 项数据变换。'
                          if transforms else ''))
            st.rerun()
            return

    if transforms:
        _run_transform_actions(transforms)


def _run_transform_actions(actions) -> None:
    """在当前数据上按顺序执行多条数据变换，并逐条记录结果。"""
    frame = st.session_state.get('df')
    if frame is None or not actions:
        return
    push_data_snapshot(f'整理：{len(actions)} 项数据变换')
    overrides = dict(st.session_state.get('type_overrides') or {})
    results = []
    for action in actions:
        label = str(action.get('label') or action.get('type') or '')
        rows_before, columns_before = len(frame), len(frame.columns)
        frame, ok, note = _apply_transform(frame, action, overrides)
        results.append({'label': label, 'ok': ok, 'note': note,
                        'rows': rows_before, 'columns': columns_before})
        if ok:
            record_schema_action(action, rows=rows_before, columns=columns_before)

    st.session_state.schema_batch_results = results
    st.session_state.type_overrides = overrides
    if not any(item['ok'] for item in results):
        # 一条都没成，不必替换数据，只把原因显示出来
        return
    succeeded = sum(1 for item in results if item['ok'])
    load_dataframe_into_session(
        frame, sheet_name=current_sheet_label(),
        source=f'AI 整理（{succeeded}/{len(results)} 项）')
    st.rerun()


def apply_pending_transforms() -> None:
    """
    执行上一轮挂起的「数据变换」类操作。

    只有在批量执行里同时选了读取设置类时才会产生挂起：那类操作要先重新解析原始
    文件，本节在重新解析之后的这一轮才跑，确保变换作用在新数据上。
    必须在页面渲染早期调用 —— 它会替换当前数据。
    """
    pending = st.session_state.pop('pending_transform_actions', None)
    if not pending:
        return
    _run_transform_actions(pending)


def render_schema_batch_results() -> None:
    """显示上一次批量整理的逐条结果。"""
    results = st.session_state.get('schema_batch_results')
    if not results:
        return
    st.markdown('<div class="subsection-header">上次批量整理的结果</div>',
                unsafe_allow_html=True)
    for index, item in enumerate(results, 1):
        head = (f"{index}. {item['label']}"
                f"（{item['rows']} 行 × {item['columns']} 列）")
        if item['ok']:
            st.success(head)
        else:
            st.error(f'{head}：{item["note"]}')
    if st.button('知道了', key='schema_batch_clear'):
        st.session_state.schema_batch_results = None
        st.rerun()


def render_schema_doctor(df) -> None:
    """
    AI 表格结构诊断：判断表格排版问题，并给出可执行的整理步骤。

    默认只发送单元格类型骨架（不含真实值）；发送前几行原样需要显式勾选，
    且字段名会脱敏。整理操作经过白名单校验，逐条由用户确认后执行。
    """
    from modules.ai_actions import extract_json_block
    from modules.ai_client import AIClient, AIError
    from modules.ai_context import payload_to_json
    from modules.ai_prompts import SCHEMA_SYSTEM_PROMPT, SCHEMA_USER_TEMPLATE
    from modules.ai_schema import (build_schema_payload, describe_actions,
                                   parse_actions, summarize_payload)

    st.caption('实际拿到的 Excel 往往不是分析型表：顶部有合并大标题、指标名在行上、'
               '时间在列上、末尾还有备注行 —— 直接导入会让变量全部错位、分析页没有可选项。'
               '这里让 AI 判断排版结构，并给出**可执行的整理步骤**，'
               '每一步你都能先看理由再决定是否应用。')

    config = current_ai_config()
    if not config.is_ready():
        st.info('未配置 AI：在左侧「🤖 AI 解读（可选）」中填入服务地址与模型名即可使用。'
                '未启用不影响其它功能。')
        return

    # 勾选了「整理后自动继续」时，上一轮整理触发的闭环评估在这里补跑
    if st.session_state.pop('schema_pending_closure', False):
        run_schema_closure(df)

    include_values = st.checkbox(
        '同时发送前 8 行原样内容（默认关闭）', value=False, key='schema_include_values',
        help='默认只发单元格类型标记（文本/数字/日期/空），不含真实值，通常已足够判断结构。'
             '开启后会发送单元格实际内容（字段名自动脱敏）；'
             '若表格含身份证号、手机号等隐私信息，请勿开启。')

    notes = []
    profile = st.session_state.get('structure_profile') or {}
    wide_columns = (profile.get('roles') or {}).get('wide_value_columns') or []
    if wide_columns:
        notes.append('本地规则引擎怀疑这些列是「时间点在列上」：'
                     + '、'.join(str(name) for name in wide_columns[:8]))
    header_warning = detect_header_warning(df)
    if header_warning:
        notes.append('本地规则引擎检测到列名大量重复，怀疑大标题行被当成了字段名。')

    payload = build_schema_payload(
        df, include_values=bool(include_values), redact=bool(config.redact),
        extra_notes=notes)
    st.caption(summarize_payload(payload))
    with st.expander('👁️ 预览将发送的全部内容（所见即所发）', expanded=False):
        st.code(payload_to_json(payload), language='json')

    if st.button('🩺 让 AI 诊断这张表', type='primary', use_container_width=True,
                 key='schema_diagnose'):
        messages = [
            {'role': 'system', 'content': SCHEMA_SYSTEM_PROMPT},
            {'role': 'user', 'content': SCHEMA_USER_TEMPLATE.format(
                payload=payload_to_json(payload))},
        ]
        with st.spinner(f'正在诊断…（流式，最长间隔 {config.timeout} 秒）'):
            try:
                output = run_ai(config, messages, label='正在诊断表格结构')
            except AIError as error:
                st.session_state.schema_result = None
                st.error(f'❌ AI 调用失败：{error}')
            except Exception as error:
                st.session_state.schema_result = None
                st.error(f'❌ 未预期错误：{error}')
            else:
                st.session_state.schema_result = dict(output)

    result = st.session_state.get('schema_result')
    if not result:
        return

    extracted = extract_json_block(result.get('content') or '')
    parsed = parse_actions((extracted.get('data') or {}).get('actions') or [],
                           list(df.columns))

    st.markdown(extracted['body'] or '（AI 未返回正文说明）')
    st.caption(f"来源：{result.get('model')} · 耗时 {float(result.get('elapsed') or 0):.1f}s")

    if extracted.get('data') is None:
        st.info('AI 未返回可执行的整理清单，以上为诊断说明。')
        return

    if not parsed['valid']:
        st.success('AI 认为这张表无需整理，或者没有可行的操作。')
    else:
        st.markdown('<div class="subsection-header">可执行的整理步骤</div>',
                    unsafe_allow_html=True)
        render_action_list(parsed['valid'], key_prefix='schema_apply')

    if parsed['rejected']:
        st.warning('⚠️ 以下建议未通过校验，不会被执行：')
        for item in parsed['rejected']:
            st.markdown(f"- {item['action']}：{item['reason']}")


def run_schema_closure(df) -> None:
    """
    调用 AI 评估整理后的数据状态，并给出建议的第一步。

    与「结构诊断」的区别：这里额外带上整理动作历史，让 AI 知道数据是怎么变成现在这样的。
    """
    from datetime import datetime

    from modules.ai_client import AIClient, AIError
    from modules.ai_context import build_structure_payload, payload_to_json
    from modules.ai_prompts import CLOSURE_SYSTEM_PROMPT, CLOSURE_USER_TEMPLATE
    from modules.ai_schema import build_closure_payload

    config = current_ai_config()
    profile = st.session_state.get('structure_profile')
    if not profile:
        # 本页在「结构画像」之前渲染，这里兜底计算，避免让 AI 收到空的推荐清单
        profile = DataLoader.profile_structure(df, st.session_state.get('report_fields'))
        st.session_state.structure_profile = profile

    structure = build_structure_payload(
        df, profile, st.session_state.get('report_fields'), redact=bool(config.redact))
    payload = build_closure_payload(
        structure, st.session_state.get('schema_action_log'),
        question=st.session_state.get('ai_question') or '',
        design=st.session_state.get('ai_design') or '未指定',
        rows=len(df), columns=len(df.columns))
    st.session_state.closure_payload_sent = payload_to_json(payload)

    messages = [
        {'role': 'system', 'content': CLOSURE_SYSTEM_PROMPT},
        {'role': 'user', 'content': CLOSURE_USER_TEMPLATE.format(
            payload=payload_to_json(payload))},
    ]
    with st.spinner(f'正在评估整理结果…（流式，最长间隔 {config.timeout} 秒）'):
        try:
            output = run_ai(config, messages, label='正在评估整理结果')
        except AIError as error:
            st.session_state.closure_result = None
            st.error(f'❌ AI 调用失败：{error}')
        except Exception as error:
            st.session_state.closure_result = None
            st.error(f'❌ 未预期错误：{error}')
        else:
            st.session_state.closure_result = dict(output)
            st.session_state.closure_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def render_schema_closure(df) -> None:
    """
    整理完成后的闭环：整理后的结构摘要 + 整理动作历史 → AI 评估 → 建议的第一步。

    这是「整理」与「分析」之间缺的那一环：整理后数据变了，之前的结构建议未必还适用；
    只有把新摘要重新交给 AI，才能判断「现在这份数据够不够支撑你想做的分析」。
    """
    from modules.ai_actions import extract_json_block
    from modules.ai_context import build_structure_payload, payload_to_json
    from modules.ai_schema import (build_closure_payload, parse_actions,
                                   summarize_closure, summarize_log)

    log = st.session_state.get('schema_action_log') or []
    st.caption('整理之后数据变了，之前的结构建议未必还适用。这里把**整理后的摘要**与'
               '**整理动作历史**重新交给 AI，评估这份数据现在够不够支撑分析，'
               '并给出**建议的第一步**。')

    if not log:
        st.info('还没有执行过整理操作。在上方「🩺 表格结构诊断」里执行整理步骤后，'
                '这里会自动接上评估。')
        return

    st.markdown(f"**已执行的整理**（{len(log)} 步）：{summarize_log(log)}")
    with st.expander('📜 整理明细', expanded=False):
        for index, item in enumerate(log, 1):
            detail = '、'.join(f'{key}={value}'
                              for key, value in (item.get('params') or {}).items())
            st.markdown(f"- {index}. {item.get('label')}　`{detail}`"
                        + (f"　（{item.get('at')}）" if item.get('at') else ''))
            if item.get('why'):
                st.caption('　当初的理由：' + item['why'])
        if st.button('🗑️ 清空整理记录', key='schema_log_clear'):
            st.session_state.schema_action_log = []
            st.session_state.closure_result = None
            st.rerun()

    config = current_ai_config()
    if not config.is_ready():
        st.caption('未配置 AI：配置后即可自动评估整理结果并给出下一步。'
                   '整理记录会保留，配置完成后随时可评估。')
        return

    profile = st.session_state.get('structure_profile')
    if not profile:
        profile = DataLoader.profile_structure(df, st.session_state.get('report_fields'))
        st.session_state.structure_profile = profile

    structure = build_structure_payload(
        df, profile, st.session_state.get('report_fields'), redact=bool(config.redact))
    payload = build_closure_payload(
        structure, log,
        question=st.session_state.get('ai_question') or '',
        design=st.session_state.get('ai_design') or '未指定',
        rows=len(df), columns=len(df.columns))
    st.caption(summarize_closure(payload))
    with st.expander('👁️ 预览将发送的全部内容（所见即所发）', expanded=False):
        st.code(payload_to_json(payload), language='json')

    col_run, col_auto = st.columns([2, 3])
    with col_run:
        if st.button('🔁 评估整理结果并给出下一步', type='primary',
                     use_container_width=True, key='schema_closure_run'):
            run_schema_closure(df)
            st.rerun()
    with col_auto:
        st.checkbox('以后每次应用整理后自动评估（每次会消耗一次 AI 调用）',
                    value=False, key='schema_auto_closure')

    result = st.session_state.get('closure_result')
    if not result:
        return

    parsed = parsed_ai_output(result.get('content') or '', profile)
    st.markdown(parsed['body'] or '（AI 未返回内容）')
    st.caption(f"来源：{result.get('model')} · 耗时 {float(result.get('elapsed') or 0):.1f}s · "
               f"{st.session_state.get('closure_at', '')}")

    if parsed.get('state'):
        st.info(f"**AI 对整理后数据的判断**：{parsed['state']}")

    step = parsed.get('next_step')
    if step:
        lines = [f"**建议的第一步：{step['action']}**"]
        if step.get('page'):
            lines.append(f"页签：{step['page']}")
        if step.get('needs'):
            lines.append(f"需要选择：{step['needs']}")
        if step.get('why'):
            lines.append(f"理由：{step['why']}")
        st.success('\n\n'.join(lines))

        # 建议的第一步同样可以一键配置（跳到页签 + 预填变量），但仍由用户点运行
        from modules.ai_setup import plan_setup

        fields_now = st.session_state.get('report_fields') or {}
        columns_now = list(df.columns)
        step_plan = plan_setup(step.get('method') or step.get('action'),
                               step.get('page'), step.get('variables'),
                               fields_now, columns_now)
        if step_plan is not None:
            if step_plan['from_ai']:
                st.caption('AI 指定：' + '；'.join(step_plan['from_ai']))
            if step_plan['guessed']:
                st.caption('程序推测（请核对）：' + '；'.join(step_plan['guessed']))
            if step_plan['tabs_hint']:
                st.caption(f"该页为标签页结构，需手动点开「{step_plan['tabs_hint']}」标签")
            if st.button('▶️ 按此方案配置并跳转', type='primary',
                         use_container_width=True, key='closure_setup_run'):
                st.session_state.pending_setup = step_plan
                st.rerun()

        # 探索入口与「该方法是否已被识别」无关：方法名没被识别时反而更需要探索
        col_step_explore, _step_blank = st.columns([1, 1])
        with col_step_explore:
            if st.button('🧪 用探索分析继续', use_container_width=True,
                         key='closure_explore_run'):
                need = step.get('needs') or ''
                st.session_state.agent_question = (
                    f"{step['action']}（涉及：{need}）" if need else step['action'])[:300]
                st.session_state.explore_handoff = {'label': step.get('action') or ''}
                st.session_state.pending_nav = NAV_OVERVIEW
                st.rerun()

    col_report, col_note = st.columns([1, 3])
    with col_report:
        if st.button('📥 加入报告', use_container_width=True, key='closure_to_report'):
            st.session_state.cache.add_section(
                '整理后评估与下一步', parsed['body'] or '', level=1)
            st.success('已加入报告，可到「📄 报告导出」导出 Word')
    with col_note:
        st.caption('AI 只给建议，不会自动执行任何分析；页签与变量请自行核对后再操作。')

    render_ai_review_panels(parsed, key_prefix='closure_view')

    # AI 若认为还需要继续整理，直接给出可执行的动作
    extracted = extract_json_block(result.get('content') or '')
    more = parse_actions((extracted.get('data') or {}).get('actions') or [],
                         list(df.columns))
    if more['valid']:
        st.markdown('<div class="subsection-header">AI 认为还需继续整理</div>',
                    unsafe_allow_html=True)
        render_action_list(more['valid'], key_prefix='closure_apply')


def apply_pending_setup() -> None:
    """
    应用 AI 建议的「一键配置」：切换页签、切到子面板、预填参数。

    必须在**页面控件实例化之前**执行 —— Streamlit 禁止在控件渲染后修改它的状态。
    这里只预填，不触发任何分析：统计方法的责任在研究者。
    """
    plan = st.session_state.get('pending_setup')
    if not plan:
        return
    st.session_state.pending_setup = None
    st.session_state.nav_choice = plan.get('page') or NAV_OVERVIEW
    panel_key, panel_value = plan.get('panel_key'), plan.get('panel_value')
    if panel_key and panel_value:
        st.session_state[panel_key] = panel_value
    for key, value in (plan.get('params') or {}).items():
        st.session_state[key] = value
    st.session_state.setup_notice = {
        'label': plan.get('label') or '',
        'page': plan.get('page') or '',
        'from_ai': list(plan.get('from_ai') or []),
        'guessed': list(plan.get('guessed') or []),
        'skipped': list(plan.get('skipped') or []),
        'tabs_hint': plan.get('tabs_hint') or '',
        'hint': plan.get('hint') or '',
    }


def render_setup_notice() -> None:
    """页面顶部提示条：说明刚刚预填了什么，提醒用户核对后再运行。"""
    notice = st.session_state.get('setup_notice')
    if not notice:
        return
    lines = [f"🤖 已按 AI 建议完成配置：**{notice.get('label') or '分析' }**"]
    if notice.get('from_ai'):
        lines.append('AI 指定的变量：' + '；'.join(notice['from_ai']))
    if notice.get('guessed'):
        lines.append('程序推测的变量（**请重点核对**）：' + '；'.join(notice['guessed']))
    if notice.get('tabs_hint'):
        lines.append(f"⚠️ 该页是标签页结构，程序无法自动切换 —— "
                     f"请点开「{notice['tabs_hint']}」标签查看")
    if notice.get('skipped'):
        for label, reason in notice['skipped']:
            lines.append(f'· {label} 未预填：{reason}')
    lines.append('需要点按钮的分析（回归、Table 1、外部验证等）**不会被自动执行**；'
                 '但部分页面在你选定变量后会自动计算（如组间比较、描述性统计），'
                 '这一类结果会直接进入报告缓存与分析台账。请核对参数，需要修改时重新选一次即可。')
    st.info('\n\n'.join(lines))
    if st.button('知道了', key='setup_notice_clear'):
        st.session_state.setup_notice = None
        st.rerun()


def render_ai_setup_panel(parsed, *, key_prefix: str) -> None:
    """
    「一键配置」区块：把 AI 的方法建议落到对应页签与控件上。

    只预填参数，不代替用户点运行 —— AI 只看得到结构摘要，不知道某个字段
    在这份数据里究竟是结局、混杂还是暴露。
    """
    from modules.ai_setup import plan_setup

    methods = list(parsed.get('methods') or [])
    if not methods:
        return
    fields = st.session_state.get('report_fields') or {}
    frame = st.session_state.get('df')
    columns = list(frame.columns) if frame is not None else []
    if not columns:
        return

    plans = []
    for item in methods:
        plan = plan_setup(item.get('name'), item.get('page'), item.get('variables'),
                          fields, columns)
        if plan is not None:
            plans.append(plan)
    if not plans:
        return

    st.markdown('<div class="subsection-header">▶️ 按 AI 建议一键配置</div>',
                unsafe_allow_html=True)
    st.caption('点击会跳到对应页签、切好子面板并预填变量 —— **不会自动运行分析**，'
               '请核对参数后再点运行。')

    for index, plan in enumerate(plans, 1):
        col_info, col_button = st.columns([5, 1])
        with col_info:
            st.markdown(f"**{index}. {plan['label']}** → {plan['page']}")
            details = []
            if plan['from_ai']:
                details.append('AI 指定：' + '；'.join(plan['from_ai']))
            if plan['guessed']:
                details.append('程序推测：' + '；'.join(plan['guessed']))
            if plan['tabs_hint']:
                details.append(f"需手动点开「{plan['tabs_hint']}」标签")
            if details:
                st.caption('　｜　'.join(details))
            for label, reason in plan['skipped']:
                st.caption(f'⚠️ {label} 未能预填：{reason}')
        with col_button:
            if st.button('配置并跳转', key=f'{key_prefix}_setup_{index}',
                         use_container_width=True):
                st.session_state.pending_setup = plan
                st.rerun()


def render_agent_run(saved) -> None:
    """渲染一次已完成的 Agent 探索（含各轮结果、结论与免责标注）。"""
    from modules.agent import CONCLUSION_DISCLAIMER, multicomp_warning

    history = saved.get('history') or []
    conclusion = saved.get('conclusion') or {}
    done = [item for item in history if not item.get('failed')]

    if saved.get('stopped_reason'):
        st.warning(f"已停止：{saved['stopped_reason']}")
    if not done:
        return

    st.markdown(f"**共执行 {len(done)} 轮分析**")
    for index, entry in enumerate(done, 1):
        with st.expander(f"第 {index} 轮：{entry.get('label')}", expanded=index == 1):
            params = entry.get('params') or {}
            if params:
                st.caption('参数：' + '、'.join(f'{key}={value}'
                                              for key, value in params.items()))
            if entry.get('table') is not None:
                render_table(entry['table'].head(12), hide_index=True)
            if entry.get('plot'):
                st.image(entry['plot'], use_container_width=True)
            st.code(entry.get('summary') or '', language='markdown')

    warning = multicomp_warning(len(done))
    if warning:
        st.warning('⚠️ ' + warning)

    if conclusion.get('conclusion'):
        st.markdown('### 探索结论')
        st.markdown(conclusion['conclusion'])
    if conclusion.get('methods_text'):
        st.markdown('### 方法学段落（核对后可使用）')
        st.markdown(conclusion['methods_text'])
    for caution in conclusion.get('cautions') or []:
        st.markdown(f'- ⚠️ {caution}')
    if conclusion:
        st.error(CONCLUSION_DISCLAIMER)

    col_report, col_clear = st.columns([1, 3])
    with col_report:
        if st.button('📥 加入报告', use_container_width=True, key='agent_to_report'):
            from modules.agent import build_history_text

            st.session_state.cache.add_section(
                '探索性分析（AI 多轮）',
                f"研究问题：{saved.get('question') or '（未指定）'}\n\n"
                + (conclusion.get('conclusion') or ''),
                table=None, level=1)
            st.session_state.cache.add_section(
                '各轮分析明细', build_history_text(done), level=2)
            st.success('已加入报告，可到「📄 报告导出」导出')
    with col_clear:
        st.caption('各轮动作与 AI 的决策理由已记入分析台账，可在「📄 报告导出」中查看与导出。')
        if st.button('🗑️ 清除本次探索结果', key='agent_clear'):
            st.session_state.agent_run = None
            st.rerun()


def render_agent_panel(df, profile) -> None:
    """
    探索性分析 Agent：连续多轮执行分析，直到给出结论。

    约束由程序强制（轮数上限、白名单、参数校验、多重比较提醒、结论标注），
    不依赖 AI 自觉。适合探索性分析，不适合验证性分析。
    """
    from datetime import datetime

    from modules.agent import (ACTIONS, MAX_ROUNDS, build_agent_messages,
                               parse_agent_reply, run_one_round)

    st.markdown('---')
    st.markdown('<div class="subsection-header">🧪 探索性分析（AI 多轮）</div>',
                unsafe_allow_html=True)
    st.caption('给一个研究问题，AI 会**连续**跑多轮分析：自己选方法、看结果、再决定下一步，'
               '最后给出结论。适合「这份数据能看出什么」这类探索；'
               '若研究假设已经确定，方法应由研究设计决定，建议用上面的「一键配置」而不是让 AI 去试。')

    config = current_ai_config()
    if not config.is_ready():
        st.info('未配置 AI：在左侧「🤖 AI 解读（可选）」中填入服务地址与模型名后即可使用。')
        return

    with st.expander('📋 AI 可执行的分析（白名单，共 {} 项）'.format(len(ACTIONS)),
                     expanded=False):
        for key, spec in ACTIONS.items():
            st.markdown(f"- `{key}`：**{spec['label']}** —— {spec['purpose']}")

    handoff = st.session_state.pop('explore_handoff', None)
    if handoff and handoff.get('label'):
        st.info(f"已按 AI 建议「{handoff['label']}」填入研究问题 —— "
                '探索分析会逐轮把统计结果发送给 AI，请核对后勾选下方确认项再点开始。'
                '若不合适，直接改上面的研究问题即可。')

    confirmed = st.checkbox(
        '我已确认：多轮分析会把各轮的统计结果表发送给 AI（截断 12 行 × 12 列，不含个体数据行）',
        key='agent_confirm',
        help='与「AI 审阅」相同的边界：不含任何个体数据行，但会包含汇总统计量。')

    question = st.text_area(
        '研究问题（可留空）', key='agent_question', height=80,
        placeholder='例如：哪些因素与住院天数独立相关？')
    max_rounds = st.slider('最多执行轮数', 1, MAX_ROUNDS, MAX_ROUNDS, 1,
                           key='agent_rounds',
                           help='每次分析都会放大假阳性风险，轮数越多越需要谨慎解读')

    # 注意：按钮的 key 不能与数据键 agent_run 同名 —— Streamlit 禁止一个 key
    # 既是控件状态又是普通会话数据
    if st.button('🧪 开始探索性分析', type='primary', use_container_width=True,
                 key='agent_run_start'):
        if not confirmed:
            st.warning('请先勾选上方确认项 —— 多轮分析会发送各轮的统计结果表。')
        else:
            _run_agent_loop(df, profile, config, question, int(max_rounds))

    saved = st.session_state.get('agent_run')
    if saved:
        render_agent_run(saved)


def _run_agent_loop(df, profile, config, question, max_rounds) -> None:
    """执行 Agent 循环。整段在一次 run 内完成，进度实时刷新。"""
    from datetime import datetime

    from modules.agent import build_agent_messages, parse_agent_reply, run_one_round
    from modules.ai_client import AIError
    from modules.ai_context import build_structure_payload
    from modules.ledger import append as ledger_append
    from modules.ledger import make_entry

    structure = build_structure_payload(
        df, profile, st.session_state.get('report_fields'), redact=bool(config.redact))
    fields = st.session_state.get('report_fields') or {}
    columns = list(df.columns)

    history: list = []
    conclusion: dict = {}
    stopped_reason = ''
    progress = st.empty()

    for round_index in range(1, max_rounds + 1):
        progress.markdown(f'_⏳ 第 {round_index} / {max_rounds} 轮：AI 正在决定下一步…_')
        messages = build_agent_messages(structure, question, history)
        try:
            reply = run_ai(config, messages, label=f'第 {round_index} 轮思考')
        except AIError as error:
            stopped_reason = f'AI 调用失败：{error}'
            break
        except Exception as error:  # noqa: BLE001
            stopped_reason = f'未预期错误：{error}'
            break

        parsed = parse_agent_reply(reply.get('content') or '')
        if not parsed['ok']:
            stopped_reason = 'AI 未返回可识别的下一步（可能是模型未按格式输出），已停止'
            break
        if parsed['action'] == 'finish':
            conclusion = parsed
            break
        if parsed['action'] != 'run':
            stopped_reason = f'AI 返回了无法执行的动作「{parsed["action"] or "空"}」'
            break

        progress.markdown(
            f'_⏳ 第 {round_index} / {max_rounds} 轮：正在执行「{parsed["analysis"]}」…_')
        outcome = run_one_round(parsed['analysis'], df, parsed['params'], fields, columns)
        if not outcome['ok']:
            # 把失败原因写进历史回告 AI，让它下一轮改正 ——
            # 不由程序悄悄替换变量（那样用户就看不到 AI 选错了什么）
            history.append({
                'kind': parsed['analysis'],
                'label': f"（本轮未执行）{parsed['analysis']}",
                'params': parsed['params'],
                'summary': ('未能执行，原因：' + '；'.join(outcome['problems'])
                            + '。请改用数据中真实存在、类型也合适的字段重新决定。'),
                'failed': True})
            progress.markdown('_⚠️ 参数未通过校验，已把原因回告 AI 重新决定…_')
            continue

        entry = outcome['entry']
        history.append(entry)
        st.session_state.analysis_ledger = ledger_append(
            st.session_state.get('analysis_ledger') or [],
            make_entry(title=f"[AI 探索] {entry['label']}",
                       content=parsed['why'] or entry['summary'][:150],
                       has_table=entry['table'] is not None,
                       has_image=entry['plot'] is not None,
                       at=datetime.now().strftime('%m-%d %H:%M'),
                       params=entry['params']))
    else:
        stopped_reason = f'已达到设定的 {max_rounds} 轮上限，AI 未给出最终结论'

    progress.empty()
    st.session_state.agent_run = {
        'history': history,
        'conclusion': conclusion,
        'stopped_reason': stopped_reason,
        'question': (question or '').strip(),
    }
    st.rerun()


def render_ai_review_panels(parsed, *, key_prefix: str) -> None:
    """
    渲染「AI 建议 vs 本地规则引擎」对照，以及需二次确认的字段类型修正。

    这里只做展示与「用户确认后」的应用，绝不自动执行任何分析。
    """
    from modules.ai_actions import summarize_comparison

    if not parsed.get('ok'):
        if parsed.get('notice'):
            st.caption(parsed['notice'])
        return

    comparison = parsed['comparison']
    st.markdown('<div class="subsection-header">AI 建议 vs 本地规则引擎</div>',
                unsafe_allow_html=True)
    st.caption(summarize_comparison(comparison) +
               ' 两者不一致时请自行判断：AI 更熟悉方法学，规则引擎更清楚这份数据实际具备什么。')

    if comparison['conflicts']:
        st.warning('⚠️ **分歧**：以下分析 AI 认为可做，但规则引擎判定当前数据条件不足，'
                   '请先确认缺什么再决定是否继续。')
        render_table(pd.DataFrame([
            {'AI 建议': item['name'],
             '规则引擎对应条目': item['local_title'] or '—',
             '未满足条件': '；'.join(item['blockers']) or '—'}
            for item in comparison['conflicts']]), hide_index=True)

    if comparison['both']:
        st.markdown(f'**✅ 双方一致（{len(comparison["both"])} 条）**')
        render_table(pd.DataFrame([
            {'分析': item['name'], '规则引擎对应条目': item['local_title'] or '—'}
            for item in comparison['both']]), hide_index=True)

    col_ai, col_local = st.columns(2)
    with col_ai:
        st.markdown(f'**🆕 仅 AI 建议（{len(comparison["ai_only"])} 条）**')
        if comparison['ai_only']:
            for item in comparison['ai_only']:
                page = f' → {item["page"]}' if item.get('page') else ''
                st.markdown(f'- {item["name"]}{page}')
        else:
            st.caption('无')
    with col_local:
        st.markdown(f'**📋 仅规则引擎推荐（{len(comparison["local_only"])} 条）**')
        if comparison['local_only']:
            for item in comparison['local_only']:
                st.markdown(f'- {item["name"]}')
        else:
            st.caption('无')

    # ---------- 字段类型修正：二次确认后才应用 ----------
    fixes = parsed['type_fixes']
    st.markdown('<div class="subsection-header">AI 建议的字段类型修正</div>',
                unsafe_allow_html=True)
    if not fixes:
        st.caption('AI 未提出字段类型修正。')
    else:
        st.caption('类型改动会影响所有后续分析（含已做过的），请逐条核对后再确认应用。')
        fields = st.session_state.get('report_fields') or {}
        rows = []
        for item in fixes:
            current = (fields.get(item['column']) or {}).get('dtype', '未知')
            item['from_label'] = TYPE_LABELS.get(current, str(current))
            target = item['to_label']
            if current == item['to']:
                target += '（与当前一致，无需改动）'
            rows.append({'字段': item['column'], '当前类型': item['from_label'],
                         'AI 建议改为': target,
                         '理由': item['reason'] or '—'})
        render_table(pd.DataFrame(rows), hide_index=True)

        confirmed = st.checkbox('我已核对上述修正，确认应用到当前数据',
                                key=f'{key_prefix}_fix_confirm')
        if st.button('✅ 应用这些修正', key=f'{key_prefix}_fix_apply',
                     use_container_width=True):
            if not confirmed:
                st.warning('请先勾选确认项 —— 类型改动会影响所有后续分析。')
            else:
                apply_ai_type_fixes(fixes)

    if parsed['rejected']:
        st.warning('⚠️ 以下建议未通过校验，**不会**被应用：')
        for item in parsed['rejected']:
            st.markdown(f"- 「{item['column'] or '未命名字段'}」→ "
                        f"{item['to'] or '未指定类型'}：{item['reason']}")

    if parsed['unavailable']:
        st.info('ℹ️ AI 提到程序尚未内置的方法：' + '、'.join(parsed['unavailable']) +
                '（可在程序内选择最接近的替代做法）')

    render_ai_setup_panel(parsed, key_prefix=key_prefix)


def offer_report_section(key: str, title: str, text: str = '', *, table=None,
                         image=None, level: int = 2, note: str = '',
                         group_sections=None) -> None:
    """
    统一的「点按钮才收录」控件 —— 各分析页的结果只在这里进报告。

    背景：这些页面原本是「选完变量就自动写进报告」，而报告的章节名带变量名
    （`A vs B - 相关分析`、`年龄 - 分布直方图`）。换几个变量组合下来，报告里
    就堆满用不着的条目。实测最普通的一种用法：只把 8 个页签浏览一遍、一个按钮
    都不点，报告就从 1 章涨到 11 章，其中 10 章是默认变量的分布图这类用户从未
    要求的内容。

    统一后的规则只有一条：**结果照常显示与预览，只有点了这个按钮的那一条才进报告**。

    key            本页唯一标识（避免控件键冲突）
    group_sections 与主结果一起收录的附加章节，元素为 (标题, 正文, 表格, 图片)。
                   用于「一个字段的三张分布图」这类天然成组的结果 —— 一次收录一组，
                   而不是让用户点三次。
    """
    note = note or '结果不会自动写入报告 —— 需要写进报告时点这里。'
    col_button, col_note = st.columns([1, 4])
    with col_button:
        if st.button('📥 加入报告', key=f'report_{key}',
                     use_container_width=True):
            cache = st.session_state.cache
            cache.add_section(title, text or '', table=table, image=image,
                              level=level)
            total = 1
            for extra in (group_sections or []):
                extra_title, extra_text = extra[0], extra[1]
                extra_table = extra[2] if len(extra) > 2 else None
                extra_image = extra[3] if len(extra) > 3 else None
                cache.add_section(extra_title, extra_text or '',
                                  table=extra_table, image=extra_image,
                                  level=level)
                total += 1
            st.success(f"已收录「{title}」" +
                       (f'，连同另外 {total - 1} 个章节' if total > 1 else ''))
    with col_note:
        st.caption(note)


def store_panel_result(key: str, result, signature=None) -> None:
    """把某个分析面板的运行结果存进会话（见 panel_result 的说明）。"""
    st.session_state[f'_panel_result_{key}'] = {'result': result,
                                                'signature': signature}


def panel_result(key: str, signature=None):
    """
    取某分析面板最近一次运行的结果。

    这些面板的结果原本只活在「点了运行」的那一轮里 —— 之后再点页面上的任何
    控件（包括结果下方的「📥 加入报告」）都会触发 rerun，屏幕上刚算出来的结果
    就消失了：对一次要跑几十秒的建模来说，这既让人以为程序出错，也逼着用户
    重复运行。现在统一把结果存进会话，运行一次可以反复查看、收录。

    signature 用于确认「参数没被改过」：若用户改了变量选择，旧结果不再显示，
    而不是把上一次的模型结果张冠李戴地留在屏幕上。
    """
    stored = st.session_state.get(f'_panel_result_{key}')
    if not stored:
        return None
    if signature is not None and stored.get('signature') != signature:
        return None
    return stored.get('result')


def render_inline_ai_write(key: str, context_title: str, summary: str = '',
                           tables=None, material: str = '') -> None:
    """
    分析页结果下方的「让 AI 写这段结果」小组件。

    key           本页唯一标识，避免控件键冲突
    context_title 这段分析的名字（同时作为报告章节名）
    summary       一句话结论，作为材料的一部分
    tables        结果表格（DataFrame 或列表）
    material      额外材料文本
    """
    from datetime import datetime

    from modules.ai_client import AIClient, AIError
    from modules.ai_prompts import WRITE_SYSTEM_PROMPT, WRITE_USER_TEMPLATE

    config = current_ai_config()
    if not config.is_ready():
        return  # 未配置 AI 时保持安静，不占用结果区

    if isinstance(tables, pd.DataFrame):
        tables = [tables]
    parts = [f'### {context_title}']
    if summary:
        parts.append(str(summary))
    for frame in (tables or []):
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            parts.append(frame.iloc[:15, :12].to_string(index=False))
    if material:
        parts.append(str(material)[:2000])
    payload = '\n'.join(parts)[:8000]

    with st.expander('✍️ 让 AI 写这段结果', expanded=False):
        st.caption('把这段分析的结果交给 AI，写成可直接放进论文的段落。'
                   '发送内容是统计结果（不含任何个体数据行），先勾选确认再生成。')
        if st.checkbox('查看将发送的内容', key=f'{key}_ai_preview'):
            st.code(payload, language='markdown')
        confirmed = st.checkbox('确认发送该段统计结果', key=f'{key}_ai_confirm')
        col_kind, col_lang = st.columns(2)
        kind = col_kind.selectbox('段落类型', ['结果（Results）', '方法（Methods）'],
                                  key=f'{key}_ai_kind')
        language = col_lang.selectbox('语言', ['中文', '英文'], key=f'{key}_ai_lang')
        if st.button('✍️ 生成段落', key=f'{key}_ai_run', use_container_width=True):
            if not confirmed:
                st.warning('请先勾选确认项。')
            else:
                messages = [
                    {'role': 'system', 'content': WRITE_SYSTEM_PROMPT},
                    {'role': 'user', 'content': WRITE_USER_TEMPLATE.format(
                        kind=kind, language=language, payload=payload)},
                ]
                with st.spinner(f'正在撰写…（流式，最长间隔 {config.timeout} 秒）'):
                    try:
                        output = run_ai(config, messages, label='正在撰写这段结果',
                                        temperature=0.3)
                    except AIError as error:
                        st.session_state[f'{key}_ai_result'] = None
                        st.error(f'❌ AI 调用失败：{error}')
                    except Exception as error:
                        st.session_state[f'{key}_ai_result'] = None
                        st.error(f'❌ 未预期错误：{error}')
                    else:
                        st.session_state[f'{key}_ai_result'] = {
                            **output, 'kind': kind, 'language': language,
                            'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

        ai_result = st.session_state.get(f'{key}_ai_result')
        if ai_result:
            st.markdown(ai_result.get('content') or '（AI 未返回内容）')
            st.caption(f"来源：{ai_result.get('model')} · {ai_result.get('kind')} · "
                       f"{ai_result.get('language')} · {ai_result.get('at', '')}")
            if st.button('📥 加入报告', key=f'{key}_ai_to_report', use_container_width=True):
                st.session_state.cache.add_section(
                    f'AI 撰写段落（{context_title}）', ai_result.get('content') or '', level=1)
                st.success('已加入报告，可到「📄 报告导出」导出 Word')


def render_data_prep() -> None:
    """🧹 数据准备：整理数据、调整类型、处理缺失、关联多表 —— 以及查看当前数据。

    这些内容原先都挤在「📊 总览与洞察」页里（那里有 7 个折叠区），与「看总览、
    拿建议」混在一起。但两者是不同环节：这里是**动手改数据**，通常只在开始分析前
    做一次；首页应当承担「看结论、拿建议」。拆开后首页回到简洁，这一页专管改数据 ——
    也正因为它们混在一起，首页此前有 14 个折叠区、要滚动很久才能找到想看的东西。
    """
    st.markdown("""
    <div class="section-header">
        <h2>🧹 数据准备</h2>
    </div>
    """, unsafe_allow_html=True)

    df = st.session_state.df
    summary = st.session_state.quality_report['summary']

    if summary['numeric_count'] == 0:
        st.warning(
            '⚠️ 当前没有任何数值变量，分析页会没有可选项。'
            '常见原因：表格顶部有标题行 / 合并单元格、数值列带单位、'
            '或数据是「月份在列上」的宽表。'
            '可在下方展开「变量类型调整」或「宽表转长表」处理；'
            '也可在侧边栏调整表头行后重新解析。'
        )

    # 结果报告放在这里而不是动作清单内：批量执行后列名已变，AI 的那些建议会全部失效、
    # 清单不再渲染，报告若挂在清单里就再也看不到了
    render_schema_batch_results()

    with st.expander('↩️ 回到上一步（撤销整理 / 清洗 / 类型改动）', expanded=False):
        render_history_panel()

    with st.expander('🩺 表格结构诊断（AI 帮你看懂该怎么整理）', expanded=False):
        render_schema_doctor(df)

    with st.expander('🔁 整理后评估与下一步（AI 闭环）', expanded=False):
        render_schema_closure(df)

    with st.expander('🔗 多表分析（一个 Excel 里每个维度一张表）', expanded=False):
        render_multi_sheet_panel()

    with st.expander('🏷️ 变量类型调整（自动识别不准时手动指定）',
                     expanded=summary['numeric_count'] == 0):
        render_type_editor()

    with st.expander('🧩 宽表转长表（把 1月…12月 折叠成行）', expanded=False):
        render_reshape_panel()

    with st.expander('🩹 缺失模式与多重插补', expanded=False):
        render_missing_panel()

    # 当前数据的查看 / 筛选 / 导出（原「📌 数据摘录」页）：与上面同属
    # 「处理当前这份数据」，合并进来后导航少一个页签
    st.markdown('<div class="subsection-header">当前数据（查看 / 筛选 / 导出）</div>',
                unsafe_allow_html=True)
    render_data_extract()


def render_overview_and_insights() -> None:
    """📊 总览与洞察：数据概况 → 结构画像 → 推荐分析 → AI 建议（数据准备已独立成页）"""
    st.markdown("""
    <div class="section-header">
        <h2>📊 总览与洞察</h2>
    </div>
    """, unsafe_allow_html=True)

    df = st.session_state.df
    report = st.session_state.quality_report
    summary = report['summary']

    # ---------- ⓪ 表头错位提醒 ----------
    # 报表顶部的大标题被当成字段名时，后续所有分析都建立在错误列名上，必须先纠正
    header_warning = detect_header_warning(df)
    if header_warning:
        st.warning(header_warning)

    # ---------- ① 数据概况 ----------
    col1, col2, col3, col4 = st.columns(4)
    col1.metric('样本量', f"{report['n_rows']:,}")
    col2.metric('变量数', f"{report['n_cols']:,}")
    col3.metric('缺失率', f"{report['missing_pct']}%",
                delta='良好' if report['missing_pct'] < 5 else '需关注',
                delta_color='normal' if report['missing_pct'] < 5 else 'inverse')
    col4.metric('重复行', f"{report['duplicate_rows']}",
                delta='无重复' if report['duplicate_rows'] == 0 else '需处理',
                delta_color='normal' if report['duplicate_rows'] == 0 else 'inverse')

    type_columns = st.columns(5)
    type_columns[0].metric('🔢 数值变量', summary['numeric_count'])
    type_columns[1].metric('📋 分类变量', summary['categorical_count'])
    type_columns[2].metric('⚖️ 二分类变量', summary['binary_count'])
    type_columns[3].metric('📅 日期变量', summary['datetime_count'])
    type_columns[4].metric('📝 文本变量', summary['text_count'])

    # ---------- ② 结构画像 ----------
    # （「数据准备」的 7 个折叠区已移到独立的「🧹 数据准备」页，见 render_data_prep）
    profile = st.session_state.get('structure_profile')
    if profile is None:
        profile = DataLoader.profile_structure(df, st.session_state.get('report_fields'))
        st.session_state.structure_profile = profile

    roles = profile['roles']
    reasons = profile['reasons']
    if profile['has_time_series']:
        time_label = '有（可做趋势）'
    elif profile['has_date_field']:
        time_label = '仅日期字段'
    else:
        time_label = '无'

    st.markdown('<div class="subsection-header">识别到的结构</div>', unsafe_allow_html=True)
    st.caption(f"时间维度：{time_label}｜分组变量 {len(roles['grouping'])} 个｜"
               f"数值变量 {len(roles['numeric'])} 个｜"
               f"二分类结局候选 {len(roles['outcome_binary'])} 个")
    st.caption('判据来自数据结构本身（字段类型、取值个数、列名语义），不预设科室或业务场景。')

    role_labels = [
        ('identifier', '标识列（不参与统计）'),
        ('time_fields', '时间字段'),
        ('wide_value_columns', '宽表时间列'),
        ('grouping', '分组变量'),
        ('outcome_binary', '二分类结局候选'),
        ('count', '计数型指标'),
        ('rate', '率 / 密度型指标'),
        ('metric_column', '指标名维度列'),
        ('denominator', '强度分母候选'),
        ('duration', '随访 / 时间量'),
        ('paired', '配对列'),
        ('numeric', '数值变量'),
        ('categorical', '分类变量'),
    ]
    role_rows = []
    for key, label in role_labels:
        values = roles.get(key) or []
        if not values:
            continue
        if key == 'paired':
            shown = '、'.join(f'{before} – {after}' for before, after in values[:3])
        else:
            shown = '、'.join(str(value) for value in values[:6])
            if len(values) > 6:
                shown += f' … 等 {len(values)} 个'
        role_rows.append({
            '结构角色': label,
            '数量': len(values),
            '命中字段': shown,
            '判据': '；'.join(reasons.get(key, [])[:2]) or '—',
        })
    if role_rows:
        render_table(pd.DataFrame(role_rows), hide_index=True)
    else:
        st.info('还没有识别到可用于统计的结构，请在上方「🏷️ 变量类型调整」中修正字段类型。')

    # ---------- ④ AI 解读（可选） ----------
    render_ai_insight_block(df, profile, report)

    # ---------- ④-b 探索性分析（AI 多轮，可选） ----------
    render_agent_panel(df, profile)

    # ---------- ⑤ 推荐分析 ----------
    st.markdown('<div class="subsection-header">推荐分析（按数据支持度）</div>',
                unsafe_allow_html=True)
    for item in profile['recommendations']:
        col_main, col_action = st.columns([5, 1])
        with col_main:
            st.markdown(f"**{'✅' if item['ready'] else '⛔'} {item['title']}**")
            st.caption(f"判据：{item['evidence']}")
            for blocker in item['blockers']:
                st.caption(f"· 未满足：{blocker}")
            if item.get('hint'):
                st.caption(f"· 提示：{item['hint']}")
        with col_action:
            if item['ready'] and item['page'] in NAV_PAGES:
                if st.button('打开 →', key=f"insight_open_{item['id']}", use_container_width=True):
                    st.session_state.pending_nav = item['page']
                    st.rerun()
            else:
                st.caption('—')

    blocked = [item for item in profile['recommendations'] if not item['ready']]
    if blocked:
        st.markdown('<div class="subsection-header">暂时做不了的分析</div>',
                    unsafe_allow_html=True)
        for item in blocked:
            st.warning(f"⛔ {item['title']}：{'；'.join(item['blockers'])}")

    # ---------- ⑥ 字段元数据（折叠） ----------
    dtype_map = {
        'numeric': '🔢 数值',
        'categorical': '📋 分类',
        'binary': '⚖️ 二分类',
        'datetime': '📅 日期',
        'text': '📝 文本',
    }
    meta_df = pd.DataFrame([
        {
            '字段': name,
            '类型': dtype_map.get(meta['dtype'], meta['dtype']),
            '有效样本': meta['n_total'] - meta['n_missing'],
            '缺失': f"{meta['missing_pct']}%",
            '唯一值': meta['n_unique'],
            '异常值': (str(meta['outlier_count'])
                     if meta.get('outlier_count') is not None else '-'),
        }
        for name, meta in report['fields'].items()
    ])
    with st.expander('📋 字段元数据（逐字段缺失率、唯一值、异常值）', expanded=False):
        render_table(meta_df, hide_index=True)

    # ---------- ⑦ 更进阶的方法（折叠） ----------
    gaps = profile.get('gaps') or []
    with st.expander('🧭 更进阶的方法（当前版本未内置）', expanded=False):
        if gaps:
            gap_df = pd.DataFrame([
                {
                    '分析': gap['title'],
                    '数据结构': '✅ 已满足' if gap['structure_ready'] else '⚠️ 未满足',
                    '说明': gap['why'],
                    '建议动作': gap['action'],
                }
                for gap in gaps
            ])
            render_table(gap_df, hide_index=True)
        else:
            st.caption('当前识别到的结构所需分析均已支持。')

    # 报告缓存
    st.session_state.cache.add_section(
        '总览与洞察',
        f"样本量: {report['n_rows']}, 变量数: {report['n_cols']}, "
        f"缺失率: {report['missing_pct']}%",
        table=meta_df, level=1)


def render_time_quality() -> None:
    """⏱️ 时间与质控：率 / 密度、SPC 控制图、趋势与同比环比"""
    from modules.quality import RateAnalysis, SpcChart, TrendTest

    st.markdown("""
    <div class="section-header">
        <h2>⏱️ 时间与质控</h2>
    </div>
    """, unsafe_allow_html=True)

    df = st.session_state.df
    fields = st.session_state.report_fields
    numeric_fields = [k for k, v in fields.items()
                      if k in df.columns and v['dtype'] == 'numeric']
    cat_fields = [k for k, v in fields.items()
                  if k in df.columns and v['dtype'] in ('categorical', 'binary')]
    all_columns = list(df.columns)

    profile = st.session_state.get('structure_profile')
    if profile is None:
        profile = DataLoader.profile_structure(df, fields)
        st.session_state.structure_profile = profile

    if len(profile['roles']['wide_value_columns']) >= 3:
        st.info('💡 检测到「时间点在列上」的宽表结构：请先到「🧹 数据准备 → 🧩 宽表转长表」'
                '折叠成 时间–数值 两列，本页才能按时间顺序分析。')
    if not numeric_fields:
        st.warning('⚠️ 当前没有数值变量，可到「🧹 数据准备 → 🏷️ 变量类型调整」指定后再来。')
        return

    # 「重复测量（LMM / GEE）」与「时间序列预测」已移到「🤖 预测建模」——
    # 它们是建模方法，不是质控工具，混在质控页里既不合适，也让这一页过长。
    tab_rate, tab_spc, tab_trend = st.tabs(
        ['率与置信区间', 'SPC 控制图', '趋势与环比同比'])

    # ---------- 率与置信区间 ----------
    with tab_rate:
        col1, col2 = st.columns(2)
        numerator_col = col1.selectbox('分子列（事件数 / 例数）', numeric_fields, key='tq_rate_num')
        denominator_col = col2.selectbox('分母列（总例数 / 暴露量）',
                                         [c for c in numeric_fields if c != numerator_col],
                                         key='tq_rate_den')

        col3, col4 = st.columns(2)
        kind = col3.radio('分母含义', ['例数（率 %）', '暴露量（密度 ‰）'],
                          horizontal=True, key='tq_rate_kind')
        ci_choice = col4.selectbox('置信区间方法',
                                   ['Wilson 得分法', 'Clopper-Pearson 精确法', 'Wald 法'],
                                   key='tq_rate_ci')
        group_col = st.selectbox('分组变量（可选，用于两组比较）', ['(不分组)'] + cat_fields,
                                 key='tq_rate_group')

        # 双保险：换数据后控件可能仍返回旧列名（根因已在 load_dataframe_into_session
        # 里清理，见 prune_stale_column_state）。这里再校验一次 —— 否则 df[不存在的列]
        # 会直接抛 KeyError，而报错位置离真正的原因很远，极难排查。
        stale = [name for name in (numerator_col, denominator_col)
                 if name not in df.columns]
        if stale:
            st.error('⚠️ 原先选择的「' + '、'.join(stale) + '」已不在当前数据中'
                     '（换过数据或做过多表关联）。已临时改用前两个数值列，'
                     '请在上方重新选择后再看结果。')
            numerator_col = numeric_fields[0]
            denominator_col = next((name for name in numeric_fields
                                    if name != numerator_col), numeric_fields[0])

        numerator = pd.to_numeric(df[numerator_col], errors='coerce')
        denominator = pd.to_numeric(df[denominator_col], errors='coerce')
        events_total = float(numerator.sum(skipna=True))
        denominator_total = float(denominator.sum(skipna=True))
        method_map = {'Wilson 得分法': 'wilson',
                      'Clopper-Pearson 精确法': 'exact',
                      'Wald 法': 'wald'}
        is_proportion = kind.startswith('例数')

        if is_proportion:
            overall = RateAnalysis.proportion_ci(events_total, denominator_total,
                                                 method=method_map[ci_choice])
            overall_label = '总体率（95%CI）'
        else:
            overall = RateAnalysis.poisson_rate_ci(events_total, denominator_total)
            overall_label = '总体密度（95%CI）'

        if 'error' in overall:
            # 这条「分子应小于分母」的报错，最常见的成因是两列还没选对（默认选的是
            # 前两个数值列），不是数据本身有问题。用中性提示引导重选，避免一进页面
            # 就先给出一个像报错的警告。
            st.info(f"💡 当前这两列还不能算率：{overall['error']}。"
                    '请把上方两列换成真正的「事件数列」与「总例数列」——'
                    '事件数应当是总例数的一部分，即分子合计应小于分母合计。')
        else:
            st.metric(overall_label, overall['display'])
            st.caption(f"分子合计 {events_total:g}，分母合计 {denominator_total:g}"
                       f"（{overall.get('method', 'Poisson 精确法')}）")

        if group_col != '(不分组)':
            groups = [g for g in pd.unique(df[group_col].dropna())][:10]
            rows = []
            for group in groups:
                mask = df[group_col] == group
                group_events = float(numerator[mask].sum(skipna=True))
                group_denominator = float(denominator[mask].sum(skipna=True))
                if is_proportion:
                    group_result = RateAnalysis.proportion_ci(group_events, group_denominator,
                                                              method=method_map[ci_choice])
                    scale = 100
                else:
                    group_result = RateAnalysis.poisson_rate_ci(group_events, group_denominator)
                    scale = 1
                if 'error' in group_result:
                    continue
                rows.append({
                    '组别': str(group),
                    '分子': group_events,
                    '分母': group_denominator,
                    '估计值': round(group_result['rate'] * scale, 4),
                    '95%CI': f"{group_result['ci_low'] * scale:.2f} – "
                             f"{group_result['ci_high'] * scale:.2f}",
                })
            if rows:
                render_table(pd.DataFrame(rows), hide_index=True)

            if len(groups) == 2:
                mask1, mask2 = df[group_col] == groups[0], df[group_col] == groups[1]
                events1 = float(numerator[mask1].sum(skipna=True))
                denominator1 = float(denominator[mask1].sum(skipna=True))
                events2 = float(numerator[mask2].sum(skipna=True))
                denominator2 = float(denominator[mask2].sum(skipna=True))

                if is_proportion:
                    compare = RateAnalysis.compare_proportions(events1, denominator1,
                                                               events2, denominator2)
                else:
                    compare = RateAnalysis.compare_poisson_rates(events1, denominator1,
                                                                 events2, denominator2)

                if 'error' in compare:
                    st.warning(f"⚠️ {compare['error']}")
                else:
                    st.markdown(f"**{groups[0]} vs {groups[1]} 比较**")
                    if is_proportion:
                        st.caption(f"率差 {compare['diff'] * 100:+.2f}%"
                                   f"（{compare['diff_ci'][0] * 100:.2f} ~ "
                                   f"{compare['diff_ci'][1] * 100:.2f}）")
                    st.caption(f"{'率比' if is_proportion else '密度比'} {compare['ratio_display']}；"
                               f"{compare['test']}：p = {compare['p']:.4f}")
                    offer_report_section(
                        'tq_rate_cmp',
                        f"{'率' if is_proportion else '密度'}比较：{groups[0]} vs {groups[1]}",
                        f"{'率比' if is_proportion else '密度比'} {compare['ratio_display']}；"
                        f"{compare['test']}：p = {compare['p']:.4f}")

    # ---------- SPC 控制图 ----------
    with tab_spc:
        col1, col2, col3 = st.columns(3)
        value_col = col1.selectbox('监测指标（数值列）', numeric_fields, key='tq_spc_value')
        denominator_col = col2.selectbox('分母 / 暴露量（P 图或 U 图必填）',
                                         ['(无)'] + [c for c in numeric_fields if c != value_col],
                                         key='tq_spc_den')
        label_col = col3.selectbox('时间 / 标签列（可选）', ['(无)'] + all_columns,
                                   key='tq_spc_label')
        chart_choice = st.radio('控制图类型',
                                ['自动选择', 'I-MR（个体值）', 'P 图（率）', 'U 图（密度）'],
                                horizontal=True, key='tq_spc_type')

        values = pd.to_numeric(df[value_col], errors='coerce')
        labels = df[label_col].astype(str).tolist() if label_col != '(无)' else None
        resolved = chart_choice
        if resolved == '自动选择':
            if denominator_col == '(无)':
                resolved = 'I-MR（个体值）'
            else:
                # 事件数始终不超过分母 → 更像二项「率」，选 P 图；否则按「密度」选 U 图
                probe_value = pd.to_numeric(df[value_col], errors='coerce')
                probe_denominator = pd.to_numeric(df[denominator_col], errors='coerce')
                probe_ok = probe_value.notna() & probe_denominator.notna() & (probe_denominator > 0)
                looks_like_rate = bool(
                    probe_ok.any() and (probe_value[probe_ok] <= probe_denominator[probe_ok]).all()
                )
                resolved = 'P 图（率）' if looks_like_rate else 'U 图（密度）'

        outcome = None
        if resolved.startswith('I-MR'):
            outcome = SpcChart.i_mr_chart(values.tolist(), labels=labels)
        elif denominator_col == '(无)':
            st.warning(f'⚠️ {resolved} 需要选择分母 / 暴露量列')
        else:
            denominators = pd.to_numeric(df[denominator_col], errors='coerce')
            if resolved.startswith('P 图'):
                outcome = SpcChart.p_chart(values.tolist(), denominators.tolist(), labels=labels)
            else:
                outcome = SpcChart.u_chart(values.tolist(), denominators.tolist(), labels=labels)

        if outcome is not None:
            if outcome.get('error'):
                st.warning(f"⚠️ {outcome['error']}")
            else:
                st.image(outcome['chart'], use_container_width=True)
                st.caption(f"{resolved}｜{outcome['display']}")
                signals = outcome['signals']
                if signals:
                    st.markdown(f"**判异信号（{len(signals)} 条）**")
                    render_table(pd.DataFrame(signals), hide_index=True)
                else:
                    st.success('✅ 未发现判异信号（单点超 3σ / 连续 8 点同侧 / 连续 6 点趋势 / 2σ 规则）')
                offer_report_section(
                    'tq_spc', f"{resolved}｜{value_col}",
                    (f"发现 {len(signals)} 条判异信号："
                     + '；'.join(s['规则'] for s in signals[:5]))
                    if signals else '未发现判异信号',
                    image=outcome['chart'])

    # ---------- 趋势与环比同比 ----------
    with tab_trend:
        col1, col2, col3 = st.columns(3)
        value_col = col1.selectbox('指标列（数值）', numeric_fields, key='tq_trend_value')
        denominator_col = col2.selectbox('分母列（可选，用于率的趋势检验）',
                                         ['(无)'] + [c for c in numeric_fields if c != value_col],
                                         key='tq_trend_den')
        label_col = col3.selectbox('时间 / 标签列（可选）', ['(无)'] + all_columns,
                                   key='tq_trend_label')
        period = st.number_input('同比周期长度（月度数据填 12）', min_value=1, max_value=36,
                                 value=12, key='tq_trend_period')

        values = pd.to_numeric(df[value_col], errors='coerce')
        labels = df[label_col].astype(str).tolist() if label_col != '(无)' else None

        linear = TrendTest.linear_trend(values.tolist())
        mann_kendall = TrendTest.mann_kendall(values.tolist())
        col_a, col_b = st.columns(2)
        with col_a:
            if linear.get('error'):
                st.warning(f"⚠️ 线性趋势：{linear['error']}")
            else:
                st.markdown(f"**线性趋势：{linear['direction']}**")
                st.caption(f"斜率 {linear['slope']:+.4f}"
                           f"（95%CI {linear['slope_ci'][0]:+.4f} ~ {linear['slope_ci'][1]:+.4f}）"
                           f"；R² = {linear['r_squared']:.3f}；p = {linear['p']:.4f}")
        with col_b:
            if mann_kendall.get('error'):
                st.warning(f"⚠️ Mann-Kendall：{mann_kendall['error']}")
            else:
                st.markdown(f"**Mann-Kendall：{mann_kendall['direction']}**")
                st.caption(f"tau = {mann_kendall['tau']:+.4f}；p = {mann_kendall['p']:.4f}")

        if denominator_col != '(无)':
            denominators = pd.to_numeric(df[denominator_col], errors='coerce')
            cochran = TrendTest.cochran_armitage(values.tolist(), denominators.tolist())
            if cochran.get('error'):
                st.warning(f"⚠️ Cochran-Armitage：{cochran['error']}")
            else:
                st.markdown(f"**率的时间趋势（Cochran-Armitage）：{cochran['direction']}**")
                st.caption(f"z = {cochran['z']:.4f}；p = {cochran['p']:.4f}")

        table = TrendTest.period_over_period(values.tolist(), labels=labels,
                                             period=int(period), value_name=value_col)
        st.markdown('**环比 / 同比变化**')
        render_table(table, hide_index=True)
        if not linear.get('error'):
            offer_report_section(
                'tq_trend', f"时间趋势：{value_col}",
                f"线性趋势 {linear['direction']}（斜率 {linear['slope']:+.4f}，p = {linear['p']:.4f}）",
                table=table)
            render_inline_ai_write(
                'tq_trend', f'{value_col} 的时间趋势',
                summary=(f"线性趋势 {linear['direction']}（斜率 {linear['slope']:+.4f}，"
                         f"95%CI {linear['slope_ci'][0]:+.4f}~{linear['slope_ci'][1]:+.4f}，"
                         f"p = {linear['p']:.4f}）"),
                tables=table)

def render_longitudinal_panel() -> None:
    """重复测量（LMM / GEE）：同一对象被反复测量时的分析。

    原先放在「⏱️ 时间与质控」页，但它本质上是纵向建模（与普通回归同类），
    因此移到「🤖 预测建模」，与其余模型放在一起。
    """

    df = st.session_state.df
    fields = st.session_state.report_fields

    from modules.longitudinal import (GEE_CORR_LABELS, GEE_FAMILY_LABELS,
                                      LongitudinalAnalysis)

    st.caption('适用：同一对象被反复测量（一行 = 一个对象的一个时间点）。'
               '配对 t / Wilcoxon 只能比较两个时间点，这里可处理 ≥3 个时间点、'
               '随访次数不均衡，并且能纳入协变量做调整。')

    numeric_list = [k for k, v in fields.items() if v['dtype'] == 'numeric']
    if not numeric_list:
        st.warning('⚠️ 重复测量分析需要数值型的时间与结局变量，'
                   '可到「🧹 数据准备 → 🏷️ 变量类型调整」中指定。')
        return

    suggestions = LongitudinalAnalysis.suggest_columns(df, fields)
    subject_candidates = suggestions['subject'] or list(df.columns)

    def default_index(candidates, options) -> int:
        for name in candidates:
            if name in options:
                return options.index(name)
        return 0

    c1, c2, c3 = st.columns(3)
    with c1:
        subject_col = st.selectbox('个体标识列（同一对象重复出现）', subject_candidates,
                                   key='lg_subject',
                                   help='例如患者ID：同一个对象的多条记录写同一个值')
    with c2:
        time_col = st.selectbox('时间变量', numeric_list, key='lg_time',
                                index=default_index(suggestions['time'], numeric_list),
                                help='数值型时间点，如 0/1/2/3 月或随访次数')
    with c3:
        outcome_col = st.selectbox('结局变量（重复测量）', numeric_list, key='lg_outcome',
                                   index=default_index(suggestions['outcome'], numeric_list))

    covariates = st.multiselect(
        '协变量（可选）', [c for c in numeric_list if c not in (time_col, outcome_col)],
        key='lg_covars')

    method = st.radio('方法', ['线性混合效应模型（LMM）', 'GEE 广义估计方程'],
                      horizontal=True, key='lg_method')

    family, correlation = 'gaussian', 'exchangeable'
    if method.startswith('GEE'):
        g1, g2 = st.columns(2)
        family = g1.selectbox('结局分布族', list(GEE_FAMILY_LABELS.keys()),
                              format_func=lambda key: GEE_FAMILY_LABELS[key],
                              key='lg_family')
        correlation = g2.selectbox('相关结构', list(GEE_CORR_LABELS.keys()),
                                   format_func=lambda key: GEE_CORR_LABELS[key],
                                   key='lg_corr')
        st.caption('二分类结局选 Binomial（输出 OR）、计数结局选 Poisson（输出 RR）、'
                   '连续型选 Gaussian；相关结构不确定时先用「可交换」；'
                   'AR(1) 在部分数据上不收敛（会给出提示），此时改用可交换即可，结论通常一致。')
    else:
        st.caption('LMM 会给出 ICC（组内相关系数）：ICC 越大说明个体间差异越重要，'
                   '越有必要用重复测量模型而不是普通回归。')

    if st.button('🚀 运行重复测量分析', type='primary', use_container_width=True,
                 key='lg_run'):
        if not (subject_col and time_col and outcome_col):
            st.warning('⚠️ 请先选择个体标识列、时间变量与结局变量')
        elif subject_col == time_col:
            st.warning('⚠️ 个体标识列与时间变量不能是同一列')
        else:
            with st.spinner('正在拟合模型...'):
                if method.startswith('GEE'):
                    result = LongitudinalAnalysis.gee(
                        df, outcome_col, time_col, subject_col, covariates,
                        family=family, correlation=correlation)
                else:
                    result = LongitudinalAnalysis.linear_mixed(
                        df, outcome_col, time_col, subject_col, covariates)
            if 'error' in result:
                st.session_state.lg_result = None
                st.error(result['error'])
            else:
                st.session_state.lg_result = result
                render_inline_ai_write(
                    'long_lmm', f'重复测量分析（{outcome_col}）',
                    summary=result['display'], tables=result['table'])

    result = st.session_state.get('lg_result')
    if result and 'error' not in result:
        st.success(result['display'])
        m1, m2, m3, m4 = st.columns(4)
        m1.metric('对象数', result['n_subjects'])
        m2.metric('记录数', result['n_records'])
        m3.metric('时间点', result['n_time_points'])
        if result.get('icc') is not None:
            m4.metric('ICC', result['icc'],
                      help='组内相关系数：个体间差异占总变异的比例')
        elif result.get('effect_label'):
            m4.metric('效应量', result['effect_label'])
        render_table(result['table'], hide_index=True)
        if result.get('plot'):
            st.image(result['plot'], use_container_width=True)
        if result.get('converged') is False:
            st.warning('⚠️ 模型未报告收敛，结果需谨慎解读：可减少协变量或改用 GEE。')
        if result.get('missing_dropped'):
            st.caption(f"已剔除 {result['missing_dropped']} 行缺失记录"
                       '（结局、时间或协变量缺失）')
        with st.expander('📘 结果怎么读', expanded=False):
            st.markdown(
                '- 时间效应系数表示**每经过 1 个时间单位**结局的平均变化量；'
                '若时间以「月」计，即为每月变化。\n'
                '- LMM：给出固定效应系数、p 值与 ICC；GEE：用稳健标准误，'
                '对相关结构假设不敏感，二分类结局输出 OR。\n'
                '- 两者都不要求每个对象的测量次数相同，适合随访不均衡的数据。')
        # 收录按钮放在读取会话结果的稳定分支里，rerun 后不会消失
        offer_report_section(
            'long_lmm', f'重复测量分析：{outcome_col}（{result["model"]}）',
            result['display'], table=result['table'],
            image=result.get('plot') or None)


def render_forecast_panel() -> None:
    """时间序列预测：用历史序列预测未来走势。

    原先在「⏱️ 时间与质控」页，属于建模而非质控，移到「🤖 预测建模」。
    """

    df = st.session_state.df
    fields = st.session_state.report_fields

    from modules.timeseries import METHOD_LABELS, TimeSeriesForecast

    st.caption('用历史时间点序列预测未来走势（例如未来 3–6 个月的发生率或例数）。'
               '程序会自动把最后 20% 时间点留出做验证，并与「朴素法」基准比较：'
               '若模型还不如朴素法，说明该序列没有可利用的趋势或周期。')
    st.caption('提示：若数据是「每月一列」的宽表，请先在「🧹 数据准备 → 🧩 宽表转长表」'
               '折叠成纵向，再回到这里预测。')

    numeric_list = [k for k, v in fields.items() if v['dtype'] == 'numeric']
    if not numeric_list:
        st.warning('⚠️ 时间序列预测需要数值型指标列，'
                   '可到「🧹 数据准备 → 🏷️ 变量类型调整」中指定。')
        return

    f1, f2, f3, f4 = st.columns(4)
    with f1:
        forecast_col = st.selectbox('要预测的指标列', numeric_list, key='ts_value',
                                    help='按数据行顺序作为时间先后')
    with f2:
        label_choice = st.selectbox('时间标签列（可选，用于图轴）',
                                    ['（无）'] + [c for c in df.columns if c != forecast_col],
                                    key='ts_label')
    with f3:
        forecast_periods = st.number_input('预测期数', min_value=1, max_value=60,
                                           value=6, step=1, key='ts_periods')
    with f4:
        forecast_method = st.selectbox('方法', list(METHOD_LABELS.keys()),
                                       format_func=lambda key: METHOD_LABELS[key],
                                       key='ts_method')

    seasonal_periods = 12
    if forecast_method in ('holt_winters', 'auto'):
        seasonal_periods = st.number_input(
            '季节周期（Holt-Winters 使用，月度数据填 12）',
            min_value=2, max_value=60, value=12, step=1, key='ts_seasonal')

    if st.button('🚀 运行预测', type='primary', use_container_width=True, key='ts_run'):
        labels = ([str(value) for value in df[label_choice].tolist()]
                  if label_choice != '（无）' else None)
        with st.spinner('正在拟合与留出回测...'):
            result = TimeSeriesForecast.forecast(
                df[forecast_col], periods=int(forecast_periods),
                method=forecast_method, seasonal_periods=int(seasonal_periods),
                labels=labels)
        if 'error' in result:
            st.session_state.ts_result = None
            st.error(result['error'])
        else:
            st.session_state.ts_result = result

    result = st.session_state.get('ts_result')
    if result and 'error' not in result:
        st.success(result['display'])
        if result.get('beats_naive') is False:
            st.warning('⚠️ 所选模型的留出误差并未优于「朴素法（下期 = 本期）」，'
                       '说明该序列缺乏可利用的模式，预测值仅供参考。')
        stationarity = result.get('stationarity') or {}
        if stationarity.get('error'):
            st.caption(f"平稳性检验：{stationarity['error']}")
        else:
            st.caption(stationarity.get('display', ''))
        st.markdown('**各方法对比（留出验证）**')
        render_table(result['evaluation'], hide_index=True)
        st.markdown('**未来预测值**')
        render_table(result['table'], hide_index=True)
        st.caption(f"区间说明：{result['interval_note']}")
        if result.get('plot'):
            st.image(result['plot'], use_container_width=True)
        st.download_button('⬇️ 下载预测结果 CSV',
                           data=result['table'].to_csv(index=False).encode('utf-8-sig'),
                           file_name='时间序列预测.csv', mime='text/csv', key='ts_download')
        render_inline_ai_write(
            'tq_forecast', f'{forecast_col} 的时间序列预测',
            summary=result['display'], tables=result['table'])
        offer_report_section(
            'ts_forecast', f'时间序列预测：{forecast_col}', result['display'],
            table=result['table'], image=result.get('plot') or None)

# ========== 各功能页 ==========
def render_data_extract():
    """当前数据的查看 / 筛选 / 导出（原「📌 数据摘录」页，现挂在「🧹 数据准备」页内）"""
    df = st.session_state.df
    all_columns = list(df.columns)
    selected_columns = st.multiselect(
        "摘录字段",
        all_columns,
        default=all_columns,
        help="只保留需要查看或导出的字段，不会修改原始数据。",
    )

    if not selected_columns:
        st.warning("请至少选择一个字段")
        return

    filter_column = st.selectbox(
        "筛选字段（可选）",
        ["(不筛选)"] + selected_columns,
        key="extract_filter_column",
    )
    extracted = df.loc[:, selected_columns].copy()

    if filter_column != "(不筛选)":
        series = extracted[filter_column]
        if pd.api.types.is_numeric_dtype(series):
            valid_values = pd.to_numeric(series, errors="coerce").dropna()
            if valid_values.empty:
                st.info("该字段没有可用于范围筛选的数值")
            else:
                lower, upper = st.slider(
                    "数值范围",
                    float(valid_values.min()),
                    float(valid_values.max()),
                    (float(valid_values.min()), float(valid_values.max())),
                    key="extract_numeric_range",
                )
                numeric_series = pd.to_numeric(series, errors="coerce")
                extracted = extracted[numeric_series.between(lower, upper) | numeric_series.isna()]
        else:
            keyword = st.text_input(
                "关键词",
                key="extract_keyword",
                help="匹配包含关键词的文本；留空表示不过滤。",
            ).strip()
            if keyword:
                matched = series.astype("string").str.contains(keyword, case=False, na=False)
                extracted = extracted[matched]

    row_limit = st.number_input(
        "最多摘录行数",
        min_value=1,
        max_value=max(1, len(extracted)),
        value=min(1000, max(1, len(extracted))),
        step=100,
        help="仅限制摘录结果，不影响原始数据。",
    )
    extracted = extracted.head(int(row_limit)).reset_index(drop=True)

    c1, c2, c3 = st.columns(3)
    c1.metric("摘录行数", f"{len(extracted):,}")
    c2.metric("摘录字段", f"{len(extracted.columns):,}")
    c3.metric("原始行数", f"{len(df):,}")

    numeric_columns = extracted.select_dtypes(include="number").columns.tolist()
    disabled_columns = [column for column in extracted.columns if column not in numeric_columns]
    if "extract_table_maximized" not in st.session_state:
        st.session_state.extract_table_maximized = False
    if not st.session_state.extract_table_maximized:
        if st.button("⛶ 全屏查看表格", use_container_width=False):
            st.session_state.extract_table_maximized = True
            st.rerun()

    if st.session_state.extract_table_maximized:
        st.markdown("""
        <style>
        .st-key-extract-fullscreen {
            position: fixed;
            inset: 0;
            z-index: 999999;
            width: 100vw;
            height: 100vh;
            overflow: auto;
            padding: 1rem 2rem 2rem;
            background: white;
        }
        .st-key-extract-fullscreen [data-testid="stDataEditor"] {
            min-width: 100%;
        }
        </style>
        """, unsafe_allow_html=True)

    table_container = st.container(key="extract-fullscreen" if st.session_state.extract_table_maximized else "extract-table")
    with table_container:
        if st.session_state.extract_table_maximized:
            header_col, action_col = st.columns([5, 1])
            with header_col:
                st.subheader("📌 数据摘录 · 全屏查看")
            with action_col:
                if st.button("↙ 退出全屏", use_container_width=True):
                    st.session_state.extract_table_maximized = False
                    st.rerun()
        st.caption("可直接修改数值字段；文本、分类和日期字段为只读。修改仅作用于本次摘录，不会改变原始数据。")
        extracted = st.data_editor(
            extracted,
            hide_index=True,
            use_container_width=True,
            height=720 if st.session_state.extract_table_maximized else 360,
            num_rows="fixed",
            disabled=disabled_columns,
            key="extract_editor",
        )
        # ---------- 导出前脱敏（可选） ----------
        from modules.privacy import Desensitizer

        with st.expander('🔒 脱敏设置（导出前可选）', expanded=False):
            detected = Desensitizer.detect_sensitive_columns(extracted)
            if detected:
                preview_text = '、'.join(str(name) for name in detected[:8])
                if len(detected) > 8:
                    preview_text += f' 等 {len(detected)} 个'
                st.caption(f'识别到疑似身份信息字段：{preview_text}')
            else:
                st.caption('未按字段名或内容特征识别到身份信息字段（仍可手动指定）。')

            mask_enabled = st.checkbox(
                '导出时应用脱敏', value=bool(detected), key='extract_mask_enabled',
                help='只影响下载的 CSV 与预览；上方表格编辑不受影响。')
            show_all_columns = st.checkbox(
                '展开全部字段（默认只列出疑似敏感字段）', value=False,
                key='extract_mask_show_all')

            default_plan = Desensitizer.plan_for_all(extracted)
            plan = {name: 'keep' for name in extracted.columns}
            columns_to_show = list(extracted.columns) if show_all_columns else detected
            if columns_to_show:
                st.markdown('##### 逐字段策略')
                for column in columns_to_show:
                    current = default_plan.get(column, 'keep')
                    keys = list(Desensitizer.STRATEGIES.keys())
                    plan[column] = st.selectbox(
                        f'{column}', keys,
                        index=keys.index(current) if current in keys else 0,
                        format_func=lambda key: Desensitizer.STRATEGIES[key],
                        help=Desensitizer.STRATEGY_HELP.get(current, ''),
                        key=f'extract_strategy_{column}')
            st.caption(Desensitizer.summary(plan))

        export_frame = extracted
        if st.session_state.get('extract_mask_enabled'):
            export_frame = Desensitizer.apply(extracted, plan)
            with st.expander('👁️ 预览脱敏结果（前 5 行）', expanded=False):
                render_table(export_frame.head(5), hide_index=True)

        csv_bytes = export_frame.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button(
            "📥 下载摘录 CSV" + ("（已脱敏）" if st.session_state.get('extract_mask_enabled') else ""),
            data=csv_bytes,
            file_name=f"数据摘录_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            type="primary",
            use_container_width=True,
        )


def render_descriptive():
    """📈 描述性分析"""
    from modules.descriptive import DescriptiveAnalysis
    from modules.inferential import InferentialTests

    st.markdown("""
    <div class="section-header">
        <h2>📈 描述性分析</h2>       
    </div>
    """, unsafe_allow_html=True)

    df = st.session_state.df
    fields = st.session_state.report_fields
    numeric_fields = [k for k, v in fields.items()
                      if k in df.columns and v['dtype'] == 'numeric']
    cat_fields = [k for k, v in fields.items()
                  if k in df.columns and v['dtype'] in ('categorical', 'binary')]

    # 「相关分析」已移到「🔬 统计推断」（它输出 r 与 p，属于推断）
    tab1, tab2 = st.tabs(["数值变量", "分类变量"])

    with tab1:
        if not numeric_fields:
            st.info("💡 当前数据无数值变量")
        else:
            c1, c2 = st.columns([2, 2])
            with c1:
                field = st.selectbox("选择数值变量", numeric_fields, key='desc_num')
            with c2:
                group_by = st.selectbox("按变量分组（可选）", ['(无)'] + cat_fields, key='desc_grp')

            series = df[field]
            gb = None if group_by == '(无)' else df[group_by]

            summary = DescriptiveAnalysis.numeric_summary(series)

            if summary.get('error'):
                st.warning(
                    f"⚠️ 字段「{field}」{summary['error']}。"
                    "可在「🧹 数据准备 → 变量类型调整」中修正该字段类型，或改选其他变量。"
                )

            # 关键指标
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("样本量", summary.get('n', 0))
            c2.metric("均数 ± SD", summary['mean_std'])
            c3.metric("中位数 (IQR)", summary['median_iqr'])
            c4.metric("正态性",
                     "✓ 正态" if summary['is_normal'] else "✗ 非正态",
                     delta=f"p {summary['normality_p_display']}",
                     delta_color="normal" if summary['is_normal'] else "inverse")

            # 方法推荐提示
            if summary['is_normal']:
                st.success(f"💡 **方法推荐**：因变量描述用 **{summary['recommended_desc']}**，组间比较用 **{summary['recommended_test']}**")
            else:
                st.info(f"💡 **方法推荐**：因变量描述用 **{summary['recommended_desc']}**，组间比较用 **{summary['recommended_test']}**")

            with st.expander("📋 查看详细数值"):
                st.json(summary)

            # 可视化
            st.markdown('<div class="subsection-header">分布可视化</div>', unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            with c1:
                img_hist = DescriptiveAnalysis.hist_plot(series, f"{field} 分布")
                st.image(img_hist, use_container_width=True)
            with c2:
                img_qq = DescriptiveAnalysis.qq_plot(series, field)
                st.image(img_qq, use_container_width=True)

            img_box = DescriptiveAnalysis.box_plot(series, f"{field} 箱线图", gb)
            st.image(img_box, use_container_width=True)
            offer_report_section(
                'desc_num', f"{field} - 分布直方图", image=img_hist,
                group_sections=[(f"{field} - Q-Q图", '', None, img_qq),
                                (f"{field} - 箱线图", '', None, img_box)],
                note='换个字段就会重画这三张图，所以默认不自动收录 —— '
                     '需要的那次点这里，一次收录这三个图。')
            render_inline_ai_write(
                'desc_num', f'{field} 的分布特征',
                summary=(f"样本量 {summary.get('n')}；{summary.get('mean_std')}；"
                         f"{summary.get('median_iqr')}；"
                         f"正态性检验 p={summary.get('normality_p_display')}；"
                         f"建议 {summary.get('recommended_desc')}"))

    with tab2:
        if not cat_fields:
            st.info("💡 当前数据无分类变量")
        else:
            field = st.selectbox("选择分类变量", cat_fields, key='cat_field')

            summary = DescriptiveAnalysis.categorical_summary(df[field])

            c1, c2, c3 = st.columns(3)
            c1.metric("有效样本", summary['n'])
            c2.metric("类别数", summary['n_categories'])
            c3.metric("最高频类别", f"{summary['top']} ({summary['top_pct']}%)")

            st.markdown('<div class="subsection-header">频数分布</div>', unsafe_allow_html=True)
            tbl = pd.DataFrame(summary['table'])
            tbl.columns = ['取值', '频数', '构成比(%)']
            render_table(tbl, hide_index=True)

            c1, c2 = st.columns(2)
            with c1:
                img_bar = DescriptiveAnalysis.bar_plot(df[field], f"{field} 频数")
                st.image(img_bar, use_container_width=True)
            with c2:
                img_pie = DescriptiveAnalysis.pie_plot(df[field], f"{field} 构成比")
                st.image(img_pie, use_container_width=True)
            offer_report_section(
                'desc_cat', f"{field} - 频数分布", table=tbl,
                group_sections=[(f"{field} - 柱状图", '', None, img_bar),
                                (f"{field} - 饼图", '', None, img_pie)],
                note='换个字段就会重出这三项，所以默认不自动收录 —— '
                     '需要的那次点这里，一次收录这三项。')

    # 「相关分析」原先是这一页的第三个子页签，但它输出 r 与 p 值，属于推断而不是
    # 描述，已移到「🔬 统计推断」页（见 render_correlation_panel）。


def render_correlation_panel() -> None:
    """相关分析：相关系数热图 + 两变量相关（Pearson / Spearman）。

    原先在「📈 描述性分析」页，但相关系数与 p 值是推断结果，与 t 检验、卡方检验
    同类，故归入「🔬 统计推断」。
    """
    from modules.descriptive import DescriptiveAnalysis
    from modules.inferential import InferentialTests

    df = st.session_state.df
    fields = st.session_state.report_fields
    numeric_fields = [k for k, v in fields.items()
                      if k in df.columns and v['dtype'] == 'numeric']

    if len(numeric_fields) < 2:
        st.info("💡 相关分析至少需要 2 个数值变量")
        return

    st.markdown('<div class="subsection-header">相关系数热图</div>',
                unsafe_allow_html=True)
    cols_sel = st.multiselect("选择参与相关的数值变量", numeric_fields,
                              default=numeric_fields[:min(6, len(numeric_fields))],
                              key='corr_select')
    if len(cols_sel) >= 2:
        sub = df[cols_sel].apply(pd.to_numeric, errors='coerce')
        img = DescriptiveAnalysis.correlation_heatmap(sub, "Pearson 相关系数热图")
        st.image(img, use_container_width=True)
        offer_report_section('cor_heatmap', "相关系数热图", image=img,
                             note='换一组变量就会重画，默认不自动收录。')

    st.markdown('<div class="subsection-header">两变量相关分析</div>',
                unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1:
        x = st.selectbox("X 变量", numeric_fields, key='cor_x')
    with c2:
        y = st.selectbox("Y 变量", [n for n in numeric_fields if n != x], key='cor_y')

    method = st.radio("方法", ['auto', 'pearson', 'spearman'], horizontal=True,
                      help="auto 会根据正态性自动选择")
    result = InferentialTests.correlation(df[x], df[y], method)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("相关系数 r", f"{result['r']}")
    c2.metric("决定系数 R²", f"{result['r_squared']}")
    c3.metric("p 值", f"{result['p_value']}")
    c4.metric("显著性",
              "✓ 显著" if result['significant'] else "✗ 不显著",
              delta=result['interpretation'],
              delta_color="normal" if result['significant'] else "off")

    st.info(f"**{result['test']}**：{result['interpretation']} {result.get('ci_95', '')}")

    img = DescriptiveAnalysis.scatter_plot(df[x], df[y], f"{x} vs {y}")
    st.image(img, use_container_width=True)
    offer_report_section(
        'cor_pair', f"{x} vs {y} - 相关分析",
        f"{result['test']}, r={result['r']}, p={result['p_value']}",
        image=img,
        note='换一组变量就会重算，默认不自动收录 —— '
             '只把要写进报告的那一条加进来。')


def render_multicomp_panel() -> None:
    """多重比较校正：已有一组 p 值做校正 / 多组 Tukey HSD 两两比较。"""
    from modules.multicomp import MultiComparison, parse_pvalues

    df = st.session_state.df
    fields = st.session_state.report_fields
    numeric_fields = [k for k, v in fields.items() if v['dtype'] == 'numeric']
    cat_fields = [k for k, v in fields.items() if v['dtype'] in ('categorical', 'binary')]

    mode = st.radio('校正对象', ['已有一组 p 值（做校正）', '多组两两比较（Tukey HSD）'],
                    horizontal=True, key='mc_mode')

    if mode == '已有一组 p 值（做校正）':
        st.caption('把需要一起校正的 p 值粘贴进来（换行、逗号、分号或空格分隔均可），'
                   '例如同一研究里多个指标、多个亚组产生的多重检验。')
        text = st.text_area('p 值列表', key='mc_pvalues', height=120,
                            placeholder='0.012\n0.043\n0.210\n0.008\n<0.001')

        c1, c2 = st.columns(2)
        with c1:
            method = st.selectbox('校正方法', list(MultiComparison.METHOD_LABELS.keys()),
                                  format_func=lambda key: MultiComparison.METHOD_LABELS[key],
                                  key='mc_method')
        with c2:
            alpha = st.selectbox('显著性水平 α', [0.05, 0.01, 0.10], key='mc_alpha')
        st.caption(MultiComparison.METHOD_HELP.get(method, ''))

        values = parse_pvalues(text)
        if values:
            st.caption(f'已识别 {len(values)} 个 p 值：'
                       + '、'.join(f'{value:g}' for value in values[:12])
                       + (' …' if len(values) > 12 else ''))
        if st.button('🔍 执行校正', type='primary', use_container_width=True, key='mc_run'):
            result = MultiComparison.adjust_pvalues(values, method=method, alpha=float(alpha))
            if 'error' in result:
                st.session_state.mc_result = None
                st.error(result['error'])
            else:
                st.session_state.mc_result = result
                render_inline_ai_write(
                    'inf_mc', '多重比较校正', summary=result['display'],
                    tables=result['table'])

        result = st.session_state.get('mc_result')
        if result and 'error' not in result:
            st.success(result['display'])
            render_table(result['table'], hide_index=True)
            if result['n_changed']:
                st.warning(f"⚠️ 有 {result['n_changed']} 个检验的结论在校正后发生变化，"
                           '论文中应报告校正后的 p 值并说明所用方法。')
            # 收录按钮放在读取会话结果的稳定分支里：run 分支只跑一次，
            # 放在那里的话 rerun 之后按钮就没了，而结果还在屏幕上
            offer_report_section('inf_mc', '多重比较校正', result['display'],
                                 table=result['table'])
        return

    st.caption('Tukey HSD 用于「多组均值两两比较」，通常跟在单因素 ANOVA 之后；'
               '它要求各组方差大致齐性，并已对多重比较做了校正。')
    candidates = [c for c in cat_fields if 2 <= df[c].nunique() <= 10]
    if not numeric_fields or not candidates:
        st.warning('⚠️ Tukey HSD 需要 1 个数值变量与 1 个分组变量（2–10 类）。'
                   '若缺少，可到「🧹 数据准备 → 🏷️ 变量类型调整」中修正字段类型。')
        return
    c1, c2, c3 = st.columns(3)
    with c1:
        var = st.selectbox('数值变量', numeric_fields, key='tukey_var')
    with c2:
        grp = st.selectbox('分组变量（2–10 类）',
                           [c for c in cat_fields if 2 <= df[c].nunique() <= 10], key='tukey_grp')
    with c3:
        alpha = st.selectbox('显著性水平 α', [0.05, 0.01, 0.10], key='tukey_alpha')

    if st.button('🚀 运行 Tukey HSD', type='primary', use_container_width=True, key='tukey_run'):
        if not (var and grp):
            st.warning('⚠️ 请先选择数值变量与分组变量')
        else:
            result = MultiComparison.pairwise_tukey(df[var], df[grp], alpha=float(alpha))
            if 'error' in result:
                st.session_state.tukey_result = None
                st.error(result['error'])
            else:
                st.session_state.tukey_result = result

    result = st.session_state.get('tukey_result')
    if result and 'error' not in result:
        st.success(result['display'])
        if result.get('plot'):
            st.image(result['plot'], use_container_width=True)
        render_table(result['table'], hide_index=True)
        st.caption('说明：均值差为「组1 − 组2」，CI 不含 0 即该对组间差异有统计学意义。')
        offer_report_section('tukey', f'Tukey HSD 两两比较（{var} × {grp}）',
                             result['display'], table=result['table'],
                             image=result.get('plot') or None)


def render_table1_panel() -> None:
    """基线特征表（Table 1）自动化。"""
    from modules.table1 import BaselineTable

    df = st.session_state.df
    fields = st.session_state.report_fields
    all_fields = list(fields.keys())
    cat_fields = [k for k, v in fields.items() if v['dtype'] in ('categorical', 'binary')]

    st.caption('按分组自动生成论文基线表：数值变量按正态性选择 mean ± SD 或 median [IQR]，'
               '分类变量给 n (%)，并给出组间检验与标准化均值差（SMD）。')

    c1, c2 = st.columns([2, 1])
    with c1:
        group_choice = st.selectbox('分组变量', ['（不分组，只输出总体）'] + cat_fields, key='t1_group')
    with c2:
        include_smd = st.checkbox('输出 SMD', value=True, key='t1_smd',
                                  help='标准化均值差；|SMD| < 0.1 通常认为组间均衡（PSM 前后常用）')

    default_vars = [name for name in all_fields if name != group_choice][:12]
    variables = st.multiselect('纳入变量', all_fields, default=default_vars, key='t1_vars')

    if st.button('📋 生成 Table 1', type='primary', use_container_width=True, key='t1_run'):
        if not variables:
            st.warning('⚠️ 请至少纳入一个变量')
        else:
            group_col = None if str(group_choice).startswith('（') else group_choice
            result = BaselineTable.build(df, variables, group_col=group_col,
                                         include_smd=bool(include_smd))
            if 'error' in result:
                st.session_state.t1_result = None
                st.error(result['error'])
            else:
                st.session_state.t1_result = result
                render_inline_ai_write(
                    'table1', '基线特征表（Table 1）', summary=result['display'],
                    tables=result['table'])

    result = st.session_state.get('t1_result')
    if result and 'error' not in result:
        st.success(result['display'])
        render_table(result['table'], hide_index=True)
        offer_report_section('table1', '基线特征表（Table 1）', result['display'],
                             table=result['table'], level=1,
                             note='Table 1 通常会放进论文，点这里收录进报告。')
        csv_bytes = result['table'].to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig')
        st.download_button('⬇️ 下载 CSV（可直接粘进 Word 做三线表）', data=csv_bytes,
                           file_name='table1.csv', mime='text/csv', key='t1_download')
        with st.expander('🔬 每个变量用了什么检验', expanded=False):
            render_table(result['test_summary'], hide_index=True)
        if result.get('notes'):
            with st.expander('📝 判定说明', expanded=False):
                for note in result['notes']:
                    st.caption('· ' + note)
        st.caption('提示：正态性按各组分别用 Shapiro-Wilk 判断（n≥8 时），'
                   '缺失按变量独立处理（pairwise）。')


def render_inferential():
    """🔬 统计推断"""
    from modules.descriptive import DescriptiveAnalysis
    from modules.inferential import InferentialTests

    st.markdown("""
    <div class="section-header">
        <h2>🔬 统计推断</h2>       
    </div>
    """, unsafe_allow_html=True)

    df = st.session_state.df
    fields = st.session_state.report_fields
    numeric_fields = [k for k, v in fields.items() if v['dtype'] == 'numeric']
    cat_fields = [k for k, v in fields.items() if v['dtype'] in ('categorical', 'binary')]

    test_type = st.radio(
        "检验类型",
        ["数值变量 × 分组", "分类变量 × 分组", "配对样本", "相关分析",
         "一致性分析（Kappa / ICC / Bland-Altman）",
         "多重比较校正（FDR / Tukey HSD）"],
        horizontal=True, key='test_type'
    )

    if test_type == "相关分析":
        render_correlation_panel()
        return

    if test_type == "多重比较校正（FDR / Tukey HSD）":
        render_multicomp_panel()
        return

    if test_type == "数值变量 × 分组":
        c1, c2 = st.columns(2)
        with c1:
            var = st.selectbox("数值变量（结局）", numeric_fields, key='num_var')
        with c2:
            grp = st.selectbox("分组变量（2-10类）", [c for c in cat_fields if df[c].nunique() <= 10],
                             key='grp_var')

        if grp and var:
            series = pd.to_numeric(df[var], errors='coerce')
            # 先按原始缺失情况筛掉，再做 astype(str)：
            # 否则 NaN 会变成 "nan" 字符串，凭空多出一个分组参与检验
            valid = series.notna() & df[grp].notna()
            excluded_rows = int((~valid).sum())
            series = series[valid]
            groups = df.loc[valid, grp].astype(str)
            n_groups = groups.nunique()
            if excluded_rows:
                st.caption(f"已剔除 {excluded_rows} 行缺失记录（结局变量或分组缺失）")

            if n_groups < 2:
                st.warning("⚠️ 分组数 < 2，无法比较")
            elif n_groups == 2:
                result = InferentialTests.two_groups_numeric(series, groups)
            else:
                result = InferentialTests.multi_groups_numeric(series, groups)

            if 'error' in result:
                st.error(result['error'])
                return

            st.markdown(f'<div class="subsection-header">检验结果 - {result["test"]}</div>', unsafe_allow_html=True)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("检验方法", result['test'], help=method_help(result['test']))
            c2.metric("统计量", result['statistic'])
            c3.metric("p 值", result['p_value'])
            c4.metric("显著性",
                     "✓ 显著" if result['significant'] else "✗ 不显著",
                     delta="p < 0.05" if result['significant'] else "p ≥ 0.05",
                     delta_color="normal" if result['significant'] else "off")

            st.success(f"💡 **效应量**：{result['effect_size']} {result.get('ci_95', '')}")

            if 'groups' in result:
                st.markdown('<div class="subsection-header">分组统计</div>', unsafe_allow_html=True)
                grp_df = pd.DataFrame(result['groups']).T
                render_table(grp_df)

                if 'post_hoc' in result:
                    st.markdown('<div class="subsection-header">事后多重比较（Bonferroni 校正）</div>', unsafe_allow_html=True)
                    ph_df = pd.DataFrame(result['post_hoc'])
                    render_table(ph_df, hide_index=True)

            elif 'group1' in result:
                st.markdown('<div class="subsection-header">分组统计</div>', unsafe_allow_html=True)
                grp_df = pd.DataFrame({
                    '组1': result['group1'],
                    '组2': result['group2']
                })
                render_table(grp_df)

            img = DescriptiveAnalysis.box_plot(df[var], f"{var} 按 {grp} 分组", df[grp])
            st.image(img, use_container_width=True)

            offer_report_section(
                'inf_num', f"{var} × {grp} - {result['test']}",
                f"统计量={result['statistic']}, p={result['p_value']}, {result['effect_size']}",
                image=img)
            render_inline_ai_write(
                'inf_num', f'{var} × {grp} 组间比较',
                summary=(f"{result['test']}：统计量={result['statistic']}，"
                         f"p={result['p_value']}，{result['effect_size']}"))

    elif test_type == "分类变量 × 分组":
        c1, c2 = st.columns(2)
        with c1:
            var = st.selectbox("分类变量（结局）", cat_fields, key='cat_outcome')
        with c2:
            grp = st.selectbox("分组变量", [c for c in cat_fields if c != var and df[c].nunique() <= 10],
                             key='cat_grp')

        if var and grp:
            result = InferentialTests.categorical_vs_categorical(df[var], df[grp])
            if 'error' in result:
                st.error(result['error'])
                return

            st.markdown(f'<div class="subsection-header">检验结果 - {result["test"]}</div>', unsafe_allow_html=True)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("检验方法", result['test'], help=method_help(result['test']))
            c2.metric("统计量", result['statistic'])
            c3.metric("p 值", result['p_value'])
            c4.metric("显著性",
                     "✓ 显著" if result['significant'] else "✗ 不显著",
                     delta_color="normal" if result['significant'] else "off")

            st.success(f"💡 **效应量**：{result['effect_size']}")

            st.markdown('<div class="subsection-header">列联表</div>', unsafe_allow_html=True)
            render_table(result['contingency'])

            if result.get('ci_95'):
                st.success(f"**临床意义**：{result['ci_95']}")

            with st.expander("📊 单元格详情（期望频数 + 标准化残差）"):
                render_table(pd.DataFrame(result['cell_details']), hide_index=True)

            offer_report_section(
                'inf_cat', f"{var} × {grp} - {result['test']}",
                f"统计量={result['statistic']}, p={result['p_value']}, {result['effect_size']}",
                table=pd.DataFrame(result['contingency']))
            render_inline_ai_write(
                'inf_cat', f'{var} × {grp} 组间比较',
                summary=(f"{result['test']}：统计量={result['statistic']}，"
                         f"p={result['p_value']}，{result['effect_size']}"))

    elif test_type == "配对样本":
        st.markdown('<div class="subsection-header">配对样本检验</div>', unsafe_allow_html=True)
        st.caption("适用于：治疗前后、左右配对、双胞胎等配对设计")

        if len(numeric_fields) < 2:
            st.info("💡 至少需要2个当前数据中的数值变量")
            return

        c1, c2 = st.columns(2)
        with c1:
            before = st.selectbox("前 / 左", numeric_fields, key='before')
        with c2:
            after = st.selectbox("后 / 右", [n for n in numeric_fields if n != before], key='after')

        if before not in df.columns or after not in df.columns:
            st.warning("当前选择的变量已不在数据表中，请重新选择或重新上传数据")
            return

        result = InferentialTests.paired_ttest(df[before], df[after])
        if 'error' in result:
            st.error(result['error'])
        else:
            statistic = result.get('t', result.get('W', 'N/A'))
            st.metric("检验方法", result['test'], help=method_help(result['test']))
            st.success(f"**{result['test']}**：统计量={statistic}, p={result['p_value']}, 显著={result['significant']}")
            with st.expander("📋 检验明细"):
                detail_df = pd.DataFrame([{'指标': k, '值': str(v)} for k, v in result.items()])
                render_table(detail_df, hide_index=True)
            offer_report_section(
                'inf_paired', f"{before} vs {after} - {result['test']}",
                f"统计量={statistic}, p={result['p_value']}")
            render_inline_ai_write(
                'inf_paired', f'{before} vs {after} 配对比较',
                summary=f"{result['test']}：统计量={statistic}，p={result['p_value']}")

    else:  # 一致性分析
        render_agreement_analysis(df, fields, numeric_fields, cat_fields)


def render_agreement_analysis(df, fields, numeric_fields, cat_fields) -> None:
    """诊断一致性：Kappa / ICC / Bland-Altman"""
    from modules.agreement import AgreementAnalysis

    st.markdown('<div class="subsection-header">诊断一致性分析</div>', unsafe_allow_html=True)
    st.caption('适用于同一指标的两次测量、两名评估者或两种方法（需要两列）。')

    analysis = st.radio('分析类型',
                        ['分类一致性（Kappa）', '数值一致性（ICC）', 'Bland-Altman 一致性'],
                        horizontal=True, key='agree_kind')

    if analysis.startswith('分类'):
        if len(cat_fields) < 2:
            st.info('💡 分类一致性至少需要两个分类变量（如两位评估者的判定）')
            return
        col1, col2, col3 = st.columns(3)
        first = col1.selectbox('评估者 / 方法 1', cat_fields, key='agree_cat_a')
        second = col2.selectbox('评估者 / 方法 2',
                                [c for c in cat_fields if c != first], key='agree_cat_b')
        weight_label = col3.selectbox('加权方式', ['未加权', '线性加权', '平方加权'],
                                      key='agree_weight',
                                      help='有序分类（如 轻 / 中 / 重度）建议用加权 Kappa')
        weight_map = {'未加权': None, '线性加权': 'linear', '平方加权': 'quadratic'}
        result = AgreementAnalysis.cohens_kappa(df[first], df[second],
                                                weights=weight_map[weight_label])
        if result.get('error'):
            st.warning(f"⚠️ {result['error']}")
            return
        col1, col2, col3, col4 = st.columns(4)
        col1.metric('Kappa', f"{result['kappa']:.3f}")
        col2.metric('95%CI', f"{result['ci'][0]:.3f} ~ {result['ci'][1]:.3f}")
        col3.metric('一致性强度', result['interpretation'])
        col4.metric('有效配对', result['n'])
        st.caption(f"实际一致率 {result['observed_agreement'] * 100:.1f}%，"
                   f"期望一致率 {result['expected_agreement'] * 100:.1f}%（{result['test']}）")
        contingency = pd.crosstab(df[first], df[second])
        st.markdown('**交叉表（行 = 评估者1，列 = 评估者2）**')
        render_table(contingency.reset_index(), hide_index=True)
        offer_report_section(
            'agree_kappa', f"一致性检验：{first} vs {second}",
            f"Kappa = {result['kappa']:.3f}"
            f"（95%CI {result['ci'][0]:.3f}–{result['ci'][1]:.3f}），"
            f"{result['interpretation']}，n = {result['n']}",
            table=contingency.reset_index())

    elif analysis.startswith('数值'):
        if len(numeric_fields) < 2:
            st.info('💡 数值一致性至少需要两个数值变量')
            return
        col1, col2 = st.columns(2)
        first = col1.selectbox('评估者 / 方法 1', numeric_fields, key='agree_num_a')
        second = col2.selectbox('评估者 / 方法 2',
                                [c for c in numeric_fields if c != first], key='agree_num_b')
        result = AgreementAnalysis.icc(df[first], df[second])
        if result.get('error'):
            st.warning(f"⚠️ {result['error']}")
            return
        col1, col2, col3, col4 = st.columns(4)
        col1.metric('ICC(2,1) 绝对一致性', f"{result['icc_21']:.3f}")
        col2.metric('95%CI', f"{result['ci'][0]:.3f} ~ {result['ci'][1]:.3f}")
        col3.metric('ICC(3,1) 一致性', f"{result['icc_31']:.3f}")
        col4.metric('一致性强度', result['interpretation'])
        st.caption(f"n = {result['n']}；F = {result['f']:.2f}，p = {result['p']:.4g}"
                   f"；Bootstrap 重抽样 {result.get('n_bootstrap', 0)} 次")
        if result.get('note'):
            st.info(result['note'])
        offer_report_section(
            'agree_icc', f"ICC 一致性：{first} vs {second}",
            f"ICC(2,1) = {result['icc_21']:.3f}"
            f"（95%CI {result['ci'][0]:.3f}–{result['ci'][1]:.3f}），{result['interpretation']}")

    else:
        if len(numeric_fields) < 2:
            st.info('💡 Bland-Altman 至少需要两个数值变量')
            return
        col1, col2 = st.columns(2)
        first = col1.selectbox('测量 1', numeric_fields, key='agree_ba_a')
        second = col2.selectbox('测量 2',
                                [c for c in numeric_fields if c != first], key='agree_ba_b')
        image, result = AgreementAnalysis.bland_altman(
            df[first], df[second],
            title=f'{first} 与 {second} 的 Bland-Altman 图',
            label1=first, label2=second)
        if result.get('error'):
            st.warning(f"⚠️ {result['error']}")
            return
        if image:
            st.image(image, use_container_width=True)
        col1, col2, col3 = st.columns(3)
        col1.metric('平均偏倚', f"{result['bias']:.3f}")
        col2.metric('一致性界限', f"{result['lower_loa']:.3f} ~ {result['upper_loa']:.3f}")
        col3.metric('超出界限', f"{result['outside']}（{result['outside_pct']}%）")
        st.caption(f"n = {result['n']}；差值 SD = {result['sd']:.3f}；"
                   f"比例偏倚斜率 = {result['proportional_slope']:.4f}"
                   f"（p = {result['proportional_p']:.4f}）")
        if result['proportional_p'] < 0.05:
            st.warning('⚠️ 差值随测量均值变化（比例偏倚显著），偏倚不恒定，解释一致性界限需谨慎。')
        offer_report_section(
            'agree_ba', f"Bland-Altman：{first} vs {second}",
            f"偏倚 {result['bias']:.3f}"
            f"（95%LoA {result['lower_loa']:.3f}~{result['upper_loa']:.3f}），"
            f"超界 {result['outside']} 例（{result['outside_pct']}%）",
            image=image)


def render_external_validation_panel(df, fields) -> None:
    """外部验证：当前数据建模 → 独立数据集评估判别力与校准。"""
    from modules.external_validation import ExternalValidation

    st.caption('先在当前数据上拟合 Logistic 模型，再把同一套系数与编码规则应用到**另一个独立数据集**，'
               '比较判别力（AUC 变化）与校准曲线。为保证编码规则可移植，'
               '预测变量只支持「数值型」与「二分类」。')

    numeric_fields = [k for k, v in fields.items() if v['dtype'] == 'numeric']
    binary_fields = [k for k, v in fields.items() if v['dtype'] == 'binary']
    candidates = numeric_fields + binary_fields
    if len(candidates) < 2 or not binary_fields:
        st.warning('⚠️ 外部验证需要至少 1 个二分类结局变量与 1 个预测变量'
                   '（数值型或二分类），可到「🧹 数据准备 → 🏷️ 变量类型调整」中指定。')
        return

    c1, c2 = st.columns([1, 2])
    with c1:
        outcome = st.selectbox('结局变量（二分类）', binary_fields, key='ev_outcome')
    with c2:
        predictors = st.multiselect('预测变量（数值 / 二分类）',
                                    [name for name in candidates if name != outcome],
                                    key='ev_predictors')

    st.markdown('**验证集（独立数据集）**')
    uploaded = st.file_uploader('上传验证集 Excel（与训练集结构相同）', type=['xlsx', 'xls'],
                                key='ev_upload')
    new_frame = None
    new_outcome = None
    if uploaded is not None:
        try:
            sheet_names = cached_list_sheets(uploaded.getvalue())
        except Exception as error:
            st.error(f'❌ 验证集读取失败：{error}')
            sheet_names = []
        if sheet_names:
            v1, v2, v3 = st.columns(3)
            with v1:
                sheet = st.selectbox('Sheet', sheet_names, key='ev_sheet')
            with v2:
                header_rows = st.number_input('表头行数', min_value=0, max_value=5, value=1,
                                              step=1, key='ev_header')
            with v3:
                skip_rows = st.number_input('跳过开头行数', min_value=0, max_value=50, value=0,
                                            step=1, key='ev_skip')
            try:
                new_frame = cached_read_sheet(uploaded.getvalue(), sheet, int(header_rows),
                                              int(skip_rows), False)
            except Exception as error:
                st.error(f'❌ 验证集解析失败：{error}')
            if new_frame is not None:
                st.caption(f"验证集：{len(new_frame)} 行 × {len(new_frame.columns)} 列")
                if st.checkbox('预览验证集前 5 行', key='ev_preview'):
                    render_table(new_frame.head(5), hide_index=True)
                options = list(new_frame.columns)
                default_index = options.index(outcome) if outcome in options else 0
                new_outcome = st.selectbox('验证集里的结局列', options, index=default_index,
                                           key='ev_new_outcome')

    if st.button('🚀 运行外部验证', type='primary', use_container_width=True, key='ev_run'):
        if not (outcome and predictors):
            st.warning('⚠️ 请先选择结局变量与至少一个预测变量')
        elif new_frame is None:
            st.warning('⚠️ 请先上传并解析验证集')
        else:
            with st.spinner('正在训练并做外部验证...'):
                trained = ExternalValidation.train(df, outcome, predictors)
                if 'error' in trained:
                    st.session_state.ev_result = None
                    st.error(trained['error'])
                else:
                    result = ExternalValidation.evaluate(trained, new_frame,
                                                         new_outcome=new_outcome)
                    if 'error' in result:
                        st.session_state.ev_result = None
                        st.error(result['error'])
                    else:
                        st.session_state.ev_result = result
                        st.session_state.ev_auc_drop = result['auc_drop']
                        render_inline_ai_write(
                            'model_external', f'外部验证（{outcome}）',
                            summary=result['display'], tables=result['metrics'])

    result = st.session_state.get('ev_result')
    if result and 'error' not in result:
        st.success(result['display'])
        drop = float(result.get('auc_drop') or 0)
        if abs(drop) <= 0.05:
            st.info('💡 AUC 外部下降幅度 ≤ 0.05，判别力在外部数据上保持良好。')
        elif drop < -0.05:
            st.warning('⚠️ 外部 AUC 明显低于内部，提示存在过拟合或人群差异，'
                       '建议检查变量定义与人群纳入标准是否一致。')
        else:
            st.info('💡 外部 AUC 高于内部（常见于小样本验证集），不必据此认为模型更优。')
        render_table(result['metrics'], hide_index=True)
        st.markdown('**校准（预测概率 vs 实际发生率）**')
        render_table(result['calibration'], hide_index=True)
        if result.get('plot'):
            st.image(result['plot'], use_container_width=True)
        st.caption('判读：校准点越贴近对角线越准；AUC 反映区分能力，Brier 分数同时反映区分与校准，'
                   '越小越好。')
        offer_report_section(
            'model_external', f'外部验证（{outcome}）', result['display'],
            table=result['metrics'], image=result.get('plot') or None)


def render_linear_panel(df, fields) -> None:
    """
    多重线性回归（连续结局）。

    补的是「常规数据分析」里最常见的一类问题：以连续变量为结局的回归
    （住院天数、费用、评分）。把分组变量作为其中一个自变量，就是 ANCOVA。
    """
    from modules.linear import LinearModel

    st.markdown('<div class="subsection-header">模型配置</div>', unsafe_allow_html=True)
    st.caption('用于回答「哪些因素与某个连续指标独立相关」。把分组变量也选进自变量，'
               '就是 **ANCOVA** —— 校正基线后再比较组间差异。')

    numeric_fields = [k for k, v in fields.items() if v['dtype'] == 'numeric']
    if not numeric_fields:
        st.warning('⚠️ 线性回归的结局必须是连续变量，当前没有数值型字段。'
                   '可到「🧹 数据准备 → 🏷️ 变量类型调整」指定后再来。')
        return

    c1, c2 = st.columns(2)
    with c1:
        outcome = st.selectbox('结局变量（连续）', numeric_fields, key='lin_out')
    with c2:
        predictors = st.multiselect(
            '自变量（可多选）', [k for k in df.columns if k != outcome], key='lin_pred',
            help='数值变量直接进入模型；文本或取值很少的列会展开为哑变量，第一个水平作参照')
    if not predictors:
        st.info('💡 请至少选择一个自变量。选「分组变量 + 基线值」即构成 ANCOVA。')
        return

    categorical = [name for name in predictors
                   if fields.get(name, {}).get('dtype') in ('categorical', 'binary')]
    st.caption('按分类处理（展开为哑变量）：'
               + ('、'.join(categorical) if categorical else '无')
               + '｜缺失值按完整案例法剔除，剔除数量会在结果中写明。')

    if st.button('📈 运行线性回归', type='primary', use_container_width=True, key='lin_run'):
        st.session_state.lin_result = LinearModel.fit(
            df, outcome, predictors, categorical)

    result = st.session_state.get('lin_result')
    if not result:
        return
    if 'error' in result:
        st.error(f"❌ {result['error']}")
        return

    st.markdown('<div class="subsection-header">模型整体</div>', unsafe_allow_html=True)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric('R²', result['r2'])
    m2.metric('调整 R²', result['adj_r2'])
    m3.metric('F 检验 p', f"{result['f_p']:.3g}")
    m4.metric('有效样本', str(result['n']))
    st.caption(f"回归方程：{result['formula']}｜残差标准误 {result['sigma']}"
               + (f"｜剔除缺失 {result['n_dropped']} 例（原始 {result['n_input']} 例）"
                  if result['n_dropped'] else '｜无缺失'))
    if result['f_p'] < 0.05:
        st.success(f"✅ 模型整体显著（F={result['f_stat']}, p={result['f_p']:.4g}）："
                   f"这些自变量整体上解释了结局变异的 {result['r2'] * 100:.1f}%")
    else:
        st.warning(f"⚠️ 模型整体不显著（F={result['f_stat']}, p={result['f_p']:.4g}）："
                   '自变量整体上未能解释结局变异，此时单个系数不宜解读')

    st.markdown('<div class="subsection-header">回归系数</div>', unsafe_allow_html=True)
    coef_df = LinearModel.coef_frame(result)
    render_table(coef_df, hide_index=True)
    st.caption('**β**：自变量每增加 1 个单位，结局的平均变化量。'
               '**标准化 β**：用于比较不同量纲自变量的相对重要性'
               '（分类变量展开的哑变量，其标准化 β 解释意义有限）。')
    if result['notes']:
        st.caption('参照水平：' + '；'.join(result['notes']))
    st.download_button('⬇️ 下载系数表 CSV', data=LinearModel.coef_csv(result),
                       file_name='线性回归系数表.csv', mime='text/csv', key='lin_csv',
                       use_container_width=True)

    st.markdown('<div class="subsection-header">模型诊断</div>', unsafe_allow_html=True)
    d1, d2 = st.columns(2)
    with d1:
        with st.expander('📊 共线性诊断（VIF）', expanded=True):
            render_table(LinearModel.vif_frame(result), hide_index=True)
            st.caption('VIF ≥ 5 需留意；≥ 10 说明这几个变量的系数与 p 值不稳定，'
                       '建议只保留其中一个。')
    with d2:
        with st.expander('📐 残差假设检验', expanded=True):
            if result.get('dw') is not None:
                st.markdown(f"- Durbin-Watson = {result['dw']}"
                            '（理想约 2；< 1.5 或 > 2.5 提示残差自相关）')
            if result.get('bp'):
                verdict = '存在异方差' if result['bp']['p'] < 0.05 else '未见异方差'
                st.markdown(f"- Breusch-Pagan 检验 p = {result['bp']['p']:.4g}（{verdict}）")
            st.markdown(f"- 结局偏度 = {result['skew']}（|偏度| > 1 提示明显偏态，"
                        '可考虑对结局取对数）')

    residual_png = LinearModel.residual_figure(result)
    if residual_png:
        st.image(residual_png, use_container_width=True)
        st.caption('Q-Q 图上的点贴近直线说明残差接近正态；残差-预测图不应出现喇叭形'
                   '（异方差）或明显弯曲（非线性）。')

    if result['outlier_count']:
        with st.expander(f"⚠️ 可疑观测点（{result['outlier_count']} 个）", expanded=True):
            st.caption('这些是离群点或强影响点，对系数的影响可能很大。请先核对是否为录入错误；'
                       '确认无误时可剔除后重跑，作为敏感性分析。')
            render_table(LinearModel.outlier_frame(result), hide_index=True)

    for alert in result['alerts']:
        st.warning(alert)

    offer_report_section(
        'model_lin', f"多重线性回归 - {outcome}",
        (f"回归方程 {result['formula']}；n={result['n']}，R²={result['r2']}，"
         f"调整 R²={result['adj_r2']}，F={result['f_stat']}（p={result['f_p']:.3g}）"),
        table=coef_df, image=residual_png)
    render_inline_ai_write(
        'model_lin', f'多重线性回归（结局：{outcome}）',
        summary=(f"n={result['n']}，R²={result['r2']}，调整 R²={result['adj_r2']}，"
                 f"F={result['f_stat']}（p={result['f_p']:.3g}）；"
                 f"自变量：{'、'.join(predictors)}"),
        tables=coef_df,
        material=('研究者应关注的诊断提示：' + '；'.join(result['alerts'])
                  if result['alerts'] else '模型诊断未见明显问题。'))


def render_modeling():
    """🤖 预测建模"""
    from modules.modeling import PredictiveModeling

    st.markdown("""
    <div class="section-header">
        <h2>🤖 预测建模</h2>       
    </div>
    """, unsafe_allow_html=True)

    df = st.session_state.df
    fields = st.session_state.report_fields
    numeric_fields = [k for k, v in fields.items() if v['dtype'] == 'numeric']
    binary_fields = [k for k, v in fields.items() if v['dtype'] == 'binary']
    cat_fields = [k for k, v in fields.items() if v['dtype'] in ('categorical', 'binary')]

    model_type = st.radio("模型类型",
        ["多重线性回归（连续结局）", "Logistic 回归", "Cox 生存回归",
         "LASSO 筛选 + Bootstrap 内部验证",
         "剂量-反应（限制性立方样条）",
         "外部验证（第二个数据集）",
         "重复测量（LMM / GEE）", "时间序列预测"],
        horizontal=True, key='model_type')

    if model_type == "多重线性回归（连续结局）":
        render_linear_panel(df, fields)
        return

    if model_type == "外部验证（第二个数据集）":
        render_external_validation_panel(df, fields)
        return

    # 下面两个原先在「⏱️ 时间与质控」页：它们是建模方法（纵向建模 / 时间序列），
    # 与质控（率、SPC、趋势）不是一类，归到这里与其余模型并列
    if model_type == "重复测量（LMM / GEE）":
        render_longitudinal_panel()
        return

    if model_type == "时间序列预测":
        render_forecast_panel()
        return

    if model_type == "Logistic 回归":
        st.markdown('<div class="subsection-header">模型配置</div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            outcome = st.selectbox("结局变量（二分类）", binary_fields, key='lr_out')
        with c2:
            predictors = st.multiselect("预测变量", [p for p in numeric_fields + cat_fields if p != outcome])

        test_size = st.slider("测试集比例", 0.1, 0.5, 0.3, 0.05,
                            help="将数据分为训练集和测试集，用于评估模型泛化能力")

        run_logistic = st.button("🚀 运行 Logistic 回归", type="primary")
        log_sig = (outcome, tuple(predictors or []), float(test_size))
        if run_logistic and not (outcome and predictors):
            st.warning('⚠️ 请先选择结局变量与至少一个预测变量')
        if run_logistic and outcome and predictors:
            store_panel_result(
                'logistic',
                PredictiveModeling.logistic_regression(
                    df, outcome, predictors, test_size=test_size),
                signature=log_sig)

        # 结果从会话里取：运行一次之后，再点页面上的任何控件都不会让结果消失
        result = panel_result('logistic', signature=log_sig)
        if result and result.get('error'):
            st.error(result['error'])
        if result and not result.get('error'):
            # 模型评价
            st.markdown('<div class="subsection-header">模型评价</div>', unsafe_allow_html=True)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("样本量", result['n'])
            c2.metric("Pseudo R²", result['pseudo_r2'])
            if result.get('n_dropped'):
                st.caption(f"⚠️ 有 {result['n_dropped']} 行因缺失值被排除，实际建模 n = {result['n']}"
                           "（如需保留可用「数据总览 → 缺失模式与多重插补」先插补）")
            if result.get('separation_vars'):
                st.warning(
                    "⚠️ **完全分离**：变量 " + "、".join(result['separation_vars'])
                    + " 几乎能完美区分结局，系数与标准误已失真，"
                    "OR 与 p 值不可直接解读。建议减少该变量、合并类别，或改用「LASSO 筛选」。"
                )
            c3.metric("AUC (训练)",
                     result.get('train_metrics', {}).get('auc', '-') if result.get('train_metrics') else '-')
            c4.metric("AUC (测试)",
                     result.get('test_metrics', {}).get('auc', '-') if result.get('test_metrics') else '-')

            # 系数表
            st.markdown('<div class="subsection-header">回归系数表</div>', unsafe_allow_html=True)
            coef_df = pd.DataFrame(result['coef_table'])
            render_table(coef_df, hide_index=True)

            # VIF
            if result.get('vif'):
                with st.expander("📊 共线性诊断（VIF）"):
                    vif_df = pd.DataFrame(result['vif'])
                    render_table(vif_df, hide_index=True)
                    high_vif = [v for v in result['vif'] if v['VIF'] > 5]
                    if high_vif:
                        st.warning(f"⚠️ 存在共线性问题：{[v['variable'] for v in high_vif]}（VIF > 5）")

            # H-L
            if result.get('hosmer_lemeshow'):
                hl = result['hosmer_lemeshow']
                if hl['good_fit']:
                    st.success(f"✅ **Hosmer-Lemeshow 拟合优度**：χ²={hl['chi2']}, p={hl['p_value']} → 拟合良好")
                else:
                    st.warning(f"⚠️ **Hosmer-Lemeshow 拟合优度**：χ²={hl['chi2']}, p={hl['p_value']} → 拟合不佳")

            # ROC
            st.markdown('<div class="subsection-header">ROC 曲线</div>', unsafe_allow_html=True)
            st.image(result['roc_plot'], use_container_width=True)

            # 诊断截断值分析
            cutoff = result.get('cutoff') or {}
            if cutoff and 'error' not in cutoff:
                st.markdown('<div class="subsection-header">最佳截断值（诊断效能）</div>',
                            unsafe_allow_html=True)
                st.success(cutoff['display'])
                summary_table = cutoff['summary_table'].drop(
                    columns=['TP', 'FP', 'FN', 'TN'], errors='ignore')
                render_table(summary_table, hide_index=True)
                st.caption('三种常用判据并列展示：临床更看重灵敏度时选平衡点，'
                           '追求总体判别力时用约登指数最大。')
                st.caption('注：截断值基于当前数据集的全样本预测概率估计；'
                           '若模型在同一数据上拟合，诊断效能可能偏乐观，'
                           '建议结合测试集或外部验证确认。')
                if cutoff.get('plot'):
                    st.image(cutoff['plot'], use_container_width=True)
                with st.expander('📋 各阈值完整指标表（含混淆矩阵）', expanded=False):
                    render_table(cutoff['threshold_table'], hide_index=True)
                    st.download_button(
                        '⬇️ 下载阈值表 CSV',
                        data=cutoff['threshold_table'].to_csv(index=False).encode('utf-8-sig'),
                        file_name='诊断阈值表.csv', mime='text/csv', key='cutoff_download')
                    if cutoff.get('sampled'):
                        st.caption('提示：阈值过多，表格已等距抽样展示（三个候选截断点已保留）。')
                offer_report_section(
                    'cutoff', '最佳截断值分析', cutoff['display'],
                    table=summary_table, image=cutoff.get('plot') or None,
                    note='截断值是模型的附带分析，需要写进报告时点这里。')

            # 校准 + DCA
            c1, c2 = st.columns(2)
            with c1:
                st.markdown('<div class="subsection-header">校准曲线</div>', unsafe_allow_html=True)
                st.image(result['calibration_plot'], use_container_width=True)
            with c2:
                st.markdown('<div class="subsection-header">决策曲线分析（DCA）</div>', unsafe_allow_html=True)
                st.image(result['dca_plot'], use_container_width=True)

            # 列线图
            var_ranges = {p: (pd.to_numeric(df[p], errors='coerce').min(),
                             pd.to_numeric(df[p], errors='coerce').max()) for p in predictors}
            nomo = PredictiveModeling.nomogram_points(result['coef_table'], var_ranges)
            if nomo:
                st.markdown('<div class="subsection-header">列线图（变量贡献）</div>', unsafe_allow_html=True)
                st.image(nomo, use_container_width=True)

            # 测试集分类报告
            if result.get('test_metrics') and 'classification_report' in result['test_metrics']:
                st.markdown('<div class="subsection-header">测试集分类报告</div>', unsafe_allow_html=True)
                cr = result['test_metrics']['classification_report']
                cr_df = pd.DataFrame(cr).transpose().round(3)
                render_table(cr_df)

            # 收录到报告（点按钮才收录）
            offer_report_section(
                'model_lr', f"Logistic 回归 - {outcome}",
                f"Pseudo R²={result['pseudo_r2']}, n={result['n']}",
                table=coef_df, image=result['roc_plot'])
            train_auc = (result.get('train_metrics') or {}).get('auc')
            render_inline_ai_write(
                'model_lr', f'Logistic 回归（结局：{outcome}）',
                summary=(f"样本量 {result['n']}，Pseudo R²={result['pseudo_r2']}，"
                         + (f"训练集 AUC={train_auc}，事件 {result.get('n_events', '—')}" if train_auc else '')),
                tables=coef_df)

    elif model_type == "Cox 生存回归":
        st.markdown('<div class="subsection-header">模型配置</div>', unsafe_allow_html=True)
        st.info("💡 Cox 回归需要：生存时间列（>0）+ 事件列（0/1）+ 协变量")

        if not numeric_fields or not binary_fields:
            st.warning('⚠️ Cox 回归需要至少一个数值列（生存时间）与一个二分类列（事件）。'
                       '可到「🧹 数据准备 → 🏷️ 变量类型调整」指定后再试。')
            return

        # 默认选中「最像」生存时间 / 事件的列，避免用户错把它当成第一个数值列。
        # 注意不能只看单个字：'年' 会命中「年龄」、'月' 会命中「月龄」，
        # 因此按语义强度排序，并复用关键词表与排除词。
        def _time_rank(name: str) -> int:
            lower = str(name).lower()
            if any(hint in lower for hint in ('随访', '生存', 'duration', 'time')):
                return 2
            if (any(hint in lower for hint in DataLoader.DURATION_HINTS)
                    and not any(hint in lower for hint in DataLoader.DURATION_EXCLUDE)):
                return 1
            return 0

        time_default = max(range(len(numeric_fields)), key=lambda i: _time_rank(numeric_fields[i]))
        event_default = next(
            (i for i, name in enumerate(binary_fields)
             if any(hint in str(name).lower() for hint in DataLoader.OUTCOME_HINTS)), 0)
        # 用会话值显式校正：selectbox 的 index 只在首次创建时生效，切数据集后要重设
        if st.session_state.get('cox_time') not in numeric_fields:
            st.session_state.cox_time = numeric_fields[time_default]
        if st.session_state.get('cox_event') not in binary_fields:
            st.session_state.cox_event = binary_fields[event_default]

        c1, c2 = st.columns(2)
        with c1:
            time_col = st.selectbox("生存时间列（必须 > 0）", numeric_fields, key='cox_time')
        with c2:
            event_col = st.selectbox("事件列（0/1）", binary_fields, key='cox_event')

        predictors = st.multiselect("预测变量",
            [p for p in numeric_fields + cat_fields if p not in (time_col, event_col)])

        run_cox = st.button("🚀 运行 Cox 回归", type="primary")
        cox_sig = (time_col, event_col, tuple(predictors or []))
        if run_cox and not (time_col and event_col and predictors):
            st.warning('⚠️ 请先选择生存时间列、事件列与至少一个预测变量')
        if run_cox and time_col and event_col and predictors:
            with st.spinner("拟合 Cox 模型中..."):
                store_panel_result(
                    'cox', PredictiveModeling.cox_regression(
                        df, time_col, event_col, predictors), signature=cox_sig)

        result = panel_result('cox', signature=cox_sig)
        if result and result.get('error'):
            st.error(result['error'])
        elif result:
            st.markdown('<div class="subsection-header">模型评价</div>', unsafe_allow_html=True)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("样本量", result['n'])
            c2.metric("事件数", result['events'])
            c3.metric("C-index", result['concordance_index'])
            c4.metric("AIC", result['AIC'])

            coef_df = pd.DataFrame(result['coef_table'])
            st.markdown('<div class="subsection-header">Cox 回归系数表</div>', unsafe_allow_html=True)
            render_table(coef_df, hide_index=True)

            st.markdown('<div class="subsection-header">KM 生存曲线</div>', unsafe_allow_html=True)
            st.image(result['km_plot'], use_container_width=True)

            offer_report_section(
                'model_cox', f"Cox 回归 - {time_col}×{event_col}",
                f"C-index={result['concordance_index']}",
                table=coef_df, image=result['km_plot'])
            render_inline_ai_write(
                'model_cox', f'Cox 生存回归（{time_col} × {event_col}）',
                summary=(f"C-index={result['concordance_index']}，"
                         f"事件数 {result.get('n_events', '—')}，样本量 {result.get('n', '—')}"),
                tables=coef_df)

    elif model_type == "LASSO 筛选 + Bootstrap 内部验证":
        render_model_validation(df, numeric_fields, cat_fields, binary_fields)

    else:  # 剂量-反应
        render_dose_response()


def render_model_validation(df, numeric_fields, cat_fields, binary_fields) -> None:
    """LASSO 变量筛选 + 交叉验证 + Bootstrap 内部验证"""
    from modules.validation import ModelValidation

    st.markdown('<div class="subsection-header">LASSO 筛选与内部验证</div>',
                unsafe_allow_html=True)
    st.caption('候选变量较多时用它筛选自变量，并估计模型的真实泛化能力（乐观度校正）。')

    if not binary_fields:
        st.info('💡 需要 0/1 结局变量（如 是否复发 / 是否死亡 / 是否感染）')
        return

    if st.session_state.pop('pending_select_all_predictors', False):
        current = st.session_state.get('val_outcome') or (binary_fields[0] if binary_fields else None)
        st.session_state['val_predictors'] = [p for p in numeric_fields + cat_fields
                                              if p != current]

    col1, col2 = st.columns(2)
    outcome = col1.selectbox('结局变量（0/1）', binary_fields, key='val_outcome')
    candidates = [p for p in numeric_fields + cat_fields if p != outcome]
    predictors = col2.multiselect('候选预测变量', candidates, key='val_predictors')
    if col2.button('全选候选变量', key='val_select_all'):
        st.session_state['pending_select_all_predictors'] = True
        st.rerun()
    col3, col4 = st.columns(2)
    n_bootstrap = col3.slider('Bootstrap 重抽样次数', 100, 1000, 200, 100, key='val_boot',
                              help='次数越多越稳定，耗时也越长')
    cv_folds = col4.slider('交叉验证折数', 3, 10, 5, 1, key='val_folds')

    if len(predictors) < 2:
        st.caption('请选择至少 2 个候选预测变量')
        return

    val_sig = (outcome, tuple(predictors or []), int(n_bootstrap), int(cv_folds))
    if st.button('🚀 运行 LASSO + 内部验证', type='primary'):
        with st.spinner('正在筛选变量并做重抽样验证...'):
            lasso = ModelValidation.lasso_selection(df, outcome, predictors, cv_folds=cv_folds)
            if lasso.get('error'):
                store_panel_result('lasso', {'error': lasso['error']}, signature=val_sig)
            else:
                store_panel_result('lasso', {
                    'lasso': lasso,
                    'cv': ModelValidation.cross_validation(df, outcome, predictors,
                                                           folds=cv_folds),
                    'boot': ModelValidation.bootstrap_validation(
                        df, outcome, predictors, n_bootstrap=int(n_bootstrap)),
                }, signature=val_sig)

    # 结果从会话里取：重抽样很花时间，运行一次之后不该因为点别的控件就白跑
    stored = panel_result('lasso', signature=val_sig)
    if stored and stored.get('error'):
        st.error(f"❌ {stored['error']}")
    if stored and not stored.get('error'):
        lasso = stored['lasso']
        cv_result = stored['cv']
        boot = stored['boot']

        st.markdown('<div class="subsection-header">1. LASSO 变量筛选</div>',
                    unsafe_allow_html=True)
        col1, col2, col3 = st.columns(3)
        col1.metric('有效样本', lasso['n'])
        col2.metric('候选变量', lasso['n_candidates'])
        col3.metric('入选变量', len(lasso['selected']))
        st.caption(f"最优正则强度 C = {lasso['best_c']:.4f}（{cv_folds} 折交叉验证选择）")
        if lasso['selected']:
            st.success('入选变量：' + '、'.join(lasso['selected']))
        else:
            st.warning('没有变量被保留，可减少候选变量或增加样本')
        render_table(pd.DataFrame(lasso['table']), hide_index=True)
        offer_report_section(
            'lasso', f"LASSO 变量筛选 - {outcome}",
            f"入选 {len(lasso['selected'])} 个变量：" + '、'.join(lasso['selected']),
            table=pd.DataFrame(lasso['table']))

        st.markdown('<div class="subsection-header">2. 内部验证</div>', unsafe_allow_html=True)
        col1, col2 = st.columns(2)
        if cv_result.get('error'):
            col1.warning(f"⚠️ 交叉验证：{cv_result['error']}")
        else:
            col1.metric(f"{cv_folds} 折交叉验证 AUC", cv_result['display'])
            col1.caption('各折 AUC：' + '、'.join(str(score) for score in cv_result['scores']))

        if boot.get('error'):
            col2.warning(f"⚠️ Bootstrap 验证：{boot['error']}")
        else:
            col2.metric('校正后 AUC', f"{boot['corrected_auc']:.3f}",
                        delta=f"乐观度 {boot['optimism']:.3f}")
            col2.caption(boot['display'])
            offer_report_section(
                'validation', f"内部验证 - {outcome}",
                f"表观 AUC {boot['apparent_auc']:.3f}，平均乐观度 {boot['optimism']:.3f}，"
                f"校正 AUC {boot['corrected_auc']:.3f}"
                f"（95%CI {boot['ci'][0]:.3f}–{boot['ci'][1]:.3f}）")


def render_dose_response() -> None:
    """剂量-反应关系（限制性立方样条 RCS）"""
    from modules.dose_response import DoseResponse

    df = st.session_state.df
    fields = st.session_state.report_fields
    numeric_fields = [k for k, v in fields.items()
                      if k in df.columns and v['dtype'] == 'numeric']
    binary_fields = [k for k, v in fields.items()
                     if k in df.columns and v['dtype'] == 'binary']
    cat_fields = [k for k, v in fields.items()
                  if k in df.columns and v['dtype'] in ('categorical', 'binary')]

    st.markdown("""
    <div style="background: #EFF6FF; border-left: 3px solid #3B82F6; padding: 0.8rem 1rem;
                border-radius: 8px; margin-bottom: 1rem; color: #1E40AF; font-size: 0.9rem;">
        💡 <strong>用它能回答什么</strong>：连续暴露与结局之间是直线关系，还是存在
        阈值、饱和或 J 型？限制性立方样条（RCS）比「人为分组比较」保留更多信息，
        也能检验非线性是否显著。
    </div>
    """, unsafe_allow_html=True)

    if not numeric_fields:
        st.info('💡 需要至少一个连续暴露变量')
        return

    col1, col2, col3 = st.columns(3)
    exposure = col1.selectbox('暴露变量（连续）', numeric_fields, key='dr_exposure')
    outcome_candidates = [c for c in numeric_fields + binary_fields if c != exposure]
    if not outcome_candidates:
        st.warning('⚠️ 没有可用的结局变量')
        return
    outcome = col2.selectbox('结局变量（连续或 0/1）', outcome_candidates, key='dr_outcome')
    n_knots = col3.selectbox('节点数（自由度 = 节点数 − 1）', [3, 4, 5, 6], index=1,
                             key='dr_knots',
                             help='节点越多曲线越灵活，也越容易过拟合；4 个节点最常用')

    covariates = st.multiselect(
        '协变量（可选，用于调整混杂）',
        [c for c in numeric_fields + cat_fields if c not in (exposure, outcome)],
        key='dr_covariates')

    col4, col5 = st.columns(2)
    reference_mode = col4.radio('参考点', ['中位数（推荐）', '自定义'], horizontal=True,
                                key='dr_ref_mode')
    reference = None
    if reference_mode == '自定义':
        exposure_values = pd.to_numeric(df[exposure], errors='coerce').dropna()
        default_reference = float(exposure_values.median()) if not exposure_values.empty else 0.0
        reference = float(col5.number_input('参考点取值', value=default_reference,
                                            key='dr_reference'))

    outcome_type = 'binary' if outcome in binary_fields else 'continuous'

    dr_sig = (exposure, outcome, tuple(covariates or []), int(n_knots), reference)
    if st.button('🚀 运行剂量-反应分析', type='primary'):
        with st.spinner('正在拟合限制性立方样条...'):
            store_panel_result('rcs', DoseResponse.analyze(
                df, exposure, outcome, covariates=covariates,
                outcome_type=outcome_type, n_knots=int(n_knots),
                reference=reference), signature=dr_sig)

    result = panel_result('rcs', signature=dr_sig)
    if result and result.get('error'):
        st.error(f"❌ {result['error']}")
    if result and not result.get('error'):

        col1, col2, col3 = st.columns(3)
        col1.metric('有效样本', result['n'])
        col2.metric('整体关联 p', f"{result['overall']['p']:.4g}")
        col3.metric('非线性 p', f"{result['nonlinear']['p']:.4g}",
                    delta='非线性' if result['nonlinear']['p'] < 0.05 else '近似线性',
                    delta_color='inverse' if result['nonlinear']['p'] < 0.05 else 'normal')

        if result['nonlinear']['p'] < 0.05:
            st.warning('⚠️ 存在显著非线性：暴露与结局不是直线关系，'
                       '按线性回归或简单二分组解释会丢失信息。')
        else:
            st.success('✅ 未见显著非线性：可近似按线性关系解读，线性模型的斜率更易解释与推广。')

        st.caption(f"参考点：{result['reference_label']}；节点："
                   f"{'、'.join(f'{knot:.4g}' for knot in result['knots'])}；"
                   f"调整变量：{'、'.join(result['covariates']) if result['covariates'] else '无'}"
                   f"；伪 R² = {result['pseudo_r2']:.4f}")

        st.image(result['plot'], use_container_width=True)

        st.markdown('**相对参考点的效应估计（抽样展示）**')
        curve = result['curve']
        step = max(1, len(curve) // 20)
        render_table(curve.iloc[::step].reset_index(drop=True), hide_index=True)
        st.caption('完整曲线见上图；曲线两端的估计基于样本稀疏区域，解释需谨慎；'
                   '协变量固定在参照水平（连续变量取 0 / 分类变量取参照类）。')

        offer_report_section(
            'rcs', f"剂量-反应（RCS）- {exposure} → {outcome}",
            f"整体关联 p = {result['overall']['p']:.4g}，"
            f"非线性 p = {result['nonlinear']['p']:.4g}；参考点 {result['reference_label']}",
            image=result['plot'])


def render_research():
    """📚 科研论文辅助"""
    from modules.research import ResearchAssistant

    st.markdown("""
    <div class="section-header">
        <h2>📚 科研论文辅助</h2>
    </div>
    """, unsafe_allow_html=True)

    df = st.session_state.df
    fields = st.session_state.report_fields
    numeric_fields = [k for k, v in fields.items() if v['dtype'] == 'numeric']
    binary_fields = [k for k, v in fields.items() if v['dtype'] == 'binary']

    func = st.radio("功能",
        ["基线特征表（Table 1）", "倾向性评分匹配（PSM）", "IPTW 逆概率加权",
         "亚组分析森林图", "敏感性分析（E-value）"],
        horizontal=True, key='paper_func'
    )

    if func == "基线特征表（Table 1）":
        render_table1_panel()
        return

    if func == "倾向性评分匹配（PSM）":
        st.markdown("""
        <div style="background: #EFF6FF; border-left: 3px solid #3B82F6; padding: 0.8rem 1rem;
                    border-radius: 8px; margin-bottom: 1rem; color: #1E40AF; font-size: 0.9rem;">
            💡 <strong>PSM 用途</strong>：当处理组与对照组基线特征不均衡时，使用倾向性评分匹配来模拟"随机分组"效果，控制混杂偏倚。
        </div>
        """, unsafe_allow_html=True)

        c1, c2 = st.columns(2)
        with c1:
            treatment = st.selectbox("处理变量", binary_fields, key='psm_treat')
        with c2:
            outcome = st.selectbox("结局变量", [b for b in binary_fields if b != treatment], key='psm_outcome')

        covariates = st.multiselect("协变量（用于计算倾向性评分）",
            [n for n in numeric_fields if n not in (treatment, outcome)])

        c1, c2 = st.columns(2)
        with c1:
            caliper = st.slider("卡尺（SD）", 0.05, 0.5, 0.2, 0.05,
                              help="越小越严格，匹配数越少")
        with c2:
            ratio = st.selectbox("匹配比例", [1, 2, 3], index=0,
                                help="1:k 最近邻匹配")

        run_psm = st.button("🚀 运行 PSM", type="primary")
        psm_sig = (treatment, outcome, tuple(covariates or []), float(caliper), int(ratio))
        if run_psm and not (treatment and outcome and covariates):
            st.warning('⚠️ 请先选择处理变量、结局变量与至少一个协变量')
        if run_psm and treatment and outcome and covariates:
            with st.spinner("匹配中..."):
                store_panel_result(
                    'psm', ResearchAssistant.propensity_score_matching(
                        df, treatment, outcome, covariates, caliper=caliper,
                        ratio=ratio), signature=psm_sig)

        # 从会话取结果，匹配结果不会因为点别的控件而从屏幕上消失
        result = panel_result('psm', signature=psm_sig)
        if result is not None:
            if 'error' in result:
                st.error(result['error'])
            else:
                st.markdown('<div class="subsection-header">匹配概览</div>', unsafe_allow_html=True)
                c1, c2, c3 = st.columns(3)
                c1.metric("原始 处理/对照",
                         f"{result['n_treated_original']} / {result['n_control_original']}")
                c2.metric("匹配后对数", result['n_matched_pairs'])
                c3.metric("PS 模型 AUC", f"{result.get('ps_model_auc', 0):.3f}",
                         delta="模型区分度" + ("良好" if result.get('ps_model_auc', 0) > 0.7 else "一般"))

                outcome_res = result['outcome_analysis']
                st.markdown('<div class="subsection-header">匹配后效应估计</div>', unsafe_allow_html=True)
                if outcome_res.get('type') == 'binary':
                    if outcome_res['p_value'] < 0.05:
                        st.success(f"✅ **McNemar 检验**：χ²={outcome_res['mcnemar_chi2']:.3f}, "
                                 f"p={outcome_res['p_value']:.4f}\n\n"
                                 f"**OR = {outcome_res.get('OR'):.3f}**, "
                                 f"{outcome_res.get('OR_CI', '')}")
                    else:
                        st.info(f"ℹ️ **McNemar 检验**：χ²={outcome_res['mcnemar_chi2']:.3f}, "
                              f"p={outcome_res['p_value']:.4f}（不显著）")
                else:
                    st.success(f"**配对 t 检验**：t={outcome_res['t']}, p={outcome_res['p_value']}, "
                             f"差值={outcome_res['mean_diff']}")

                if result['all_balanced_after']:
                    st.success("✅ 所有协变量匹配后 |SMD| < 0.1，达到良好平衡")
                else:
                    st.warning("⚠️ 部分协变量 |SMD| ≥ 0.1，建议调整卡尺或增加协变量")

                # 可视化
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown('<div class="subsection-header">匹配前 PS 分布</div>', unsafe_allow_html=True)
                    st.image(result['ps_plot_before'], use_container_width=True)
                with c2:
                    st.markdown('<div class="subsection-header">匹配后 PS 分布</div>', unsafe_allow_html=True)
                    st.image(result['ps_plot_after'], use_container_width=True)

                st.markdown('<div class="subsection-header">Love Plot - 协变量平衡</div>', unsafe_allow_html=True)
                st.image(result['love_plot'], use_container_width=True)

                smd_df = pd.DataFrame(result['smd_before'])
                offer_report_section(
                    'psm', "倾向性评分匹配",
                    f"匹配对={result['n_matched_pairs']}, PS-AUC={result['ps_model_auc']}",
                    table=smd_df, image=result['love_plot'])

    elif func == "亚组分析森林图":
        st.markdown("""
        <div style="background: #EFF6FF; border-left: 3px solid #3B82F6; padding: 0.8rem 1rem;
                    border-radius: 8px; margin-bottom: 1rem; color: #1E40AF; font-size: 0.9rem;">
            💡 <strong>亚组分析</strong>：检验处理效应在不同人群（如年龄、性别、严重程度分层）中是否一致。交互 p < 0.05 提示存在显著亚组差异。
        </div>
        """, unsafe_allow_html=True)

        c1, c2 = st.columns(2)
        with c1:
            outcome = st.selectbox("结局变量", binary_fields, key='sub_out')
        with c2:
            treatment = st.selectbox("处理变量", [b for b in binary_fields if b != outcome], key='sub_treat')

        cat_fields = [k for k, v in fields.items() if v['dtype'] in ('categorical', 'binary') and v['n_unique'] <= 8]
        subgroup_var = st.selectbox("亚组变量", [c for c in cat_fields if c not in (outcome, treatment)])

        run_sub = st.button("🚀 运行亚组分析", type="primary")
        sub_sig = (outcome, treatment, subgroup_var)
        if outcome and treatment and subgroup_var and run_sub:
            store_panel_result(
                'subgroup', ResearchAssistant.subgroup_analysis(
                    df, outcome, treatment, subgroup_var), signature=sub_sig)

        result = panel_result('subgroup', signature=sub_sig)
        if result is not None:
            if 'error' in result:
                st.error(result['error'])
            else:
                sub_df = pd.DataFrame(result['results'])
                st.markdown(f'<div class="subsection-header">{subgroup_var} 各亚组效应</div>', unsafe_allow_html=True)
                render_table(sub_df, hide_index=True)

                if result['interaction_p'] is not None:
                    if result['interaction_significant']:
                        st.warning(f"⚠️ **交互检验** p = {result['interaction_p']:.4f} < 0.05，存在显著亚组差异")
                    else:
                        st.success(f"✅ **交互检验** p = {result['interaction_p']:.4f} ≥ 0.05，无显著亚组差异")

                st.markdown('<div class="subsection-header">森林图</div>', unsafe_allow_html=True)
                st.image(result['forest_plot'], use_container_width=True)
                offer_report_section(
                    'subgroup', f"亚组分析 - {subgroup_var}",
                    f"交互 p={result['interaction_p']}",
                    table=sub_df, image=result['forest_plot'])

    elif func == "敏感性分析（E-value）":
        st.markdown("""
        <div style="background: #EFF6FF; border-left: 3px solid #3B82F6; padding: 0.8rem 1rem;
                    border-radius: 8px; margin-bottom: 1rem; color: #1E40AF; font-size: 0.9rem;">
            💡 <strong>E-value 解读</strong>：评估未测量混杂对观察关联的解释能力。<br>
            E-value ≥ 1.5 为中等稳健，≥ 2 为强稳健，越大越难被混杂解释。
        </div>
        """, unsafe_allow_html=True)

        rr = st.number_input("观察到的 RR / OR", min_value=0.1, max_value=10.0, value=2.0, step=0.1)
        if st.button("计算 E-value"):
            result = ResearchAssistant.e_value(rr)
            st.metric("E-value", f"{result['E_value']:.2f}")
            st.info(result['interpretation'])

            # 解读建议
            ev = result['E_value']
            if ev >= 2:
                st.success("🟢 **强稳健**：即使存在较强的未测量混杂，结论也较难被推翻")
            elif ev >= 1.5:
                st.info("🟡 **中等稳健**：需要中等强度的未测量混杂才能解释观察关联")
            else:
                st.warning("🟠 **欠稳健**：未测量混杂可能解释观察关联，建议谨慎解读")

    else:  # IPTW
        render_iptw()


def render_iptw() -> None:
    """逆概率加权（IPTW）：倾向评分 → 权重 → 平衡诊断 → 加权效应"""
    from modules.iptw import IPTWAnalysis

    df = st.session_state.df
    fields = st.session_state.report_fields
    numeric_fields = [k for k, v in fields.items()
                      if k in df.columns and v['dtype'] == 'numeric']
    binary_fields = [k for k, v in fields.items()
                     if k in df.columns and v['dtype'] == 'binary']
    cat_fields = [k for k, v in fields.items()
                  if k in df.columns and v['dtype'] in ('categorical', 'binary')]

    st.markdown("""
    <div style="background: #EFF6FF; border-left: 3px solid #3B82F6; padding: 0.8rem 1rem;
                border-radius: 8px; margin-bottom: 1rem; color: #1E40AF; font-size: 0.9rem;">
        💡 <strong>IPTW 与 PSM 的区别</strong>：IPTW 按倾向评分的倒数给每个样本加权，
        保留全部样本；PSM 只保留匹配上的样本。两者都需要「处理变量 + 结局 + 预先确定的协变量」。
    </div>
    """, unsafe_allow_html=True)

    if not binary_fields:
        st.info('💡 需要 0/1 处理变量（如 是否接受某种治疗 / 是否使用某药）')
        return

    col1, col2 = st.columns(2)
    treatment = col1.selectbox('处理变量（0/1）', binary_fields, key='iptw_treat')
    outcome_candidates = [c for c in numeric_fields + binary_fields if c != treatment]
    if not outcome_candidates:
        st.warning('⚠️ 没有可用的结局变量（需要至少一个数值或 0/1 变量）')
        return
    outcome = col2.selectbox('结局变量（连续或 0/1）', outcome_candidates, key='iptw_outcome')

    covariate_candidates = [c for c in numeric_fields + cat_fields
                            if c not in (treatment, outcome)]
    if st.session_state.pop('pending_select_all_covariates', False):
        st.session_state['iptw_covariates'] = list(covariate_candidates)

    covariates = st.multiselect(
        '协变量（建议在研究设计阶段预先确定，不要按结果挑选）',
        covariate_candidates, key='iptw_covariates')
    if st.button('全选协变量', key='iptw_select_all'):
        st.session_state['pending_select_all_covariates'] = True
        st.rerun()

    col3, col4, col5 = st.columns(3)
    weight_label = col3.selectbox(
        '权重类型',
        ['ATE 权重（全人群平均处理效应）',
         'ATT 权重（处理组的平均处理效应）',
         'ATM 重叠权重（极端倾向评分影响更小）'],
        key='iptw_weight')
    trim = col4.slider('权重截断分位', 0.0, 0.10, 0.01, 0.01, key='iptw_trim',
                       help='按分位数截断极端权重，0 表示不截断')
    n_bootstrap = col5.slider('Bootstrap 次数', 100, 500, 200, 50, key='iptw_boot')

    if not covariates:
        st.caption('请至少选择 1 个协变量，用于估计倾向评分')
        return

    weight_type = 'ATE'
    if weight_label.startswith('ATT'):
        weight_type = 'ATT'
    elif weight_label.startswith('ATM'):
        weight_type = 'ATM'
    iptw_sig = (treatment, outcome, tuple(covariates or []), weight_type,
                float(trim), int(n_bootstrap))

    if st.button('🚀 运行 IPTW 分析', type='primary'):
        with st.spinner('正在估计倾向评分、构造权重并做重抽样推断...'):
            store_panel_result(
                'iptw', IPTWAnalysis.run(
                    df, treatment, outcome, covariates, weight_type=weight_type,
                    trim=float(trim), n_bootstrap=int(n_bootstrap)),
                signature=iptw_sig)

    result = panel_result('iptw', signature=iptw_sig)
    if result and result.get('error'):
        st.error(f"❌ {result['error']}")
    if result and not result.get('error'):

        st.markdown('<div class="subsection-header">加权后的效应估计</div>',
                    unsafe_allow_html=True)
        col1, col2, col3, col4 = st.columns(4)
        col1.metric('有效样本', result['n'])
        col2.metric('有效样本量 ESS', f"{result['ess']:.0f}")
        col3.metric('加权前最大 |SMD|', f"{result['max_smd_before']:.3f}")
        col4.metric('加权后最大 |SMD|', f"{result['max_smd_after']:.3f}",
                    delta='已平衡' if result['max_smd_after'] < 0.1 else '仍有失衡',
                    delta_color='normal' if result['max_smd_after'] < 0.1 else 'inverse')
        st.caption(f"{result['weight_label']}；处理组 {result['n_treated']} 例 / "
                   f"对照组 {result['n_control']} 例；权重截断分位 {result['trim']}；"
                   f"Bootstrap {result['n_bootstrap']} 次（每次重估倾向评分）")

        summary = result['summary']
        if result['outcome_type'] == 'continuous':
            st.markdown(f"**加权后平均处理效应（差值）：{result['effect']['estimate']:.3f}**")
            st.caption(f"95%CI {summary['ci'][0]:.3f} ~ {summary['ci'][1]:.3f}；"
                       f"p = {summary['p']:.4f}；处理组 {result['effect']['treated']:.3f}，"
                       f"对照组 {result['effect']['control']:.3f}")
        else:
            st.markdown(f"**加权后风险差：{result['effect']['estimate']:.4f}**")
            st.caption(f"95%CI {summary['ci'][0]:.4f} ~ {summary['ci'][1]:.4f}；"
                       f"p = {summary['p']:.4f}；处理组风险 {result['effect']['treated']:.4f}，"
                       f"对照组风险 {result['effect']['control']:.4f}")
            col1, col2 = st.columns(2)
            extra = result.get('extra_summary') or {}
            if 'risk_ratio' in result['effect']:
                col1.metric('风险比 RR', f"{result['effect']['risk_ratio']:.4f}")
                ratio_ci = (extra.get('risk_ratio') or {}).get('ci')
                if ratio_ci:
                    col1.caption(f"95%CI {ratio_ci[0]:.3f} ~ {ratio_ci[1]:.3f}")
            if 'odds_ratio' in result['effect']:
                col2.metric('优势比 OR', f"{result['effect']['odds_ratio']:.4f}")
                odds_ci = (extra.get('odds_ratio') or {}).get('ci')
                if odds_ci:
                    col2.caption(f"95%CI {odds_ci[0]:.3f} ~ {odds_ci[1]:.3f}")

        st.markdown('<div class="subsection-header">平衡诊断</div>', unsafe_allow_html=True)
        if result['love_plot']:
            st.image(result['love_plot'], use_container_width=True)
        with st.expander('📋 加权前后平衡表（SMD）'):
            st.markdown('**加权前**')
            render_table(result['balance_before'], hide_index=True)
            st.markdown('**加权后**')
            render_table(result['balance_after'], hide_index=True)

        if result['max_smd_after'] >= 0.1:
            st.warning('⚠️ 加权后仍有协变量 |SMD| ≥ 0.1。可尝试换权重类型、调整截断分位，'
                       '或检查倾向评分模型是否遗漏了重要混杂因素。')
        else:
            st.success('✅ 加权后所有协变量 |SMD| < 0.1，组间均衡性良好。')

        offer_report_section(
            'iptw', f"IPTW（{result['weight_type']}）- {treatment} → {outcome}",
            f"加权后效应 {result['effect']['estimate']:.4f}"
            f"（95%CI {summary['ci'][0]:.4f}~{summary['ci'][1]:.4f}，p = {summary['p']:.4f}）；"
            f"加权后最大 |SMD| = {result['max_smd_after']:.3f}；ESS = {result['ess']:.0f}",
            table=result['balance_after'], image=result['love_plot'])


def _sections_to_material(sections, max_rows: int = 12, max_cols: int = 12,
                          max_chars: int = 20000) -> str:
    """把已收集的分析章节转成给 AI 的材料文本（表格截断，不含个体数据行）。"""
    lines = []
    for index, section in enumerate(sections or [], 1):
        lines.append(f'### {index}. {section.get("title", "")}')
        content = section.get('content')
        if content:
            lines.append(str(content)[:2000])
        table = section.get('table')
        if isinstance(table, pd.DataFrame) and not table.empty:
            trimmed = table.iloc[:max_rows, :max_cols]
            lines.append(trimmed.to_string(index=False))
            if len(table) > max_rows or len(table.columns) > max_cols:
                lines.append(f'（表格已截断展示：完整为 {len(table)} 行 × {len(table.columns)} 列）')
        if section.get('image'):
            lines.append('（本章节含统计图，未发送图片内容）')
        lines.append('')
    return '\n'.join(lines)[:max_chars]


def render_methods_panel(cache) -> None:
    """
    报告导出页的「方法学段落」区块。

    把本次实际执行过的处置（缺失、异常值、变量类型、用过的统计方法）汇总成
    可写入论文的方法学草稿。只汇总真实发生过的操作，不推测。
    """
    from modules.methods import build_draft, collect, summarize, to_plain_text

    st.markdown('<div class="subsection-header">📝 方法学段落（自动汇总）</div>',
                unsafe_allow_html=True)
    st.caption('论文的「方法」部分必须交代缺失值如何处理、异常值是否排除、用了哪些检验。'
               '这里把本次**实际执行过的处置**汇总成草稿——未做过的操作不会出现。')

    col_design, col_alpha = st.columns(2)
    design = col_design.selectbox(
        '研究设计（写入首句）',
        ['未指定', '回顾性队列研究', '前瞻性队列研究', '横断面研究',
         '病例对照研究', '随机对照试验的二次分析', '其他'],
        key='methods_design')
    alpha = col_alpha.selectbox('检验水准 α', [0.05, 0.01, 0.10], key='methods_alpha')

    collected = collect(
        st.session_state.get('df'), st.session_state.get('quality_report'),
        cache.sections,
        overrides=st.session_state.get('type_overrides'),
        missing=st.session_state.get('missing_result'),
        cleaning=st.session_state.get('cleaning_record'),
        design=('' if design == '未指定' else design),
        alpha=float(alpha), software=f'医析台 MedBench {APP_VERSION}',
        dataset_source=st.session_state.get('dataset_source') or '')

    st.caption(summarize(collected))

    strategy_label = {'winsorize': '缩尾处理', 'drop': '剔除记录',
                      'keep': '保留未处理'}.get(
                          collected['outliers'].get('strategy') or 'keep', '—')
    st.table(pd.DataFrame([
        ['样本量', f"{collected['sample']['n']} 例、{collected['sample']['columns']} 个变量"],
        ['缺失值', (collected['missing'].get('handled')
                 or ('完整案例分析（未插补）' if collected['missing']['any_missing'] else '无缺失'))],
        ['异常值', f"{strategy_label}（识别 {collected['outliers'].get('total', 0)} 个可疑值）"],
        ['变量类型人工核定', f"{len(collected['overrides'])} 个"],
        ['统计方法', f"{len(collected['methods'])} 类"],
    ], columns=['项目', '本次做法']))

    with st.expander('📋 各类统计方法对应的分析', expanded=False):
        if collected['methods']:
            for index, method in enumerate(collected['methods'], 1):
                st.markdown(f"{index}. **{method['category']}**：{method['statement']}")
                if method.get('titles'):
                    st.caption('　来源：' + '；'.join(str(t) for t in method['titles'][:4]))
        else:
            st.caption('尚未收录任何分析结果，请先在各分析页完成分析。')

    draft = build_draft(collected)
    with st.expander('👁️ 预览生成的段落', expanded=False):
        st.markdown(draft)

    col_add, col_note = st.columns([1, 2])
    with col_add:
        if st.button('📥 把方法学段落加入报告', use_container_width=True,
                     key='methods_to_report'):
            cache.add_section('方法学段落（自动汇总）', to_plain_text(draft), level=1)
            st.success('已加入报告，可在上方生成 Word')
        if st.session_state.get('report_language') == 'en':
            st.caption('⚠️ 方法学段落目前只生成中文版本。改用英文投稿时，'
                       '可用下方「AI 写作」选英文，把这一段转成英文表述。')
    with col_note:
        st.caption('草稿按**纯文本**加入报告（Word 不渲染 Markdown）。'
                   '写入论文前请通读核对，尤其是缺失处理与检验选择依据。')


def render_report_ai_panel(cache) -> None:
    """报告导出页的 AI 审阅（C）与 AI 写作（D）。"""
    from datetime import datetime

    from modules.ai_client import AIClient, AIError
    from modules.ai_prompts import (REVIEW_SYSTEM_PROMPT, WRITE_SYSTEM_PROMPT,
                                    WRITE_USER_TEMPLATE)

    st.markdown('---')
    st.markdown('<div class="subsection-header">🤖 AI 审阅与写作（可选）</div>',
                unsafe_allow_html=True)
    config = current_ai_config()
    if not config.is_ready():
        st.info('未配置 AI：在左侧「🤖 AI 解读（可选）」中填入服务地址与模型名后即可使用。'
                '未启用不影响报告导出。')
        return

    material = _sections_to_material(cache.sections)

    # 台账是「实际执行过哪些分析」的准确清单，比从章节标题反推可靠得多，
    # 因此一并作为材料发送（仍然不含任何个体数据行）
    ledger_entries = [item for item in (st.session_state.get('analysis_ledger') or [])
                      if item.get('kind') != 'data']
    if ledger_entries:
        from modules.ledger import to_text

        material = (material + '\n\n---\n\n# 本次实际执行过的分析台账\n\n'
                    '（以下为程序自动记录的分析清单，可作为方法部分的依据，'
                    '与上面的结果章节互为补充）\n\n'
                    + to_text(st.session_state.get('analysis_ledger')))

    st.caption(f'将发送：本次收录的 {len(cache.sections)} 个章节（标题、文本与统计表格），'
               + (f'以及分析台账（{len(ledger_entries)} 次分析），' if ledger_entries else '')
               + f'约 {len(material)} 字符；表格截断为最多 12 行 × 12 列，'
                 '不含任何个体数据行。')
    with st.expander('👁️ 预览将发送的全部材料（所见即所发）', expanded=False):
        st.code(material, language='markdown')
        st.download_button('⬇️ 下载材料文本', material.encode('utf-8'),
                           file_name='分析材料.md', mime='text/markdown',
                           key='ai_material_download')

    confirmed = st.checkbox(
        '我已确认：审阅与写作会发送上面的统计结果表格（不含个体数据行）',
        key='ai_report_confirm',
        help='与「结构摘要」不同，审阅必须看到结果数值才能判断方法是否用对，因此需要你显式确认。')

    # ---------- C. 审阅 ----------
    st.markdown('**🧑‍⚖️ 让 AI 审阅本次分析**')
    st.caption('检查方法是否适配、前提是否满足、解读是否存在常见错误、哪些地方可能被审稿人质疑。')
    if st.button('🧑‍⚖️ 开始审阅', type='primary', use_container_width=True, key='ai_review_run'):
        if not confirmed:
            st.warning('请先勾选上方确认项。')
        else:
            messages = [{'role': 'system', 'content': REVIEW_SYSTEM_PROMPT},
                        {'role': 'user', 'content': material}]
            with st.spinner(f'正在审阅…（流式，最长间隔 {config.timeout} 秒）'):
                try:
                    output = run_ai(config, messages, label='正在审阅本次分析')
                except AIError as error:
                    st.session_state.ai_review = None
                    st.error(f'❌ AI 调用失败：{error}')
                except Exception as error:
                    st.session_state.ai_review = None
                    st.error(f'❌ 未预期错误：{error}')
                else:
                    st.session_state.ai_review = {**output,
                                                  'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

    review = st.session_state.get('ai_review')
    if review:
        st.markdown(review.get('content') or '（AI 未返回内容）')
        st.caption(f"来源：{review.get('model')} · {review.get('at', '')}")
        if st.button('📥 审阅意见加入报告', use_container_width=True, key='ai_review_to_report'):
            cache.add_section('AI 审阅意见', review.get('content') or '', level=1)
            st.success('已加入报告，可重新生成 Word')

    # ---------- D. 写作 ----------
    st.markdown('**✍️ 让 AI 写论文段落**')
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        scope = st.selectbox('取材章节',
                             ['（全部分析）'] + [section.get('title', '') for section in cache.sections],
                             key='ai_write_scope')
    with c2:
        kind = st.selectbox('段落类型', ['结果（Results）', '方法（Methods）'], key='ai_write_kind')
    with c3:
        language = st.selectbox('语言', ['中文', '英文'], key='ai_write_lang')

    if st.button('✍️ 生成段落', type='primary', use_container_width=True, key='ai_write_run'):
        if not confirmed:
            st.warning('请先勾选上方确认项。')
        else:
            if scope == '（全部分析）':
                payload_text = material
            else:
                subset = [section for section in cache.sections
                          if section.get('title') == scope]
                payload_text = _sections_to_material(subset)
            messages = [
                {'role': 'system', 'content': WRITE_SYSTEM_PROMPT},
                {'role': 'user', 'content': WRITE_USER_TEMPLATE.format(
                    kind=kind, language=language, payload=payload_text)},
            ]
            with st.spinner(f'正在撰写…（流式，最长间隔 {config.timeout} 秒）'):
                try:
                    output = run_ai(config, messages, label='正在撰写段落',
                                    temperature=0.3)
                except AIError as error:
                    st.session_state.ai_write = None
                    st.error(f'❌ AI 调用失败：{error}')
                except Exception as error:
                    st.session_state.ai_write = None
                    st.error(f'❌ 未预期错误：{error}')
                else:
                    st.session_state.ai_write = {
                        **output, 'kind': kind, 'language': language,
                        'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

    write = st.session_state.get('ai_write')
    if write:
        st.markdown(write.get('content') or '（AI 未返回内容）')
        st.caption(f"来源：{write.get('model')} · {write.get('kind')} · {write.get('language')} · "
                   f"{write.get('at', '')}")
        if st.button('📥 段落加入报告', use_container_width=True, key='ai_write_to_report'):
            cache.add_section(f"AI 撰写段落（{write.get('kind')}）",
                              write.get('content') or '', level=1)
            st.success('已加入报告，可重新生成 Word')


def sync_ledger_from_sections() -> None:
    """
    把报告缓存里新增的章节同步进分析台账。

    报告缓存恰好只包含「分析成功后自动收录」的章节，不含用户没看过的内容，
    因此它天然就是「实际做过的分析」清单。同步过程按指纹去重，反复运行也不会重复记录。
    """
    from datetime import datetime

    from modules.ledger import from_sections

    cache = st.session_state.get('cache')
    sections = getattr(cache, 'sections', None) or []
    if not sections:
        return
    st.session_state.analysis_ledger = from_sections(
        st.session_state.get('analysis_ledger') or [], sections,
        at=datetime.now().strftime('%m-%d %H:%M'))


def render_ledger_panel() -> None:
    """分析台账：回溯「做过哪些分析、基于哪版数据」。"""
    from modules.ledger import (counts_by_type, summarize, to_csv, to_frame)

    entries = st.session_state.get('analysis_ledger') or []
    analyses = [item for item in entries if item.get('kind') != 'data']

    st.markdown('---')
    with st.expander(f'📋 分析台账（已记录 {len(analyses)} 次分析）', expanded=False):
        st.caption('每次产出结果的分析都会自动记入台账，可导出为清单。'
                   '台账独立于报告缓存 —— 换数据时缓存会清空，台账保留并插入'
                   '「数据已更新」分隔标记，因此能看出哪一段分析是基于哪一版数据做的。'
                   '（清空台账时会一并清空报告缓存：两者记录的是同一批分析。）')
        if not entries:
            st.info('台账为空：在任意分析页完成一次分析后，这里会自动记录。')
            return

        st.markdown(f'**{summarize(entries)}**')
        counts = counts_by_type(analyses)
        if counts:
            st.caption('类型分布：' + '、'.join(
                f'{name} {count}'
                for name, count in sorted(counts.items(), key=lambda pair: -pair[1])))

        frame = to_frame(entries)
        render_table(frame, hide_index=True)

        col_csv, col_report, col_clear = st.columns(3)
        with col_csv:
            st.download_button('⬇️ 导出 CSV', data=to_csv(entries),
                               file_name='分析台账.csv', mime='text/csv',
                               key='ledger_csv', use_container_width=True)
        with col_report:
            if st.button('📥 加入报告', use_container_width=True, key='ledger_to_report'):
                st.session_state.cache.add_section(
                    '分析台账（本次分析清单）', summarize(entries),
                    table=frame, level=1)
                st.success('已加入报告，可导出 Word')
        with col_clear:
            if st.button('🗑️ 清空台账与缓存', use_container_width=True, key='ledger_clear'):
                # 必须连报告缓存一起清：台账每轮都会从缓存同步，
                # 只清台账的话下一轮就会被原样填回来
                st.session_state.analysis_ledger = []
                st.session_state.cache.clear()
                st.session_state.report_export = None
                st.rerun()


def render_export():
    """📄 报告导出"""
    from modules.export import ResultCache

    st.markdown("""
    <div class="section-header">
        <h2>📄 报告导出</h2>      
    </div>
    """, unsafe_allow_html=True)

    cache = st.session_state.cache

    c1, c2 = st.columns([3, 1])
    with c1:
        st.markdown(f"**当前已收集 {len(cache.sections)} 个分析章节**")
    with c2:
        if st.button("🗑️ 清空缓存", use_container_width=True):
            cache.clear()
            st.session_state.report_export = None
            st.success("已清空")
            st.rerun()

    render_ledger_panel()

    if not cache.sections:
        st.info("💡 在各分析模块中完成分析后，结果会自动收录到这里，可以一并导出。")
        return

    st.markdown("---")

    # 报告语言（决定章节名与表/图题注的语言）
    from modules.localize import SUPPORTED_LANGUAGES, describe_language

    language_labels = {code: label for code, label in SUPPORTED_LANGUAGES}
    report_language = st.radio(
        '报告语言', options=[code for code, _ in SUPPORTED_LANGUAGES],
        format_func=lambda code: language_labels.get(code, code),
        horizontal=True, key='report_language')
    st.caption(describe_language(report_language))

    title = st.text_input(
        'Report title' if report_language == 'en' else '报告标题',
        'Medical Data Analysis Report' if report_language == 'en' else '医疗数据分析报告',
        key='report_title_input')
    if report_language == 'en' and not re.search(r'[A-Za-z]', title or ''):
        st.warning('当前标题不含英文字母，投英文期刊时建议改为英文标题。')

    all_titles = [sec['title'] for sec in cache.sections]
    selected = st.multiselect(
        "要导出的章节（默认全选；同一份数据做多个课题时，可只勾选本次需要的）",
        all_titles, default=all_titles, key='export_selected_sections')
    selected_set = set(selected or [])
    sections = [sec for sec in cache.sections if sec['title'] in selected_set]

    # ---------- 期刊模板（表格样式与数字格式） ----------
    from modules.journal import (FIGURE_TEMPLATES, TEMPLATES, FigureStyle, TableStyle,
                                 describe as describe_style, describe_figures)

    CUSTOM_TEMPLATE = '自定义（手动设置）'
    with st.expander('📐 投稿规格（三线表 + 图注编号）', expanded=False):
        st.caption('不同期刊对三线表的线宽、字体、字号与小数位要求不同；'
                   '报告会自动给表格加「表 1」、插图加「图 1」编号，也可在此调整。')
        template_choice = st.selectbox('期刊模板',
                                       list(TEMPLATES.keys()) + [CUSTOM_TEMPLATE],
                                       key='journal_template')
        if template_choice == CUSTOM_TEMPLATE:
            r1c1, r1c2, r1c3 = st.columns(3)
            top_pt = r1c1.number_input('顶/底线宽 (pt)', 0.25, 3.0, 1.5, 0.25, key='jt_top')
            header_pt = r1c2.number_input('表头线宽 (pt)', 0.25, 3.0, 0.75, 0.25, key='jt_header')
            body_size = r1c3.number_input('表体字号', 7.0, 14.0, 9.0, 0.5, key='jt_bsize')
            r2c1, r2c2, r2c3 = st.columns(3)
            header_size = r2c1.number_input('表头字号', 7.0, 14.0, 10.0, 0.5, key='jt_hsize')
            decimals = r2c2.number_input('小数位', 1, 4, 3, 1, key='jt_decimals')
            p_decimals = r2c3.number_input('p 值小数位', 2, 4, 3, 1, key='jt_pdecimals')
            r3c1, r3c2 = st.columns(2)
            table_font = r3c1.selectbox('字体', ['宋体', 'Times New Roman', 'Arial'],
                                        key='jt_font')
            header_bold = r3c2.checkbox('表头加粗', True, key='jt_bold')
            table_style = TableStyle(
                name=CUSTOM_TEMPLATE, font=table_font, top_pt=top_pt,
                header_pt=header_pt, bottom_pt=top_pt, header_bold=header_bold,
                header_size_pt=header_size, body_size_pt=body_size,
                decimals=int(decimals), p_decimals=int(p_decimals))
            f1, f2, f3, f4 = st.columns(4)
            fig_font = f1.selectbox('图片内字体',
                                    ['Microsoft YaHei', 'SimHei', 'SimSun', 'Arial'],
                                    key='fig_font')
            fig_base = f2.number_input('基础字号', 6.0, 16.0, 10.0, 0.5, key='fig_base')
            fig_title = f3.number_input('标题字号', 6.0, 18.0, 11.0, 0.5, key='fig_title')
            fig_tick = f4.number_input('刻度字号', 6.0, 14.0, 9.0, 0.5, key='fig_tick')
            fig_dpi = st.number_input('图片分辨率 (dpi)', 150, 600, 300, 50, key='fig_dpi')
            figure_style = FigureStyle(font=fig_font, base_size=float(fig_base),
                                       title_size=float(fig_title),
                                       label_size=float(fig_base),
                                       tick_size=float(fig_tick),
                                       legend_size=float(fig_tick),
                                       savefig_dpi=float(fig_dpi))
        else:
            table_style = TEMPLATES[template_choice]
            figure_style = FIGURE_TEMPLATES.get(template_choice) or FigureStyle()

        # 图注编号与图片宽度：这两项在导出时生效，改完立即有用
        c1, c2 = st.columns(2)
        number_captions = c1.checkbox('自动给表/图编号（表 1、图 1）', True, key='jt_number')
        image_width_cm = c2.number_input('图片宽度 (cm)，0 = 自适应', 0.0, 20.0, 0.0, 0.5,
                                         key='jt_imgwidth')
        table_style = table_style.with_style(
            number_captions=bool(number_captions),
            image_width_cm=float(image_width_cm),
            language=report_language)

        st.caption('表格：' + describe_style(table_style))
        st.caption('图片：' + describe_figures(figure_style))
        st.caption('数字格式按列名语义处理：p 值列固定小数位（极小值写 <0.001）、'
                   '百分比列一位小数、计数列取整，其余按「小数位」并去掉多余零。'
                   '注意：**图片内字体与字号在绘图时生效，改动后需重新运行分析页**；'
                   '图注编号与图片宽度在导出时生效。')

    export_kind = st.radio(
        "导出格式",
        ['📄 Word 报告（三线表 + 插图）',
         '📊 Excel 结果表（每章节一个工作表）',
         '🖼️ 图片包 ZIP（300 dpi PNG + TIFF）'],
        horizontal=True, key='export_kind')

    col_generate, col_download, col_metric = st.columns([2, 2, 1])
    with col_generate:
        if st.button("📥 生成导出文件", type="primary", use_container_width=True):
            if not sections:
                st.session_state.report_export = None
                st.warning("请至少勾选一个章节。")
            else:
                try:
                    stamp = pd.Timestamp.now().strftime('%Y%m%d_%H%M')
                    if export_kind.startswith('📊'):
                        payload = cache.export_excel(sections)
                        name = f"{title}_结果表_{stamp}.xlsx"
                        mime = ('application/vnd.openxmlformats-officedocument'
                                '.spreadsheetml.sheet')
                    elif export_kind.startswith('🖼️'):
                        payload = cache.export_images_zip(sections)
                        name = f"{title}_图片_{stamp}.zip"
                        mime = 'application/zip'
                    else:
                        payload = cache.export_word(title, sections,
                                                    table_style=table_style)
                        name = f"{title}_{stamp}.docx"
                        mime = ('application/vnd.openxmlformats-officedocument'
                                '.wordprocessingml.document')
                except Exception as error:
                    st.session_state.report_export = None
                    st.error(f"❌ 生成失败：{error}")
                else:
                    st.session_state.report_export = {
                        'data': payload, 'name': name, 'mime': mime,
                        'kind': export_kind, 'count': len(sections)}
                    st.success(f"✅ 已生成（{len(sections)} 个章节，"
                               f"{len(payload) / 1024:.0f} KB），请在右侧下载")
    with col_download:
        # 下载按钮常驻：若只在点击那一次渲染，点下载触发的 rerun 会让按钮消失
        ready = st.session_state.get('report_export')
        if ready:
            st.download_button(
                f"💾 下载 {ready['name'].rsplit('.', 1)[-1].upper()}",
                ready['data'], file_name=ready['name'], mime=ready['mime'],
                use_container_width=True)
    with col_metric:
        st.metric("本次导出章节", len(sections))

    st.caption('Word 报告中的统计表格为**三线表**（顶线与底线加粗、表头下细线、无竖线），'
               '插图为 300 dpi；图片包内含 TIFF（写入 dpi 元数据），供要求分辨率的投稿系统使用。')

    # 方法学段落（自动汇总本次实际处置）
    render_methods_panel(cache)

    # AI 审阅（C）与 AI 写作（D）
    render_report_ai_panel(cache)

    with st.expander(f"📋 查看 / 清理已收集章节（{len(cache.sections)} 个）", expanded=True):
        st.caption('**页面渲染时自动产生的结果也会在这里**（例如描述性分析用默认变量跑出的图表、'
                   '组间比较的默认选择）。写论文时只勾选你真正要报告的分析即可；'
                   '不需要的可以点右侧 🗑️ 直接从缓存移除。')
        for i, sec in enumerate(cache.sections, 1):
            sec_type = "📊 数据" if "总览" in sec['title'] else \
                       "📈 描述" if "分布" in sec['title'] or "直方" in sec['title'] else \
                       "🔬 推断" if "×" in sec['title'] or "检验" in sec['title'] else \
                       "🤖 建模" if "Logistic" in sec['title'] or "Cox" in sec['title'] else \
                       "📚 论文" if "PSM" in sec['title'] or "亚组" in sec['title'] else \
                       "📄 其他"
            col_info, col_remove = st.columns([9, 1])
            with col_info:
                st.markdown(f"{i}. {sec_type} **{sec['title']}**")
            with col_remove:
                if st.button('🗑️', key=f'drop_sec_{i}',
                             help='从报告缓存中移除这个章节'):
                    cache.remove(sec['title'])
                    st.session_state.report_export = None
                    st.rerun()


# ========== 主程序 ==========
def main():
    render_hero()

    # session初始化
    if 'df' not in st.session_state:
        st.session_state.df = None
    if 'cache' not in st.session_state:
        from modules.export import ResultCache

        st.session_state.cache = ResultCache()

    session_defaults = {
        'parsed_df': None,
        'structure_profile': None,
        'missing_result': None,
        'pending_nav': None,
        'nav_choice': NAV_OVERVIEW,
        'report_export': None,
        'schema_action_log': [],
        'schema_batch_results': None,
        'pending_transform_actions': None,
        'schema_auto_closure': False,
        'schema_pending_closure': False,
        'closure_result': None,
        'lin_result': None,
        'analysis_ledger': [],
        'data_history': [],
        'pending_setup': None,
        'setup_notice': None,
        'agent_run': None,
        'type_overrides': {},
        'sheet_names': [],
        'excel_bytes': None,
        'multi_sheet_brief': None,
        'multi_sheet_result': None,
        'multi_apply_request': None,
        'multi_join_reports': None,
        'multi_join_applied': False,
        'import_settings': default_import_settings(),
        'excel_state': None,
        'dataset_source': None,
        'sheet_pick': None,
        'load_error': None,
        'upload_id': None,
    }
    for key, value in session_defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    # 图片样式：按会话设置应用（影响之后新绘制的图）
    apply_session_figure_style()

    # 侧边栏
    with st.sidebar:
        # 品牌区
        st.markdown(f"""
        <div style="text-align: center; padding: 0.5rem 0 1rem;">
            <div style="font-size: 1.8rem;">🩺</div>
            <div style="font-size: 1rem; font-weight: 700; color: {SLATE_800}; margin-top: 0.3rem;">
                医析台 MedBench
            </div>
            <div style="font-size: 0.72rem; color: {SLATE_400}; margin-top: 0.2rem;">
                本地计算 · {APP_VERSION}
            </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("---")

        # 数据上传区
        st.markdown(f"""
        <div style="margin-bottom: 0.5rem; color: {SLATE_700}; font-weight: 600; font-size: 0.9rem;">
            📂 数据源
        </div>
        """, unsafe_allow_html=True)

        uploaded = st.file_uploader("上传 Excel 文件", type=['xlsx', 'xls'],
                                    help="数据全程本地处理，不上传任何服务器",
                                    label_visibility="collapsed")

        if uploaded is not None:
            file_id = f"{uploaded.name}|{getattr(uploaded, 'size', 0)}"
            if st.session_state.get('upload_id') != file_id:
                st.session_state.upload_id = file_id
                st.session_state.import_settings = default_import_settings()
                st.session_state.excel_state = None
                st.session_state.type_overrides = {}
                st.session_state.parsed_df = None
                st.session_state.df = None
                st.session_state.dataset_source = None
                st.session_state.sheet_pick = None
                st.session_state.load_error = None
                try:
                    raw_bytes = uploaded.getvalue()
                    # 留一份原始字节：多表分析要读到全部工作表，而主页面拿不到 uploaded 对象
                    st.session_state.excel_bytes = raw_bytes
                    st.session_state.sheet_names = cached_list_sheets(raw_bytes)
                    st.session_state.load_error = None
                except Exception as error:
                    st.session_state.excel_bytes = None
                    st.session_state.sheet_names = []
                    st.session_state.load_error = f"❌ 读取失败：{error}"

            if st.session_state.get('load_error'):
                st.error(st.session_state.load_error)

            names = st.session_state.get('sheet_names') or []
            if not names and not st.session_state.get('load_error'):
                st.warning("该 Excel 中没有可用的 Sheet")

            if names:
                if st.session_state.get('sheet_pick') not in names:
                    st.session_state.sheet_pick = names[0]
                if len(names) > 1:
                    sheet_name = st.selectbox("选择 Sheet", names, key='sheet_pick')
                else:
                    sheet_name = names[0]
                    st.caption(f"Sheet：{sheet_name}")

                settings = st.session_state.get('import_settings') or default_import_settings()
                with st.expander("⚙️ 导入设置（表头 / 大标题行）",
                                 expanded=st.session_state.df is None):
                    header_rows = st.number_input(
                        "表头行数", min_value=0, max_value=5,
                        value=int(settings.get('header_rows', 1)), step=1,
                        help="1 = 首行就是字段名；2 = 前两行合并成字段名（多级表头）；0 = 无表头")
                    skip_rows = st.number_input(
                        "跳过开头行数", min_value=0, max_value=50,
                        value=int(settings.get('skip_rows', 0)), step=1,
                        help="表格顶部的大标题行数（如「2025年EICU人力资源结构」）")
                    fill_data_merged = st.checkbox(
                        "合并单元格向下填充", value=bool(settings.get('fill_data_merged', False)),
                        help="纵向合并单元格读入后是空白，勾选后沿用上一行的值")
                    if st.checkbox("预览前 8 行", value=False, key='show_preview'):
                        try:
                            preview = cached_read_sheet(
                                uploaded.getvalue(), sheet_name, int(header_rows),
                                int(skip_rows), bool(fill_data_merged)).head(8)
                            render_table(preview, hide_index=True)
                        except Exception as error:
                            st.error(f"❌ 预览失败：{error}")

                    if st.button("🔄 重新解析该 Sheet", use_container_width=True):
                        try:
                            manual_settings = {
                                'header_rows': int(header_rows),
                                'skip_rows': int(skip_rows),
                                'fill_data_merged': bool(fill_data_merged),
                            }
                            st.session_state.import_settings = manual_settings
                            st.session_state.dataset_source = 'Excel'
                            parse_uploaded_sheet(uploaded, sheet_name, manual_settings)
                            st.session_state.excel_state = {
                                'sheet': sheet_name, 'settings': dict(manual_settings)}
                            st.rerun()
                        except Exception as error:
                            st.error(f"❌ 解析失败：{error}")

                new_settings = {
                    'header_rows': int(header_rows),
                    'skip_rows': int(skip_rows),
                    'fill_data_merged': bool(fill_data_merged),
                }
                excel_state = {'sheet': sheet_name, 'settings': new_settings}
                if (st.session_state.get('excel_state') != excel_state
                        and st.session_state.get('dataset_source') != '示例数据'):
                    try:
                        st.session_state.import_settings = new_settings
                        parse_uploaded_sheet(uploaded, sheet_name, new_settings)
                        st.session_state.excel_state = excel_state
                        current = st.session_state.df
                        st.success(
                            f"✅ {uploaded.name} / {sheet_name}\n\n"
                            f"{len(current)} 行 × {len(current.columns)} 列")
                    except Exception as error:
                        st.session_state.excel_state = None
                        st.error(f"❌ 解析失败：{error}")

        # 示例数据
        sample_path = Path(__file__).parent / 'sample_data' / 'demo_clinical.xlsx'
        if sample_path.exists():
            if st.button("🎯 加载示例数据（300例）", use_container_width=True):
                try:
                    df = DataLoader.read_sheet(sample_path)
                    st.session_state.parsed_df = df
                    st.session_state.type_overrides = {}
                    st.session_state.schema_action_log = []
                    load_dataframe_into_session(df, sheet_name='demo', source='示例数据')
                    st.success(f"✅ 示例数据已加载（{len(df)} 行 × {len(df.columns)} 列）")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 示例加载失败: {e}")

        # 项目：保存 / 打开
        from modules.project import load_project

        st.markdown("---")
        st.markdown(f"""
        <div style="margin-bottom: 0.5rem; color: {SLATE_700}; font-weight: 600; font-size: 0.9rem;">
            💾 项目
        </div>
        """, unsafe_allow_html=True)

        notice = st.session_state.pop('project_loaded_notice', None)
        if notice:
            st.success(notice)

        if st.session_state.get('df') is not None:
            if st.button("💾 保存项目（.mda）", use_container_width=True,
                         help='把当前数据、变量类型指定与报告章节打包，'
                              '便于以后继续分析，或发给同事复核'):
                try:
                    payload = build_project_bytes()
                except Exception as error:
                    st.error(f"❌ 保存失败：{error}")
                else:
                    st.session_state.project_file = payload
                    st.session_state.project_name = (
                        f"分析项目_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.mda")
                    st.success(f"已生成（{len(payload) / 1024 / 1024:.1f} MB），点下方下载")
            if st.session_state.get('project_file'):
                st.download_button(
                    "⬇️ 下载项目文件", st.session_state.project_file,
                    file_name=st.session_state.get('project_name') or '分析项目.mda',
                    mime='application/zip', use_container_width=True)
        else:
            st.caption('上传数据后可保存为项目文件；也可直接打开已有项目。')

        opened = st.file_uploader(
            "打开项目（.mda）", type=['mda', 'zip'], key='project_uploader',
            label_visibility="collapsed",
            help='载入后恢复数据、变量类型指定、报告章节与 AI 解读')
        if opened is not None:
            signature = f"{opened.name}|{getattr(opened, 'size', 0)}"
            if st.session_state.get('project_upload_id') != signature:
                st.session_state.project_upload_id = signature
                try:
                    project_state = load_project(opened.getvalue())
                except Exception as error:
                    st.error(f"❌ 打开项目失败：{error}")
                else:
                    restore_project_state(project_state)

        # 数据清洗
        if st.session_state.df is not None:
            st.markdown("---")
            st.markdown(f"""
            <div style="margin-bottom: 0.5rem; color: {SLATE_700}; font-weight: 600; font-size: 0.9rem;">
                🧹 数据清洗
            </div>
            """, unsafe_allow_html=True)

            st.markdown('<div style="font-size: 0.82rem; color: #475569; margin-bottom: 0.3rem;">异常值处理策略</div>', unsafe_allow_html=True)
            outlier_strategy = st.selectbox(
                "异常值处理",
                ['keep（保留）', 'winsorize（缩尾）', 'drop（删除）'],
                label_visibility="collapsed")
            if st.button("应用清洗", use_container_width=True):
                strategy = outlier_strategy.split('（')[0]
                rows_before = len(st.session_state.df)
                cleaned = DataLoader.clean_data(st.session_state.df, outlier_strategy=strategy)
                # 记录处置供「方法学段落」使用：论文必须交代异常值如何处理
                st.session_state.cleaning_record = {
                    'strategy': strategy,
                    'rows_before': int(rows_before),
                    'rows_after': int(len(cleaned)),
                }
                # 走统一加载路径：它会清理指向旧列的会话状态、重建质量报告，
                # 并记入撤销历史。此前是直接写 st.session_state.df —— 既绕过了清理，
                # 也让清洗成为唯一没有撤销点的数据变更。
                push_data_snapshot(f'数据清洗（{strategy}）')
                load_dataframe_into_session(cleaned, sheet_name=current_sheet_label(),
                                            source=f'清洗（{strategy}）')
                st.success(f"✅ 清洗完成，当前 {len(cleaned)} 行"
                           "（可用「↩️ 回到上一步」撤销）")
                st.rerun()

            # 数据集概要
            st.markdown("---")
            st.markdown(f"""
            <div style="margin-bottom: 0.5rem; color: {SLATE_700}; font-weight: 600; font-size: 0.9rem;">
                📊 当前数据集
            </div>
            """, unsafe_allow_html=True)
            report = st.session_state.quality_report
            st.markdown(f"""
            <div style="background: white; padding: 0.8rem; border-radius: 8px; border: 1px solid {SLATE_200};">
                <div style="display: flex; justify-content: space-between; padding: 0.3rem 0; font-size: 0.85rem;">
                    <span style="color: {SLATE_500};">样本数</span>
                    <span style="font-weight: 600; color: {SLATE_800};">{report['n_rows']}</span>
                </div>
                <div style="display: flex; justify-content: space-between; padding: 0.3rem 0; font-size: 0.85rem;">
                    <span style="color: {SLATE_500};">变量数</span>
                    <span style="font-weight: 600; color: {SLATE_800};">{report['n_cols']}</span>
                </div>
                <div style="display: flex; justify-content: space-between; padding: 0.3rem 0; font-size: 0.85rem;">
                    <span style="color: {SLATE_500};">缺失率</span>
                    <span style="font-weight: 600; color: {SLATE_800};">{report['missing_pct']}%</span>
                </div>
                <div style="display: flex; justify-content: space-between; padding: 0.3rem 0; font-size: 0.85rem;">
                    <span style="color: {SLATE_500};">当前 Sheet</span>
                    <span style="font-weight: 600; color: {PRIMARY};">{st.session_state.get('current_sheet', '-')}</span>
                </div>
            </div>
            """, unsafe_allow_html=True)

        # AI 助手（可选）
        render_ai_config_sidebar()

        # 底部信息
        st.markdown("---")
        privacy_text = ('⚙️ 本地计算 + AI 解读已启用（结构摘要会发送至所配服务）'
                        if ai_is_enabled() else '🛡️ 完全本地处理，数据零外传')
        st.markdown(
            '<div style="text-align: center; color: #64748B; font-size: 0.78rem; '
            'padding: 0.8rem 0 0.6rem;">'
            f'{privacy_text}</div>',
            unsafe_allow_html=True
        )
    # 主区域
    if st.session_state.df is None:
        render_hero_empty()
        return

    st.markdown("---")

    # 主导航（总览与洞察页的推荐按钮通过 pending_nav 跳转）
    pending_nav = st.session_state.get('pending_nav')
    if pending_nav:
        st.session_state.nav_choice = pending_nav
        st.session_state.pending_nav = None
    # AI 建议的一键配置：必须在这里应用（页面控件实例化之前），否则 Streamlit 会拒绝
    apply_pending_setup()
    # 批量整理里若同时选了「读取设置」类，会在重新解析完成后挂起「数据变换」类，
    # 由这里接着执行 —— 它替换当前数据，必须跑在页面渲染之前，且不受当前页签影响
    # （原先只在首页执行，用户若停在别的页签，挂起的变换就一直不生效）
    apply_pending_transforms()
    if st.session_state.get('nav_choice') not in NAV_PAGES:
        st.session_state.nav_choice = NAV_OVERVIEW
    render_setup_notice()

    nav = st.radio("分析导航", NAV_PAGES, horizontal=True,
                   label_visibility="collapsed", key='nav_choice')

    if nav == NAV_OVERVIEW:
        render_overview_and_insights()
    elif nav == NAV_PREP:
        render_data_prep()
    elif nav == '⏱️ 时间与质控':
        render_time_quality()
    elif nav == '📈 描述性分析':
        render_descriptive()
    elif nav == '🔬 统计推断':
        render_inferential()
    elif nav == '🤖 预测建模':
        render_modeling()
    elif nav == '📚 论文辅助':
        render_research()
    elif nav == '📄 报告导出':
        render_export()

    # 分析成功后会自动入报告缓存，这里把它同步进台账（按指纹去重，只增不减）
    sync_ledger_from_sections()


if __name__ == '__main__':
    main()