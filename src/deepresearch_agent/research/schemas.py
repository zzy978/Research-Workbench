"""Validated research scope and deterministic, dependency-aware fingerprints."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SpecPart(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResearchItem(SpecPart):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$", max_length=80)
    name: str = Field(min_length=1)
    version: str = ""
    rationale: str = ""


class ResearchField(SpecPart):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$", max_length=80)
    label: str = Field(min_length=1)
    description: str = ""
    evidence_requirement: str = ""
    required_access: Literal['snippet', 'full_text'] = 'snippet'
    applies_to: list[str] = Field(default_factory=list)


class ResearchScope(SpecPart):
    time_range: str = ""
    inclusion: list[str] = Field(default_factory=list)
    exclusion: list[str] = Field(default_factory=list)


class ResearchBudget(SpecPart):
    max_search_calls: int = Field(default=20, ge=0, strict=True)
    max_active_seconds: int = Field(default=1800, ge=1, strict=True)
    max_llm_tokens: int = Field(default=200000, ge=1, strict=True)


class ResearchSpec(SpecPart):
    title: str = ""
    questions: list[str] = Field(min_length=1)
    hard_constraints: list[str] = Field(default_factory=list)
    items: list[ResearchItem] = Field(default_factory=list)
    fields: list[ResearchField] = Field(default_factory=lambda: [ResearchField(id="evidence", label="证据与结论")], min_length=1)
    scope: ResearchScope = Field(default_factory=ResearchScope)
    source_policy: str = ""
    allowed_domains: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)
    budget: ResearchBudget = Field(default_factory=ResearchBudget)
    stop_conditions: list[str] = Field(default_factory=list)

    @field_validator("allowed_domains")
    @classmethod
    def validate_allowed_domains(cls, domains):
        normalized = []
        for domain in domains:
            hostname = domain.strip().lower()
            labels = hostname.split(".")
            if len(hostname) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels
            ):
                raise ValueError("允许的来源必须是合法主机名，不能包含协议、路径、端口或通配符")
            normalized.append(hostname)
        return sorted(set(normalized))

    @model_validator(mode="after")
    def validate_scope(self):
        self.questions = [q.strip() for q in self.questions]
        if any(not q for q in self.questions):
            raise ValueError("研究问题不能为空")
        for entries in (self.items, self.fields):
            if len({entry.id for entry in entries}) != len(entries):
                raise ValueError("对象和字段 ID 必须各自唯一")
        item_ids = {item["id"] for item in effective_items(self)}
        if any(set(field.applies_to) - item_ids for field in self.fields):
            raise ValueError("字段适用范围含未知对象")
        return self


def as_spec(spec: ResearchSpec | dict) -> ResearchSpec:
    return spec if isinstance(spec, ResearchSpec) else ResearchSpec.model_validate(spec)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def effective_items(spec: ResearchSpec | dict) -> list[dict]:
    value = spec if isinstance(spec, ResearchSpec) else as_spec(spec)
    if value.items:
        return [item.model_dump() for item in value.items]
    return [{"id": f"q{i + 1}", "name": q, "version": "", "rationale": ""} for i, q in enumerate(value.questions)]


def spec_fingerprint(spec: ResearchSpec | dict) -> str:
    return digest(as_spec(spec).model_dump(mode="json"))


def cell_fingerprint(spec: ResearchSpec | dict, item_id: str, field_id: str) -> str:
    spec = as_spec(spec)
    item = next((i for i in effective_items(spec) if i["id"] == item_id), None)
    field = next((f.model_dump() for f in spec.fields if f.id == field_id), None)
    if item is None or field is None:
        raise ValueError("未知对象或字段")
    # Other objects' applicability has no bearing on this cell.
    field["applies_to"] = not field["applies_to"] or item_id in field["applies_to"]
    return digest({"item": item, "field": field, "questions": spec.questions if spec.items else [item["name"]],
                   "hard_constraints": spec.hard_constraints, "scope": spec.scope.model_dump(),
                   "source_policy": spec.source_policy, "allowed_domains": spec.allowed_domains})
