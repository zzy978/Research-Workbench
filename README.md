<h1 align="center">DeepReseach Agent Harness</h1>

<p align="center">
  <strong>面向复杂研究任务的可恢复、多智能体、证据驱动 Agent Runtime</strong>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white">
  <img alt="React" src="https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black">
  <img alt="Neo4j" src="https://img.shields.io/badge/Neo4j-5.22-4581C3?logo=neo4j&logoColor=white">
  <img alt="Tests" src="https://img.shields.io/badge/tests-134%20passed-22A06B">
  <img alt="Evidence" src="https://img.shields.io/badge/evidence%20coverage-100%25-7C3AED">
</p>

<p align="center">
  <a href="#核心亮点">核心亮点</a> ·
  <a href="#系统架构">系统架构</a> ·
  <a href="#快速启动">快速启动</a> ·
  <a href="#测试与评测">测试与评测</a> ·
  <a href="#常用-api">API</a>
</p>

> 以可恢复的 **Agent Harness** 为运行底座，驱动 **Context → Plan → Execute → Report → Verify → Replan** 闭环，统一接入私域混合 RAG 与 Web 搜索，并提供全量证据上下文压缩、持久化执行、实时可观测、受控自进化和系统化 Agent 评测能力。

系统关注的不只是“生成一篇报告”，而是让长时间、多步骤、强证据约束的 Agent 任务能够可靠执行、失败恢复、过程审计、结果验证和持续演进。

新建 Web 研究支持“编辑研究范围 → 确认开始 → 比较矩阵与证据 → 增量补查 → 审阅定稿”。详见[研究工作台使用说明](docs/research-workbench.md)。

在工作台的“历史版本”中，可选择任意版本，只读查看完整研究范围，并核对与当前版本的差异。点击“回退到此版本”并确认后，所选内容会保存为一个新版本，旧版本全部保留；正在运行的研究会先暂停，新版本需重新确认才能开始调查。回退只恢复研究范围，不恢复旧审批、报告或验收状态，也不会重置累计用量。

---

## 核心亮点

### 1. 可恢复 Agent Harness

Harness Runtime 将 DeepResearch 和多智能体 Plan–Execute–Report 工作流统一封装为持久化执行协议：

- Session / Run 状态机管理会话与单次研究任务；
- Lease、版本化 Checkpoint 和幂等 ToolCall 防止重复副作用；
- Token、工具调用、并发数、重试次数和墙钟时间多维预算控制；
- 支持取消、澄清、中断恢复、错误分类重试和动态 Replan；
- Completion Contract 在 Run 完成前执行强制质量门禁。

### 2. 闭环 Agent Loop

~~~mermaid
flowchart LR
    U([User Request]) --> C[Context Build]
    C --> P[Plan]
    P --> E[Execute]
    E --> R[Report]
    R --> V{Completion Contract}

    V -->|8/8 Pass| DONE([Verified Completion])
    V -->|Report defect| FIX[Targeted Repair]
    V -->|Evidence gap| RP[Dynamic Replan]
    V -->|Budget / permission blocked| FAIL([Honest Failure])

    FIX --> R
    RP --> P

    M[(Memory)] -. bounded context .-> C
    H[(Session History)] -. on-demand recall .-> C
    S[(Approved Skills)] -. procedural guidance .-> C
    T[[Tools / Workers]] <--> E
    EL[(Evidence Ledger)] --> R
    EL --> V

    classDef entry fill:#111827,color:#fff,stroke:#111827,stroke-width:2px;
    classDef stage fill:#EEF2FF,color:#312E81,stroke:#6366F1,stroke-width:1.5px;
    classDef decision fill:#FFF7ED,color:#9A3412,stroke:#F97316,stroke-width:2px;
    classDef success fill:#ECFDF5,color:#065F46,stroke:#10B981,stroke-width:2px;
    classDef failure fill:#FEF2F2,color:#991B1B,stroke:#EF4444,stroke-width:2px;
    classDef store fill:#F8FAFC,color:#334155,stroke:#94A3B8,stroke-dasharray: 5 4;

    class U entry;
    class C,P,E,R,FIX,RP stage;
    class V decision;
    class DONE success;
    class FAIL failure;
    class M,H,S,EL store;
