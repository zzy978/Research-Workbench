---
name: personal-advice-research-calibration
description: 面向 web 源、deep_research 工作流下的开放性个人建议类问题（如“该怎么谈恋爱”），把“结构完整、引用齐全”与“研究质量合格”区分开：先澄清用户情境或显式标注未知条件，再按待答子问题检索并披露来源核验与检索局限，主动检索反例，区分事实/推断/建议并按证据强度收敛结论，最后显式交代未完成部分。
version: 0.1.0
status: candidate
source_modes:
- web
created_from_runs:
- run_c89d5a169d5843e2b268eea49ec54bb2
---

## 触发条件

1. 用户提出开放式个人建议问题（例如“该怎么 X”），且未提供自身情境、目标或约束
2. 报告已具备完整章节与引用，但需要判定是否达到研究质量合格线
3. 单次 run 中检索调用数明显少于预算上限而结论已覆盖多个子话题

## 输入要求

1. 用户原始问题原文
2. 已知的用户情境约束（若为空须显式记录为空）
3. source_mode 冻结值（web）
4. 每个待答子问题与其对应检索查询
5. 来源元数据：作者/机构、日期、方法、适用范围、是否转载

## 步骤

1. R1 对缺少用户情境、目标、约束的开放性个人建议问题，先做一轮澄清；若无法澄清或用户未回应，必须在报告中列出未知条件并给出条件式建议（如“若单身/若处于暗恋/若已有伴侣”），不得直接输出无分支的通用清单。 [trace_refs: contract:check_run_c89d5a169d5843e2b268eea49ec54bb2_research_quality, run:run_c89d5a169d5843e2b268eea49ec54bb2]
2. R2 每个检索步骤必须对应一个当时尚未回答的具体子问题，并记录查询与目标子问题的映射；连续无新增信息时停止该子问题检索并保留未解决问题，不得把检索次数或工具成功当作回答完成。 [trace_refs: tool_call:call_ea69c17418264554b8ef25378d62b907, tool_call:call_fed3ac1d437d4c33b13da190e6b65c69, tool_call:call_a39478da4db94277bfcc3e3df79dafc6, tool_call:call_ac7ab4231ba84d65a5240ccb7c540ac4]
3. R3 来源必须核验后再使用：记录作者/机构、日期、方法与适用范围，区分原始来源与转载/聚合内容，不以搜索排名判定可信度；无法核验的条目降级为弱证据并在报告中标注。 [trace_refs: evidence:ev_a5462aa163d7286c211704f9, evidence:ev_9d04726de1cdbbe04a53d726, evidence:ev_48998bf7194520cfb9173df0, contract:contract_run_c89d5a169d5843e2b268eea49ec54bb2_source_diversity]
4. R4 必须主动检索反例、风险与不利结果（如建议失效的条件、可能造成伤害的做法）；未找到反证时须说明“未检索到不等于不存在”，并披露检索局限。 [trace_refs: contract:check_run_c89d5a169d5843e2b268eea49ec54bb2_research_quality, evidence:ev_6d6e75d9ba3232e1c1e45ad8, evidence:ev_aadf486d93a3aef099cd6f64]
5. R5 报告须区分事实、推断与建议三类陈述，并让结论强度不超过证据质量与已知个人条件；来源不足时改用条件式表述降低结论力度，禁止伪造来源多样性。 [trace_refs: contract:contract_run_c89d5a169d5843e2b268eea49ec54bb2_claim_support, contract:contract_run_c89d5a169d5843e2b268eea49ec54bb2_source_diversity, evidence:ev_1fd4a27f8fa5b01dc346f440]
6. R6 交付时显式说明未完成部分与已知局限，不得以证据条数、引用存在或工具调用成功替代完整交付；delivery_completeness 通过不等于 research_quality 通过。 [trace_refs: contract:check_run_c89d5a169d5843e2b268eea49ec54bb2_delivery_completeness, contract:check_run_c89d5a169d5843e2b268eea49ec54bb2_research_quality, event:742]
7. R7 每条实质性主张就近引用当前 run 的稳定 evidence_id，引用须可回溯到 Evidence Ledger；不得跨 run 复用或凭记忆生成引用 ID。 [trace_refs: contract:contract_run_c89d5a169d5843e2b268eea49ec54bb2_citation_integrity, evidence:ev_2377574a85821a2f83a7b459, evidence:ev_9a8b7ef700e923f0b5cc4983]

## 允许工具

1. tavily_search

## 失败回退

