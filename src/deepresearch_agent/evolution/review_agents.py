"""Separated LLM proposer and read-only critic for Skill learning."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from .review_schema import CriticReview, ReviewPack, SkillProposal


def _content(response: Any) -> str:
    value = getattr(response, "content", response)
    if value is None:
        return ""
    if isinstance(value, list):
        return "".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in value)
    return str(value)


def _json_object(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            raise ValueError("LLM 未返回 JSON 对象")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM 输出必须是 JSON 对象")
    return value


def _normalize_skill_proposal(payload: dict) -> dict:
    """Normalize common rich JSON shapes without changing proposal semantics."""
    normalized = dict(payload)
    collected_refs = list(normalized.get("trace_refs") or [])
    list_fields = (
        "triggers", "inputs", "rules", "anti_patterns", "stop_conditions",
        "verification", "allowed_tools", "limitations", "trace_refs",
    )
    for field in list_fields:
        value = normalized.get(field)
        if isinstance(value, str):
            normalized[field] = [value]
        elif field == "rules" and isinstance(value, list):
            rules: list[str] = []
            for item in value:
                if isinstance(item, str):
                    rules.append(item)
                    continue
                if isinstance(item, dict):
                    rule = item.get("rule") or item.get("text") or item.get("description")
                    if rule:
                        rules.append(str(rule))
                    refs = item.get("trace_refs") or []
                    collected_refs.extend([str(ref) for ref in refs] if isinstance(refs, list) else [str(refs)])
            normalized[field] = rules
    if isinstance(normalized.get("applicability"), str):
        normalized["applicability"] = {"description": normalized["applicability"]}
    if isinstance(normalized.get("machine_policy"), str):
        normalized["machine_policy"] = {"policy": normalized["machine_policy"]}
    normalized["trace_refs"] = list(dict.fromkeys(str(ref) for ref in collected_refs if ref))
    return normalized


async def _invoke(llm: Any, prompt: str, *, metadata=None) -> str:
    if llm is None:
        raise RuntimeError("Skill 学习已启用但未配置 LLM")
    structured_llm = (
        llm.bind(
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
        )
        if hasattr(llm, "bind") else llm
    )
    if hasattr(structured_llm, "ainvoke"):
        response = await structured_llm.ainvoke(prompt)
    else:
        response = await asyncio.to_thread(structured_llm.invoke, prompt)
    if metadata is not None:
        metadata.update({"response_metadata": getattr(response, "response_metadata", {}),
                         "usage_metadata": getattr(response, "usage_metadata", None)})
    return _content(response)


class ModelOutputError(ValueError):
    """The model did not provide a valid review, rather than rejecting a Skill."""


async def _invoke_typed_json(llm: Any, prompt: str, model_type, *, record_response=None):
    async def invoke_recorded(text, attempt):
        metadata = {}
        try:
            raw = await asyncio.wait_for(_invoke(llm, text, metadata=metadata), timeout=90)
        except Exception as exc:
            if record_response:
                await record_response({"agent": model_type.__name__, "attempt": attempt, "error": type(exc).__name__})
            raise
        if record_response:
            await record_response({"agent": model_type.__name__, "attempt": attempt, "raw": raw, **metadata})
        if not raw.strip():
            raise ModelOutputError("模型返回空正文，无法形成复盘结论")
        return raw

    raw = await invoke_recorded(prompt, "initial")
    try:
        payload = _json_object(raw)
        if model_type is SkillProposal:
            payload = _normalize_skill_proposal(payload)
        return model_type.model_validate(payload)
    except (ValueError, json.JSONDecodeError) as first_error:
        repair = f"""原始复盘任务与证据如下。请依据它重新输出完整有效的 JSON，不得编造缺失依据。技术错误不能作为 Skill 质量拒绝，ignore 必须解释具体依据。
