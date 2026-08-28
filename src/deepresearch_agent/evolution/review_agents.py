"""Separated LLM proposer and read-only critic for Skill learning."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from .review_schema import CriticReview, ReviewPack, SkillProposal


def _content(response: Any) -> str:
    value = getattr(response, "content", response)
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


async def _invoke(llm: Any, prompt: str) -> str:
    if llm is None:
        raise RuntimeError("Skill 学习已启用但未配置 LLM")
    if hasattr(llm, "ainvoke"):
        return _content(await llm.ainvoke(prompt))
    return _content(await asyncio.to_thread(llm.invoke, prompt))


async def _invoke_typed_json(llm: Any, prompt: str, model_type):
    raw = await _invoke(llm, prompt)
    try:
        return model_type.model_validate(_json_object(raw))
    except (ValueError, json.JSONDecodeError) as first_error:
        repair = f"""将下面内容修复为一个严格有效的 JSON 对象。不得改变语义，不得添加 Markdown 代码围栏或解释；补齐缺失的逗号、引号、括号和必需字段。
目标 JSON Schema：{json.dumps(model_type.model_json_schema(), ensure_ascii=False)}
待修复内容：{raw[:12000]}"""
        repaired = await _invoke(llm, repair)
        try:
            return model_type.model_validate(_json_object(repaired))
        except Exception as second_error:
            raise ValueError(f"结构化输出修复失败: first={first_error}; second={second_error}") from second_error


class SkillProposerAgent:
    def __init__(self, llm: Any):
        self.llm = llm

    async def propose(self, pack: ReviewPack, *, revision_instructions: list[str] | None = None) -> SkillProposal:
        prompt = f"""你是 DeepResearch Agent Harness 的 Skill Proposer。只从给定 ReviewPack 中提取可验证、可复用的程序性经验。
必须输出一个 JSON 对象并符合：decision=create|patch|ignore。优先 patch loaded_skills；无稳定增量必须 ignore。
禁止复制完整答案，禁止扩大工具/来源权限，所有规则必须有 trace_refs；必须给出反模式、停止条件、验证方法和有界 machine_policy。
rules 至少 3 条；create 时给出 kebab-case name 和 0.1.0；patch 时给出 target_skill_id/base_version/base_content_hash/proposed_version。
可用字段：decision,target_skill_id,base_version,base_content_hash,title,rationale,name,proposed_version,description,applicability,triggers,inputs,rules,anti_patterns,stop_conditions,verification,fallback,allowed_tools,limitations,machine_policy,support_files,trace_refs。
修订要求：{json.dumps(revision_instructions or [], ensure_ascii=False)}
ReviewPack：{pack.model_dump_json(exclude_none=True)}"""
        return await _invoke_typed_json(self.llm, prompt, SkillProposal)


class SkillCriticAgent:
    def __init__(self, llm: Any):
        self.llm = llm

    async def review(self, pack: ReviewPack, proposal: SkillProposal) -> CriticReview:
        prompt = f"""你是只读的 Skill Critic，不得创建、修改或批准 Skill。审查提案的真实性、泛化性、安全性、成本、一致性、冲突和可验证性。
输出 JSON：decision=pass|revise|reject，scores，blocking_issues，revision_instructions，unsupported_rule_refs，conflict_refs。
缺少 trace、扩大权限、无界循环、一次性答案或未解决失败必须 reject/revise。评分范围 0..1；pass 时 grounding/generalizability/safety/cost_control/consistency 均不得低于 0.7。
ReviewPack：{pack.model_dump_json(exclude_none=True)}
Proposal：{proposal.model_dump_json(exclude_none=True)}"""
        return await _invoke_typed_json(self.llm, prompt, CriticReview)
