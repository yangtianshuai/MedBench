"""
结构摘要构造与脱敏（L1 级别）

发送给 AI 的内容严格限定为「结构信息」：
  - 样本量、字段数、字段名（敏感名可替换为别名）、字段类型、唯一值个数、缺失率
  - 规则引擎识别到的结构角色、推荐与缺口及其判据（这些本身只是元信息）

绝不包含：任何个体数据行、任何字段的均值/中位数/最值/频数等数值统计。
send_safety_check() 会在发送前递归校验断言这一点。
"""
import json
from typing import Dict, List, Optional

import pandas as pd

# 命中即视为疑似身份信息字段名，发送前替换为「字段A、字段B…」
# 说明：只覆盖身份标识与联系方式类字段；年龄、性别等分析必需的变量名保留，
# 否则会显著削弱 AI 对方法的判断质量（它们单独并不构成身份识别）。
SENSITIVE_HINTS = (
    '姓名', '名字', '患者', '病人', '身份证', '证件', '出生', '住址', '地址',
    '电话', '手机', '邮箱', '微信', '联系人', '家属', '籍贯', '单位', '工作', '职业',
    '住院号', '病历号', '门诊号', '病案号', '社保', '医保', '银行卡', '帐号', '账号',
    'id', '编号', '序号',
)

ROLE_LABELS = {
    'identifier': '标识列（唯一值占比高，不参与统计）',
    'time_fields': '时间字段',
    'wide_value_columns': '宽表时间列（月份/季度/年份在列上）',
    'grouping': '分组变量（2–10 类）',
    'outcome_binary': '二分类结局候选',
    'count': '计数型指标（例数/次数）',
    'rate': '率/密度型指标',
    'metric_column': '指标名维度列（指标名在取值里）',
    'denominator': '强度分母候选（总例数/留置日等）',
    'duration': '随访或时长型数值',
    'paired': '配对列（前后/左右/入院出院）',
    'numeric': '数值变量',
    'categorical': '分类变量',
    'binary': '二分类变量',
}

MAX_FIELDS = 80


def is_sensitive_name(name) -> bool:
    """字段名是否疑似包含身份信息。"""
    lower = str(name).lower()
    return any(hint in lower for hint in SENSITIVE_HINTS)


def build_alias_map(columns, redact: bool = True) -> Dict[str, str]:
    """为敏感字段名生成别名（字段A、字段B…）。redact=False 时返回空映射。"""
    if not redact:
        return {}
    mapping: Dict[str, str] = {}
    index = 0
    for name in columns:
        if is_sensitive_name(name):
            mapping[name] = f'字段{chr(ord("A") + index)}'
            index += 1
    return mapping


def _meta_get(meta, key, default=None):
    """兼容 FieldMeta 实例与普通 dict 两种元数据形式。"""
    if meta is None:
        return default
    if isinstance(meta, dict):
        return meta.get(key, default)
    return getattr(meta, key, default)


def build_structure_payload(df: pd.DataFrame, profile: Optional[Dict],
                            fields: Optional[Dict] = None,
                            redact: bool = True) -> Dict:
    """构造 L1 结构摘要（不含个体数据与数值统计）。"""
    profile = profile or {}
    roles = profile.get('roles') or {}
    fields = fields or {}
    alias = build_alias_map(list(df.columns), redact=redact)

    field_items: List[Dict] = []
    for name in list(df.columns)[:MAX_FIELDS]:
        series = df[name]
        meta = fields.get(name)
        dtype = _meta_get(meta, 'dtype') or str(series.dtype)
        unique_count = _meta_get(meta, 'n_unique')
        if unique_count is None:
            unique_count = int(series.nunique(dropna=True))
        missing_pct = _meta_get(meta, 'missing_pct')
        if missing_pct is None:
            missing_pct = float(series.isna().mean() * 100)
        display = alias.get(name, str(name))
        field_items.append({
            '字段': display,
            '类型': str(dtype),
            '唯一值数': int(unique_count),
            '缺失率': f'{float(missing_pct):.1f}%',
            '已脱敏': display != str(name),
        })

    role_items: Dict[str, List[str]] = {}
    for key, values in roles.items():
        if not values:
            continue
        label = ROLE_LABELS.get(key, key)
        shown: List[str] = []
        for value in values:
            if isinstance(value, (tuple, list)) and len(value) == 2:
                shown.append(' – '.join(str(alias.get(v, v)) for v in value))
            else:
                shown.append(str(alias.get(value, value)))
        role_items[label] = shown

    payload = {
        '样本量': int(len(df)),
        '字段数': int(len(df.columns)),
        '是否宽表（时间点在列上）': bool(len(roles.get('wide_value_columns') or []) >= 3),
        '时间维度': ('有（可做趋势与同比环比）' if profile.get('has_time_series')
                 else ('仅日期字段（可做时间分组，不能做趋势）' if profile.get('has_date_field')
                       else '无')),
        '字段清单': field_items,
        '结构角色': role_items,
        '本地规则引擎推荐': [
            {
                '分析': item.get('title'),
                '是否可做': bool(item.get('ready')),
                '判据': item.get('evidence') or '',
                '未满足条件': list(item.get('blockers') or []),
                '提示': item.get('hint') or '',
            }
            for item in (profile.get('recommendations') or [])
        ],
        '能力缺口': [
            {
                '分析': gap.get('title'),
                '结构是否已满足': bool(gap.get('structure_ready')),
                '说明': gap.get('why') or '',
                '建议动作': gap.get('action') or '',
            }
            for gap in (profile.get('gaps') or [])
        ],
        '脱敏说明': ('疑似包含身份信息的字段名已替换为「字段A、字段B…」，'
                  '字段类型、唯一值数与缺失率保持不变。' if alias
                  else '未对字段名做替换（可在侧边栏开启脱敏）。'),
        '数据边界': '本摘要只含数据结构与规则引擎判据，不含任何个体数据行，也不含均值/频数等数值统计。',
    }
    if len(df.columns) > MAX_FIELDS:
        payload['字段清单省略说明'] = (f'字段过多，仅列出前 {MAX_FIELDS} 个，'
                                 f'其余 {len(df.columns) - MAX_FIELDS} 个未列出。')

    send_safety_check(payload)
    return payload


