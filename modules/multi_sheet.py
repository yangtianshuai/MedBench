# -*- coding: utf-8 -*-
"""
多表（多 Sheet）清单与关系识别。

临床数据最常见的形态是「一个 Excel，每个维度一张表」—— 基本信息、检验指标、
用药记录、结局各占一个 Sheet，靠住院号之类的标识对应。这类数据有两个特点：
用户往往不知道该从哪下手，而各表与对象的关系**并不一样**（有的是一对一，
有的是一对多）。

本模块只做**确定性的结构判断**，不猜临床含义：
- 每张表是什么量级、哪些列可以当主键、属于对象级还是明细级
- 表与表之间能不能关联、按哪列关联、是一对一还是一对多

判断结果供两处使用：给 AI 做「这批表能研究什么」的解读；以及关联向导的预填与校验。
输出只含结构信息 —— 字段名、类型、唯一值数、缺失率，**不含任何个体数据行**。
"""
from typing import Dict, Iterable, List, Optional

import pandas as pd

# 像标识的列名。命中只是加分项，仍要满足唯一性才可能成为主键。
KEY_NAME_HINTS = ('住院号', '病案号', '病历号', '病例号', '患者', '登记号',
                  '门诊号', '序号', '编号', '标识', 'id', 'uid', 'pid', 'code')
TIME_NAME_HINTS = ('日期', '时间', 'date', 'time', '年', '月', '随访')
# 数值型但天生不该当主键的列名（「年龄」唯一时也不该被当成对象标识）
MEASURE_HINTS = ('年龄', '天数', '时长', '评分', '分数', '剂量', '体重', '身高',
                 '率', '比例', '比值', 'count', 'score', 'age', 'days')

KEY_SCORE_THRESHOLD = 3


def _text(value) -> str:
    return str(value or '').strip()


def _hit(name: str, hints: Iterable[str]) -> bool:
    low = _text(name).lower()
    return any(hint.lower() in low for hint in hints)


def dtype_label(series: pd.Series) -> str:
    if pd.api.types.is_datetime64_any_dtype(series):
        return 'datetime'
    if pd.api.types.is_bool_dtype(series):
        return 'binary'
    if pd.api.types.is_numeric_dtype(series):
        return 'numeric'
    return 'text'


def describe_column(series: pd.Series) -> Dict:
    """单列的结构指标（唯一性、缺失、类型）。"""
    total = int(len(series))
    missing = int(series.isna().sum())
    valid = total - missing
    unique = int(series.nunique(dropna=True)) if valid else 0
    return {
        'n': total,
        'missing': missing,
        'missing_pct': round(missing / total * 100, 2) if total else 0.0,
        'unique': unique,
        'unique_ratio': round(unique / valid, 4) if valid else 0.0,
        'dtype': dtype_label(series),
        'numeric': bool(pd.api.types.is_numeric_dtype(series)),
        'float_like': bool(pd.api.types.is_float_dtype(series)),
    }


def key_score(name: str, info: Dict) -> int:
    """
    这一列像不像主键。分数越低越不像，< KEY_SCORE_THRESHOLD 不作为候选。

    唯一性只是必要条件：年龄在某个样本里也可能恰好每人不同，但它不是标识。
    所以还要看列名语义，并把「年龄 / 天数 / 评分」这类测量列主动扣分。
    """
    score = 0
    if info['unique_ratio'] >= 1.0 and info['missing'] == 0:
        score += 3
    elif info['unique_ratio'] >= 0.98:
        score += 1
    if _hit(name, KEY_NAME_HINTS):
        score += 2
    if _hit(name, MEASURE_HINTS):
        score -= 3
    if info['dtype'] == 'datetime':
        score -= 3        # 日期即使每条都不同，也不是「对象标识」
    if info['float_like'] and info['unique_ratio'] < 1.0:
        score -= 1        # 小数且会重复 → 测量值
    if info['numeric'] and info['unique_ratio'] >= 1.0:
        score += 1        # 连续编号（100001、100002…）
    return score


