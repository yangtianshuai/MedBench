"""
探索性分析 Agent（受约束的多轮循环）

与 L1 的区别：L1 是「AI 建议 → 用户点一下」，这里让 AI **连续**执行多轮 ——
跑一个分析、看结果、再决定下一步，直到它认为可以给出结论。

**为什么必须"受约束"**，约束全部由程序强制、不依赖 AI 自觉：

  - **轮数上限**（默认 4）：防跑飞与成本失控
  - **白名单执行**：AI 只能从 ACTIONS 里选，不能自由构造统计过程
  - **参数校验**：字段必须真实存在且类型合适，非法参数直接拒绝并回告 AI
  - **结果截断**：发给 AI 的表格最多 12 行 × 12 列，且不含个体数据行
  - **多重比较提醒**：连续跑 ≥3 个分析等于隐式做了多次检验，程序强制提示
  - **可回溯**：每轮的「做了什么 + 为什么」记入分析台账
  - **结论定性**：最终结论标注为「AI 整理的说明」，不是统计结论

适用范围要说清楚：这套东西适合**探索性分析**（这份数据能看出什么），
不适合**验证性分析**（研究假设已定，方法该由研究设计决定，不该让 AI 去试）。
"""
import json
from typing import Dict, List, Optional, Tuple

import pandas as pd

MAX_ROUNDS = 6
MAX_TABLE_ROWS = 12
MAX_TABLE_COLS = 12
MULTICOMP_ROUNDS = 3          # 达到这个轮数就强制提示多重比较
MAX_TABLE1_VARIABLES = 25

MULTICOMP_HINT = (
    '本次共执行了 {count} 个分析，相当于在同一份数据上做了多次检验。'
    '若要写进论文，建议对主要结局的多重比较做校正'
    '（🔬 统计推断 → 多重比较校正），否则假阳性率会明显升高。'
)

CONCLUSION_DISCLAIMER = (
    '⚠️ 以上「结论」由 AI 基于各轮结果整理而成，**属于表述性总结，不是统计结论**。'
    '方法选择与结果解释请自行核对，所有数值以本地计算为准。'
)

# 变量角色（与 ai_setup 对齐）
ROLE_LABELS = {
    'outcome': '数值变量（连续）',
    'outcome_binary': '二分类变量',
    'grouping': '分组变量',
    'predictors': '变量（可多个）',
    'columns': '纳入变量',
}


def _run_table1(df, params, fields):
    from modules.table1 import BaselineTable

    group = params.get('grouping')
    variables = [name for name in df.columns if name != group][:MAX_TABLE1_VARIABLES]
    return BaselineTable.build(df, variables, group_col=group)


def _run_compare_numeric(df, params, fields):
    from modules.inferential import InferentialTests

    outcome, grouping = params['outcome'], params['grouping']
    return InferentialTests.two_groups_numeric(df[outcome], df[grouping])


def _run_correlation(df, params, fields):
    from modules.inferential import InferentialTests

    return InferentialTests.correlation(df[params['x']], df[params['y']], 'auto')


def _run_linear(df, params, fields):
    from modules.linear import LinearModel

    predictors = list(params.get('predictors') or [])
    categorical = [name for name in predictors
                   if (fields.get(name) or {}).get('dtype') in ('categorical', 'binary')]
    return LinearModel.fit(df, params['outcome'], predictors, categorical)


def _run_logistic(df, params, fields):
    from modules.modeling import PredictiveModeling

    return PredictiveModeling.logistic_regression(
        df, params['outcome'], list(params.get('predictors') or []))


def _run_tukey(df, params, fields):
    from modules.multicomp import MultiComparison

    return MultiComparison.pairwise_tukey(df[params['outcome']], df[params['grouping']])


def _run_compare_categorical(df, params, fields):
    from modules.inferential import InferentialTests

    return InferentialTests.categorical_vs_categorical(
        df[params['outcome']], df[params['grouping']])


def _run_paired(df, params, fields):
    from modules.inferential import InferentialTests

    return InferentialTests.paired_ttest(df[params['before']], df[params['after']])


def _run_anova(df, params, fields):
    from modules.inferential import InferentialTests

    return InferentialTests.multi_groups_numeric(df[params['outcome']],
                                                 df[params['grouping']])


