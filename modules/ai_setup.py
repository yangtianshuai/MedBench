"""
AI 分析建议的「一键配置」映射

把 AI 的结构化建议（方法名 + 变量）翻译成程序里的实际配置：目标页签、
要切换的子面板、以及需要预填的控件与变量。

**边界很明确：只预填参数，不代替用户点运行。**
统计方法的责任在研究者 —— AI 只看得到结构摘要，不知道某个字段在这份数据里
究竟是结局、混杂还是暴露。所以这里做的是「把方案摆好」，执行与否由用户决定。

变量来源有两级：
  1. AI 在 variables 里给出的变量（必须真实存在且类型合适才采用）
  2. AI 没给或给的不可用时，按角色做启发式兜底（如分组变量优先选 2–5 类的分类变量）

两种来源都会在界面上写明，让用户知道哪些是 AI 指定的、哪些是程序猜的。
"""
from typing import Dict, List, Optional, Tuple

# 允许的变量角色
ROLE_LABELS = {
    'outcome': '结局变量',
    'outcome_binary': '二分类结局',
    'grouping': '分组变量',
    'exposure': '暴露变量',
    'predictors': '自变量',
    'time': '时间/生存时间',
    'event': '事件',
    'subject': '个体标识',
    'metric': '指标列',
}

