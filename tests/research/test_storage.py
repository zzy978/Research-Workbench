import asyncio
import uuid

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import update

from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.harness.budgets import BudgetExceeded
from deepresearch_agent.persistence.database import Database
from deepresearch_agent.persistence.models import SessionModel, MessageModel, RunModel, EvidenceModel, ToolCallModel
from deepresearch_agent.research import ResearchSpec, ResearchStore, cell_fingerprint, spec_fingerprint


def spec(**changes):
    value = {"title": "比较", "questions": ["哪种更适合？"],
             "items": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
             "fields": [{"id": "cost", "label": "费用"}],
             "budget": {"max_search_calls": 5, "max_active_seconds": 60, "max_llm_tokens": 1000}}
    value.update(changes)
    return ResearchSpec.model_validate(value)


@pytest_asyncio.fixture
async def db(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'research.db').as_posix()}")
    await db.create_schema()
    async with db.transaction() as session:
        session.add(SessionModel(session_id="session", title="test", created_at="now", updated_at="now"))
    yield db
    await db.close()


async def run(db, status="completed"):
    rid = uuid.uuid4().hex
    async with db.transaction() as session:
        session.add(MessageModel(message_id=rid, session_id="session", role="user", content="test", created_at="now"))
        await session.flush()
        session.add(RunModel(run_id=rid, session_id="session", trigger_message_id=rid, source_mode="web", workflow_mode="deep_research", status=status, config_snapshot_json="{}", budget_json="{}", created_at="now", updated_at="now"))
    return rid


async def study(db, value=None):
    store = ResearchStore(db)
    rid = await run(db)
    view = await store.create("session", rid, value or spec())
    return store, view, rid


async def approved(db, value=None):
    store, view, _ = await study(db, value)
    view = await store.approve(view["study_id"], 1, view["fingerprint"])
    rid = await run(db)
    await store.link_run(view["study_id"], 1, rid)
    return store, view, rid


async def evidence(db, rid, eid="e1", source="https://a.example", hash="hash"):
    async with db.transaction() as session:
        session.add(EvidenceModel(evidence_id=eid, run_id=rid, source_mode="web", provider="test", source_id=source, summary="observed", content_hash=hash, created_at="now"))
    return {"evidence_id": eid, "content_hash": hash, "locator": source, "access": "public"}


def test_cell_fingerprint_only_invalidates_relevant_scope():
    a = spec()
    b = a.model_dump()
    b["budget"]["max_search_calls"] = 20
    b["sections"] = ["总结"]
    b["items"][1]["version"] = "v2"
    assert spec_fingerprint(a) != spec_fingerprint(b)
    assert cell_fingerprint(a, "a", "cost") == cell_fingerprint(b, "a", "cost")
    assert cell_fingerprint(a, "b", "cost") != cell_fingerprint(b, "b", "cost")
    b["scope"]["time_range"] = "2026"
    assert cell_fingerprint(a, "a", "cost") != cell_fingerprint(b, "a", "cost")


def test_schema_rejects_duplicate_ids_unknown_applicability_and_empty_questions():
    for changes in ({"questions": []}, {"items": [{"id": "a", "name": "A"}, {"id": "a", "name": "B"}]}, {"fields": [{"id": "x", "label": "X", "applies_to": ["unknown"]}]}):
        with pytest.raises(ValidationError):
            spec(**changes)


def test_allowed_domains_are_canonical_hostnames_and_affect_cell_scope():
    original = spec()
    restricted = spec(allowed_domains=["Docs.Example.org", "example.org", "docs.example.org"])
    assert restricted.allowed_domains == ["docs.example.org", "example.org"]
    assert cell_fingerprint(original, "a", "cost") != cell_fingerprint(restricted, "a", "cost")
    reordered = spec(allowed_domains=["example.org", "docs.example.org"])
    assert spec_fingerprint(restricted) == spec_fingerprint(reordered)


@pytest.mark.parametrize("domain", ["https://example.org", "example.org/path", "example.org:443", "*.example.org", "example.org?x=1", "user@example.org", "bad..example.org", "-bad.example.org", "bad_.example.org", ""])
def test_allowed_domains_reject_non_hostname_input(domain):
    with pytest.raises(ValidationError):
        spec(allowed_domains=[domain])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["supported", "inference", "conflict"])
