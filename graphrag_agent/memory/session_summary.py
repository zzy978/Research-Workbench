"""Structured rolling summaries that never replace source messages."""

from __future__ import annotations

import json

from graphrag_agent.persistence.repositories import MessageRepository, SessionRepository


class SessionSummarizer:
    def __init__(self, sessions: SessionRepository, messages: MessageRepository, *, threshold_messages: int = 20):
        self.sessions, self.messages = sessions, messages
        self.threshold_messages = max(10, threshold_messages)

    async def get_or_update(self, session_id: str) -> dict:
        session = await self.sessions.get(session_id)
        if session is None:
            return {}
        messages = await self.messages.list_for_session(session_id, limit=500)
        current = json.loads(session.summary_json or "{}")
        if len(messages) < self.threshold_messages:
            return current
        protected = min(16, max(2, len(messages) // 2))
        middle = messages[:-protected]
        if not middle:
            return current
        summary = {
            "summary": "；".join(item.content[:180] for item in middle[-8:]),
            "decisions": [item.content[:180] for item in middle if item.role == "user" and any(key in item.content for key in ("要求", "决定", "使用"))][-5:],
            "verified_facts": [{"text": item.content[:180], "message_id": item.message_id, "run_id": item.run_id} for item in middle if item.role == "assistant"][-5:],
            "unresolved": [item.content[:180] for item in middle if item.role == "user"][-3:],
            "message_ids": [item.message_id for item in middle],
        }
        await self.sessions.update_summary(session_id, summary)
        return summary
