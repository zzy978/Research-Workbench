from types import SimpleNamespace

from deepresearch_agent.harness.research_quality import SearchProgress


def test_small_budget_leaves_room_for_search_and_report():
    from deepresearch_agent.harness.research_quality import search_ceiling, stage_reserves, search_budget, search_budget_exhausted
    assert 0 < search_ceiling(10000) < 10000 - sum(stage_reserves(10000))
    with search_budget('budget-test', 100, baseline=101):
        assert search_budget_exhausted()
    assert not search_budget_exhausted()


def test_recovered_budget_counts_new_process_usage(monkeypatch):
    from deepresearch_agent.harness.research_quality import search_budget, search_budget_exhausted
    from deepresearch_agent.models.prefix_cache import tracker
    used = [0]
    monkeypatch.setattr(tracker, 'run_snapshot', lambda _: {'totals': {'input_tokens': used[0]}})
    with search_budget('recovered', 100, baseline=80):
        assert not search_budget_exhausted()
        used[0] = 30
        assert search_budget_exhausted()


def test_deep_research_budget_stop_survives_checkpoint():
    from deepresearch_agent.harness.workflow import DeepResearchDriver
    from deepresearch_agent.harness.run_context import RunContext
    context = RunContext(run_id='r', session_id='s', trigger_message_id='m', source_mode='web',
                         workflow_mode='deep_research', original_query='q', status='executing')
    driver = DeepResearchDriver(context, SimpleNamespace())
    driver._search_stop_reason = 'report_budget_reserved'
    context.workflow_state = driver.snapshot()
    restored = DeepResearchDriver(context, SimpleNamespace())
    assert restored.delivery_status()['budget_limited'] is True


def test_search_progress_uses_content_not_generated_evidence_ids():
    progress = SearchProgress()
    def result(identifier):
        return SimpleNamespace(result_id=identifier, evidence='相同正文',
            metadata=SimpleNamespace(source_id='https://example.com/a',content_hash='same'))
    assert not progress.exhausted([result('first')])
    assert not progress.exhausted([result('second')])
    assert progress.exhausted([result('third')])