async def test_cells_reject_evidence_outside_approved_domain_list(db, status):
    store, view, rid = await approved(db, spec(allowed_domains=["official.example"]))
    good = await evidence(db, rid, "good", source="https://docs.official.example/page")
    evil = await evidence(db, rid, "evil", source="https://official.example.attacker.test/page")
    evil["locator"] = "https://official.example/fake-locator"
    evil["source_id"] = "https://official.example/fake-source"
    citations = [evil] if status != "conflict" else [good, evil]
    with pytest.raises(AppError) as error:
        await store.save_cell(view["study_id"], 1, rid, {"item_id": "a", "field_id": "cost", "status": status, "value": "claim", "citations": citations})
    assert error.value.code == ErrorCode.CONFLICT
    assert (await store.matrix(view["study_id"]))["counts"]["missing"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["https://official.example/page", "https://docs.official.example/page", "https://DOCS.OFFICIAL.EXAMPLE/page"])
async def test_cells_allow_exact_approved_hostname_and_its_subdomains(db, source):
    store, view, rid = await approved(db, spec(allowed_domains=["official.example"]))
    citation = await evidence(db, rid, source=source)
    await store.save_cell(view["study_id"], 1, rid, {"item_id": "a", "field_id": "cost", "status": "supported", "value": 0, "citations": [citation]})
    assert (await store.matrix(view["study_id"]))["counts"]["current"] == 1


@pytest.mark.asyncio
async def test_snippet_cannot_fulfil_field_requiring_full_text(db):
    store, view, rid = await approved(db, spec(fields=[{'id': 'cost', 'label': '费用', 'required_access': 'full_text'}]))
    citation = await evidence(db, rid)
    citation['access'] = 'full_text'  # Caller-authored access cannot override the ledger.
    with pytest.raises(AppError, match='正文'):
        await store.save_cell(view['study_id'], 1, rid, {'item_id': 'a', 'field_id': 'cost',
            'status': 'supported', 'value': 0, 'citations': [citation]})


@pytest.mark.asyncio
async def test_approval_is_cas_and_revision_rejects_late_writes(db):
    store, view, rid = await approved(db)
    sid = view["study_id"]
    assert (await store.approve(sid, 1, view["fingerprint"]))["approved_revision"] == 1
    revised = await store.revise(sid, 1, view["fingerprint"], spec(title="new"))
    assert revised["current_revision"] == 2
    assert revised["approved_revision"] is None
    for action in (store.approve(sid, 1, view["fingerprint"]), store.reserve_request(rid)):
        with pytest.raises(AppError) as exc:
            await action
        assert exc.value.code == ErrorCode.CONFLICT


@pytest.mark.asyncio
async def test_discovery_and_atomic_shared_budget_survive_reopen(db):
    store, view, outline = await study(db)
    with pytest.raises(AppError):
        await store.reserve_request(outline)
    for _ in range(3):
        await store.reserve_request(outline, phase="discovery")
    with pytest.raises(BudgetExceeded):
        await store.reserve_request(outline, phase="discovery")
    await store.approve(view["study_id"], 1, view["fingerprint"])
    rid = await run(db)
    await store.link_run(view["study_id"], 1, rid)
    # Separate Store instances intentionally exercise the database lock.
    results = await asyncio.gather(*(ResearchStore(db).reserve_request(rid) for _ in range(6)), return_exceptions=True)
    assert sum(not isinstance(v, Exception) for v in results) == 2
    assert sum(isinstance(v, BudgetExceeded) for v in results) == 4
    assert (await ResearchStore(db).get(view["study_id"]))["usage"]["external_calls"] == 5


@pytest.mark.asyncio
async def test_usage_is_cumulative_per_run_and_shared_across_runs(db):
    store, view, rid = await approved(db)
    await store.record_usage(rid, 30, 10)
    await store.record_usage(rid, 30, 10)
    await store.record_usage(rid, 20, 9)
    other = await run(db)
    await store.link_run(view["study_id"], 1, other, purpose="followup", targets=[{"item_id": "a", "field_id": "cost"}])
    await store.record_usage(other, 70, 4)
    usage = (await store.get(view["study_id"]))["usage"]
    assert usage["llm_tokens"] == 100
    assert usage["active_seconds"] == 14


