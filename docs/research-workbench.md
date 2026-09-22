# 研究工作台

研究工作台面向公开资料下的技术比较。新建 Web 研究会先生成研究范围，确认后才开始深入调查。私域研究和已有历史报告仍沿用原来的入口。

## 完成一次研究

1. 在对话中选择 Web 资料，输入问题，例如“比较三个框架的持久化、恢复方式和许可证”。
2. 等待大纲生成，核对问题、硬约束、候选对象及版本、比较字段、来源准则和预算。没有比较对象的开放问题按问题组织覆盖情况。
3. 在表单中编辑，或输入自然语言修改要求并查看生成的新版本。需要正文证据的字段请选择“需要正文”；需要限制官方来源时填写允许的来源域名。修改不会自动批准。
4. 点击“确认并开始”。确认绑定当前版本；旧页面提交会收到版本冲突，需要刷新核对。
5. 在比较矩阵中查看已调查、未找到、冲突和待补查的字段。点击引用可打开证据、原始地址及访问记录。搜索片段与正文有明确区别。
6. 新增字段或调整对象版本后保存并重新确认。系统保留仍有效的单元，只调查缺失或失效的部分。也可以选中已有单元，填写原因后定向补查。
7. 阅读报告和原始证据。完整报告可以定稿；预算耗尽、取消或失败留下的部分结果可以导出，但不能作为完整研究定稿。

运行中的范围修改会先请求暂停，待当前单元保存后才能继续。如果提示正在保存进度，请稍后重试。新补查沿用研究项目剩余预算；提高预算也要保存新版本并确认。等待确认期间不消耗运行时间预算。

## 证据状态的含义

| 状态 | 含义 |
|---|---|
| 有证据支持 | 当前结论关联了有效来源和定位；仍需核对其语义是否支持结论 |
| 推断 | 基于所列来源推导，附带前提与解释 |
| 存在冲突 | 至少两个不同来源存在差异，需要结合版本与条件判断 |
| 未找到 | 已有检索记录，但未取得满足字段要求的证据；不等于该能力不存在 |
| 不适用 | 当前字段的适用范围不包含该对象 |
| 尚未调查 | 尚无该单元的调查结果 |
| 需要复查 | 范围、来源或定向补查要求使已有结论失效 |

结构检查会核对引用存在性、研究归属、内容指纹和范围版本。它不等于事实核验，也不能自动证明引文在语义上支持结论。

## 启动与数据库升级

使用项目现有的环境配置和前后端启动方式。已有数据库先备份，再在项目根目录执行一次：

```powershell
python -m alembic upgrade head
```

开发环境的 `create_schema` 同样会注册研究表。不要针对已有数据库重新初始化或导入演示数据。日常启动无需重复安装依赖或执行演示脚本。

研究项目、大纲版本、审批、关联运行、比较单元和报告均保存在 SQLite。导出的 Markdown 和 JSON 是可读副本，不是另一套可写状态库。历史 Run 不会被补造审批记录。

## API

研究 ID 由消息提交和 Run 查询返回，所有版本修改使用 `revision` 与 `fingerprint`。

| 路径 | 操作 |
|---|---|
| `GET /api/v1/research/{id}` | 当前范围、审批、关联运行、累计用量和报告 |
| `POST /api/v1/research/{id}/revisions` | 完整 `spec` 或自然语言 `instruction`，二选一 |
| `POST /api/v1/research/{id}/approve` | 确认具体版本并幂等启动 |
| `GET /api/v1/research/{id}/matrix` | 单元结果与引用 |
| `POST /api/v1/research/{id}/followups` | 指定 `cells`、`reason` 和 `client_request_id` |
| `POST /api/v1/research/{id}/accept` | 确认当前报告的 `report_fingerprint` |
| `GET /api/v1/research/{id}/export?format=markdown` | 导出报告；`format=json` 包含范围与证据矩阵 |

审批、补查和定稿的过期请求返回 `409`。重复的补查请求 ID 必须对应同一版本、目标和原因。复用的旧证据仍保留原 Run 与证据 ID。

## 复现验证

确定性测试使用隔离的临时 SQLite 和固定模型响应，不访问生产会话。前端构建与状态测试：

```powershell
$env:PYTHONPATH = 'src;.;tests/api'
python -m pytest tests/research tests/api/test_research_workbench.py tests/api/test_research_lifecycle_regressions.py tests/retrieval/test_research_request_guard.py -q
Push-Location frontend
npm run test:research
npm run build
Pop-Location
```

浏览器验收使用真实 API、SQLite、SSE 和页面，只替换外部模型与检索响应。需要 Node.js 22、Microsoft Edge，以及一次性安装的浏览器驱动包：

```powershell
npm install --prefix .local-run/workbench/e2e-playwright --no-save playwright-core@1.63.0
```

在项目根目录的第一个终端启动隔离后端，每次完整验收使用新的目录名：

```powershell
$env:RESEARCH_E2E_ROOT = Join-Path (Get-Location) ('.local-run/workbench/e2e-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
python -m uvicorn scripts.research_workbench_e2e_server:app --host 127.0.0.1 --port 8013
```

第二个终端启动前端，第三个终端运行浏览器场景：

```powershell
# 第二个终端
$env:VITE_API_BASE_URL = 'http://127.0.0.1:8013/api/v1'
Set-Location frontend
npm run dev -- --host 127.0.0.1 --port 5173 --strictPort

# 第三个终端，工作目录为项目根目录
node tests/e2e/research_workbench_browser.mjs .local-run/workbench/e2e-browser
```

浏览器场景保存截图与断言结果，覆盖三个对象、新增字段、重复确认、旧页面提交、断线重连、旧证据打开和定稿后刷新。测试后关闭这两个服务；日常使用请恢复正常后端地址。

真实模型小型对照使用现有模型与 Tavily 配置，会产生实际调用费用。输出放在 `.local-run/workbench/live/` 的时间戳目录，使用独立数据库：

```powershell
$env:PYTHONPATH = 'src;.'
python scripts/evaluate_research_workbench.py --live --limit 2 --baseline --timeout 480
```

固定案例覆盖框架持久化能力和开源许可证。结果保存模型名、状态、用量、研究范围、矩阵和报告。小样本用于发现故障，不能据此宣称总体质量提升；引用支持仍需人工逐项核查。

完成许可证案例后，可用 `scripts/evaluate_research_incremental.py <输出目录> --live` 新增一个仅适用于首个对象的字段，沿用剩余预算。只有显式传入 `--max-active-seconds` 或 `--max-llm-tokens` 才会修订相应总预算并通过批准接口继续。每轮结果单独保存，不覆盖失败记录。验证结果与已知限制见[验证记录](research-workbench-validation.md)。

## 适用边界

本版本使用公开 Web 检索和现有模型配置。国产模型并不自动等于数据不出网，Tavily 和远程模型接口会收到相应请求。本版本不提供内网隔离、涉密处理、多租户权限或数据防泄漏保证。来源准则和停止条件中的自然语言要求用于指导研究；允许的来源域名、正文访问级别、Web/私域工具边界、审批版本和请求额度由程序检查。
