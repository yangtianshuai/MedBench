# 06 · 开源选型与 Palantir Foundry 平替

## 0. 结论先行

> **没有单一开源产品能 1:1 替代 Palantir Foundry。**
> Foundry 的核心壁垒是 **Ontology（本体）+ 端到端治理 + 应用构建 + write-back 行动闭环**的一体化体验，这是闭源商业壁垒。
>
> **可行路径**：按能力域组合平替。医院问数场景 80% 的价值集中在「语义层 + 查询 + 可视化 + 权限」，这部分完全可以组合开源实现。

---

## 1. Foundry 能力域拆解与平替

| Foundry 概念 | 能力说明 | 开源平替（推荐） | 备选 | 成熟度 |
|---|---|---|---|---|
| **Datasets / Pipeline** | 数据接入、转换、编排 | **dbt-core** + **Apache DolphinScheduler** | Dagster、Prefect、Airflow | ★★★★★ |
| **Data Connection / Sync** | 多源抽取同步 | **Apache SeaTunnel**、**DataX** | Flink CDC、Debezium | ★★★★★ |
| **Storage / Lakehouse** | 湖仓存储 | **Apache Iceberg** / **Paimon** / Delta Lake | Hudi | ★★★★☆ |
| **Transformation (Code Repositories)** | 建模转换 | **dbt-core** | SQL 脚本 + Git | ★★★★★ |
| **Data Lineage** | 血缘 | **DataHub** | OpenMetadata、Apache Atlas、Amundsen | ★★★★☆ |
| **Data Health / Expectations** | 数据质量 | **Great Expectations** | Soda Core | ★★★★☆ |
| **Ontology（本体）** | 对象 + 属性 + 关系 + 行动 | **（无成熟开源平替）** MetricFlow/Cube 语义层 + **Neo4j** 自建图本体 | Ontotext GraphDB、Apache Jena | ★★☆☆☆ |
| **Metric / Semantic Layer** | 指标语义 | **dbt + MetricFlow**、**Cube.dev** | Apache Kylin、自研 YAML | ★★★★☆ |
| **Contour** | 可视化探索 | **Apache Superset** | Metabase、DataEase | ★★★★★ |
| **Workshop / 应用构建** | 低代码数据应用 | **Appsmith**、**ToolJet** | NocoDB、Budibase | ★★★☆☆ |
| **Quiver / 图表分析** | 时序分析 | Superset / Grafana | — | ★★★★☆ |
| **AIP / 自然语言** | NL 查询与分析 | **SuperSonic**、**DB-GPT**、**WrenAI** | Vanna.ai、Chat2DB | ★★★☆☆ |
| **Permissions / Markings** | 细粒度权限 | **Apache Ranger** + **OpenFGA** + Keycloak | Sentry、Casbin | ★★★☆☆ |
| **Foundry Actions / write-back** | 行动闭环与回写 | **无开源平替，需自研**（工单系统 + 业务库接口） | — | ★☆☆☆☆ |
| **模型/ML** | ML 工作流 | MLflow、OpenMLDB | Kubeflow | ★★★★☆ |
| **联邦/虚拟化查询** | 跨源查询 | **Trino** | Dremio | ★★★★★ |

**关键判断**：本体（Ontology）与 write-back 是 Foundry 最难替代的两块。医院问数在 MVP~P3 阶段不强依赖它们，可自研轻量方案；若要做到 P4 行动闭环，必须自研。

---

## 2. 数据平台选型评分矩阵

评分 1~5，权重按医院问数场景设定。

