# Golden 集（评估基准集）

| 文件 | 说明 |
|---|---|
| `golden_set_template.csv` | **标注表模板**，Excel / 飞书多维表 / WPS 可直接打开，照此填写 |
| `golden_set_sample.jsonl` | **31 条已标注样例**，覆盖常规/歧义/越权/超范围/问口径/时间变体/排名对比（v1.1 修正记录见 docs/09 §9.1） |
| `golden_attribution.jsonl` | **7 条归因用例**，验证结构-价格分解（含 4 条门禁反例） |
| `validate_golden.py` | **标注质量校验脚本**：必填、枚举、DSL schema、覆盖分布、双人一致性 |
| `run_golden.py` | **评估执行器**：含 MockOracle / MockPerturbed 自测引擎，验证评估器自身可信 |

标注前请先阅读 [../docs/09-Golden集标注规范.md](../docs/09-Golden集标注规范.md)。

---

## 快速开始

```bash
# 1. 复制模板开始标注
cp golden_set_template.csv golden_set_v1.csv

# 2. 校验标注质量（纯静态检查）
python validate_golden.py --input golden_set_v1.csv

# 2b. 校验歧义题的 clarify_resolution 能否被语义层写回（**建议必开**）
#     写错措辞的后果不是"猜错"，而是管线拒绝出数；语义层是按字面匹配的
python validate_golden.py --input golden_set_v1.csv \
    --ambiguity ../semantic-layer/ambiguity_rules.yaml

# 3. 校验 + 连库执行 expected_sql（推荐，能抓出写错的 SQL）
python validate_golden.py --input golden_set_v1.csv \
    --db "mysql+pymysql://user:pwd@host:9030/dwd"

# 4. 校验 + 指标编码与语义层注册表对齐
python validate_golden.py --input golden_set_v1.csv \
    --registry ../semantic-layer/metric_registry.yaml

# 5. 双人一致性检查
python validate_golden.py --input golden_set_a.csv --file-b golden_set_b.csv

# 6. 跑评估（先用自测引擎确认评估器本身没问题）
python run_golden.py --golden golden_set_sample.jsonl --engine mock_oracle \
    --registry ../semantic-layer/metric_registry.yaml      # 应为 100%

# 7. 归因分解用例自检
python ../engine/attribution.py --selftest
```

退出码：`0` = 通过（无 error）；`1` = 存在 error，需修复后才能入库。

---

## 字段速查

### 标注者必填

| 字段 | 取值 |
|---|---|
| `intent` | `query_metric` / `compare` / `rank` / `trend` / `attribution` / `meta_question` / `out_of_scope` / `need_clarify` |
| `requires_clarify` | `true` / `false` |
| `clarify_rule_id` | `AMB-001` ~ `AMB-014`（见 `../semantic-layer/ambiguity_rules.yaml`） |
| `role` | 院长 / 副院长 / 医务处 / 运营管理部 / 财务处 / 医保办 / 科室主任 / 医生 |
| `date_basis` | `admit` / `discharge` / `settle` / `visit` / `operation` |
| `dept_basis` | 门诊域 `order` / `exec` / `belong`；住院域 `admit` / `discharge` / `belong` |
| `dedup` | `visits` / `patients` |
| `permission_expect` | `allow` / `deny` / `partial` |
| `difficulty` | `easy` / `medium` / `hard` |
| `source` | `meeting` / `report` / `interview` / `ticket` / `synthetic` |
| `status` | `draft` / `reviewed` / `adjudicated` / `rejected` |

### 系统派生（不填）

`expected_dsl`、`expected_result_hash`、`expected_result_sample`

---

## 校验规则

### ERROR（阻断入库）

- 必填字段缺失或为空
- 枚举值非法
- `metric_code` 不在语义层注册表中（需 `--registry`）
- `requires_clarify=true` 但无 `clarify_rule_id`
- `expected_sql` 为空 / 含 `SELECT *` / ratio 使用 `AVG(`（需人工确认除外）
- 越权类题目（`permission_expect=deny`）却给出了 `expected_sql`
- 执行 `expected_sql` 报错（需 `--db`）

### WARN（需确认）

- 分布不达标：歧义 < 20%、越权 < 5%、超范围 < 5%、问口径 < 5%
- 某指标覆盖 < 8 条（ratio 指标 < 12 条）
- 时间表达类别覆盖不足
- `synthetic` 来源占比 > 15%
- `status` 仍为 `draft`

---

## 目录约定

```
eval/
├── golden_set_v1.csv          # 当前版本（纳入 Git）
├── golden_set_sample.jsonl    # 样例
├── validate_golden.py
└── reports/
    └── eval_<date>_<config>.json   # 每次评估的报告存档
```

> **Golden 集必须纳入版本管理**，评估报告需记录 Golden 集的 commit hash，保证结论可复现。
