import json

import pytest
import pytest_asyncio

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionMetadata, ExecutionRecord, ToolCall
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.harness import RunStatus, SourceMode, WorkflowMode
from deepresearch_agent.harness.budgets import BudgetExceeded, BudgetLimits, BudgetManager
from deepresearch_agent.harness.checkpoints import CheckpointManager
from deepresearch_agent.harness.contracts import ContractEvaluator
from deepresearch_agent.harness.evidence import EvidenceLedger
from deepresearch_agent.harness.run_context import RunContext
from deepresearch_agent.harness.runtime import HarnessRuntime
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.harness.state_machine import InvalidTransition, StateMachine
from deepresearch_agent.persistence import ArtifactStore, Database
from deepresearch_agent.persistence.repositories import (
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


async def create_run(
    database, *, budget=None,
    source_mode=SourceMode.GRAPHRAG,
    workflow_mode=WorkflowMode.DEEP_RESEARCH,
):
    session = await SessionRepository(database).create(SessionCreate(title="stage3"))
    message, run, _ = await RunRepository(database).create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="请给出有证据的研究报告", client_message_id=f"client-{session.session_id}"),
        RunCreate(
            session_id=session.session_id, trigger_message_id="atomic",
            source_mode=source_mode, workflow_mode=workflow_mode,
            config_snapshot={"min_evidence": 1}, budget=budget or BudgetLimits().model_dump(),
        ),
    )
    return message, run


class FakeDriver:
    execute_calls = 0
    plan_calls = 0
    report_calls = 0

    def __init__(self, context, *, dangling=False, tool_count=1, plan_task_count=1, token_count=0):
        self.context = context
        restored = context.workflow_state
        self.executed = bool(restored.get("executed"))
        self.repaired = bool(restored.get("repaired"))
        self.dangling = dangling
        self.tool_count = tool_count
        self.plan_task_count = plan_task_count
        self.token_count = token_count
        self.mode = context.source_mode.value
        self.tool_name = "tavily_search" if self.mode == "web" else "local_search"
        self.results = [
            RetrievalResult(
                result_id=item_id, granularity="Chunk", evidence=f"evidence-{index}",
                metadata=RetrievalMetadata(
                    source_id=(f"https://example.com/source-{index}" if self.mode == "web" else f"doc-{index}"),
                    source_type=("webpage" if self.mode == "web" else "chunk"),
                    title=f"source-{index}",
                    url=(f"https://example.com/source-{index}" if self.mode == "web" else None),
                    content_hash=f"{'a' if index == 1 else 'b'}" * 64,
                ),
                source=self.tool_name, source_mode=self.mode, score=0.9,
            )
            for index, item_id in enumerate(("raw-1", "raw-2"), start=1)
        ]
        for result, saved in zip(self.results, restored.get("results", [])):
            result.result_id = saved["result_id"]
        self._report = restored.get("report")

    async def plan(self, failures=None):
        type(self).plan_calls += 1
        self.failures = failures or []

    async def execute(self):
        if not self.executed:
            type(self).execute_calls += 1
            self.executed = True

    async def report(self):
        type(self).report_calls += 1
        dangling = " [ev_missing]" if self.dangling and not self.repaired else ""
        self._report = f"# 研究报告\n\n结论内容已有充分说明 [{self.results[0].result_id}]{dangling}\n\n## 方法\n\n基于证据。\n\n## 局限\n\n当前报告仅直接引用一个来源。"
        return self._report

    async def repair_report(self, failures):
        self.repaired = True
        return set(failures).issubset({"citation_integrity", "required_section", "source_diversity"})

    def snapshot(self):
        return {"executed": self.executed, "repaired": self.repaired, "report": self._report, "results": [item.to_dict() for item in self.results]}

    def execution_records(self):
        calls = [ToolCall(tool_name=self.tool_name, tool_call_id=f"call-{self.context.run_id}-{index}", source_mode=self.mode, args={"query": "safe"}, result={"ok": True}) for index in range(self.tool_count)]
        return [ExecutionRecord(task_id=f"task-{self.context.run_id}", session_id=self.context.session_id, worker_type="fake", tool_calls=calls, evidence=self.results, metadata=ExecutionMetadata(worker_type="fake", token_usage={"total": self.token_count}, tool_calls_count=len(calls), evidence_count=len(self.results)))]

    def evidence_results(self):
        return [(f"task-{self.context.run_id}", f"call-{self.context.run_id}-0", self.tool_name, item) for item in self.results]

    def report_consistency(self):
        return True

    def plan_record(self):
        tasks = [
            {"task_id": (f"task-{self.context.run_id}" if index == 0 else f"task-{self.context.run_id}-{index}"), "task_type": "deep_research", "source_mode": self.mode, "status": "pending", "description": f"research-{index}"}
            for index in range(self.plan_task_count)
        ]
        return ({"plan_id": f"plan-{self.context.run_id}", "status": "executing", "tasks": tasks}, tasks)


