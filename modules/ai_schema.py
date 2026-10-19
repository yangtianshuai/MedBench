"""
表格结构诊断：让 AI 看懂「不合规」的表格，并给出可执行的整理方案。

背景：实际拿到的 Excel 往往不是分析型表 —— 顶部有合并大标题、表头是多级的、
指标名在行上、时间在列上、末尾还有备注行。这类表直接导入后变量全部错位，
分析页自然没有可选项，而用户很难自己判断该怎么改。

本模块做三件事：
    1. **构造安全的诊断载荷**：默认只发送「类型骨架」，AI 据此判断结构；
    2. **校验 AI 给出的整理方案**：只接受白名单内、参数合法的操作；
    3. **执行**：作用域分两类 —— 重新解析（跳过行/表头行）与数据变换（宽转长等）。

隐私设计（本模块的核心约束）：
    - 默认只发送**类型骨架**：每个单元格替换为「文本 / 数字 / 日期 / 空 / 布尔」标记，
      **不含任何真实值**。骨架足以分辨大标题行、表头行、单位行、数据行与指标名列。
    - 可选发送**前若干行原样**（字段名自动脱敏 + 逐字预览），用于识别单位行、
      缺失标记等细节；该选项默认关闭，必须由用户显式勾选。
"""
import math
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd

SKELETON_ROWS = 8
SKELETON_FIELDS = 40

# 整理操作白名单。scope 决定执行方式：
#   reparse   —— 改变读取方式，需要重新解析原始文件
#   transform —— 对当前数据做变换
ACTION_SPECS: Dict[str, Dict] = {
    'skip_rows': {
        'label': '跳过开头行数', 'scope': 'reparse', 'params': ('value', int),
        'hint': '表格顶部的大标题行数',
    },
    'header_rows': {
        'label': '表头行数', 'scope': 'reparse', 'params': ('value', int),
        'hint': '1 = 首行是字段名；2 = 前两行合并成字段名',
    },
    'fill_merged': {
        'label': '合并单元格向下填充', 'scope': 'reparse', 'params': ('value', bool),
        'hint': '纵向合并单元格读入后为空白，勾选后沿用上一行',
    },
    'wide_to_long': {
        'label': '宽表转长表', 'scope': 'transform',
        'params': ('id_columns', list),
        'params_extra': ('value_columns', list),
        'hint': '把横向铺开的列（1月…12月）折叠成「名称 + 数值」两行结构',
    },
    'drop_rows': {
        'label': '删除指定行', 'scope': 'transform',
        'params': ('column', str), 'params_extra': ('match', str),
        'hint': '删除备注行、合计行等非数据行',
    },
    'drop_columns': {
        'label': '删除列', 'scope': 'transform', 'params': ('columns', list),
        'hint': '删除空列或与本次分析无关的列',
    },
    'rename_columns': {
        'label': '重命名列', 'scope': 'transform', 'params': ('mapping', dict),
        'hint': '把错位的列名改成正确的字段名',
    },
    'set_types': {
        'label': '指定变量类型', 'scope': 'transform', 'params': ('mapping', dict),
        'hint': '把被误判的列指定为数值 / 分类 / 二分类 / 日期 / 文本',
    },
}

VALID_TYPES = ('numeric', 'categorical', 'binary', 'datetime', 'text')


def cell_kind(value) -> str:
    """单元格类型标记（不含真实值）。"""
    try:
        if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
            return '空'
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return '布尔'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return '空'
        return '数字'
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return '日期'
    text = str(value).strip()
    if not text:
        return '空'
    try:
        float(text.replace(',', '').replace('%', ''))
        return '数字'
    except ValueError:
        return '文本'


