import json

import pytest
from pydantic import ValidationError

from deepresearch_agent.agents.multi_agent.core.plan_spec import TaskGraph, TaskNode
from deepresearch_agent.agents.multi_agent.planner.plan_reviewer import (
    PlanReviewer,
    PlanValidationResult,
)


@pytest.mark.parametrize("field", ["issues", "suggestions"])
def test_review_preserves_object_feedback_and_raw_response(field):
    entries = [
        "原有文字意见",
        {"task_id": "task_001", "issue": "缺少用户的未知条件"},
        {"task_id": "task_003", "description": "补充风险/失败条件"},
        {"task_id": "task_004", "suggestion": "确保两者交汇"},
        {"task_graph": "global", "details": {"missing": ["用户每项要求"]}},
    ]
    response = json.dumps({
        "problem_statement": {"original_query": "该怎么谈恋爱"},
        "validation_results": {"is_valid": False, field: entries},
    }, ensure_ascii=False)

    class ReplyLLM:
        def bind(self, **kwargs):
            return self

        def invoke(self, prompt):
            return type("Message", (), {"content": response})()

    outcome = PlanReviewer(llm=ReplyLLM()).review(
        original_query="该怎么谈恋爱", refined_query=None, assumptions=[],
        task_graph=TaskGraph(nodes=[TaskNode(
            task_id="task_001", task_type="web_search", description="检索关系建议",
        )]), source_mode="web",
    )
    normalized = getattr(outcome.validation, field)
    assert normalized[0] == entries[0]
    assert [json.loads(item) for item in normalized[1:]] == entries[1:]
    assert "未知条件" in normalized[1]
    assert outcome.validation.raw_response == response
    assert outcome.validation.is_valid is False
    assert outcome.plan_spec.task_graph.nodes[0].source_mode == "web"
    assert getattr(PlanValidationResult.model_validate(outcome.validation.model_dump()), field) == normalized


@pytest.mark.parametrize("field", ["issues", "suggestions"])
def test_string_feedback_remains_unchanged(field):
    entries = ["task_001：补充条件", "全局：补充验收要求"]
    assert getattr(PlanValidationResult(**{field: entries}), field) == entries
    assert getattr(PlanValidationResult(), field) == []


@pytest.mark.parametrize("field", ["issues", "suggestions"])
@pytest.mark.parametrize("value", [None, "文字", {"issue": "文字"}, [None], [42], [True], [[]], [{}]])
def test_invalid_feedback_is_not_silently_accepted(field, value):
    with pytest.raises(ValidationError):
        PlanValidationResult(**{field: value})