LINK_SCORE_THRESHOLD = 3


def link_score(name: str, info: Dict) -> int:
    """
    这一列能不能用来与其他表关联（外键），**不要求唯一**。

    与主键的区别很关键：明细表里「住院号」重复出现是正常的 —— 它标识的是
    「这条记录属于哪个对象」。若照主键标准要求唯一，检验表、用药记录这类表
    就会一个键都识别不出来，两张明细表之间的关系也就永远判不出来。
    """
    if info['unique'] == 0 or info['n'] == 0:
        return 0
    score = 0
    if _hit(name, KEY_NAME_HINTS):
        score += 3
    if _hit(name, MEASURE_HINTS):
        score -= 3
    if info['dtype'] == 'datetime':
        score -= 3
    ratio = info['unique_ratio']
    if ratio >= 1.0 and info['missing'] == 0:
        score += 2        # 唯一 → 对象级表的主键，天然可作关联键
    elif 0.01 <= ratio < 1.0:
        score += 1        # 有重复 → 典型的外键形态
    else:
        score -= 2        # 取值几乎恒定，没有区分度
    return score


def describe_sheet(frame: pd.DataFrame, sheet_name: str = '') -> Dict:
    """一张表的结构清单。frame 只在本地使用，不进入给 AI 的载荷。"""
    columns: Dict[str, Dict] = {}
    keys: List[Dict] = []
    links: List[Dict] = []
    for column in frame.columns:
        name = str(column)
        info = describe_column(frame[column])
        columns[name] = dict(info, name=name)
        score = key_score(name, info)
        if score >= KEY_SCORE_THRESHOLD:
            keys.append({'column': name, 'score': score,
                         'unique': info['unique'], 'missing': info['missing']})
        link = link_score(name, info)
        if link >= LINK_SCORE_THRESHOLD:
            links.append({'column': name, 'score': link,
                          'unique': info['unique'], 'missing': info['missing']})
    keys.sort(key=lambda item: (-item['score'], item['column']))
    links.sort(key=lambda item: (-item['score'], item['column']))

    rows = int(len(frame))
    primary = keys[0] if keys else None
    if primary and primary['unique'] >= rows:
        level = '对象级（一行一个对象）'
    elif keys or links:
        # 有标识列但不唯一 —— 正是明细表的形态（一个对象多行）
        level = '明细级（一个对象多行）'
    else:
        level = '未识别出主键'

    # 疑似时间列：明细表通常有「日期」这类列，做重复测量（LMM / GEE）要用
    time_candidates = [name for name, info in columns.items()
                       if info['dtype'] == 'datetime' or _hit(name, TIME_NAME_HINTS)]

    return {
        'sheet': _text(sheet_name) or '(未命名)',
        'rows': rows,
        'columns': int(len(frame.columns)),
        'level': level,
        'key_candidates': keys[:3],
        'link_candidates': links[:3],
        'time_candidates': time_candidates[:3],
        'fields': [{'name': name, 'type': info['dtype'], 'unique': info['unique'],
                    'missing_pct': info['missing_pct']}
                   for name, info in columns.items()],
        '_columns': columns,      # 内部用：关系判定需要全部列的唯一性
    }


