# 医院智能问数产品设计文档集

> 定位：面向三甲医院（含多院区）的**自然语言问数 + 指标语义层**产品建设方案。
> 核心判断：问数产品的难点不在大模型，而在**指标口径治理**。因此本方案以「语义层」为资产核心，以「问数引擎」为可替换耗材层。

## 文档导航

| 编号 | 文档 | 内容 | 主要读者 |
|---|---|---|---|
| 01 | [架构总体方案](docs/01-架构总体方案.md) | 六层架构、技术路线决策、选型总表、部署架构、与 Foundry 的映射 | 架构师 / 技术负责人 |
| 02 | [数据模型设计](docs/02-数据模型设计.md) | 分层规范、十大主题域、DDL 示例、时间/科室口径建模、OMOP 适配 | 数据工程师 / 数仓 |
| 03 | [指标字典与语义层建模](docs/03-指标字典与语义层建模.md) | 指标分层、指标字典模板、三种语义层 YAML 示例、歧义词表 | 数据产品经理 / BI |
| 04 | [问数引擎设计](docs/04-问数引擎设计.md) | 意图路由、反问澄清、受限生成、自我修正、评估体系、提示词 | 算法 / 后端 |
| 05 | [安全合规与权限](docs/05-安全合规与权限.md) | 数据分级、行列级权限、脱敏矩阵、审计、等保密评 | 安全 / 合规 / 信息科 |
| 06 | [开源选型与 Foundry 平替](docs/06-开源选型与Foundry平替.md) | 按能力域拆解平替、选型评分矩阵、三套推荐组合 | 架构师 / 采购 |
| 07 | [POC 验证方案](docs/07-POC验证方案.md) | 语义层 POC、问数引擎 POC、开源组件验证清单与决策门 | 技术负责人 |
| 08 | [MVP 实施排期与团队](docs/08-MVP实施排期与团队.md) | 12 周排期、团队配置、里程碑、风险登记册、预算 | 项目经理 / 管理层 |
| 09 | [Golden 集标注规范](docs/09-Golden集标注规范.md) | 评估基准集：问题来源、覆盖矩阵、字段定义、标注流程、一致性判定 | 数据 PM / 算法 / 业务 |
| 10 | [创新设计与可行性评估](docs/10-创新设计与可行性评估.md) | 9 个创新点，每项附严谨性论证、工程量、做/不做判定 | 产品 / 架构师 |
| 11 | [产品功能设计](docs/11-产品功能设计.md) | 八个功能模块 F1~F8、核心交互流程、版本规划、HIS 实测驱动的设计要求 | 产品 / 全体 |
| 12 | [身份认证与权限落地设计](docs/12-身份认证与权限落地设计.md) | 身份授信（SSO/网关）、可治理授权、多院区围栏接线、审计不可篡改、五组验收用例 | 安全 / 后端 / 信息科 |
| 13 | [界面产品化设计](docs/13-界面产品化设计.md) | 问数客户端与语义层后台的信息架构、五态卡片规范、后台页面×接口对齐、前端工程化决策、界面级验收 | 产品 / 前端 / 信息科 |

## 参考实现（已跑通验证）

```
engine/
├── dsl.py           查询 DSL —— LLM 与语义层之间的唯一契约，含口径合法性校验
├── sql_builder.py   语义层 → SQL 生成器（正确性地基）+ 自测
├── attribution.py   结构-价格两因素分解器（docs/10 IN-04）+ 恒等式自测
└── pipeline.py      ★ 端到端管线：患者级门禁→召回→LLM→校验→意图→权限→反问→SQL
                        可插拔 LLM 适配器（MockOracle / Perturbed / HTTP）
                        多轮：`context` = 上一轮**实际执行的有效 DSL**，注入提示词并记 trace

app/
├── core/identity.py   ★ 身份授信（F6.1）：demo / gateway（可信头 + **来源白名单**）/
│                         sso（CAS / OIDC 票据校验）三态；网关模式的伪造头在入口被剥离
├── core/session.py    ★ 服务端会话（F6.1）：Cookie 只放不透明令牌；登出**服务端立即失效**；
│                         绝对 8h + 空闲 30min（管理端 15min）；踢账号可一键下线
├── core/authz_store.py ★ 授权台账（F6.2）：谁能看哪些科室/到什么时候/依据是什么；
│                         同一人同一时刻只有一条有效授权；空范围**不回落**角色默认值
├── core/authz_sync.py  组织架构同步（F6.2）：映射表 + 增量计划；映射不到**整体不导**
├── core/audit.py      ★ 审计独立存储（F6.5）：独立库 + **HMAC 链（密钥在库外）** +
│                         **链锚**（检出"删掉尾部记录"）+ 库级只允许追加 + 遗留数据迁移
├── core/export_trace.py 导出流水号与校验位（F4.4）：EX-YYYYMMDD-SSSS-CC（ISO 7064 MOD 97-10）
├── core/chat.py       会话与消息（F1.9）：归属校验 + 渲染载荷裁剪（rows ≤ 200 行）
├── core/rule_llm.py   无模型时的降级 NLU；含**有限的**多轮指代消解（换粒度/换范围/加对比/指定或排除科室）
├── routers/chat.py    /api/chat：会话建/列/读/改名/软删 + 回放
├── web/index.html     ★ 问数客户端：三栏（会话历史 · 对话流 · 口径与证据），零 CDN 零构建
│                        一轮 = 问句气泡 + 独立卡片 + 独立证据区（多轮互不覆盖）
└── web/admin.html     ★ 管理端：导航按「资产/治理/平台/运营」分组
                         资产：指标目录与编辑（F5.1/F5.2/F5.6）+ 版本留痕 + 本体总览
                         治理：口径规则与科室分组 + 口径对账（F5.7，如实展示待配置）
                         平台：模型服务 + 数据源　运营：运维与时效 + 只读守门体检

eval/
├── run_golden.py    评估执行器，含 MockOracle / MockPerturbed 自测引擎
└── validate_golden.py  标注质量校验

scripts/
├── probe_his.py       HIS 只读盘点：meta(跨schema规模) / find(按关键字定位表)
│                      / dict(字典专项) / profile(列·行数·时间跨度·空值率)
├── check_auth_flow.py ★ 身份授信 + 授权台账验收（docs/12 §7 A·B 组，**43 项**）：自带 mock 统一
│                        身份平台（CAS + OIDC），起真服务走真 HTTP —— 登录跳转 / 票据校验 / 建会话 /
│                        Cookie 属性 / 伪造头无效 / 登出立即失效 / 身份事件留痕 / demo 绑 0.0.0.0 起不来 /
│                        **台账授权后科室级用户真能出数且 RLS 真收口** / 撤销即时生效 / 留痕链完整
├── sync_authz.py      ★ 组织架构 → 授权台账同步（默认 dry-run）：`--source yaml` 迁移 /
│                        `--source file` 院方导出（CSV/JSON）；`--revoke-missing` 是危险开关
├── reconcile_his.py   口径对账(F5.7)：语义层计算值 vs HIS 厂商报表 RPT_DEPT_MONTH，
│                      差异定位到统计科室级，输出 JSON 报告
├── build_ads.py       A 路线：从 HIS 抽取住院域建 ADS 宽表 dwd_fact_inpatient_discharge，
│                      同时产出生产环境 Doris DDL
├── check_ui_states.js 界面状态卡验收（docs/13 §8.1）：把客户端脚本抽出来在 Node 里跑，
│                      喂真实接口 payload，断言六态各自渲染成不同的卡 + 对话流端到端（69 项，不用浏览器）
├── check_admin_states.js 管理端验收（24 项）：门禁、指标编辑器界面不变式、写接口契约
│                      ★ 只对语义层**副本**跑，脚本自带"路径不含 _admin_check 就跳过写操作"的保险
└── gen_hospital_profile.py  生成院方业务配置 hospital_profile.yaml
                      （科室分组 + 口径规则），从 HIS 读预置，院方手改
```

`sql_builder.py` 强制执行的正确性约束：ratio 先聚合后相除、日期半开区间、除法 `NULLIF`、行级权限 SQL 层注入、小样本 `HAVING` 抑制、口径编造拦截。

`pipeline.py` 额外把三条**安全边界**变成硬约束（都由自测反推得出，见 docs/04 §1）：

| 约束 | 拦截的事故 |
|---|---|
| 患者级门禁最前，命中即 `denied` | 「患者名单」被判成超范围后只回一句「暂不支持」，不拒绝不留痕 |
| 权限门禁早于反问 | 越权请求先被反问 → 既漏放（need_clarify）又泄露「这份数据存在」 |
| 时间范围不得静默取默认 | 模型漏给 time 也能出数，用户拿到口径不明的数字 |