1. 无法获得至少两个独立来源时，降级为“单一来源/低置信”报告并明确标注，禁止伪造多样性。用户拒绝或无法提供情境时，输出条件式分支回答，并在开头声明未知条件与结论适用范围。来源元数据缺失（无作者/日期）时，将该来源标为弱证据，仅用于辅证而不用作唯一依据。若 research_quality 任一必需项无法满足，输出部分交付并附明确未完成清单，而非完整结论。

## 反模式

1. 用结构整洁、引用齐全的通用建议清单代替对用户情境的澄清或未知条件声明（本次 research_quality 明确判定为失败项）。 [trace_refs: contract:check_run_c89d5a169d5843e2b268eea49ec54bb2_research_quality]
2. 把“20 条证据 + 71/71 主张有引用 + 4 次工具调用成功”当作研究质量合格，忽略 research_quality 失败。 [trace_refs: contract:contract_run_c89d5a169d5843e2b268eea49ec54bb2_min_evidence, contract:contract_run_c89d5a169d5843e2b268eea49ec54bb2_claim_support, contract:check_run_c89d5a169d5843e2b268eea49ec54bb2_research_quality]
3. 只检索支持性材料，不检索反例与风险，也不说明检索局限。 [trace_refs: contract:check_run_c89d5a169d5843e2b268eea49ec54bb2_research_quality]
4. 以问答社区、自媒体聚合页的搜索排名作为唯一可信依据，不区分转载与独立来源、不记录作者与日期。 [trace_refs: evidence:ev_48998bf7194520cfb9173df0, evidence:ev_9d04726de1cdbbe04a53d726, contract:contract_run_c89d5a169d5843e2b268eea49ec54bb2_source_diversity]
5. 把未完成/未验证部分略去不写，让读者误以为已完整交付。 [trace_refs: contract:check_run_c89d5a169d5843e2b268eea49ec54bb2_delivery_completeness, event:742]

## 停止与降级条件

1. 澄清轮次达到 machine_policy.clarify_rounds_max 且仍无用户情境 → 停止追问，转为条件式回答并在报告显式标注未知条件。
2. 连续检索无新增信息（同一子问题重复命中同一批 evidence_id）→ 停止检索该子问题并保留为未解决问题。
3. tavily_calls 或 tool_calls 达到预算上限但关键子问题仍未回答 → 终止检索并标注未完成部分，不得升级为完整交付。
4. 报告缺少“局限/未完成/条件式建议”任一必需段落 → 不得置为完成，须回补或标注未完成。

## 验证方式

1. 检查报告中是否存在显式的用户未知条件声明与条件式建议分支（对应 R1）。
2. 逐条抽样核对实质性主张附近是否有当前 run 的 evidence_id，且不出现跨 run 或虚构 ID（对应 R7）。
3. 检查来源条目是否带有作者/机构、日期、方法/适用范围或明确的核验失败标注（对应 R3）。
4. 检查是否包含反例/风险段落或“未检索到反证”的局限说明（对应 R4）。
5. 检查是否存在事实/推断/建议的区分标记或等价表达（对应 R5）。
6. 检查是否显式列出未完成部分；delivery_completeness 通过不得单独作为 research_quality 通过依据（对应 R6）。
7. 运行确定性契约检查：citation_integrity、claim_support、source_diversity 通过且 research_quality 为 passed 才可声明成功。

## 已知限制

1. 仅来自单次 terminal_status=failed 的 web/deep_research run（run_c89d5a169d5843e2b268eea49ec54bb2），delivery_assessment=not_independently_verified，未经修复后重跑验证。
2. 未覆盖非 web source_mode、非 deep_research 工作流、非中文语境，也未验证多轮澄清的实际效果。
3. 本技能不包含、也不验证任何恋爱领域知识结论；仅约束检索—校准—披露流程。
4. 预算与工具权限不得由本技能扩大，allowed_tools 严格限定为轨迹中实际出现的工具。
5. 研究质量的判定来自既有确定性/规则检查器的输出，其覆盖面有限，本技能不能替代人工复核。

## Machine Policy

```yaml
workflow_mode: deep_research
source_mode: web
clarify_rounds_max: 1
require_unknown_conditions_statement: true
require_conditional_advice_when_unclarified: true
require_source_verification_fields: true
require_counterevidence_search: true
require_limitations_section: true
require_fact_inference_advice_labeling: true
min_independent_sources: 2
min_rules: 3
max_plan_tasks: 8
max_replans: 2
max_task_retries: 2
max_tavily_calls: 20
max_tool_calls: 30
tool_timeout_seconds: 60
wall_time_seconds: 1800
success_requires: research_quality=passed 且 citation_integrity/claim_support/source_diversity
  通过；delivery_completeness 通过为必要非充分条件
```