def _run_cox(df, params, fields):
    from modules.modeling import PredictiveModeling

    return PredictiveModeling.cox_regression(
        df, params['time'], params['event'], list(params.get('predictors') or []))


def _run_dose_response(df, params, fields):
    from modules.dose_response import DoseResponse

    outcome = params['outcome']
    dtype = (fields.get(outcome) or {}).get('dtype')
    return DoseResponse.analyze(df, params['exposure'], outcome,
                                outcome_type='binary' if dtype == 'binary' else 'continuous')


def _run_psm(df, params, fields):
    from modules.research import ResearchAssistant

    return ResearchAssistant.propensity_score_matching(
        df, params['exposure'], params['outcome'],
        covariates=list(params.get('predictors') or []))


def _run_iptw(df, params, fields):
    from modules.iptw import IPTWAnalysis

    return IPTWAnalysis.run(df, params['exposure'], params['outcome'],
                            covariates=list(params.get('predictors') or []))


def _run_subgroup(df, params, fields):
    from modules.research import ResearchAssistant

    return ResearchAssistant.subgroup_analysis(
        df, params['outcome'], params['exposure'], params['grouping'])


def _run_lmm(df, params, fields):
    from modules.longitudinal import LongitudinalAnalysis

    return LongitudinalAnalysis.linear_mixed(
        df, params['outcome'], params['time'], params['subject'],
        covariates=list(params.get('predictors') or []) or None)


def _run_gee(df, params, fields):
    from modules.longitudinal import LongitudinalAnalysis

    outcome = params['outcome']
    dtype = (fields.get(outcome) or {}).get('dtype')
    family = 'binomial' if dtype == 'binary' else 'gaussian'
    return LongitudinalAnalysis.gee(
        df, outcome, params['time'], params['subject'],
        covariates=list(params.get('predictors') or []) or None, family=family)


def _run_kappa(df, params, fields):
    from modules.agreement import AgreementAnalysis

    return AgreementAnalysis.cohens_kappa(df[params['rater_a']], df[params['rater_b']])


def _run_icc(df, params, fields):
    from modules.agreement import AgreementAnalysis

    return AgreementAnalysis.icc(df[params['measure1']], df[params['measure2']])