```
python engine/pipeline.py --selftest          # 端到端 37 项自测（含多轮上文）
python engine/sql_builder.py                  # 语义层 → SQL，含 2 条反向测试
python engine/attribution.py --selftest       # 分解器，15 项自测
python -m app.core.permission --selftest      # 列级权限：目录层 vs 执行层一致性对拍
python -m app.core.sources --selftest         # 多源注册：密钥不落盘 + 域→源映射完整性
python -m app.core.rule_llm --selftest        # 降级 NLU 的多轮继承（含"没有上文就不许猜"）
python -m app.core.identity --selftest        # 身份授信 38 项（白名单/CAS/OIDC/伪造头/属性映射）
python -m app.core.session --selftest         # 服务端会话 21 项（空闲/绝对过期/登出即失效/踢账号）
python -m app.core.authz --selftest           # 身份与授权边界 21 项（401 语义、参数不可自报身份）
python -m app.core.authz_store --selftest     # 授权台账 25 项（依据必填/唯一有效/生效期/坏数据从严）
python -m app.core.audit --selftest           # ★ 审计 13 项（只允许追加/HMAC/链锚防截断/迁移保真/并发）
python -m app.core.export_trace --selftest    # 导出流水号 8 项（校验位/改一位/非法日期/边界序号）
python -m app.core.security_gate --selftest   # 安全闸门 37 项（A4 启动闸门 / 伪造头剥离 / 配置自检）
node scripts/check_ui_states.js <BASE>        # 界面验收 73 项（六态卡片 + 对话流端到端）
node scripts/check_admin_states.js <BASE>     # 管理端验收 59 项（须指向 ASK_REGISTRY 为副本的服务）
python scripts/check_auth_flow.py             # ★ 身份+授权+审计端到端 48 项（自带 mock SSO，不用院方环境）
python eval/run_golden.py --golden eval/golden_set_sample.jsonl \
    --engine mock_oracle --registry semantic-layer/metric_registry.yaml
# → 结果正确率 100%、越权漏放 0、评估器自测 [OK]

# POC-B：接真实模型跑 Golden 集（API Key 只走环境变量，不要写进仓库/命令行）
$env:ASK_LLM_API_KEY = "sk-xxx"
python eval/poc_b_real_llm.py --base-url https://api.deepseek.com --model deepseek-chat \
    --output eval/reports/poc_b.json --md eval/reports/poc_b.md

# 消融对比：G0 裸跑 SQL / G1 语义层 / G3 完整方案（支持多模型）
python eval/run_ablation.py --groups g0,g1,g3 \
    --model "deepseek-chat@https://api.deepseek.com" \
    --md eval/reports/ablation_deepseek.md

# Golden 集覆盖度体检（只算缺口，不生成题目）
python eval/check_coverage.py --target 300 --md eval/reports/golden_gap.md
```

> ⚠️ 公网 API 仅用于评测跑通；**院内正式环境必须指向院内网私有化部署的模型服务**，
> 且问句中不得含任何患者信息。

## 服务端（v0.1 · F1~F8 骨架已跑通）

```powershell
$env:ASK_HIS_PASSWORD  = "***"   # 只读账号（禁用带写权限的应用账号）
$env:ASK_HIS_THICK     = "1"     # Oracle 11g 必须 thick mode
$env:ASK_LLM_BASE_URL  = "http://..."   # 院内私有化模型服务
$env:ASK_LLM_API_KEY   = "***"
uvicorn app.main:app --host 127.0.0.1 --port 8000     # → http://127.0.0.1:8000
```

### 容器化（Docker Compose）

```bash
docker compose up -d --build            # → http://127.0.0.1:8000
docker compose logs -f app              # 启动日志会打印鉴权模式 / 模型模式 / 审计迁移结果

# 首次建管理员（一次性口令只显示一次，登录后强制改密）
docker compose exec app python -m app.core.local_accounts \
    --create-admin u0001 --name 信息科管理员 --reason 部署单号-2026-1017 --role 信息科
```

三条约定（改之前先看 `docker-compose.yml` 注释）：

| 约定 | 原因 |
|---|---|
| `var/` 必须挂到宿主机 | 元数据库、审计库、**审计链 HMAC 密钥**、运行时配置都在里面；不挂则重启即丢，审计链断裂 |
| `semantic-layer/` 建议挂 `:ro` | 口径调整不必重打镜像，改完调 `/api/admin/pipeline/reload` 热载；只读可防容器内误改资产 |
| 默认 `ASK_DS=ads`（本地宽表） | 直连 HIS 需要院方提供 Oracle Instant Client 11.2 + 授权，且必须 thick mode；镜像刻意不带 |

> ⚠️ **容器里必须换掉 `demo` 鉴权**。安全闸门在 demo 模式下绑非环回地址会**拒绝启动**，
> 这是刻意设计（宁可起不来，也不要"起得来、界面上一切正常、实际没有身份"）。
> `docker-compose.yml` 默认给的是 **`ASK_AUTH_MODE=sso` + `ASK_AUTH_LOCAL=primary`**。
> ★ 合法取值只有 `demo` / `gateway` / `sso`（`identity.VALID_MODES`）——「local」不是一个
> 身份模式，它是**本地账号这个凭证入口**，靠上面两项组合启用。写成 `ASK_AUTH_MODE=local`
> 会让每个请求返回 `bad_mode`（登录页都出不来）；写成 `ASK_AUTH_LOCAL=on` 不在
> `(primary|breakglass)` 内，登录入口不开（`security_gate.local_warnings` 会告警）。

**镜像未在本机实测**（开发机无 Docker）。`Dockerfile` 采用 `python:3.11-slim` + 非 root + 健康检查，
无编译型依赖；可构建性由 CI 的 `docker build` 步骤在流水线里验证。

### CI 门禁

```bash
python scripts/ci.py            # 14 项离线回归，约 4 秒
python scripts/ci.py --list     # 看清单
python scripts/ci.py --only 召回,端到端
```

门禁只包含**离线**检查（不连库、不调模型）——需要 HIS 或模型才能跑的检查进 CI，
会因环境抖动变红，团队很快就会学会"重跑一次"，门禁等于没有：

| 层 | 检查 |
|---|---|
| 生成器 | `sql_builder` 自测（JOIN 正反向 + 列限定器） |
| 主链路 | `pipeline --selftest`（31 条 golden + 召回/示例库/未上线指标） |
| 语义层 | `recall`（断词/泛化/同义词）、`fewshot`（合法性/防抄袭/选择策略） |
| 动作 | `attribution_service`（恒等式 + 拒绝路径）、`ops_stats`（榜单口径） |
| 可信度 | `quality`（新鲜度/稳定性/治理三档扣分）、`physical_map_store`（配置校验与覆盖度）；`check_quality_map_api` 走真接口验鉴权、二次确认与"校验先于写盘" |
| 评测 | `validate_golden`、`sql_snapshot`（17 条 SQL 逐字）、`eval_recall`、`run_golden --gate` |

`--gate` 的含义：**mock_oracle 必须满分**。完美引擎拿不到满分说明评估器或语义层被改坏了，
此时任何真实模型的分数都不可信。

CI 配置只做一层薄调用（`.github/workflows/ci.yml`）。换 GitLab / Jenkins 时改那一层即可，
**不要把检查项抄进各家 YAML** —— 抄一次就会漂一次。GitLab CI 对应写法：

```yaml
offline-checks:
  image: python:3.11-slim
  script:
    - pip install -r requirements.txt
    - python scripts/ci.py
```

**身份来源（F6.1 · `ASK_AUTH_MODE`）** —— 这一项决定"谁是谁"，配错的后果与配漏权限不是一个量级：

```powershell
# ① demo（默认）：身份可切换，仅限本机。绑 0.0.0.0 会**拒绝启动**（不是打印一条警告）
$env:ASK_AUTH_MODE = "demo"

# ② gateway：身份由网关注入可信头。必须配来源白名单，否则**所有请求 401**（fail-closed）
$env:ASK_AUTH_MODE       = "gateway"
$env:ASK_TRUSTED_PROXIES = "10.1.33.0/24,10.1.34.7"      # 只信这些来源注入的头

# ③ sso：服务端自校验票据（CAS 或 OIDC），身份落在服务端会话里
$env:ASK_AUTH_MODE       = "sso"
$env:ASK_SSO_PROTOCOL    = "cas"                         # cas | oidc
$env:ASK_CAS_BASE        = "https://sso.hospital.local/cas"
$env:ASK_SSO_SERVICE_URL = "https://ask.hospital.local"   # 必须与院方登记的完全一致
$env:ASK_ATTR_USER       = "uid"                          # 工号属性名（院方平台定）
$env:ASK_SESSION_COOKIE_SECURE = "1"                      # 内网走 HTTPS 后必须打开
```

