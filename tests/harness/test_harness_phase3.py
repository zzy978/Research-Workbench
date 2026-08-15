import json

import pytest
import pytest_asyncio

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from graphrag_agent.agents.multi_agent.core.execution_record import ExecutionMetadata, ExecutionRecord, ToolCall
from graphrag_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from graphrag_agent.harness import RunStatus, SourceMode, WorkflowMode
from graphrag_agent.harness.budgets import BudgetExceeded, BudgetLimits, BudgetManager
from graphrag_agent.harness.checkpoints import CheckpointManager
from graphrag_agent.harness.contracts import ContractEvaluator
from graphrag_agent.harness.run_context import RunContext
from graphrag_agent.harness.runtime import HarnessRuntime
from graphrag_agent.harness.errors import AppError, ErrorCode
from graphrag_agent.harness.state_machine import InvalidTransition, StateMachine
from graphrag_agent.persistence import ArtifactStore, Database
from graphrag_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
    EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository,
    SessionRepository,
)


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'stage3.db').as_posix()}")
    await db.create_schema()
    yield db
    await db.close()


async def create_run(database, *, budget=None):
    session = await SessionRepository(database).create(SessionCreate(title="stage3"))
    message, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="请给出有证据的研究报告", client_message_id=f"client-{session.session_id}"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH,
            config_snapshot={"min_evidence": 1}, budget=budget or BudgetLimits().model_dump(),
        ),
    )
    return message, run


class FakeDriver:
    execute_calls = 0

    def __init__(self, context, *, dangling=False, tool_count=1):
        self.context = context
        restored = context.workflow_state
        self.executed = bool(restored.get("executed"))
        self.repaired = bool(restored.get("repaired"))
        self.dangling = dangling
        self.tool_count = tool_count
        self.results = [
            RetrievalResult(
                result_id=item_id, granularity="Chunk", evidence=f"evidence-{index}",
                metadata=RetrievalMetadata(source_id=f"doc-{index}", source_type="chunk", content_hash=f"{'a' if index == 1 else 'b'}" * 64),
                source="local_search", source_mode="graphrag", score=0.9,
            )
            for index, item_id in enumerate(("raw-1", "raw-2"), start=1)
        ]
        for result, saved in zip(self.results, restored.get("results", [])):
            result.result_id = saved["result_id"]
        self._report = restored.get("report")

    async def plan(self, failures=None):
        self.failures = failures or []

    async def execute(self):
        if not self.executed:
            type(self).execute_calls += 1
            self.executed = True

    async def report(self):
        dangling = " [ev_missing]" if self.dangling and not self.repaired else ""
        self._report = f"# 研究报告\n\n结论 [{self.results[0].result_id}]{dangling}\n\n## 方法\n\n基于证据。"
        return self._report

    async def repair_report(self, failures):
        self.repaired = True
        return set(failures).issubset({"citation_integrity", "required_section", "source_diversity"})

    def snapshot(self):
        return {"executed": self.executed, "repaired": self.repaired, "report": self._report, "results": [item.to_dict() for item in self.results]}

    def execution_records(self):
        calls = [ToolCall(tool_name="local_search", tool_call_id=f"call-{self.context.run_id}-{index}", source_mode="graphrag", args={"query": "safe"}, result={"ok": True}) for index in range(self.tool_count)]
        return [ExecutionRecord(task_id=f"task-{self.context.run_id}", session_id=self.context.session_id, worker_type="fake", tool_calls=calls, evidence=self.results, metadata=ExecutionMetadata(worker_type="fake", tool_calls_count=len(calls), evidence_count=len(self.results)))]

    def evidence_results(self):
        return [(f"task-{self.context.run_id}", f"call-{self.context.run_id}-0", "local_search", item) for item in self.results]

    def report_consistency(self):
        return True

    def plan_record(self):
        task = {"task_id": f"task-{self.context.run_id}", "task_type": "deep_research", "source_mode": "graphrag", "status": "pending", "description": "research"}
        return ({"plan_id": f"plan-{self.context.run_id}", "status": "executing", "tasks": [task]}, [task])


class RetryOnceDriver(FakeDriver):
    attempts = 0

    async def execute(self):
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise AppError(ErrorCode.RETRIEVAL_TIMEOUT, "temporary timeout", retryable=True)
        await super().execute()


