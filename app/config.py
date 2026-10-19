# -*- coding: utf-8 -*-
"""全局配置。所有敏感信息只从环境变量读取，不落盘、不入库。"""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _b(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0") in ("1", "true", "True", "yes")


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    # ── 语义层资产（L3）──
    registry_path: str = field(default_factory=lambda: os.getenv(
        "ASK_REGISTRY", str(ROOT / "semantic-layer" / "metric_registry.yaml")))
    ambiguity_path: str = field(default_factory=lambda: os.getenv(
        "ASK_AMBIGUITY", str(ROOT / "semantic-layer" / "ambiguity_rules.yaml")))
    physical_map_path: str = field(default_factory=lambda: os.getenv(
        "ASK_PHYSICAL_MAP", str(ROOT / "semantic-layer" / "physical_map_his.yaml")))
    # 院方业务配置：科室分组 + 口径规则（给医院管理员改，不是技术映射）
    hospital_profile_path: str = field(default_factory=lambda: os.getenv(
        "ASK_HOSPITAL_PROFILE", str(ROOT / "semantic-layer" / "hospital_profile.yaml")))
    # 召回用词表：同义词归一 + 主题域扩展（只管"怎么找到指标"，不含口径）
    synonyms_path: str = field(default_factory=lambda: os.getenv(
        "ASK_SYNONYMS", str(ROOT / "semantic-layer" / "synonyms.yaml")))
    # 执行层限流（保护 HIS 只读库）：并发闸 + 每分钟速率闸，见 app/core/ratelimit.py
    query_max_concurrent: int = field(default_factory=lambda: _i("ASK_QUERY_MAX_CONCURRENT", 4))
    query_per_minute: int = field(default_factory=lambda: _i("ASK_QUERY_PER_MINUTE", 120))
    query_acquire_timeout_ms: int = field(default_factory=lambda: _i("ASK_QUERY_WAIT_MS", 3000))
    # Few-shot 示例（G2）：**默认关闭**。
    # 它是"待验证的增益"而非"已证明的收益"——先用 eval/run_ablation.py 跑出
    # G1 vs G2 的差值，再决定是否打开；默认开启等于拿生产流量做实验。
    examples_path: str = field(default_factory=lambda: os.getenv(
        "ASK_EXAMPLES", str(ROOT / "semantic-layer" / "examples.yaml")))
    fewshot_enabled: bool = field(default_factory=lambda: _b("ASK_FEWSHOT", False))
    # 数据源：his = 直连 HIS 镜像（B 路线）；ads = 本地 ADS 宽表（A 路线）
    data_source: str = field(default_factory=lambda: os.getenv("ASK_DS", "his"))
    ads_path: str = field(default_factory=lambda: os.getenv(
        "ASK_ADS", str(ROOT / "var" / "ads.db")))
    dialect: str = field(default_factory=lambda: os.getenv("ASK_DIALECT")
                         or ("sqlite" if os.getenv("ASK_DS", "his") == "ads" else "oracle"))
    source_domains_path: str = field(default_factory=lambda: str(ROOT / "app" / "config" / "source_domains.yaml"))
    # 数据源注册表（F8.1）：多源接入在抽取层；此处只存「环境变量名」，不存密钥
    sources_path: str = field(default_factory=lambda: os.getenv(
        "ASK_SOURCES", str(ROOT / "app" / "config" / "sources.yaml")))
    roles_path: str = field(default_factory=lambda: str(ROOT / "app" / "config" / "roles.yaml"))

    # ── HIS 只读数据源（L1）──
    his_host: str = field(default_factory=lambda: os.getenv("ASK_HIS_HOST", "10.1.33.46"))
    his_port: int = field(default_factory=lambda: _i("ASK_HIS_PORT", 1521))
    his_service: str = field(default_factory=lambda: os.getenv("ASK_HIS_SERVICE", "oracle10"))
    his_user: str = field(default_factory=lambda: os.getenv("ASK_HIS_USER", "his"))
    his_password: str = field(default_factory=lambda: os.getenv("ASK_HIS_PASSWORD", ""))
    his_thick: bool = field(default_factory=lambda: _b("ASK_HIS_THICK", True))

    # ── 物理映射（F5.3）──
    # 两条路线各一份：HIS 直连（B 路线）与本地 ADS 宽表（A 路线）。
    # ★ 为什么 ADS 也要一份：语义层声明了、宽表没物化的列（三种科室口径、院区）
    #   在这里声明"落回哪一列"，否则注入 RLS 时会直接报 no such column。
    physical_map_ads_path: str = field(default_factory=lambda: os.getenv(
        "ASK_PHYSICAL_MAP_ADS", str(ROOT / "semantic-layer" / "physical_map_ads.yaml")))

    # ── 本地元数据库（快照/反馈/偏好/授权台账，不含业务数据）──
    #   ★ v0.20 起 `audit_log` **不再写在这里**：审计迁到独立库（见下），
    #     老库里的历史行由 `audit.migrate_from_meta()` 一次性搬走（迁完如实标 legacy）。
    meta_db_path: str = field(default_factory=lambda: os.getenv(
        "ASK_META_DB", str(ROOT / "var" / "askdata.db")))

    # ── 审计独立存储（docs/12 §6.2）──
    #   ⚠️ 只把审计"换个文件"并不等于不可篡改：同一个 DBA 两个库都能改。
    #   真正起作用的是这三件事，本产品三件都做：
    #     ① HMAC 链 —— 密钥**不在库里**（环境变量或独立密钥文件），
    #        所以能改库的人没有密钥，重算不出正确的链；
    #     ② 链锚（anchor）—— 定期把 (条数, 尾哈希, 密钥指纹) 追加到**另一个位置**，
    #        整链重算或截断尾部都会与锚不符；
    #     ③ 库级只允许追加 —— SQLite 触发器对 UPDATE/DELETE 直接 RAISE，
    #        挡住"顺手改一行"这类绝大多数情况（挡不住 DROP TRIGGER，那由 ① 兜）。
    audit_path: str = field(default_factory=lambda: os.getenv(
        "ASK_AUDIT_DB", str(ROOT / "var" / "audit.db")))
    # 密钥来源二选一：环境变量（推荐，由信息科统一分发）或密钥文件（自动生成 0600）
    audit_hmac_key: str = field(default_factory=lambda: os.getenv("ASK_AUDIT_HMAC_KEY", ""))
    audit_key_path: str = field(default_factory=lambda: os.getenv(
        "ASK_AUDIT_KEY_FILE", str(ROOT / "var" / ".audit_hmac_key")))
    # 链锚文件：只追加。正式上线建议指向**另一个存储**（共享盘 / 日志平台采集的目录）
    audit_anchor_path: str = field(default_factory=lambda: os.getenv(
        "ASK_AUDIT_ANCHOR", str(ROOT / "var" / "audit.anchor.jsonl")))
    # 每写 N 条自动落一个锚（也可由运维手动 seal）
    audit_anchor_every: int = field(default_factory=lambda: _i("ASK_AUDIT_ANCHOR_EVERY", 200))
    # 可选镜像：每写一条同时追加一行 JSONL（便于被日志平台/WORM 采集）
    audit_mirror_path: str = field(default_factory=lambda: os.getenv("ASK_AUDIT_MIRROR", ""))

    # ── 模型（L5，须指向院内私有化服务）──
    llm_base_url: str = field(default_factory=lambda: os.getenv("ASK_LLM_BASE_URL", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("ASK_LLM_MODEL", ""))
    llm_api_key: str = field(default_factory=lambda: os.getenv("ASK_LLM_API_KEY", ""))
    llm_timeout: int = field(default_factory=lambda: _i("ASK_LLM_TIMEOUT", 60))

    # ── 身份来源（docs/12 §3.4）──
    # demo    = 示例身份（仅限本机演示；身份可切换）
    # gateway = 信任网关注入的可信头（生产）
    # sso     = CAS / OIDC 自校验票据（生产）
    # ★ 客户端据此决定「是否显示演示身份切换器」：非 demo 模式下必须消失。
    auth_mode: str = field(default_factory=lambda: os.getenv("ASK_AUTH_MODE", "demo"))

    # ── 身份授信 · SSO 对接（docs/12 §3.2 / §3.5）──
    # ⚠️ 这些值**必须由院方提供**（见 docs/12 §3.5 对接清单）。未配置时
    #    `sso` 模式不猜身份：一律 401，并在返回值里说清缺哪一项 —— 不静默降级。
    sso_protocol: str = field(default_factory=lambda: os.getenv("ASK_SSO_PROTOCOL", "cas").lower())
    cas_base_url: str = field(default_factory=lambda: os.getenv("ASK_CAS_BASE", "").rstrip("/"))
    # CAS 校验票据的属性名 → 主体字段的映射（属性名由院方身份平台决定）
    cas_service_validate_path: str = field(default_factory=lambda: os.getenv(
        "ASK_CAS_VALIDATE_PATH", "/serviceValidate"))
    oidc_issuer: str = field(default_factory=lambda: os.getenv("ASK_OIDC_ISSUER", "").rstrip("/"))
    oidc_client_id: str = field(default_factory=lambda: os.getenv("ASK_OIDC_CLIENT_ID", ""))
    oidc_client_secret: str = field(default_factory=lambda: os.getenv("ASK_OIDC_CLIENT_SECRET", ""))
    oidc_scopes: str = field(default_factory=lambda: os.getenv("ASK_OIDC_SCOPES", "openid profile"))
    # 本服务的对外地址（回调必须与 CAS/OIDC 侧登记的完全一致，否则票据校验必失败）
    sso_service_url: str = field(default_factory=lambda: os.getenv("ASK_SSO_SERVICE_URL", "").rstrip("/"))
    sso_timeout: int = field(default_factory=lambda: _i("ASK_SSO_TIMEOUT", 8))
    # 默认**校验** TLS 证书（内网自签证书请把 CA 装进系统信任库，不要关卡检查）
    sso_verify_tls: bool = field(default_factory=lambda: _b("ASK_SSO_VERIFY_TLS", True))
    # SSO 属性 → 主体字段。工号是必填（没有工号就绑不到授权表）
    attr_user: str = field(default_factory=lambda: os.getenv("ASK_ATTR_USER", "uid"))
    attr_name: str = field(default_factory=lambda: os.getenv("ASK_ATTR_NAME", "cn"))
    attr_dept: str = field(default_factory=lambda: os.getenv("ASK_ATTR_DEPT", "deptCode"))
    attr_campus: str = field(default_factory=lambda: os.getenv("ASK_ATTR_CAMPUS", "campusCode"))

    # ── 身份授信 · 网关模式加固（docs/12 §3.4 gateway 行）──
    # 只在**这些来源**注入了可信头时才认身份；其余来源一律 401。
    # 为空 = 未配置白名单 → 启动时告警（见 security_gate），且请求级拒绝（fail-closed）。
    trusted_proxies: list = field(default_factory=lambda: [
        x.strip() for x in os.getenv("ASK_TRUSTED_PROXIES", "").split(",") if x.strip()])

    # ── 服务端会话（docs/12 §3.3）──
    session_cookie_name: str = field(default_factory=lambda: os.getenv("ASK_SESSION_COOKIE", "ask_session"))
    session_ttl_hours: int = field(default_factory=lambda: _i("ASK_SESSION_TTL_HOURS", 8))
    session_idle_minutes: int = field(default_factory=lambda: _i("ASK_SESSION_IDLE_MIN", 30))
    # 管理端会话更短：能改数据源与口径的会话被劫持，后果与"看数"不是一个量级
    admin_session_idle_minutes: int = field(default_factory=lambda: _i("ASK_ADMIN_SESSION_IDLE_MIN", 15))
    session_cookie_secure: bool = field(default_factory=lambda: _b("ASK_SESSION_COOKIE_SECURE", False))

    # ── 本地账号兜底（docs/12 §3.6）──
    # off       = 无本地登录入口（默认；院方 SSO 已稳定时用）
    # breakglass= SSO 为主，本地账号仅作**应急通道**（默认 disabled，启用需依据 + 有效期）
    # primary   = 本地账号为主入口（SSO 还没接上时；SSO 若已配置仍可作第二入口）
    # ★ 它**不改变** ASK_AUTH_MODE 的语义：本地登录与 SSO 共用同一条服务端会话链路，
    #   靠 user_session.auth_source（local / cas / oidc）区分来源 —— 这正是"长期兜底"能成立的原因。
    auth_local: str = field(default_factory=lambda: os.getenv("ASK_AUTH_LOCAL", "off").strip().lower())
    # 口令策略：长度优先，不强制"大小写+符号"（NIST SP 800-63B 的做法 ——
    # 复杂度规则会把人逼向 Passw0rd! 这类可预测口令）
    # ★ 默认 8 位，且 `local_accounts.check_policy()` 里有 **8 位硬下限**：
    #   配置只能把它调高，不能调到 8 以下（省得一次误配把全线口令变成 4 位）。
    local_pwd_min_len: int = field(default_factory=lambda: _i("ASK_LOCAL_PWD_MIN_LEN", 8))
    # 禁止复用最近 N 次口令（0 = 不检查）
    local_pwd_history: int = field(default_factory=lambda: _i("ASK_LOCAL_PWD_HISTORY", 3))
    # 口令有效期（天）：0 = 不强制定期更换（同样来自 NIST 的取舍 —— 定期换口令的
    # 实际结果是"末尾加个 1"）。等保如明确要求，设成 90 即可。
    local_pwd_max_days: int = field(default_factory=lambda: _i("ASK_LOCAL_PWD_MAX_DAYS", 0))
    # 失败锁定：连续错 N 次锁 M 分钟；锁定期内**正确口令也拒绝**（否则爆破照样成功）
    local_max_fails: int = field(default_factory=lambda: _i("ASK_LOCAL_MAX_FAILS", 5))
    local_lock_minutes: int = field(default_factory=lambda: _i("ASK_LOCAL_LOCK_MINUTES", 15))
    # breakglass 档位下的账号数上限：兜底账号多到一定程度，它就不再是"应急通道"，
    # 而是一套未经 SSO 治理的平行账号体系。
    local_breakglass_max: int = field(default_factory=lambda: _i("ASK_LOCAL_BREAKGLASS_MAX", 5))

    # ── 授权台账（F6.2 · docs/12 §4.2）──
    # auto  = 台账里没有该用户 → 回落 roles.yaml.users（**迁移期**用法）
    # table = 台账是唯一来源，台账里没有就是无授权（**上线后**必须切到这个）
    # yaml  = 只看 roles.yaml（台账只作留痕，供排障对照）
    # ★ 为什么不直接删掉 yaml 分支：上线当天把全院的授权一次性搬进台账，
    #   一点遗漏就是"一批人突然查不了数"。迁移期允许双读，但**必须能收敛**，
    #   收敛的开关就是它（管理端会显示当前档位与台账覆盖率）。
    authz_from: str = field(default_factory=lambda: os.getenv("ASK_AUTHZ_FROM", "auto"))
    authz_sync_path: str = field(default_factory=lambda: os.getenv(
        "ASK_AUTHZ_SYNC", str(ROOT / "semantic-layer" / "authz_sync.yaml")))
    # 授权即将到期的提醒窗口（天）：临时授权到期前要有人管，否则到期当天就是事故
    authz_expiry_warn_days: int = field(default_factory=lambda: _i("ASK_AUTHZ_EXPIRY_WARN_DAYS", 14))

    # ── 安全与可信（F2 / F6）──
    small_cell_threshold: int = field(default_factory=lambda: _i("ASK_SMALL_CELL", 5))
    max_rows: int = field(default_factory=lambda: _i("ASK_MAX_ROWS", 5000))
    query_timeout: int = field(default_factory=lambda: _i("ASK_QUERY_TIMEOUT", 30))
    export_watermark: bool = field(default_factory=lambda: _b("ASK_EXPORT_WATERMARK", True))
    audit_enabled: bool = field(default_factory=lambda: _b("ASK_AUDIT", True))

    # 源域停更判定阈值（天）：超过则标记 source_stale
    stale_after_days: int = field(default_factory=lambda: _i("ASK_STALE_AFTER_DAYS", 45))

    # 结算截止日：财务类指标在此日之前一律 provisional
    settle_cutoff_day: int = field(default_factory=lambda: _i("ASK_SETTLE_CUTOFF_DAY", 5))

    def ensure_dirs(self) -> None:
        Path(self.meta_db_path).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
