# 语义层示例

本目录提供三种语义层实现的**可运行示例**，用于 POC-A 对比验证。

| 文件 | 对应方案 | 适用阶段 |
|---|---|---|
| `metric_registry.yaml` | **自研轻量语义层** | MVP 推荐（schema 已刻意对齐 MetricFlow，便于后期迁移） |
| `dbt/semantic_models.yml` + `dbt/metrics.yml` | dbt + MetricFlow | 成长期标准方案 |
| `cube/hospital_metrics.yml` | Cube.dev | 备选 |
| `ambiguity_rules.yaml` | 歧义词表（与方案无关，通用） | 全程 |
| `synonyms.yaml` | 召回用词表：同义词归一 + 主题域扩展 | 全程 |
| `examples.yaml` | Few-shot 示例库（G2 实验条件） | 有真实 LLM 后 |

## 设计原则

1. **时间口径是一等公民**：每个 measure 都声明 `date_basis.supported` 与 `expr_map`。
2. **科室口径是一等公民**：同理声明 `dept_basis`。
3. **人次/人数显式声明**：`dedup: visits | patients`。
4. **ratio 指标禁止跨时间 avg**：只能 `SUM(num)/SUM(den)`。
5. **alias 是问数召回的关键**：每个指标至少 3 个通俗别名。
6. **召回与口径分离**：`synonyms.yaml` 只解决"怎么找到指标"，`metric_registry.yaml` 只解决"指标怎么算"。
   新增指标不用改词表（主题域靠 `metrics[].domain` 自动归并）；只有出现新口语说法才改。

## 指标召回（engine/recall.py）

三路打分，优先级从高到低，分数可直接进 trace 解释"为什么这条进候选"：

| 路径 | 分数 | 解决的问题 |
|---|---|---|
| 整词命中 | 100 + 3×词长 | 名称/别名完整出现 |
| 碎片命中 | 45 × 覆盖率²（≥50%） | 别名被问题拆开："门诊多少人次" vs "门诊人次" |
| 主题域 | 20 | 口语泛化："收入" → 收入域下所有指标进候选 |

两条纪律：
- **空候选是有效信号**，不兜底。把无关指标塞给模型，会把"答不了"变成"答错"。
- 召回只做 narrowing，指标认定权仍在 LLM，口径否决权在语义层。

自测与度量：
```bash
python engine/recall.py --selftest        # 断词/泛化/同义词/排序回归
python eval/eval_recall.py --golden eval/golden_set_sample.jsonl \
    --registry semantic-layer/metric_registry.yaml --top-k 8
```

**分母怎么定（比分子更重要）**：`intent=out_of_scope` 的用例（如「床位使用率」这类
尚未建模的指标）**不进召回分母**——它本来就该答「暂不支持 + 指标申请」，其 `metrics`
是"用户想问的"而不是"系统应当召回的"。把它算进分母，召回率会永久卡在 96.7%，
还会把真正的问题（缺事实表）伪装成"召回算法不行"。修正后：**recall@8 = 100%（29/29）**。

自测与度量基线（2026-10）：
```
recall@1 86.2% / recall@3 93.1% / recall@8 100% / MRR 0.96
```

## 维度 JOIN（`resolve: join`）

维度默认 `resolve: fact`，即维度列直接长在事实表上，**不出 JOIN**（既有 golden SQL 逐字不变）。
切到 `resolve: join` 后：

```sql
FROM dwd_fact_inpatient_discharge f
LEFT JOIN dim_department d1 ON f.discharge_dept_code = d1.dept_code
SELECT d1.dept_name AS dept, ...
```

四条规则（由编译器强制）：

| 规则 | 违反时 |
|---|---|
| 只在 `resolve: join` 时 JOIN | `fact` 模式零 JOIN，SQL 与历史一致 |
| 只允许 `many_to_one` / `one_to_one` | 一对多会放大指标 → 直接报错 |
| 连接键按 `dept_basis` 分档选 | 当前口径没声明 → 报错，绝不拿别的口径凑 |
| JOIN 后所有列带别名（`f.` / `d1.`） | 两表同名列不限定会 ambiguous |

两个坑，已经踩过：

1. **字段名是 `join_on`，不是 `on`**：YAML 1.1 会把裸写的 `on:` 解析成布尔 `True`，
   `rel.get("on")` 读出来是 `None`，症状是"配置明明写了却没生效"。代码兼容 `True`/`"on"`，但新配置请用 `join_on`。
2. **切 join 前先确认物理映射里有这张维表**：编译器只校验关系声明，不校验库表是否存在。

回归方式（改 SQL 生成器前后各跑一次）：
```bash
python eval/sql_snapshot.py     # 既有 17 条 SQL 必须逐字一致
python engine/sql_builder.py    # JOIN 正向 + 三条反向拦截自测
```

## Few-shot 示例库（`examples.yaml`，默认关闭）

两类示例，职责不同：

| 类型 | 内容 | 注入条件 | 作用 |
|---|---|---|---|
| `structure` | 只演示 DSL 形状，code 用 `<指标code>` 占位 | 固定 2 条 | 约束格式，**零抄袭风险** |
| `caliber` | 真实 code + 真实口径 | 其指标已进入本轮候选 | 示范口径与时间锚点 |

三条纪律：

1. **结构示例不许出现真实指标 code**。模型会照抄示例里的指标，输出合法但答非所问。
2. **口径示例只在候选命中时注入**。同理，防止从"很官方"的示例里挑错指标。
3. **默认关闭**（`ASK_FEWSHOT=0`）。它是待验证的增益，先用 `run_ablation` 跑 G1 vs G2，
   有差值再打开；默认开启等于拿生产流量做实验。

```bash
python engine/fewshot.py --selftest                        # 合法性 / 防抄袭 / 选择策略
python engine/fewshot.py --audit-golden eval/golden_set_sample.jsonl  # 注入率审计
python engine/fewshot.py --show "各科室平均住院日排名前10"      # 看实际注入块
```

审计基线（29 条应出数用例）：平均注入 3.97 条、含口径示例 **100%**。
`G0016 床位使用率` 按口径排除（语义层明确不支持，不该拿到口径示例）。

## 自研 YAML → MetricFlow 迁移映射

| 自研字段 | MetricFlow 对应 |
|---|---|
| `measures[].code` | `semantic_models[].measures[].name` |
| `measures[].agg` | `measures[].agg` |
| `measures[].expr` | `measures[].expr` |
| `measures[].date_basis.expr_map` | `semantic_models[].dimensions`（多个 time 维度） |
| `measures[].dept_basis.expr_map` | 多个 categorical 维度 |
| `metrics[] (type: ratio)` | `metrics[] (type: ratio, numerator/denominator)` |
| `metrics[] (type: atomic)` | `metrics[] (type: simple, measure)` |
| `dimensions[]` | `entities[] + dimensions[]` |
| `relations[]`（事实→维表，带 join_on 分档） | `semantic_models[].entities[]`（primary / foreign entity） |

> 未映射：`date_basis` / `dept_basis` / `dedup` 这三个医疗专有概念在 MetricFlow 中没有原生支持，迁移时需通过**生成多个维度 + 查询时选择**的方式模拟，或在自研查询生成器中处理。这正是 POC-A 要验证的关键点。
