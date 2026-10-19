"""
项目文件：把一次分析完整保存下来，之后可直接打开继续，或把文件发给同事复核。

格式是 zip 容器（扩展名 `.mda`），内含：

    数据.xlsx        当前工作区数据（已应用人工类型指定）
    设置.json        类型指定、数据来源、报告标题、章节内容、AI 结果
    图/NN_标题.png    报告章节里的统计图（按原文件写入）
    说明.txt         版本、生成时间、字段清单（供人直接阅读核对）

两个设计取舍：
1. 保存「当前数据」而不是「原始文件 + 重放步骤」：宽表转长表、缺失插补等操作会改写
   数据，逐步重放成本高且容易不一致，直接保存结果更可靠。
2. 图片按原始 PNG 写入 zip，不做 base64：PNG 本身已是压缩格式，base64 会额外膨胀 33%。
"""
import io
import json
import re
import zipfile
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

PROJECT_VERSION = 1
PROJECT_EXT = '.mda'
DATA_NAME = '数据.xlsx'
SETTINGS_NAME = '设置.json'
README_NAME = '说明.txt'
IMAGE_DIR = '图'
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


class ProjectError(RuntimeError):
    """带用户可读提示的项目文件错误。"""


def _safe_name(text, limit: int = 40) -> str:
    cleaned = _BAD_CHARS.sub('_', str(text or '图')).strip('_')
    return cleaned[:limit] or '图'


def _jsonable(value):
    """把 numpy 标量等转成可 JSON 序列化的原生类型。"""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, 'item'):          # numpy 标量
        try:
            return value.item()
        except Exception:
            return str(value)
    if value is pd.NaT:
        return None
    return str(value)


def _table_to_records(table):
    """表格 → {'columns': [...], 'rows': [[...]]}（None 表示没有表格）。"""
    if not isinstance(table, pd.DataFrame) or table.empty:
        return None
    frame = table.where(pd.notna(table), None)
    rows: List[List] = []
    for _, row in frame.iterrows():
        rows.append([_jsonable(value) for value in row])
    return {'columns': [str(column) for column in frame.columns], 'rows': rows}


def _records_to_table(payload):
    if not payload:
        return None
    try:
        return pd.DataFrame(payload.get('rows') or [],
                            columns=payload.get('columns') or [])
    except Exception:
        return None


def _readme(settings: Dict) -> str:
    shape = settings.get('数据形状') or [0, 0]
    fields = settings.get('字段') or []
    lines = [
        '医析台 MedBench · 项目文件',
        '=' * 34,
        f"项目版本：v{settings.get('项目版本')}",
        f"生成时间：{settings.get('生成时间')}",
        f"数据来源：{settings.get('数据来源') or '（未记录）'}",
        f"数据规模：{shape[0]} 行 × {shape[1]} 列",
        f"人工类型指定：{len(settings.get('类型指定') or {})} 项",
        f"报告章节：{len(settings.get('章节') or [])} 个",
        '',
        '本文件是 zip 容器，可改扩展名为 .zip 后直接解压查看：',
        '  数据.xlsx  —— 当前工作区数据',
        '  设置.json  —— 类型指定、报告标题、章节内容与统计表格',
        '  图/        —— 报告中的统计图（300 dpi PNG）',
        '',
        '字段清单：',
    ]
    lines += [f'  {index}. {name}' for index, name in enumerate(fields, 1)]
    lines += ['', '在程序中通过侧边栏「📂 打开项目」载入即可继续分析。']
    return '\n'.join(lines)


def save_project(df, *, type_overrides: Optional[Dict] = None,
                 dataset_source: str = '', report_title: str = '',
                 sections: Optional[List[Dict]] = None,
                 ai_results: Optional[Dict] = None) -> bytes:
    """把当前工作区打包成项目文件，返回字节。"""
    if df is None or len(df) == 0:
        raise ProjectError('当前没有数据，无法保存项目。请先上传数据或打开已有项目。')

    sections = list(sections or [])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        data_buffer = io.BytesIO()
        df.to_excel(data_buffer, index=False, engine='openpyxl')
        archive.writestr(DATA_NAME, data_buffer.getvalue())

        section_meta: List[Dict] = []
        image_count = 0
        for index, section in enumerate(sections, 1):
            entry = {
                'title': str(section.get('title') or ''),
                'content': str(section.get('content') or ''),
                'level': int(section.get('level') or 1),
                'table': _table_to_records(section.get('table')),
                'image': '',
            }
            image = section.get('image')
            if image:
                image_count += 1
                name = f'{IMAGE_DIR}/{index:02d}_{_safe_name(entry["title"])}.png'
                archive.writestr(name, image)
                entry['image'] = name
            section_meta.append(entry)

        settings = {
            '项目版本': PROJECT_VERSION,
            '生成时间': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            '数据来源': str(dataset_source or ''),
            '类型指定': {str(key): str(value)
                     for key, value in (type_overrides or {}).items()},
            '报告标题': str(report_title or ''),
            '章节': section_meta,
            'AI 结果': ai_results or {},
            '数据形状': [int(df.shape[0]), int(df.shape[1])],
            '字段': [str(column) for column in df.columns],
        }
        archive.writestr(SETTINGS_NAME,
                         json.dumps(settings, ensure_ascii=False, indent=2))
        archive.writestr(README_NAME, _readme(settings))
    return buffer.getvalue()


def load_project(data: bytes) -> Dict:
    """读取项目文件，返回可直接恢复到会话的状态字典。"""
    if not data:
        raise ProjectError('项目文件为空。')
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except Exception as error:
        raise ProjectError(f'无法读取项目文件（可能不是本程序生成的 .mda）：{error}')

    with archive:
        names = set(archive.namelist())
        missing = [name for name in (DATA_NAME, SETTINGS_NAME) if name not in names]
        if missing:
            raise ProjectError('项目文件不完整：缺少 ' + '、'.join(missing))
        try:
            settings = json.loads(archive.read(SETTINGS_NAME).decode('utf-8'))
        except Exception as error:
            raise ProjectError(f'项目设置无法解析：{error}')

        version = int(settings.get('项目版本') or 0)
        if version > PROJECT_VERSION:
            raise ProjectError(
                f'该项目的版本（v{version}）高于当前程序支持的版本（v{PROJECT_VERSION}）。'
                '请先升级程序再打开。')

        try:
            frame = pd.read_excel(io.BytesIO(archive.read(DATA_NAME)))
        except Exception as error:
            raise ProjectError(f'项目数据无法读取：{error}')

        sections: List[Dict] = []
        for entry in settings.get('章节') or []:
            image = None
            image_name = str(entry.get('image') or '')
            if image_name and image_name in names:
                image = archive.read(image_name)
            sections.append({
                'title': str(entry.get('title') or ''),
                'content': str(entry.get('content') or ''),
                'level': int(entry.get('level') or 1),
                'table': _records_to_table(entry.get('table')),
                'image': image,
            })

    return {
        'df': frame,
        'type_overrides': {str(key): str(value)
                           for key, value in (settings.get('类型指定') or {}).items()},
        'dataset_source': settings.get('数据来源') or '项目文件',
        'report_title': settings.get('报告标题') or '',
        'sections': sections,
        'ai_results': settings.get('AI 结果') or {},
        'created_at': settings.get('生成时间') or '',
        'version': version,
        'row_count': int(frame.shape[0]),
        'column_count': int(frame.shape[1]),
        'image_count': sum(1 for section in sections if section.get('image')),
    }