三条**不依赖院方输入**、现在就能验的事实：

| 事实 | 怎么看 |
|---|---|
| 配错身份**不会静默降级**（拿不到可信身份就 401，不会回落到某个默认用户） | `python -m app.core.authz --selftest` |
| `gateway` 下伪造 `X-Auth-User: u001` 换不到院长身份（来源不在白名单 → 入口剥离该头） | `python -m app.core.identity --selftest` |
| 登出是**服务端**失效，不是只清 Cookie（旧 Cookie 再访问仍是 401） | `python scripts/check_auth_flow.py` |

⚠️ **与院方统一身份平台的联调尚未完成**：地址、属性名、证书、测试账号属院方提供项
（清单见 [docs/12 §3.5](docs/12-身份认证与权限落地设计.md)）。上面的三态是**框架与判定**已就绪，
联调时改配置即可，不改代码。

**授权台账（F6.2 · `ASK_AUTHZ_FROM`）** —— 身份解决"你是谁"，授权解决"你能看哪些科室"。
分工是刻意的：`roles.yaml` 只定义**角色能做什么**（可见指标 / 能否导出 / 能否看 SQL），
**谁能担任、覆盖哪些科室、到什么时候**在数据库台账里，由组织架构同步刷新：

```powershell
# ① 迁移：把 roles.yaml 的名单搬进台账（先看 dry-run 打印的计划，再 --apply）
python scripts/sync_authz.py --source yaml --ref "MIGRATE-v0.19"
python scripts/sync_authz.py --source yaml --ref "MIGRATE-v0.19" --apply

# ② 增量：从院方组织架构导出同步（列名/岗位映射改 semantic-layer/authz_sync.yaml）
python scripts/sync_authz.py --source file --file 组织结构.csv --ref "HRP-2026-10" --apply

# ③ 收敛：台账覆盖到 100% 后切严格模式（此后"从台账里删人"才等于真的收回权限）
$env:ASK_AUTHZ_FROM = "table"
```

三条设计上的取舍，验收时可直接看到：

| 结论 | 依据 |
|---|---|
| 台账里的**空范围 = 无授权**，不回落到角色的 `default_dept_scope`（YAML 来源才回落） | `python -m app.core.authz_store --selftest` |
| 一次不完整的组织架构导出**不会**撤掉名单外所有人的权限（`--revoke-missing` 默认关） | `python scripts/sync_authz.py --help` |
| 台账里改/撤授权**立刻生效**，不需要重启服务（数据范围不是缓存的） | `python scripts/check_auth_flow.py`（H15/H17） |

⚠️ **严格模式下有一个"鸡生蛋"**：台账为空时**没有人是管理员**，管理端进不去 ——
首启必须先用命令行跑一次迁移（`scripts/check_auth_flow.py` 的 H13 就是这条用例）。

**审计独立存储（F6.5 · docs/12 §6.2）** —— 等保测评必问"你怎么证明日志没被改过"。
审计已迁到独立库 `var/audit.db`，但**"换个库文件"本身解决不了问题**（同一个 DBA 两个库都能改）。
真正起作用的是三件事，缺一不可：

| 防线 | 做法 | 防不住什么 |
|---|---|---|
| HMAC 链 | 链哈希用 HMAC-SHA256，**密钥在库外**（`ASK_AUDIT_HMAC_KEY` 或 `var/.audit_hmac_key`） | 同时拿到密钥与库权限的人 |
| **链锚** | 每 N 条把 `(条数, 尾哈希, 密钥指纹)` 追加到**另一个位置** | 锚文件被一起改的人（建议锚指到另一块盘） |
| 库级只允许追加 | SQLite 触发器对 `UPDATE`/`DELETE` 直接 `RAISE(ABORT)` | `DROP TRIGGER` 后再改（由 HMAC 兜住） |

```powershell
$env:ASK_AUDIT_DB          = "D:/audit/audit.db"      # 审计库（建议与业务元数据不同盘）
$env:ASK_AUDIT_HMAC_KEY    = "<由信息科分发的密钥>"     # 不设则自动生成到 var/.audit_hmac_key
$env:ASK_AUDIT_ANCHOR      = "E:/worm/audit.anchor.jsonl"  # 链锚（建议另一块盘/另一台机器）
$env:ASK_AUDIT_ANCHOR_EVERY = "200"                    # 每 200 条自动封一个锚
```

⚠️ **密钥丢了，历史链就永久无法校验**（不是"读不出来"，是"永远说不清有没有被动过"）。
请把密钥与库一起纳入备份；**CLI 与服务必须用同一把密钥**，否则 `sync_authz.py`
写的留痕行会看起来像"被改过"。锚记录里的 `key_id` 用来区分"记录被改"与"换了密钥"。

| 事实 | 怎么看 |
|---|---|
| 直接 `UPDATE`/`DELETE` 审计行会被**库层拒绝** | `python -m app.core.audit --selftest` |
| 删一条 + 用公开算法**重算整链** → 仍判失败（密钥在库外） | 同上（这条是"哈希链"最常被高估的地方） |
| **只删尾部**（链仍自洽）→ 被链锚抓到 | 同上 |
| 迁移前的历史行如实标 `legacy`，且可用旧算法单独校验 | 同上（迁移**逐字保真**，NULL 不能被补成 0） |
| 一份流传到院外的文件，能按水印里的流水号反查 | 管理端「导出追溯」→ 反查流水号 |