| 组件类别 | 候选 | 功能 | 社区活跃 | 中文生态 | 运维成本 | 医院案例 | **加权总分** |
|---|---|---|---|---|---|---|---|
| **调度** | DolphinScheduler | 5 | 5 | 5 | 4 | 5 | **4.8** |
| | Airflow | 5 | 5 | 3 | 3 | 3 | 3.9 |
| | Dagster | 5 | 4 | 2 | 3 | 2 | 3.3 |
| **元数据/血缘** | **DataHub** | 5 | 5 | 3 | 3 | 3 | **4.0** |
| | OpenMetadata | 4 | 4 | 3 | 4 | 2 | 3.6 |
| | Apache Atlas | 4 | 3 | 3 | 2 | 3 | 3.0 |
| **语义层** | **MetricFlow(dbt)** | 5 | 5 | 3 | 4 | 3 | **4.2** |
| | **Cube.dev** | 5 | 4 | 3 | 3 | 2 | 3.7 |
| | 自研 YAML | 3 | — | 5 | 5 | 4 | **4.2**（MVP 场景） |
| | Apache Kylin | 4 | 3 | 5 | 2 | 4 | 3.5 |
| **问数引擎** | **SuperSonic** | 4 | 4 | 5 | 4 | 3 | **4.1** |
| | **DB-GPT** | 4 | 5 | 5 | 3 | 3 | 4.0 |
| | WrenAI | 4 | 4 | 3 | 4 | 2 | 3.5 |
| | Vanna.ai | 3 | 4 | 2 | 5 | 2 | 3.2 |
| **BI** | **Superset** | 5 | 5 | 4 | 3 | 4 | **4.4** |
| | Metabase | 4 | 5 | 4 | 5 | 3 | 4.2 |
| | DataEase | 4 | 3 | 5 | 5 | 3 | 4.0 |
| **MPP 引擎** | **Doris / StarRocks** | 5 | 5 | 5 | 4 | 4 | **4.7** |
| | ClickHouse | 5 | 5 | 4 | 3 | 3 | 4.1 |
| | Hive + Spark | 4 | 5 | 5 | 2 | 5 | 3.8 |
| **联邦查询** | **Trino** | 5 | 5 | 3 | 3 | 3 | 4.0 |
| **质量** | **Great Expectations** | 4 | 4 | 2 | 3 | 2 | 3.3 |
| **脱敏/权限** | **ShardingSphere** | 4 | 5 | 5 | 3 | 4 | **4.3** |
| | Apache Ranger | 4 | 4 | 3 | 2 | 3 | 3.3 |

---

## 3. 问数引擎专项对比

| 项目 | 语言 | 核心思路 | 语义层 | 中文支持 | 私有化 | 适配度 | 备注 |
|---|---|---|---|---|---|---|---|
| **SuperSonic**（腾讯） | Java | **Headless BI 语义层 + Chat BI**，先建语义模型再问答 | ✅ 内置（S2 semantic model） | ★★★★★ | ✅ | **★★★★★** | 架构最贴题：语义层优先，而非 SQL 优先 |
| **DB-GPT** | Python | 多智能体 + RAG + Text2SQL + AWEL 编排 | ⚠️ 弱 | ★★★★★ | ✅ | ★★★★☆ | 生态最全（含微调、知识库、多数据源），但语义层不是强项 |
| **WrenAI** | TS+Python | Text-to-SQL，自带 MDL 语义建模层 | ✅ MDL | ★★★☆☆ | ✅ | ★★★★☆ | 语义层设计优秀，中文需验证 |
| **Vanna.ai** | Python | RAG + 训练反馈（Q→SQL 对） | ❌ | ★★☆☆☆ | ✅ | ★★★☆☆ | 极易嵌入、反馈闭环好，但偏 SQL 直连路线 |
| **Chat2DB** | Java | DBA/开发向 SQL 助手 | ❌ | ★★★★☆ | ✅ | ★★☆☆☆ | 面向技术人员，不适合业务用户 |
| **SQLChat** | TS | 轻量对话查库 | ❌ | ★★★☆☆ | ✅ | ★★☆☆☆ | 同上 |
| **自研** | Python/Java | 完全贴合自有语义层 | ✅ 复用 | ★★★★★ | ✅ | ★★★★☆ | **推荐**：语义层已自建的前提下，自研引擎比改造开源更可控 |

### 3.1 选型建议

