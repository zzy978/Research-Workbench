"""HTTP DTOs for the phase-4 FastAPI boundary."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from graphrag_agent.harness.contracts import SourceMode, WorkflowMode


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorBody(ApiModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    error: ErrorBody
    request_id: str


class SessionPatch(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "archived"] | None = None


class MessageSend(ApiModel):
    client_message_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=20000)
    source_mode: SourceMode
    workflow_mode: WorkflowMode = WorkflowMode.DEEP_RESEARCH
    report_type: Literal["brief", "long_document"] = "brief"


class ClarificationSubmit(ApiModel):
    content: str = Field(min_length=1, max_length=10000)


class RunAccepted(ApiModel):
    message_id: str
    run_id: str
    status: str
    events_url: str
    created: bool


class RunControl(ApiModel):
    run_id: str
    status: str


class MemoryLifecycleUpdate(ApiModel):
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    status: Literal["candidate", "active", "rejected", "expired"] | None = None


class SkillActionResponse(ApiModel):
    accepted: bool
    reason: str