| 模块 | 路由 | 实现要点 |
|---|---|---|
| F1 对话问数 | `/api/ask` | 复用 `engine/pipeline.py`，五态状态机；小样本上卷；非授权角色不返回 SQL；**同一 `session_id` 内自动带上上一轮有效 DSL 做指代消解** |
| F1.9 会话与历史 | `/api/chat` | 会话（建/列/读/改名/**软删**）+ 消息回放；消息只存渲染载荷（rows 截断 200 行）；**归属校验：他人会话一律 404** |
| F2 可信透明 | `/api/trust` | 口径条、成熟度四态（含 `source_stale`）、快照与复现、数字护城河、偏好记忆 |
| F3 分析能力 | `/api/analysis` | 对比/排名/趋势/下钻（继承上文口径）；归因带 DRG 分组器版本门禁 |
| F4 报表驾驶舱 | `/api/reports` | 角色驾驶舱；导出：水印含**导出流水号 `EX-YYYYMMDD-SSSS-CC`（带校验位）**，快照只认 `answer_id`；`/exports` 导出追溯（限管理员，谁何时导了什么）、`/exports/verify` 按流水号**反查真伪**（格式/校验位/日志三态） |
| F5 语义层后台 | `/api/semantic` | 指标注册（必须填责任人）、版本留痕、争议下线、口径对账入口。**写入语义是"合并"**：`None` = 保留原值（只改名称不会抹掉 `measure`/维度，也不会把 `restricted` 静默降成 `public`），返回 `changed[]` 供界面回显 |
| F6 权限安全 | `/api/security` | 角色/行级/列级、全量审计、**只读守门自检**（写语句必被拦）；**本地账号兜底**（v0.21 · docs/12 §3.6）：`/auth/local/status`、`/auth/local/login`、`/auth/local/password/change`、`/local/users`（建号 / 启用 / 停用 / 重置 / 解锁 / 续期，**口令哈希不出库**）；身份授信：`/session` 身份来源、`/login` + `/callback`（CAS/OIDC）、`/logout`（服务端立即失效）、`/sessions`（在线会话，**不含令牌**）、`/events`（身份事件留痕）；授权台账：`/grants` 台账与授权变更、`/grants/revoke`、`/grants/log`（变更链）、`/grants/sync`（组织架构同步：预览/写入，**界面只允许 yaml 源**）；审计：`/audit`、`/audit/summary`、`/audit/verify`（链 + 链锚）、`/audit/storage`（库/密钥/锚现状）、`/audit/seal`（封锚） |
| F7 运营闭环 | `/api/ops` | 四类反馈、打回自动进回流队列、运营指标统计 |
| F8 平台管理 | `/api/admin` | **源注册表**（`/sources/registry`，多源可配、密钥只存环境变量名）、源域时效探测（`streaming/t_plus_1/delayed/stale/unconfigured`）、模型与语义层状态 |

**已打通（v0.7）**：F5.3 物理映射 + Oracle 方言已落地，住院域 5 个指标在 HIS 镜像上真实出数；
ADS 宽表建成后只切 `ASK_DS=ads` 即可复用同一份语义层（结果逐位一致，**语义层零改动**）。

切换数据源：
```powershell
$env:ASK_DS = "his"      # B 路线：直连 HIS 镜像（Oracle 方言 + 物理映射）
$env:ASK_DS = "ads"      # A 路线：本地 ADS 宽表（SQLite 方言，语义层零改动）
python scripts/build_ads.py --since 2023-01-01     # 先建 ADS
```

**性能实测 —— 为什么必须走 A 路线**（2023 全年，同一份语义层）：

| 指标 | B 路线（直连 HIS） | A 路线（ADS 宽表） | 提速 |
|---|---|---|---|
| 住院收入 `ip_revenue` | 17,822 ms | 52 ms | **343×** |
| 耗占比 `material_rate` | 23,787 ms | 68 ms | **350×** |
| 出院人次 `ip_discharge_cnt` | 193 ms | 54 ms | 3.6× |

费用类指标直连是 20 秒级（`INP_BILL_DETAIL` 5550 万行全表扫），用户等不了。
更关键的是 `ip_avg_fee`（次均费用）**跨 `PAT_VISIT` 与 `INP_BILL_DETAIL` 两张表，
直连根本算不了**，只有 ADS 宽表能算 —— 实测 5,811 元/人次。

住院域 MVP 指标现状（2023 全年）：出院 59,653 / 入院 59,376 / 占用床日 387,362 /
院内死亡 39 / 平均住院日 6.49 天 / 住院收入 3.47 亿 / 耗占比 17.11 / 次均费用 5,811 元。

**院方自己定义的口径（改配置，不改代码）** —— `semantic-layer/hospital_profile.yaml`：

| 能力 | 配置段 | 说明 |
|---|---|---|
| 科室分组 | `dept_groups` | 哪些 HIS 科室算一组；统计科室只是预置的一种，可加「内科系统」「重点专科群」 |
| 口径规则 | `caliber_rules` | 哪些患者算不算（如是否计入新生儿），开关式，**默认全关**——口径由院方拍板，系统不替他决定 |

配置由 `scripts/gen_hospital_profile.py` 从 HIS 生成预置（128 个统计科室 / 233 条映射），
院方手改后跑 `scripts/reconcile_his.py` 即可验证 —— **对账与线上出数读同一份配置**，
不会各说各话。科室分组落地为语义层的 `dept_group` 维度（`hierarchy` 里早就声明过的位置）。

规则里的列名写 `{逻辑列名}` 占位符，加载时按数据源渲染：直连 HIS 用物理列名
（`NEWBORN`），ADS 用逻辑列名（`newborn`）。**两条路线结果逐位一致** ——
实测开启「不计新生儿」后两边都是 47,687。标 `his_only: true` 的规则
（如引用了 `COMM.DEPT_DICT` 的规则）在 ADS 上会明确跳过并告警，
而不是拼一条必然报错的 SQL 出来。

该机制已产出一个**用配置而不是用嘴确认**的结论：开启「不计新生儿」后，
差异从 -5,804 扩大到 -17,537 —— 说明 **HIS 报表是计入新生儿的**，这条规则不该开。

**当前断点**：
1. `1.1.1.1`/`1.1.1.2` 未闭合：报表有数而我方为 0，其映射到的 `010303`（神经内科门诊）、
   `010306`（心血管门诊）**是门诊科室**，报表却给它们记了住院出院人次，来源待查。
   注意这不是配置能解决的 —— 需要先搞清楚报表那一格的数据从哪来。
2. 药品/耗材分类四套口径并存（病案首页 / 财务 / 收据 / 结算），现用病案首页口径；
   院方要换口径，改 `physical_map_his.yaml` 的 `measures` 段即可。
3. 生产性能需 Doris 实环境验证 —— 本地 ADS 为 SQLite，提速比例成立，绝对值不代表 Doris。

## 尚未实现（诚实清单 · 按 F1–F8）

上面那是"有什么"，这一节是"没什么"。写出来的理由很直接：**边界应该由我们先说，
而不是被问到才承认**（与 [docs/12](docs/12-身份认证与权限落地设计.md) 的 S 系列、
[docs/13](docs/13-界面产品化设计.md) 的 C 系列同一个写法）。

### A. 文档声明 MVP、实际还缺的（风险最高）

| 编号 | 功能 | 现状与证据 |
|---|---|---|
| F5.4 | 字典映射管理（科室 / ICD / 收费 / 医保 / SCD2 科室变更） | **未开工**：`app/routers/semantic.py` 无接口，`/reconcile` 只回"需先由信息科确认 `ITEM_CODE` ↔ 指标码的映射（尚未配置）"（docs/13 §7 C4） |
| F5.2 | 指标变更**审批** | 只有"留痕 + 事后可查"，**没有审批流**：管理端版本留痕页明写"审批流尚未实现…不假装有审批按钮"（`app/web/admin.html`） |
| F8.5 | Golden 集 CI（回退强制阻断） | 回流队列与导出已有（`/api/ops/backlog`、`/backlog/export`）；**CI 本体与报告页未做**（管理端无此页） |
| F6.1 | SSO 与院方统一身份平台**联调** | 联调本身仍是**院方提供项**（地址/属性名/证书/测试账号，docs/12 §3.5）——改配置不改代码。**本地账号兜底已落地（v0.21）**：`ASK_AUTH_LOCAL=primary/breakglass`，只补凭证入口、不碰权限（权限仍 100% 来自台账）；scrypt 哈希 + 失败锁定（锁定期内正确口令也拒）+ 首登强制改密 + 会话与 SSO 同一条链路；首启用 `python -m app.core.local_accounts --create-admin` 自举 |
| F5.3 | 物理映射**后台接口** | 能力已落地（`physical_map.py` + Oracle 方言），缺 `GET/PUT /api/semantic/physical_map` → 后台页没有数据源，**只能改 YAML** |
| F2.3 | `restated` 态 | 四态里唯一的空位：**没有产出路径**（"谁是修正来源"未定），界面如实标"未接入"，不假装能算 |

### B. 规划内（P1/P2/P3）尚未开工

| 编号 | 功能 | 说明 |
|---|---|---|
| F1.8 | 语音输入 | P3（文档自评"价值低，不优先"） |
| F2.4 | 质量分与告警 | P1：Q<0.8 强制附质量提示，代码零命中 |
| F3.5 | 结构-价格归因 | P2：接口是 **501**（`app/routers/analysis.py`）—— 不是"跑不通"，是**没接线**，不留假接口 |
| F3.6 / F3.7 / F3.8 | 多指标组合 / 异常推送 / 口径漂移哨兵 | P1 / P3 / P2 |
| F4.2 / F4.3 | 自动周报月报 / 订阅推送 | P1 / P2（`app/` 中"周报/月报/订阅/推送"零命中） |
| F5.5 / F5.8 | 血缘与影响分析 / 指标热度与缺口 | P1（无血缘实现，无 `hit_count` 类统计） |
| F5.6 | 指标**灰度** | P1（"争议下线"已做，灰度未做） |
| F6.2 | **多院区围栏** | 显式未接线 —— 见下一节（代码与配置就绪，差数据源那一列） |
| F6.4 | 患者级临时授权**审批流** | P2：默认拒绝与"请走数据申请流程"提示已做，审批流本身未做 |
| F7.3 / F7.4 / F7.5 | 未命中排行 / 采纳率埋点 / 转人工工单 | P1 / 近似 / P1：`/api/ops/stats` 只有 `miss_rate` 比率与 `answer_rate` 近似，**没有排行，也没埋"是否重问/追问"事件** |
| F8.1 | 多数据源连接器 | 只有 `oracle` / `sqlite`，MySQL/PG 等未实现（`app/core/sources.py` 直接报错，不静默跳过） |
| F8.2 | 床位域时效 | `app/config/source_domains.yaml` 标"待补"（日期列未探明） |
| F8.3 / F8.4 | 模型灰度与回退 / 缓存与限流 | 只有模型配置与连通性测试；"模型更新须过 Golden CI"与"预聚合优先、明细限流"均未做 |

### C. 界面侧（docs/13 §9）

| 阶段 | 未做 |
|---|---|
| P1 客户端 | 骨架屏、导出**对话框**、口径抽屉的"影响面预览" |
| P2 后台 | 口径规则变更的"影响面提示"（UI-8） |
| P5 平台页 | 血缘、字典映射、Golden CI 展示（**整页未做**） |

> **反向的一处不一致也已修**：docs/13 §9 的 P3 行原写"角色/主体授权、审计链页、导出追溯"未做，
> 这三项实际已在 v0.19 落地（见上表 F6 行与「授权台账」「角色与审计」「导出追溯」三个面板），
> 现已在该文档中改标为完成 —— 文档落后于实现，和实现落后于承诺一样有害。

### 多院区围栏（F6.2）的真实状态

这是本节最容易被误读的一条：**不是"没写"，而是"写了，但数据源没有那一列"**。

| 环节 | 状态 |
|---|---|
| 授权解析 | ✅ `permission.resolve_user`：不写 = 不启用围栏 / `[]` = 拒绝 / `["*"]` = 全院区 |
| 授权台账 | ✅ `authz_store` 的 `campus_scope` 字段 + 变更留痕；`authz_sync.yaml` 已含院区的列名映射 |
| 预检 | ✅ `detect_out_of_scope_campus`：点名了范围外院区**直接拒绝**（不靠 SQL 收口，否则"问东院"会返回西院的数——不报错，但答案是错的） |
| SQL 围栏 | ✅ `sql_builder` 注入 `campus_code IN (...)`；**列不存在时在生成 SQL 之前**报可诊断错误 |
| 启动告警 | ✅ `security_gate.rls_warnings`：有账号被限定院区却没这一列时，启动日志直接说明 |
| **数据源的院区列** | ❌ **缺**：HIS 镜像 `PAT_VISIT` 未抽取该字段；ADS 宽表 `build_ads.py` 没建列。两份物理映射都**显式写 `null`**（而不是不写） |

即：一旦有账号被限定具体院区，问数会在生成 SQL 前被**明确拒绝并说明原因**，
而不是静默返回错院区的数字。接线的唯一前置动作是**给数据源补院区列**，
在那之前请保持 `campus_scope` 为 `["*"]` 或不写。详见 [docs/12 §5.3](docs/12-身份认证与权限落地设计.md)。

## 评估集（Golden Set）

```
eval/
├── golden_set_template.csv      # 标注表模板（Excel/飞书多维表可直接打开）
├── golden_set_sample.jsonl      # 31 条已标注样例（覆盖常规/歧义/越权/超范围/问口径/时间变体/排名）
└── validate_golden.py           # 校验脚本：必填、枚举、DSL schema、SQL 规范、覆盖分布、双人一致性
```

已用 `--registry` 跑通，结果 `0 error`。规范见 [docs/09](docs/09-Golden集标注规范.md)。

## 可运行示例

```
semantic-layer/
├── metric_registry.yaml           # 自研轻量语义层（MVP 推荐，含 20+ 个医院核心指标）
├── ambiguity_rules.yaml           # 歧义词表（触发反问的规则 + apply 口径写回映射，支持热更新）
├── dbt/semantic_models.yml        # dbt MetricFlow 语义模型示例
├── dbt/metrics.yml                # dbt MetricFlow 指标定义示例
└── cube/hospital_metrics.yml      # Cube.dev 语义层示例 + 预聚合配置
```

自研 YAML 的 schema 已刻意对齐 MetricFlow（measure / dimension / entity / metric / filter），后期迁移成本极低。唯一无法原生映射的是医疗专有的 `date_basis` / `dept_basis` / `dedup` 三概念——这正是 POC-A 要验证的关键点。

## 一句话结论

- **没有单一开源产品能 1:1 替代 Palantir Foundry**，可行路径是「按能力域组合平替」。
- 组合基线：`DataX/SeaTunnel + 数仓 + dbt + MetricFlow/Cube + SuperSonic(或自研问数引擎) + Superset + DataHub + DolphinScheduler + Ranger`。
- 医疗行业强烈建议引入 **OHDSI / OMOP CDM** 作为临床数据标准化底座，避免自研映射。
- **P0 数据盘点与口径梳理占 50~60% 工作量**，被低估是此类项目失败的第一原因。

## 建议阅读顺序

1. 管理层：01 → 08 → 06
2. 数据团队：01 → 02 → 03
3. 算法/工程团队：01 → 03 → 04 → 07
4. 安全合规：05

## 更新记录

| 日期 | 版本 | 说明 |
|---|---|---|
| 2026-10-14 | v0.1 | 初版框架建立 |
| 2026-10-14 | v0.2 | 打通端到端管线 `engine/pipeline.py`（13 项自测全通过）；反问策略改为 N1~N5 + R1~R3；Golden 集修正为 31 条（见 docs/09 §9.1） |
| 2026-10-14 | v0.3 | 接入真实模型跑通 POC-B（`eval/poc_b_real_llm.py`）：deepseek-chat 31 条实测 意图 100% / 可执行 100% / 正确率 83.9% / 越权漏放 0；据实测修 10 处（模型方言翻译、越权预检、意图规则预判等），详见 docs/07 §B.6 |
| 2026-10-14 | v0.4 | 消融对比 `eval/run_ablation.py`：G0 裸跑 SQL 54.8%（科室归属字段仅命中 25.8%、越权 4 条全未拦）/ G1 61.3% / G3 96.8%，G3−G0 = +42pt 过决策门；Golden 覆盖度体检 `eval/check_coverage.py`（31→300 共 42 项缺口，只算缺口不编题），详见 docs/07 §B.6.2 与 docs/09 §11.1 |
| 2026-10-14 | v0.5 | 失败诊断 `eval/diag_failures.py`（多次运行 + 字段级归因，区分稳定失败与抖动）：据诊断补 5 条口语约定（怎么样→环比、医保上位概念、时间锚点、排名维度、急诊=就诊类型），83.9% → 96.8%；修评估器两处误判（正确拒绝被判失败、意图准确率扣正确拒绝）。⚠️ 规则在 31 条上归纳，存在过拟合，须在 300 条真实集复验，详见 docs/07 §B.6.3 |
| 2026-10-16 | v0.6 | 接入 HIS 全量同步镜像做只读盘点（`scripts/probe_his.py`）：Oracle 11g / 14 个业务 schema；实测住院域可用（`PAT_VISIT` 76.5 万、`INP_BILL_DETAIL` 5550 万，更新至 T-1），门诊就诊主表近空、`DRG_DETAIL` 止于 2019-06、`REPORT` 层止于 2024-10；源库 `num_rows` 不可信（估算与实测差 8 倍）；新增 [docs/11 产品功能设计](docs/11-产品功能设计.md)（F1~F8 模块 + 版本规划） |
| 2026-10-16 | v0.7 | **B 路线打通**：新增 Oracle 11g 方言（`TO_CHAR`/`TO_DATE`/ROWNUM，修 ORA-00911 与 ORA-01861）与物理映射 `semantic-layer/physical_map_his.yaml`，住院域 5 个指标在 HIS 镜像上真实出数（2023 全年：出院 59,653 / 入院 59,376 / 占用床日 387,362 / 死亡 39 / 平均住院日 6.49 天）。**A 路线打通**：`scripts/build_ads.py` 建 ADS 宽表（14.4 万行，1.6s），切换 `ASK_DS=ads` 后结果逐位一致，语义层零改动。新增 `scripts/reconcile_his.py` 口径对账：40/43 个统计科室对上，差异定位到 3 个统计科室 |
| 2026-10-16 | v0.8 | 新增 [docs/12 身份认证与权限落地设计](docs/12-身份认证与权限落地设计.md)：身份授信（demo/gateway/sso 三态迁移）、可治理授权、`campus_scope` 接线、审计不可篡改（哈希链 + 独立存储）、五组验收用例。**盘查中发现两处高危**：① `guest` 的 `dept_scope` 被 `or ["*"]` 提升为全院通配符（越权预检 / 科室检查 / RLS 三处同时失效，未授权访客可出全院 `internal` 指标）；② 管理端与审计/快照/导出接口零鉴权，且可改写数据源与口径规则 |
| 2026-10-16 | v0.9 | **修复 S2「空范围被兜底成全院通配符」**。根因是空列表语义歧义：`[]` 的正确语义是「无授权」，此前被当成「未指定」。改 6 处并全部保持 fail-closed——router / `engine_bridge` 去掉 `or ["*"]`、`pipeline` 空范围直接 `denied`、`sql_builder` 空范围 `raise DSLError`（纵深防御）、`filter_metrics` 空则返回空集、各 API 的 `user_id` 默认值 `u001`（院长）→ `guest`。回归：`engine/pipeline.py --selftest` 新增 3 项（对照 / 空范围拒绝 / SQL 层兜底）**16 项全过**，`sql_builder` 与 `attribution` 自测均通过；真实 API 实测 `u999` → `denied`、`rows=None`、`sql=None`，院长视角 2023 全年出院 59,653 不变。**顺带发现 S9**：`internal` 在执行层（`sql_builder` 放行）与目录层（`can_see_metric` 要求 `sensitive_metrics`）语义不一致，导致 `科室主任` / `运营管理部` / `医保办` 的驾驶舱为 0 个指标，已记入 docs/12 §1.2 待决策 |
| 2026-10-16 | v0.10 | **修复 S9「执行层与目录层对 `internal` 的语义不一致」**。授权名单改为**只读语义层** `permission.restricted_metrics_roles`（唯一事实来源），`roles.yaml` 的 `sensitive_metrics` 降级为回退——执行层（`sql_builder.py:359`）与目录层（`permission.can_see_metric`）从此必然一致。顺带揪出**第三种 guest**：`resolve_user` 的「未知用户」分支漏了 `visible_metrics`，与 `roles.yaml` 的 `guest` 语义不同（未授权用户反而看得到指标目录），已合并为同一条组装路径。[docs/03 §2.0](docs/03-指标字典与语义层建模.md) 补上 `sensitivity` 三级语义与可见性规则表。新增 `python -m app.core.permission --selftest`（含「全角色 × 全指标」一致性对拍），**5 项全过**；实测 `科室主任` 的驾驶舱与指标目录由 **0 → 20**（与执行层一致），`u999` 为 0 |
| 2026-10-16 | v0.11 | **多源真正可配**（原则：多源接入在**抽取层**、不在查询层）。新增源注册表 `app/config/sources.yaml`（6 个源：his / ads / lis / emr / insurance / hrp，声明 `kind` / `layer` / `enabled`，**密钥只写环境变量名**）与连接器工厂 `app/core/sources.py`（`OracleSource` 泛化 + `SqliteSource`，复用同一套只读守门）。`source_domains.yaml` 每个域加 `source:` 字段，时效探测**按源路由**（此前写死 `his.query`，接 LIS 必然静默失效）；时效取值扩展 `unconfigured`（未接入）并与 `disabled` 区分，`missing` 列出缺失的环境变量。新增 `meta_db` 轻量迁移（老库补 `source` 列）与 `GET /api/admin/sources/registry`。新增 `python -m app.core.sources --selftest`（9 项，含**密钥不落盘**与域→源映射完整性强制校验）；实测域指向 `ads` 后走 SQLite 连接器返回 `2026-07-20`、`source` 字段正确落库；`permission` / `pipeline` 自测无回退。设计见 docs/11 §5.6.1 |
| 2026-10-16 | v0.12 | 新增 [docs/13 界面产品化设计](docs/13-界面产品化设计.md)：把 F1~F8 的接口能力落到**三个界面**（问数客户端 / 语义层后台 / 审计与授权台）上。盘点出 8 类界面缺口（U1~U8）与 8 项后端契约缺口（C1~C9），其中最关键的两条：**C1 反问只有纯文本、没有结构化选项**（`pipeline._render_clarify` 的入参里其实已有 `id/ask/options/default`，只是没暴露给前端），**C9 `resolved` 只跳过重复反问、不作用于 DSL**（`ambiguity_rules.yaml` 的 `aliases_map`/`conditional_default`/`remember_user_choice` 三组字段在 `engine/` 与 `app/` 中零读取方）。另记：`restated` 四态之一**无产出路径**（`caliber.py` 定义了它但 `_judge_maturity` 从不返回）；`/api/analysis/*` 五个接口在 `app/web/` 中出现次数为 0；**状态全集实为 6 个**（`answered/meta/need_clarify/denied/out_of_scope/error`），11 §3 声明的 `no_data` 在实现中不存在——空结果需界面按 `answered && rows 为空` 判定 |
| 2026-10-16 | v0.15 | **落地 docs/13 的 P4：F3 分析能力界面**（下钻 / 排名 / 趋势 / 对比，归因不在 MVP）。① 结果卡上长出四个分析动作，**全部继承上一次问数的有效 DSL**（口径写回之后的那份），只改分组维度 / 时间粒度 / 排序 —— 分析不会悄悄改口径，卡片上的口径条与上一张一致。② 图表零依赖：趋势自绘 inline SVG 折线、排名/下钻用 CSS 柱状条（内网不能引公网 CDN 的图表库）。③ **新增 `GET /api/semantic/dimensions`**：可用维度 = 语义层声明 ∧ 权限允许 ∧ **当前数据源确实物化了**；判断不靠猜，用 `engine_bridge.probe_dimension()` 拿真 SQL 生成器造查询再让数据库 `SELECT * FROM (…) WHERE 1=0` 做元数据校验。实测发现 **ADS 宽表只物化了 `dept / stat_dept`**，`campus / pay_type / discharge_type / doctor` 都没列（`scripts/build_ads.py` 里就没有）→ 以前点这些维度只会得到 `no such column`；现在界面会把不可用维度连原因一起标出来（受限的「医师」还会回引擎原话"涉及个人绩效"）。④ 快照/回显改用**有效 DSL**（`dsl_raw` 保留模型原始输出）：否则用户选完口径再下钻，快照里是写回前的 DSL，下钻会悄悄退回默认口径。⑤ 修两处"假门禁"：归因接口的参数与引擎签名完全不一致（从来跑不通，一直表现成 500）→ 改为 **501 + 明确说明未接线**；`post_check` 里"同比变动超 50%"在找 `*_yoy / *_mom` 列，而 SQL 生成器从不产出这种列 → 换成"对比期只覆盖 1 个年份就提示（别把当期读成同比 -100%）"。⑥ 回归：`pipeline --selftest` **31/31**、`node scripts/check_ui_states.js` **42/42**（含"未物化维度必被标注""对比期无数据必提示""排名 SQL 真的带 TopN"）、golden `mock_oracle` 仍 **100%** |
| 2026-10-16 | v0.14 | **修 C9：用户选的口径必须真的写回 DSL**。① 语义层新增 `apply` 口径写回契约（`ambiguity_rules.yaml`）：`field`+`values`（单选字段）/ `set`（一次改多处）/ `alias_to_default`（院方认可的同义说法 = 本院默认口径）/ `unresolvable_values`（做不到的选项 + 原因），并删掉从未被代码读取的 `aliases_map`（内容并入 `apply.values`）与 `conditional_default`（与 registry 的 `*.default` 重复）。② 管线新增**步骤 4.5 口径写回**（`apply_resolved()`）：**必须早于权限门禁**——口径可能换掉指标（AMB-001），权限要按最终指标判定；写回后**再跑一次 `validate_dsl`**，与指标不兼容（如给门诊指标选「出院日期」）则**整批回退**并要求重新确认；**未声明映射的选项一律拒绝出数并说明原因**（`need_clarify` + SQL 为空），不许拿默认口径顶替。③ 14 条规则的每个选项都被显式分类并写回（`time.date_basis` 3 条口径 / `options.dept_basis` 5 个说法 / `set` 排序方向+TopN / `last_n_months` 窗口 / `time.compare` 同比环比 / `options.dedup` 人次人数…），做不到的（国家考核口径药占比、仅主手术、含出院后死亡、财务月、编制床位、15/7日再入院、全院收入、挂号人次、同比+环比同时要）全部写明原因。④ 前端：反问卡逐项标注「暂不能自动应用」（`options_detail`），提交只发 `resolved`（不再往问句里拼口径词），口径做不到时卡片展示原因而不是换个口径出数。⑤ 自测：管线 **29 项全过**（新增 8 项，含**映射表静态自检**：路径合法 / 取值在枚举内 / 指标已注册 / **每个选项都被显式分类**）；`node scripts/check_ui_states.js` **28/28**（新增"选了最低前10 → SQL 里出现 ASC"与"选了做不到的口径 → SQL 为空 + 说明原因"）；`run_golden.py --engine mock_oracle --registry ...` 仍 **100%**。⑥ 顺带：多指标静默丢弃改为显式警告（SQL 生成器只取 `metrics[0]`） |
| 2026-10-16 | v0.17 | **落地 docs/13 的 P2：后台资产页（F5.1/F5.2/F5.6/F5.7）**。① 管理端导航按 §4.1 重排为「资产 / 治理 / 平台 / 运营」四组，新增**管理端身份条与门禁**（非信息科不渲染后台骨架，并写明"前端门禁不是安全边界，真边界在网关"）。② **指标目录与编辑器**：字段逐一对齐 `MetricIn`；`measure` / 可用维度 / 默认口径做成**只读区**（界面改了就会让指标查不出数，所以不给入口）；口径多选取自 measure 的 `supported`（与 F2.2 同一来源）；**责任人必填**（后端也 400）；敏感级旁边写清三级后果。③ **版本留痕页**：谁/何时/改了什么/说明 —— 明确写出「审批流尚未实现，不假装有审批按钮」。④ **争议下线**：界面强制填原因（≥4 字）—— 后端 `reason` 默认空串，不拦就会静默写成"下线"。⑤ **口径对账页**：如实展示 `pending_config`（缺的是信息科对 `ITEM_CODE ↔ 指标码` 的人工确认）+ 基准层停更状态，**不画假对比表**；并给出"对账怎么配"的三步。⑥ **只读守门体检**接进「运维」页：显示真发一条 `DELETE` 看它被不被拦的结果（不是"声明只读"）。⑦ **修两个"错得无声"的写接口坑**（C14）：`upsert_metric` 原是**整条替换**，而接口字段集不含 `measure`/`dimensions` → 界面上改个"名称"就会把指标与度量的绑定删掉、变成查不出数的僵尸条目；`MetricIn` 又给 `type/sensitivity/status` 设了默认值 → 只发 `{code,name,owner}` 会把 `restricted` 静默降成 `public`、把下线指标重新上线。现改为**合并语义**（`None`=保留原值，`[]`/`""`=显式清空），默认值只在新建时补，并返回 `changed[]`。⑧ 新增 `scripts/check_admin_states.js`（**24/24**，自带"语义层路径不含 `_admin_check` 就跳过写操作"的保险，实测真实语义层零改动）。回归：客户端 **73/73**、`pipeline`/`rule_llm`/`permission`/`sources` 自测全过 |
| 2026-10-16 | v0.16 | **客户端改为「以对话为核心」（docs/13 §3.1 三栏落地）+ 补齐多轮（F1.2/F1.9）**。① **会话地基**：此前 `meta_db` 8 张表里没有会话/消息，客户端只有"一问一卡"，多轮指代无从落地 —— 新增 `chat_session`（软删，会话也是数据访问记录）+ `chat_message`（只存**渲染载荷**，rows 截断 200 行，完整结果仍在快照里）与 `app/core/chat.py`（归属校验 + 载荷裁剪唯一实现）。② **`/api/chat`**：建/列/读/改名/软删 + 回放；**他人的会话一律 404**（403 等于承认"这个 ID 存在"）；`/api/ask` 不传 `session_id` 会自动建会话，并把这一轮写成 问-答 两条消息（故障/拒绝也落库，否则用户回看历史会以为"我没问过"）。③ **多轮上文**：`AskPipeline.ask(context=…)` 把上一轮**实际执行的有效 DSL** 注入提示词（附硬规则"只改被问到的部分"）并记 `context_in` 到 trace；**引擎不做强制字段合并**（C13 待办）。降级 NLU 侧补**有限的**指代消解：换粒度/换范围/加对比/指定或排除科室，**没有上文时「那心内科呢」仍然明确 out_of_scope，绝不猜**；顺带修「排除急诊」认不出科室（口语常省"科"字）。④ **界面三栏**：左栏会话历史 + 我的快照、中栏常驻问句框 + 对话流（轮次容器 id 唯一，多轮互不覆盖）、右栏口径常驻 + **可切换口径**（F2.2）+ 证据与反馈；空态即引导。⑤ **修三处"界面骗人"**：口径可切换项此前读 metric 的 `supported_*`（实际定义在 **measure** 上）**永远为空** → 新增 `registry.caliber_options()`，`/api/semantic/metrics/{code}` 一并返回；演示身份下拉此前会显示 u001 院长而实际授权是 guest → 下拉必含**当前生效**身份；快照/复现/下钻**补归属校验**（此前拿到 answer_id 就能读别人的快照），并修复现写死 `his.query` 导致 ADS 路线必然复现失败。⑥ 回归：`pipeline --selftest` **37/37**、`rule_llm --selftest` **9/9**、`permission`/`sources` 自测通过、`node scripts/check_ui_states.js` **69/69**（新增"第 2 轮追加不覆盖第 1 轮""追问沿用上一轮时间范围""他人在会话 404""切换身份清空会话"等 27 项；并把 Node 端 DOM 桩升级到能跑 boot/提问/DOM 分支） |
| 2026-10-16 | v0.19 | **落地 F6.2 授权台账 + 组织架构同步（docs/12 §4.2/§4.3/§4.4），并修掉一个"科室主任一条数都查不出来"的真 bug**。① 新增 `auth_grant`/`auth_grant_log` 两张表与 `app/core/authz_store.py`：授权从 `roles.yaml`（**代码配置，改一次要走发布**）搬进台账，"某人转岗/离职"这类每天都在发生的事才改得动 —— 配置改不动日常，日常就会绕过配置。`roles.yaml` 退为**角色定义模板**（能做什么），台账只管**谁担任、哪些科室、到什么时候**。三条硬约束：**依据必填**（无依据的授权答不出"这个权限谁给的"）、同一人同一时刻**只有一条**有效授权（两条并存 = 谁生效看查询顺序）、台账里的空范围按**无授权**处理且**不回落**角色默认值（否则把"同步漏填"变成"全院"）。② 变更走"撤旧 + 写新 + 留痕"（**不删历史行** —— 审计要的恰恰是"他曾经有什么"），留痕带哈希链，`actor`（谁改的）与 `grantee`（改了谁）分开记；生效期支持临时授权到期自动回收。③ 新增 `app/core/authz_sync.py` + `scripts/sync_authz.py` + `semantic-layer/authz_sync.yaml`：岗位→角色、科室名→科室码的**映射表**（台账锚点是科室码不是名字；更名/合并走 `dept_alias` 显式迁移）；默认 dry-run、**映射不到整体不导**（一半人导进去的状态没人算得清）、`--revoke-missing` 默认关（**一份不完整的导出会撤掉全院权限**）。④ `ASK_AUTHZ_FROM = auto/table/yaml` 与 `resolve_user()` 接线：台账命中则范围**只认台账**，管理端「授权台账」页显示档位与覆盖率（还有多少人靠 YAML 兜着）。⑤ ★ 修 bug：RLS 注入的列名**写死**为 `belong_dept_code`，而它在 HIS 上叫 `DEPT_DISCHARGE_FROM`、在 ADS 宽表上叫 `discharge_dept_code` —— 两条路线都不存在这一列，**任何科室级授权的用户一条数都问不出来**（全院范围的人无感，因为 RLS 对 `["*"]` 不注入），而单测只断言字符串里出现过 `belong_dept_code`，于是一路绿灯。改为从语义层 `permission.rls` + 物理映射解析真实列；ADS 补 `physical_map_ads.yaml`（belong/order/exec 三种科室口径的落回关系与 HIS 逐位一致）；数据源没物化某列（如院区）时**在生成 SQL 前**报可诊断错误，而不是丢一个 `no such column` 给运维。⑥ 启动自检新增 RLS/台账两条（院区围栏未接线、迁移进度）。⑦ 自测：`authz_store` **25 项**、`permission` 台账接线 **13 项**、`sql_builder` RLS 列解析 **4 项**；端到端 `check_auth_flow.py` **43 项**（新增 H13~H20：严格模式空台账→管理员也进不去（鸡生蛋）、科室级授权真能出数 **10533 行 < 全院 59653 行**、改台账数值立刻变、撤销即时生效、留痕链完整、同步接口拒绝 file 源（否则是任意文件读取口））。回归：全部自测通过，客户端 **73/73**、管理端 **49/49**（新增 5 项授权台账用例）、golden 仍 **100%**。**未完成**：院方组织架构导出接口/格式确认（§4.2）、与 SSO 平台联调（§3.5）、审计独立存储（§6.2）、导出防伪（§6.4） |
| 2026-10-16 | v0.18 | **落地 F6.1 身份授信（docs/12 §3）**：把 `sso` 从"gateway 的别名"变成真能用的模式。① 新增 `app/core/identity.py`：三种身份来源（demo / gateway / sso），CAS（JSON **与 XML** 两路解析，11g 时代的老平台只回 XML）与 OIDC（授权码换主体 + `state` 防跨站伪造回调）自校验票据；属性名映射走配置（`ASK_ATTR_USER` 等），**取不到工号一律登录失败，不许拿姓名兜底**。② 新增 `app/core/session.py` + `user_session` 表：Cookie **只放不透明令牌**（不含工号/角色/科室）、HttpOnly + SameSite=Lax；绝对 8h + 空闲 30min（管理端 15min）；**登出是服务端立即失效**（只清 Cookie 不算登出），`revoke_user` 可一键踢掉某人全部会话（转岗/离职）；★ 会话只证明"你是这个工号"，**授权每次按工号重新解析**，所以授权变更即时生效。③ **网关模式加固**（此前只认头、不认来源）：新增 `ASK_TRUSTED_PROXIES` 来源白名单 + 入口中间件**剥离**非可信来源的身份头 —— 此前任何能连上服务的人加一个 `X-Auth-User: u001` 就是院长。④ `/api/security` 新增 `login`（缺参数时明确列出缺哪一项，不生成必然失败的地址）/ `callback` / `logout` / `sessions`（在线会话清单，**不返回令牌**）/ `events`（身份事件：登录/登出/票据失败/伪造头被拒 → `auth_event` 表，与问数审计分开，避免污染"谁问了多少"）。⑤ 启动自检打印"三类系统照常跑、就是没人能登录"的配错（gateway 无白名单 / sso 缺参数 / Cookie 未开 Secure）。⑥ 客户端与后台：未登录时给出登录入口**并关掉提问框**（不让用户先打一整顿再吃 401）、回调失败原因显示一次后清地址栏、登出按钮；管理端那句"身份桥仍待接线"按实际模式改写。⑦ 自测：`identity` **38 项**、`session` **21 项**、`authz` **21 项**、`security_gate` **37 项**；新增 `scripts/check_auth_flow.py`（**24 项**，自带 mock 统一身份平台，起真服务走真 HTTP：登录跳转/票据校验/建会话/Cookie 属性/伪造头无效/登出即失效/事件留痕/A4 启动闸门）。回归：`pipeline`/`sql_builder`/`rule_llm`/`permission`/`sources` 自测全过，客户端 **73/73**、管理端 **44/44**、golden `mock_oracle` 仍 **100%**。**未完成**：与院方身份平台的联调（地址/属性/证书/测试账号，docs/12 §3.5）、授权表与组织架构同步（§4.2）、审计独立存储（§6.2） |
| 2026-10-17 | v0.21 | **落地本地账号兜底（docs/12 §3.6）**：院方统一身份平台短期接不上，但系统不能"没人能登录"。① **只补凭证、不建第二套权限**：新增 `local_user` / `local_pwd_history` 两张表与 `app/core/local_accounts.py`，口令用 stdlib `hashlib.scrypt`（参数随哈希存，零新依赖），**哈希永不出库**（清单接口显式列字段，不写 `SELECT *`）；权限仍 100% 由 `auth_grant` 台账解析 —— 所以 SSO 接上后切档，台账一行都不用改。② **不新增 `ASK_AUTH_MODE=local`**：新增 `ASK_AUTH_LOCAL=off/breakglass/primary`；`identity._resolve_session()` 与 SSO 共用同一条服务端会话链路，靠 `user_session.auth_source=local` 区分来源（分成两条链路写，早晚出现"SSO 会话 8 小时、本地会话永不过期"）。③ 安全纪律：连续失败 5 次锁 15 分钟且**锁定期内正确口令也拒绝**；管理员重置发**一次性初始口令**（只显示一次、不落日志）+ 撤销该人全部会话 + **首登强制改密**（服务端中间件对其它 `/api/` 接口一律 409，不靠界面自觉）；停用立刻踢会话；过期账号不许启用；口令策略长度优先（12 位，不强制复杂度）、禁止含工号、禁止复用最近 3 次。④ **自举**：账号表为空时没人能登录，`--create-admin` 是唯一入口（建号 + 写台账授权 + 打印一次性口令）。⑤ 启动自检 6 条（无可用账号 / 仍有初始口令 / breakglass 超上限 / 已过期 / 即将到期 / 创建人自己启用）。⑥ 前端：客户端未登录页按 `local_login.enabled` 渲染口令登录并强制改密；管理端新增「本地账号」页。回归：`local_accounts --selftest` **36 项**、`identity --selftest` 38 项、端到端 `check_auth_flow.py` 新增 **H30~H40**（含"错误口令与账号不存在同答""未改密 409""锁定后正确口令也拒"）。★ 验收里抓到并修掉一个真缺陷：登录限速把**成功**登录也计入且阈值 10 次/分钟/IP —— 全院共用一个出口 IP 时早高峰会被 429 挡住，已改为"只统计失败、30 次/分钟/IP" |
| 2026-10-18 | v0.22 | **首次初始化入口（账号表为空时从界面建管理员）+ 测试数据一键清理**。① **空系统的出口**：`--create-admin` 只在有命令行时可用，交付的是 exe / 容器时现场会卡在"账号表是空的、登录页也登不进去"。新增 `GET /api/security/bootstrap/status` 与 `POST /api/security/bootstrap/admin`：**仅当 `local_user` 为空时可调用**（进程内加锁串行，建成第一个人后入口永久关闭），口令策略、失败限速、身份事件留痕与普通登录同规；账号直接获得 `roles.yaml` 里带 `admin: true` 的角色（不写死角色名）并写入授权台账，建完即签发与 SSO **同一种**服务端会话。② 前端：客户端与管理端在 `needs_bootstrap` 时渲染「创建管理员账号」而不是登录框（一个空系统上没有可登录的账号，显示登录框等于让人去猜账号是什么）；`/api/security/session` 带上 `bootstrap` 状态，不靠前端猜。③ **测试数据一键清理**：新增 `app/core/maintenance.py` 与管理端「测试数据清理」页，按分组清 —— `qa`（审计库 + 老库底档 + 快照/反馈/回流/导出追溯/口径偏好）、`chat`（会话与消息）、`identity`（登录事件，在线会话保留以免把人踢下线）、`accounts`（本地账号 + 授权台账，**保留调用者自己**，否则一键就把自己锁在门外）。④ 审计不是"删几条"而是**整体复位**：先摘掉"只允许追加"的触发器再清空，链锚一并清空并立刻封一个空锚 —— 否则下次校验会报"锚里 727 条、库里 0 条"，那正是"删除尾部记录"的判定特征。⑤ 二次确认用手打短语（`我确认清理测试数据`）而不是勾选框：勾选框会被顺手点上，这一步不可撤销。⑥ 回归：新增 `python -m app.core.maintenance --selftest`（13 项，含审计复位后链仍自洽）与 `check_auth_flow.py` **H50~H58**（空系统初始化入口 / 建成即关闭 / 口令策略不走后门 / 管理权来自台账 / 清理必须确认 / 清完审计链自洽），端到端 **79 项全过**；同时修正 `.env.example` 里两个非法值（`ASK_AUTH_MODE=local`、`ASK_AUTH_LOCAL=on` —— 写成这样登录页都出不来） |
| 2026-10-18 | v0.23 | **口令下限改为 8 位 + 登录/改密/初始化口令框加「小眼睛」**。① 最小长度默认由 12 位降到 **8 位**（`ASK_LOCAL_PWD_MIN_LEN`），并在 `check_policy()` 里写死 **8 位硬下限**：配置只能往上调 —— 现场一次误配（写成 4）不该让全线口令一起变弱；`/api/security/auth/local/status` 与 `bootstrap` 状态里的 `pwd_min_len` 均走同一个 `pwd_min_len()`，界面提示与后端校验同源。② 客户端与管理端的**所有口令输入框**（登录 / 首次初始化 / 强制改密）右侧加「小眼睛」：只切换 `input.type`，值不动、不复制、不落任何地方（浏览器密码管理器照常工作），显示时按钮高亮、`title` 变为「隐藏口令」。③ 顺带修无障碍：小眼睛在 `<label>` 内会把按钮的 `aria-label` 并进控件名（读屏念成「口令 显示口令」），现给输入框显式 `aria-label`（「口令」/「再输一次」/「新口令」）。④ 回归：`local_accounts --selftest` 新增「配置调到 4 位 → 仍按 8 位校验」，端到端 **79/79**（H30b 断言 `pwd_min_len=8`、H51 改为「7 位拒绝」、H52 用**恰好 8 位**口令建管理员）；`node scripts/check_ui_states.js` 72/73（唯一失败仍是既有的归因用例环境依赖，与本次无关） |
| 2026-10-16 | v0.13 | **落地 docs/13 的 P0（安全前置）**。① **身份不再由前端自选决定授权**：新增 `GET /api/security/session`（`ASK_AUTH_MODE = demo/gateway/sso`），只有服务端声明 demo 模式时客户端才显示「演示身份」，`gateway/sso` 下前端不显示切换器、也不自报身份。② **C1 反问结构化**：`AskResult.clarify` + `pipeline.clarify_item()`，`/api/ask` 返回 `clarify:[{id,name,ambiguity,ask,options,default,remember}]`，卡片式反问（F1.3）才做得出来；管线自测新增 2 项（反问项结构化可渲染 / 非反问场景不残留），**18 项全过**。③ **客户端六态渲染**：`answered / meta / need_clarify / denied / out_of_scope / 空结果 / error` 各一张专属卡（拒绝卡含依据 + 下一步 + 留痕；空的区分「无数据行」与数字 0；故障卡与「没权限」「没数据」分开），删除 5 个越界分支与全部 `alert()`，导出改为「先看水印再下载」。④ 顺带修两处：`rule_llm` 补 `meta_question` 识别（否则没装模型的演示环境里 meta 卡永远出不来）、口径条的绝对时间区间按**闭区间**展示（`2023-01-01 ~ 2023-12-31`，原来显示的是半开区间的 end）。⑤ 新增 `scripts/check_ui_states.js`（Node 直跑客户端脚本 + 真实 payload，**21 项全过**） |
