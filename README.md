# DeepResearch HybridRAG Agent（Hermes Inspired Local MVP）

一个多智能体深度研究系统：结合**私有知识图谱与联网搜索**双信息源，引入 Harness Runtime、四层持久化 Memory 与受控自进化（Skills）闭环，并通过 React 前端 + FastAPI 后端提供完整的聊天式研究体验。

---

## 项目概述

系统围绕三个核心概念构建：

- **Session（会话）**——多轮对话的长期容器，保存消息历史、摘要、来源选择与关联运行，支持服务重启后继续聊天。
- **Run（运行）**——一条用户消息触发的一次研究任务，具有独立状态、信息源、预算、计划、证据、报告与 checkpoint，支持中断恢复与取消。
- **Harness（编排运行时）**——统一包裹 DeepResearch、Fusion 与多智能体 Plan–Execute–Report 三种工作流，提供 Completion Contract、预算控制、事件流与持久化。

前端每次发送消息须二选一指定信息源（`graphrag` 私有库 / `web` 联网搜索），本轮检索只允许调用选定来源，报告中每条引用明确标注 `[私有库]` 或 `[Web]`。

### 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.10/3.11、FastAPI、SSE（sse-starlette）、Uvicorn、SQLAlchemy + SQLite（aiosqlite）、Alembic |
| 核心引擎 | LangGraph、LangChain、Tavily、GraphRAG（Neo4j 5.22 + APOC + GDS）、HanLP、Faiss、SentenceTransformers |
| 前端 | React 18 + TypeScript + Vite、zustand、react-markdown、motion |
| 部署 | Docker Compose（Neo4j）、本地单机单 worker |

---

## 核心亮点

1. **双信息源二选一**：统一 `RetrievalProvider` 抽象，`graphrag`（私有知识图谱）与 `web`（Tavily）每轮严格互斥；跨轮可切换，来源切换不污染证据链。
2. **Harness Runtime + Agent Loop**：三类工作流（DeepResearch / Fusion / Plan–Execute–Report）统一纳入运行时，附带 Completion Contract 逐项验证、墙钟/调用/Token 预算、错误重试分类、事件总线与审计轨迹。
3. **多智能体 Plan–Execute–Report**：Planner 规划 → 并行/串行 Worker 执行（含 Reflection 反思重试）→ Reporter 写作；支持一致性检查与 Map-Reduce 长文档模式；并行 Worker 使用隔离状态快照，Coordinator 单线程归并。
4. **四层持久化 Memory**：会话摘要、情节（episodic）、语义（semantic）记忆与上下文组装器；支持代词/省略式追问与跨会话召回，服务重启后依然有效。
5. **受控自进化（Skills）**：成功运行轨迹蒸馏为候选 Skill → 离线评测 → **人工门禁 promote** 才生效 → 版本管理与一键回滚；未通过门禁的候选不会进入运行链。
6. **崩溃恢复**：Run/Event/Checkpoint 持久化，`executing` 状态中断后服务重启可自动恢复，前端通过 SSE 事件回放补齐进度。
7. **可观测性**：SSE 增量事件流（计划、工具调用、证据、阶段状态）实时推送，报告与引用逐条对应 Evidence Ledger，取消/恢复/澄清接口完备。
8. **本地 MVP 交付**：SQLite + Neo4j + 文件全部落在项目 `data/` 目录；8 个端到端场景验收 PASS，离线门禁 `70 passed`。

---

## 快速启动

### 1. 环境要求

- Python 3.10 或 3.11
- Node.js 20 LTS
- Docker（仅 `graphrag` 私有库模式需要，用于启动 Neo4j）

### 2. 配置环境变量

```bash
cp .env.example .env
```

按需修改 `.env`（密钥只保存在本机 `.env`，不提交仓库）：