# 每个方法对应页面上要配置的控件：
#   (控件 key, 变量角色, 是否必需, 界面上的标签)
SETUP_RULES = (
    {
        'id': 'table1',
        'keywords': ('基线特征', 'table 1', 'table1', '表 1', '表1', '基线表', 'smd'),
        'page': '📚 论文辅助',
        'panel': ('paper_func', '基线特征表（Table 1）'),
        'fields': [('t1_group', 'grouping', True, '分组变量')],
        'hint': '确认分组变量后点「生成 Table 1」',
    },
    {
        'id': 'compare_numeric',
        'keywords': ('两组比较', '组间比较', '数值变量 × 分组', 't 检验', 't检验',
                     'welch', 'mann-whitney', 'mann whitney', '秩和检验'),
        'page': '🔬 统计推断',
        'panel': ('test_type', '数值变量 × 分组'),
        'priority': 20,
        'fields': [('num_var', 'outcome', True, '数值变量（结局）'),
                   ('grp_var', 'grouping', True, '分组变量')],
        'hint': '确认变量与检验方法（正态时 t 检验、偏态时 Mann-Whitney）后点运行',
    },
    {
        'id': 'compare_categorical',
        'keywords': ('分类变量 × 分组', '卡方', 'fisher', '确切概率', '率比较'),
        'page': '🔬 统计推断',
        'panel': ('test_type', '分类变量 × 分组'),
        'fields': [('cat_outcome', 'outcome_binary', True, '分类变量（结局）'),
                   ('cat_grp', 'grouping', True, '分组变量')],
        'hint': '确认变量后点运行；期望频数偏小时程序会自动改用 Fisher 精确检验',
    },
    {
        'id': 'paired',
        'keywords': ('配对', 'wilcoxon', '符号秩', '前后对比'),
        'page': '🔬 统计推断',
        'panel': ('test_type', '配对样本'),
        'priority': 10,
        'fields': [('before', 'predictors', True, '前 / 左'),
                   ('after', 'predictors', True, '后 / 右')],
        'hint': '确认配对的两个变量后点运行',
    },
    {
        'id': 'multicomp',
        'keywords': ('多重比较', 'fdr', 'bonferroni', 'holm', 'šidák', 'sidak',
                     'tukey', '假阳性', '多重检验'),
        'page': '🔬 统计推断',
        'panel': ('test_type', '多重比较校正（FDR / Tukey HSD）'),
        'fields': [],
        'hint': '若已有一组 p 值就选「已有一组 p 值（做校正）」；多组两两比较则选 Tukey HSD',
    },
    {
        'id': 'correlation',
        'keywords': ('相关分析', 'pearson', 'spearman', '相关'),
        # 相关分析已从「描述性分析」移到「统计推断」（它输出 r 与 p，属于推断），
        # 因此现在是一个可自动切换的 radio 面板，不再需要 tabs_hint
        'page': '🔬 统计推断',
        'panel': ('test_type', '相关分析'),
        'fields': [('cor_x', 'predictors', False, 'X 变量'),
                   ('cor_y', 'predictors', False, 'Y 变量')],
        'hint': '确认 X / Y 变量后即可看到 r、R² 与 p 值',
    },
    {
        'id': 'descriptive',
        'keywords': ('描述统计', '描述性', '正态性', 'shapiro', '频数分布'),
        'page': '📈 描述性分析',
        'panel': None,
        'tabs_hint': '数值变量',
        'fields': [('desc_num', 'outcome', False, '数值变量')],
        'hint': '该页为标签页结构，无法自动切换：请点开对应标签查看',
    },
    {
        'id': 'linear',
        'keywords': ('线性回归', '多重线性回归', 'ols', '连续结局', '多元线性'),
        'page': '🤖 预测建模',
        'panel': ('model_type', '多重线性回归（连续结局）'),
        'fields': [('lin_out', 'outcome', True, '结局变量（连续）'),
                   ('lin_pred', 'predictors', True, '自变量')],
        'hint': '确认结局与自变量后点「运行线性回归」；把分组变量也选进自变量即为 ANCOVA',
    },
    {
        'id': 'logistic',
        'keywords': ('logistic', '逻辑回归', 'logit', 'or 值', '比值比'),
        'page': '🤖 预测建模',
        'panel': ('model_type', 'Logistic 回归'),
        'fields': [('lr_out', 'outcome_binary', True, '结局变量（二分类）'),
                   ('lr_pred', 'predictors', False, '自变量')],
        'hint': '确认结局与自变量后点运行',
    },
    {
        'id': 'cox',
        'keywords': ('cox', '生存分析', '生存回归', '风险比', 'hr 值', 'km 曲线'),
        'page': '🤖 预测建模',
        'panel': ('model_type', 'Cox 生存回归'),
        'fields': [('cox_time', 'time', True, '生存时间列'),
                   ('cox_event', 'event', True, '事件列'),
                   ('cox_covars', 'predictors', False, '协变量')],
        'hint': '确认生存时间、事件与协变量后点运行',
    },
    {
        'id': 'lasso',
        'keywords': ('lasso', '变量筛选', '正则化', '内部验证', 'bootstrap'),
        'page': '🤖 预测建模',
        'panel': ('model_type', 'LASSO 筛选 + Bootstrap 内部验证'),
        'fields': [('lasso_out', 'outcome_binary', False, '结局变量'),
                   ('lasso_pred', 'predictors', False, '候选自变量')],
        'hint': '确认结局与候选自变量后点运行',
    },
    {
        'id': 'dose_response',
        'keywords': ('剂量-反应', '剂量反应', '立方样条', 'spline', '非线性关系'),
        'page': '🤖 预测建模',
        'panel': ('model_type', '剂量-反应（限制性立方样条）'),
        'fields': [('dose_exposure', 'exposure', False, '连续暴露变量'),
                   ('dose_outcome', 'outcome', False, '结局变量')],
        'hint': '确认暴露与结局后点运行',
    },
    {
        'id': 'external_validation',
        'keywords': ('外部验证', '独立数据集', '验证集'),
        'page': '🤖 预测建模',
        'panel': ('model_type', '外部验证（第二个数据集）'),
        'fields': [('ev_outcome', 'outcome_binary', True, '结局变量（二分类）'),
                   ('ev_predictors', 'predictors', False, '预测变量')],
        'hint': '配置好模型后，上传验证集 Excel 再点「运行外部验证」',
    },
    {
        'id': 'psm',
        'keywords': ('倾向性评分', 'psm', '倾向评分', '匹配'),
        'page': '📚 论文辅助',
        'panel': ('paper_func', '倾向性评分匹配（PSM）'),
        'fields': [('psm_treat', 'exposure', True, '处理变量'),
                   ('psm_outcome', 'outcome_binary', True, '结局变量')],
        'hint': '确认处理变量、结局与协变量后点运行',
    },
    {
        'id': 'iptw',
        'keywords': ('iptw', '逆概率', '加权'),
        'page': '📚 论文辅助',
        'panel': ('paper_func', 'IPTW 逆概率加权'),
        'fields': [],
        'hint': '选择处理变量与结局后点运行',
    },
    {
        'id': 'subgroup',
        'keywords': ('亚组', '森林图', '交互作用', 'effect modification'),
        'page': '📚 论文辅助',
        'panel': ('paper_func', '亚组分析森林图'),
        'fields': [('sub_treat', 'exposure', True, '处理变量'),
                   ('sub_out', 'outcome_binary', True, '结局变量')],
        'hint': '选定处理变量、结局与亚组变量后点运行',
    },
    {
        'id': 'evalue',
        'keywords': ('e-value', 'e 值', 'evalue', '敏感性分析'),
        'page': '📚 论文辅助',
        'panel': ('paper_func', '敏感性分析（E-value）'),
        'fields': [],
        'hint': '填入效应量与置信区间后计算',
    },
    {
        'id': 'longitudinal',
        'keywords': ('lmm', 'gee', '混合效应', '重复测量', '广义估计', '纵向'),
        # 这两个方法原先在「时间与质控」的标签页里，现已移入「预测建模」的模型清单，
        # 因此从「需手动点开标签」变成可自动切换
        'page': '🤖 预测建模',
        'panel': ('model_type', '重复测量（LMM / GEE）'),
        'fields': [('lg_subject', 'subject', True, '个体标识列'),
                   ('lg_time', 'time', True, '时间变量'),
                   ('lg_outcome', 'outcome', True, '结局变量'),
                   ('lg_covars', 'predictors', False, '协变量')],
        'hint': '确认个体标识列、时间变量与结局变量后点「运行重复测量分析」',
    },
    {
        'id': 'timeseries',
        'keywords': ('时间序列', 'arima', 'holt', '指数平滑', '预测趋势'),
        'page': '🤖 预测建模',
        'panel': ('model_type', '时间序列预测'),
        'fields': [('ts_value', 'metric', True, '要预测的指标列')],
        'hint': '确认要预测的指标列与预测期数后点「运行预测」',
    },
    {
        'id': 'spc',
        'keywords': ('控制图', 'spc', 'i-mr', 'p 图', 'u 图', '判异'),
        'page': '⏱️ 时间与质控',
        'panel': None,
        'tabs_hint': 'SPC 控制图',
        'fields': [('tq_spc_value', 'metric', False, '监测指标（数值列）')],
        'hint': '该页为标签页结构，无法自动切换：请点开「SPC 控制图」标签',
    },
    {
        'id': 'rate',
        'keywords': ('率的置信区间', '率与置信区间', '发生率的置信区间', '感染密度', '率的 95'),
        'page': '⏱️ 时间与质控',
        'panel': None,
        'tabs_hint': '率与置信区间',
        'fields': [('tq_rate_num', 'metric', False, '分子列（事件数）'),
                   ('tq_rate_den', 'metric', False, '分母列')],
        'hint': '该页为标签页结构，无法自动切换：请点开「率与置信区间」标签',
    },
    {
        'id': 'trend',
        'keywords': ('趋势检验', 'mann-kendall', 'cochran-armitage', '环比', '同比', '时间趋势'),
        'page': '⏱️ 时间与质控',
        'panel': None,
        'tabs_hint': '趋势与环比同比',
        'fields': [('tq_trend_value', 'metric', False, '指标列（数值）')],
        'hint': '该页为标签页结构，无法自动切换：请点开「趋势与环比同比」标签',
    },
    {
        'id': 'agreement',
        'keywords': ('kappa', 'icc', 'bland-altman', '一致性', '组内相关'),
        'page': '🔬 统计推断',
        'panel': ('test_type', '一致性分析（Kappa / ICC / Bland-Altman）'),
        'fields': [],
        'hint': '选择两位评估者（或两次测量）的变量后点运行',
    },
)


