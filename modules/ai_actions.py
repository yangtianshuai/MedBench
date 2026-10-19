"""
解析 AI 输出的「机器可读清单」，并与本地规则引擎的推荐做对照。

设计原则：
1. **AI 只给建议，程序不代替执行**：本模块只做「解析 + 校验 + 对照」，
   真正写入会话状态的动作一律由界面层在用户确认后触发。
2. **解析失败优雅降级**：返回 `ok=False` 与原文，界面只显示正文，不报错、不丢内容。
3. **字段名严格校验**：AI 给出的字段必须真实存在（含脱敏别名反查），
   类型必须在白名单内；不合规的条目进入 `rejected` 并在界面明示，绝不静默应用。
4. **与本地规则引擎并列展示**：输出「双方一致 / 仅 AI 建议 / 仅本地推荐 /
   冲突（AI 认为可做但规则引擎判定条件不足）」，由用户自行判断。
"""
import json
import re
from typing import Dict, List, Optional

# 与 app.py 的 TYPE_LABELS 保持一致（此处不 import 界面模块，避免循环依赖）
VALID_TYPES = {
    'numeric': '数值',
    'categorical': '分类',
    'binary': '二分类',
    'datetime': '日期',
    'text': '文本',
}

# AI 可以给出的变量角色（用于把建议落到具体控件上）
VARIABLE_ROLES = ('outcome', 'outcome_binary', 'grouping', 'exposure', 'predictors',
                  'time', 'event', 'subject', 'metric')

# 页签名归一化：容忍 AI 只写「统计推断」而不是「🔬 统计推断」
PAGE_ALIASES = {
    '总览': '📊 总览与洞察',
    '总览与洞察': '📊 总览与洞察',
    '洞察': '📊 总览与洞察',
    '数据准备': '🧹 数据准备',
    '数据摘录': '🧹 数据准备',
    '摘录': '🧹 数据准备',
    '时间与质控': '⏱️ 时间与质控',
    '质控': '⏱️ 时间与质控',
    '描述性分析': '📈 描述性分析',
    '描述性': '📈 描述性分析',
    '统计推断': '🔬 统计推断',
    '推断': '🔬 统计推断',
    '预测建模': '🤖 预测建模',
    '建模': '🤖 预测建模',
    '论文辅助': '📚 论文辅助',
    '报告导出': '📄 报告导出',
}

# 方法标签词表：用于把 AI 的方法名与本地规则引擎的推荐条目对上号。
# 做法是「标签交集匹配」而不是字符串相等 —— 两边的措辞几乎不可能完全一致。
METHOD_KEYWORDS = {
    'table1': ('基线特征', 'table 1', 'table1', '表 1', '表1', '基线表'),
    'multicomp': ('多重比较', 'fdr', 'bonferroni', 'holm', 'tukey', 'šidák', 'sidak'),
    'cutoff': ('截断', 'cutoff', '阈值', '约登', 'youden'),
    'external': ('外部验证', '验证集', '独立数据集'),
    'internal': ('内部验证', 'bootstrap', '交叉验证'),
    'lmm': ('混合效应', 'lmm', '随机截距', 'icc', '组内相关'),
    'gee': ('gee', '广义估计'),
    'timeseries': ('时间序列', 'arima', 'holt', '指数平滑'),
    'ttest': ('t 检验', 't检验', 'welch'),
    'mwu': ('mann-whitney', '秩和'),
    'anova': ('anova', '方差分析'),
    'kruskal': ('kruskal',),
    'chi': ('卡方', 'chi-square', 'chi square', 'χ2'),
    'fisher': ('fisher', '确切概率'),
    'paired': ('配对', 'wilcoxon'),
    'agreement': ('kappa', 'bland-altman', '一致性'),
    'roc': ('roc', 'auc'),
    'calibration': ('校准',),
    'dca': ('dca', '决策曲线'),
    'nomogram': ('列线图', 'nomogram'),
    'lasso': ('lasso',),
    'logistic': ('logistic', 'logit'),
    'cox': ('cox', '生存分析', 'km 曲线', 'kaplan'),
    'dose': ('剂量-反应', '剂量反应', '立方样条', 'spline'),
    'psm': ('倾向性评分', 'psm', '倾向评分'),
    'iptw': ('iptw', '逆概率', '加权'),
    'subgroup': ('亚组', '森林图'),
    'evalue': ('e-value', 'e 值', 'evalue'),
    'rate': ('发生率', '感染率', '率与', '率的', '率差', '密度', '‰', '阳转率'),
    'spc': ('控制图', 'spc', 'p 图', 'u 图', 'i-mr'),
    'trend': ('趋势', 'mann-kendall', 'cochran-armitage', '环比', '同比'),
    'correlation': ('相关分析', 'pearson', 'spearman', '热图'),
    'descriptive': ('描述性', '频数分布', '正态性', 'shapiro', '四分位'),
    'missing': ('插补', 'mice', '缺失模式'),
    'reshape': ('宽表', '转长表', '逆透视'),
    'privacy': ('脱敏', '匿名化', '掩码'),
    'vif': ('共线性', 'vif'),
}