@pytest.mark.asyncio
async def test_run_usage_returns_only_persisted_run_totals_with_zero_defaults(db):
    store, view, rid = await study(db)
    assert await store.run_usage(rid) == {"external_calls": 0, "discovery_calls": 0, "llm_tokens": 0, "active_seconds": 0}
    await store.reserve_request(rid, phase="discovery")
    await store.record_usage(rid, 15, 2.5)
    await store.approve(view["study_id"], 1, view["fingerprint"])
    second = await run(db)
    await store.link_run(view["study_id"], 1, second)
    await store.record_usage(second, 40, 7)
    reopened = ResearchStore(db)
    assert await reopened.run_usage(rid) == {"external_calls": 1, "discovery_calls": 1, "llm_tokens": 15, "active_seconds": 2.5}
    assert await reopened.run_usage(second) == {"external_calls": 0, "discovery_calls": 0, "llm_tokens": 40, "active_seconds": 7}
    with pytest.raises(AppError) as error:
        await store.run_usage("missing")
    assert error.value.code == ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_evidence_cannot_cross_studies_and_hashes_are_rechecked(db):
    store, view, rid = await approved(db)
    _, other, foreign = await approved(db)
    citation = await evidence(db, foreign)
    cell = {"item_id": "a", "field_id": "cost", "status": "supported", "value": 0, "citations": [citation]}
    with pytest.raises(AppError):
        await store.save_cell(view["study_id"], 1, rid, cell)
    cell["citations"] = [await evidence(db, rid, "e2")]
    await store.save_cell(view["study_id"], 1, rid, cell)
    assert (await store.matrix(view["study_id"]))["cells"][0]["value"] == 0
    async with db.transaction() as session:
        await session.execute(update(EvidenceModel).where(EvidenceModel.evidence_id == "e2").values(content_hash="changed"))
    assert (await store.matrix(view["study_id"]))["counts"]["stale"] == 1


@pytest.mark.asyncio
async def test_incremental_revision_reuses_only_unchanged_cells(db):
    store, view, rid = await approved(db)
    citation = await evidence(db, rid)
    for item in ("a", "b"):
        await store.save_cell(view["study_id"], 1, rid, {"item_id": item, "field_id": "cost", "status": "supported", "value": False, "citations": [citation]})
    changed = spec().model_dump()
    changed["items"][1]["version"] = "v2"
    revised = await store.revise(view["study_id"], 1, view["fingerprint"], changed)
    await store.approve(view["study_id"], 2, revised["fingerprint"])
    next_run = await run(db)
    await store.link_run(view["study_id"], 2, next_run)
    matrix = await store.matrix(view["study_id"])
    assert matrix["counts"] == {"expected": 2, "current": 1, "missing": 0, "stale": 1, "unknown": 0}
    assert matrix["cells"][0]["origin_run_id"] == rid
    assert matrix["cells"][0]["run_id"] == next_run


@pytest.mark.asyncio
async def test_report_completion_acceptance_and_evidence_freshness(db):
    store, view, rid = await approved(db, spec(items=[{"id": "a", "name": "A"}]))
    sid = view["study_id"]
    with pytest.raises(AppError):
        await store.set_report(sid, 1, rid, "未提供证据", True)
    citation = await evidence(db, rid)
    await store.save_cell(sid, 1, rid, {"item_id": "a", "field_id": "cost", "status": "supported", "value": 0, "citations": [citation]})
    report_view = await store.set_report(sid, 1, rid, "费用为 0 [e1]。", True)
    assert report_view["acceptance"] is None
    accepted = await store.accept(sid, 1, view["fingerprint"], report_view["report"]["fingerprint"])
    assert accepted["status"] == "complete"
    async with db.transaction() as session:
        await session.execute(update(EvidenceModel).where(EvidenceModel.evidence_id == "e1").values(invalidated_at="now"))
    with pytest.raises(AppError):
        await store.accept(sid, 1, view["fingerprint"], report_view["report"]["fingerprint"])