def relate_sheets(left: Dict, right: Dict) -> List[Dict]:
    """
    两张表能按哪些同名列关联、是什么关系。

    同名不等于可关联：两张表都叫「年龄」的列并不能把患者对起来，两表都有一列
    「性别」更不行。因此要求至少有一方该列像**关联键**（标识列，可以是重复的），
    再从唯一性推出关系方向。
    """
    shared = [name for name in left.get('_columns') or {} if name in right.get('_columns') or {}]
    results: List[Dict] = []
    for name in shared:
        li = left['_columns'][name]
        ri = right['_columns'][name]
        if li['unique'] == 0 or ri['unique'] == 0:
            continue
        left_link = link_score(name, li) >= LINK_SCORE_THRESHOLD
        right_link = link_score(name, ri) >= LINK_SCORE_THRESHOLD
        if not (left_link or right_link):
            continue
        left_uniq = li['unique'] >= left['rows']
        right_uniq = ri['unique'] >= right['rows']
        if left_uniq and right_uniq:
            relation = '1:1（两边都是一行一个对象，可直接挂接）'
        elif left_uniq:
            relation = f'1:N（{right["sheet"]} 一个对象多行，需先聚合）'
        elif right_uniq:
            relation = f'N:1（{left["sheet"]} 一个对象多行，需先聚合）'
        else:
            relation = 'N:N（两边都是一个对象多行，不能直接挂接）'
        results.append({
            'columns': [name],
            'relation': relation,
            'safe_to_join': bool(left_uniq and right_uniq),
            'left_unique': li['unique'],
            'right_unique': ri['unique'],
            'left_rows': int(left['rows']),
            'right_rows': int(right['rows']),
            'left_missing_pct': li['missing_pct'],
            'right_missing_pct': ri['missing_pct'],
        })
    # 能安全挂接的排前面
    results.sort(key=lambda item: (not item['safe_to_join'], item['columns'][0]))
    return results


def analyze_workbook(file_bytes: bytes, settings_map: Optional[Dict] = None,
                     skip: Optional[List[str]] = None) -> Dict:
    """
    读入工作簿的全部 Sheet，给出结构清单与表间关系。

    settings_map  可为每张表单独指定读取设置（大标题行 / 合并单元格），
                  未指定的用 header_rows=1
    skip          用户手动排除的表名
    """
    import io

    from modules.data_loader import DataLoader

    settings_map = settings_map or {}
    skipped = set(skip or [])

    listed = DataLoader.list_sheets(io.BytesIO(file_bytes))
    sheets: List[Dict] = []
    failed: List[Dict] = []
    for name in listed:
        if name in skipped:
            continue
        settings = dict(settings_map.get(name) or {})
        try:
            frame = DataLoader.read_sheet(io.BytesIO(file_bytes), name, **settings)
        except Exception as error:                     # noqa: BLE001
            failed.append({'sheet': name, 'reason': str(error)[:120]})
            continue
        if len(frame) == 0:
            failed.append({'sheet': name, 'reason': '没有有效数据'})
            continue
        sheets.append(describe_sheet(frame, name))

    relations: List[Dict] = []
    for index in range(len(sheets)):
        for other in range(index + 1, len(sheets)):
            for item in relate_sheets(sheets[index], sheets[other]):
                relations.append({'left': sheets[index]['sheet'],
                                  'right': sheets[other]['sheet'], **item})

    return {
        'sheets': sheets,
        'relations': relations,
        'skipped': sorted(skipped),
        'failed': failed,
        'available': listed,
    }


def describe_level(brief: Dict, sheet_name: str) -> str:
    for item in brief.get('sheets') or []:
        if item['sheet'] == sheet_name:
            return item['level']
    return ''


def relations_of(brief: Dict, sheet_name: str) -> List[Dict]:
    """与某张表有关的所有关系（左或右）。"""
    return [item for item in brief.get('relations') or []
            if sheet_name in (item.get('left'), item.get('right'))]