class RetryOnceDriver(FakeDriver):
    attempts = 0

    async def execute(self):
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise AppError(ErrorCode.RETRIEVAL_TIMEOUT, "temporary timeout", retryable=True)
        await super().execute()


def build_runtime(database, tmp_path, factory, *, prefix_tracker=None):
    return HarnessRuntime(
        run_repository=RunRepository(database), message_repository=MessageRepository(database),
        event_repository=EventRepository(database), checkpoint_repository=CheckpointRepository(database),
        evidence_repository=EvidenceRepository(database), contract_repository=ContractRepository(database),
        trajectory_repository=PlanTaskToolRepository(database), workflow_factory=factory,
        artifact_store=ArtifactStore(tmp_path / "artifacts"), artifact_repository=ArtifactRepository(database),
        prefix_tracker=prefix_tracker,
    )


def test_state_machine_rejects_illegal_and_unverified_completion():
    machine = StateMachine()
    with pytest.raises(InvalidTransition):
        machine.validate(RunStatus.QUEUED, RunStatus.COMPLETED, contract_passed=True)
    with pytest.raises(InvalidTransition):
        machine.validate(RunStatus.VERIFYING, RunStatus.COMPLETED)
    machine.validate(RunStatus.VERIFYING, RunStatus.COMPLETED, contract_passed=True)


def test_budget_manager_enforces_tool_retry_and_replan_limits(monkeypatch):
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
    manager = BudgetManager(BudgetLimits(max_plan_tasks=1, max_llm_tokens=10))
    with pytest.raises(BudgetExceeded):
        manager.observe_plan_tasks(2)
    manager = BudgetManager(BudgetLimits(max_llm_tokens=10))
    with pytest.raises(BudgetExceeded):
        manager.observe_tokens(11)
    manager = BudgetManager(BudgetLimits(max_tavily_calls=1))
    manager.consume_tool(tavily=True)
    with pytest.raises(BudgetExceeded):
        manager.consume_tool(tavily=True)
    from deepresearch_agent.harness import budgets as budget_module
    ticks = iter((100.0, 102.0))
    monkeypatch.setattr(budget_module.time, "monotonic", lambda: next(ticks))
    manager = BudgetManager(BudgetLimits(wall_time_seconds=1))
    with pytest.raises(BudgetExceeded):
        manager.assert_available()


def test_token_usage_total_does_not_double_count_provider_total():
    assert HarnessRuntime._token_usage_total({"prompt_tokens": 60, "completion_tokens": 10, "total_tokens": 70}) == 70
    assert HarnessRuntime._token_usage_total({"prompt_tokens": 60, "completion_tokens": 10}) == 70


@pytest.mark.asyncio
async def test_web_deep_research_bypasses_answer_cache_and_replan_reexecutes():
    from deepresearch_agent.harness.workflow import DeepResearchDriver

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
    from deepresearch_agent.agents.base import BaseAgent
    from deepresearch_agent.agents.deep_research_agent import DeepResearchAgent

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
    result = await build_runtime(database, tmp_path, lambda ctx, events=None: FakeDriver(ctx)).execute_run(run.run_id)
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
    result = await build_runtime(database, tmp_path, lambda ctx, events=None: FakeDriver(ctx, dangling=True)).execute_run(run.run_id)
    assert result.status is RunStatus.COMPLETED
    events = await EventRepository(database).list_after(run.run_id)
    assert any(event.event_type == "run.retrying" for event in events)
    assert json.loads((await RunRepository(database).get(run.run_id)).usage_json)["usage"]["task_retries"] == 1


@pytest.mark.asyncio
async def test_cancel_stops_before_new_tool_call(database, tmp_path):
    FakeDriver.execute_calls = 0
    _, run = await create_run(database)
    assert await RunRepository(database).request_cancel(run.run_id)
    result = await build_runtime(database, tmp_path, lambda ctx, events=None: FakeDriver(ctx)).execute_run(run.run_id)
    assert result.status is RunStatus.CANCELLED
    assert FakeDriver.execute_calls == 0