原始任务：{prompt}
校验错误：{first_error}
目标 JSON Schema：{json.dumps(model_type.model_json_schema(), ensure_ascii=False)}
待修复内容：{raw}"""
        repaired = await invoke_recorded(repair, "repair")
        try:
            payload = _json_object(repaired)
            if model_type is SkillProposal:
                payload = _normalize_skill_proposal(payload)
            return model_type.model_validate(payload)
        except Exception as second_error:
            raise ModelOutputError(f"结构化输出修复失败: {type(second_error).__name__}") from second_error


class SkillProposerAgent:
    def __init__(self, llm: Any):
        self.llm = llm

    async def propose(
        self,
        pack: ReviewPack,
        *,
        revision_instructions: list[str] | None = None,
        previous_proposal: SkillProposal | None = None,
        record_response=None,
    ) -> SkillProposal:
        prompt = f"""你是 DeepResearch Agent Harness 的 Skill Proposer。只从给定 ReviewPack 中提取可验证、可复用的程序性经验。
必须输出一个 JSON 对象并符合：decision=create|patch|ignore。优先 patch loaded_skills；无稳定增量必须 ignore。
ignore 也必须给出非空 rationale，解释轨迹为何没有可复用增量。预算降级或未执行任务不能称为完整成功；不得把用户要求当成已验证经验。
禁止复制完整答案，禁止扩大工具/来源权限，所有规则必须有 trace_refs；必须给出反模式、停止条件、验证方法和有界 machine_policy。
适用范围、平台和领域不得超出 ReviewPack.user_goal、source_mode 与现有轨迹实际覆盖范围，禁止类推到未出现的平台。
trace_refs 只能从 ReviewPack.artifact_refs、episodes.cards.trace_refs、episodes.trace_refs 或 completion_contract.ref 逐字复制，禁止手写、缩短或改写 ID。
rules 至少 3 条；create 时给出 kebab-case name 和 0.1.0；patch 时给出 target_skill_id/base_version/base_content_hash/proposed_version。
可用字段：decision,target_skill_id,base_version,base_content_hash,title,rationale,name,proposed_version,description,applicability,triggers,inputs,rules,anti_patterns,stop_conditions,verification,fallback,allowed_tools,limitations,machine_policy,support_files,trace_refs。
修订要求：{json.dumps(revision_instructions or [], ensure_ascii=False)}
原提案（修订时必须返回包含全部字段的完整替代对象，不得只返回差异）：{previous_proposal.model_dump_json(exclude_none=True) if previous_proposal else "无"}
ReviewPack：{pack.model_dump_json(exclude_none=True)}"""
        return await _invoke_typed_json(self.llm, prompt, SkillProposal, record_response=record_response)


class SkillCriticAgent:
    def __init__(self, llm: Any):
        self.llm = llm

    async def review(self, pack: ReviewPack, proposal: SkillProposal, *, record_response=None) -> CriticReview:
        prompt = f"""你是只读的 Skill Critic，不得创建、修改或批准 Skill。审查提案的真实性、泛化性、安全性、成本、一致性、冲突和可验证性。
输出 JSON：decision=pass|revise|reject，scores，blocking_issues，revision_instructions，unsupported_rule_refs，conflict_refs。
缺少 trace、扩大权限、无界循环、一次性答案或未解决失败必须 reject/revise。评分范围 0..1；pass 时 grounding/generalizability/safety/cost_control/consistency 均不得低于 0.7。
所有决定均须提供上述五项评分。reject 须给出具体质量问题，revise 须给出修改要求。缺少输入、空响应或格式错误属于技术失败，不得用 reject 代替。核对预算降级和未完成任务，不得仅以 completed 状态推断完整交付。
无法执行审查时请返回 technical_error 说明，不得编造评分。
ReviewPack：{pack.model_dump_json(exclude_none=True)}
Proposal：{proposal.model_dump_json(exclude_none=True)}"""
        return await _invoke_typed_json(self.llm, prompt, CriticReview, record_response=record_response)
