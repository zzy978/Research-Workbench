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
python search_without_stream.py "你的问题" --agent deep
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
