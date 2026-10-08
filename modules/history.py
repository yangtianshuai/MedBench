"""
数据快照栈：支持「回到上一步」

**为什么必须深拷贝**：pandas 2.x 的 Copy-on-Write 默认是关闭的（3.0 才默认开启），
而项目里存在原地赋值（`df[col] = ...`）。用浅拷贝做快照，后续的原地写入会穿透到
历史里，撤销后拿到的其实是被后续操作污染过的数据 —— 这种错误很隐蔽，
所以这里老老实实深拷贝。

**代价是内存**：每层快照都是完整的一份数据。因此限制栈深（默认 8 层），
界面对大文件给出提示。

快照里同时保存「人工类型指定」与「导入设置」：这两者决定数据怎么被解析，
只回退数据不回退它们，下次 rerun 会立刻按旧设置重新算一遍，撤销等于白做。
"""
from typing import Dict, List, Optional

import pandas as pd

STACK_LIMIT = 8


def make_snapshot(*, df, overrides: Optional[Dict] = None,
                  import_settings: Optional[Dict] = None, source: str = '',
                  label: str = '', at: str = '') -> Dict:
    """
    保存一份可用于回退的状态。df 会被深拷贝。

    label 描述的是「即将执行的操作」，因为快照保存的是该操作**之前**的状态，
    界面上会显示成「撤销：<label>」。
    """
    is_frame = isinstance(df, pd.DataFrame)
    return {
        'df': df.copy(deep=True) if is_frame else None,
        'overrides': dict(overrides or {}),
        'import_settings': dict(import_settings or {}),
        'source': str(source or ''),
        'label': str(label or '数据变更'),
        'at': str(at or ''),
        'rows': int(df.shape[0]) if is_frame else 0,
        'columns': int(df.shape[1]) if is_frame else 0,
    }


def push(stack, snapshot: Dict, limit: int = STACK_LIMIT) -> List[Dict]:
    """压入快照；超过栈深时丢弃最早的（最久远的状态不再可回退）。"""
    items = list(stack or [])
    items.append(snapshot)
    return items[-limit:]


def pop(stack) -> tuple:
    """弹出并返回 (快照, 剩余栈)；栈为空时返回 (None, 原栈)。"""
    items = list(stack or [])
    if not items:
        return None, items
    return items[-1], items[:-1]


def describe_stack(stack) -> str:
    items = list(stack or [])
    if not items:
        return '暂无可回退的历史'
    latest = items[-1]
    return (f"可回退 {len(items)} 步（最近一步：撤销「{latest.get('label')}」）")


def to_frame(stack) -> pd.DataFrame:
    """历史列表：按时间顺序，最新在最下面，与「回退方向」一致。"""
    rows = []
    for index, item in enumerate(stack or []):
        rows.append({
            '回退步数': index + 1,
            '撤销的操作': item.get('label') or '数据变更',
            '数据规模': f"{item.get('rows', 0)} 行 × {item.get('columns', 0)} 列",
            '时间': item.get('at') or '—',
        })
    return pd.DataFrame(rows, columns=['回退步数', '撤销的操作', '数据规模', '时间'])


def estimate_memory_mb(stack) -> float:
    """粗略估算快照栈占用的内存（MB），用于提醒大文件用户。"""
    total = 0.0
    for item in stack or []:
        frame = item.get('df')
        if isinstance(frame, pd.DataFrame):
            try:
                total += float(frame.memory_usage(deep=True).sum())
            except Exception:  # noqa: BLE001
                continue
    return round(total / 1024 / 1024, 1)
