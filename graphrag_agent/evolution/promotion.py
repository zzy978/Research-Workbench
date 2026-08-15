"""Human-gated activation and reversible Skill version pointers."""

from __future__ import annotations

import json

from graphrag_agent.persistence.repositories import AuditRepository, SkillRepository

from .registry import SkillRegistry


class PromotionRejected(ValueError):
    pass


class PromotionPolicy:
    def __init__(self, repository: SkillRepository, registry: SkillRegistry, audit: AuditRepository):
        self.repository, self.registry, self.audit = repository, registry, audit

    async def promote(self, *, name: str, version: str, human_approved: bool):
        if not human_approved:
            raise PromotionRejected("必须由用户明确批准 Promote")
        candidate = await self.repository.find_candidate(name, version)
        target = await self.repository.get_version(name, version)
        if candidate is None or target is None:
            raise PromotionRejected("候选版本不存在")
        evaluation = await self.repository.latest_eval(candidate.candidate_id)
        if evaluation is None or evaluation.status != "passed":
            raise PromotionRejected("候选尚未通过离线评测")
        metrics = json.loads(evaluation.metrics_json or "{}")
        if not (metrics.get("citation_integrity") == 1.0 and metrics.get("tool_policy_violations") == 0 and metrics.get("source_leakage") == 0 and metrics.get("safety_passed") is True and metrics.get("contract_pass_rate", 0) >= metrics.get("active_contract_pass_rate", 1)):
            raise PromotionRejected("候选未通过硬门禁")
        for previous in await self.repository.list_versions(name=name, status="active"):
            previous_hash = self.registry.rewrite_status(previous.content_path, "previous")
            await self.repository.update_version_hash(name, previous.version, previous_hash)
        digest = self.registry.rewrite_status(target.content_path, "active")
        if not await self.repository.promote(candidate_id=candidate.candidate_id, name=name, version=version, content_hash=digest):
            raise PromotionRejected("候选状态不允许 Promote")
        await self.audit.append(action="skill.promoted", entity_type="skill", entity_id=f"{name}:{version}", payload={"candidate_id": candidate.candidate_id, "eval_run_id": evaluation.eval_run_id})
        return await self.repository.get_version(name, version)

    async def rollback(self, name: str):
        active = (await self.repository.list_versions(name=name, status="active"))
        previous = (await self.repository.list_versions(name=name, status="previous"))
        if not active or not previous:
            raise PromotionRejected("没有可回滚的 previous 版本")
        current, target = active[0], previous[0]
        current_hash = self.registry.rewrite_status(current.content_path, "candidate")
        target_hash = self.registry.rewrite_status(target.content_path, "active")
        if not await self.repository.rollback(name, active_hash=current_hash, previous_version=target.version, previous_hash=target_hash):
            raise PromotionRejected("回滚状态冲突")
        await self.audit.append(action="skill.rolled_back", entity_type="skill", entity_id=name, payload={"from": current.version, "to": target.version})
        return await self.repository.get_version(name, target.version)
