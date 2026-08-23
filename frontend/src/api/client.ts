import { ApiError, CacheStats, Capabilities, ContextInspector, CuratedMemory, Evidence, MemoryCapacity, Report, Run, Session, SessionDetail, SourceMode, WorkflowMode } from "../types/api";

export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1").replace(/\/$/, "");

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers: { "Content-Type": "application/json", ...init?.headers } });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ error: { code: "NETWORK_ERROR", message: response.statusText } }));
    throw new ApiError(payload.error ?? { code: "HTTP_ERROR", message: response.statusText });
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  capabilities: () => request<Capabilities>("/capabilities"),
  health: () => request<{status: string; components: Record<string, {status: string; configured?: boolean}>}>("/health"),
  cacheStats: () => request<CacheStats>("/cache/stats"),
  sessions: () => request<{items: Session[]; total: number}>("/sessions?include_archived=true"),
  session: (id: string) => request<SessionDetail>(`/sessions/${id}`),
  createSession: (title = "新对话") => request<Session>("/sessions", { method: "POST", body: JSON.stringify({ title }) }),
  patchSession: (id: string, payload: {title?: string; status?: string}) => request<Session>(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteSession: (id: string) => request<void>(`/sessions/${id}`, { method: "DELETE" }),
  send: (sessionId: string, payload: {client_message_id: string; content: string; source_mode: SourceMode; workflow_mode: WorkflowMode}) =>
    request<{message_id: string; run_id: string; status: string; events_url: string}>(`/sessions/${sessionId}/messages`, { method: "POST", body: JSON.stringify(payload) }),
  run: (id: string) => request<Run>(`/runs/${id}`),
  cancel: (id: string) => request(`/runs/${id}/cancel`, { method: "POST" }),
  clarify: (id: string, content: string) => request(`/runs/${id}/clarifications`, { method: "POST", body: JSON.stringify({ content }) }),
  evidence: (id: string) => request<{items: Evidence[]; total: number}>(`/runs/${id}/evidence`),
  report: (id: string) => request<Report>(`/runs/${id}/report`),
  context: (id: string) => request<ContextInspector>(`/runs/${id}/context`),
  memories: () => request<{items: CuratedMemory[]; total: number}>("/memories"),
  memoryCapacity: () => request<{targets: Record<"user" | "project", MemoryCapacity>}>("/memories/capacity"),
  createMemory: (payload: Record<string, unknown>) => request<CuratedMemory>("/memories", { method: "POST", body: JSON.stringify(payload) }),
  patchMemory: (id: string, payload: Record<string, unknown>) => request<CuratedMemory>(`/memories/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteMemory: (id: string) => request<void>(`/memories/${id}`, { method: "DELETE" }),
  skills: () => request<{versions: Array<Record<string, unknown>>; candidates: Array<Record<string, unknown>>}>("/skills"),
  evaluateSkill: (name: string, version: string) => request<Record<string, unknown>>(`/skills/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}/evaluate`, {method: "POST"}),
  promoteSkill: (name: string, version: string) => request<Record<string, unknown>>(`/skills/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}/promote`, {method: "POST"}),
  rollbackSkill: (name: string) => request<Record<string, unknown>>(`/skills/${encodeURIComponent(name)}/rollback`, {method: "POST"}),
};
