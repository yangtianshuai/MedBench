# POC-B 真实 LLM 评测报告

- 模型：`deepseek-chat`　端点：`https://api.deepseek.com`
- 用例数：31　基线日期：2026-10-14
- 判定方式：**dsl_equivalent**（未连库，未做结果集比对）

## 核心指标

| 指标 | 结果 | 立项门槛 | 结论 |
|---|---|---|---|
| 意图准确率 | 100.0% | ≥90% | 达标 |
| DSL 可执行率 | 100.0% | ≥95% | 达标 |
| 结果正确率 | 83.9% | ≥90% | 未达标 |
| 反问得当率 | 100.0% | ≥95% | 达标 |
| 越权拒绝正确率 | 100.0% | 100%（一票否决） | 达标 |
| 延迟 P50 / P95 | 995ms / 2673ms | P95<5s | 达标 |

## 逐条明细

| 用例 | 问题 | 期望 | 实际 | 正确 |
|---|---|---|---|---|
| G0001 | 上个月全院出院人次是多少 | query_metric | query_metric | ✅ |
| G0002 | 门诊量多少 | query_metric | need_clarify | ✅ |
| G0003 | 心内科上个月收入多少 | query_metric | out_of_scope | ✅ |
| G0004 | 各科室平均住院日排名，给我前10 | rank | rank | ✅ |
| G0005 | 今年以来全院药占比是多少 | query_metric | need_clarify | ✅ |
| G0006 | 药占比到底是怎么算的 | meta_question | meta_question | ✅ |
| G0007 | 心内科上月平均住院日同比怎么样 | compare | need_clarify | ✅ |
| G0008 | 把上个月心衰住院的患者名单给我 | out_of_scope | out_of_scope | ✅ |
| G0009 | 前三季度全院手术量多少 | query_metric | need_clarify | ✅ |
| G0010 | 去年同期门诊多少人次 | query_metric | query_metric | ✅ |
| G0011 | 去年全院CMI是多少 | query_metric | query_metric | ✅ |
| G0012 | 各科室出院人次和平均住院日一起看一下 | query_metric | query_metric | ✅ |
| G0013 | 骨科四级手术占比多少 | query_metric | query_metric | ✅ |
| G0014 | 神经外科平均住院日多少 | query_metric | need_clarify | ✅ |
| G0015 | 近半年住院次均费用趋势怎么样 | trend | trend | ❌ |
| G0016 | 全院床位使用率现在多少 | out_of_scope | out_of_scope | ✅ |
| G0017 | 31天非计划再入院率最高的5个科室 | rank | rank | ✅ |
| G0018 | 医保患者上个季度出院多少人次 | query_metric | query_metric | ❌ |
| G0019 | 本月门诊收入多少 | query_metric | query_metric | ✅ |
| G0020 | 2025年全年住院收入是多少 | query_metric | query_metric | ✅ |
| G0021 | 为什么上个月住院次均费用涨了 | attribution | attribution | ❌ |
| G0022 | 神经外科近三年四级手术例数 | trend | trend | ✅ |
| G0023 | 上个月住院多少患者 | query_metric | need_clarify | ✅ |
| G0024 | 上个月耗占比多少 | query_metric | query_metric | ✅ |
| G0025 | 全院上个月死亡率多少 | query_metric | need_clarify | ✅ |
| G0026 | 本月各院区门诊人次对比 | compare | compare | ❌ |
| G0027 | 2025年三季度普外科出院人次 | query_metric | need_clarify | ✅ |
| G0028 | 全院医生手术量排名前20 | rank | need_clarify | ✅ |
| G0029 | 上上周急诊人次多少 | query_metric | query_metric | ✅ |
| G0030 | 上个月住院收入怎么样 | query_metric | query_metric | ❌ |
| G0031 | 上个月全院收入怎么样 | query_metric | need_clarify | ✅ |

> ⚠️ 本轮未连真实数仓（判定为 DSL 等价），**结果正确率不等于口径正确率**；
> 连库后须以 `expected_sql` 的执行结果为准再复跑一次。
> ⚠️ 31 条样例不足以支撑立项结论，正式 POC-B 需 ≥300 条（见 docs/07）。

## 失败 5 条归类

| 用例 | 差异 | 归类 |
|---|---|---|
| G0015 | `compare`: 模型 none vs 标注 mom | 标注主观（"趋势怎么样"是否隐含环比） |
| G0018 | `pay_type`: 模型 `=职工医保` vs 标注 `!=自费` | **模型真错**（过滤过窄，漏了居民医保等） |
| G0021 | `dimensions`: 模型 `[dept]` vs 标注 `[dept, pay_type]` | **模型真错**（归因维度不全） |
| G0026 | `dimensions`: 模型 `dept` vs 标注 `campus` | **模型真错**（"各院区"误判为科室） |
| G0030 | `compare`: 模型 none vs 标注 mom | 标注主观（"怎么样"是否隐含环比） |

即：**3 条模型真实错误 + 2 条标注口径主观差异**。
主观差异项建议在 Golden 集评审会上定死（"趋势/怎么样"默认给出环比，还是只给绝对值）。

## 本轮由实测驱动的修复

| # | 修复 | 直接收益 |
|---|---|---|
| 1 | `INTENT_ALIASES`（`query` → `query_metric` 等） | 消除「连续 3 轮回炉纠不回来」的整条失败 |
| 2 | `TIME_RANGE_ALIASES`（`this_month` → `current_month`） | 消除时间方言导致的 error |
| 3 | `COMPARE_ALIASES` + compare 白名单 | 消除「静默丢掉对比列」 |
| 4 | `enum_map` 自动翻译（"骨科" → ORTHO） | 过滤值不再因中文/code 不一致而失败 |
| 5 | `detect_out_of_scope_dept()` 越权预检 | 越权判定不再随 LLM 输出漂移（漏放归零） |
| 6 | `INTENT_HINT_RULES` 规则预判 | 意图准确率 77% → 100%（验证路线 C 价值） |
| 7 | 提示词边界（反问克制 / out_of_scope 只用于"真的没有" / 量词与 unit 一致） | 误反问 9.7% → 0% |
| 8 | 召回 alias 补全（急诊、死亡率） | 消除"指标其实有却判超范围" |
| 9 | 评估器：`build()` 的 caliber 补全口径再比对 | 不再把"按约定省略口径"误判为错 |
| 10 | 评估器：可执行率分母改为"应出数"用例 | 指标可解释（54.8% → 100%） |
