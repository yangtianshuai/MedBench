#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""质量分 / 物理映射 端点回归 —— 走真 ASGI，**不落盘**。

跑法（在仓库根目录）：
    python scripts/check_quality_map_api.py

为什么 `app.core.quality` / `app.core.physical_map_store` 的自测还不够：
    模块自测证明的是"算得对"，证明不了"接线对"。下面这些只有打真接口才看得见：
      · 路由有没有真的注册、前缀有没有拼错（自测里根本没有 app）
      · 鉴权有没有挡在写操作**前面**，还是先写盘再报 403
      · 二次确认短语是不是服务端下发的 —— 前端硬编码就会和这里对不上
      · 质量分是**数据属性**：同一个人问两次、换个人问，结果都必须一致；
        一旦有人把 user_id 拼进了评分，这里会立刻红

★ 全程不改动 `physical_map.yaml`：
    PUT 的用例只覆盖"应当被拒绝"的路径（缺确认 / 确认错 / 配置非法 / 非管理员），
    并逐条核对文件指纹 sha 与备份数量没有变化 —— 这样才能证明
    「校验发生在备份与写入之前」，而不是"写坏了再回滚"。

退出码：0 全过；1 有失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = True
total = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, total
    total += 1
    if not cond:
        passed = False
    print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""), flush=True)


def _level(score: float) -> str:
    """与服务端约定一致的三档（这里重算一遍，防止 level 与 score 脱钩）。"""
    if score >= 0.8:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"