~~~

Planner 将研究问题拆解为 DAG 任务图，Worker 在隔离状态中串行或并行执行，Coordinator 确定性归并结果；Reflection、缺口分析和矛盾检测用于修复局部失败，避免一次模型异常导致整条研究链路失效。

### 3. 全量 Evidence Card 上下文工程

系统不精选或丢弃证据，而是将全部原始证据写入不可变 Evidence Ledger，再为每条证据生成可追溯的 Evidence Card：

- 原始证据完整保留，压缩卡始终回指稳定 evidence_id；
- 按报告章节路由相关 Card，避免每章重复注入全部原文；
- 章节超预算时执行分批 Map、Digest 和递归 Reduce；
- 阶段预算为报告和验证预留 Token；
- 全量证据附录与覆盖门禁确保任何证据都不会被静默遗漏。

| 上下文工程指标 | 优化效果 |
|---|---:|
| 累计上下文暴露量 | **↓ 93.58%** |
| 跨章节重复注入 | **↓ 80.00%** |
| 单章节最大证据上下文 | **↓ 97.48%** |
| 真实链路 LLM Token | **↓ 19.23%** |
| 全量证据覆盖率 | **100%** |

> 固定条件：58 条证据、5 个章节，原始证据完整保留。详见 [上下文冗余优化评测报告](docs/CONTEXT_REDUNDANCY_EVALUATION_REPORT.md)。

### 4. 持久化与全链路可观测

- SQLite 持久化 Session、Message、Run、Plan、Task、ToolCall、Evidence、Contract、Checkpoint、Memory、Skill 与审计记录；
- SSE 实时推送阶段变化、任务开始、工具输出、证据入账、报告生成和验证结果；
- “白板”按时间线展示每轮对话、AI 回复、Run 事件、工具参数与完整输出；
- 服务重启后可从 Checkpoint 恢复，并通过 Durable Event Replay 补齐前端进度；
- Evidence Ledger 打通“检索来源 → 工具调用 → 任务 → 报告引用 → 验证门禁”证据链。

### 5. 受控自进化

系统从验证通过的高质量 Run 轨迹中蒸馏 Skill Candidate，形成受控演进闭环：

~~~mermaid
flowchart LR
    A([Verified<br/>Trajectory]) --> B[Skill<br/>Distillation]
    B --> C{Lint & Safety}
    C -->|Reject| X([Rejected])
    C -->|Pass| D[Offline<br/>Evaluation]
    D -->|Regression| X
    D -->|Passed| E{Human<br/>Promote Gate}
    E -->|Hold| H([Candidate])
    E -->|Approve| F[(Versioned<br/>Skill Registry)]
    F --> G[Progressive Load]
    G --> I[Runtime Feedback]
    I -. new verified trajectory .-> A
    F -->|Rollback| PREV[Previous Version]

    classDef source fill:#111827,color:#fff,stroke:#111827;
    classDef process fill:#EEF2FF,color:#312E81,stroke:#6366F1;
    classDef gate fill:#FFF7ED,color:#9A3412,stroke:#F97316,stroke-width:2px;
    classDef success fill:#ECFDF5,color:#065F46,stroke:#10B981;
    classDef reject fill:#FEF2F2,color:#991B1B,stroke:#EF4444;
    classDef store fill:#F5F3FF,color:#5B21B6,stroke:#8B5CF6;

    class A source;
    class B,D,G,I,PREV process;
    class C,E gate;
    class F store;
    class H success;
    class X reject;
~~~

候选 Skill 在通过安全检查和离线评测前不会进入运行链；启用必须经过人工 Promote，支持版本管理和一键回滚。Skill 只沉淀程序性经验，不会扩大当前 Run 的信息源和工具权限。

### 6. Memory 与长会话上下文