def build_brief_payload(brief: Dict, *, max_fields: int = 40) -> Dict:
    """
    给 AI 的多表结构清单。

    隐私边界与单表结构摘要一致：字段名、类型、唯一值数、缺失率，
    不含任何个体数据行，也不含均值 / 频数这类数值统计。
    """
    sheets = []
    for item in brief.get('sheets') or []:
        sheets.append({
            '工作表': item['sheet'],
            '规模': f"{item['rows']} 行 × {item['columns']} 列",
            '量级判断': item['level'],
            '疑似主键': [key['column'] for key in item.get('key_candidates') or []],
            '疑似关联键（可重复，用于与其他表挂接）':
                [link['column'] for link in item.get('link_candidates') or []],
            '疑似时间列': item.get('time_candidates') or [],
            '字段': [{'名': field['name'], '类型': field['type'],
                      '唯一值数': field['unique'], '缺失率': field['missing_pct']}
                     for field in item['fields'][:max_fields]],
            '字段数超限说明': (f"仅列出前 {max_fields} 个字段"
                              if len(item['fields']) > max_fields else ''),
        })
    return {
        '工作表数': len(sheets),
        '工作表清单': sheets,
        '程序识别出的表间关系': [
            {'表A': item['left'], '表B': item['right'],
             '关联列': item['columns'], '关系': item['relation'],
             '可否直接挂接': item['safe_to_join'],
             'A唯一值数': item['left_unique'], 'B唯一值数': item['right_unique'],
             'A行数': item['left_rows'], 'B行数': item['right_rows']}
            for item in brief.get('relations') or []],
        '无法读取的表': brief.get('failed') or [],
        '说明': '以上只有表结构与关系信息，不含任何个体数据行与数值统计。'
                '字段唯一值数用于判断能否作主键，不是对数据内容的统计。',
    }


def summarize_brief(brief: Dict) -> str:
    """给界面用的一句话摘要。"""
    sheets = brief.get('sheets') or []
    if not sheets:
        return '没有可分析的工作表。'
    parts = [f'共 {len(sheets)} 张表（' +
             '、'.join(f"{s['sheet']} {s['rows']}×{s['columns']}" for s in sheets[:4]) +
             ('…' if len(sheets) > 4 else '') + '）']
    keyed = [s for s in sheets if s.get('key_candidates')]
    parts.append(f'{len(keyed)} 张识别出疑似主键')
    relations = brief.get('relations') or []
    safe = [r for r in relations if r.get('safe_to_join')]
    if relations:
        parts.append(f'可关联 {len(relations)} 组（其中 {len(safe)} 组可直接挂接）')
    else:
        parts.append('未发现可关联的表（没有共同的主键列）')
    return '；'.join(parts) + '。'


# ==================== 关联执行（第③段） ====================

def _agg_funcs():
    """聚合方式的实现。any 是「该对象在这列上是否有过记录」，用于生成 0/1 暴露列。"""
    return {
        'first': lambda series: (series.dropna().iloc[0]
                                 if series.notna().any() else None),
        'last': lambda series: (series.dropna().iloc[-1]
                                if series.notna().any() else None),
        'max': 'max',
        'min': 'min',
        'mean': 'mean',
        'sum': 'sum',
        'count': 'count',
        'any': lambda series: int(series.notna().any()),
    }


# 只能作用于数值列的聚合方式（对文本列求和会把字符串拼起来）
NUMERIC_ONLY = ('mean', 'sum')


def aggregate_to_key(frame: pd.DataFrame, key_col: str, aggregations: Dict,
                     problems: Optional[List[str]] = None) -> pd.DataFrame:
    """
    把明细表按关联键压成「一行一个对象」。

    **没有指定聚合方式的列不会进入结果**，而不是默认取均值 —— 对「用药天数」
    这类累加指标，默认平均是错的；宁可留空让人补，也不产出错误列。
    """
    problems = problems if problems is not None else []
    if key_col not in frame.columns:
        problems.append(f'工作表里没有关联列「{key_col}」')
        return frame

    funcs = _agg_funcs()
    used: Dict[str, object] = {}
    for column, how in (aggregations or {}).items():
        if column not in frame.columns:
            problems.append(f'聚合方案里的列「{column}」在表中不存在，已忽略')
            continue
        if how not in funcs:
            problems.append(f'不支持的聚合方式「{how}」（列 {column}），已忽略')
            continue
        if how in NUMERIC_ONLY and not pd.api.types.is_numeric_dtype(frame[column]):
            problems.append(f'「{column}」不是数值列，不能做 {how} 聚合，已忽略')
            continue
        used[column] = funcs[how]

    if not used:
        problems.append('没有任何可执行的聚合方式 —— 一个对象多行的表必须先聚合')
        return frame

    grouped = (frame.groupby(key_col, dropna=False, sort=False)
               .agg(used).reset_index())
    return grouped


