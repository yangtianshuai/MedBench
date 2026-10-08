"""
分析台账

记录每一次实际产出过的分析（标题、结论摘要、是否含表格与图、时间），用于三件事：

1. 回溯「我到底做过哪些分析、大概是基于哪版数据」
2. 给「AI 写作」与「方法学段落」提供事实基础 —— 在此之前只能从报告章节的标题去猜
3. 导出成一张清单，应对「你做了哪些统计分析」这类审稿要求

与报告缓存刻意分开，原因有二：
  - 报告缓存在换数据时会整体清空（旧结果确实不再适用）；台账**保留**并插入
    「数据已更新」分隔标记，这样才能看出哪一段分析是基于哪一版数据做的
  - 台账只记标题与摘要，不存表格与图，体量很小，可以长期累积

条目形如：
    {'分析': 'Logistic 回归 - 死亡', '摘要': 'Pseudo R²=0.42, n=150',
     '类型': '回归与建模', '含表格': True, '含图': True, '时间': '10-06 11:40'}
"""
import hashlib
from typing import Dict, List, Optional

import pandas as pd

LEDGER_LIMIT = 300
BRIEF_LIMIT = 160

# 分析类型归类：用于界面分组、方法学统计
TYPE_RULES = (
    ('数据变更', ('—— 数据已更新',)),
    ('数据准备', ('插补', '填补', '宽表', '转长表', '变量类型', '清洗', '缺失模式', '脱敏')),
    ('基线特征', ('table 1', 'table1', '基线特征', '基线表')),
    ('描述性分析', ('描述统计', '描述性', '正态性', '频数', '分布')),
    ('相关分析', ('相关分析', 'pearson', 'spearman', '热图')),
    ('组间比较', ('两组比较', '组间比较', '差异比较', 'anova', 'kruskal', '卡方', 'fisher',
              'mann-whitney', '配对', 'wilcoxon', 't 检验')),
    ('一致性', ('kappa', 'icc', 'bland', '一致性')),
    ('多重比较', ('多重比较', 'fdr', 'tukey', 'bonferroni', 'holm')),
    ('重复测量', ('lmm', 'gee', '混合效应', '重复测量')),
    ('时间与质控', ('率与', '置信区间', 'spc', '控制图', '趋势', '环比', '同比',
                '时间序列', 'arima', 'holt')),
    ('因果推断', ('psm', '倾向性评分', 'iptw', '逆概率', '亚组', 'e-value', '敏感性分析')),
    ('回归与建模', ('回归', 'logistic', 'cox', 'lasso', '列线图', '校准', '决策曲线',
                '剂量-反应', '剂量反应', '截断值', 'roc', '验证')),
)


def classify(title) -> str:
    """按标题判断分析类型（用于台账分组）。"""
    lowered = str(title or '').lower()
    for label, hints in TYPE_RULES:
        if any(hint in lowered for hint in hints):
            return label
    return '其他'


def _brief(content, limit: int = BRIEF_LIMIT) -> str:
    """把结论摘要压成一行，便于在台账里横向阅读。"""
    text = ' '.join(str(content or '').split())
    if len(text) > limit:
        text = text[:limit] + '…'
    return text


def fingerprint(title, content) -> str:
    """条目指纹：用于「同一条分析只记一次」，因为缓存同步会在每轮运行都发生。"""
    raw = f'{title}|{_brief(content, 120)}'
    return hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]


def make_entry(*, title, content='', has_table=False, has_image=False,
               at='', kind='analysis', params=None) -> Dict:
    """构造一条台账记录。"""
    return {
        '分析': str(title or '').strip(),
        '摘要': _brief(content),
        '类型': classify(title),
        '含表格': bool(has_table),
        '含图': bool(has_image),
        '参数': {str(key): str(value) for key, value in (params or {}).items()},
        '时间': str(at or ''),
        '指纹': fingerprint(title, content),
        'kind': kind,
    }


