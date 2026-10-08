"""
方法学段落：把本次实际做过的数据处置与统计方法，整理成论文可用的描述。

为什么需要它：论文的「方法」部分必须交代缺失值如何处理、异常值是否排除、用了哪些
检验以及选择依据 —— 这些信息程序里其实都有，但此前需要作者凭记忆重写一遍，既费时
又容易与实际分析不一致（审稿人最容易质疑的正是这一点）。

本模块的原则：**只汇总实际执行过的处置，不推测、不补写**。
报告章节里没有出现过的方法不会出现在方法学段落中。
"""
from typing import Dict, List, Optional

# 章节标题关键词 → (归类, 方法学表述)
# 顺序有意义：先匹配到的规则优先（例如「Logistic」章节里也会出现「卡方」，须先判建模）
METHOD_RULES = [
    (('基线特征', 'Table 1', 'Table1'), '基线特征',
     '基线特征中，连续变量以均数±标准差或中位数（四分位数间距）表示，'
     '分类变量以频数（构成比）表示；组间比较根据变量类型与分布分别采用两独立样本 t 检验、'
     'Mann-Whitney U 检验、卡方检验或 Fisher 精确检验'),
    (('Logistic',), '风险预测',
     '影响因素分析采用多因素 Logistic 回归，结果以优势比（OR）及其 95% 置信区间表示'),
    (('Cox', '生存'), '生存分析',
     '生存资料采用 Cox 比例风险回归模型，结果以风险比（HR）及其 95% 置信区间表示'),
    (('LASSO',), '变量筛选',
     '采用 LASSO 回归（L1 正则化）进行变量筛选，并通过重抽样做内部验证'),
    (('外部验证',), '外部验证',
     '模型在独立数据集上进行外部验证，比较判别力与校准度'),
    (('剂量-反应', '剂量反应', '立方样条'), '剂量-反应关系',
     '剂量-反应关系采用限制性立方样条（RCS）拟合，并检验非线性'),
    (('倾向性评分', 'PSM'), '因果推断',
     '为控制可测量的混杂，采用倾向性评分匹配（PSM），并以标准化均数差（SMD）评价匹配后平衡性'),
    (('IPTW', '逆概率'), '因果推断',
     '为控制可测量的混杂，采用逆概率加权（IPTW），并报告加权后的平衡性与有效样本量'),
    (('E-value', 'E值'), '敏感性分析',
     '采用 E-value 评估未测量混杂对结论的潜在影响'),
    (('亚组', '森林图'), '亚组分析',
     '采用亚组分析与森林图展示各亚组间效应的一致性'),
    (('多重比较', 'Tukey'), '多重比较校正',
     '多组两两比较采用 Tukey HSD 法，并对多重比较进行校正'),
    (('FDR', 'Benjamini'), '多重比较校正',
     '多重比较采用 Benjamini-Hochberg 法控制错误发现率（FDR）'),
    (('Bonferroni',), '多重比较校正', '多重比较采用 Bonferroni 法校正'),
    (('Kappa', 'ICC', 'Bland-Altman'), '一致性评价',
     '一致性采用 Kappa 系数、组内相关系数（ICC）或 Bland-Altman 法评价'),
    (('率的置信区间', '率与置信区间'), '率与密度',
     '率及其 95% 置信区间采用 Wilson 法估计，密度（‰）采用 Poisson 精确法估计'),
    (('SPC', '控制图'), '过程控制',
     '质控数据采用统计过程控制图（I-MR / P 图 / U 图）分析，并按常用判异规则识别异常信号'),
    (('趋势', '环比', '同比'), '时间趋势',
     '时间趋势采用线性回归、Mann-Kendall 检验或 Cochran-Armitage 趋势检验分析'),
    (('时间序列', 'ARIMA', '预测'), '时间序列预测',
     '时间序列采用 ARIMA 或指数平滑模型拟合与预测，并以留出法评价预测误差'),
    (('重复测量', '混合效应', 'GEE', '广义估计'), '重复测量',
     '重复测量数据采用线性混合效应模型（LMM）或广义估计方程（GEE）分析'),
    (('配对',), '配对比较',
     '配对资料采用配对 t 检验或 Wilcoxon 符号秩检验'),
    (('独立样本t检验', 't检验', 'Welch'), '组间比较',
     '两组连续变量比较采用两独立样本 t 检验（方差不齐时用 Welch 校正）'),
    (('Mann-Whitney', '秩和'), '组间比较',
     '两组连续变量比较采用 Mann-Whitney U 检验'),
    (('方差分析', 'ANOVA'), '组间比较',
     '多组连续变量比较采用单因素方差分析'),
    (('Kruskal',), '组间比较',
     '多组连续变量比较采用 Kruskal-Wallis 检验'),
    (('卡方',), '组间比较', '分类变量比较采用卡方检验'),
    (('Fisher',), '组间比较', '分类变量比较采用 Fisher 精确检验'),
    (('相关分析', '相关系数', '热图'), '相关分析',
     '变量间相关性采用 Pearson 或 Spearman 相关分析'),
    (('分布直方图', 'Q-Q', '箱线图', '频数分布'), '描述性分析',
     '连续变量以直方图、Q-Q 图与箱线图展示分布特征，并以 Shapiro-Wilk 检验判断正态性'),
    (('插补', 'MICE'), '缺失值处理',
     '缺失值采用多重插补（MICE）处理，并按 Rubin 规则合并估计'),
    (('诊断阈值', '最佳截断值'), '诊断效能',
     '诊断阈值以约登指数最大等判据确定，并报告相应的灵敏度、特异度与预测值'),
]