# 大小写不敏感：本地模型常把标签写成 ```JSON
JSON_BLOCK = re.compile(r'```(?:json)?\s*(\{.*?\})\s*```', re.DOTALL | re.IGNORECASE)

# 结构化清单必须含其中至少一个键 —— 既用于辨认「这段 JSON 是不是清单」，
# 也避免把正文里恰好出现的普通 JSON 片段误当成清单。
# thinking 是 Agent 多轮循环的回复标志。
STRUCTURE_KEYS = ('methods', 'type_fixes', 'unavailable', 'actions',
                  'next_step', 'state', 'thinking',
                  # 多表（各维度一张表）解读
                  'scenario', 'joins', 'long_format')

# 多表关联时可用的聚合方式。程序需要按它执行，所以只能取固定值；
# AI 若写成「取峰值」这类自由文本，会被记为未通过校验而不是静默忽略。
AGG_CHOICES = {
    'first': '首次值',
    'last': '末次值',
    'max': '最大值',
    'min': '最小值',
    'mean': '平均值',
    'sum': '求和',
    'count': '记录条数',
    'any': '是否发生过（0/1）',
}
JOIN_RELATIONS = ('1:1', '1:N')
_NULL_WORDS = ('null', 'none', '无', 'n/a', 'na', '-')


def normalize_page(name) -> Optional[str]:
    """把 AI 给的页签名归一化到程序里的实际页签名。"""
    text = str(name or '').strip()
    if not text:
        return None
    for label in PAGE_ALIASES.values():
        if label == text:
            return label
    cleaned = text.lstrip('📊📌⏱️📈🔬🤖📚📄🩺 ').strip()
    return PAGE_ALIASES.get(cleaned)


def resolve_column(name, alias_map: Optional[Dict[str, str]],
                   columns) -> Optional[str]:
    """
    把 AI 给出的字段名还原为真实列名。

    AI 看到的是脱敏后的别名（字段A…），因此必须反查回去，否则应用修正时会找不到列。
    """
    text = str(name or '').strip()
    if not text:
        return None
    for column in columns:
        if str(column) == text:
            return column
    for real, alias in (alias_map or {}).items():
        if str(alias) == text:
            return real
    lowered = text.lower()
    for column in columns:
        if str(column).strip().lower() == lowered:
            return column
    return None


def method_tags(text) -> set:
    """提取方法标签，用于跨措辞的匹配。"""
    lowered = str(text or '').lower()
    return {key for key, hints in METHOD_KEYWORDS.items()
            if any(hint in lowered for hint in hints)}


def _similar_name(left, right) -> bool:
    """两个名称是否指向同一件事：标签有交集，或归一化后互相包含。"""
    if method_tags(left) & method_tags(right):
        return True
    a = re.sub(r'[\s（）()【】\[\]、，,。.:：\-—_*#]+', '', str(left or '').lower())
    b = re.sub(r'[\s（）()【】\[\]、，,。.:：\-—_*#]+', '', str(right or '').lower())
    if len(a) >= 3 and len(b) >= 3:
        return a in b or b in a
    return False