@pytest.mark.asyncio
async def test_invalid_unknown_and_conflicting_cells_rejected(db):
    store, view, rid = await approved(db)
    for cell in ({"status": "not_found", "reason": "none"}, {"status": "supported", "value": "yes"}, {"status": "conflict", "value": "disagree"}):
        with pytest.raises(AppError):
            await store.save_cell(view["study_id"], 1, rid, {"item_id": "a", "field_id": "cost", **cell})
    await store.save_cell(view["study_id"], 1, rid, {"item_id": "a", "field_id": "cost", "status": "not_found", "reason": "公开资料未报告", "search_log": [{"query": "A cost", "result": "none"}]})
    assert (await store.matrix(view["study_id"]))["counts"]["unknown"] == 1


@pytest.mark.asyncio
async def test_live_run_blocks_scope_change_and_acceptance(db):
    store, view, rid = await approved(db)
    async with db.transaction() as session:
        await session.execute(update(RunModel).where(RunModel.run_id == rid).values(status="executing"))
    with pytest.raises(AppError):
        await store.revise(view["study_id"], 1, view["fingerprint"], spec(title="new"))


@pytest.mark.asyncio
async def test_outline_run_can_promote_once_without_losing_usage(db):
    store, view, rid = await study(db)
    await store.reserve_request(rid, phase="discovery")
    revised = await store.revise(view["study_id"], 1, view["fingerprint"], spec(title="model outline"))
    await store.link_run(view["study_id"], 2, rid, purpose="outline")
    await store.approve(view["study_id"], 2, revised["fingerprint"])
    await store.link_run(view["study_id"], 2, rid, purpose="research")
    assert (await store.reserve_request(rid))["usage"]["external_calls"] == 2
    next_view = await store.revise(view["study_id"], 2, revised["fingerprint"], spec(title="later"))
    await store.approve(view["study_id"], 3, next_view["fingerprint"])
    with pytest.raises(AppError):
        await store.link_run(view["study_id"], 3, rid)


@pytest.mark.asyncio
async def test_followup_invalidates_selected_cell_report_and_acceptance(db):
    store, view, rid = await approved(db, spec(items=[{"id": "a", "name": "A"}]))
    sid = view["study_id"]
    citation = await evidence(db, rid)
    cell = {"item_id": "a", "field_id": "cost", "status": "supported", "value": 0, "citations": [citation]}
    await store.save_cell(sid, 1, rid, cell)
    report = (await store.set_report(sid, 1, rid, "有证据的报告", True))["report"]
    await store.accept(sid, 1, view["fingerprint"], report["fingerprint"])
    next_run = await run(db)
    await store.link_run(sid, 1, next_run, purpose="followup", targets=[{"item_id": "a", "field_id": "cost"}])
    current = await store.get(sid)
    assert current["acceptance"] is None
    assert current["report"] is None
    assert (await store.matrix(sid))["cells"][0]["status"] == "stale"
    await store.save_cell(sid, 1, next_run, cell)
    assert (await store.matrix(sid))["cells"][0]["status"] == "supported"


@pytest.mark.asyncio
async def test_report_cannot_be_accepted_while_run_is_live_or_after_cell_changes(db):
    store, view, rid = await approved(db, spec(items=[{"id": "a", "name": "A"}]))
    sid = view["study_id"]
    cell = {"item_id": "a", "field_id": "cost", "status": "supported", "value": 0, "citations": [await evidence(db, rid)]}
    await store.save_cell(sid, 1, rid, cell)
    report = (await store.set_report(sid, 1, rid, "报告", True))["report"]
    async with db.transaction() as session:
        await session.execute(update(RunModel).where(RunModel.run_id == rid).values(status="executing"))
    with pytest.raises(AppError):
        await store.accept(sid, 1, view["fingerprint"], report["fingerprint"])
    async with db.transaction() as session:
        await session.execute(update(RunModel).where(RunModel.run_id == rid).values(status="completed"))
    await store.save_cell(sid, 1, rid, {**cell, "value": 10})
    with pytest.raises(AppError):
        await store.accept(sid, 1, view["fingerprint"], report["fingerprint"])