def match_rule(method_name) -> Optional[Dict]:
    """
    按方法名匹配一条配置规则。

    用关键词包含匹配而不是精确相等：AI 的措辞（「卡方检验」「卡方」）与
    页面标签（「分类变量 × 分组」）几乎不可能完全一致。

    命中数相同时看 priority（数字小的优先）：像「配对 t 检验」会同时命中
    「配对」（paired）与「t 检验」（compare_numeric），必须让更具体的规则胜出。
    """
    lowered = str(method_name or '').lower()
    if not lowered:
        return None
    best = None
    best_score = None
    for order, rule in enumerate(SETUP_RULES):
        hit = sum(1 for keyword in rule['keywords'] if keyword in lowered)
        if not hit:
            continue
        score = (hit, -int(rule.get('priority') or 50), -order)
        if best_score is None or score > best_score:
            best, best_score = rule, score
    return best


def _role_ok(role: str, name: str, fields_meta: Dict) -> Tuple[bool, str]:
    """变量类型是否适合该角色。"""
    meta = fields_meta.get(name)
    if meta is None:
        return False, '字段不在当前数据中'
    dtype = meta.get('dtype') if isinstance(meta, dict) else getattr(meta, 'dtype', None)
    unique = meta.get('n_unique') if isinstance(meta, dict) else None
    if role in ('outcome',):
        return (dtype == 'numeric', '需要数值型字段')
    if role in ('outcome_binary', 'event'):
        return (dtype == 'binary', '需要二分类字段（0/1）')
    if role == 'grouping':
        if dtype not in ('categorical', 'binary'):
            return False, '需要分类字段'
        return True, ''
    if role == 'time':
        return (dtype == 'numeric', '需要数值型（时长/时间点）')
    if role == 'subject':
        return (True, '')
    if role == 'exposure':
        return (dtype in ('numeric', 'categorical', 'binary'), '需要数值或分类字段')
    if role in ('predictors', 'metric'):
        return (dtype is not None, '')
    return (True, '')


