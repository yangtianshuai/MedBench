# POC-B 真实 LLM 评测报告

- 模型：`deepseek-chat`　端点：`https://api.deepseek.com`
- 用例数：31　基线日期：2026-10-14
- 判定方式：**dsl_equivalent**（未连库，未做结果集比对）

## 核心指标

| 指标 | 结果 | 立项门槛 | 结论 |
|---|---|---|---|
| 意图准确率 | 93.5% | ≥90% | 达标 |
| DSL 可执行率 | 100.0% | ≥95% | 达标 |
| 结果正确率 | 77.4% | ≥90% | 未达标 |
| 反问得当率 | 80.0% | ≥95% | 未达标 |
| 越权拒绝正确率 | 100.0% | 100%（一票否决） | 达标 |
| 延迟 P50 / P95 | 968ms / 1365ms | P95<5s | 达标 |

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
| G0025 | 全院上个月死亡率多少 | query_metric | out_of_scope | ❌ |
| G0026 | 本月各院区门诊人次对比 | compare | compare | ❌ |
| G0027 | 2025年三季度普外科出院人次 | query_metric | need_clarify | ✅ |
| G0028 | 全院医生手术量排名前20 | rank | need_clarify | ✅ |
| G0029 | 上上周急诊人次多少 | query_metric | query_metric | ✅ |
| G0030 | 上个月住院收入怎么样 | query_metric | query_metric | ❌ |
| G0031 | 上个月全院收入怎么样 | query_metric | out_of_scope | ❌ |

> ⚠️ 本轮未连真实数仓（判定为 DSL 等价），**结果正确率不等于口径正确率**；
> 连库后须以 `expected_sql` 的执行结果为准再复跑一次。