def send_safety_check(node, path: str = 'payload') -> None:
    """
    发送前的硬性自检：摘要里只允许出现 str/int/float/bool/None/列表/字典。
    一旦混入 DataFrame、Series、ndarray 等（可能携带个体数据），立即阻止发送。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            send_safety_check(value, f'{path}.{key}')
        return
    if isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            send_safety_check(value, f'{path}[{index}]')
        return
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    raise ValueError(
        f'摘要中出现非结构化内容（{path}: {type(node).__name__}），'
        '为避免个体数据外传，已阻止本次发送。')


def build_advice_payload(structure_payload: Dict, question: str = '',
                         design: str = '未指定') -> Dict:
    """
    把（可选）研究问题与结构摘要打包成分析建议请求（依然不含个体数据）。

    研究问题为空是**正常输入**而不是错误：此时 AI 回答「这份数据本身能做什么」。
    请求说明也随之不同 —— 这正是原本「解读」与「方案」两个入口的差别所在。
    """
    question = (question or '').strip()
    if question:
        note = ('请结合数据摘要判断该研究问题是否可做，并给出分析路线、'
                '前提检查清单、变量与数据准备、解读口径与方法学表述。')
    else:
        note = ('用户没有提出研究问题，请按「这份数据本身能支撑什么分析」作答：'
                '这份数据是什么、现在能做什么、暂时做不了什么、'
                '数据质量与改造建议、建议的第一步。')
    payload = {
        '研究问题': question,
        '研究设计': (design or '未指定').strip(),
        '数据摘要': structure_payload,
        '请求说明': note,
    }
    send_safety_check(payload)
    return payload


def summarize_advice_payload(payload: Dict) -> str:
    question = str(payload.get('研究问题') or '')
    if question:
        short = question if len(question) <= 40 else question[:40] + '…'
        head = f'将发送：研究问题「{short}」'
    else:
        head = '将发送：结构摘要（未填研究问题，AI 将按「这份数据能做什么」作答）'
    design = payload.get('研究设计') or '未指定'
    return (head + f'、研究设计「{design}」，以及上面那份结构摘要。'
            '不含任何个体数据行与数值统计。')


def build_multisheet_payload(brief: Dict, *, question: str = '',
                             design: str = '未指定') -> Dict:
    """
    多表解读的请求载荷。

    这是单表结构摘要的「多表版」：每张表的字段清单与量级判断，加上程序识别出的
    表间关系（哪两张表能按哪列关联、1:1 还是 1:N、能否直接挂接）。
    隐私边界与单表摘要完全一致 —— 只有字段名、类型、唯一值数、缺失率，
    不含任何个体数据行，也不含均值 / 频数这类数值统计。
    """
    from modules.multi_sheet import build_brief_payload

    payload = {
        '请求说明': ('这个 Excel 里有多个工作表，每个维度一张表，用户可能并不清楚'
                 '这些数据能做什么分析。请判断这是一批什么数据、表与表之间该怎么关联、'
                 '能回答哪些研究问题、以及建议的第一步。'),
        '工作簿': build_brief_payload(brief),
        '研究问题': str(question or '').strip(),
        '研究设计': str(design or '未指定').strip(),
    }
    send_safety_check(payload)
    return payload


def summarize_multisheet_payload(payload: Dict) -> str:
    workbook = payload.get('工作簿') or {}
    sheets = workbook.get('工作表清单') or []
    relations = workbook.get('程序识别出的表间关系') or []
    text = (f'将发送：{len(sheets)} 张表的结构清单（字段名、类型、唯一值数、缺失率）'
            f'与 {len(relations)} 组表间关系')
    question = str(payload.get('研究问题') or '')
    if question:
        short = question if len(question) <= 30 else question[:30] + '…'
        text += f'，以及研究问题「{short}」'
    return text + '。不含任何个体数据行与数值统计。'


def payload_to_json(payload: Dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def summarize_payload(payload: Dict) -> str:
    """给界面显示的一句话摘要，让用户明确知道将要发送什么。"""
    fields = payload.get('字段清单') or []
    redacted = sum(1 for item in fields if item.get('已脱敏'))
    parts = [
        f"{payload.get('样本量', 0)} 行 × {payload.get('字段数', 0)} 列的结构信息",
        f"字段清单 {len(fields)} 项（类型 / 唯一值数 / 缺失率）",
        f"结构角色 {len(payload.get('结构角色') or {})} 类",
        f"规则引擎判据 {len(payload.get('本地规则引擎推荐') or [])} 条",
    ]
    text = '将发送：' + '、'.join(parts)
    if redacted:
        text += f'；其中 {redacted} 个敏感字段名已脱敏'
    return text + '。不含任何个体数据行与数值统计。'