def _heuristic(role: str, fields_meta: Dict, columns: List[str],
               exclude: Optional[set] = None) -> Optional[str]:
    """
    AI 没给变量时的兜底选择。

    选择标准尽量贴合常规做法：分组变量优先 2–5 类的分类变量（组数太多不便比较），
    结局优先数值型（连续结局信息量最大），时间变量优先列名含时间语义的数值列。
    """
    exclude = exclude or set()
    candidates = [name for name in columns if name not in exclude]
    scored: List[Tuple[int, str]] = []
    for name in candidates:
        meta = fields_meta.get(name) or {}
        dtype = meta.get('dtype') if isinstance(meta, dict) else None
        unique = meta.get('n_unique') if isinstance(meta, dict) else None
        unique = int(unique) if isinstance(unique, (int, float)) else 0
        score = 0
        lowered = str(name).lower()
        if role == 'grouping':
            if dtype in ('categorical', 'binary'):
                score = 6
                if 2 <= unique <= 5:
                    score += 4
                elif unique > 10:
                    score -= 6
            else:
                score = -5
        elif role == 'outcome':
            score = 6 if dtype == 'numeric' else (-4 if dtype == 'binary' else -8)
        elif role == 'outcome_binary':
            score = 6 if dtype == 'binary' else -4
        elif role == 'time':
            if dtype == 'numeric':
                score = 3
                if any(hint in name for hint in
                       ('时间', '时长', '天数', '月', '年', '随访', 'time', 'days')):
                    score += 5
        elif role == 'event':
            score = 6 if dtype == 'binary' else -4
            if any(hint in name for hint in ('事件', '死亡', '结局', 'event', 'died')):
                score += 3
        elif role == 'subject':
            if unique >= max(2, len(candidates) // 3):
                score = 4
            if any(hint in lowered for hint in ('id', '编号', '序号')):
                score -= 3          # 纯序号列不是个体标识，容易误选
        elif role == 'exposure':
            score = 5 if dtype in ('binary', 'categorical') else 3
        elif role in ('predictors', 'metric'):
            score = 4 if dtype == 'numeric' else 1
        if score > 0:
            scored.append((score, name))
    if not scored:
        return None
    scored.sort(key=lambda pair: -pair[0])
    return scored[0][1]


def resolve_variables(rule: Dict, variables: Optional[Dict],
                      fields_meta: Dict, columns: List[str]) -> Dict:
    """
    为一条规则解析出要预填的参数。

    返回 {'params': {...}, 'from_ai': [...], 'guessed': [...], 'skipped': [...]}
    """
    variables = variables or {}
    columns = list(columns or [])
    fields = rule.get('fields') or []
    params: Dict = {}
    from_ai: List[str] = []
    guessed: List[str] = []
    skipped: List[Tuple[str, str]] = []
    used: set = set()

    # ---- 第一轮：只认 AI 明确给出的变量 ----
    # 必须先把它们占位。否则启发式可能先挑走同一个字段（例如把 AI 指定的结局
    # 当成时间变量用掉），导致 AI 的指定反而被挤掉、变成「已被占用」。
    pending: List[Tuple[str, str, bool, str]] = []
    for key, role, required, label in fields:
        if role == 'predictors':
            value = variables.get('predictors')
            names = [name for name in (value if isinstance(value, list) else [value])
                     if name in columns and name not in used]
            if names:
                params[key] = names
                from_ai.append(f'{label}：{"、".join(names)}')
                used.update(names)
            else:
                pending.append((key, role, required, label))
            continue

        value = variables.get(role)
        if isinstance(value, list):
            value = value[0] if value else None
        if value and value in columns and value not in used:
            ok, reason = _role_ok(role, value, fields_meta)
            if ok:
                params[key] = value
                from_ai.append(f'{label}：{value}')
                used.add(value)
                continue
            skipped.append((label, f'AI 给的「{value}」不适合作为{label}（{reason}）'))
        elif value and value in columns:
            skipped.append((label, f'AI 给的「{value}」已被前一个参数占用'))
        pending.append((key, role, required, label))

    # ---- 第二轮：AI 没给或给的不可用时，按角色启发式兜底 ----
    for key, role, required, label in pending:
        guessed_name = _heuristic(role, fields_meta, columns, used)
        if guessed_name:
            params[key] = guessed_name
            guessed.append(f'{label}：{guessed_name}（程序推测，请核对）')
            used.add(guessed_name)
        elif required:
            skipped.append((label, '没有找到合适的字段'))

    return {'params': params, 'from_ai': from_ai, 'guessed': guessed,
            'skipped': skipped}


def plan_setup(method_name, page, variables, fields_meta, columns) -> Optional[Dict]:
    """
    为一条 AI 建议生成「跳转 + 预填」方案。

    返回 None 表示这条建议没有可配置的面板（纯概念性建议，或方法名匹配不上规则）。
    """
    rule = match_rule(method_name)
    if rule is None:
        return None
    resolved = resolve_variables(rule, variables, fields_meta, columns)
    return {
        'rule_id': rule['id'],
        'page': rule['page'],
        'panel_key': (rule.get('panel') or (None, None))[0],
        'panel_value': (rule.get('panel') or (None, None))[1],
        'tabs_hint': rule.get('tabs_hint') or '',
        'params': resolved['params'],
        'from_ai': resolved['from_ai'],
        'guessed': resolved['guessed'],
        'skipped': resolved['skipped'],
        'label': str(method_name or ''),
        'hint': rule.get('hint') or '',
    }


def describe_setup(plan: Dict) -> str:
    """一句话说明这次预填的内容。"""
    if not plan:
        return ''
    parts = []
    if plan.get('params'):
        parts.append(f"预填 {len(plan['params'])} 项参数")
    if plan.get('from_ai'):
        parts.append(f"其中 {len(plan['from_ai'])} 项来自 AI 指定")
    if plan.get('guessed'):
        parts.append(f"{len(plan['guessed'])} 项由程序推测")
    if plan.get('tabs_hint'):
        parts.append(f"该页需手动点开「{plan['tabs_hint']}」标签")
    return '；'.join(parts) if parts else '仅跳转到页签'
