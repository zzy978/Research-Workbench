import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionMetadata, ExecutionRecord, ToolCall
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.harness import SourceMode, WorkflowMode
from deepresearch_agent.persistence.repositories import CheckpointRepository, RunRepository, SessionRepository
from deepresearch_agent.context import ArtifactEditContextBuilder
from deepresearch_agent.evolution import SkillSpec


class ApiFakeDriver:
    def __init__(self, context, events=None):
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
        self._report = f"# 研究报告\n\nAPI 主链结论已有证据支持 [{self.results[0].result_id}] [{self.results[1].result_id}]\n\n## 方法\n\n持久 Harness。"
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
        artifact_root=tmp_path / "artifacts", skills_root=tmp_path / "skills", workflow_factory=ApiFakeDriver, auto_resume=False,
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
    context = client.get(f"/api/v1/runs/{run_id}/context")
    assert context.status_code == 200
    inspected = context.json()
    assert inspected["ready"] is True
    assert inspected["checkpoint"]["verified"] is True
    assert inspected["stable_snapshot_id"]
    assert inspected["total_input_tokens"] > 0
    assert inspected["stable_blocks"] and inspected["dynamic_blocks"]
    assert all({"name", "source_type", "source_ids", "trust_level", "tokens", "trimmed"} <= set(item) for item in inspected["retrieval_trace"])
    with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "1"}) as response:
        body = "".join(response.iter_text())
    assert "event: run.completed" in body
    assert "id: 1\n" not in body


def test_context_inspector_verifies_preserved_report_sections(client):
    run_id = send(client, new_session(client)).json()["run_id"]
    assert wait_terminal(client, run_id)["status"] == "completed"
    report = client.get(f"/api/v1/runs/{run_id}/report").json()["content"]
    sections = ArtifactEditContextBuilder.parse_sections(report)
    preserved = sections[0]

    async def save_inspector_checkpoint():
        await CheckpointRepository(client.app.state.database).save(run_id, "completed", {
            "context_snapshot": {
                "stable_snapshot_id": "stable-demo", "memory_snapshot_version": 2,
                "stable_blocks": [], "dynamic_blocks": [], "retrieval_trace": [],
                "token_usage_by_block": {}, "total_input_tokens": 12,
                "curated_memory": [], "recent_messages": [], "session_summary": {},
                "historical_recall_searched": False, "historical_recall": [], "used_session_ids": [],
                "artifact_edit": {
                    "operation": "replace_section", "base_run_id": "base-run", "base_sha256": "a" * 64,
                    "target_heading": "方法", "target_content": "## 方法\n\n旧的方法。",
                    "preserve_sections": [{"heading": preserved.heading, "sha256": preserved.sha256}],
                },
            },
        })

    client.portal.call(save_inspector_checkpoint)
    payload = client.get(f"/api/v1/runs/{run_id}/context").json()
    verification = payload["artifact_verification"]
    assert verification["available"] is True and verification["target_changed"] is True
    assert verification["all_preserved"] is True
    assert verification["preserved_sections"][0]["passed"] is True


def test_cancel_queued_run(client):
    client.app.state.run_service.schedule = lambda _run_id: None
    run_id = send(client, new_session(client)).json()["run_id"]
    response = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelling"
    cancelled = client.get(f"/api/v1/runs/{run_id}").json()
    assert cancelled["cancellation_requested"] is True
    assert cancelled["status"] == "cancelled"


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
    from deepresearch_agent.config import settings
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "")
    capabilities = client.get("/api/v1/capabilities").json()
    assert capabilities["sources"]["web"] == {"available": False, "reason": "TAVILY_API_KEY 未配置"}
    paths = client.get("/openapi.json").json()["paths"]
    required = {"/api/v1/sessions", "/api/v1/sessions/search", "/api/v1/runs/{run_id}/events", "/api/v1/runs/{run_id}/context", "/api/v1/memories", "/api/v1/memories/capacity", "/api/v1/skills", "/api/v1/evaluations/summary", "/api/v1/evaluations/runs/{run_id}"}
    assert required.issubset(paths)