# 白名单：AI 只能从这里选。每个动作声明它需要哪些参数、以及参数的角色。
ACTIONS: Dict[str, Dict] = {
    'table1': {
        'label': '基线特征表（Table 1）',
        'params': [('grouping', 'grouping', True, '分组变量')],
        'runner': _run_table1,
        'purpose': '了解整体分布、各组基线是否均衡',
    },
    'compare_numeric': {
        'label': '两组比较（数值变量）',
        'params': [('outcome', 'outcome', True, '数值变量'),
                   ('grouping', 'grouping', True, '分组变量')],
        'runner': _run_compare_numeric,
        'purpose': '比较两组的数值指标差异（自动选择 t 检验或 Mann-Whitney U）',
    },
    'correlation': {
        'label': '相关分析',
        'params': [('x', 'outcome', True, 'X 变量'), ('y', 'outcome', True, 'Y 变量')],
        'runner': _run_correlation,
        'purpose': '看两个连续变量之间的关联强度与方向',
    },
    'linear': {
        'label': '多重线性回归',
        'params': [('outcome', 'outcome', True, '结局变量（连续）'),
                   ('predictors', 'predictors', True, '自变量')],
        'runner': _run_linear,
        'purpose': '连续结局的独立相关因素分析；含 VIF、Durbin-Watson 与残差诊断',
    },
    'logistic': {
        'label': 'Logistic 回归',
        'params': [('outcome', 'outcome_binary', True, '结局变量（二分类）'),
                   ('predictors', 'predictors', True, '自变量')],
        'runner': _run_logistic,
        'purpose': '二分类结局的危险因素分析（输出 OR 值）',
    },
    'tukey': {
        'label': '多组两两比较（Tukey HSD）',
        'params': [('outcome', 'outcome', True, '数值变量'),
                   ('grouping', 'grouping', True, '分组变量')],
        'runner': _run_tukey,
        'purpose': '三组以上时的两两比较，已内置多重比较校正',
    },
    'anova': {
        'label': '多组比较（ANOVA / Kruskal-Wallis）',
        'params': [('outcome', 'outcome', True, '数值变量'),
                   ('grouping', 'grouping', True, '分组变量')],
        'runner': _run_anova,
        'purpose': '三组及以上的组间差异（按正态性自动选 ANOVA 或 Kruskal-Wallis，含事后检验）',
    },
    'compare_categorical': {
        'label': '分类变量比较（卡方 / Fisher）',
        'params': [('outcome', 'outcome_binary', True, '二分类变量'),
                   ('grouping', 'grouping', True, '分组变量')],
        'runner': _run_compare_categorical,
        'purpose': '比较两组之间某个二分类结局的比例差异（如两组并发症发生率）',
    },
    'paired': {
        'label': '配对比较（配对 t / Wilcoxon）',
        'params': [('before', 'outcome', True, '前 / 左（数值）'),
                   ('after', 'outcome', True, '后 / 右（数值）')],
        'runner': _run_paired,
        'purpose': '同一对象前后两次（或左右两侧）测量的差异',
    },
    'cox': {
        'label': 'Cox 比例风险回归',
        'params': [('time', 'time', True, '生存时间列'),
                   ('event', 'event', True, '事件列（0/1）'),
                   ('predictors', 'predictors', True, '协变量')],
        'runner': _run_cox,
        'purpose': '生存数据的危险因素分析（输出 HR 值）',
    },
    'dose_response': {
        'label': '剂量-反应（限制性立方样条）',
        'params': [('exposure', 'outcome', True, '连续暴露变量'),
                   ('outcome', 'exposure', True, '结局变量')],
        'runner': _run_dose_response,
        'purpose': '检验连续暴露与结局之间是否线性、有无阈值或 J 型关系',
    },
    'psm': {
        'label': '倾向性评分匹配（PSM）',
        'params': [('exposure', 'exposure', True, '处理变量（0/1）'),
                   ('outcome', 'outcome_binary', True, '结局变量（二分类）'),
                   ('predictors', 'predictors', True, '匹配用协变量')],
        'runner': _run_psm,
        'purpose': '观察性研究中控制混杂：按倾向评分匹配处理组与对照组',
    },
    'iptw': {
        'label': 'IPTW 逆概率加权',
        'params': [('exposure', 'exposure', True, '处理变量（0/1）'),
                   ('outcome', 'outcome_binary', True, '结局变量（二分类）'),
                   ('predictors', 'predictors', True, '协变量')],
        'runner': _run_iptw,
        'purpose': '与 PSM 互补：用权重保留全部样本，同样用于控制混杂',
    },
    'subgroup': {
        'label': '亚组分析（森林图）',
        'params': [('exposure', 'exposure', True, '处理变量（0/1）'),
                   ('outcome', 'outcome_binary', True, '结局变量（二分类）'),
                   ('grouping', 'grouping', True, '亚组变量')],
        'runner': _run_subgroup,
        'purpose': '看处理效应在不同亚组间是否一致（含交互作用检验）',
    },
    'lmm': {
        'label': '线性混合效应模型（LMM）',
        'params': [('subject', 'subject', True, '个体标识列'),
                   ('time', 'time', True, '时间变量'),
                   ('outcome', 'outcome', True, '结局变量（连续）'),
                   ('predictors', 'predictors', False, '协变量')],
        'runner': _run_lmm,
        'purpose': '同一对象多次测量的连续结局分析（含 ICC）',
    },
    'gee': {
        'label': 'GEE 广义估计方程',
        'params': [('subject', 'subject', True, '个体标识列'),
                   ('time', 'time', True, '时间变量'),
                   ('outcome', 'exposure', True, '结局变量'),
                   ('predictors', 'predictors', False, '协变量')],
        'runner': _run_gee,
        'purpose': '重复测量数据的稳健分析（二分类结局输出 OR，对相关结构假设不敏感）',
    },
    'kappa': {
        'label': '一致性分析（Kappa）',
        'params': [('rater_a', 'grouping', True, '评估者 / 方法 1'),
                   ('rater_b', 'grouping', True, '评估者 / 方法 2')],
        'runner': _run_kappa,
        'purpose': '两名评估者对同一批对象的分类判定是否一致',
    },
    'icc': {
        'label': '一致性分析（ICC / Bland-Altman）',
        'params': [('measure1', 'outcome', True, '测量值 1（数值）'),
                   ('measure2', 'outcome', True, '测量值 2（数值）')],
        'runner': _run_icc,
        'purpose': '两次测量或两种方法的数值一致性（含 ICC(2,1) 与 95%CI）',
    },
}


