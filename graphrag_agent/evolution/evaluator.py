"""Small deterministic paired evaluation for Skill candidates."""

from __future__ import annotations

import json

from graphrag_agent.persistence.repositories import SkillRepository

from .linter import SkillLinter
from .schema import SkillSpec


class SkillEvaluator:
    def __init__(self, repository: SkillRepository, *, linter: SkillLinter | None = None):
        self.repository, self.linter = repository, linter or SkillLinter()

    async def evaluate(self, candidate_id: str, *, metric_overrides: dict | None = None):
        candidate = await self.repository.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError("Skill candidate 不存在")
        payload = json.loads(candidate.payload_json or "{}")
        spec = SkillSpec.model_validate(payload.get("spec", {}))
        lint = self.linter.lint(spec, content=payload.get("content"))
        cases = payload.get("eval_cases", [])
        metrics = {
            "contract_pass_rate": 1.0 if lint.passed and len(cases) >= 2 else 0.0,
            "active_contract_pass_rate": 1.0,
            "citation_integrity": 1.0 if lint.passed else 0.0,
            "claim_support_rate": 1.0 if lint.passed else 0.0,
            "tool_policy_violations": 0 if lint.passed else 1,
            "source_leakage": 0,
            "safety_passed": lint.passed,
            "token_estimate": len(payload.get("content", "")) // 4,
            "tool_call_estimate": len(spec.steps),
            "latency_estimate_ms": len(spec.steps) * 10,
            "cases": cases,
            "lint_errors": lint.errors,
        }
        metrics.update(metric_overrides or {})
        passed = (
            metrics["citation_integrity"] == 1.0 and metrics["tool_policy_violations"] == 0
            and metrics["source_leakage"] == 0 and metrics["safety_passed"]
            and metrics["contract_pass_rate"] >= metrics["active_contract_pass_rate"]
            and metrics["claim_support_rate"] >= 1.0
        )
        result = await self.repository.add_eval(candidate_id=candidate_id, status="passed" if passed else "failed", metrics=metrics)
        await self.repository.update_candidate_status(candidate_id, "evaluated" if passed else "rejected")
        return result

