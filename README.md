# DeepResearch HybridRAG Agent

This folder is a standalone extraction of the original project focused on:

- `FusionGraphRAGAgent`
- `DeepResearchAgent`
- Knowledge graph construction, indexing, and community summaries

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` with your Neo4j and LLM settings before running graph build or search.

## Run Search

```bash
python search_without_stream.py "你的问题" --agent fusion
python search_without_stream.py "你的问题" --agent deep_research
python search_without_stream.py "你的问题" --agent fusion --source-mode web
```

## Build Knowledge Graph

Place source documents in `files/`, then run:

```bash
python build_knowledge_graph.py
```

The same build pipeline is also available as:

```bash
python -m graphrag_agent.integrations.build.main
```

## Kept Modules

The extracted package keeps only the runtime modules needed by the two agents and graph building:

- `graphrag_agent/agents`: Fusion, DeepResearch, shared base, and multi-agent orchestration
- `graphrag_agent/search`: GraphRAG search tools and deep research tools
- `graphrag_agent/graph`, `community`, `pipelines`, `integrations/build`
- `graphrag_agent/config`, `models`, `cache_manager`

Generated cache files stay under `cache/`; raw input documents stay under `files/`.

## Local MVP development baseline (phases 0–5)

The repository now includes phases 0–5 described in `HERMES_INSPIRED_LOCAL_MVP_DEVELOPMENT_EXECUTION_PLAN.md`: the engineering baseline, durable fact store, unified retrieval, persistent Harness, FastAPI/SSE service and React browser client.

- Python 3.11 is the target runtime (the code remains compatible with Python 3.10); Node.js 20 LTS and Neo4j 5.22 are the target local services.
- The existing DeepResearch and Plan–Execute–Report entry points remain intact. Parallel PER workers execute against isolated state snapshots and the coordinator merges results deterministically.
- SQLite stores Sessions, Messages, Runs, Events, Checkpoints, plans/tasks/tool calls, Evidence, Contract checks, Memory/Skill records and audit events. Large artifacts remain under `data/artifacts/`.
- `RetrievalRouter` freezes each run to exactly one provider. GraphRAG reuses the existing local/global/hybrid/naive tools; Tavily maps Web results into the same `RetrievalResult` and Evidence provenance used by PER and DeepResearch.
- DeepResearch retains its iterative query/gap-resolution loop and PER retains Planner/TaskGraph/Worker/Reporter. Both receive their source through provider injection; a failed provider is never replaced by the other source.
- The default CLI path now creates a durable Session/Message/Run and drives both workflows through `Context → Plan → Execute → Report → Verify`. Use `--legacy` only for the pre-Harness compatibility path.
- Run state transitions, budgets, events, checkpoints, cancellation, typed retry/replan, lease recovery, Evidence Ledger and Completion Contract checks are persisted. `completed` can only be written together with an idempotent assistant message after all required checks pass.
- Web search requires `TAVILY_API_KEY` in the backend environment. Without it, GraphRAG still works and requesting `--source-mode web` returns an explicit unavailable/configuration error.
- The FastAPI service exposes health/capabilities, Session/Message/Run control, durable SSE replay, Evidence/Report reads, and the phase-appropriate Memory/Skill schemas under `/api/v1`.
- The React client supports persistent sessions, strict per-message source selection, workflow choice, progress/SSE recovery, cancellation, clarification, reports, evidence provenance, and Memory/Skills/System Status pages.
- The local MVP backend must run with one FastAPI worker. Set `FASTAPI_WORKERS=1` in `.env`.

Install the updated dependencies and create/update the database:

```powershell
python -m pip install -r requirements.txt
alembic upgrade head
```

Start the backend and frontend in separate terminals:

```powershell
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --workers 1
cd frontend
npm.cmd run dev
```

Open `http://localhost:5173`. The default CORS origin is `http://localhost:5173`; add other local origins to `FRONTEND_ORIGINS` when needed.

Run the phase 0–5 tests and frontend production build:

```powershell
python -m pytest -q
cd frontend
npm.cmd run build
```

The migration applies SQLite WAL, foreign keys and FTS5 indexes through the configured connection. Runtime data, artifacts, frontend dependencies/build output and real environment files are ignored by Git. Do not put credentials in `.env.example`; the checked-in file contains empty placeholders only.

Memory extraction/context recall and Skill evaluate/promote/rollback remain gated to execution-plan phases 6–7. The phase-5 management pages show the real persisted records and Memory lifecycle operations, but do not bypass those later-stage learning and promotion gates.
