# -*- coding: utf-8 -*-
"""
使用说明生成脚本
把 `docs/使用说明.md` 转成可阅读 / 可打印的 HTML，并可选调用 Edge 无头模式导出 PDF。

用法（在项目根目录执行）：
    py -3.11 scripts/生成说明文档.py           # 只生成 HTML
    py -3.11 scripts/生成说明文档.py --pdf     # 同时导出 PDF（需要本机安装 Edge / Chrome）
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 仓库根目录（本脚本位于 scripts/）
DOCS = ROOT / 'docs'
SOURCE = DOCS / '使用说明.md'
TARGET_HTML = DOCS / '医析台_使用说明.html'
TARGET_PDF = DOCS / '医析台_使用说明.pdf'
EDGE_CANDIDATES = [
    Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'),
    Path(r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'),
]


def find_browser():
    """优先用系统 PATH 里的 Edge / Chrome，取不到再退回常见安装路径。"""
    for name in ('msedge', 'chrome', 'google-chrome', 'chromium'):
        found = shutil.which(name)
        if found:
            return Path(found)
    return next((path for path in EDGE_CANDIDATES if path.is_file()), None)

STYLE = """
  body { font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
         line-height: 1.75; color: #1e293b; max-width: 900px;
         margin: 0 auto; padding: 2rem 1.5rem 3rem; }
  h1 { font-size: 1.85rem; border-bottom: 3px solid #3B82F6; padding-bottom: .6rem; }
  h2 { font-size: 1.35rem; margin-top: 2.2rem; color: #1e40af;
       border-left: 4px solid #3B82F6; padding-left: .6rem; }
  h3 { font-size: 1.1rem; margin-top: 1.6rem; color: #334155; }
  p, li { font-size: .95rem; }
  table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .9rem; }
  th, td { border: 1px solid #cbd5e1; padding: .5rem .7rem;
           text-align: left; vertical-align: top; }
  th { background: #f1f5f9; }
  code { background: #f1f5f9; padding: .1rem .35rem; border-radius: 4px; font-size: .9em; }
  pre { background: #0f172a; color: #e2e8f0; padding: 1rem;
        border-radius: 8px; overflow: auto; }
  pre code { background: none; color: inherit; }
  ul, ol { padding-left: 1.4rem; }
  li { margin: .3rem 0; }
  hr { border: none; border-top: 1px solid #e2e8f0; margin: 2rem 0; }
  @media print {
    body { max-width: none; padding: 0; }
    h2 { page-break-after: avoid; }
    table, pre { page-break-inside: avoid; }
  }
"""


def build_html() -> None:
    import markdown

    body = markdown.markdown(
        SOURCE.read_text(encoding='utf-8'),
        extensions=['tables', 'fenced_code', 'sane_lists'],
    )
    document = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>医析台 MedBench · 使用说明</title>
<style>
{STYLE}
</style>
</head>
<body>
{body}
</body>
</html>
"""
    TARGET_HTML.write_text(document, encoding='utf-8')
    print(f'HTML 已生成：{TARGET_HTML.name}（{TARGET_HTML.stat().st_size} 字节）')


def build_pdf() -> None:
    browser = find_browser()
    if browser is None:
        print('未找到可用的浏览器（Edge / Chrome），跳过 PDF 生成（可打开 HTML 后用浏览器打印为 PDF）')
        return
    address = TARGET_HTML.resolve().as_uri()
    subprocess.run(
        [str(browser), '--headless=new', '--disable-gpu', '--no-pdf-header-footer',
         f'--print-to-pdf={TARGET_PDF}', address],
        check=False,
        capture_output=True,
    )
    if TARGET_PDF.is_file():
        print(f'PDF 已生成：{TARGET_PDF.name}（{TARGET_PDF.stat().st_size / 1024 / 1024:.2f} MB）')


def main() -> None:
    parser = argparse.ArgumentParser(description='生成使用说明 HTML / PDF')
    parser.add_argument('--pdf', action='store_true', help='同时导出 PDF')
    options = parser.parse_args()

    if not SOURCE.is_file():
        raise SystemExit(f'未找到源文件：{SOURCE}')
    build_html()
    if options.pdf:
        build_pdf()


if __name__ == '__main__':
    main()
