# 研究工作台验证记录

验证日期：2026-09-22。实现分支：`codex/research-workbench`。所有自动化及真实模型测试均使用隔离数据库，未修改原项目的研究历史或未跟踪 Skill。

## 确定性验证

最终可运行回归：**370 passed，3 deselected，79.03 秒**。另有一个依赖 Neo4j 的测试文件因收集阶段需要服务连接而未纳入运行。完整日志保存在 `.local-run/workbench/release-regression.log`。

排除项已单独核对：

- `test_private_search_regressions.py`：导入时连接本机未提供的 Neo4j。
- `test_search_graph_boundary.py` 的 `local_search_tool-LocalSearchTool` 参数：同样依赖 Neo4j。
- `test_delivery_files_and_configuration_are_complete`、`test_acceptance_matrix_contains_every_design_must_id_once`：原仓库已经缺少它们引用的验收文档；在原仓库重跑同样失败。

因此本记录不宣称私域真实服务回归已通过，也不把整个仓库描述为零失败。未删除或弱化这些已有测试。

覆盖的新增行为包括：

- 未确认、旧版本和旧 Run 不能进行深调；初稿独立发现额度、重试和并发预占；等待确认不会被自动恢复为调查。
- 两种工作流都经过同一范围门禁；重复批准和补查不创建重复执行、不暂停较新的执行。
- 初次创建、批准关联、旧大纲提升为研究时的故障注入；重放请求修复关联，无法修复时拒绝执行。
- 对象和字段的增量失效、跨修订补查失效记录、旧证据复用、零值与 `false`、跨研究和失效引用拒绝。
- 来源域名在请求、结果回收和结论存储三处校验；正文要求读取真实台账元数据，不能由模型改写访问级别。
- 修改后的预算同步至运行和恢复检查点，初稿检索耗时与多 Run 累计用量计入项目预算。
- 取消、失败和超预算保留部分结果；不完整或过期报告不能定稿。
- 等待确认与最终用量保存交错时仍能完成取消；重复定稿只创建一次成功学习候选。
- 缓存空结果不重复扣取外部请求额度；模型报告修复复用已完成矩阵，不重复调查。

Alembic 在独立数据库完成了空库升级 → 降至 `20260828_0003` → 再升级 `20260922_0004`。开发数据库初始化也由持久化测试覆盖。

完整回归复现命令（项目根目录）：

```powershell
$env:PYTHONPATH = 'src;.;tests/api'
$env:PYTHONIOENCODING = 'utf-8'
python -m pytest tests `
  --ignore=tests/retrieval/test_private_search_regressions.py `
  --deselect=tests/retrieval/test_search_graph_boundary.py::test_graph_specialists_keep_vector_query_and_text_fallback[local_search_tool-LocalSearchTool] `
  --deselect=tests/acceptance/test_delivery_phase8.py::test_delivery_files_and_configuration_are_complete `
  --deselect=tests/acceptance/test_delivery_phase8.py::test_acceptance_matrix_contains_every_design_must_id_once `
  -q --tb=short --disable-warnings