def _is_manifest(data) -> bool:
    """这段 JSON 是否像「结构化清单」。"""
    return isinstance(data, dict) and any(key in data for key in STRUCTURE_KEYS)


def _parse_json_object(text) -> Optional[Dict]:
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _scan_bare_json(text: str):
    """
    在正文里扫描**没有围栏**的 JSON 对象（本地小模型常见的输出方式）。

    用 JSONDecoder.raw_decode 而不是正则：清单是嵌套结构，正则很难正确界定边界
    （非贪婪会停在内层 `}` 上）。从每个 `{` 起尝试，返回第一个合法的清单。
    """
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != '{':
            continue
        try:
            data, end = decoder.raw_decode(text, index)
        except Exception:
            continue
        if _is_manifest(data):
            return data, index, end
    return None


def extract_json_block(text: str) -> Dict:
    """
    从 AI 输出中取出结构化清单。

    三级容错（本地模型的输出格式比云端随意得多，任何一级缺失都会导致
    对照表、建议卡片、类型修正静默消失，而界面看起来一切正常）：
      1. 优先取**最后一个**合法围栏块 —— 正文里可能先出现示例性代码块
      2. 围栏标签大小写不敏感（```JSON / ```Json 都能认）
      3. 完全没有围栏时，用 raw_decode 扫描正文中的裸 JSON 对象

    识别不到时返回 {'data': None, 'body': 原文}，由界面降级为「只显示正文」。
    """
    raw = str(text or '')
    for match in reversed(list(JSON_BLOCK.finditer(raw))):
        data = _parse_json_object(match.group(1))
        # 兼容多类结构化输出：解读/方案清单、表格整理操作（actions）、
        # 整理后的闭环评估（next_step / state）
        if _is_manifest(data):
            body = (raw[:match.start()] + raw[match.end():]).strip()
            return {'data': data, 'body': body}

    found = _scan_bare_json(raw)
    if found is not None:
        data, start, end = found
        return {'data': data, 'body': (raw[:start] + raw[end:]).strip()}

    return {'data': None, 'body': raw}


def _clean_text(value, limit: int = 200) -> str:
    return str(value or '').strip()[:limit]