@pytest.mark.asyncio
async def test_tool_budget_exhaustion_is_terminal_and_not_completed(database, tmp_path):
    _, run = await create_run(database, budget=BudgetLimits(max_tool_calls=1).model_dump())
    result = await build_runtime(database, tmp_path, lambda ctx, events=None: FakeDriver(ctx, tool_count=2)).execute_run(run.run_id)
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert not await ContractRepository(database).required_checks_passed(run.run_id)


@pytest.mark.asyncio
async def test_plan_task_and_reported_token_budgets_are_enforced(database, tmp_path):
    FakeDriver.execute_calls = 0
    _, plan_run = await create_run(database, budget=BudgetLimits(max_plan_tasks=1).model_dump())
    plan_result = await build_runtime(
        database, tmp_path, lambda ctx, events=None: FakeDriver(ctx, plan_task_count=2)
    ).execute_run(plan_run.run_id)
    assert plan_result.status is RunStatus.BUDGET_EXHAUSTED
    assert FakeDriver.execute_calls == 0
    assert (await RunRepository(database).get(plan_run.run_id)).error_message.startswith("预算耗尽: plan_tasks=")

    _, token_run = await create_run(database, budget=BudgetLimits(max_llm_tokens=10).model_dump())
    token_result = await build_runtime(
        database, tmp_path, lambda ctx, events=None: FakeDriver(ctx, token_count=11)
    ).execute_run(token_run.run_id)
    assert token_result.status is RunStatus.BUDGET_EXHAUSTED
    assert token_result.budget_usage.llm_tokens == 11
    assert (await RunRepository(database).get(token_run.run_id)).error_message.startswith("预算耗尽: llm_tokens=")

    class PrefixUsage:
        def run_snapshot(self, run_id):
            return {"totals": {"requests": 1, "input_tokens": 8, "output_tokens": 3, "hit_tokens": 4, "miss_tokens": 4}}

    _, tracked_run = await create_run(database, budget=BudgetLimits(max_llm_tokens=10).model_dump())
    tracked_result = await build_runtime(
        database, tmp_path, lambda ctx, events=None: FakeDriver(ctx), prefix_tracker=PrefixUsage(),
    ).execute_run(tracked_run.run_id)
    assert tracked_result.status is RunStatus.BUDGET_EXHAUSTED
    assert tracked_result.budget_usage.llm_tokens == 11


@pytest.mark.asyncio
async def test_retryable_transport_error_retries_same_execution_once(database, tmp_path):
    RetryOnceDriver.attempts = 0
    _, run = await create_run(database)
    result = await build_runtime(database, tmp_path, lambda ctx, events=None: RetryOnceDriver(ctx)).execute_run(run.run_id)
    assert result.status is RunStatus.COMPLETED
    assert RetryOnceDriver.attempts == 2
    events = await EventRepository(database).list_after(run.run_id)
    assert {"run.retrying", "run.retry_resumed"}.issubset({event.event_type for event in events})


