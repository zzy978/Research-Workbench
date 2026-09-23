import { API_BASE } from "./client";
import { ApiError } from "../types/api";
import { ResearchMatrix, ResearchMutationResult, ResearchRevision, ResearchRevisionRef, ResearchSpec, ResearchStudy } from "../types/research";

async function researchRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ error: { code: "NETWORK_ERROR", message: response.statusText } }));
    throw new ApiError(payload.error ?? { code: "HTTP_ERROR", message: response.statusText });
  }
  return response.json() as Promise<T>;
}

const post = <T>(path: string, body: unknown) => researchRequest<T>(path, { method: "POST", body: JSON.stringify(body) });

export const researchApi = {
  study: (studyId: string) => researchRequest<ResearchStudy>(`/research/${encodeURIComponent(studyId)}`),
  revisions: (studyId: string) => researchRequest<ResearchRevisionRef[]>(`/research/${encodeURIComponent(studyId)}/revisions`),
  revision: (studyId: string, revision: number) => researchRequest<ResearchRevision>(`/research/${encodeURIComponent(studyId)}/revisions/${revision}`),
  restore: (study: ResearchStudy, sourceRevision: number) => post<ResearchStudy>(`/research/${encodeURIComponent(study.study_id)}/restore`, {
    revision: study.current_revision, fingerprint: study.fingerprint, source_revision: sourceRevision,
  }),
  matrix: (studyId: string) => researchRequest<ResearchMatrix>(`/research/${encodeURIComponent(studyId)}/matrix`),
  reviseSpec: (study: ResearchStudy, spec: ResearchSpec) => post<ResearchStudy>(`/research/${encodeURIComponent(study.study_id)}/revisions`, {
    revision: study.current_revision, fingerprint: study.fingerprint, spec,
  }),
  reviseInstruction: (study: ResearchStudy, instruction: string) => post<ResearchStudy>(`/research/${encodeURIComponent(study.study_id)}/revisions`, {
    revision: study.current_revision, fingerprint: study.fingerprint, instruction,
  }),
  approve: (study: ResearchStudy) => post<ResearchMutationResult>(`/research/${encodeURIComponent(study.study_id)}/approve`, {
    revision: study.current_revision, fingerprint: study.fingerprint, client_request_id: crypto.randomUUID(),
  }),
  followup: (study: ResearchStudy, cells: Array<{item_id: string; field_id: string}>, reason: string) => post<ResearchMutationResult>(`/research/${encodeURIComponent(study.study_id)}/followups`, {
    revision: study.current_revision, fingerprint: study.fingerprint, client_request_id: crypto.randomUUID(), cells, reason,
  }),
  accept: (study: ResearchStudy) => post<ResearchStudy>(`/research/${encodeURIComponent(study.study_id)}/accept`, {
    revision: study.current_revision, fingerprint: study.fingerprint, report_fingerprint: study.report?.fingerprint,
  }),
  exportUrl: (studyId: string, format?: "json") => `${API_BASE}/research/${encodeURIComponent(studyId)}/export${format ? `?format=${format}` : ""}`,
};