def action_menu() -> str:
    """给 AI 看的可选动作清单。"""
    lines = []
    for key, spec in ACTIONS.items():
        params = '、'.join(f'{name}（{ROLE_LABELS.get(role, role)}）'
                          for name, role, _required, _label in spec['params'])
        lines.append(f'- `{key}`：{spec["label"]} —— {spec["purpose"]}；需要参数：{params}')
    return '\n'.join(lines)


def describe_action(kind: str, params: Dict) -> str:
    spec = ACTIONS.get(kind)
    if spec is None:
        return str(kind)
    detail = '、'.join(f'{key}={value}' for key, value in (params or {}).items())
    return f'{spec["label"]}（{detail}）' if detail else spec['label']


# ==================== 参数校验 ====================

def _type_ok(role: str, name: str, fields: Dict) -> Tuple[bool, str]:
    meta = fields.get(name) or {}
    dtype = meta.get('dtype')
    if role == 'outcome':
        return (dtype == 'numeric', '需要数值型字段')
    if role == 'outcome_binary':
        return (dtype == 'binary', '需要二分类字段（0/1）')
    if role == 'event':
        return (dtype == 'binary', '需要二分类字段（0/1）')
    if role == 'grouping':
        return (dtype in ('categorical', 'binary'), '需要分类字段')
    return (dtype is not None, '字段不可用')


def check_params(kind: str, params: Dict, columns: List[str],
                 fields: Dict) -> Tuple[bool, Dict, List[str]]:
    """
    校验并规范化 AI 给出的参数。

    返回 (是否可用, 规范化的参数, 问题列表)。问题会回告 AI，让它下一轮改正 ——
    而不是由程序悄悄替换成别的变量。
    """
    spec = ACTIONS.get(kind)
    if spec is None:
        return False, {}, [f'不支持的分析：{kind}（只能从可选清单里选）']
    columns = list(columns or [])
    params = params or {}
    cleaned: Dict = {}
    problems: List[str] = []

    for name, role, required, label in spec['params']:
        value = params.get(name)
        if role == 'predictors':
            names = [item for item in (value if isinstance(value, list) else [value])
                     if isinstance(item, str)]
            names = [item for item in names if item in columns]
            if not names:
                # 可选参数（如 LMM / GEE 的协变量）允许留空，
                # 不能因为「没给可选参数」就否掉整轮
                if required:
                    problems.append(f'{label} 缺失或字段不存在')
                continue
            cleaned[name] = names
            continue
        if not isinstance(value, str) or value not in columns:
            if required:
                problems.append(f'{label} 缺失或字段「{value}」不在数据中')
            continue
        ok, reason = _type_ok(role, value, fields)
        if not ok:
            problems.append(f'{label}「{value}」类型不合适（{reason}）')
            continue
        cleaned[name] = value

    if problems:
        return False, cleaned, problems
    return True, cleaned, []


def run_action(kind: str, df: pd.DataFrame, params: Dict,
               fields: Optional[Dict] = None) -> Dict:
    """执行白名单里的一个分析，返回统一结构。"""
    spec = ACTIONS.get(kind)
    if spec is None:
        return {'error': f'不支持的分析：{kind}'}
    try:
        result = spec['runner'](df, params, fields or {})
    except Exception as error:  # noqa: BLE001
        return {'error': f'执行失败：{error}'}
    if not isinstance(result, dict):
        return {'error': '分析没有返回可用结果'}
    if result.get('error'):
        return {'error': str(result['error'])}
    return result


# ==================== 结果摘要（发给 AI） ====================

def _format_table(frame: pd.DataFrame, max_rows: int = MAX_TABLE_ROWS,
                  max_cols: int = MAX_TABLE_COLS) -> str:
    if frame is None or len(frame) == 0:
        return ''
    view = frame.iloc[:max_rows, :max_cols]
    header = '| ' + ' | '.join(str(col) for col in view.columns) + ' |'
    divider = '| ' + ' | '.join('---' for _ in view.columns) + ' |'
    lines = [header, divider]
    for _, row in view.iterrows():
        cells = []
        for value in row.tolist():
            text = str(value)
            cells.append(text[:40].replace('|', '／').replace('\n', ' '))
        lines.append('| ' + ' | '.join(cells) + ' |')
    if len(frame) > max_rows:
        lines.append(f'（表格共 {len(frame)} 行，仅列出前 {max_rows} 行）')
    if len(frame.columns) > max_cols:
        lines.append(f'（表格共 {len(frame.columns)} 列，仅列出前 {max_cols} 列）')
    return '\n'.join(lines)