def build_runtime(database, tmp_path, factory):
    return HarnessRuntime(
        run_repository=RunRepository(database), message_repository=MessageRepository(database),
        event_repository=EventRepository(database), checkpoint_repository=CheckpointRepository(database),
        evidence_repository=EvidenceRepository(database), contract_repository=ContractRepository(database),
        trajectory_repository=PlanTaskToolRepository(database), workflow_factory=factory,
        artifact_store=ArtifactStore(tmp_path / "artifacts"), artifact_repository=ArtifactRepository(database),
    )


def test_state_machine_rejects_illegal_and_unverified_completion():
    machine = StateMachine()
    with pytest.raises(InvalidTransition):
        machine.validate(RunStatus.QUEUED, RunStatus.COMPLETED, contract_passed=True)
    with pytest.raises(InvalidTransition):
        machine.validate(RunStatus.VERIFYING, RunStatus.COMPLETED)
    machine.validate(RunStatus.VERIFYING, RunStatus.COMPLETED, contract_passed=True)


def test_budget_manager_enforces_tool_retry_and_replan_limits():
    manager = BudgetManager(BudgetLimits(max_tool_calls=1, max_task_retries=1, max_replans=1))
    manager.consume_tool()
    with pytest.raises(BudgetExceeded):
        manager.consume_tool()
    manager = BudgetManager(BudgetLimits(max_task_retries=1))
    manager.consume_retry()
    with pytest.raises(BudgetExceeded):
        manager.consume_retry()
    manager = BudgetManager(BudgetLimits(max_replans=1))
    manager.consume_replan()
    with pytest.raises(BudgetExceeded):
        manager.consume_replan()


@pytest.mark.asyncio
async def test_web_deep_research_bypasses_answer_cache_and_replan_reexecutes():
    from graphrag_agent.harness.workflow import DeepResearchDriver

    class Agent:
        def __init__(self):
            self.calls = []
            self.research_tool = type("Tool", (), {"provider_results": [], "provider_calls": []})()

        def ask(self, query, session_id, *, bypass_cache=False):
            self.calls.append(bypass_cache)
            return "answer"

    context = RunContext(
        run_id="run-web-cache", session_id="ses-web-cache", trigger_message_id="msg-web-cache",
        source_mode=SourceMode.WEB, workflow_mode=WorkflowMode.DEEP_RESEARCH,
        status=RunStatus.QUEUED, original_query="current web query", budget_limits=BudgetLimits(),
    )
    agent = Agent()
    driver = DeepResearchDriver(context, agent)
    await driver.plan()
    await driver.execute()
    await driver.plan(["min_evidence"])
    await driver.execute()
    assert agent.calls == [True, True]


def test_real_deep_research_agent_ask_accepts_and_forwards_bypass_cache(monkeypatch):
    """真实 DeepResearchAgent.ask 必须接受并转发 bypass_cache（Web Run TypeError 回归）。

    早期回归只用自定义假 Agent（自带 bypass_cache 签名），漏掉了真实
    DeepResearchAgent 重写 ask() 时未转发参数导致的 TypeError。
    """
    from graphrag_agent.agents.base import BaseAgent
    from graphrag_agent.agents.deep_research_agent import DeepResearchAgent

    # 仅构造最小实例，避免 __init__ 初始化 DeepResearchTool 的外部依赖
    agent = DeepResearchAgent.__new__(DeepResearchAgent)
    agent.show_thinking = False
    agent.use_deeper_tool = False

    calls = []

    def fake_base_ask(self, query, thread_id="default", recursion_limit=None, *, bypass_cache=False):
        calls.append((query, thread_id, recursion_limit, bypass_cache))
        return "ok"

    monkeypatch.setattr(BaseAgent, "ask", fake_base_ask)
    assert agent.ask("q", "t", 5, bypass_cache=True) == "ok"
    assert calls[-1] == ("q", "t", 5, True)
    assert agent.ask("q2", "t2", bypass_cache=False) == "ok"
    assert calls[-1] == ("q2", "t2", None, False)


@pytest.mark.asyncio
async def test_required_contract_failure_never_completes(database):
    _, run = await create_run(database)
    evaluator = ContractEvaluator(ContractRepository(database))
    verdict = await evaluator.evaluate(run_id=run.run_id, source_mode="graphrag", report="# 空报告", evidence=[], consistency_passed=True)
    assert not verdict.passed
    with pytest.raises(ValueError):
        await RunRepository(database).complete_verified(run.run_id, assistant_content="bad", usage={})


