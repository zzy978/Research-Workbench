from types import SimpleNamespace

import pytest

from deepresearch_agent.research.workflow import ResearchWorkflow


@pytest.mark.asyncio
async def test_explicit_followup_does_not_schedule_other_missing_cells():
    class Store:
        async def assert_allowed(self, run_id):
            return {'study_id': 'study'}

        async def matrix(self, study_id):
            return {'items': [{'id': 'a'}, {'id': 'b'}], 'fields': [{'id': 'f'}],
                    'cells': [{'item_id': i, 'field_id': 'f', 'status': 'missing'} for i in ['a', 'b']]}

    context = SimpleNamespace(run_id='run', workflow_state={},
        config_snapshot={'research_targets': [{'item_id': 'b', 'field_id': 'f'}]})
    driver = ResearchWorkflow(context, SimpleNamespace(store=Store(), database=None), None, None)
    await driver.plan()
    assert [(u['item']['id'], [f['id'] for f in u['fields']]) for u in driver.units] == [('b', ['f'])]