| 配置 | 说明 |
|---|---|
| `OPENAI_API_KEY` | **必填**，OpenAI 兼容 API 密钥 |
| `OPENAI_BASE_URL` | 兼容服务地址，默认 `http://localhost:13000/v1` |
| `OPENAI_LLM_MODEL` / `OPENAI_EMBEDDINGS_MODEL` | 生成模型 / 向量模型 |
| `TAVILY_API_KEY` | **联网模式必填**（Tavily） |
| `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` | **私有库模式必填**，Neo4j 连接 |
| `APP_DATABASE_URL` | SQLite 路径，默认 `./data/app.db` |
| `APP_PORT` / `FRONTEND_ORIGINS` | 服务端口与 CORS 白名单 |
| `FRONTEND_PORT` | Docker 前端宿主端口，默认 `5173`；端口冲突时可改为 `5174` |

完整配置项及说明见 `.env.example` 注释（Harness 预算、检索参数、多智能体编排、缓存、图谱构建等均有覆盖）。

### 3. 安装依赖

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt

cd frontend
npm install
cd ..
```

### 4. 启动 Neo4j（私有库模式）

```bash
docker compose up -d neo4j
```

如需用 Docker 启动完整应用：

```powershell
docker compose up -d --build
```

若提示 `127.0.0.1:5173` 已被占用，可先停止此前由本地脚本启动的前端，或为本次 Compose 改用其他端口：

```powershell
# 方案一：停止 scripts/start-local.ps1 启动的本地进程
.\scripts\stop-local.ps1
docker compose up -d

# 方案二：保留已有进程，让 Docker 前端使用 5174
$env:FRONTEND_PORT = "5174"
docker compose up -d
# 浏览器访问 http://127.0.0.1:5174
```

### 5. 初始化数据库并启动后端

```bash
python -m alembic upgrade head   # src 定位已由 alembic.ini 的 prepend_sys_path 处理
```

后端采用 `src/` 布局（核心包位于 `src/deepresearch_agent/`），启动前需将 `src` 加入 `PYTHONPATH`：

```powershell
# Windows PowerShell
$env:PYTHONPATH = "$PWD\src"
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

```bash
# Linux / macOS
export PYTHONPATH="$PWD/src"
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

### 6. 启动前端

```bash
cd frontend
npm run dev
```

浏览器访问 `http://127.0.0.1:5173`，创建会话 → 选择信息源（私有库 / 联网搜索）→ 发送消息，即可观察实时研究进度与最终报告。

若运行结束显示 `BUDGET_EXHAUSTED: ... llm_tokens=...`，表示本轮 prompt + completion 确实达到 `RUN_MAX_LLM_TOKENS`。先重新构建后端，再按机器资源提高 `.env` 中的值，例如 `RUN_MAX_LLM_TOKENS=200000`；不要把已完成的 Run 直接标记为成功。

### Windows 一键启动

```powershell
.\scripts\start-local.ps1          # 含 Neo4j 启动、Alembic 迁移、前后端拉起
.\scripts\stop-local.ps1           # 停止本地进程
```

该脚本也会识别没有 PID 文件的本项目 Vite 进程；如果 5173 仍被其他程序占用，使用 `netstat -ano | findstr :5173` 检查其 PID。

### 运行测试

```bash
python -m pytest -q                # 后端离线门禁（当前 101 passed）
cd frontend && npm run build       # 前端类型检查 + 生产构建
```

### 系统量化评测

评测器从持久化 Run 轨迹计算 Verified/First-pass Completion、引用合法率、恢复与 Replan 成功率、Checkpoint 完整率、重复副作用率、P50/P95 时延和每个验证通过任务 Token；提供 Gold Evidence 时额外计算 Precision@K、Recall@K、MRR 与 nDCG@K。

```powershell
python scripts/evaluate_runs.py --output output/evaluation/summary.json
python scripts/evaluate_runs.py --labels evals/system/cases.json --retrieval-k 10
```

语义 Claim Support 与报告质量只接受人工或独立 Judge 标签，未标注时不会用确定性引用规则伪造分数。数据格式和完整指标说明见 `evals/system/README.md`。

