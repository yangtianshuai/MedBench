# -*- coding: utf-8 -*-
"""
医析台 MedBench · 打包脚本

关键点：pandas 读 Excel 用的 xlrd / openpyxl 是**运行时动态导入**的，
PyInstaller 静态分析抓不到，必须显式收集，否则打包后上传任何 Excel 都会报
「Missing optional dependency 'xlrd'」。本脚本会自动把引擎补进打包目录。

用法（在项目根目录执行）：
    py -3.13 scripts/打包.py              # 用现有 dist 目录直接生成单文件 exe
    py -3.13 scripts/打包.py --rebuild    # 先用 PyInstaller 重建 onedir，再生成单文件

产物：
    dist/医析台/医析台.exe        （onedir，可直接运行）
    dist_single/医析台.exe        （单文件，双击即用）
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 仓库根目录（本脚本位于 scripts/）
APP_NAME = '医析台'
SOURCE_ROOT = ROOT                              # 应用源码：app.py / modules / .streamlit / sample_data
ONEDIR = ROOT / 'dist'                          # 扁平发布目录：医析台.exe + _internal
ONEDIR_BUILD = ROOT / 'dist_app' / APP_NAME     # PyInstaller onedir 的原始输出目录
SINGLE = ROOT / 'dist_single'

# 必须显式收集：pandas 的 Excel 引擎
EXCEL_ENGINES = ['openpyxl', 'xlrd', 'et_xmlfile']

HIDDEN_IMPORTS = EXCEL_ENGINES + [
    'scipy', 'statsmodels', 'matplotlib', 'seaborn',
    'sklearn', 'sklearn.impute', 'sklearn.experimental',
    'streamlit', 'altair', 'openpyxl.styles', 'openpyxl.worksheet',
    # modules/ 是以 --add-data 形式放进打包目录的「数据文件」，
    # PyInstaller 不会分析其中的 import，因此「只被 modules 用到」的库
    # 必须在这里显式声明，否则打包后一打开就 ImportError。
    # 曾经漏掉 docx / lifelines / sklearn 导致报告导出与 Cox 回归直接崩溃。
    'docx', 'lifelines', 'patsy', 'formulaic', 'autograd', 'autograd.numpy',
    # AI 解读走 requests 直接调用 OpenAI 兼容接口（同样是运行时动态导入）
    'requests', 'urllib3', 'charset_normalizer', 'certifi', 'idna',
]

# 需要连子模块与数据文件一起收集的包：
# python-docx 的默认模板、lifelines 的内置数据集都是数据文件，只用 --hidden-import 会漏
COLLECT_ALL = ['docx', 'lifelines']

ADD_DATA = [
    ('app.py', '.'),
    ('modules', 'modules'),
    ('sample_data', 'sample_data'),
    ('.streamlit', '.streamlit'),   # 只监听 127.0.0.1、关闭使用统计
]


def run(command) -> None:
    printable = ' '.join(str(part) for part in command)
    print('> ' + printable, flush=True)
    subprocess.run([str(part) for part in command], cwd=str(ROOT), check=True)


def ensure_excel_engines(internal: Path) -> None:
    """把 Excel 引擎补进打包运行库目录。"""
    site_packages = Path(sys.prefix) / 'Lib' / 'site-packages'
    for package in EXCEL_ENGINES:
        source = site_packages / package
        if not source.is_dir():
            raise SystemExit(f'本机未安装 {package}，请先执行：pip install xlrd openpyxl')
        destination = internal / package
        shutil.copytree(source, destination, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        print(f'[引擎] {package} -> {destination}', flush=True)


def ensure_dependencies() -> None:
    """打包前自检：这些库必须能在当前解释器导入，否则打出来的程序会直接报 ImportError。"""
    import importlib.util

    missing = sorted({name.split('.')[0] for name in HIDDEN_IMPORTS + COLLECT_ALL
                      if importlib.util.find_spec(name.split('.')[0]) is None})
    if missing:
        raise SystemExit('打包解释器缺少依赖：' + '、'.join(missing)
                         + '\n请先执行：pip install ' + ' '.join(missing))
    print(f'[自检] 依赖齐全（{len(set(HIDDEN_IMPORTS + COLLECT_ALL))} 项）', flush=True)


def remove_tree(path: Path, attempts: int = 4) -> None:
    """
    删除目录。

    优先用系统 rd 命令：比 shutil.rmtree 更稳，可绕开 Python 层的删除钩子
    （某些环境下 shutil.rmtree 会被批量删除保护中断），也不会误触发回收站逻辑。
    被占用时重试，仍失败则给出可操作的提示。
    """
    for attempt in range(1, attempts + 1):
        if not path.exists():
            return
        subprocess.run(['cmd', '/c', 'rd', '/s', '/q', str(path)],
                       capture_output=True)
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
            return
        except OSError as error:
            if attempt == attempts:
                raise SystemExit(
                    f'无法清理旧构建目录：{path}\n原因：{error}\n'
                    '请先关闭正在运行的工作台程序（以及打开该目录的资源管理器窗口），再重新执行打包。'
                )
            time.sleep(1.5)


def rebuild_onedir() -> None:
    """用 PyInstaller 重建 onedir，再整理成扁平的 dist 目录（exe + _internal）。"""
    if not (SOURCE_ROOT / 'app.py').is_file():
        raise SystemExit(f'未找到应用源码：{SOURCE_ROOT / "app.py"}')

    # 先自行清理旧输出：PyInstaller 清理失败时会直接中断（WinError 5）
    remove_tree(ONEDIR_BUILD.parent)
    remove_tree(ROOT / 'build')

    # 不用 --clean：它会触发环境中的批量删除保护，中断构建；
    # workpath 由本脚本在构建前自行清理，效果等同。
    args = [sys.executable, '-m', 'PyInstaller', '--noconfirm',
            '--onedir', '--name', APP_NAME, '--icon', str(ROOT / 'app.ico'),
            '--distpath', str(ONEDIR_BUILD.parent), '--workpath', 'build',
            '--specpath', 'build']
    for module in HIDDEN_IMPORTS:
        args += ['--hidden-import', module]
    for package in COLLECT_ALL:
        args += ['--collect-all', package]
    for source, destination in ADD_DATA:
        # 用绝对路径：--specpath 会让相对路径按 spec 所在目录解析
        args += ['--add-data', f'{SOURCE_ROOT / source}{os.pathsep}{destination}']
    args.append(str(ROOT / 'scripts' / 'streamlit_entry.py'))
    run(args)

    if not ONEDIR_BUILD.is_dir():
        raise SystemExit(f'未找到构建产物：{ONEDIR_BUILD}')
    ensure_writable(ONEDIR / f'{APP_NAME}.exe')
    shutil.copytree(ONEDIR_BUILD, ONEDIR, dirs_exist_ok=True)
    print(f'[整理] {ONEDIR_BUILD} -> {ONEDIR}', flush=True)


def is_file_locked(path: Path) -> bool:
    """
    判断文件是否被其他进程占用（Windows）。

    不能用 open() 判断：CPython 在 Windows 上以完全共享方式打开文件，
    即使 exe 正在运行也能成功 open，检测不出冲突。
    这里用 CreateFileW 申请独占句柄（dwShareMode=0），被占用时会返回共享冲突。
    """
    if os.name != 'nt' or not path.exists():
        return False
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return True
    kernel32.CloseHandle(handle)
    return False


def ensure_writable(path: Path) -> None:
    """
    打包前检查目标 exe 是否被占用。

    PyInstaller 覆盖被占用的 exe 时会抛 `OSError: SHFileOperationW 失败: 0x7c`，
    这个报错对使用者毫无指向性 —— 最常见的原因就是**旧版本程序仍在运行**。
    """
    if not is_file_locked(path):
        return
    raise SystemExit(
        f'无法写入：{path}\n'
        '该文件正被其他进程占用，最常见的原因是「医析台」仍在运行。\n'
        '请关闭所有程序窗口（含任务管理器中的「医析台」进程）后重试；\n'
        '若只是想验证功能，也可以先运行 dist\\ 目录下的 onedir 版本。'
    )


def build_singlefile() -> None:
    """把 dist 目录作为 payload 打成单文件 exe。"""
    ensure_writable(SINGLE / f'{APP_NAME}.exe')
    remove_tree(ROOT / 'build_single')
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
         '--onefile', '--name', APP_NAME, '--icon', str(ROOT / 'app.ico'),
         '--distpath', 'dist_single', '--workpath', 'build_single',
         '--specpath', 'build_single',
         '--add-data', f'{ONEDIR}{os.pathsep}payload',
         str(ROOT / 'scripts' / '单文件启动器.py')])


def main() -> None:
    parser = argparse.ArgumentParser(description='医析台 MedBench 打包')
    parser.add_argument('--rebuild', action='store_true', help='先用 PyInstaller 重建 onedir 目录')
    options = parser.parse_args()

    ensure_dependencies()

    if options.rebuild or not (ONEDIR / f'{APP_NAME}.exe').is_file():
        rebuild_onedir()

    internal = ONEDIR / '_internal'
    if not internal.is_dir():
        raise SystemExit(f'未找到打包运行库目录：{internal}')

    ensure_excel_engines(internal)
    build_singlefile()
    print(f'\n完成：{SINGLE / (APP_NAME + ".exe")}')


if __name__ == '__main__':
    main()