@pytest.mark.asyncio
async def test_runtime_happy_path_persists_events_checkpoints_contract_and_message(database, tmp_path):
    FakeDriver.execute_calls = 0
    _, run = await create_run(database)
    result = await build_runtime(database, tmp_path, lambda ctx: FakeDriver(ctx)).execute_run(run.run_id)
    assert result.status is RunStatus.COMPLETED
    assert FakeDriver.execute_calls == 1
    events = await EventRepository(database).list_after(run.run_id)
    names = [event.event_type for event in events]
    assert {"run.started", "plan.created", "task.completed", "tool.completed", "evidence.added", "report.completed", "verification.completed", "run.completed"}.issubset(names)
    latest = await CheckpointRepository(database).latest(run.run_id)
    assert latest.stage == "completed" and CheckpointRepository.verify(latest)
    assert await ContractRepository(database).required_checks_passed(run.run_id)
    messages = await MessageRepository(database).list_for_session(run.session_id)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert len(await EvidenceRepository(database).list_for_run(run.run_id)) == 2
    assert await database.foreign_key_violations() == []


@pytest.mark.asyncio
async def test_citation_failure_is_locally_repaired_with_bounded_retry(database, tmp_path):
    _, run = await create_run(database)
    result = await build_runtime(database, tmp_path, lambda ctx: FakeDriver(ctx, dangling=True)).execute_run(run.run_id)
    assert result.status is RunStatus.COMPLETED
    events = await EventRepository(database).list_after(run.run_id)
    assert any(event.event_type == "run.retrying" for event in events)
    assert json.loads((await RunRepository(database).get(run.run_id)).usage_json)["usage"]["task_retries"] == 1


@pytest.mark.asyncio
async def test_cancel_stops_before_new_tool_call(database, tmp_path):
    FakeDriver.execute_calls = 0
    _, run = await create_run(database)
    assert await RunRepository(database).request_cancel(run.run_id)
    result = await build_runtime(database, tmp_path, lambda ctx: FakeDriver(ctx)).execute_run(run.run_id)
    assert result.status is RunStatus.CANCELLED
    assert FakeDriver.execute_calls == 0


@pytest.mark.asyncio
async def test_tool_budget_exhaustion_is_terminal_and_not_completed(database, tmp_path):
    _, run = await create_run(database, budget=BudgetLimits(max_tool_calls=1).model_dump())
    result = await build_runtime(database, tmp_path, lambda ctx: FakeDriver(ctx, tool_count=2)).execute_run(run.run_id)
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert not await ContractRepository(database).required_checks_passed(run.run_id)


@pytest.mark.asyncio
async def test_retryable_transport_error_retries_same_execution_once(database, tmp_path):
    RetryOnceDriver.attempts = 0
    _, run = await create_run(database)
    result = await build_runtime(database, tmp_path, lambda ctx: RetryOnceDriver(ctx)).execute_run(run.run_id)
    assert result.status is RunStatus.COMPLETED
    assert RetryOnceDriver.attempts == 2
    events = await EventRepository(database).list_after(run.run_id)
    assert {"run.retrying", "run.retry_resumed"}.issubset({event.event_type for event in events})


@pytest.mark.asyncio
async def test_interrupted_executing_checkpoint_resumes_without_reexecuting_completed_batch(database, tmp_path):
    FakeDriver.execute_calls = 0
    message, run = await create_run(database)
    context = RunContext(
        run_id=run.run_id, session_id=run.session_id, trigger_message_id=message.message_id,
        source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH,
        status=RunStatus.EXECUTING, original_query=message.content, budget_limits=BudgetLimits(),
        workflow_state={"executed": True, "results": []},
    )
    await CheckpointManager(CheckpointRepository(database)).save(context, "executing")
    await RunRepository(database).update_status(run.run_id, status="interrupted", current_stage="interrupted")
    result = await build_runtime(database, tmp_path, lambda ctx: FakeDriver(ctx)).execute_run(run.run_id)
    assert result.status is RunStatus.COMPLETED
    assert FakeDriver.execute_calls == 0


@pytest.mark.asyncio
async def test_final_assistant_message_is_idempotent(database, tmp_path):
    _, run = await create_run(database)
    runtime = build_runtime(database, tmp_path, lambda ctx: FakeDriver(ctx))
    result = await runtime.execute_run(run.run_id)
    assert result.status is RunStatus.COMPLETED
    message, created = await RunRepository(database).complete_verified(run.run_id, assistant_content=result.report, usage={})
    assert created is False and message.run_id == run.run_id
    messages = await MessageRepository(database).list_for_session(run.session_id)
    assert len([item for item in messages if item.role == "assistant"]) == 1
