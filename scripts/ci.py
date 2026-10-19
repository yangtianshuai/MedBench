#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 门禁：一条命令跑完全部离线回归。

为什么把门禁写成脚本而不是直接堆在 CI 配置里：
    院内交付可能是 GitHub / GitLab / Jenkins / 内网流水线，YAML 各不相同，
    但"跑哪些检查、失败怎么算"应当只有一份定义。CI 配置只负责调用本脚本。

★ 这些检查全部**离线、不连数据库、不调模型**：
    任何需要 HIS / 模型才能跑的检查都不能进 CI —— 否则门禁会因为环境抖动而红，
    团队很快就学会"重跑一次"和"忽略它"，门禁等于没有。

覆盖：
    1. 语法          compileall（app / engine / scripts / eval）
    2. 生成器        engine/sql_builder.py 自测（含 JOIN 正反向）
    3. 主链路        engine/pipeline.py --selftest
    4. 召回          engine/recall.py --selftest
    5. 示例库        engine/fewshot.py --selftest
    6. 归因          app.core.attribution_service 自测
    7. 运营榜单      app.core.ops_stats 自测
    8. 质量分        app.core.quality 自测
    9. 物理映射      app.core.physical_map_store 自测
   10. 质量映射 API  scripts/check_quality_map_api.py（真接口 + 鉴权 + 二次确认）
   11. 标注校验      eval/validate_golden.py
   12. SQL 逐字      eval/sql_snapshot.py（17 条基线）
   13. 召回指标      eval/eval_recall.py
   14. 端到端        eval/run_golden.py --gate（mock_oracle 必须满分）

用法:
    python scripts/ci.py              # 全量
    python scripts/ci.py --list       # 只列检查项
    python scripts/ci.py --only 召回,端到端
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
GOLDEN = "eval/golden_set_sample.jsonl"
REGISTRY = "semantic-layer/metric_registry.yaml"

Check = Tuple[str, List[str]]

CHECKS: List[Check] = [
    ("语法", [PY, "-m", "compileall", "-q", "app", "engine", "scripts", "eval"]),
    ("生成器", [PY, "engine/sql_builder.py"]),
    ("主链路", [PY, "engine/pipeline.py", "--selftest"]),
    ("召回", [PY, "engine/recall.py", "--selftest"]),
    ("示例库", [PY, "engine/fewshot.py", "--selftest"]),
    ("归因", [PY, "-m", "app.core.attribution_service"]),
    ("运营榜单", [PY, "-m", "app.core.ops_stats"]),
    ("质量分", [PY, "-m", "app.core.quality"]),
    ("物理映射", [PY, "-m", "app.core.physical_map_store"]),
    ("质量映射API", [PY, "scripts/check_quality_map_api.py"]),
    ("标注校验", [PY, "eval/validate_golden.py", "--input", GOLDEN,
                  "--registry", REGISTRY]),
    ("SQL逐字", [PY, "eval/sql_snapshot.py"]),
    ("召回指标", [PY, "eval/eval_recall.py", "--golden", GOLDEN,
                  "--registry", REGISTRY]),
    ("端到端", [PY, "eval/run_golden.py", "--golden", GOLDEN, "--engine",
                "mock_oracle", "--registry", REGISTRY, "--gate"]),
]


def _run(name: str, cmd: Sequence[str], tail: int = 30) -> Tuple[bool, float, str]:
    started = time.time()
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")     # 中文输出在 CI 里不炸
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    try:
        p = subprocess.run(list(cmd), cwd=str(ROOT), env=env, text=True,
                           encoding="utf-8", errors="replace",
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=600)
        out = p.stdout or ""
        ok = p.returncode == 0
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + "\n[超时] 超过 600 秒"
        ok = False
    except FileNotFoundError as e:
        out, ok = f"[无法执行] {e}", False
    cost = time.time() - started
    detail = "\n".join(out.strip().splitlines()[-tail:])
    return ok, cost, detail


def _force_utf8_console() -> None:
    """Windows 控制台默认是 GBK，中文门禁输出会变成乱码。

    光 reconfigure 不够：字节是 UTF-8 了，但终端仍按 936 解码。
    所以两边一起改 —— 控制台代码页切 65001，Python 侧切 utf-8。
    """
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # noqa: BLE001 —— 重定向到文件时无控制台
            pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def main(argv: Sequence[str] = None) -> int:
    _force_utf8_console()
    ap = argparse.ArgumentParser(description="离线回归门禁")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", help="逗号分隔的检查名（模糊匹配）")
    ap.add_argument("--tail", type=int, default=30, help="失败时打印最后 N 行")
    args = ap.parse_args(argv)

    checks = CHECKS
    if args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        checks = [c for c in CHECKS if any(k in c[0] for k in keys)]
        if not checks:
            print(f"没有匹配的检查项：{args.only}")
            return 2

    if args.list:
        for name, cmd in checks:
            print(f"  {name:<10} {' '.join(cmd[1:])}")
        return 0

    print("=" * 72)
    print(f"CI 门禁 · {len(checks)} 项 · root={ROOT}")
    print("=" * 72)
    failures: List[Tuple[str, str]] = []
    for name, cmd in checks:
        ok, cost, detail = _run(name, cmd, args.tail)
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<10} {cost:6.1f}s")
        if not ok:
            failures.append((name, detail))
            if detail:
                print("-" * 72)
                print(detail)
                print("-" * 72)

    print("=" * 72)
    if failures:
        print(f"门禁未通过：{len(failures)}/{len(checks)} 项失败 → "
              + ", ".join(n for n, _ in failures))
        return 1
    print(f"门禁通过：{len(checks)}/{len(checks)} 项全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