@pytest.mark.asyncio
async def test_question_mode_and_applicability(db):
    store, view, rid = await approved(db, spec(items=[], fields=[{"id": "cost", "label": "费用", "applies_to": ["q1"]}], questions=["问题一", "问题二"]))
    matrix = await store.matrix(view["study_id"])
    assert matrix["view_mode"] == "questions"
    assert [c["status"] for c in matrix["cells"]] == ["missing", "not_applicable"]
    with pytest.raises(AppError):
        await store.save_cell(view["study_id"], 1, rid, {"item_id": "q1", "field_id": "cost", "status": "not_applicable"})


@pytest.mark.asyncio
async def test_usage_limits_stop_further_external_attempts(db):
    store, view, rid = await approved(db)
    await store.record_usage(rid, 1000, 0)
    with pytest.raises(BudgetExceeded):
        await store.reserve_request(rid)
    assert (await store.get(view["study_id"]))["usage"]["external_calls"] == 0


@pytest.mark.asyncio
async def test_study_reopens_and_keeps_real_foreign_keys(db):
    store, view, rid = await approved(db)
    reopened = Database(db.url)
    try:
        assert (await ResearchStore(reopened).for_run(rid))["fingerprint"] == view["fingerprint"]
        assert await reopened.foreign_key_violations() == []
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_prepared_tool_intent_is_not_a_completed_search_attempt(db):
    store, view, rid = await approved(db)
    async with db.transaction() as session:
        session.add(ToolCallModel(tool_call_id="intent", run_id=rid, tool_name="search", source_mode="web", status="prepared", args_json="{}", created_at="now"))
    cell = {"item_id": "a", "field_id": "cost", "status": "not_found", "reason": "未找到"}
    with pytest.raises(AppError):
        await store.save_cell(view["study_id"], 1, rid, cell)
    async with db.transaction() as session:
        await session.execute(update(ToolCallModel).where(ToolCallModel.tool_call_id == "intent").values(status="completed"))
    await store.save_cell(view["study_id"], 1, rid, cell)


@pytest.mark.asyncio
async def test_old_run_cannot_finalize_report_after_new_followup(db):
    store, view, rid = await approved(db)
    next_run = await run(db)
    await store.link_run(view["study_id"], 1, next_run, purpose="followup", targets=[{"item_id": "a", "field_id": "cost"}])
    with pytest.raises(AppError):
        await store.set_report(view["study_id"], 1, rid, "旧运行延迟报告", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["assert_allowed", "reserve_request", "save_cell"])
async def test_superseded_paused_run_cannot_execute_but_its_evidence_can_be_reused(db, entrypoint):
    store, view, rid = await approved(db)
    sid = view["study_id"]
    citation = await evidence(db, rid)
    cell = {"item_id": "a", "field_id": "cost", "status": "supported", "value": 0, "citations": [citation]}
    await store.save_cell(sid, 1, rid, cell)
    await store.save_cell(sid, 1, rid, {**cell, "item_id": "b"})
    async with db.transaction() as session:
        await session.execute(update(RunModel).where(RunModel.run_id == rid).values(status="paused"))
    assert (await store.assert_allowed(rid))["run_id"] == rid
    next_run = await run(db)
    await store.link_run(sid, 1, next_run, purpose="followup", targets=[{"item_id": "a", "field_id": "cost"}])

    with pytest.raises(AppError) as error:
        if entrypoint == "save_cell":
            await store.save_cell(sid, 1, rid, {**cell, "value": 100})
        else:
            await getattr(store, entrypoint)(rid)
    assert error.value.code == ErrorCode.CONFLICT
    assert (await store.run_usage(rid))["external_calls"] == 0
    assert (await store.assert_allowed(next_run))["run_id"] == next_run
    matrix = await store.matrix(sid)
    assert [c["status"] for c in matrix["cells"]] == ["stale", "supported"]
    assert matrix["cells"][1]["origin_run_id"] == rid
    assert matrix["cells"][0]["value"] == 0
    await store.save_cell(sid, 1, next_run, cell)
    assert (await store.matrix(sid))["counts"]["current"] == 2