def join_one_table(base: pd.DataFrame, extra: pd.DataFrame, key_col: str,
                   aggregations: Dict, *, relation: str = '',
                   base_sheet: str = '', extra_sheet: str = '') -> Dict:
    """
    把一个维度表挂到主表上，返回 (挂接后的表, 报告)。

    三道检查，为的是「绝不静默产出放大的样本量」：
      1. 关联列两边都要有，类型要能对上
      2. 待挂表若一个对象多行（明细行数 > 唯一键数），必须有聚合方式，否则拒绝执行
      3. **挂接后行数必须与主表一致** —— 一旦变多，说明还有未处理的重复键，
         这时宁可报错，也不产出样本量虚增的数据（这种错误肉眼看不出来）
    """
    report = {'base_sheet': base_sheet, 'extra_sheet': extra_sheet,
              'key': key_col, 'ok': False, 'problems': [],
              'rows_before': int(len(base)), 'rows_after': 0,
              'matched': 0, 'unmatched': 0, 'added_columns': [],
              'aggregated': {}}
    problems = report['problems']

    if key_col not in base.columns:
        problems.append(f'主表里没有关联列「{key_col}」')
        return base, report
    if key_col not in extra.columns:
        problems.append(f'待挂表里没有关联列「{key_col}」')
        return base, report

    extra_rows = int(len(extra))
    extra_keys = int(extra[key_col].nunique(dropna=True))
    need_aggregate = extra_keys > 0 and extra_rows > extra_keys

    working = extra.copy()
    if need_aggregate:
        if not aggregations:
            problems.append(
                f'「{extra_sheet}」里一个对象有多行（{extra_rows} 行 / '
                f'{extra_keys} 个对象），但没有给出任何聚合方式 —— '
                '直接挂接会让行数成倍膨胀、样本量虚增，已拒绝执行')
            return base, report
        working = aggregate_to_key(working, key_col, aggregations, problems)
        report['aggregated'] = dict(aggregations)

    working = working.drop_duplicates(subset=[key_col], keep='first')
    key_set = set(working[key_col].dropna().tolist())
    report['matched'] = int(base[key_col].isin(key_set).sum())

    # 待挂列与前缀，避免与主表列重名（同名时 pandas 会加 _x/_y 后缀，很难读）。
    # 关联列本身两边同名，所以用 on= 合并 —— 结果里只保留一列，不会多出一份重复的键。
    renamed = {}
    for column in working.columns:
        if column == key_col:
            continue
        new_name = column
        if new_name in base.columns:
            new_name = f'{extra_sheet}·{column}'
        renamed[column] = new_name
    merged = base.merge(working.rename(columns=renamed), on=key_col, how='left')

    report['rows_after'] = int(len(merged))
    report['unmatched'] = int(len(base) - report['matched'])
    report['added_columns'] = [name for name in renamed.values()
                              if name not in base.columns]

    if len(merged) != len(base):
        problems.append(
            f'挂接后行数从 {len(base)} 变成了 {len(merged)} —— '
            f'说明「{extra_sheet}」里同一个对象仍有重复的关联值，'
            '聚合没覆盖到，已撤销这次挂接。请检查关联列本身是否唯一')
        return base, report

    if report['unmatched']:
        problems.append(
            f'{report["unmatched"]} 个对象在「{extra_sheet}」里没有匹配到记录，'
            '挂接后这些行该列为空（请确认这是预期内的）')

    report['ok'] = True
    return merged, report