def _clean_variables(raw) -> Dict:
    """
    规范化 AI 给出的变量角色表。

    只保留允许的角色键；值为空、写 null 或写「无」的一律丢弃 ——
    AI 有时会为了「填满格式」写上占位符，那些不能当成真实变量去预填。
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict = {}
    for key, value in raw.items():
        role = str(key or '').strip().lower()
        if role not in VARIABLE_ROLES:
            continue
        if isinstance(value, (list, tuple)):
            items = [_clean_text(item, 60) for item in value]
            items = [item for item in items if item and item.lower() not in _NULL_WORDS]
            if items:
                cleaned[role] = items
        else:
            text = _clean_text(value, 60)
            if text and text.lower() not in _NULL_WORDS:
                cleaned[role] = text
    return cleaned


def _parse_joins(items, rejected: Optional[List] = None) -> List[Dict]:
    """
    解析多表关联方案。

    聚合方式是程序要照着执行的东西，所以必须落在白名单里。AI 写成自由文本时
    宁可明确记为「未通过校验」（用户能看见并自己改），也不要静默丢掉 ——
    关联方案少了聚合方式，就等于允许把一对多直接挂上去，那正是要防的事。
    """
    joins: List[Dict] = []
    for item in (items or [])[:8]:
        if not isinstance(item, dict):
            continue
        base = _clean_text(item.get('base'), 60)
        extra = _clean_text(item.get('extra'), 60)
        key = _clean_text(item.get('key'), 60)
        if not (base and extra and key):
            if rejected is not None:
                rejected.append({
                    'column': '', 'to': '',
                    'reason': '关联方案缺少「主表 / 待挂表 / 关联列」，已忽略'})
            continue

        aggregations: Dict[str, str] = {}
        bad: List[str] = []
        for column, how in (item.get('aggregations') or {}).items():
            name = _clean_text(column, 60)
            method = str(how or '').strip().lower()
            if not name:
                continue
            if method not in AGG_CHOICES:
                bad.append(f'{name}→{how}')
                continue
            aggregations[name] = method
        if bad and rejected is not None:
            rejected.append({
                'column': '', 'to': '',
                'reason': ('聚合方式不在支持范围内，已忽略这些列：' + '、'.join(bad[:4])
                           + '。支持：' + '、'.join(AGG_CHOICES))})

        relation = _clean_text(item.get('relation'), 20)
        joins.append({
            'base': base,
            'extra': extra,
            'key': key,
            'relation': relation if relation in JOIN_RELATIONS else '1:N',
            'aggregations': aggregations,
            'why': _clean_text(item.get('why'), 200),
        })
    return joins


def _parse_long_format(items) -> List[Dict]:
    """解析「某张表更适合保留多次测量」的建议。"""
    plans: List[Dict] = []
    for item in (items or [])[:5]:
        if not isinstance(item, dict):
            continue
        sheet = _clean_text(item.get('sheet'), 60)
        key = _clean_text(item.get('key'), 60)
        if not (sheet and key):
            continue
        values = [_clean_text(value, 60) for value in (item.get('values') or [])]
        plans.append({
            'sheet': sheet,
            'key': key,
            'time': _clean_text(item.get('time'), 60),
            'values': [value for value in values if value][:10],
        })
    return plans


def parse_ai_output(text: str, *, columns, profile: Optional[Dict] = None,
                    alias_map: Optional[Dict[str, str]] = None) -> Dict:
    """
    解析并校验 AI 输出。

    columns   : 当前数据的真实列名（用于字段校验与别名反查）
    profile   : 本地规则引擎的画像（读取 recommendations 做对照）
    alias_map : {真实列名: 脱敏别名}
    """
    columns = list(columns or [])
    extracted = extract_json_block(text)
    data = extracted.get('data') or {}
    result = {
        'ok': bool(extracted.get('data')),
        'body': extracted.get('body') or '',
        'state': '',
        'next_step': None,
        'scenario': '',
        'joins': [],
        'long_format': [],
        'methods': [],
        'type_fixes': [],
        'unavailable': [],
        'rejected': [],
        'comparison': {'both': [], 'ai_only': [], 'local_only': [], 'conflicts': []},
        'notice': '',
    }
    if not result['ok']:
        result['notice'] = 'AI 未返回结构化清单，仅展示正文建议。'
        return result

    # ---------- 方法建议 ----------
    for item in (data.get('methods') or [])[:20]:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get('name'), 80)
        if not name:
            continue
        result['methods'].append({
            'name': name,
            'page': normalize_page(item.get('page')) or '',
            'priority': int(item.get('priority') or len(result['methods']) + 1)
            if str(item.get('priority') or '').strip().isdigit() else len(result['methods']) + 1,
            'local_same': _clean_text(item.get('local_same'), 80),
            'variables': _clean_variables(item.get('variables')),
        })

    # ---------- 字段类型修正（严格校验） ----------
    for item in (data.get('type_fixes') or [])[:20]:
        if not isinstance(item, dict):
            continue
        shown = _clean_text(item.get('column'), 60)
        target = str(item.get('to') or '').strip().lower()
        reason = _clean_text(item.get('reason'), 120)
        column = resolve_column(shown, alias_map, columns)
        if column is None:
            result['rejected'].append({
                'kind': 'type_fix', 'column': shown, 'to': target,
                'reason': '该字段不在当前数据中（可能是 AI 写错或已脱敏无法反查）'})
            continue
        if target not in VALID_TYPES:
            result['rejected'].append({
                'kind': 'type_fix', 'column': shown, 'to': target,
                'reason': '类型不在允许范围（numeric / categorical / binary / datetime / text）'})
            continue
        result['type_fixes'].append({
            'column': column, 'shown': shown, 'to': target,
            'to_label': VALID_TYPES[target], 'reason': reason})

    # ---------- 程序未内置的方法 ----------
    for item in (data.get('unavailable') or [])[:20]:
        text_item = _clean_text(item, 80)
        if text_item:
            result['unavailable'].append(text_item)

    # ---------- 整理闭环：数据状态与建议的第一步 ----------
    result['state'] = _clean_text(data.get('state'), 200)

    # ---------- 多表解读：场景、关联方案、长表建议 ----------
    result['scenario'] = _clean_text(data.get('scenario'), 200)
    result['joins'] = _parse_joins(data.get('joins'), result['rejected'])
    result['long_format'] = _parse_long_format(data.get('long_format'))
    step = data.get('next_step')
    if isinstance(step, dict):
        action_text = _clean_text(step.get('action'), 160)
        if action_text:
            result['next_step'] = {
                'action': action_text,
                'page': normalize_page(step.get('page')) or '',
                'method': _clean_text(step.get('method'), 80),
                'needs': _clean_text(step.get('needs'), 160),
                'why': _clean_text(step.get('why'), 200),
                'variables': _clean_variables(step.get('variables')),
            }

    result['comparison'] = compare_with_local(
        result['methods'], (profile or {}).get('recommendations') or [])
    return result


def compare_with_local(methods: List[Dict], recommendations: List[Dict]) -> Dict:
    """把 AI 建议与本地规则引擎的推荐逐条对照，标出分歧。"""
    comparison = {'both': [], 'ai_only': [], 'local_only': [], 'conflicts': []}
    used_recs = set()

    for index, method in enumerate(methods):
        same_hint = str(method.get('local_same') or '').strip()
        matched = None
        matched_index = None
        # ① AI 主动声明「与本地某条相同」时，优先按该条匹配
        if same_hint and same_hint.lower() not in _NULL_WORDS:
            for position, rec in enumerate(recommendations):
                if _similar_name(same_hint, rec.get('title')):
                    matched, matched_index = rec, position
                    break
        # ② 否则按方法标签/名称相似度匹配
        if matched is None:
            for position, rec in enumerate(recommendations):
                if _similar_name(method['name'], rec.get('title')):
                    matched, matched_index = rec, position
                    break

        if matched is None:
            comparison['ai_only'].append({
                'name': method['name'], 'page': method.get('page') or '',
                'priority': method.get('priority')})
            continue

        used_recs.add(matched_index)
        entry = {
            'name': method['name'], 'page': method.get('page') or '',
            'local_title': matched.get('title'),
            'local_ready': bool(matched.get('ready')),
            'blockers': list(matched.get('blockers') or []),
            'priority': method.get('priority')}
        if matched.get('ready'):
            comparison['both'].append(entry)
        else:
            # AI 认为可做，但规则引擎判定条件不足 —— 最需要用户看的分歧
            comparison['conflicts'].append(entry)

    for position, rec in enumerate(recommendations):
        if position in used_recs or not rec.get('ready'):
            continue
        comparison['local_only'].append({
            'name': rec.get('title'), 'evidence': rec.get('evidence') or ''})

    return comparison


def summarize_comparison(comparison: Dict) -> str:
    """给界面用的一句话摘要。"""
    both = len(comparison.get('both') or [])
    ai_only = len(comparison.get('ai_only') or [])
    local_only = len(comparison.get('local_only') or [])
    conflicts = len(comparison.get('conflicts') or [])
    parts = [f'一致 {both} 条']
    if ai_only:
        parts.append(f'仅 AI 建议 {ai_only} 条')
    if local_only:
        parts.append(f'仅规则引擎推荐 {local_only} 条')
    if conflicts:
        parts.append(f'分歧（规则引擎判定条件不足）{conflicts} 条')
    return '对照结果：' + '、'.join(parts) + '。'
