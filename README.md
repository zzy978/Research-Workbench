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

## Local MVP development baseline (phases 0–1)

The repository now includes the phase 0 engineering baseline and phase 1 durable fact store described in `HERMES_INSPIRED_LOCAL_MVP_DEVELOPMENT_EXECUTION_PLAN.md`:

- Python 3.11 is the target runtime (the code remains compatible with Python 3.10); Node.js 20 LTS and Neo4j 5.22 are the target local services.
- The existing DeepResearch and Plan–Execute–Report entry points remain intact. Parallel PER workers execute against isolated state snapshots and the coordinator merges results deterministically.
- SQLite stores Sessions, Messages, Runs, Events, Checkpoints, plans/tasks/tool calls, Evidence, Contract checks, Memory/Skill records and audit events. Large artifacts remain under `data/artifacts/`.
- The local MVP backend must run with one FastAPI worker. Set `FASTAPI_WORKERS=1` in `.env` before later API stages are started.

Install the updated dependencies and create/update the database:

```powershell
python -m pip install -r requirements.txt
alembic upgrade head
```

Run the phase 0/1 tests:

```powershell
python -m pytest tests/persistence tests/smoke
```

The migration applies SQLite WAL, foreign keys and FTS5 indexes through the configured connection. Runtime data, artifacts, frontend dependencies/build output and real environment files are ignored by Git. Do not put credentials in `.env.example`; the checked-in file contains empty placeholders only.

The React directory is intentionally only a buildable phase-0 engineering shell. HTTP/SSE chat behavior belongs to phases 4–5 and is not claimed as implemented here.
