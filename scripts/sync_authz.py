#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""组织架构 → 授权台账同步（F6.2 · docs/12 §4.2 / §4.3）—— **命令行入口**。

逻辑在 `app/core/authz_sync.py`（管理端走 `/api/security/grants/sync`，同一份实现）。

跑法（默认 **dry-run**，不写库）：
    # ① 迁移：把 roles.yaml 的用户名单搬进台账（一次性；之后 YAML 只留角色模板）
    python scripts/sync_authz.py --source yaml --ref "MIGRATE-v0.19"

    # ② 从院方导出的组织架构同步（CSV / JSON，列名在 authz_sync.yaml 的 fields 段配）
    python scripts/sync_authz.py --source file --file 组织结构.csv --ref "HRP-2026-10"

    # ③ 确认 dry-run 输出无误后落库
    python scripts/sync_authz.py --source file --file 组织结构.csv --ref "HRP-2026-10" --apply

危险开关（默认关闭，请读注释再打开）：
    --revoke-missing ：快照里没有的人一律撤销。**一份不完整的导出会撤掉全院权限**，
                       只在确认导出覆盖全部在岗人员时使用。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import authz_sync                                   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="组织架构 → 授权台账同步（默认 dry-run）")
    ap.add_argument("--source", choices=["yaml", "file"], default="file")
    ap.add_argument("--file", help="组织架构导出文件（CSV / JSON）")
    ap.add_argument("--config", help="映射表路径（默认 semantic-layer/authz_sync.yaml）")
    ap.add_argument("--ref", required=True,
                    help="★ 授权依据：组织架构版本号 / 工单号 / 备案号（必填）")
    ap.add_argument("--apply", action="store_true", help="真正写库（不加则只预览）")
    ap.add_argument("--revoke-missing", action="store_true",
                    help="★ 快照里没有的人一律撤销（默认关：不完整的导出会撤掉全院权限）")
    ap.add_argument("--valid-days", type=int, help="授权有效期（天），默认取配置")
    args = ap.parse_args()

    if args.source == "file" and not args.file:
        print("[FAIL] --source file 需要 --file（院方组织架构导出）")
        return 2

    r = authz_sync.run(source=args.source, ref=args.ref, file=args.file,
                       apply=args.apply, revoke_missing=args.revoke_missing,
                       valid_days=args.valid_days, config_path=args.config)
    if not r.get("ok"):
        print(f"[FAIL] {r['error']}")
        for b in (r.get("bad") or [])[:50]:
            print(f"  - {b['user_id']} {b.get('name', '')}：{b['why']}")
        if r.get("bad") and len(r["bad"]) > 50:
            print(f"  ...另有 {len(r['bad']) - 50} 条")
        return 1

    print(f"同步源：{r['source']}　依据：{r['ref']}　有效期：{r['valid_days']} 天")
    print(f"读取条目：{r['read']}　映射成功：{r['mapped']}　"
          f"新增 {r['counts'].get('grant', 0)}　变更 {r['counts'].get('update', 0)}　"
          f"不变 {r['counts'].get('keep', 0)}　撤销 {r['counts'].get('revoke', 0)}")
    for p in r["plan"]:
        if p["action"] != "keep":
            tag = {"grant": "新增", "update": "变更", "revoke": "撤销"}[p["action"]]
            scope = "、".join(p["dept_scope"]) or "无授权"
            print(f"  [{tag}] {p['user']} {p.get('name') or ''}　{p['role']}　{scope}"
                  f"　（{p['why']}）")
    if not r["applied"]:
        print("\n以上为 **dry-run** 预览，未写库。确认无误后加 --apply。")
        print("提示：不完整的导出不要加 --revoke-missing。")
        return 0
    res = r["result"]
    print(f"\n已写入：新增 {res['grant']}　变更 {res['update']}　撤销 {res['revoke']}"
          f"　失败 {res['failed']}")
    print(f"授权变更链：{'完整' if r['chain']['ok'] else '断裂 @' + str(r['chain'].get('broken_at'))}"
          f"（{r['chain']['checked']} 条）")
    print(f"台账现状：有效授权 {r['stats']['active_grants']} 条；"
          f"仍只在 YAML 里（未迁移）：{len(r['stats']['yaml_only_users'])} 人")
    return 0


if __name__ == "__main__":
    sys.exit(main())
