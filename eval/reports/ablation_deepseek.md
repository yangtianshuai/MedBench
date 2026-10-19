# POC-B 消融对比报告

- 用例数：31　基线日期：2026-10-14
- 判定方式：G1/G3 = DSL 等价；**G0 = SQL 口径要素启发式检查（非结果比对）**

## 1. 组间对比

| 组 | 配置 | 结果正确率 | 意图准确率 | DSL 可执行率 | 反问得当率 | 越权拒绝 | 漏放 | P95 |
|---|---|---|---|---|---|---|---|---|
| G0 | 裸 Text-to-SQL（无语义层） | 54.8%* | — | — | — | 0% | **4** | 1504ms |
| G1 | 语义层 + 受限 DSL | 61.3% | 93.1% | 100.0% | 0.0% | 100.0% | 0 | 1447ms |
| G3 | G1 + 反问 + 规则预判（候选主方案） | 96.8% | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 1476ms |

> \* G0 的正确率是「SQL 口径要素全部命中」的比例，属于启发式上界：命中不等于数对，漏了几乎必然错。

## 2. G0 裸跑 SQL 的口径要素命中率

| 口径要素 | 命中率 | 漏掉的后果 |
|---|---|---|
| date_col | 90.3% | 日期口径错（出院日 vs 结算日），月度数对不上 |
| dept_col | 25.8% | 科室归属错（开单科室 vs 出院科室） |
| expr | 87.1% | 用错字段（如把人次写成人数） |
| ratio_nullif | 83.9% | 除零 → 报错或脏数 |
| table | 100.0% | 查错表，数字张冠李戴 |
| time_filter | 100.0% | 没有时间范围 → 全表扫描 / 口径不明 |

**全部要素命中：54.8%**　（越权/超范围题 4 条全部未被拦截（G0 无权限层））

## 3. 语义层路线的增益

| 对比 | 增益 |
|---|---|
| G3 − G1（反问机制 + 规则预判） | 35.5 pt |
| G3 − G0（语义层 vs 裸跑 SQL） | 42.0 pt* |

> 门槛：G3 vs G0 增益 ≥ +15 pt（docs/07 §B.5）

## G1 失败用例（12 条）

| 用例 | 期望 | 实际 |
|---|---|---|
| G0002 | query_metric | query_metric |
| G0005 | query_metric | query_metric |
| G0007 | compare | query_metric |
| G0009 | query_metric | query_metric |
| G0014 | query_metric | query_metric |
| G0021 | attribution | attribution |
| G0022 | trend | query_metric |
| G0023 | query_metric | query_metric |
| G0025 | query_metric | query_metric |
| G0027 | query_metric | query_metric |
| G0028 | rank | rank |
| G0031 | query_metric | query_metric |

## G3 失败用例（1 条）

| 用例 | 期望 | 实际 |
|---|---|---|
| G0021 | attribution | attribution |

> ⚠️ G0 只生成 SQL 文本，未连接数据库、未执行任何语句。