```

## 浏览器与前端

`npm run test:research`：**4/4 通过**；`npm run build`：TypeScript 检查与 Vite 生产构建通过。

Edge 无头浏览器使用隔离数据库和确定性外部响应，完整验收了：三个对象的 3 个单元 → 新增一个字段 → 仅执行三个新增单元 → 6 个有效单元 → 查看原 Run 的复用证据 → 定稿 → 刷新后仍已定稿。同时验证双击确认不重复执行、旧页面提交返回冲突并保留输入、浏览器离线后重连。

最后源码对应的断言记录为 `.local-run/workbench/e2e-final-latest-20260922/screens/result.json`，`ok=true`，同目录保存 8 张阶段截图。复现命令见使用说明。

浏览器复跑发现并修复了两个界面时序问题：离线后事件连接未及时显示重连状态，以及最后单元提交与轮询停止交错导致界面停留在 5/6。终态转换现在会触发矩阵的最终读取，旧 Run 的异步响应不会回写当前 Run。

这项验收证明页面与后端交互链路；模型和检索在本场景中由固定响应替换，不作为真实研究语义质量的证据。真实调用结果单列如下。

## 国内模型与公开资料

使用当前配置的模型名 **`deepseek-v4-flash`** 与真实 Tavily。该名称是配置别名，未获得提供商更细的不可变模型构建版本。Token 为项目记录的输入加输出用量，耗时为活动时间，不包含等待批准的时间。

两例小型对照使用相同模型、200,000 Token、480 秒和 12 次检索的配置上限。下表来自引入最终域名/正文强制约束前的对照轮次，不能替代最终严格版本的质量评测。

| 案例 | 流程 | 状态 | Token | 活动秒数 |
|---|---|---|---:|---:|
| LangGraph / AutoGen 持久化检查点 | 工作台 | completed | 174,640 | 280.27 |
| 同题 | 旧流程 | completed | 46,984 | 98.30 |
| FastAPI / Flask 许可证 | 工作台 | completed | 142,278 | 343.86 |
| 同题 | 旧流程 | completed | 63,623 | 150.88 |

工作台两例各记录 2 个字段单元，均包含一个“未找到”。旧流程没有同构的单元记录，不能把其字段覆盖率记为零。样本显示工作台增加了研究过程成本，**没有证据支持节省 Token 或提速**。旧流程的检索计数定义与新流程的实际外部尝试计数也不完全一致，不据此计算成本改善百分比。

最终严格来源版本另跑 FastAPI / Flask 案例：**completed，139,482 Token，365.05 秒，5 次实际外部检索**。两个适用字段都有来源：FastAPI 为直接支持，Flask 保留为推断。来源为官方仓库的 LICENSE 文件；没有把从条款推断 SPDX 标识说成文件直接声明。其结构覆盖为 2/2，未知字段 0。

这仍不是普遍质量提升证明。真实输出保留了版本未锁定等限制。独立人工对语义支持和结论质量的评分尚未完成，不用引用存在性或字面定位检查代替。

## 真实增量与恢复

在严格来源案例上新增仅适用于 FastAPI 的“官方仓库地址”字段，不提高预算：

- 两个原许可证单元继续有效并被复用。
- 只创建一个待调查工作单元；Flask 的新增字段为不适用。
- 新增 2 次外部检索、39,439 Token、114.23 秒；新字段证据成功保存。
- 报告阶段到达剩余时间上限，状态为 `budget_exhausted`；导出保留已完成结果，但 `report.complete=false`。

随后测试脚本明确把研究总预算从 480 秒 / 200,000 Token 修订为 720 秒 / 260,000 Token，再批准新版本；检索上限仍为 12。新 Run 复用已保存单元，直接进入报告阶段，不重新检索。

第一次报告恢复因模型把“不适用”范围状态写成无引用结论而失败，原始记录保留。修复后重新执行：**completed，新增外部检索 0 次，复用 3 个有证据单元，新增 13,958 Token / 48.27 秒，完整报告可审阅**。整个研究累计 7 次外部检索、228,237 Token、647.05 秒，未重置早期失败的用量。

修复保持了原引用检查：不适用和缺失状态由矩阵表达；无效模型综合文字不能直接通过，必要时使用已验证单元的值和对应真实引用生成保守报告。

## 证据文件与复现

本地原始记录：

- `.local-run/workbench/live/20260922-180658/`：首次报告引用故障，保留失败现场。
- `.local-run/workbench/live/20260922-181626/results.json`：两例工作台/旧流程对照。
- `.local-run/workbench/live/20260922-182649/license-workbench.json`：严格来源真实案例。
- `.local-run/workbench/live/20260922-182649/incremental.json`：不提高预算的真实增量停止。
- `.local-run/workbench/live/20260922-182649/incremental-r5.json`：保留报告恢复失败现场。
- `.local-run/workbench/live/20260922-182649/incremental-r6.json`：最终恢复完成，零新增检索。

运行命令见[研究工作台说明](research-workbench.md)。真实案例保留为待审阅状态，未替用户接受其事实结论。语义复核应逐单元核对引用原文、对象版本、条件差异及推断前提。