```
若语义层走「自研 YAML」（MVP 推荐）
   → 问数引擎自研（工程量约 3~4 人月），或选 SuperSonic 二次开发

若语义层走「dbt + MetricFlow」
   → 自研引擎调 MetricFlow API；或 SuperSonic（需做语义层桥接）

若语义层走「Cube」
   → Cube 自带 REST API + 有 LLM 插件，可直接接问数前端
```

**我的推荐**：**自研轻量语义层 + 自研问数引擎（前端/交互借鉴 SuperSonic）**。理由：
1. 医疗口径的歧义处理（`date_basis` / `dept_basis` / `dedup`）是高度定制的，开源框架都不支持，改造成本 ≥ 自研。
2. 自研引擎能把「反问澄清」做成一等公民，这是医院场景正确率的关键。
3. SuperSonic 的**前端交互与可视化值得直接借鉴/二次开发**，不必从零造。

---

## 4. 医疗行业专用开源（强烈建议纳入）

| 项目 | 用途 | 建议 |
|---|---|---|
| **OHDSI / OMOP CDM** | 临床数据标准化通用模型 | ✅ **强烈建议**。全球医疗数据标准化事实标准，避免自研术语映射 |
| **OHDSI ATLAS** | 队列定义、表征、发生率分析 | ✅ 科研问数场景直接用 |
| **Achilles** | 数据质量画像 | ✅ **先跑它**，2 周内摸清全院数据家底 |
| **OHDSI Vocabulary** | ICD-10→SNOMED、RxNorm、ATC 官方映射 | ✅ 复用，定期更新 |
| **i2b2** | 临床数据仓库 + 查询工具 | ➖ 老牌，国内案例少 |
| **OpenMRS** | 开源电子病历系统 | ➖ 不适用（医院已有 EMR） |
| **HAPI FHIR** | FHIR 标准实现 | ➖ 若需对接区域平台/互联互通测评则有用 |

### 4.1 为什么不自研临床数据模型

医院诊断编码、药品编码、操作编码的映射是**无底洞**。ICD-10 有 2 万+ 条目，医保"五统一"编码又是一套。OHDSI 官方 vocabulary 已提供标准化映射并持续维护，复用可省**至少 6~12 人月**。

### 4.2 国内外标准清单（映射目标）

| 标准 | 编号/版本 | 用途 |
|---|---|---|
| 疾病分类与代码 | GB/T 14396（ICD-10 国标版） | 诊断 |
| 手术操作分类与代码 | ICD-9-CM-3 | 手术操作 |
| 国家医保编码"五统一" | 医保发〔2019〕34 号 | 医保结算 |
| 卫生信息数据元目录 | WS 363 | 数据元 |
| 卫生信息数据元值域代码 | WS 364 | 值域 |
| 卫生信息元数据规范 | WS 365 | 元数据 |
| 电子病历基本数据集 | WS 445 | 病历结构化 |
| ATC / 解剖治疗化学分类 | WHO | 药品分类 |
| SNOMED CT | — | 临床术语（OHDSI 用） |
| 互联互通标准化成熟度测评 | 四级甲等常见要求 | 院内集成 |

---

## 5. 三套推荐组合

### 组合 A：轻量起步（MVP，8~12 周，3~5 人）

**目标**：验证价值，锁定 1 个场景 + 30 个指标。

```
DataX（抽取，HIS→ODS）
  → 现有数仓或 Doris 单机/3 节点
  → dbt 建模（或纯 SQL 脚本）
  → 自研 YAML 语义层（metric_registry.yaml）
  → 自研问数引擎（FastAPI + Qwen2.5-14B/vLLM）
  → Superset 出图 + 自研对话前端
  → 最小权限（角色 + 科室过滤）+ 审计表
```

**成本**：3~5 人 · 12 周 · 3~5 台服务器（含 1 张 GPU）

### 组合 B：标准方案（推荐，6~9 个月，8~12 人）

