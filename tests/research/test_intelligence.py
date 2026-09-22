import pytest

from deepresearch_agent.research.intelligence import ResearchModel


@pytest.mark.asyncio
async def test_summary_uses_host_resolved_short_references(monkeypatch):
    model = ResearchModel()
    responses = iter([{'summary': '错误引用 [C99]'}, {'summary': '支持恢复 [C1]'}])
    prompts = []

    async def respond(prompt):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(model, '_json', respond)
    text = await model.synthesize({'questions': ['恢复能力'], 'hard_constraints': []}, [
        {'status': 'supported', 'value': True, 'citations': [{'evidence_id': 'ev_actual'}]},
    ])
    assert text == '支持恢复 [ev_actual]'
    assert len(prompts) == 2
    assert 'ev_actual' not in prompts[0]


@pytest.mark.asyncio
async def test_summary_refuses_repeated_unknown_reference(monkeypatch):
    model = ResearchModel()

    async def respond(prompt):
        return {'summary': '不存在的结论 [ev_invented]'}

    monkeypatch.setattr(model, '_json', respond)
    with pytest.raises(ValueError, match='不存在'):
        await model.synthesize({'questions': ['Q'], 'hard_constraints': []}, [])


@pytest.mark.asyncio
async def test_summary_without_references_cannot_present_supported_claims(monkeypatch):
    model = ResearchModel()

    async def respond(prompt):
        return {'summary': '强结论，没有来源。'}

    monkeypatch.setattr(model, '_json', respond)
    with pytest.raises(ValueError):
        await model.synthesize({'questions': ['Q'], 'hard_constraints': []}, [
            {'status': 'supported', 'value': True, 'citations': [{'evidence_id': 'ev_real'}]},
        ])
