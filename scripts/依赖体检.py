# -*- coding: utf-8 -*-
"""
打包依赖体检脚本

用途：PyInstaller 只会收集「被引用到」的模块，像 sklearn.impute、
sklearn.experimental 这类可选工具模块不会被自动收集，运行到才会抛
ModuleNotFoundError。本脚本把源码里所有 import 与打包归档（PYZ）里的
模块清单做比对，提前暴露这类问题。

用法（重新打包后执行，或改动依赖后执行）：
    py -3.13 scripts/依赖体检.py
    py -3.13 scripts/依赖体检.py --exe "其他路径\\医析台.exe"
"""
import argparse
import ast
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 仓库根目录（本脚本位于 scripts/）
SOURCE_ROOT = ROOT
DEFAULT_EXE = ROOT / 'dist' / '医析台.exe'


def load_archive_modules(exe_path: Path) -> set:
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    reader = CArchiveReader(str(exe_path))
    entries = [name for name in reader.toc if 'PYZ' in name.upper()]
    if not entries:
        raise SystemExit('该 exe 内没有找到 PYZ 归档，请确认路径是否正确')
    temp_path = os.path.join(tempfile.gettempdir(), 'dependency_check.pyz')
    with open(temp_path, 'wb') as handle:
        handle.write(reader.extract(entries[0]))
    return set(ZlibArchiveReader(temp_path).toc)


def collect_imports(paths) -> list:
    found = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.append((path.name, alias.name, node.lineno))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.append((path.name, node.module, node.lineno))
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description='打包依赖体检')
    parser.add_argument('--exe', default=str(DEFAULT_EXE), help='内层 onedir exe 路径')
    options = parser.parse_args()

    exe_path = Path(options.exe)
    if not exe_path.is_file():
        raise SystemExit(f'未找到 exe：{exe_path}')

    modules = load_archive_modules(exe_path)

    def module_exists(name: str) -> bool:
        if name in modules:
            return True
        return any(entry.startswith(name + '.') for entry in modules)

    internal_dirs = {'modules', 'sample_data'}
    sources = [SOURCE_ROOT / 'app.py'] + sorted((SOURCE_ROOT / 'modules').glob('*.py'))

    problems = []
    for file_name, module, line in collect_imports(sources):
        top_level = module.split('.')[0]
        if top_level in sys.stdlib_module_names or top_level in ('modules',):
            continue
        if top_level in internal_dirs:
            continue
        if not module_exists(module):
            problems.append((file_name, module, line))

    print(f'源文件 {len(sources)} 个，归档模块 {len(modules)} 个')
    if problems:
        print('⚠️ 以下 import 在打包归档中找不到，运行时会报 ModuleNotFoundError：')
        for file_name, module, line in problems:
            print(f'    {file_name}:{line}  {module}')
        raise SystemExit(1)
    print('✅ 全部 import 都能在打包归档中找到')


if __name__ == '__main__':
    main()