```
DataX / SeaTunnel 抽取
  → ODS/DWD/DWS/ADS（Doris 集群 3~5 节点）
  → dbt-core 建模 + MetricFlow 语义层（从自研 YAML 迁移）
  → 自研问数引擎（调 MetricFlow API）
  → Superset 可视化 + 自研对话前端
  → DataHub（血缘 + 目录）+ DolphinScheduler（调度）
  → Great Expectations（质量）
  → Apache Ranger + ShardingSphere + Keycloak（权限/脱敏/SSO）
  → OMOP CDM 并行建设（科研场景）
  → 审计 + 等保改造
```

### 组合 C：完整平台（12~18 个月，15+ 人）

在组合 B 基础上追加：

```
+ Neo4j 自建本体层（科室-人员-设备-病种关系图谱）
+ 异常检测 / 预测（Prophet、LightGBM、MLflow）
+ 行动闭环：预警 → 工单 → 科室反馈 → 效果回流（自研，对齐 Foundry Actions）
+ Appsmith 搭建运营数据应用
+ 多院区 / 医共体扩展
+ 密评改造（国密 SM2/SM3/SM4）
```

---

## 6. License 与信创注意事项

| 项目 | License | 商用友好 | 信创适配备注 |
|---|---|---|---|
| DolphinScheduler | Apache-2.0 | ✅ | 国产主导，麒麟/达梦适配好 |
| SeaTunnel / DataX | Apache-2.0 | ✅ | 国产 |
| Doris / StarRocks | Apache-2.0 | ✅ | 国产，信创目录常见 |
| dbt-core | Apache-2.0 | ✅ | 注意 dbt Cloud 是商业版，只用 core |
| MetricFlow | Apache-2.0 | ✅ | dbt 1.6+ 内置 |
| Cube.dev | Apache-2.0（核心） | ⚠️ 部分功能商业版 | 注意区分 OSS / Cloud |
| DataHub | Apache-2.0 | ✅ | 依赖较多（Kafka/ES/MySQL），运维偏重 |
| Apache Superset | Apache-2.0 | ✅ | 中文完善 |
| Great Expectations | Apache-2.0 | ✅ | 新版需注册（可选） |
| Apache Ranger | Apache-2.0 | ✅ | 主要适配 Hadoop 生态 |
| ShardingSphere | Apache-2.0 | ✅ | 国产 |
| Trino | Apache-2.0 | ✅ | — |
| Milvus | Apache-2.0 | ✅ | 国产 |
| **Neo4j** | **GPL-3.0 / 商业双许可** | ⚠️ **社区版功能受限** | 商用需评估，或用 NebulaGraph（Apache-2.0，国产） |
| Metabase | AGPL-3.0 / 商业 | ⚠️ | AGPL 有传染性，闭源产品慎用 |
| Appsmith | Apache-2.0（核心） | ⚠️ 企业版商业 | — |
| SuperSonic | Apache-2.0 | ✅ | 国产 |
| DB-GPT | Apache-2.0 | ✅ | 国产 |
| Qwen / DeepSeek / GLM | 开源许可（各自模型许可） | ✅ 多数可商用 | 需逐个确认模型 License 的商用条款与月活限制 |

> ⚠️ **License 红线**：Metabase（AGPL）、Neo4j（社区版限制）在闭源商业产品中需谨慎评估。建议优先 Apache-2.0 项目。图数据库优先 **NebulaGraph**（Apache-2.0，国产）。

---

## 7. 选型决策 checklist

- [ ] 所有组件可私有化部署，无强制外网依赖
- [ ] License 允许商用（优先 Apache-2.0）
- [ ] 有国内落地案例 / 中文文档 / 中文社区
- [ ] 团队具备运维能力（DataHub 依赖多，评估人力）
- [ ] 与院内现有技术栈（多为 Oracle/SQL Server/达梦）兼容
- [ ] 支持信创数据库与操作系统（招标常见硬性要求）
- [ ] 语义层 schema 设计向后兼容（便于从自研迁移到 MetricFlow）
- [ ] 已通过 POC 验证（见 [07](07-POC验证方案.md)）