def main() -> int:
    # Windows 控制台默认 GBK：代码页与 Python 侧要一起切，否则中文全成乱码
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # noqa: BLE001 —— 输出被重定向时没有控制台
            pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 老终端不支持就算了
        pass

    try:
        import warnings

        # starlette 对 httpx 的弃用提示与本次校验无关，别把它混进验收输出
        warnings.filterwarnings("ignore", message=".*httpx.*")

        from fastapi.testclient import TestClient

        from app.main import app
        from app.routers.admin import MAP_CONFIRM
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] 无法导入应用：{e}")
        return 1

    # 给一个固定客户端地址：部分鉴权/日志逻辑依赖 client.host
    c = TestClient(app, client=("127.0.0.1", 51000))

    # ── 一、质量分：全量 ────────────────────────────────────────
    r = c.get("/api/trust/quality", params={"user_id": "u001"})
    check("质量分全量 → 200", r.status_code == 200, f"status={r.status_code}")
    body = r.json() if r.status_code == 200 else {}
    items = body.get("items") or []

    check("返回了指标且数量自洽", bool(items) and body.get("count") == len(items),
          f"count={body.get('count')} items={len(items)}")

    scores = [i.get("score") for i in items]
    check("分数都在 [0,1]",
          all(isinstance(s, (int, float)) and 0.0 <= s <= 1.0 for s in scores))
    check("低分在前（便于先修最差的）", scores == sorted(scores), f"前 3 项={scores[:3]}")
    check("level 与 score 阈值一致",
          all(i.get("level") == _level(i.get("score", 0)) for i in items))
    check("signal 全部为 observed（不是模型打分）",
          all(i.get("signal") == "observed" for i in items))
    check("每个指标都有理由，不只给一个数",
          all(isinstance(i.get("reasons"), list) and i["reasons"] for i in items))
    check("低分项必须给出可读原因",
          all(i.get("reasons") for i in items if i.get("level") == "low"))

    # ── 二、质量分：单项 ────────────────────────────────────────
    probe = items[0]["metric"] if items else None
    if probe:
        r1 = c.get(f"/api/trust/quality/{probe}", params={"user_id": "u001"})
        one = r1.json() if r1.status_code == 200 else {}
        check("单项质量分 → 200 且与列表同源",
              r1.status_code == 200 and one.get("score") == items[0]["score"],
              f"{probe} score={one.get('score')}")

        r2 = c.get(f"/api/trust/quality/{probe}", params={"user_id": "u001"})
        check("同一指标重复查询结果稳定（确定性）",
              r2.status_code == 200 and r2.json().get("score") == one.get("score"))

    r = c.get("/api/trust/quality/__not_registered__", params={"user_id": "u001"})
    nr = r.json() if r.status_code == 200 else {}
    check("未注册指标 → unknown 而不是 500",
          r.status_code == 200 and nr.get("level") == "unknown" and nr.get("score") == 0.0,
          f"status={r.status_code} level={nr.get('level')}")

    r_other = c.get("/api/trust/quality", params={"user_id": "u002"})
    check("换个人问，质量分不该变（它是数据属性，不是个人视角）",
          r_other.status_code == 200 and r_other.json().get("items") == items)

    # ── 三、物理映射：读取与诊断 ────────────────────────────────
    r = c.get("/api/admin/physical-map", params={"user_id": "u005"})
    check("管理员读取物理映射 → 200", r.status_code == 200, f"status={r.status_code}")
    pm = r.json() if r.status_code == 200 else {}
    text = pm.get("text") or ""
    sha0 = pm.get("sha")
    check("映射文件存在且带指纹", bool(pm.get("exists")) and bool(sha0),
          f"path={pm.get('path')} sha={sha0}")
    check("确认短语由服务端下发（前端不许硬编码）",
          pm.get("confirm_phrase") == MAP_CONFIRM, f"phrase={pm.get('confirm_phrase')}")

    diag = pm.get("diagnose") or {}
    t, mapped, unmapped = diag.get("total"), diag.get("mapped_count"), diag.get("unmapped")
    check("诊断结果结构完整",
          isinstance(t, int) and isinstance(mapped, int) and isinstance(unmapped, list),
          f"total={t} mapped={mapped} unmapped={len(unmapped or [])}")
    if isinstance(t, int) and t > 0 and isinstance(mapped, int):
        check("覆盖度与计数口径一致",
              abs(float(diag.get("coverage") or 0) - mapped / t) < 1e-6,
              f"coverage={diag.get('coverage')} mapped/total={mapped}/{t}")

    backups0 = pm.get("backups")
    n_backup0 = len(backups0) if isinstance(backups0, list) else None

    # ── 四、物理映射：保存前预检 ────────────────────────────────
    r = c.post("/api/admin/physical-map/validate", params={"user_id": "u005"},
               json={"text": text})
    v = r.json() if r.status_code == 200 else {}
    check("预检：当前正文本身必须合法", r.status_code == 200 and v.get("ok") is True,
          f"errors={v.get('errors')}")

    r = c.post("/api/admin/physical-map/validate", params={"user_id": "u005"},
               json={"text": text + "\nfoo_bar: 1\n"})
    v = r.json() if r.status_code == 200 else {}
    check("预检：未知字段被拦下（拼错不会静默失效）",
          r.status_code == 200 and v.get("ok") is False
          and any("foo_bar" in str(e) for e in (v.get("errors") or [])),
          f"errors={v.get('errors')}")

    r = c.post("/api/admin/physical-map/validate", params={"user_id": "u005"},
               json={"text": "a: [未闭合"})
    check("预检：坏 YAML → 400 级错误而不是 500",
          r.status_code == 200 and r.json().get("ok") is False, f"status={r.status_code}")

    r = c.post("/api/admin/physical-map/validate", params={"user_id": "u005"},
               json={"text": ""})
    check("预检：空正文被拒（顶层必须是映射）",
          r.status_code == 200 and r.json().get("ok") is False)

    # ── 五、物理映射：保存必须二次确认，且校验先于写入 ──────────
    #  下面每个 PUT 都是"应当失败"的路径，正文用当前原文（即便被写也是同内容）
    r = c.put("/api/admin/physical-map", params={"user_id": "u005"},
              json={"text": text})
    check("PUT 缺 confirm → 400 且提示二次确认",
          r.status_code == 400 and "二次确认" in str(r.json().get("detail")),
          f"status={r.status_code}")

    r = c.put("/api/admin/physical-map", params={"user_id": "u005"},
              json={"text": text, "confirm": "确认"})
    check("PUT confirm 写错 → 400（不是「差不多就行」）", r.status_code == 400,
          f"status={r.status_code}")

    # 关键用例：confirm 正确但配置非法 → 必须在备份/写入之前被拦下
    bad_text = text + "\nunknown_key: 1\n"
    r = c.put("/api/admin/physical-map", params={"user_id": "u005"},
              json={"text": bad_text, "confirm": MAP_CONFIRM})
    check("PUT 确认正确但配置非法 → 400", r.status_code == 400, f"status={r.status_code}")

    r = c.get("/api/admin/physical-map", params={"user_id": "u005"})
    pm2 = r.json() if r.status_code == 200 else {}
    check("失败的写入没有改动文件（sha 不变）", pm2.get("sha") == sha0,
          f"{sha0} -> {pm2.get('sha')}")
    if n_backup0 is not None:
        check("失败的写入没有产生备份（校验在备份之前）",
              len(pm2.get("backups") or []) == n_backup0,
              f"{n_backup0} -> {len(pm2.get('backups') or [])}")

    # ── 六、鉴权：普通身份不得进入管理端 ──────────────────────
    r = c.get("/api/admin/physical-map", params={"user_id": "u002"})
    check("非管理员读物理映射 → 403", r.status_code == 403, f"status={r.status_code}")

    r = c.put("/api/admin/physical-map", params={"user_id": "u002"},
              json={"text": text, "confirm": MAP_CONFIRM})
    check("非管理员写物理映射 → 403（鉴权先于校验与写入）",
          r.status_code == 403, f"status={r.status_code}")

    r = c.get("/api/admin/physical-map", params={"user_id": "u005"})
    check("鉴权拒绝后文件仍然完好",
          r.status_code == 200 and r.json().get("sha") == sha0)

    print("-" * 60)
    print("质量分 / 物理映射 端点回归: " + ("全部通过" if passed else "存在失败")
          + f"（{total} 项）")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
