# -*- coding: utf-8 -*-
"""运行时配置：管理端界面上可改、可落盘、可热生效的配置。

与 app/config.py 的分工：
  - config.py   ：进程启动时的默认值 + 环境变量（运维注入密钥用）
  - 本模块      ：管理端改的配置，落盘到 var/runtime_config.yaml

优先级：环境变量 > 运行时配置 > 默认值。

为什么让环境变量优先：
  生产上换数据源、注入密钥是运维动作，不该被界面上一次误点「保存」盖掉。
  界面上会明确标出哪几项「已被环境变量接管」，避免管理员改了没反应还不知道为什么。

为什么密码类字段要单独处理：
  GET 一律脱敏（只回 *是否已配置*，不回原文）。否则一个只读的
  监控页就能把数据库密码和模型 Key 捞走。
"""

from __future__ import annotations

import os
import sys
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.config import ROOT, settings

_LOCK = threading.Lock()
_CFG_PATH = ROOT / "var" / "runtime_config.yaml"

# 敏感字段：GET 时脱敏，PUT 时若留空则沿用旧值
SECRET_KEYS = {
    ("llm", "api_key"),
    ("datasource", "his", "password"),
}

# ★ 进程内密钥（**不落盘**）：路径 → 值。
#   管理端保存的密钥只活在本次进程里，重启即失效 —— 这是刻意的，不是缺陷：
#   落盘等于把数据库口令与模型 Key 放进一个 `cat` 就能读的文件里，
#   而 config.py 开篇写的就是"敏感信息不落盘、不入库"（docs/12 §1.2 S6）。
#   生产要持久化就用环境变量注入（ASK_LLM_API_KEY / ASK_HIS_PASSWORD）——那是运维管辖的边界。
_MEM_SECRETS: Dict[Tuple[str, ...], str] = {}

DEFAULTS: Dict[str, Any] = {
    "llm": {"base_url": "", "api_key": "", "model": "", "timeout": 60},
    "datasource": {
        "active": "his",                      # his | ads
        "his": {"host": "", "port": 1521, "service": "", "user": "",
                "password": "", "thick": True},
        "ads": {"path": ""},
    },
}

# 运行时配置的键 → 对应 settings 属性名（put 时写回 settings）
_SETTINGS_MAP = {
    ("llm", "base_url"): "llm_base_url",
    ("llm", "api_key"): "llm_api_key",
    ("llm", "model"): "llm_model",
    ("llm", "timeout"): "llm_timeout",
    ("datasource", "active"): "data_source",
    ("datasource", "his", "host"): "his_host",
    ("datasource", "his", "port"): "his_port",
    ("datasource", "his", "service"): "his_service",
    ("datasource", "his", "user"): "his_user",
    ("datasource", "his", "password"): "his_password",
    ("datasource", "his", "thick"): "his_thick",
    ("datasource", "ads", "path"): "ads_path",
}

