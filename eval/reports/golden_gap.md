# Golden 集覆盖度体检与采集缺口

- 现有 31 条 / 目标 300 条　状态：**存在缺口**

## 1. 现状

| 维度 | 现状 |
|---|---|
| 场景分布 | {'常规': 29.0, '歧义': 32.3, '越权': 12.9, '排名对比': 16.1, '时间变体': 19.4, '问口径': 3.2, '超范围': 6.5} |
| 难度分布 | {'easy': 29.0, 'hard': 22.6, 'medium': 48.4} |
| 来源分布 | {'meeting': 38.7, 'interview': 25.8, 'synthetic': 6.5, 'report': 19.4, 'ticket': 9.7} |
| 时间表达 | {'last_month': 17, 'year_to_date': 1, '': 1, 'first_three_quarters': 1, 'same_period_last_year': 1, '2025-01-01': 2, 'last_6_months': 1, 'current_month': 3, 'last_quarter': 1, 'last_3_years': 1, '2025-07-01': 1, 'week_before_last': 1} |
| 多指标组合 | 3 条（要求 ≥20） |

## 2. 缺口清单（按此去院内采集）

| 类别 | 项目 | 现有 | 需要 | 还缺 |
|---|---|---|---|---|
| 规模 | 总条数 | 31 | 300 | **269** |
| 指标 | op_visit_cnt（atomic） | 4 | 8 | **4** |
| 指标 | ip_discharge_cnt（atomic） | 5 | 8 | **3** |
| 指标 | ip_admit_cnt（atomic） | 0 | 8 | **8** |
| 指标 | op_patient_cnt（atomic） | 0 | 8 | **8** |
| 指标 | ip_occupy_bed_days（atomic） | 0 | 8 | **8** |
| 指标 | op_revenue（atomic） | 3 | 8 | **5** |
| 指标 | ip_revenue（atomic） | 4 | 8 | **4** |
| 指标 | surgery_cnt（atomic） | 2 | 8 | **6** |
| 指标 | surgery_level4_cnt（atomic） | 1 | 8 | **7** |
| 指标 | drg_weight_sum（atomic） | 0 | 8 | **8** |
| 指标 | ip_los_avg（ratio） | 4 | 12 | **8** |
| 指标 | op_avg_fee（ratio） | 0 | 12 | **12** |
| 指标 | ip_avg_fee（ratio） | 2 | 12 | **10** |
| 指标 | drug_rate（ratio） | 2 | 12 | **10** |
| 指标 | material_rate（ratio） | 1 | 12 | **11** |
| 指标 | surgery_level4_rate（ratio） | 1 | 12 | **11** |
| 指标 | cmi（ratio） | 1 | 12 | **11** |
| 指标 | unplanned_readmit_31d_rate（ratio） | 1 | 12 | **11** |
| 指标 | inpatient_death_rate（ratio） | 1 | 12 | **11** |
| 指标组合 | 多指标查询条数 | 3 | 20 | **17** |
| 场景 | 歧义 | 10 | 60 | **50** |
| 场景 | 越权 | 4 | 15 | **11** |
| 场景 | 超范围 | 2 | 15 | **13** |
| 场景 | 问口径 | 1 | 15 | **14** |
| 场景 | 时间变体 | 6 | 45 | **39** |
| 场景 | 排名对比 | 5 | 30 | **25** |
| 时间表达 | last_quarter | 1 | 3 | **2** |
| 时间表达 | last_n_months | 0 | 3 | **3** |
| 时间表达 | year_to_date | 1 | 5 | **4** |
| 时间表达 | first_three_quarters | 1 | 5 | **4** |
| 时间表达 | same_period_last_year | 1 | 5 | **4** |
| 时间表达 | last_3_years | 1 | 3 | **2** |
| 时间表达 | week_before_last | 1 | 3 | **2** |
| 时间表达 | absolute | 0 | 3 | **3** |
| 难度 | easy | 9 | 120 | **111** |
| 难度 | medium | 15 | 120 | **105** |
| 难度 | hard | 7 | 60 | **53** |
| 来源 | meeting | 12 | 105 | **93** |
| 来源 | report | 6 | 60 | **54** |
| 来源 | interview | 8 | 45 | **37** |
| 来源 | ticket | 3 | 45 | **42** |

## 3. 采集指引

- **P0（55%）**：院周会/运营会/质控会纪要 35% + 月报季报图表反推 20%
- **P1（30%）**：科室主任访谈 ≥5 人各 20 条 15% + 历史取数工单改写 15%
- **P2（≤15%）**：标注者构造，必须是业务口吻，禁止技术口吻

⚠️ 缺口**不能靠脚本编造**：构造占比超过 15% 会让分布偏斜、评估失真（docs/09 §2.1）。本脚本只负责算缺口。