def make_data_change(label='', at='') -> Dict:
    """数据变更分隔标记：说明「以下分析基于更新后的数据」。"""
    return {
        '分析': f'—— 数据已更新（{label or "新数据"}）——',
        '摘要': '以下分析基于更新后的数据；上面的记录属于上一版数据。',
        '类型': '数据变更',
        '含表格': False,
        '含图': False,
        '参数': {},
        '时间': str(at or ''),
        '指纹': '',
        'kind': 'data',
    }


def append(entries, entry, limit: int = LEDGER_LIMIT) -> List[Dict]:
    """追加一条记录（指纹重复则忽略，避免缓存同步反复写入同一条）。"""
    entries = list(entries or [])
    print_key = entry.get('指纹')
    if print_key and any(item.get('指纹') == print_key for item in entries):
        return entries
    entries.append(entry)
    return entries[-limit:]


def from_sections(entries, sections, at='') -> List[Dict]:
    """
    把报告缓存里的章节同步进台账。

    报告缓存恰好只包含「分析成功后自动收录」的章节，不含用户没看过的内容，
    因此它天然就是「实际做过的分析」清单。
    """
    entries = list(entries or [])
    for section in sections or []:
        entries = append(entries, make_entry(
            title=section.get('title'), content=section.get('content'),
            has_table=bool(section.get('table') is not None),
            has_image=bool(section.get('image')), at=at))
    return entries


def counts_by_type(entries) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in entries or []:
        key = item.get('类型') or '其他'
        counts[key] = counts.get(key, 0) + 1
    return counts


def summarize(entries) -> str:
    """给界面用的一句话摘要。"""
    items = [item for item in (entries or []) if item.get('kind') != 'data']
    if not items:
        return '台账为空：还没有执行过会产生结果的分析。'
    counts = counts_by_type(items)
    detail = '、'.join(f'{name} {count}' for name, count in sorted(
        counts.items(), key=lambda pair: -pair[1])[:4])
    return f'共 {len(items)} 次分析：{detail}'


def to_frame(entries) -> pd.DataFrame:
    """台账表格（时间倒序，最新的在最上面）。"""
    rows = []
    for item in reversed(list(entries or [])):
        rows.append({
            '时间': item.get('时间') or '—',
            '类型': item.get('类型') or '',
            '分析': item.get('分析') or '',
            '摘要': item.get('摘要') or '',
            '含图': '是' if item.get('含图') else '',
            '含表': '是' if item.get('含表格') else '',
        })
    return pd.DataFrame(rows, columns=['时间', '类型', '分析', '摘要', '含图', '含表'])


def to_text(entries) -> str:
    """
    台账的纯文本形式，供 AI 写方法学段落时作为事实依据。

    只给「分析名 + 摘要」，不含任何个体数据 —— 与结构摘要的隐私边界保持一致。
    """
    lines: List[str] = []
    current: Optional[str] = None
    for item in entries or []:
        if item.get('kind') == 'data':
            lines.append(f"\n【{item.get('分析')}】")
            current = None
            continue
        group = item.get('类型') or '其他'
        if group != current:
            lines.append(f"\n## {group}")
            current = group
        brief = item.get('摘要') or ''
        lines.append(f"- {item.get('分析')}" + (f"：{brief}" if brief else ''))
    return '\n'.join(lines).strip() or '（台账为空）'


def to_csv(entries) -> bytes:
    return to_frame(entries).to_csv(index=False).encode('utf-8-sig')


def to_markdown(entries) -> str:
    """台账的 Markdown 形式，可直接加入 Word 报告（作为附录）。"""
    frame = to_frame(entries)
    if frame.empty:
        return '（台账为空）'
    header = '| ' + ' | '.join(frame.columns) + ' |'
    divider = '| ' + ' | '.join('---' for _ in frame.columns) + ' |'
    lines = [header, divider]
    for _, row in frame.iterrows():
        cells = [str(value).replace('|', '／').replace('\n', ' ')
                 for value in row.tolist()]
        lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines)