def build_schema_payload(frame, *, rows: int = SKELETON_ROWS,
                         include_values: bool = False, redact: bool = True,
                         extra_notes: Optional[List[str]] = None) -> Dict:
    """
    构造表格诊断载荷。

    默认（include_values=False）只含类型骨架，绝无真实值。
    """
    from modules.ai_context import build_alias_map, send_safety_check

    frame = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    shown_columns = [str(name) for name in frame.columns][:SKELETON_FIELDS]
    head = frame.head(int(rows))

    skeleton: List[List[str]] = []
    for _, row in head.iterrows():
        skeleton.append([cell_kind(row[name]) for name in frame.columns[:SKELETON_FIELDS]])

    payload: Dict = {
        '说明': ('这是一份「表格结构骨架」：每个单元格只标注类型'
                 '（文本 / 数字 / 日期 / 空 / 布尔），不含任何真实数值或文字内容。'
                 '请据此判断表格的排版结构。'),
        '行列数': [int(frame.shape[0]), int(frame.shape[1])],
        '列名': shown_columns,
        '前若干行的单元格类型': skeleton,
        '骨架读法': ('按行看：[全部是文本] 通常是表头行；[只有 1 个文本、其余为空] 通常是大标题行；'
                 '[文本 + 数字 + 数字…] 通常是数据行；'
                 '[文本, 数字, 数字] 且该行只有 1–2 行这种模式时，可能是单位行或说明行。'),
    }
    if extra_notes:
        payload['本地规则引擎的线索'] = [str(note) for note in extra_notes[:10]]

    if include_values:
        alias = build_alias_map(list(frame.columns), redact=redact)
        payload['列名（已按需脱敏）'] = [
            alias.get(name, str(name)) for name in frame.columns[:SKELETON_FIELDS]]
        sample: List[List[str]] = []
        for _, row in head.iterrows():
            sample.append([
                '' if (not isinstance(row[name], (list, dict)) and pd.isna(row[name]))
                else str(row[name])[:24]
                for name in frame.columns[:SKELETON_FIELDS]])
        payload[f'前 {int(rows)} 行原样（已按需脱敏）'] = sample
        payload['脱敏与风险提示'] = (
            '字段名已按需脱敏；单元格内容未做处理。'
            '如果表格里含身份证号、手机号、住址等，请不要开启「含样例行」。')

    send_safety_check(payload)
    return payload


def summarize_payload(payload: Dict) -> str:
    """界面用的一句话说明。"""
    rows, columns = (payload.get('行列数') or [0, 0])[:2]
    text = f'将发送：{rows} 行 × {columns} 列的结构骨架（单元格类型标记，不含真实值）'
    if '前 8 行原样（已按需脱敏）' in payload or any(
            key.startswith('前 ') and key.endswith('原样（已按需脱敏）') for key in payload):
        text += '，以及前若干行原样（已按需脱敏）'
    return text + '。'


def parse_actions(raw_actions, columns) -> Dict:
    """
    校验 AI 给出的整理方案，只保留白名单内且参数合法的操作。

    返回 {'valid': [...], 'rejected': [{'action':..., 'reason':...}]}
    """
    columns = [str(name) for name in (columns or [])]
    column_set = set(columns)
    valid: List[Dict] = []
    rejected: List[Dict] = []
    seen = set()

    for item in (raw_actions or [])[:12]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get('type') or '').strip()
        spec = ACTION_SPECS.get(kind)
        if spec is None:
            rejected.append({'action': kind or '(未指定)',
                             'reason': '不在支持的操作范围内'})
            continue
        if kind in seen and kind not in ('rename_columns', 'set_types'):
            rejected.append({'action': spec['label'], 'reason': '重复操作，已忽略'})
            continue

        params: Dict = {}
        problem = ''
        key, expected = spec['params'][0], spec['params'][1]
        value = item.get(key, item.get('value'))
        if expected is int:
            try:
                params[key] = max(0, int(value))
            except (TypeError, ValueError):
                problem = f'{key} 必须是整数'
        elif expected is bool:
            params[key] = bool(value)
        elif expected is str:
            params[key] = str(value or '').strip()
            if not params[key]:
                problem = f'{key} 不能为空'
            elif kind == 'drop_rows' and params[key] not in column_set:
                problem = f'列「{params[key]}」不在当前表格中'
        elif expected is list:
            raw_list = value if isinstance(value, (list, tuple)) else [value]
            items = [str(entry).strip() for entry in raw_list if str(entry or '').strip()]
            missing = [entry for entry in items if entry not in column_set]
            if missing:
                problem = '列不在当前表格中：' + '、'.join(missing[:3])
            elif not items:
                problem = f'{key} 不能为空'
            params[key] = items
        elif expected is dict:
            raw_map = value if isinstance(value, dict) else {}
            cleaned = {str(k).strip(): v for k, v in raw_map.items() if str(k or '').strip()}
            missing = [key_name for key_name in cleaned if key_name not in column_set]
            if missing:
                problem = '列不在当前表格中：' + '、'.join(missing[:3])
            elif not cleaned:
                problem = '没有有效的列名'
            params[key] = cleaned

        # 附加参数（如宽转长的值列）
        if not problem and 'params_extra' in spec:
            extra_key, extra_type = spec['params_extra']
            extra_value = item.get(extra_key)
            if extra_type is list:
                raw_list = extra_value if isinstance(extra_value, (list, tuple)) else []
                items = [str(entry).strip() for entry in raw_list if str(entry or '').strip()]
                if not items:
                    problem = f'{extra_key} 不能为空'
                elif kind == 'drop_rows':
                    params[extra_key] = str(extra_value or '')
                else:
                    missing = [entry for entry in items if entry not in column_set]
                    if missing:
                        problem = '列不在当前表格中：' + '、'.join(missing[:3])
                    else:
                        params[extra_key] = items
            else:
                params[extra_key] = str(extra_value or '').strip()

        if kind == 'set_types' and not problem:
            bad = [name for name, target in params['mapping'].items()
                   if str(target).strip().lower() not in VALID_TYPES]
            if bad:
                problem = '类型不在允许范围：' + '、'.join(bad[:3])

        if problem:
            rejected.append({'action': spec['label'], 'reason': problem})
            continue

        seen.add(kind)
        valid.append({
            'type': kind,
            'label': spec['label'],
            'scope': spec['scope'],
            'params': params,
            'why': str(item.get('why') or '')[:160],
        })

    return {'valid': valid, 'rejected': rejected}