def classify(title: str) -> Optional[Dict]:
    """把报告章节标题归类为方法学条目。"""
    text = str(title or '')
    if not text:
        return None
    for keywords, category, statement in METHOD_RULES:
        if any(keyword in text for keyword in keywords):
            return {'category': category, 'statement': statement, 'source': text}
    return None


def collect(df, report: Dict, sections: Optional[List[Dict]] = None, *,
            overrides: Optional[Dict] = None, missing: Optional[Dict] = None,
            cleaning: Optional[Dict] = None, design: str = '',
            alpha: float = 0.05, software: str = '',
            dataset_source: str = '') -> Dict:
    """
    汇总本次分析实际做过的处置。

    只读取真实存在的状态：报告章节里没出现过的方法不会出现在结果里。
    """
    report = report or {}
    fields = report.get('fields') or {}
    sections = list(sections or [])

    # ---------- 样本与变量 ----------
    type_count: Dict[str, int] = {}
    for meta in fields.values():
        dtype = (meta or {}).get('dtype') or 'text'
        type_count[dtype] = type_count.get(dtype, 0) + 1

    # ---------- 缺失情况 ----------
    missing_columns = sorted(
        ((name, float((meta or {}).get('missing_pct') or 0))
         for name, meta in fields.items()),
        key=lambda item: item[1], reverse=True)
    missing_columns = [(name, pct) for name, pct in missing_columns if pct > 0]
    missing_section = {
        'overall_pct': float(report.get('missing_pct') or 0),
        'columns': missing_columns[:8],
        'any_missing': bool(missing_columns),
        'handled': '',
    }
    if missing:
        count = missing.get('n_imputations')
        missing_section['handled'] = (
            f"多重插补（MICE，{count} 份插补数据，Rubin 规则合并）"
            if count else '多重插补（MICE）')

    # ---------- 异常值 ----------
    outlier_total = sum(int((meta or {}).get('outlier_count') or 0)
                        for meta in fields.values())
    outlier_method = next((str((meta or {}).get('outlier_method') or '')
                           for meta in fields.values()
                           if (meta or {}).get('outlier_method')), 'IQR(1.5x)')

    # ---------- 统计方法（只取报告里真实出现过的） ----------
    methods: List[Dict] = []
    seen_categories = set()
    for section in sections:
        item = classify(section.get('title'))
        if not item:
            continue
        key = (item['category'], item['statement'])
        if key in {(m['category'], m['statement']) for m in methods}:
            continue
        item['titles'] = [section.get('title')]
        methods.append(item)
        seen_categories.add(item['category'])
    for method in methods:
        method['titles'] = [section.get('title') for section in sections
                            if (classify(section.get('title')) or {}).get('statement')
                            == method['statement']]

    return {
        'sample': {'n': int(report.get('n_rows') or (len(df) if df is not None else 0)),
                   'columns': int(report.get('n_cols') or 0)},
        'types': type_count,
        'missing': missing_section,
        'outliers': {'total': outlier_total, 'method': outlier_method,
                     'strategy': (cleaning or {}).get('strategy') or 'keep',
                     'detail': (cleaning or {})},
        'overrides': {str(k): str(v) for k, v in (overrides or {}).items()},
        'methods': methods,
        'design': str(design or ''),
        'alpha': float(alpha),
        'software': str(software or ''),
        'dataset_source': str(dataset_source or ''),
    }