def test_system_evaluation_api_reads_completed_run_metrics(client):
    run_id = send(client, new_session(client)).json()["run_id"]
    assert wait_terminal(client, run_id)["status"] == "completed"

    run_metrics = client.get(f"/api/v1/evaluations/runs/{run_id}")
    assert run_metrics.status_code == 200
    assert run_metrics.json()["verified_completion"] is True
    assert run_metrics.json()["citation_validity"] == 1.0

    summary = client.get("/api/v1/evaluations/summary", params={"run_id": run_id, "include_runs": False})
    assert summary.status_code == 200
    assert summary.json()["verified_completion_rate"] == 1.0
    assert summary.json()["runs"] == []


def test_curated_memory_capacity_and_session_search_api(client):
    created = client.post("/api/v1/memories", json={
        "target": "user", "content": "用户偏好简洁报告", "kind": "preference",
        "provenance_refs": ["user:explicit"], "activate": True,
    })
    assert created.status_code == 201
    assert created.json()["target"] == "user" and created.json()["status"] == "active"
    capacity = client.get("/api/v1/memories/capacity")
    assert capacity.status_code == 200
    assert capacity.json()["targets"]["user"]["tokens"] > 0

    old_session = new_session(client)
    run_id = send(client, old_session).json()["run_id"]
    assert wait_terminal(client, run_id)["status"] == "completed"
    new_session(client)
    found = client.get("/api/v1/sessions/search", params={"q": "生成研究报告", "detail": "full"})
    assert found.status_code == 200
    assert found.json()["items"] and found.json()["items"][0]["detail"] == "full"


def test_ten_turn_session_survives_app_restart(tmp_path):
    database_path = (tmp_path / "ten-turn.db").as_posix()
    artifact_root = tmp_path / "ten-turn-artifacts"
    app = create_app(database_url=f"sqlite+aiosqlite:///{database_path}", artifact_root=artifact_root, skills_root=tmp_path / "ten-turn-skills", workflow_factory=ApiFakeDriver, auto_resume=False)
    with TestClient(app) as first:
        session_id = new_session(first)
        run_ids = []
        for turn in range(10):
            response = first.post(f"/api/v1/sessions/{session_id}/messages", json={
                "client_message_id": f"turn-{turn}", "content": "那恢复机制呢？" if turn else "请研究 SQLite 会话恢复机制",
                "source_mode": "graphrag", "workflow_mode": "deep_research",
            })
            run_id = response.json()["run_id"]
            assert wait_terminal(first, run_id)["status"] == "completed"
            run_ids.append(run_id)
        detail = first.get(f"/api/v1/sessions/{session_id}").json()
        assert len(detail["messages"]) == 20
        assert len({item["run_id"] for item in detail["messages"] if item["role"] == "assistant"}) == 10
        assert all(item["status"] == "completed" for item in detail["runs"])

    reopened_app = create_app(database_url=f"sqlite+aiosqlite:///{database_path}", artifact_root=artifact_root, skills_root=tmp_path / "ten-turn-skills", workflow_factory=ApiFakeDriver, auto_resume=False)
    with TestClient(reopened_app) as reopened:
        detail = reopened.get(f"/api/v1/sessions/{session_id}").json()
        assert len(detail["messages"]) == 20
        assert {item["run_id"] for item in detail["runs"]} == set(run_ids)


def test_skill_api_evaluate_promote_and_rollback(client):
    run_id = send(client, new_session(client)).json()["run_id"]
    assert wait_terminal(client, run_id)["status"] == "completed"

    async def register(version):
        return await client.app.state.run_service.skill_registry.register_candidate(
            run_id=run_id,
            spec=SkillSpec(
                name="api-research-skill", description="通过 API 评测和人工启用的同源研究流程。", version=version,
                source_modes=["graphrag"], created_from_runs=[run_id], triggers=["研究报告"], inputs=["问题"],
                steps=["规划", "检索", "报告", "验证"], allowed_tools=["local_search"],
                fallback="证据不足时继续同源检索。", verification=["citation_integrity"],
            ),
            eval_cases=[{"name": "positive"}, {"name": "boundary"}],
        )

    for version in ("0.1.0", "0.1.1"):
        client.portal.call(register, version)
        evaluated = client.post(f"/api/v1/skills/api-research-skill/versions/{version}/evaluate")
        assert evaluated.status_code == 200 and evaluated.json()["status"] == "passed"
        promoted = client.post(f"/api/v1/skills/api-research-skill/versions/{version}/promote")
        assert promoted.status_code == 200 and promoted.json()["status"] == "active"
    rolled_back = client.post("/api/v1/skills/api-research-skill/rollback")
    assert rolled_back.status_code == 200 and rolled_back.json()["version"] == "0.1.0"
