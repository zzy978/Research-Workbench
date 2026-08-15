import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from graphrag_agent.agents.multi_agent.core.execution_record import ExecutionMetadata, ExecutionRecord, ToolCall
from graphrag_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from graphrag_agent.harness import SourceMode, WorkflowMode
from graphrag_agent.persistence.repositories import RunRepository, SessionRepository


class ApiFakeDriver:
    def __init__(self, context):
        self.context = context
        self.executed = bool(context.workflow_state.get("executed"))
        self.results = [
            RetrievalResult(
                result_id=f"raw-api-{index}", granularity="Chunk", evidence=f"API integrated evidence {index}",
                metadata=RetrievalMetadata(source_id=f"doc-api-{index}", source_type="chunk", content_hash=character * 64),
                source="local_search", source_mode="graphrag", score=0.95,
            )
            for index, character in ((1, "a"), (2, "b"))
        ]
        for result, saved in zip(self.results, context.workflow_state.get("result_ids", [])):
            result.result_id = saved
        self._report = context.workflow_state.get("report")

    async def plan(self, failures=None):
        return None

    async def execute(self):
        self.executed = True

    async def report(self):
        self._report = f"# 研究报告\n\nAPI 主链结论 [{self.results[0].result_id}]\n\n## 方法\n\n持久 Harness。"
        return self._report

    async def repair_report(self, failures):
        return False

    def snapshot(self):
        return {"executed": self.executed, "result_ids": [item.result_id for item in self.results], "report": self._report}

    def execution_records(self):
        call = ToolCall(tool_name="local_search", tool_call_id=f"call-{self.context.run_id}", source_mode="graphrag", args={"query": "safe"}, result={"ok": True})
        return [ExecutionRecord(task_id=f"task-{self.context.run_id}", session_id=self.context.session_id, worker_type="fake", tool_calls=[call], evidence=self.results, metadata=ExecutionMetadata(worker_type="fake", tool_calls_count=1, evidence_count=2))]

    def evidence_results(self):
        return [(f"task-{self.context.run_id}", f"call-{self.context.run_id}", "local_search", item) for item in self.results]

    def report_consistency(self):
        return True

    def plan_record(self):
        task = {"task_id": f"task-{self.context.run_id}", "task_type": "deep_research", "source_mode": "graphrag", "status": "pending"}
        return ({"plan_id": f"plan-{self.context.run_id}", "status": "executing", "tasks": [task]}, [task])


@pytest.fixture
def client(tmp_path):
    app = create_app(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
        artifact_root=tmp_path / "artifacts", workflow_factory=ApiFakeDriver, auto_resume=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def new_session(client):
    response = client.post("/api/v1/sessions", json={"title": "API Session"})
    assert response.status_code == 201
    return response.json()["session_id"]


def send(client, session_id, *, client_id=None, source="graphrag"):
    return client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "client_message_id": client_id or str(uuid.uuid4()), "content": "请生成研究报告",
        "source_mode": source, "workflow_mode": "deep_research",
    })


def wait_terminal(client, run_id):
    for _ in range(100):
        payload = client.get(f"/api/v1/runs/{run_id}").json()
        if payload["status"] in {"completed", "failed", "cancelled", "budget_exhausted"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("Run 未在测试时间内结束")


def test_session_crud_and_pagination(client):
    session_id = new_session(client)
    listing = client.get("/api/v1/sessions?limit=10").json()
    assert listing["total"] == 1 and listing["items"][0]["session_id"] == session_id
    assert client.patch(f"/api/v1/sessions/{session_id}", json={"title": "重命名", "status": "archived"}).json()["status"] == "archived"
    assert client.patch(f"/api/v1/sessions/{session_id}", json={"status": "active"}).json()["status"] == "active"
    assert client.delete(f"/api/v1/sessions/{session_id}").status_code == 204
    assert client.get(f"/api/v1/sessions/{session_id}").status_code == 404


def test_message_idempotency_and_source_validation(client):
    session_id = new_session(client)
    first = send(client, session_id, client_id="same-client")
    second = send(client, session_id, client_id="same-client")
    assert first.status_code == second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    assert first.json()["created"] is True and second.json()["created"] is False
    invalid = send(client, session_id, source="mixed")
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert invalid.headers["X-Request-ID"].startswith("req_")


def test_run_report_evidence_and_durable_sse_replay(client):
    run_id = send(client, new_session(client)).json()["run_id"]
    terminal = wait_terminal(client, run_id)
    assert terminal["status"] == "completed", (terminal["error_code"], terminal["error_message"])
    evidence = client.get(f"/api/v1/runs/{run_id}/evidence").json()
    report = client.get(f"/api/v1/runs/{run_id}/report").json()
    assert evidence["total"] == 2 and evidence["items"][0]["source_mode"] == "graphrag"
    assert report["content"].startswith("# 研究报告")
    assert report["artifact"]["sha256"] and all(item["passed"] for item in report["verification"] if item["required"])
    with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "1"}) as response:
        body = "".join(response.iter_text())
    assert "event: run.completed" in body
    assert "id: 1\n" not in body


def test_cancel_queued_run(client):
    client.app.state.run_service.schedule = lambda _run_id: None
    run_id = send(client, new_session(client)).json()["run_id"]
    response = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelling"
    assert client.get(f"/api/v1/runs/{run_id}").json()["cancellation_requested"] is True


def test_clarification_resumes_same_run(client):
    client.app.state.run_service.schedule = lambda _run_id: None
    session_id = new_session(client)

    async def create_waiting():
        message, run, _ = await RunRepository(client.app.state.database).create_for_user_message(
            MessageCreate(session_id=session_id, role="user", content="它呢？", client_message_id="clarify-original"),
            RunCreate(session_id=session_id, trigger_message_id="atomic", source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH, config_snapshot={}, budget={}),
        )
        await RunRepository(client.app.state.database).update_status(run.run_id, status="needs_user_input", current_stage="needs_user_input")
        return run.run_id

    run_id = client.portal.call(create_waiting)
    response = client.post(f"/api/v1/runs/{run_id}/clarifications", json={"content": "主体是阶段4 API"})
    assert response.status_code == 200 and response.json()["run_id"] == run_id
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "queued"


def test_capabilities_and_openapi_are_complete(client, monkeypatch):
    from graphrag_agent.config import settings
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "")
    capabilities = client.get("/api/v1/capabilities").json()
    assert capabilities["sources"]["web"] == {"available": False, "reason": "TAVILY_API_KEY 未配置"}
    paths = client.get("/openapi.json").json()["paths"]
    required = {"/api/v1/sessions", "/api/v1/runs/{run_id}/events", "/api/v1/memories", "/api/v1/skills"}
    assert required.issubset(paths)