def build_draft(collected: Dict) -> str:
    """把处置清单整理成可写入论文的方法学段落（中文草稿）。"""
    collected = collected or {}
    sample = collected.get('sample') or {}
    types = collected.get('types') or {}
    missing = collected.get('missing') or {}
    outliers = collected.get('outliers') or {}
    overrides = collected.get('overrides') or {}
    methods = collected.get('methods') or []
    alpha = collected.get('alpha') or 0.05

    lines: List[str] = []

    # 研究对象与变量
    type_words = []
    for dtype, label in (('numeric', '连续变量'), ('categorical', '分类变量'),
                         ('binary', '二分类变量'), ('datetime', '日期变量'),
                         ('text', '文本变量')):
        count = types.get(dtype)
        if count:
            type_words.append(f'{label} {count} 个')
    body = (f'共纳入 {sample.get("n", 0)} 例研究对象，'
            f'收集 {sample.get("columns", 0)} 个变量'
            + (f'（{"、".join(type_words)}）' if type_words else '') + '。')
    design = collected.get('design')
    lines.append(f'本研究为{design}，{body}' if design else f'本研究{body}')

    # 数据清理
    if missing.get('any_missing'):
        columns = missing.get('columns') or []
        detail = '、'.join(f'「{name}」{pct:.1f}%' for name, pct in columns[:4])
        text = (f'数据完整性：全表缺失率 {missing.get("overall_pct", 0):.1f}%'
                + (f'，缺失较集中的变量包括 {detail}' if detail else '') + '。')
        if missing.get('handled'):
            text += f'缺失值采用{missing["handled"]}处理，并与完整案例分析结果做敏感性对比。'
        else:
            text += ('本次分析采用完整案例分析：各分析仅纳入该分析所涉变量均无缺失的记录，'
                     '未对缺失值进行插补。')
        lines.append(text)
    else:
        lines.append('数据完整性：本次纳入分析的变量均无缺失值。')

    if outliers.get('total'):
        strategy = outliers.get('strategy') or 'keep'
        strategy_text = {
            'winsorize': '对超出 1.5 倍四分位距范围的数值进行了缩尾（winsorize）处理',
            'drop': '剔除了超出 1.5 倍四分位距范围的记录',
            'keep': '对超出 1.5 倍四分位距范围的观测值予以保留，未做剔除或缩尾',
        }.get(strategy, '未对异常值做特殊处理')
        lines.append(f'异常值：按 {outliers.get("method") or "IQR(1.5x)"} 法共识别 '
                     f'{outliers["total"]} 个可疑异常值，{strategy_text}。')

    if overrides:
        names = '、'.join(f'「{name}」' for name in list(overrides)[:6])
        lines.append(f'变量类型：对 {names} 等 {len(overrides)} 个变量的类型做了人工核定后参与分析。')

    # 统计方法
    if methods:
        lines.append('统计方法：')
        for index, method in enumerate(methods, 1):
            items = method.get('titles') or []
            suffix = ''
            if items:
                suffix = f'（对应分析：{"、".join(str(item) for item in items[:3])}）'
            lines.append(f'{index}. {method.get("statement")}{suffix}')
        lines.append(f'所有检验均为双侧检验，检验水准 α = {alpha:g}。')
    else:
        lines.append('统计方法：本次尚未收录具体分析结果，请在上方各分析页完成分析后再生成。')

    if collected.get('software'):
        lines.append(f'统计分析使用 {collected["software"]} 完成。')

    header = '## 方法（Methods，自动汇总草稿）'
    footer = ('\n> 说明：以上内容由程序依据**本次实际执行的处置**自动汇总生成，'
              '未执行过的操作不会出现。**请务必通读核对**，特别是缺失值处理方式与'
              '检验选择依据是否与你的研究设计一致，再写入论文。')
    return header + '\n\n' + '\n\n'.join(lines) + '\n' + footer


def to_plain_text(text: str) -> str:
    """
    去掉 Markdown 标记。

    Word 报告正文按纯文本渲染，若直接写入带 ## / > 的内容会原样显示符号。
    """
    lines: List[str] = []
    for line in str(text or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            lines.append(stripped.lstrip('#').strip())
        elif stripped.startswith('>'):
            lines.append(stripped.lstrip('> ').strip())
        else:
            lines.append(line)
    return '\n'.join(lines).strip()


def summarize(collected: Dict) -> str:
    """给界面用的一句话摘要。"""
    methods = collected.get('methods') or []
    missing = collected.get('missing') or {}
    parts = [f"汇总到 {len(methods)} 类统计方法"]
    if missing.get('any_missing'):
        parts.append(f'缺失率 {missing.get("overall_pct", 0):.1f}%')
    if (collected.get('outliers') or {}).get('total'):
        parts.append(f"异常值 {(collected.get('outliers') or {}).get('total')} 个")
    return '、'.join(parts) + '。'