def summarize_result(result: Dict, *, title: str = '') -> str:
    """
    把一次分析的结果压成可发给 AI 的文本。

    刻意**不硬编码**每个分析的返回键 —— 而是通用地提取标量、表格与提示文本。
    这样以后加新分析时不必再配一个摘要器，也不会因为某个键改名就断掉。
    图片（bytes）不发送，只注明生成了图。
    """
    lines: List[str] = []
    if title:
        lines.append(f'### {title}')

    scalars: List[str] = []
    tables: List[str] = []
    notes: List[str] = []
    skipped_figure = False

    def walk(node, prefix: str = '', depth: int = 0) -> None:
        nonlocal skipped_figure
        if depth > 3:
            return
        if isinstance(node, pd.DataFrame):
            text = _format_table(node)
            if text:
                tables.append(f'**{prefix or "表格"}**\n{text}')
            return
        if isinstance(node, dict):
            for key, value in node.items():
                name = f'{prefix}.{key}' if prefix else str(key)
                if isinstance(value, (dict, list, pd.DataFrame)):
                    walk(value, name, depth + 1)
                elif isinstance(value, bytes):
                    skipped_figure = True
                elif isinstance(value, (str, int, float, bool)) and value is not None:
                    text = str(value)
                    if len(text) > 200:
                        notes.append(f'{name}：{text[:200]}…')
                    else:
                        scalars.append(f'{name}={text}')
            return
        if isinstance(node, list):
            if node and all(isinstance(item, dict) for item in node):
                try:
                    text = _format_table(pd.DataFrame(node))
                    if text:
                        tables.append(f'**{prefix or "表格"}**\n{text}')
                except Exception:  # noqa: BLE001
                    pass
            elif node:
                text = '、'.join(str(item)[:40] for item in node[:20])
                scalars.append(f'{prefix or "列表"}={text}')
            return
        if isinstance(node, bytes):
            skipped_figure = True

    walk(result, '')

    if scalars:
        lines.append('**关键指标**：' + '；'.join(scalars))
    for block in tables:
        lines.append(block)
    for note in notes:
        lines.append(f'- {note}')
    if skipped_figure:
        lines.append('（本次分析还生成了一张图，未发送给 AI）')
    if not lines or (len(lines) == 1 and title):
        lines.append('（该分析没有返回可读的数值结果）')
    return '\n\n'.join(lines)


# ==================== AI 回复解析 ====================

def parse_agent_reply(text: str) -> Dict:
    """
    解析 AI 的每一轮回复。

    返回 {'ok', 'thinking', 'action', 'analysis', 'params', 'why',
          'conclusion', 'methods_text', 'cautions', 'body'}
    """
    from modules.ai_actions import extract_json_block

    extracted = extract_json_block(text or '')
    data = extracted.get('data') or {}
    result = {
        'ok': False, 'body': extracted.get('body') or '',
        'thinking': '', 'action': '', 'analysis': '', 'params': {},
        'why': '', 'conclusion': '', 'methods_text': '', 'cautions': [],
    }
    if not isinstance(data, dict):
        return result

    thinking = str(data.get('thinking') or '').strip()
    # agent 的回复必须带 thinking 或 action 才算有效（避免误抓别的清单）
    if not thinking and not data.get('action'):
        return result

    result['ok'] = True
    result['thinking'] = thinking[:400]
    result['action'] = str(data.get('action') or '').strip().lower()
    result['analysis'] = str(data.get('analysis') or '').strip()
    result['why'] = str(data.get('why') or '').strip()[:400]
    params = data.get('params')
    if isinstance(params, dict):
        result['params'] = {str(key): value for key, value in params.items()}
    conclusion = data.get('conclusion')
    if isinstance(conclusion, list):
        result['conclusion'] = '\n'.join(f'- {item}' for item in conclusion)
    else:
        result['conclusion'] = str(conclusion or '').strip()
    result['methods_text'] = str(data.get('methods_text') or '').strip()
    cautions = data.get('cautions')
    if isinstance(cautions, list):
        result['cautions'] = [str(item)[:200] for item in cautions if item]
    elif isinstance(cautions, str) and cautions.strip():
        result['cautions'] = [cautions.strip()[:200]]
    return result


