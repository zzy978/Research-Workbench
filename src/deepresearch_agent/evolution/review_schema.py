"""Typed contracts for durable trajectory review and two-agent learning."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DecisionCard(BaseModel):
    state_before: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)
    observation: dict[str, Any] = Field(default_factory=dict)
    outcome: str
    cost: dict[str, Any] = Field(default_factory=dict)
    trace_refs: list[str] = Field(default_factory=list)


class FailureCard(BaseModel):
    failure_type: str
    root_cause_evidence: list[str] = Field(default_factory=list)
    failed_actions: list[str] = Field(default_factory=list)
    recovery_actions: list[str] = Field(default_factory=list)
    contract_before: dict[str, Any] = Field(default_factory=dict)
    contract_after: dict[str, Any] = Field(default_factory=dict)
    verified_recovery: bool = False


class TrajectoryEpisode(BaseModel):
    episode_type: Literal["success", "recovery", "inefficiency", "failure"]
    summary: str
    cards: list[DecisionCard] = Field(default_factory=list)
    reusable_scope: dict[str, Any] = Field(default_factory=dict)
    trace_refs: list[str] = Field(default_factory=list)


class ReviewPack(BaseModel):
    review_id: str
    run_id: str
    user_goal: str
    workflow_mode: str
    source_mode: str
    terminal_status: str
    completion_contract: dict[str, Any] = Field(default_factory=dict)
    context_and_budget_metrics: dict[str, Any] = Field(default_factory=dict)
    loaded_skills: list[dict[str, Any]] = Field(default_factory=list)
    episodes: list[TrajectoryEpisode] = Field(default_factory=list)
    user_corrections: list[dict[str, Any]] = Field(default_factory=list)
    candidate_neighbors: list[dict[str, Any]] = Field(default_factory=list)
    protected_skills: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)


class SkillProposal(BaseModel):
    decision: Literal["create", "patch", "ignore"]
    target_skill_id: str | None = None
    base_version: str | None = None
    base_content_hash: str | None = None
    title: str = ""
    rationale: str = ""
    name: str | None = None
    proposed_version: str | None = None
    description: str = ""
    applicability: dict[str, Any] = Field(default_factory=dict)
    triggers: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    anti_patterns: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    fallback: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    machine_policy: dict[str, Any] = Field(default_factory=dict)
    support_files: list[dict[str, Any]] = Field(default_factory=list)
    trace_refs: list[str] = Field(default_factory=list)


class CriticReview(BaseModel):
    decision: Literal["pass", "revise", "reject"]
    scores: dict[str, float] = Field(default_factory=dict)
    blocking_issues: list[str] = Field(default_factory=list)
    revision_instructions: list[str] = Field(default_factory=list)
    unsupported_rule_refs: list[str] = Field(default_factory=list)
    conflict_refs: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    passed: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