- 持久化 Curated Memory 仅保存跨 Session 有价值的原子偏好、事实、决策或经验；
- 会话历史、Session Summary 与 FTS5 Episodic Recall 按需加载，不与长期 Memory 混为一体；
- Query Resolver 支持代词、省略和多轮追问消歧；
- Memory 有来源引用、容量上限、生命周期和人工确认门禁；
- 完整 AI 回复、研究报告、推理文本和研究证据不会被错误写入长期 Memory。

### 7. Agent 评测与完成门禁

Completion Contract 对每个报告执行 8 项强制检查：

| 证据与主张 | 报告与来源 |
|---|---|
| Citation Integrity | Report Consistency |
| Claim Support | Required Sections |
| Evidence Card Coverage | Source Diversity |
| Minimum Evidence | Source Match |

评测系统直接读取持久化运行轨迹，统计：

- Verified / First-pass Completion Rate；
- Required Contract Pass Rate 与 Citation Validity；
- Recovery、Replan、Checkpoint Integrity 和重复副作用率；
- P50/P95 Latency、Tokens per Verified Run、Tool Calls per Run；
- Prefix Cache Hit Rate；
- 提供 Gold Evidence 时的 Precision@K、Recall@K、MRR、nDCG@K；
- 提供人工或独立 Judge 标签时的 Semantic Claim Support、Report Quality 与 Honest Failure Rate。

---

## 系统架构

~~~mermaid
flowchart TB
    subgraph UI["Experience Layer · React Console"]
        direction LR
        CANVAS[Research Canvas]
        STREAM[Realtime SSE]
        WHITE[Whiteboard]
        MEMUI[Memory]
        SKILLUI[Skills]
    end

    subgraph API["Control Plane · FastAPI"]
        direction LR
        SESSION[Session API]
        SCHED[Run Scheduler]
        EVENT[Event Stream]
        EVALAPI[Evaluation API]
    end

    subgraph HARNESS["Agent Harness Runtime"]
        direction TB
        CORE[State Machine · Lease · Budget]
        RELIABILITY[Checkpoint · Recovery · Idempotency]
        GOVERNANCE[Completion Contract · Source Policy]
        OBS[Evidence Ledger · Audit Trail]
        CORE --> RELIABILITY --> GOVERNANCE --> OBS
    end

    subgraph INTELLIGENCE["Agent Intelligence"]
        direction LR
        subgraph RESEARCH["Multi-Agent Research"]
            PLAN[Planner]
            WORK[Worker DAG]
            REFLECT[Reflection]
            REPORT[Reporter]
            PLAN --> WORK --> REFLECT --> REPORT
            REFLECT -. retry / replan .-> PLAN
        end
        subgraph CONTEXT["Context & Evolution"]
            CTX[Context Builder]
            MEMORY[Curated Memory]
            RECALL[FTS5 Recall]
            SKILLS[Skill Registry]
            EVOLVE[Distill · Eval · Promote]
            MEMORY --> CTX
            RECALL --> CTX
            SKILLS --> CTX
            EVOLVE --> SKILLS
        end
    end

    subgraph DATA["Evidence & Data Plane"]
        direction LR
        WEB[(Tavily Web)]
        PRIVATE[Private Retrieval]
        GRAPH[(Optional Neo4j GraphRAG)]
        SQL[(SQLite)]
        ART[(Artifact Store)]
        VECTOR[(NumPy Cosine + BM25)]
        RERANK[Local Reranker]
    end

    UI -->|REST / SSE| API
    API --> HARNESS
    HARNESS --> RESEARCH
    HARNESS --> CONTEXT
    WORK --> WEB
    WORK --> PRIVATE
    PRIVATE -->|hybrid default| VECTOR
    VECTOR --> RERANK
    PRIVATE -->|explicit legacy mode| GRAPH
    REPORT --> ART
    HARNESS <--> SQL
    REPORT --> OBS
    OBS --> GOVERNANCE

    classDef layer fill:#0F172A,color:#fff,stroke:#0F172A,stroke-width:2px;
    classDef control fill:#E0F2FE,color:#075985,stroke:#0EA5E9;
    classDef runtime fill:#EEF2FF,color:#312E81,stroke:#6366F1,stroke-width:1.5px;
    classDef agent fill:#F5F3FF,color:#5B21B6,stroke:#8B5CF6;
    classDef data fill:#ECFDF5,color:#065F46,stroke:#10B981;

    class CANVAS,STREAM,WHITE,MEMUI,SKILLUI layer;
    class SESSION,SCHED,EVENT,EVALAPI control;
    class CORE,RELIABILITY,GOVERNANCE,OBS runtime;
    class PLAN,WORK,REFLECT,REPORT,CTX,MEMORY,RECALL,SKILLS,EVOLVE agent;
    class WEB,PRIVATE,GRAPH,SQL,ART,VECTOR,RERANK data;