# 键 → 对应的环境变量名（判断是否被 env 接管）
_ENV_MAP = {
    ("llm", "base_url"): "ASK_LLM_BASE_URL",
    ("llm", "api_key"): "ASK_LLM_API_KEY",
    ("llm", "model"): "ASK_LLM_MODEL",
    ("llm", "timeout"): "ASK_LLM_TIMEOUT",
    ("datasource", "active"): "ASK_DS",
    ("datasource", "his", "host"): "ASK_HIS_HOST",
    ("datasource", "his", "port"): "ASK_HIS_PORT",
    ("datasource", "his", "service"): "ASK_HIS_SERVICE",
    ("datasource", "his", "user"): "ASK_HIS_USER",
    ("datasource", "his", "password"): "ASK_HIS_PASSWORD",
    ("datasource", "his", "thick"): "ASK_HIS_THICK",
    ("datasource", "ads", "path"): "ASK_ADS",
}


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> Dict[str, Any]:
    """读运行时配置（缺失/损坏都退回默认值，绝不让界面因缺文件而打不开）。

    盘上的文件**永远不含密钥**；进程内保存过的密钥在最后叠加回来，
    因此"保存后立即生效"这个体验不变（`apply_to_settings` 读的就是这份）。
    """
    if not _CFG_PATH.exists():
        cfg = deepcopy(DEFAULTS)
    else:
        try:
            with open(_CFG_PATH, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            cfg = _deep_merge(DEFAULTS, raw)
        except Exception:
            cfg = deepcopy(DEFAULTS)
    return _overlay_secrets(cfg)


def _node_for(cfg: Dict[str, Any], path: Tuple[str, ...],
              create: bool = False) -> Optional[Dict[str, Any]]:
    """走到 path 的父节点；create=False 时结构不对就返回 None。"""
    node: Any = cfg
    for k in path[:-1]:
        nxt = node.get(k)
        if not isinstance(nxt, dict):
            if not create:
                return None
            nxt = {}
            node[k] = nxt
        node = nxt
    return node


def _overlay_secrets(cfg: Dict[str, Any]) -> Dict[str, Any]:
    for path, value in _MEM_SECRETS.items():
        node = _node_for(cfg, path, create=True)
        if node is not None:
            node[path[-1]] = value
    return cfg


def _strip_secrets(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """落盘前的净化：密钥一律写成空串，绝不写进文件。"""
    out = deepcopy(cfg)
    for path in SECRET_KEYS:
        node = _node_for(out, path)
        if node is not None and path[-1] in node:
            node[path[-1]] = ""
    return out


def save(cfg: Dict[str, Any]) -> None:
    _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    safe = _strip_secrets(cfg)
    with _LOCK:
        with open(_CFG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(safe, f, allow_unicode=True, sort_keys=False)


def _remember_secrets(cfg: Dict[str, Any]) -> List[str]:
    """把 cfg 里的密钥搬进进程内存（非空才搬，空表示"本次没改"）。"""
    remembered: List[str] = []
    for path in SECRET_KEYS:
        node = _node_for(cfg, path)
        if node is None:
            continue
        v = node.get(path[-1])
        if isinstance(v, str) and v.strip():
            _MEM_SECRETS[path] = v
            remembered.append(".".join(path))
    return remembered


def migrate_secrets_to_memory() -> List[str]:
    """一次性净化：把**已经写在盘上**的明文密钥搬进内存，并把文件里的明文抹掉。

    为什么不是直接删：删了当前进程就丢了可用配置（表现为"改完代码模型突然不能用了"）。
    先搬进内存 ⇒ 本次运行不受影响；文件被净化 ⇒ 不留明文。
    代价是**重启后需要重新注入**（环境变量或管理端再填一次）——
    这个代价是刻意的，并且会在启动日志里说明。
    """
    if not _CFG_PATH.exists():
        return []
    try:
        with open(_CFG_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        return []
    moved = _remember_secrets(_deep_merge(DEFAULTS, raw))
    if moved:
        save(_deep_merge(DEFAULTS, raw))
    return moved


def secret_source(path: Tuple[str, ...]) -> str:
    """某个密钥当前从哪来：env（生产正确形态）/ memory（管理端刚填）/ file（明文遗留）/ none。"""
    env = _ENV_MAP.get(path)
    if env and os.getenv(env):
        return "env"
    if path in _MEM_SECRETS:
        return "memory"
    if _CFG_PATH.exists():
        try:
            with open(_CFG_PATH, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            node = _node_for(_deep_merge(DEFAULTS, raw), path)
            if node is not None and node.get(path[-1]):
                return "file"
        except Exception:
            pass
    return "none"


def update(section: str, values: Dict[str, Any],
           keep_empty_secrets: bool = True) -> Dict[str, Any]:
    """合并保存某个分区。

    keep_empty_secrets：前端不会回显密钥，所以提交时该字段必然为空；
    此时必须沿用旧值，否则「改个模型名」会把密钥一起清空。
    """
    cfg = load()
    cur = cfg.setdefault(section, {})
    for k, v in (values or {}).items():
        if keep_empty_secrets and (section, k) in SECRET_KEYS and not v:
            continue
        if isinstance(v, dict) and isinstance(cur.get(k), dict):
            for k2, v2 in v.items():
                if (keep_empty_secrets and (section, k, k2) in SECRET_KEYS and not v2):
                    continue
                if v2 != "" or not keep_empty_secrets:
                    cur[k][k2] = v2
                elif k2 in cur.get(k, {}):
                    continue
        elif v != "" or not keep_empty_secrets:
            cur[k] = v
    # ★ 先搬进内存再落盘：密钥只会出现在 `_MEM_SECRETS` 里，文件里一律是空串。
    _remember_secrets(cfg)
    save(cfg)
    return cfg


def _flatten(cfg: Dict[str, Any]) -> List[Tuple[Tuple[str, ...], Any]]:
    """把嵌套配置摊平成 (路径, 值) 列表。"""
    out: List[Tuple[Tuple[str, ...], Any]] = []

    def walk(prefix: Tuple[str, ...], node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(prefix + (k,), v)
        else:
            out.append((prefix, node))

    walk((), cfg)
    return out


def env_overridden() -> List[str]:
    """哪些配置项已被环境变量接管（界面只读，改了不生效）。"""
    out = []
    for path, _ in _flatten(load()):
        env = _ENV_MAP.get(path)
        if env and os.getenv(env) not in (None, ""):
            out.append(".".join(path))
    return out


def masked() -> Dict[str, Any]:
    """给界面看的配置视图：密钥脱敏，并标注 env 覆盖情况。"""
    cfg = live()
    over = set(env_overridden())
    for path in SECRET_KEYS:
        node = cfg
        for k in path[:-1]:
            node = node.get(k) or {}
            if not isinstance(node, dict):
                node = {}
        if isinstance(node, dict):
            node[path[-1]] = "******" if node.get(path[-1]) else ""
    return {"config": cfg,
            "env_overridden": sorted(over),
            "config_path": str(_CFG_PATH),
            "active_source": settings.data_source,
            # 密钥当前从哪来：env（生产正确形态）/ memory（管理端刚填，重启失效）/ file（明文遗留）
            "secret_sources": {".".join(p): secret_source(p) for p in sorted(SECRET_KEYS)}}


def live() -> Dict[str, Any]:
    """真实值（含密钥），仅供服务端内部使用。"""
    return load()


def llm_mode_desc() -> str:
    """当前问数实际会走哪条链路 —— 给启动日志用。

    与 `engine_bridge._build_llm()` 的优先级**必须一致**（真模型 > 规则 NLU > 无）。
    为什么值得单独打印：三种模式的产出不同却不报错，
    把"规则 NLU 的结果"误当成"模型结果"会让人查错方向（实测踩过）。
    """
    # ★ 必须与 `_build_llm()` 读**同一个来源**（settings），不能直接读配置文件。
    #   密钥的推荐姿势是走环境变量（docker-compose 明写"密钥只走环境变量"，
    #   且启动时 migrate_secrets_to_memory 会把盘上的明文密钥净化掉）。
    #   只看文件会把「已配置」误报成「未配置」——横幅说没模型、实际却能出数，
    #   这是最费排查时间的一类错（实测踩过）。
    base = str(settings.llm_base_url or "")
    has_key = bool(settings.llm_api_key)
    if base and has_key:
        return f"真实模型（{base}｜model={settings.llm_model or '(默认)'}）"
    if os.getenv("ASK_LLM_MOCK") == "1":
        return "规则 NLU（ASK_LLM_MOCK=1 降级链路；结果与真模型不同，仅供演示/冒烟）"
    return "未配置模型（ASK_LLM_BASE_URL / ASK_LLM_API_KEY 均缺失，问数会直接报错，不会编数）"


def apply_to_settings(reload_pipeline: bool = True, rebuild_conn: bool = True) -> Dict[str, Any]:
    """把运行时配置写进 settings，并让缓存失效。

    这就是「管理端点保存立刻生效」的实现：
      settings 是 dataclass，字段在导入时求值一次，运行期只能这样回写。

    环境变量优先：被 env 接管的项不覆盖（与 env_overridden() 保持一致）。
    """
    cfg = load()
    over = set(env_overridden())
    applied: List[str] = []
    for path, value in _flatten(cfg):
        attr = _SETTINGS_MAP.get(path)
        if not attr:
            continue
        key = ".".join(path)
        if key in over:
            continue
        if value in ("", None):
            continue
        try:
            setattr(settings, attr, value)
            applied.append(key)
        except Exception:
            pass

    if rebuild_conn:
        # 数据源参数可能变了，旧连接不能再用（含多源连接器缓存）
        try:
            from app.core.his import his
            his.close()
        except Exception:
            pass
        try:
            from app.core.sources import close_all
            close_all()
        except Exception:
            pass
    if reload_pipeline:
        try:
            from app.core import registry as _reg
            _reg.load_registry(force=True)
            from app.core import engine_bridge
            engine_bridge.get_pipeline(force=True)
        except Exception:
            pass
    return {"applied": applied, "skipped_env": sorted(over),
            "active_source": settings.data_source}


# ────────────────────────────────────────────────────────────
# 自测：python -m app.core.runtime_config --selftest
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    global _CFG_PATH
    import tempfile

    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    old_path, old_mem = _CFG_PATH, dict(_MEM_SECRETS)
    tmpdir = Path(tempfile.mkdtemp(prefix="ask_cfg_"))
    try:
        _CFG_PATH = tmpdir / "runtime_config.yaml"
        _MEM_SECRETS.clear()

        # ① 盘上遗留明文 → 迁移后文件被净化，但本次运行仍读得到值
        _CFG_PATH.write_text(
            "llm:\n  api_key: sk-LEAK-AAA\n  model: deepseek-chat\n"
            "datasource:\n  his:\n    password: P@ss-LEAK-BBB\n",
            encoding="utf-8")
        moved = migrate_secrets_to_memory()
        text = _CFG_PATH.read_text(encoding="utf-8")
        check("迁移：识别出两个明文密钥",
              sorted(moved) == ["datasource.his.password", "llm.api_key"], str(moved))
        check("迁移：文件里不再有明文 LLM Key", "LEAK-AAA" not in text)
        check("迁移：文件里不再有明文 HIS 口令", "LEAK-BBB" not in text)
        cfg = live()
        check("迁移：本次运行仍能读到 LLM Key（不因净化而丢配置）",
              cfg["llm"]["api_key"] == "sk-LEAK-AAA")
        check("迁移：本次运行仍能读到 HIS 口令",
              cfg["datasource"]["his"]["password"] == "P@ss-LEAK-BBB")
        check("迁移：来源标为 memory", secret_source(("llm", "api_key")) == "memory")

        # ② 管理端保存新密钥 → 立即生效、且绝不落盘
        update("llm", {"api_key": "sk-NEW-CCC", "model": "deepseek-chat"})
        text = _CFG_PATH.read_text(encoding="utf-8")
        check("保存：新密钥不落盘", "NEW-CCC" not in text)
        check("保存：新密钥立即对服务端可见", live()["llm"]["api_key"] == "sk-NEW-CCC")
        check("保存：非密钥字段照常落盘（不能因安全把功能做没了）",
              "deepseek-chat" in text)

        # ③ 留空表示"本次没改" → 不能把已有密钥抹掉
        update("llm", {"api_key": ""})
        check("保存：密钥留空不覆盖既有值",
              live()["llm"]["api_key"] == "sk-NEW-CCC")

        # ④ 界面回显：脱敏 + 来源说明
        m = masked()
        check("界面：LLM Key 脱敏显示", m["config"]["llm"]["api_key"] == "******")
        check("界面：带出密钥来源",
              m["secret_sources"]["llm.api_key"] == "memory",
              str(m["secret_sources"]))

        # ⑤ 环境变量优先（生产形态）：env 设了就不能被文件/内存盖掉
        os.environ["ASK_LLM_API_KEY"] = "sk-FROM-ENV"
        try:
            check("env 优先：来源标为 env", secret_source(("llm", "api_key")) == "env")
        finally:
            os.environ.pop("ASK_LLM_API_KEY", None)
    finally:
        _CFG_PATH = old_path
        _MEM_SECRETS.clear()
        _MEM_SECRETS.update(old_mem)

    print("=" * 60)
    print("自测结果: " + ("全部通过" if passed else "存在失败"))
    return passed


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
