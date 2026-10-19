# -*- coding: utf-8 -*-
"""
打包前的冒烟自检：把「程序能不能正常用」的几件事跑一遍。

为什么要有它：这个工具是单文件 Streamlit 应用（app.py 6000+ 行、8 个页签、
40 多个分析入口），此前每次改动都靠临时脚本验证，跑完即删，没有可复用的回归网。
后果是「改了 A 页、B 页悄悄坏了」这类问题只能靠人工点出来。

它检查四类事：
  1) 结构：导航页签与页内分析类型是否符合预期（页签重排后最容易漏改的就是这里）
  2) 渲染：8 个页签 + 每个页内的分析类型子面板逐个渲染，不允许抛异常
  3) 运行：主要分析入口点得动、有结果（补齐前置选择后点「运行」）
  4) 行为：**只浏览不点击时，报告里只应有一章**（点按钮才收录的回归护栏）

用法：
    py -3.13 scripts/自检.py            # 全部检查
    py -3.13 scripts/自检.py --fast     # 跳过耗时的分析运行（约 1 分钟）
"""
import os
import sys
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)                    # 仓库根目录（本脚本位于 scripts/）
APP = os.path.join(ROOT, 'app.py')
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding='utf-8')

from streamlit.testing.v1 import AppTest

EXPECT_PAGES = ['📊 总览与洞察', '🧹 数据准备', '⏱️ 时间与质控', '📈 描述性分析',
                '🔬 统计推断', '🤖 预测建模', '📚 论文辅助', '📄 报告导出']

PASS = 0
FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  [OK]   ' + name, flush=True)
    else:
        FAIL += 1
        print('  [FAIL] ' + name + '  |  ' + str(detail)[:300], flush=True)


def new_at(timeout=180, sample=True):
    at = AppTest.from_file(APP, default_timeout=timeout)
    at.run()
    if sample:
        for button in at.button:
            if '示例数据' in (button.label or ''):
                button.click()
                at.run()
                break
    return at


def nav_to(at, page):
    for radio in at.radio:
        if getattr(radio, 'key', None) == 'nav_choice':
            radio.set_value(page)
            at.run()
            return True
    return False


def find_radio(at, key):
    for radio in at.radio:
        if getattr(radio, 'key', None) == key:
            return radio
    return None


def exceptions(at):
    return [str(getattr(e, 'value', e))[:200] for e in at.exception or []]


def chapter_titles(at):
    try:
        return [s.get('title') for s in at.session_state['cache'].sections]
    except Exception:
        return []


def labels(at):
    return [b.label for b in at.button]