~~~

## 信息源与研究工作流

每个 Run 冻结一种信息源，禁止执行过程中跨源污染：

- **graphrag**：私域信息源的兼容 API 名称。默认 `PRIVATE_RETRIEVAL_BACKEND=hybrid`，使用文档分块、NumPy 精确余弦向量检索与 BM25 关键词召回、融合及本地 Reranker 重排；面向小中规模本地语料。显式切换 `graphrag` 后端才使用旧 Neo4j 实体关系图与 Community 检索；
- **web**：Tavily 联网搜索；
- Memory 与历史对话仅用于理解问题，不可充当本轮研究证据。

API 支持两种研究工作流；检索后端选择与工作流选择相互独立：

| 工作流 | 适用场景 |
|---|---|
| deep_research | 多轮搜索、推理和答案生成 |
| plan_execute_report | DAG 规划、多 Agent 执行、长报告生成与完成验证 |

规划任务按当前检索 Provider 的能力统一生成，默认使用 `hybrid_search`、`deep_research` 和 `reflection`。旧任务名仅用于兼容读取，转换时通过 `legacy_task_type` 保留原类型。旧 Fusion 兼容入口的同步与流式调用都会重新检索，保证每次回答使用本次检索的证据。

## 技术栈

| 层 | 技术 |
|---|---|
| Agent / LLM | LangChain、LangGraph、OpenAI-compatible API |
| Harness | Python、Pydantic、异步状态机、Durable Event、Checkpoint |
| Retrieval | NumPy 精确余弦、BM25、SentenceTransformers 本地重排、Tavily；旧模式使用 Neo4j / APOC / GDS |
| Backend | FastAPI、SSE、SQLAlchemy、SQLite、Alembic |
| Frontend | React 18、TypeScript、Vite、TanStack Query、Motion |
| Deployment | Docker Compose、Nginx、Uvicorn |

---

## 快速启动

### 1. 环境要求

- Python 3.10 或 3.11
- Node.js 20 LTS
- Docker Desktop（仅 Docker 部署或旧 Neo4j 模式需要）

### 2. 配置

~~~bash
cp .env.example .env
~~~

主要配置项：