@pytest.mark.asyncio
async def test_late_old_run_active_usage_is_charged_and_blocks_latest_run_at_limit(db):
    store, view, rid = await approved(db)
    await store.record_usage(rid, 5, 40)
    next_run = await run(db)
    await store.link_run(view["study_id"], 1, next_run, purpose="followup", targets=[{"item_id": "a", "field_id": "cost"}])
    await store.record_usage(next_run, 5, 10)
    await store.reserve_request(next_run)
    # In-flight work may report after its Run has been superseded. Its real
    # usage must still count, while lower/repeated snapshots cannot refund it.
    await store.record_usage(rid, 10, 50)
    await store.record_usage(rid, 8, 45)
    await store.record_usage(rid, 10, 50)
    with pytest.raises(BudgetExceeded) as error:
        await store.reserve_request(next_run)
    assert error.value.metric == "active_seconds"
    assert (await store.get(view["study_id"]))["usage"] == {"external_calls": 1, "discovery_calls": 0, "llm_tokens": 15, "active_seconds": 60}


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
@pytest.mark.parametrize("edit", ["budget", "sections"])
async def test_unresolved_followup_stays_stale_across_equivalent_cell_revisions(db, terminal_status, edit):
    store, view, rid = await approved(db, spec(items=[{"id": "a", "name": "A"}]))
    sid = view["study_id"]
    cell = {"item_id": "a", "field_id": "cost", "status": "supported", "value": 0, "citations": [await evidence(db, rid)]}
    await store.save_cell(sid, 1, rid, cell)
    report = (await store.set_report(sid, 1, rid, "初次报告", True))["report"]
    await store.accept(sid, 1, view["fingerprint"], report["fingerprint"])
    followup = await run(db, status=terminal_status)
    await store.link_run(sid, 1, followup, purpose="followup", targets=[{"item_id": "a", "field_id": "cost"}])
    assert (await store.matrix(sid))["counts"]["stale"] == 1

    changed = view["spec"]
    if edit == "budget":
        changed["budget"]["max_search_calls"] += 10
    else:
        changed["sections"] = ["费用", "结论"]
    revised = await store.revise(sid, 1, view["fingerprint"], changed)
    await store.approve(sid, 2, revised["fingerprint"])
    next_run = await run(db)
    await store.link_run(sid, 2, next_run)
    matrix = await store.matrix(sid)
    assert matrix["counts"]["stale"] == 1
    assert matrix["counts"]["current"] == 0
    with pytest.raises(AppError):
        await store.set_report(sid, 2, next_run, "未经补查的报告", True)
    partial = (await store.set_report(sid, 2, next_run, "尚待完成补查", False))["report"]
    with pytest.raises(AppError):
        await store.accept(sid, 2, revised["fingerprint"], partial["fingerprint"])

    await store.save_cell(sid, 2, next_run, cell)
    assert (await store.matrix(sid))["counts"]["current"] == 1
    complete = (await store.set_report(sid, 2, next_run, "已补查报告", True))["report"]
    assert (await store.accept(sid, 2, revised["fingerprint"], complete["fingerprint"]))["status"] == "complete"
    changed["sections"] = ["最新报告结构"]
    await store.revise(sid, 2, revised["fingerprint"], changed)
    assert (await store.matrix(sid))["counts"]["current"] == 1


@pytest.mark.asyncio
async def test_followup_invalidation_only_applies_to_its_cell_fingerprint(db):
    initial = spec(items=[{"id": "a", "name": "A", "version": "v1"}])
    store, view, rid = await approved(db, initial)
    sid = view["study_id"]
    await store.save_cell(sid, 1, rid, {"item_id": "a", "field_id": "cost", "status": "supported", "value": 0, "citations": [await evidence(db, rid)]})
    changed = initial.model_dump()
    changed["items"][0]["version"] = "v2"
    revised = await store.revise(sid, 1, view["fingerprint"], changed)
    await store.approve(sid, 2, revised["fingerprint"])
    followup = await run(db, status="failed")
    await store.link_run(sid, 2, followup, purpose="followup", targets=[{"item_id": "a", "field_id": "cost"}])
    await store.revise(sid, 2, revised["fingerprint"], initial)
    matrix = await store.matrix(sid)
    assert matrix["counts"]["current"] == 1
    assert matrix["cells"][0]["origin_run_id"] == rid