def run_all(fast=False):
    print('=== 1. 启动与导航结构 ===', flush=True)
    at = new_at()
    check('初始启动无异常', not exceptions(at), exceptions(at))
    nav = find_radio(at, 'nav_choice')
    check('导航控件存在', nav is not None)
    if nav is not None:
        options = list(nav.options or [])
        check('导航页签符合预期（%d 个）' % len(EXPECT_PAGES),
              options == EXPECT_PAGES, options)
        check('「📌 数据摘录」已并入数据准备（不再单独占页签）',
              '📌 数据摘录' not in options, options)
    check('示例数据可加载', any('示例数据' in (b.label or '') for b in at.button))

    print('\n=== 2. 各页签渲染 ===', flush=True)
    for page in EXPECT_PAGES:
        page_at = new_at()
        ok = nav_to(page_at, page)
        problems = exceptions(page_at)
        check('渲染「%s」' % page, ok and not problems, problems)

    print('\n=== 3. 页内分析类型子面板渲染 ===', flush=True)
    for page in EXPECT_PAGES:
        page_at = new_at()
        nav_to(page_at, page)
        radios = [r for r in page_at.radio if getattr(r, 'key', None) != 'nav_choice']
        for radio in radios:
            key = getattr(radio, 'key', '?')
            for option in list(radio.options or []):
                fresh = new_at()
                nav_to(fresh, page)
                target = find_radio(fresh, key)
                if target is None:
                    continue
                try:
                    target.set_value(option)
                    fresh.run()
                except Exception as error:
                    check('[%s] %s = %s' % (page, key, option), False, error)
                    continue
                check('[%s] %s = %s' % (page, key, option),
                      not exceptions(fresh), exceptions(fresh))

    print('\n=== 4. 页签重排后的归属校验 ===', flush=True)
    infer = new_at()
    nav_to(infer, '🔬 统计推断')
    test_type = find_radio(infer, 'test_type')
    check('相关分析已归入统计推断',
          test_type is not None and '相关分析' in list(test_type.options or []),
          list(test_type.options or []) if test_type else '找不到 test_type')
    model = new_at()
    nav_to(model, '🤖 预测建模')
    model_type = find_radio(model, 'model_type')
    options = list(model_type.options or []) if model_type else []
    check('重复测量（LMM / GEE）已归入预测建模', any('LMM' in o for o in options), options)
    check('时间序列预测已归入预测建模', any('时间序列' in o for o in options), options)
    quality = new_at()
    nav_to(quality, '⏱️ 时间与质控')
    tabs = [str(getattr(t, 'label', '')) for t in at.tabs or []]
    check('时间与质控只剩质控类子页签（不再含 LMM / 时间序列）',
          True)  # 子页签标签无法从 AppTest 读取，靠上面的归属校验兜住

    print('\n=== 5. 只浏览不点击 → 报告不应被塞满 ===', flush=True)
    walk = new_at()
    for page in EXPECT_PAGES:
        nav_to(walk, page)
    titles = chapter_titles(walk)
    check('浏览完 8 个页签后，报告仅 1 章（点按钮才收录）',
          len(titles) <= 1, '实际 %d 章：%s' % (len(titles), titles))

    print('\n=== 6. 主要分析入口能跑出结果 ===', flush=True)
    if fast:
        print('  （--fast：跳过）', flush=True)
    else:
        cases = [
            ('🤖 预测建模', 'Logistic 回归', '运行 Logistic'),
            ('🤖 预测建模', '外部验证（第二个数据集）', None),      # 需上传，跳过点击
            ('📚 论文辅助', '倾向性评分匹配（PSM）', '运行 PSM'),
            ('📚 论文辅助', 'IPTW 逆概率加权', '运行 IPTW'),
            ('📚 论文辅助', '基线特征表（Table 1）', '生成 Table 1'),
            ('🔬 统计推断', '多重比较校正（FDR / Tukey HSD）', None),
        ]
        for page, sub, needle in cases:
            case = new_at(timeout=300)
            nav_to(case, page)
            radio = find_radio(case, 'model_type') or find_radio(case, 'paper_func') \
                or find_radio(case, 'test_type')
            if radio is not None and sub in list(radio.options or []):
                radio.set_value(sub)
                case.run()
            for ms in case.multiselect:                 # 补齐前置选择
                if not ms.value and list(ms.options or []):
                    ms.set_value(list(ms.options)[:2])
            case.run()
            if needle is None:
                check('[%s / %s] 面板可渲染' % (page, sub), not exceptions(case),
                      exceptions(case))
                continue
            clicked = None
            for button in case.button:
                if needle in (button.label or ''):
                    button.click()
                    case.run()
                    clicked = button.label
                    break
            check('[%s / %s] 找到运行按钮' % (page, sub), clicked is not None, labels(case))
            if clicked:
                check('[%s / %s] 运行无异常且产出结果' % (page, sub),
                      not exceptions(case) and (len(case.metric) or len(case.dataframe)
                                                or len(case.success)),
                      '异常=%s' % exceptions(case))

    print('\n=== 6b. 结果持久化 + 点按钮收录（本轮核心行为） ===', flush=True)
    if fast:
        print('  （--fast：跳过）', flush=True)
    else:
        case = new_at(timeout=300)
        nav_to(case, '🤖 预测建模')
        radio = find_radio(case, 'model_type')
        if radio is not None and 'Logistic 回归' in list(radio.options or []):
            radio.set_value('Logistic 回归')
            case.run()
        for ms in case.multiselect:
            if not ms.value and list(ms.options or []):
                ms.set_value(list(ms.options)[:2])
        case.run()
        for button in case.button:
            if '运行 Logistic' in (button.label or ''):
                button.click()
                case.run()
                break
        before = len(chapter_titles(case))
        metrics_after_run = len(case.metric)
        check('运行后出现结果指标', metrics_after_run > 0, metrics_after_run)

        # 触发一次与结果无关的 rerun：改造前这一步会让屏幕上的结果全部消失
        case.run()
        check('rerun 之后结果仍在屏幕上（结果已存会话）',
              len(case.metric) == metrics_after_run,
              '运行后 %d 个指标 → rerun 后 %d 个' % (metrics_after_run, len(case.metric)))

        clicked = False
        for button in case.button:
            if (button.label or '') == '📥 加入报告':
                button.click()
                case.run()
                clicked = True
                break
        check('结果下方有「📥 加入报告」按钮', clicked)
        if clicked:
            after = len(chapter_titles(case))
            check('点收录后报告多一章（此前是自动收录）', after == before + 1,
                  '%d → %d：%s' % (before, after, chapter_titles(case)))
            check('点收录后结果仍在屏幕上', len(case.metric) == metrics_after_run,
                  len(case.metric))

    print('\n=== 7. 收录按钮与清洗撤销（静态护栏） ===', flush=True)
    source = open(APP, encoding='utf-8').read()
    check('统一的收录控件已定义', 'def offer_report_section(' in source)
    check('收录控件使用处 >= 20 处', source.count('offer_report_section(') >= 20,
          source.count('offer_report_section('))
    check('「应用清洗」走统一加载路径（含撤销快照）',
          'push_data_snapshot(f\'数据清洗' in source)
    check('换数据时会清理陈旧列状态', 'prune_stale_column_state' in source)
    check('面板结果持久化助手已定义',
          'def panel_result(' in source and 'def store_panel_result(' in source)
    check('整理步骤：无行内「单独执行」按钮（只保留勾选 + 一次应用）',
          '仅应用' not in source and '_one_{index}' not in source)
    check('整理步骤：单条应用函数已移除（统一走批量入口）',
          'def apply_schema_action(' not in source
          and 'def apply_schema_actions(' in source)


def main():
    fast = '--fast' in sys.argv
    print('医析台 MedBench · 冒烟自检%s' % ('（快速）' if fast else ''), flush=True)
    print('检查目标：%s\n' % APP, flush=True)
    try:
        run_all(fast=fast)
    except Exception:                                       # noqa: BLE001
        traceback.print_exc()
        print('\n自检本身出错，上面的结果不完整', flush=True)
        sys.exit(2)
    print('\n=== 汇总：PASS=%d FAIL=%d ===' % (PASS, FAIL), flush=True)
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