| 配置 | 说明 |
|---|---|
| LLM_PROVIDER | 生成服务：`deepseek` 或 `openai` |
| DEEPSEEK_API_KEY | DeepSeek 密钥 |
| DEEPSEEK_BASE_URL / DEEPSEEK_MODEL | DeepSeek 地址与生成模型 |
| EMBEDDING_API_KEY / EMBEDDING_BASE_URL | 独立向量服务的密钥与地址 |
| EMBEDDING_MODEL / EMBEDDING_DIMENSIONS | 向量模型与维度 |
| EMBEDDING_REQUEST_BATCH_SIZE | 每次向量 API 请求的文本数，百炼 v4 使用 10 |
| OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_LLM_MODEL | `LLM_PROVIDER=openai` 时使用，兼容其他 OpenAI 协议服务 |
| TAVILY_API_KEY | Web 联网模式密钥 |
| PRIVATE_RETRIEVAL_BACKEND | 私域后端，默认 `hybrid`；`graphrag` 启用旧图检索 |
| RAG_INDEX_DIR | 私域索引目录，默认 `./data/rag_index` |
| RAG_CHUNK_SIZE / RAG_CHUNK_OVERLAP | 新索引分块大小与重叠字符数，默认 800 / 120 |
| RAG_CANDIDATE_K / RAG_RERANK_K | 单路召回数量 / 进入重排的融合候选数量，默认 30 / 20 |
| RAG_MIN_RERANK_SCORE | 最低重排分，默认 0.01；过滤很弱的匹配。更换模型后需重新校准，设为 0 可进行不设阈值的对照 |
| RERANKER_MODEL | 重排模型名称或本地目录，默认 `BAAI/bge-reranker-v2-m3` |
| RERANKER_DEVICE / RERANKER_BATCH_SIZE / RERANKER_MAX_LENGTH | 重排设备、批次与文本长度上限，默认 cpu / 8 / 1024 |
| NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD | 仅旧 GraphRAG 模式需要 |
| APP_DATABASE_URL | SQLite 数据库地址 |
| RUN_MAX_LLM_TOKENS / RUN_WALL_TIME_SECONDS | Harness 预算 |
| FRONTEND_PORT | 前端端口，默认 5173 |

完整选项见 [.env.example](.env.example)。

默认配置为 DeepSeek `deepseek-v4-pro` 生成 + 阿里云百炼 `text-embedding-v4` 向量（1024 维）。在 `.env` 分别填写 `DEEPSEEK_API_KEY` 和 `EMBEDDING_API_KEY`，百炼地址须与密钥所属地域一致；如控制台提供业务空间专用地址，请替换 `EMBEDDING_BASE_URL`。修改后重启后端。两家服务使用各自的密钥，不能互换。

`CACHE_EMBEDDING_PROVIDER=openai` 表示使用 OpenAI 兼容协议，实际复用上述向量服务，此配置下请求发往百炼。旧的 `OPENAI_EMBEDDINGS_MODEL`、`OPENAI_EMBEDDING_DIMENSIONS`、`OPENAI_EMBEDDING_BATCH_SIZE` 仍可使用；新的独立向量配置优先。未配置独立向量地址时，旧的共享 OpenAI 地址和密钥仍然生效。

若已有知识库向量或语义缓存，更换向量模型后需要重新生成索引并清理旧的向量缓存；即使维度相同，不同模型的向量也不能混用。

### 3. Docker 启动

~~~powershell
docker compose up -d --build
~~~

默认启动前后端，不需要 Neo4j 或 Neo4j 密码。将文档放入 `files/` 后，在运行中的后端容器里建库并预热本地重排模型：

~~~powershell
docker compose exec backend python build_rag_index.py --files /app/files --index-dir /app/data/rag_index
docker compose exec backend python build_rag_index.py --prepare-reranker
~~~

Docker 中保持 `RAG_INDEX_DIR = ./data/rag_index`（或 `/app/data/rag_index`）；不要填 Windows 主机路径。本地文档、索引与模型缓存分别通过 `files/`、`data/`、`cache/` 挂载保存，更新文档后重新执行建库命令。

访问：

- 前端：http://127.0.0.1:5173
- API：http://127.0.0.1:8000

旧图模式需要在 `.env` 设置 `PRIVATE_RETRIEVAL_BACKEND = graphrag` 和有效的 `NEO4J_PASSWORD`，再运行 `docker compose --profile graph up -d --build`。构建会根据此配置安装 `requirements-graph.txt` 中的可选图谱依赖；切换后端模式后需要重新构建镜像。仅此模式提供 Neo4j Browser：http://127.0.0.1:7474。更新旧图时可执行 `docker compose exec backend python build_knowledge_graph.py`。

若前端端口冲突：

~~~powershell
$env:FRONTEND_PORT = "5174"
docker compose up -d --build
~~~

### 4. 本地开发

~~~powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

$env:PYTHONPATH = "$PWD\src"
python -m alembic upgrade head
python -m backend.server
~~~

