"""Conservative candidate extraction from verified Runs."""

from __future__ import annotations

from graphrag_agent.persistence.repositories import ContractRepository, MessageRepository, RunRepository

from .service import MemoryRejected, MemoryService


class MemoryExtractor:
    def __init__(self, service: MemoryService, runs: RunRepository, messages: MessageRepository, contracts: ContractRepository):
        self.service, self.runs, self.messages, self.contracts = service, runs, messages, contracts

    async def extract_from_completed_run(self, run_id: str) -> list:
        run = await self.runs.get(run_id)
        if run is None or run.status != "completed" or not await self.contracts.required_checks_passed(run_id):
            return []
        trigger = await self.messages.get(run.trigger_message_id)
        answer = await self.messages.get_assistant_for_run(run_id)
        if trigger is None or answer is None:
            return []
        candidates: list[tuple[str, str, str, float]] = []
        if any(marker in trigger.content for marker in ("记住", "以后请", "我偏好", "我喜欢")):
            candidates.append((trigger.content[:1000], "user", "preference", 0.9))
        # A verified answer becomes a reviewable project fact, never active automatically.
        if len(answer.content.strip()) >= 20:
            compact = " ".join(answer.content.strip().split())[:1200]
            candidates.append((compact, "project", "fact", 0.75))
        created = []
        for content, scope, kind, confidence in candidates:
            try:
                created.append(await self.service.create_candidate(
                    content=content, scope=scope, kind=kind,
                    provenance_refs=[f"run:{run_id}", f"message:{answer.message_id if kind == 'fact' else trigger.message_id}"],
                    confidence=confidence, session_id=run.session_id, created_by="agent",
                ))
            except MemoryRejected:
                continue
        return created