def describe_actions(actions) -> str:
    """给界面用的一句话摘要。"""
    reparse = sum(1 for item in actions or [] if item.get('scope') == 'reparse')
    transform = sum(1 for item in actions or [] if item.get('scope') == 'transform')
    parts = []
    if reparse:
        parts.append(f'读取设置 {reparse} 项')
    if transform:
        parts.append(f'数据变换 {transform} 项')
    return '共 ' + '、'.join(parts) if parts else '没有可执行的操作'


# ==================== 整理 → 评估 → 下一步（闭环） ====================

def make_log_entry(action, *, rows: int = 0, columns: int = 0, at: str = '') -> Dict:
    """
    记录一次已执行的整理操作。

    闭环评估时，AI 看到的是「整理后的数据摘要」——但光有摘要它不知道数据是怎么变成
    现在这样的（比如某个字段消失是因为删列，还是因为改成了别的名字）。这份记录补上
    这段因果链，同时让用户能回看自己到底改过什么。
    """
    action = action or {}
    return {
        'label': str(action.get('label') or action.get('type') or ''),
        'type': str(action.get('type') or ''),
        'scope': str(action.get('scope') or ''),
        'params': {str(key): value for key, value in (action.get('params') or {}).items()},
        'why': str(action.get('why') or '')[:160],
        'at': str(at or ''),
        'rows_before': int(rows or 0),
        'cols_before': int(columns or 0),
    }


def summarize_log(log) -> str:
    """给界面用的整理历史摘要。"""
    log = list(log or [])
    if not log:
        return '尚未执行过整理操作。'
    labels = [str(item.get('label') or '') for item in log if item.get('label')]
    return f"已执行 {len(log)} 步整理：" + ' → '.join(labels[:6]) + \
           ('…' if len(labels) > 6 else '')


def build_closure_payload(structure_payload: Dict, action_log, *,
                          question: str = '', design: str = '未指定',
                          rows: int = 0, columns: int = 0) -> Dict:
    """
    构造「整理 → 评估 → 下一步」闭环的请求载荷。

    与普通结构摘要的区别：额外说明「刚刚做了哪些整理」「数据从什么变成什么」，
    这样 AI 才能判断**整理后的**数据够不够支撑某个分析，而不是凭原始印象作答。

    隐私边界与结构摘要完全一致：仍然不含任何个体数据行与数值统计。
    """
    from modules.ai_context import send_safety_check

    payload = {
        '请求说明': ('这份表格刚刚按建议完成了整理。请评估**整理后**的数据是否已达到分析条件、'
                 '现在可以做哪些分析、以及建议的第一步是什么。'),
        '整理动作历史': list(action_log or [])[:12],
        '整理后当前形态': f'{int(rows)} 行 × {int(columns)} 列',
        '研究问题': str(question or '').strip(),
        '研究设计': str(design or '未指定').strip(),
        '整理后的数据摘要': structure_payload,
    }
    send_safety_check(payload)
    return payload


def summarize_closure(payload: Dict) -> str:
    log = payload.get('整理动作历史') or []
    question = str(payload.get('研究问题') or '')
    parts = [f'整理动作 {len(log)} 步', f"当前形态 {payload.get('整理后当前形态', '')}"]
    text = '将发送：' + '、'.join(parts)
    if question:
        short = question if len(question) <= 30 else question[:30] + '…'
        text += f'（含研究问题「{short}」）'
    return text + '，以及整理后的结构摘要。不含任何个体数据行与数值统计。'