日常启动后端使用 `.\.venv\Scripts\python.exe -m backend.server`，无需重复安装依赖。
`requirements.txt` 用于默认 Hybrid/Web 模式。旧 Neo4j GraphRAG 需安装 `requirements-graph.txt`（已包含默认依赖）。单独使用旧向量相似度缓存时还需安装 `faiss-cpu==1.11.0`；默认 Hybrid/Web 工作流已关闭该缓存功能。当前前端使用 React，不需要 Streamlit 或 PyVis。
按一次 Ctrl+C 后，后端最多等待现有 HTTP 连接 5 秒，再取消未结束的请求并清理后台任务和数据库。
若直接使用 Uvicorn CLI，必须附带 `--timeout-graceful-shutdown 5`，否则默认无限等待连接退出。

另开终端启动前端：

~~~powershell
cd frontend
npm install
npm run dev
~~~

也可使用本地脚本：

~~~powershell
.\scripts\start-local.ps1
.\scripts\stop-local.ps1
~~~

本地启动脚本优先使用 `.venv\Scripts\python.exe`，未创建项目虚拟环境时才使用 PATH 中的 `python`。脚本读取 `.env` 的私域后端配置：默认 Hybrid 不调用 Docker；旧 `graphrag` 模式启动 Neo4j。已有外部 Neo4j 服务时可使用 `.\scripts\start-local.ps1 -SkipNeo4j`。

### 5. 建立私域文档索引

将原始文档放入 `files/`，在项目根目录执行一次建库；文档更新后再运行以发布新索引：

~~~powershell
.venv/Scripts/python.exe build_rag_index.py --files ./files --index-dir ./data/rag_index
~~~

索引复用已有的 `EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL`、`EMBEDDING_MODEL` 和维度配置，无需另配向量服务。建库会向所配置的 Embedding 服务发送文档分块；检索时会发送查询文本。请使建库的 `--index-dir` 与服务的 `RAG_INDEX_DIR` 保持一致。

默认重排模型为 `BAAI/bge-reranker-v2-m3`，在本机推理，无需 Reranker API 密钥。首次下载模型权重需要网络，重排过程不向模型下载服务发送原文。可提前下载并加载模型：

~~~powershell
.venv/Scripts/python.exe build_rag_index.py --prepare-reranker
~~~

若已有本地模型目录，在 `.env` 中独立设置 `RERANKER_MODEL = D:/models/bge-reranker-v2-m3`；生成模型与 Embedding 配置无需因此改变。默认 CPU 推理可通过 `RERANKER_DEVICE` 调整，设备必须受本机 PyTorch 支持。

本地向量检索采用 NumPy 精确余弦，适用于小到中等规模语料；大型语料需另行评估索引内存与召回延迟。CPU 重排可能需要数十秒，`RAG_RERANK_K` 控制每次推理的候选数；增大候选数前应检查 `RUN_TOOL_TIMEOUT_SECONDS`。重排分仅用于排序和阈值筛选，不是事实正确性的概率。

新流程为：原始文档 → 分块 → Embedding 与本地索引 → 向量 / BM25 双路召回 → 融合 → 本地重排 → 可引用证据。现有 Neo4j 图谱可以保留；新 RAG 索引不会自动迁移图中的数据，必须从原始 `files/` 文档重新构建。切换回旧图检索时，先安装可选依赖，再设置 `PRIVATE_RETRIEVAL_BACKEND = graphrag` 并配置、启动 Neo4j：

~~~powershell
.venv/Scripts/python.exe -m pip install -r requirements-graph.txt
~~~

只有需要构建或更新旧图时才运行：

~~~powershell
$env:PRIVATE_RETRIEVAL_BACKEND = "graphrag"
.venv/Scripts/python.exe build_knowledge_graph.py
~~~

修改配置后重启后端。API 的私域 `source_mode` 继续使用 `graphrag`，无需修改已有调用。`/api/v1/capabilities` 会给出实际 `backend`；Hybrid 可用性只表示配置、索引清单与快照文件检查通过，不代表 Embedding API 已连通或重排模型已成功加载。`/api/v1/health` 同样不会为 Hybrid 探测 Neo4j，也不进行真实模型推理。

