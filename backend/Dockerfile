FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app

COPY requirements.txt requirements-graph.txt alembic.ini ./
ARG PRIVATE_RETRIEVAL_BACKEND=hybrid
RUN if [ "$PRIVATE_RETRIEVAL_BACKEND" = "graphrag" ]; then \
        pip install --no-cache-dir -r requirements-graph.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi
COPY backend ./backend
COPY src/deepresearch_agent ./src/deepresearch_agent
COPY skills ./skills
COPY build_rag_index.py build_knowledge_graph.py ./

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
