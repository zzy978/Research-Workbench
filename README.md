# DeepResearch HybridRAG Agent — Local MVP

这是一个本地单机研究 Agent：保留既有 GraphRAG、DeepResearch 与 Plan–Execute–Report，同时增加持久 Session/Run、Hermes 风格 Harness、Tavily、React/FastAPI、多轮 Memory 和受控 Skill 自进化。

## 能力

- 每条消息固定选择 `graphrag`（私有库）或 `web`（Tavily），同一 Run 不跨源降级。
- DeepResearch 与 Plan–Execute–Report 都经过 `Context → Plan → Execute → Report → Verify`；required Completion Contract 未全过不会进入 `completed`。
- SQLite WAL 持久化 Session、Message、Run、Event、Checkpoint、Evidence、Memory、Skill 与审计；大对象在 `data/artifacts/`。
- 多轮追问、滚动摘要、FTS5 episodic memory、带 provenance 的 semantic memory、渐进披露 procedural Skill。
- 合格复杂轨迹只生成 candidate；Skill 必须 lint、安全扫描、离线 eval 和前端人工 Promote 后才能参与新 Run，并支持 rollback。
- React 页面提供聊天、来源选择、进度/SSE、报告、引用、Memory、Skills 与系统状态。

## 环境要求

- Python 3.11（当前代码兼容 Python 3.10）
- Node.js 20 LTS
- Docker Desktop / Docker Compose（用于 Neo4j 或整套发行启动）
- Neo4j 5.22（APOC + Graph Data Science）
- OpenAI/兼容 LLM 与 Embedding Key；Web 模式另需 Tavily Key

所有服务默认只监听 `127.0.0.1`，FastAPI 必须保持 `FASTAPI_WORKERS=1`。

## 配置

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，至少配置：

- `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_LLM_MODEL`、`OPENAI_EMBEDDINGS_MODEL`
- `NEO4J_URI`、`NEO4J_USERNAME`、`NEO4J_PASSWORD`
- 使用 Web 时配置 `TAVILY_API_KEY`

不要把 `.env`、Key、数据库或 artifact 提交到 Git。若后端运行在容器而兼容 LLM 在宿主机，`OPENAI_BASE_URL` 应使用 `host.docker.internal`，不能使用容器内的 `localhost`。

## 开发模式启动

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Set-Location frontend
npm.cmd ci
Set-Location ..
docker compose up -d neo4j
python -m alembic upgrade head
```

分别启动两个终端：

```powershell
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

```powershell
Set-Location frontend
npm.cmd run dev -- --host 127.0.0.1
```

也可运行 `powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1`，停止时运行 `scripts/stop-local.ps1`。打开 <http://127.0.0.1:5173>；OpenAPI 位于 <http://127.0.0.1:8000/docs>。

## 一套命令发行启动

```powershell
docker compose up -d --build
docker compose ps
```

前端：<http://127.0.0.1:5173>；后端健康：<http://127.0.0.1:8000/api/v1/health>。Compose 持久化 `data/`、`skills/`、`files/`、`cache/` 和 Neo4j named volumes。前端 Nginx 提供 SPA fallback 与 `/api/` 反向代理。

## 首次知识库构建与旧 CLI

把文档放入 `files/` 后：

```powershell
python build_knowledge_graph.py
python search_without_stream.py "你的问题" --agent deep_research --source-mode graphrag
python search_without_stream.py "你的问题" --agent fusion --source-mode web
```

旧 `ask()`/`process_query()` 兼容入口保留；浏览器/FastAPI 主链使用持久 Harness。

## Memory 与 Skills

- Working：Run checkpoint；Episodic：消息/Run/Event + FTS5；Semantic：候选/active/冲突/过期；Procedural：版本化 `skills/research/*`。
- Memory 页面可以编辑、确认、拒绝、过期、软删除并查看 provenance。事实类候选只有合格 Run 才能自动提炼，且不会自动 active。
- Skills 页面执行 candidate → evaluate → Promote → rollback。候选不能扩大 `Run.source_mode` 或 ToolPolicy 权限，也不会修改源码或提交 Git。

## 迁移、备份与恢复

```powershell
python -m alembic current
python -m alembic upgrade head
```

备份 SQLite（使用在线 backup API）、artifacts、skills、files、cache；如指定 `-IncludeNeo4j`，脚本会短暂停止 Neo4j 并生成 dump：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/backup-local.ps1
powershell -ExecutionPolicy Bypass -File scripts/backup-local.ps1 -IncludeNeo4j
```

恢复前停止后端。恢复会先保留现有 SQLite 为 `app.pre-restore-*.db`，校验备份内每个文件的 SHA-256 后覆盖同名数据：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/restore-local.ps1 -Archive backups\local-mvp-YYYYMMDD-HHMMSS.zip
powershell -ExecutionPolicy Bypass -File scripts/restore-local.ps1 -Archive backups\local-mvp-YYYYMMDD-HHMMSS.zip -RestoreNeo4j
```

## 测试与验收

离线测试必须先通过，之后才允许受控真实调用：

```powershell
python -m pytest -q
python -m compileall -q graphrag_agent backend search_without_stream.py
python scripts/scan_secrets.py frontend/dist data/acceptance
Set-Location frontend
npm.cmd run build
```

验收矩阵和记录位于 `docs/acceptance/`。真实 E2E 使用本机 `.env`，不得把输出中的 Key 写入报告；Web 查询优先复用缓存并严格控制调用次数。

## 健康、降级与排障

- `/api/v1/health`：分别报告 API、SQLite、LLM、Neo4j、Tavily；只返回 `configured` 和状态，不返回 Key。LLM/Neo4j 使用短 TCP 探测，Tavily 健康不产生搜索调用。
- `/api/v1/capabilities`：Tavily 未配置时 Web 禁用但 GraphRAG 可用；Neo4j 不可连接时 GraphRAG 禁用但 Web 可用。
- SQLite 不可写：启动迁移/建表失败，后端拒绝无持久化运行。
- Tavily 401/403：检查 Key；429：Provider 尊重 `Retry-After`；5xx/timeout：有限退避，不切到 GraphRAG。
- SSE 断线：页面按 event ID 重连并以 Run API 校准，不代表 Run 失败。
- `budget_exhausted`：查看 Run 的 error、usage 和部分报告；新建 Run 前调整 `.env` 中对应 `RUN_MAX_*`。
- Neo4j 容器：`docker compose logs neo4j`；后端：`docker compose logs backend`；前端：`docker compose logs frontend`。

## 已知限制

- 单用户、单机、单 FastAPI worker；不含认证、RBAC、多租户与公网部署。
- 不自动混合 GraphRAG/Web；外部 Tavily 本身无法保证 exactly-once，恢复会复用已持久化的成功 tool_call。
- Skill eval 是 2～5 个相关 fixture 的轻量门禁，不是统计显著性评测或自动持续进化平台。
- 默认不展示模型隐藏思维链，只展示计划、状态、工具摘要、证据与验证结果。
