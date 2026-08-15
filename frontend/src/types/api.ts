export type SourceMode = "graphrag" | "web";
export type WorkflowMode = "deep_research" | "plan_execute_report";

export interface Session { session_id: string; title: string; status: string; created_at: string; updated_at: string; }
export interface Message { message_id: string; run_id?: string | null; role: string; content: string; metadata_json: string; created_at: string; }
export interface Run {
  run_id: string; source_mode: SourceMode; workflow_mode: WorkflowMode; status: string; current_stage?: string | null;
  error_code?: string | null; error_message?: string | null; cancellation_requested?: boolean; created_at: string; updated_at: string;
  usage?: RunUsage | null;
}
export interface RunUsage {
  limits?: {
    wall_time_seconds?: number; max_plan_tasks?: number; max_tool_calls?: number; max_tavily_calls?: number;
    max_replans?: number; max_task_retries?: number; max_llm_tokens?: number; max_concurrency?: number; tool_timeout_seconds?: number;
  };
  usage?: {
    tool_calls?: number; tavily_calls?: number; replans?: number; task_retries?: number;
    llm_tokens?: number; elapsed_seconds?: number;
  };
}
export interface SessionDetail extends Session { messages: Message[]; runs: Run[]; }
export interface Evidence {
  evidence_id: string; source_mode: SourceMode; provider: string; source_id: string; title?: string | null;
  summary: string; metadata: Record<string, unknown>; score: number; created_at: string;
}
export interface Report { run_id: string; status: string; content?: string | null; verification: Array<{kind: string; required: boolean; passed?: boolean | null; evidence: Record<string, unknown>}>; }
export interface RunEvent { event_id?: number; event_type: string; stage?: string; [key: string]: unknown; }
export interface Capability { available: boolean; reason?: string | null; }
export interface Capabilities { sources: Record<SourceMode, Capability>; workflows: WorkflowMode[]; default_source_mode: SourceMode; }

export class ApiError extends Error {
  code: string; retryable: boolean; details: unknown;
  constructor(payload: {code: string; message: string; retryable?: boolean; details?: unknown}) {
    super(payload.message); this.code = payload.code; this.retryable = Boolean(payload.retryable); this.details = payload.details;
  }
}