@pytest.mark.asyncio
async def test_interrupted_executing_checkpoint_resumes_without_reexecuting_completed_batch(database, tmp_path):
    FakeDriver.execute_calls = 0
    FakeDriver.plan_calls = 0
    message, run = await create_run(database)
    context = RunContext(
        run_id=run.run_id, session_id=run.session_id, trigger_message_id=message.message_id,
        source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH,
        status=RunStatus.EXECUTING, original_query=message.content, budget_limits=BudgetLimits(),
        workflow_state={"executed": True, "results": []},
    )
    await CheckpointManager(CheckpointRepository(database)).save(context, "executing")
    await RunRepository(database).update_status(run.run_id, status="interrupted", current_stage="interrupted")
    result = await build_runtime(database, tmp_path, lambda ctx, events=None: FakeDriver(ctx)).execute_run(run.run_id)
    assert result.status is RunStatus.COMPLETED
    assert FakeDriver.execute_calls == 0
    assert FakeDriver.plan_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checkpoint_status", "workflow_state", "expected_plan", "expected_execute", "expected_report"),
    [
        (RunStatus.PLANNING, {}, 0, 1, 1),
        (RunStatus.REPORTING, {"executed": True, "report": "# 研究报告\n\n结论 [raw-1]\n\n## 方法\n\n基于证据。", "results": []}, 0, 0, 0),
    ],
)
async def test_interrupted_stage_resumes_after_latest_safe_checkpoint(
    database, tmp_path, checkpoint_status, workflow_state,
    expected_plan, expected_execute, expected_report,
):
    FakeDriver.plan_calls = 0
    FakeDriver.execute_calls = 0
    FakeDriver.report_calls = 0
    message, run = await create_run(database)
    context = RunContext(
        run_id=run.run_id, session_id=run.session_id, trigger_message_id=message.message_id,
        source_mode=SourceMode.GRAPHRAG, workflow_mode=WorkflowMode.DEEP_RESEARCH,
        status=checkpoint_status, original_query=message.content, budget_limits=BudgetLimits(),
        workflow_state=workflow_state,
        report=workflow_state.get("report"),
    )
    if checkpoint_status is RunStatus.REPORTING:
        prepared = FakeDriver(context)
        EvidenceLedger().assign(
            run_id=run.run_id, task_id=f"task-{run.run_id}",
            tool_call_id=f"call-{run.run_id}-0", provider="local_search",
            results=prepared.results,
        )
        context.workflow_state = prepared.snapshot()
        context.report = f"# 研究报告\n\n恢复后的结论已有证据支持 [{prepared.results[0].result_id}] [{prepared.results[1].result_id}]\n\n## 方法\n\n基于证据。"
        context.workflow_state["report"] = context.report
    await CheckpointManager(CheckpointRepository(database)).save(context, checkpoint_status.value)
    await RunRepository(database).update_status(run.run_id, status="interrupted", current_stage="interrupted")

    result = await build_runtime(database, tmp_path, lambda ctx, events=None: FakeDriver(ctx)).execute_run(run.run_id)

    assert result.status is RunStatus.COMPLETED
    assert FakeDriver.plan_calls == expected_plan
    assert FakeDriver.execute_calls == expected_execute
    assert FakeDriver.report_calls == expected_report
    resumed = [event for event in await EventRepository(database).list_after(run.run_id) if event.event_type == "run.resumed"]
    assert len(resumed) == 1


@pytest.mark.asyncio
async def test_startup_recovery_marks_active_run_without_lease_interrupted(database):
    from deepresearch_agent.harness.recovery import RecoveryManager

    _, run = await create_run(database)
    await RunRepository(database).update_status(run.run_id, status="planning", current_stage="planning")

    recoverable = await RecoveryManager(RunRepository(database)).scan(auto_resume=True)

    assert recoverable == [run.run_id]
    assert (await RunRepository(database).get(run.run_id)).status == "interrupted"
    events = await EventRepository(database).list_after(run.run_id)
    assert [event.event_type for event in events].count("run.interrupted") == 1


@pytest.mark.asyncio
async def test_final_assistant_message_is_idempotent(database, tmp_path):
    _, run = await create_run(database)
    runtime = build_runtime(database, tmp_path, lambda ctx, events=None: FakeDriver(ctx))
    result = await runtime.execute_run(run.run_id)
    assert result.status is RunStatus.COMPLETED
    message, created = await RunRepository(database).complete_verified(run.run_id, assistant_content=result.report, usage={})
    assert created is False and message.run_id == run.run_id
    messages = await MessageRepository(database).list_for_session(run.session_id)
    assert len([item for item in messages if item.role == "assistant"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source_mode", [SourceMode.GRAPHRAG, SourceMode.WEB])
@pytest.mark.parametrize("workflow_mode", [WorkflowMode.DEEP_RESEARCH, WorkflowMode.PLAN_EXECUTE_REPORT])
@pytest.mark.parametrize("sample", range(3))
async def test_each_workflow_source_combination_has_three_complete_harness_samples(
    database, tmp_path, source_mode, workflow_mode, sample,
):
    _, run = await create_run(
        database, source_mode=source_mode, workflow_mode=workflow_mode,
    )
    result = await build_runtime(
        database, tmp_path / f"{source_mode.value}-{workflow_mode.value}-{sample}",
        lambda ctx, events=None: FakeDriver(ctx),
    ).execute_run(run.run_id)

    assert result.status is RunStatus.COMPLETED
    evidence = await EvidenceRepository(database).list_for_run(run.run_id)
    assert len(evidence) == 2
    assert {item.source_mode for item in evidence} == {source_mode.value}
    events = await EventRepository(database).list_after(run.run_id)
    stages = {event.stage for event in events}
    assert {"context_building", "planning", "executing", "reporting", "verifying", "completed"}.issubset(stages)
    assert await ContractRepository(database).required_checks_passed(run.run_id)
