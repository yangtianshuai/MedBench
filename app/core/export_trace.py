# -*- coding: utf-8 -*-
"""导出流水号与校验位（F4.4 · docs/12 §6.4）。

## 为什么水印里要有编号

导出的 CSV 会离开系统（微信、U 盘、打印）。等它出现在院外时，
能回答的问题只有两个：**这是谁导的**、**这份文件是不是真的**。

- "谁导的"靠水印里的操作人 + 快照 ID —— 但这两样**谁都能照抄一份写进自己的文件**；
- 所以再加一个**只由服务端产生**的流水号，它同时存在于 `export_log` 里：
  拿到一个可疑文件，用编号反查就知道"我们到底有没有导出过这么一份"。

## 校验位不是加密，它防的是"手抄错"和"随手改一位"

格式 `EX-YYYYMMDD-SSSS-CC`：

    core = YYYYMMDD * 10000 + SSSS        （当天第几笔）
    CC   = 使 core*100+CC ≡ 1 (mod 97) 的两位数字（ISO 7064 MOD 97-10）

改一位数字、或把两份不同的文件的编号对上，校验位就对不上 ——
**这不阻止伪造**（谁都能自己造一个合法的编号），阻止的是"编号看着像真的"：
伪造者还得让这个编号在 `export_log` 里真的存在，而那需要写审计库的权限。

自测：`python -m app.core.export_trace --selftest`
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional, Tuple

PATTERN = re.compile(r"^EX-(\d{8})-(\d{4})-(\d{2})$")


def check_digit(core: int) -> str:
    """ISO 7064 MOD 97-10：返回两位校验码，使 `core*100+cc ≡ 1 (mod 97)`。"""
    cc = (98 - (core * 100) % 97) % 97
    return f"{cc:02d}"


def make_serial(seq: int, day: Optional[str] = None) -> str:
    """生成流水号。`seq` 是当天第几笔（从 1 开始）。"""
    d = day or time.strftime("%Y%m%d")
    core = int(d) * 10000 + int(seq)
    return f"EX-{d}-{int(seq):04d}-{check_digit(core)}"


def parse(serial: str) -> Tuple[bool, Dict[str, Any], str]:
    """解析并**校验格式与校验位**。返回 (是否合法, 各部分, 原因)。"""
    m = PATTERN.match((serial or "").strip().upper())
    if not m:
        return False, {}, "流水号格式应为 EX-YYYYMMDD-SSSS-CC"
    day, s, cc = m.group(1), int(m.group(2)), m.group(3)
    try:
        time.strptime(day, "%Y%m%d")
    except ValueError:
        return False, {"day": day}, f"日期部分 {day} 不是有效日期"
    core = int(day) * 10000 + s
    if check_digit(core) != cc:
        return False, {"day": day, "seq": s, "check_digit": cc,
                       "expected": check_digit(core)}, \
            f"校验位不符（应为 {check_digit(core)}）：编号可能被抄错或改过一位"
    return True, {"day": day, "seq": s, "check_digit": cc}, ""


def selftest() -> bool:
    """运行：python -m app.core.export_trace --selftest"""
    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    print("导出流水号自测（docs/12 §6.4）")
    s = make_serial(7, "20261016")
    check("格式稳定（EX-日期-序号-校验位）", bool(PATTERN.match(s)), s)

    ok, parts, why = parse(s)
    check("自己生成的编号能验过", ok and parts.get("seq") == 7 and not why, s)

    # 校验位必须真的挡得住"改一位"
    bad_digit = s[:-1] + ("0" if s[-1] != "0" else "1")
    ok2, _p2, why2 = parse(bad_digit)
    check("改校验位 → 被指出（并告诉你正确值）", (not ok2) and "校验位不符" in why2, why2)

    core_ok = PATTERN.match(s).group(2)
    bad_seq = s.replace(f"-{core_ok}-", f"-{int(core_ok) + 1:04d}-")
    ok3, _p3, why3 = parse(bad_seq)
    check("改序号但不动校验位 → 被抓到", not ok3, why3)

    ok4, _p4, why4 = parse("EX-20261350-0001-99")
    check("不存在的日期 → 被指出", (not ok4) and "日期" in why4, why4)

    ok5, _p5, why5 = parse("EXPORT-20261016-1")
    check("格式不对 → 给的是人话（不是正则报错）", (not ok5) and "格式" in why5, why5)

    # 跨日期/跨序号都稳定（同一份算法，运维手算也能对）
    samples = [make_serial(i, "20260101") for i in (1, 2, 999, 9999)]
    check("边界序号都能生成且自验通过",
          all(parse(x)[0] for x in samples), ", ".join(samples))
    check("同一天内序号不同 → 编号不同（否则两份文件会撞号）",
          len(set(samples)) == 4)

    print("=" * 60)
    print("自测结果: " + ("全部通过" if passed else "存在失败"))
    return passed


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
