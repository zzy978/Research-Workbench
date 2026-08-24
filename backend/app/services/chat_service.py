"""Transactional chat command service."""

from backend.app.schemas import MessageCreate, MessageSend, RunAccepted, RunCreate
from deepresearch_agent.config.settings import HARNESS_BUDGETS
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence.repositories import RunRepository, SessionRepository


class ChatService:
    def __init__(self, sessions: SessionRepository, runs: RunRepository, run_service):
        self.sessions = sessions
        self.runs = runs
        self.run_service = run_service

    async def send(self, session_id: str, request: MessageSend) -> RunAccepted:
        session = await self.sessions.get(session_id)
        if session is None:
            raise AppError(ErrorCode.NOT_FOUND, "Session 不存在")
        if session.status != "active":
            raise AppError(ErrorCode.CONFLICT, "已归档 Session 不能发送消息")
        message, run, created = await self.runs.create_for_user_message(
            MessageCreate(
                session_id=session_id, role="user", content=request.content,
                client_message_id=request.client_message_id,
                metadata={"source_mode": request.source_mode.value, "workflow_mode": request.workflow_mode.value},
            ),
            RunCreate(
                session_id=session_id, trigger_message_id="assigned-atomically",
                source_mode=request.source_mode, workflow_mode=request.workflow_mode,
                config_snapshot={
                    "min_evidence": 1,
                    "report_type": request.report_type,
                    "deep_research_max_iterations": 1 if request.source_mode.value == "graphrag" else 2,
                    "schema_version": 1,
                },
                budget=HARNESS_BUDGETS,
            ),
        )
        if created:
            if session.title.strip() in {"新对话", "New conversation", "Untitled"}:
                title = " ".join(request.content.strip().split())[:36]
                if title:
                    await self.sessions.rename(session_id, title)
            self.run_service.schedule(run.run_id)
        return RunAccepted(
            message_id=message.message_id, run_id=run.run_id, status=run.status,
            events_url=f"/api/v1/runs/{run.run_id}/events", created=created,
        )