### 常用 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health`、`/api/v1/capabilities` | 健康检查与能力声明 |
| POST/GET | `/api/v1/sessions` | 创建 / 列出会话 |
| GET/PATCH/DELETE | `/api/v1/sessions/{id}` | 会话详情 / 更新 / 删除 |
| POST | `/api/v1/sessions/{id}/messages` | 发送消息（202，触发 Run） |
| GET | `/api/v1/runs/{id}`、`.../events`、`.../evidence`、`.../report` | Run 状态 / SSE 事件流 / 证据 / 报告 |
| POST | `/api/v1/runs/{id}/cancel`、`/resume`、`/clarifications` | 取消 / 恢复 / 澄清 |
| GET/PATCH/DELETE | `/api/v1/memories` | 记忆检索 / 编辑 / 删除 |
| GET/POST | `/api/v1/skills`、`.../evaluate`、`.../promote`、`.../rollback` | Skill 查看 / 评测 / 启用 / 回滚 |
| GET | `/api/v1/evaluations/summary`、`.../runs/{run_id}` | 聚合指标 / 单 Run 指标 |

---

## 文件结构

```
.
├── backend/                      # FastAPI 后端
│   ├── app/
│   │   ├── main.py               # 应用入口与路由注册
│   │   ├── api/v1/               # health / sessions / runs / learning 路由
│   │   ├── services/             # run_service、chat_service、event_stream（SSE）
│   │   └── schemas/              # API 与持久化 DTO
│   └── Dockerfile
├── frontend/                     # React + Vite + TS 前端
│   └── src/
│       ├── pages/ChatPage.tsx    # 聊天主界面（会话侧栏、消息流、报告、看板）
│       ├── components/           # ReportView、SessionSidebar、StageCard、CanvasBoard 等
│       └── hooks/                # useRunEvents（SSE）、useStagePositions
├── src/                          # src 布局：核心引擎包（包名 deepresearch_agent，PYTHONPATH 需包含 src）
│   └── deepresearch_agent/       # 核心引擎包
│       ├── harness/              # Runtime、Workflow、bootstrap、recovery、checkpoints、event_bus、policies、evidence、verifiers
│       ├── agents/               # DeepResearch / Fusion / multi_agent（planner、executor、reporter、integration）
│       ├── search/               # 检索工具集：local/global/hybrid/naive、deep_research_tool、tool_registry
│       ├── retrieval/            # base、tavily_provider、graphrag_provider、router（统一 Provider）
│       ├── memory/               # episodic、semantic、session_summary、context_builder、service
│       ├── evolution/            # SkillLoader、SkillRegistry、TrajectoryDistiller、Evaluator、Linter、Promotion
│       ├── evaluation/           # Run/聚合指标、检索指标与 Gold 标签模型
│       ├── persistence/          # SQLite、ArtifactStore、repositories、Alembic 迁移
│       ├── graph/                # 知识图谱构建：extraction、indexing、processing、structure、community
│       ├── pipelines/ingestion/  # 文档摄入（text_chunker、document_processor、file_reader）
│       ├── integrations/build/   # 图谱索引构建（build_graph、build_chunk_index、增量更新）
│       ├── models/               # LLM / Embedding 配置与封装
│       ├── config/               # settings.py（全部环境变量）、prompts
│       ├── community/            # 社区检测（leiden / sllpa）
│       └── cache_manager/        # 模型与结果缓存
├── tests/                        # 分阶段测试：acceptance / api / harness / memory / evolution / persistence / retrieval / models / smoke
├── scripts/                      # start-local.ps1、stop-local.ps1、backup/restore、e2e-poll-check.ps1
├── skills/                       # Skill 定义目录（SKILLS_ROOT）
├── docs/acceptance/              # 验收矩阵与结果
├── evals/                        # 基线、系统指标标签格式与技能评测
├── data/                         # 运行产物：app.db、artifacts（git 忽略）
├── cache/                        # 模型与检索缓存（git 忽略）
├── files/                        # 私有知识库文档目录（FILES_DIR）
├── alembic.ini                   # 数据库迁移配置
├── docker-compose.yaml           # Neo4j 服务编排
├── pytest.ini / requirements.txt / .env.example
└── progress.md                   # 阶段化开发记录
```

---