---

## 测试与评测

### 自动化回归

~~~powershell
python -m pytest -q
cd frontend
npm run build
~~~

当前完整后端回归：**134 passed**；前端 TypeScript 类型检查与生产构建通过。

### 系统量化评测

~~~powershell
python scripts/evaluate_runs.py --output output/evaluation/summary.json
python scripts/evaluate_runs.py --labels evals/system/cases.json --retrieval-k 10
~~~

评测原则：

- 确定性指标直接从持久化 Run 轨迹计算；
- Claim Support 的规则结果只作为代理值；
- 语义事实支持率与报告质量必须来自人工或独立 Judge 标签；
- 建议固定模型、温度、预算和数据版本，并对非确定性案例重复运行。

详细说明见 [系统评测文档](evals/system/README.md)。

---

## 常用 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/v1/health | 系统与依赖健康检查 |
| GET | /api/v1/capabilities | 信息源可用性、私域实际后端与检查级别 |
| POST/GET | /api/v1/sessions | 创建或查询会话 |
| POST | /api/v1/sessions/{id}/messages | 发送消息并创建 Run |
| GET | /api/v1/sessions/{id}/whiteboard | 全链路白板日志 |
| GET | /api/v1/runs/{id} | Run 状态、阶段与预算 |
| GET | /api/v1/runs/{id}/events | Durable SSE 事件流 |
| GET | /api/v1/runs/{id}/evidence | Evidence Ledger |
| GET | /api/v1/runs/{id}/report | 报告与 Contract 结果 |
| POST | /api/v1/runs/{id}/pause | 在安全阶段边界暂停并保存 Checkpoint |
| POST | /api/v1/runs/{id}/cancel | 取消任务 |
| POST | /api/v1/runs/{id}/resume | 从 Checkpoint 恢复同一 Run |
| GET/PATCH | /api/v1/memories | Curated Memory 管理 |
| GET/POST | /api/v1/skills | Skill 查看、评测、启用与回滚 |
| GET | /api/v1/evaluations/runs/{run_id} | 单 Run 评测 |
| GET | /api/v1/evaluations/summary | 聚合评测指标 |

---

## 项目结构

~~~text
.
├── backend/                         # FastAPI API 与 Run 调度
├── frontend/                        # React 研究画板、白板、Memory、Skills
├── src/deepresearch_agent/
│   ├── harness/                     # Runtime、状态机、预算、恢复、Contract
│   ├── agents/multi_agent/          # Planner、Worker、Reflection、Reporter
│   ├── context/                     # 上下文块、预算与可信边界
│   ├── memory/                      # Curated Memory、摘要与历史召回
│   ├── evolution/                   # Skill 蒸馏、评测、Promote、回滚
│   ├── evaluation/                  # Agent 与检索评测
│   ├── persistence/                 # SQLite、Repository、Artifact、Migration
│   ├── retrieval/                   # Hybrid RAG / 旧 GraphRAG / Web Provider
│   ├── graph/                       # 知识图谱构建与 Community
│   └── search/                      # DeepResearch 检索工具
├── tests/                           # Harness、API、Memory、Evolution、E2E
├── evals/                           # Gold Evidence 与 Judge 标签
├── docs/                            # 设计、上下文工程与评测报告
├── skills/                          # 版本化 Skill 内容
├── scripts/                         # 启停、评测和真实链路脚本
├── data/                            # SQLite 与 Artifact 运行数据
├── build_rag_index.py               # 从原始文档构建私域混合检索索引
└── docker-compose.yaml
~~~

## 设计原则

- **Verified Completion over plausible output**：通过验证才算完成；
- **Evidence first**：研究结论必须能回溯到证据；
- **Durable by default**：状态、事件和副作用默认持久化；
- **Bounded context**：每个阶段都有明确上下文和预算边界；
- **Controlled evolution**：经验可以演进，但必须经过评测和人工门禁；
- **Honest failure**：证据、预算或权限不足时明确失败，不伪造成功。