def apply_join_plan(file_bytes: bytes, joins: List[Dict],
                    settings_map: Optional[Dict] = None,
                    skip: Optional[List[str]] = None) -> tuple:
    """
    按方案执行多表关联，返回 (结果表, 各步报告)。

    主表的确定：以方案里第一条的 base 为准。后续步骤的 base 只能是主表、
    或前面已经挂上来的表（相当于链式关联）；否则跳过并说明。
    """
    frames = {item['sheet']: item['frame']
              for item in sheet_frame(file_bytes, settings_map, skip)}
    reports: List[Dict] = []
    if not joins:
        return None, [{'ok': False, 'problems': ['关联方案是空的'], 'base_sheet': '',
                       'extra_sheet': '', 'key': '', 'rows_before': 0,
                       'rows_after': 0, 'matched': 0, 'unmatched': 0,
                       'added_columns': [], 'aggregated': {}}]

    base_sheet = joins[0].get('base') or ''
    if base_sheet not in frames:
        return None, [{'ok': False, 'problems': [f'找不到主表「{base_sheet}」'],
                       'base_sheet': base_sheet, 'extra_sheet': '', 'key': '',
                       'rows_before': 0, 'rows_after': 0, 'matched': 0,
                       'unmatched': 0, 'added_columns': [], 'aggregated': {}}]

    result = frames[base_sheet].copy()
    attached = {base_sheet}
    for item in joins:
        base_name = item.get('base') or ''
        extra_name = item.get('extra') or ''
        if base_name not in attached:
            reports.append({
                'base_sheet': base_name, 'extra_sheet': extra_name,
                'key': item.get('key') or '', 'ok': False,
                'problems': [f'「{base_name}」既不是主表，也不是前面已挂上来的表，已跳过'],
                'rows_before': int(len(result)), 'rows_after': int(len(result)),
                'matched': 0, 'unmatched': 0, 'added_columns': [],
                'aggregated': {}})
            continue
        if extra_name not in frames:
            reports.append({
                'base_sheet': base_name, 'extra_sheet': extra_name,
                'key': item.get('key') or '', 'ok': False,
                'problems': [f'找不到工作表「{extra_name}」，已跳过'],
                'rows_before': int(len(result)), 'rows_after': int(len(result)),
                'matched': 0, 'unmatched': 0, 'added_columns': [],
                'aggregated': {}})
            continue
        if extra_name in attached:
            reports.append({
                'base_sheet': base_name, 'extra_sheet': extra_name,
                'key': item.get('key') or '', 'ok': False,
                'problems': [f'「{extra_name}」已经挂过了，跳过重复挂接'],
                'rows_before': int(len(result)), 'rows_after': int(len(result)),
                'matched': 0, 'unmatched': 0, 'added_columns': [],
                'aggregated': {}})
            continue

        merged, report = join_one_table(
            result, frames[extra_name], item.get('key') or '',
            item.get('aggregations') or {},
            relation=item.get('relation') or '',
            base_sheet=base_name, extra_sheet=extra_name)
        reports.append(report)
        if report['ok']:
            result = merged
            attached.add(extra_name)

    return result, reports


def summarize_join_reports(reports: List[Dict], base_rows: int = 0) -> str:
    """给界面用的一句话结论。"""
    reports = reports or []
    ok = [item for item in reports if item.get('ok')]
    failed = [item for item in reports if not item.get('ok')]
    if not ok and not failed:
        return '没有可执行的关联步骤。'
    text = f'完成 {len(ok)} 步关联'
    if base_rows:
        text += f'，结果 {base_rows} 行'
    if failed:
        text += f'；{len(failed)} 步未执行'
    return text + '。'


def sheet_frame(file_bytes: bytes, settings_map: Optional[Dict] = None,
                skip: Optional[List[str]] = None) -> List[Dict]:
    """按表读出实际数据（第③段执行关联时用），返回 [{sheet, frame, settings}]。"""
    import io

    from modules.data_loader import DataLoader

    settings_map = settings_map or {}
    skipped = set(skip or [])
    result = []
    for name in DataLoader.list_sheets(io.BytesIO(file_bytes)):
        if name in skipped:
            continue
        settings = dict(settings_map.get(name) or {})
        try:
            frame = DataLoader.read_sheet(io.BytesIO(file_bytes), name, **settings)
        except Exception:                              # noqa: BLE001
            continue
        if len(frame):
            result.append({'sheet': name, 'frame': frame, 'settings': settings})
    return result
