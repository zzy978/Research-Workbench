"""Transactional research facts, with immutable revisions and evidence history.

SQLite is the application's supported database. BEGIN IMMEDIATE obtains its
writer reservation before reading CAS state, so independent workers cannot
overspend a shared budget or approve an already replaced specification.
"""
from __future__ import annotations

import json
import math
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import urlsplit

from sqlalchemy import ForeignKey, Integer, String, Text, select, text
from sqlalchemy.orm import Mapped, mapped_column

from deepresearch_agent.harness.budgets import BudgetExceeded
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence.models import Base, EvidenceModel, RunModel, ToolCallModel
from .schemas import as_spec, canonical, cell_fingerprint, digest, effective_items, spec_fingerprint


class StudyModel(Base):
    __tablename__ = "research_studies"
    study_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"))
    current_revision: Mapped[int] = mapped_column(Integer)
    approved_revision: Mapped[int | None] = mapped_column(Integer)
    approved_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="draft")
    acceptance_json: Mapped[str | None] = mapped_column(Text)


class RevisionModel(Base):
    __tablename__ = "research_revisions"
    study_id: Mapped[str] = mapped_column(ForeignKey("research_studies.study_id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    spec_json: Mapped[str] = mapped_column(Text)


class ResearchRunModel(Base):
    __tablename__ = "research_runs"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    study_id: Mapped[str] = mapped_column(ForeignKey("research_studies.study_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), unique=True)
    purpose: Mapped[str] = mapped_column(String(32))
    targets_json: Mapped[str | None] = mapped_column(Text)
    usage_json: Mapped[str] = mapped_column(Text, default="{}")


class CellModel(Base):
    __tablename__ = "research_cells"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    study_id: Mapped[str] = mapped_column(ForeignKey("research_studies.study_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    item_id: Mapped[str] = mapped_column(String(80))
    field_id: Mapped[str] = mapped_column(String(80))
    fingerprint: Mapped[str] = mapped_column(String(64))
    cell_json: Mapped[str] = mapped_column(Text)


class ReportModel(Base):
    __tablename__ = "research_reports"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    study_id: Mapped[str] = mapped_column(ForeignKey("research_studies.study_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    complete: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    run_sequence: Mapped[int] = mapped_column(Integer)


RESEARCH_TABLES = [StudyModel, RevisionModel, ResearchRunModel, CellModel, ReportModel]
LIVE_STATUSES = {"queued", "context_building", "planning", "executing", "reporting", "verifying", "retrying", "replanning", "cancelling"}


def conflict(message):
    raise AppError(ErrorCode.CONFLICT, message)


class ResearchStore:
    def __init__(self, database):
        self.database = database

    @asynccontextmanager
    async def _write(self):
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def _study(self, session, study_id):
        study = await session.get(StudyModel, study_id)
        if study is None:
            raise AppError(ErrorCode.NOT_FOUND, "研究不存在")
        return study

    async def _revision(self, session, study):
        return await session.get(RevisionModel, (study.study_id, study.current_revision))

    async def _cas(self, session, study_id, revision, fingerprint=None, approved=False):
        study = await self._study(session, study_id)
        current = await self._revision(session, study)
        if revision != study.current_revision or (fingerprint is not None and fingerprint != current.fingerprint):
            conflict("研究范围已更新，请刷新后重试")
        if approved and (study.approved_revision != revision or study.approved_fingerprint != current.fingerprint):
            conflict("当前研究范围尚未批准")
        return study, current

    async def _links(self, session, study_id):
        return list((await session.scalars(select(ResearchRunModel).where(ResearchRunModel.study_id == study_id).order_by(ResearchRunModel.sequence))).all())

    async def _run_link(self, session, run_id):
        link = await session.scalar(select(ResearchRunModel).where(ResearchRunModel.run_id == run_id))
        if link is None:
            raise AppError(ErrorCode.NOT_FOUND, "运行未关联研究")
        return link

    async def _no_live_runs(self, session, study_id):
        live = await session.scalar(select(RunModel.run_id).join(ResearchRunModel, ResearchRunModel.run_id == RunModel.run_id).where(ResearchRunModel.study_id == study_id, RunModel.status.in_(LIVE_STATUSES)).limit(1))
        if live:
            conflict("请先暂停正在运行的研究")

    async def _allowed(self, session, run_id, phase):
        link = await self._run_link(session, run_id)
        study, revision = await self._cas(session, link.study_id, link.revision, approved=phase == "research")
        latest_run_id = await session.scalar(
            select(ResearchRunModel.run_id)
            .where(ResearchRunModel.study_id == link.study_id)
            .order_by(ResearchRunModel.sequence.desc())
            .limit(1)
        )
        if latest_run_id != run_id:
            conflict("新一轮研究已经开始，旧运行不能继续执行")
        if phase == "research":
            if link.purpose == "outline":
                conflict("大纲运行不能执行深入研究")
        elif phase in {"discovery", "outline"}:
            if link.purpose != "outline" or study.approved_revision is not None:
                conflict("当前运行不允许探索检索")
        else:
            conflict("未知研究阶段")
        return study, revision, link

    @staticmethod
    def _usage(links):
        total = {"external_calls": 0, "discovery_calls": 0, "llm_tokens": 0, "active_seconds": 0}
        for link in links:
            usage = json.loads(link.usage_json)
            for key in total:
                total[key] += usage.get(key, 0)
        return total

    async def create(self, session_id, run_id, spec):
        spec = as_spec(spec)
        async with self._write() as session:
            run = await session.get(RunModel, run_id)
            if run is None or run.session_id != session_id:
                conflict("运行与研究会话不匹配")
            existing = await session.scalar(select(ResearchRunModel).where(ResearchRunModel.run_id == run_id))
            if existing:
                current = await session.get(RevisionModel, (existing.study_id, 1))
                if current.fingerprint != spec_fingerprint(spec):
                    conflict("运行已关联不同研究范围")
                return await self._view(session, await self._study(session, existing.study_id))
            study = StudyModel(study_id="study_" + uuid4().hex, session_id=session_id, current_revision=1, status="draft")
            session.add(study)
            await session.flush()
            session.add(RevisionModel(study_id=study.study_id, revision=1, fingerprint=spec_fingerprint(spec), spec_json=canonical(spec.model_dump())))
            session.add(ResearchRunModel(study_id=study.study_id, revision=1, run_id=run_id, purpose="outline", usage_json="{}"))
            await session.flush()
            return await self._view(session, study)

    async def get(self, study_id):
        async with self.database.sessions() as session:
            return await self._view(session, await self._study(session, study_id))

    async def for_run(self, run_id):
        async with self.database.sessions() as session:
            link = await session.scalar(select(ResearchRunModel).where(ResearchRunModel.run_id == run_id))
            return await self._view(session, await self._study(session, link.study_id)) if link else None

    async def revise(self, study_id, revision, fingerprint, spec):
        spec = as_spec(spec)
        async with self._write() as session:
            study, _ = await self._cas(session, study_id, revision, fingerprint)
            await self._no_live_runs(session, study_id)
            study.current_revision += 1
            study.approved_revision = study.approved_fingerprint = study.acceptance_json = None
            study.status = "draft"
            session.add(RevisionModel(study_id=study_id, revision=study.current_revision, fingerprint=spec_fingerprint(spec), spec_json=canonical(spec.model_dump())))
            await session.flush()
            return await self._view(session, study)

    async def approve(self, study_id, revision, fingerprint):
        async with self._write() as session:
            study, _ = await self._cas(session, study_id, revision, fingerprint)
            if study.approved_revision != revision:
                study.approved_revision, study.approved_fingerprint = revision, fingerprint
                study.status = "investigating"
            await session.flush()
            return await self._view(session, study)

    async def link_run(self, study_id, revision, run_id, purpose="research", targets=None):
        async with self._write() as session:
            study, rev = await self._cas(session, study_id, revision, approved=purpose != "outline")
            run = await session.get(RunModel, run_id)
            if run is None or run.session_id != study.session_id or purpose not in {"outline", "research", "followup"}:
                conflict("无效的研究运行")
            existing = await session.scalar(select(ResearchRunModel).where(ResearchRunModel.run_id == run_id))
            if existing:
                if (existing.study_id, existing.revision, existing.purpose, json.loads(existing.targets_json or "null")) == (study_id, revision, purpose, targets):
                    return
                # A waiting outline is a draft container, not an executed
                # research version. Preserve its discovery usage on promotion.
                has_cells = await session.scalar(select(CellModel.sequence).where(CellModel.run_id == run_id).limit(1))
                has_report = await session.scalar(select(ReportModel.sequence).where(ReportModel.run_id == run_id).limit(1))
                if existing.study_id != study_id or existing.purpose != "outline" or purpose not in {"outline", "research"} or targets is not None or has_cells or has_report or run.status in LIVE_STATUSES:
                    conflict("运行已关联其他研究或目标")
                existing.revision, existing.purpose = revision, purpose
                study.acceptance_json = None
                study.status = "investigating" if purpose == "research" else "draft"
                return
            spec = as_spec(json.loads(rev.spec_json))
            if targets is not None:
                if not targets:
                    conflict("补充研究至少选择一个单元")
                for target in targets:
                    self._target(spec, target.get("item_id"), target.get("field_id"))
            if purpose == "followup" and targets is None:
                conflict("补充研究必须选择目标单元")
            session.add(ResearchRunModel(study_id=study_id, revision=revision, run_id=run_id, purpose=purpose, targets_json=canonical(targets) if targets is not None else None, usage_json="{}"))
            study.acceptance_json = None
            study.status = "investigating" if purpose != "outline" else "draft"

    async def assert_allowed(self, run_id, phase="research"):
        async with self.database.sessions() as session:
            study, _, _ = await self._allowed(session, run_id, phase)
            return await self._view(session, study)

    async def reserve_request(self, run_id, phase="research"):
        async with self._write() as session:
            study, revision, link = await self._allowed(session, run_id, phase)
            usage = self._usage(await self._links(session, study.study_id))
            budget = as_spec(json.loads(revision.spec_json)).budget
            limits = [("external_calls", budget.max_search_calls), ("llm_tokens", budget.max_llm_tokens), ("active_seconds", budget.max_active_seconds)]
            if phase != "research":
                limits.append(("discovery_calls", 3))
            for key, limit in limits:
                if usage[key] >= limit:
                    raise BudgetExceeded(key, usage[key], limit)
            local = json.loads(link.usage_json)
            local["external_calls"] = local.get("external_calls", 0) + 1
            if phase != "research":
                local["discovery_calls"] = local.get("discovery_calls", 0) + 1
            link.usage_json = canonical(local)
            await session.flush()
            return await self._view(session, study)

    async def run_usage(self, run_id) -> dict:
        """Return this Run's durable cumulative counters, including zero defaults."""
        async with self.database.sessions() as session:
            return self._usage([await self._run_link(session, run_id)])

    async def record_usage(self, run_id, llm_tokens, active_seconds):
        if not isinstance(llm_tokens, int) or llm_tokens < 0 or not math.isfinite(active_seconds) or active_seconds < 0:
            conflict("用量必须是非负累计值")
        async with self._write() as session:
            link = await self._run_link(session, run_id)
            usage = json.loads(link.usage_json)
            usage["llm_tokens"] = max(usage.get("llm_tokens", 0), llm_tokens)
            usage["active_seconds"] = max(usage.get("active_seconds", 0), active_seconds)
            link.usage_json = canonical(usage)

    @staticmethod
    def _target(spec, item_id, field_id):
        try:
            fingerprint = cell_fingerprint(spec, item_id, field_id)
        except ValueError:
            conflict("未知研究对象或字段")
        field = next(f for f in spec.fields if f.id == field_id)
        return fingerprint, not field.applies_to or item_id in field.applies_to

    async def _citations(self, session, study_id, citations):
        links = {link.run_id for link in await self._links(session, study_id)}
        sources = set()
        for citation in citations:
            if not isinstance(citation, dict):
                conflict("引用格式无效")
            ev = await session.get(EvidenceModel, citation.get("evidence_id"))
            if ev is None or ev.run_id not in links or ev.invalidated_at or ev.content_hash != citation.get("content_hash"):
                conflict("引用不属于本研究或内容已失效")
            if not ev.summary.strip() or not ev.content_hash:
                conflict("引用没有实际证据内容")
            sources.add(ev.source_id)
        return sources

    async def save_cell(self, study_id, revision, run_id, cell):
        async with self._write() as session:
            study, rev = await self._cas(session, study_id, revision, approved=True)
            _, _, link = await self._allowed(session, run_id, "research")
            if link.study_id != study_id:
                conflict("运行不属于当前研究")
            spec = as_spec(json.loads(rev.spec_json))
            fingerprint, applicable = self._target(spec, cell.get("item_id"), cell.get("field_id"))
            target = {"item_id": cell["item_id"], "field_id": cell["field_id"]}
            if link.targets_json and target not in json.loads(link.targets_json):
                conflict("单元不属于本轮补充研究目标")
            status = cell.get("status")
            if status not in {"supported", "inference", "conflict", "not_found", "not_applicable"}:
                conflict("无效的单元状态")
            if applicable == (status == "not_applicable"):
                conflict("单元状态与字段适用范围不一致")
            citations = cell.get("citations") or []
            sources = await self._citations(session, study_id, citations)
            if status in {"supported", "inference", "conflict"}:
                if not citations or cell.get("value") is None or cell.get("value") == "":
                    conflict("结论需要实际取值和证据引用")
                if spec.allowed_domains:
                    for source_id in sources:
                        try:
                            source = urlsplit(source_id)
                            hostname = (source.hostname or "").lower().rstrip(".")
                        except ValueError:
                            conflict("证据来源不是有效的公开网页地址")
                        if source.scheme not in {"http", "https"} or not any(
                            hostname == domain or hostname.endswith("." + domain)
                            for domain in spec.allowed_domains
                        ):
                            conflict("证据来源不在已批准的来源域名范围内")
                field = next(f for f in spec.fields if f.id == cell['field_id'])
                if field.required_access == 'full_text':
                    for citation in citations:
                        ev = await session.get(EvidenceModel, citation['evidence_id'])
                        metadata = json.loads(ev.metadata_json or '{}')
                        if metadata.get('extra', {}).get('access') != 'full_text':
                            conflict('当前字段要求正文，搜索片段不能支撑该结论')
            if status == "conflict" and len(sources) < 2:
                conflict("冲突需要至少两个独立来源")
            if status == "not_found":
                attempt = await session.scalar(select(ToolCallModel.tool_call_id).where(ToolCallModel.run_id == run_id, ToolCallModel.status.in_({"completed", "failed"})).limit(1))
                if not str(cell.get("reason", "")).strip() or not (cell.get("search_log") or attempt):
                    conflict("未找到证据需要检索记录和原因")
            payload = {**target, "status": status, "value": cell.get("value"), "reason": cell.get("reason", ""), "citations": citations, "search_log": cell.get("search_log", [])}
            session.add(CellModel(study_id=study_id, revision=revision, run_id=run_id, item_id=cell["item_id"], field_id=cell["field_id"], fingerprint=fingerprint, cell_json=canonical(payload)))
            study.acceptance_json = None
            study.status = "investigating"

    async def _followup_barriers(self, session, study_id, links):
        """Retain refresh requests across revisions of the same cell scope.

        A failed/cancelled Run does not fulfil its requested refresh. Its
        immutable revision supplies the target fingerprint; only a cell from
        that Run or a later Run can satisfy this barrier. Indexing by fingerprint
        avoids invalidating facts from a different object/field specification.
        """
        followups = [link for link in links if link.targets_json]
        if not followups:
            return {}
        revisions = await session.scalars(select(RevisionModel).where(
            RevisionModel.study_id == study_id,
            RevisionModel.revision.in_({link.revision for link in followups}),
        ))
        specs = {revision.revision: as_spec(json.loads(revision.spec_json)) for revision in revisions}
        barriers = {}
        for link in followups:
            for target in json.loads(link.targets_json):
                item_id, field_id = target["item_id"], target["field_id"]
                fingerprint = cell_fingerprint(specs[link.revision], item_id, field_id)
                key = (item_id, field_id, fingerprint)
                barriers[key] = max(barriers.get(key, 0), link.sequence)
        return barriers

    async def _matrix(self, session, study):
        revision = await self._revision(session, study)
        spec = as_spec(json.loads(revision.spec_json))
        links = await self._links(session, study.study_id)
        current_links = [link for link in links if link.revision == study.current_revision and link.purpose != "outline"]
        current_run = current_links[-1].run_id if current_links else None
        link_order = {link.run_id: link.sequence for link in links}
        barriers = await self._followup_barriers(session, study.study_id, links)
        rows = list((await session.scalars(select(CellModel).where(CellModel.study_id == study.study_id).order_by(CellModel.sequence.desc()))).all())
        counts = {"expected": 0, "current": 0, "missing": 0, "stale": 0, "unknown": 0}
        cells = []
        for item in effective_items(spec):
            for field in spec.fields:
                fingerprint, applicable = self._target(spec, item["id"], field.id)
                base = {"item_id": item["id"], "field_id": field.id, "fingerprint": fingerprint, "value": None, "reason": "", "citations": [], "origin_run_id": None, "run_id": current_run}
                counts["expected"] += 1
                if not applicable:
                    base["status"] = "not_applicable"
                    counts["current"] += 1
                    cells.append(base)
                    continue
                relevant = [row for row in rows if row.item_id == item["id"] and row.field_id == field.id]
                invalidation = barriers.get((item["id"], field.id, fingerprint), 0)
                selected = None
                for row in relevant:
                    if row.fingerprint != fingerprint or link_order[row.run_id] < invalidation:
                        continue
                    payload = json.loads(row.cell_json)
                    try:
                        await self._citations(session, study.study_id, payload["citations"])
                    except AppError:
                        continue
                    selected = row
                    break
                if selected:
                    base.update(json.loads(selected.cell_json))
                    base["origin_run_id"] = selected.run_id
                    base["run_id"] = current_run or selected.run_id
                    counts["current"] += 1
                    counts["unknown"] += base["status"] == "not_found"
                elif relevant:
                    base.update(json.loads(relevant[0].cell_json))
                    base["origin_run_id"] = relevant[0].run_id
                    base["status"] = "stale"
                    counts["stale"] += 1
                else:
                    base["status"] = "missing"
                    counts["missing"] += 1
                cells.append(base)
        return {"items": effective_items(spec), "fields": [f.model_dump() for f in spec.fields], "cells": cells, "counts": counts, "view_mode": "comparison" if spec.items else "questions"}

    async def matrix(self, study_id):
        async with self.database.sessions() as session:
            return await self._matrix(session, await self._study(session, study_id))

    @staticmethod
    def _report_fingerprint(spec_hash, matrix, content):
        return digest({"spec": spec_hash, "cells": matrix["cells"], "content": content})

    @staticmethod
    def _complete(matrix, content):
        return bool(content.strip() and not matrix["counts"]["missing"] and not matrix["counts"]["stale"] and any(c["citations"] for c in matrix["cells"]))

    async def set_report(self, study_id, revision, run_id, content, complete):
        async with self._write() as session:
            study, rev = await self._cas(session, study_id, revision, approved=True)
            _, _, link = await self._allowed(session, run_id, "research")
            if link.study_id != study_id:
                conflict("报告运行不属于本研究")
            links = await self._links(session, study_id)
            matrix = await self._matrix(session, study)
            if complete and not self._complete(matrix, content):
                conflict("报告仍有缺失、失效单元或缺少实际证据")
            session.add(ReportModel(study_id=study_id, revision=revision, run_id=run_id, fingerprint=self._report_fingerprint(rev.fingerprint, matrix, content), content=content, complete=int(complete), run_sequence=links[-1].sequence))
            study.acceptance_json = None
            study.status = "report_review"
            await session.flush()
            return await self._view(session, study)

    async def _report(self, session, study):
        row = await session.scalar(select(ReportModel).where(ReportModel.study_id == study.study_id, ReportModel.revision == study.current_revision).order_by(ReportModel.sequence.desc()).limit(1))
        if row is None:
            return None
        links = await self._links(session, study.study_id)
        if links and links[-1].sequence > row.run_sequence:
            return None
        rev = await self._revision(session, study)
        matrix = await self._matrix(session, study)
        fresh = self._report_fingerprint(rev.fingerprint, matrix, row.content) == row.fingerprint
        return {"run_id": row.run_id, "fingerprint": row.fingerprint, "content": row.content, "complete": bool(row.complete and fresh and self._complete(matrix, row.content)), "stale": not fresh}

    async def get_report(self, study_id):
        async with self.database.sessions() as session:
            return await self._report(session, await self._study(session, study_id))

    async def accept(self, study_id, revision, fingerprint, report_fingerprint):
        async with self._write() as session:
            study, _ = await self._cas(session, study_id, revision, fingerprint, approved=True)
            await self._no_live_runs(session, study_id)
            report = await self._report(session, study)
            if not report or not report["complete"] or report["fingerprint"] != report_fingerprint:
                conflict("报告不完整或已过期，请检查最新证据后再验收")
            if not study.acceptance_json:
                study.acceptance_json = canonical({"revision": revision, "fingerprint": fingerprint, "report_fingerprint": report_fingerprint, "accepted_at": datetime.now(timezone.utc).isoformat()})
            study.status = "complete"
            await session.flush()
            return await self._view(session, study)

    async def _view(self, session, study):
        revision = await self._revision(session, study)
        links = await self._links(session, study.study_id)
        report = await self._report(session, study)
        acceptance = json.loads(study.acceptance_json) if study.acceptance_json else None
        if acceptance and (not report or not report["complete"] or report["fingerprint"] != acceptance["report_fingerprint"]):
            acceptance = None
        current_spec = json.loads(revision.spec_json)
        previous = await session.get(RevisionModel, (study.study_id, study.current_revision - 1)) if study.current_revision > 1 else None
        previous_spec = json.loads(previous.spec_json) if previous else {}
        changed = [key for key, value in current_spec.items() if previous_spec.get(key) != value] if previous else []
        return {"study_id": study.study_id, "session_id": study.session_id, "status": "report_review" if study.status == "complete" and not acceptance else study.status,
                "current_revision": study.current_revision, "fingerprint": revision.fingerprint, "spec": current_spec,
                "approved_revision": study.approved_revision, "approved_fingerprint": study.approved_fingerprint,
                "run_id": links[-1].run_id if links else None,
                "runs": [{"run_id": link.run_id, "revision": link.revision, "purpose": link.purpose} for link in links],
                "usage": self._usage(links), "report": report, "acceptance": acceptance, "diff": {"changed_fields": changed, "previous_revision": study.current_revision - 1 if previous else None}}