def build_history_text(history: List[Dict]) -> str:
    """把已完成各轮的「动作 + 结果摘要」拼成给 AI 的上下文。"""
    if not history:
        return '（还没有执行任何分析）'
    blocks = []
    for index, item in enumerate(history, 1):
        head = f'## 第 {index} 轮：{item.get("label") or item.get("kind")}'
        params = item.get('params') or {}
        detail = '、'.join(f'{key}={value}' for key, value in params.items())
        blocks.append(f'{head}\n参数：{detail or "无"}\n{item.get("summary") or ""}')
    return '\n\n'.join(blocks)


def multicomp_warning(rounds: int) -> str:
    """达到阈值时返回多重比较提醒，否则返回空串。"""
    if rounds >= MULTICOMP_ROUNDS:
        return MULTICOMP_HINT.format(count=rounds)
    return ''


def build_agent_messages(structure_payload: Dict, question: str,
                         history: List[Dict]) -> List[Dict]:
    """
    组装 Agent 的系统提示。

    只带「样本量 + 字段清单 + 研究问题 + 已完成轮次」：不带结构角色与规则引擎推荐，
    避免让 AI 被本地判据牵着走 —— 探索性分析的价值恰恰在于它可能想到别的角度。
    """
    from modules.ai_prompts import AGENT_SYSTEM_PROMPT

    field_lines = [f"- {item.get('字段')}（{item.get('类型')}，"
                   f"唯一值 {item.get('唯一值数')}，缺失 {item.get('缺失率')}）"
                   for item in (structure_payload.get('字段清单') or [])]
    fields_text = '\n'.join(field_lines) or '（没有字段信息）'
    prompt = (AGENT_SYSTEM_PROMPT
              .replace('__MENU__', action_menu())
              .replace('__FIELDS__', fields_text)
              .replace('__QUESTION__',
                       (question or '').strip() or '（未填写，按「这份数据能看出什么」来探索）')
              .replace('__HISTORY__', build_history_text(history)))
    return [{'role': 'system', 'content': prompt}]


def run_one_round(kind: str, df: pd.DataFrame, params: Dict, fields: Dict,
                  columns: List[str]) -> Dict:
    """
    执行 Agent 的一轮：校验参数 → 运行分析 → 生成摘要。

    返回 {'ok', 'entry', 'problems'}；entry 为可直接展示与记台账的完整记录。
    """
    spec = ACTIONS.get(kind)
    if spec is None:
        return {'ok': False, 'problems': [f'不支持的分析「{kind}」，请从可选清单里选']}

    usable, cleaned, problems = check_params(kind, params, columns, fields)
    if not usable:
        return {'ok': False, 'problems': problems}

    result = run_action(kind, df, cleaned, fields)
    if result.get('error'):
        return {'ok': False, 'problems': [f'执行失败：{result["error"]}']}

    label = spec['label']
    summary = summarize_result(result, title=label)
    return {
        'ok': True, 'problems': [],
        'entry': {
            'kind': kind,
            'label': label,
            'params': cleaned,
            'summary': summary,
            'result': result,
            'table': _primary_table(result),
            'plot': _primary_plot(result),
        },
    }


def _primary_table(result: Dict):
    """挑一张最适合展示的表：优先系数表 / 结果表 / 基线表。"""
    for key in ('coef_table', 'table', 'summary_table', 'smd_table'):
        value = result.get(key)
        if isinstance(value, pd.DataFrame) and len(value):
            return value
        if isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
            try:
                return pd.DataFrame(value)
            except Exception:  # noqa: BLE001
                continue
    for key, value in result.items():
        if isinstance(value, pd.DataFrame) and len(value):
            return value
    return None


def _primary_plot(result: Dict):
    for key in ('roc_plot', 'plot', 'figure', 'image'):
        value = result.get(key)
        if isinstance(value, bytes) and value[:4] == b'\x89PNG':
            return value
    return None
