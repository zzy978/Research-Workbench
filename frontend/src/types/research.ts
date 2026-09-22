export interface ResearchItem {
  id: string;
  name: string;
  version: string;
  rationale: string;
}

export interface ResearchField {
  id: string;
  label: string;
  description: string;
  evidence_requirement: string;
  required_access?: "snippet" | "full_text";
  applies_to: string[];
}

export interface ResearchSpec {
  title: string;
  questions: string[];
  hard_constraints: string[];
  items: ResearchItem[];
  fields: ResearchField[];
  scope: { time_range: string; inclusion: string[]; exclusion: string[] };
  source_policy: string;
  allowed_domains?: string[];
  queries: string[];
  sections: string[];
  budget: { max_search_calls: number; max_active_seconds: number; max_llm_tokens: number };
  stop_conditions: string[];
}

export interface ResearchRunRef {
  run_id: string;
  revision: number;
  purpose: string;
}

export interface ResearchReport {
  run_id: string;
  fingerprint: string;
  complete: boolean;
  content: string;
}

export interface ResearchStudy {
  study_id: string;
  session_id: string;
  status: string;
  run_status?: string | null;
  current_revision: number;
  fingerprint: string;
  spec: ResearchSpec;
  approved_revision?: number | null;
  approved_fingerprint?: string | null;
  run_id?: string | null;
  runs: ResearchRunRef[];
  usage: { external_calls: number; discovery_calls: number; llm_tokens: number; active_seconds: number };
  report: ResearchReport | null;
  acceptance: Record<string, unknown> | null;
  diff: unknown;
}

export interface ResearchCitation {
  evidence_id: string;
  content_hash: string;
  locator: string;
  access: string;
}

export type ResearchCellStatus = "supported" | "inference" | "conflict" | "not_found" | "not_applicable" | "missing" | "stale" | "unknown";

export interface ResearchCell {
  item_id: string;
  field_id: string;
  status: ResearchCellStatus;
  value: unknown;
  reason: string;
  citations: ResearchCitation[];
  fingerprint: string;
  origin_run_id?: string | null;
  run_id?: string | null;
}

export interface ResearchMatrix {
  items: ResearchItem[];
  fields: ResearchField[];
  cells: ResearchCell[];
  counts: { expected: number; current: number; missing: number; stale: number; unknown: number };
  view_mode: "comparison" | "questions";
}

export interface ResearchMutationResult {
  study_id: string;
  run_id: string;
  revision: number;
  status: string;
  created: boolean;
}

export interface SpecChange {
  path: string;
  before: unknown;
  after: unknown;
}

const ACTIVE_STUDY_STATUSES = new Set(["drafting", "discovering", "investigating", "researching", "running", "reporting"]);
const INACTIVE_RUN_STATUSES = new Set(["awaiting_scope_approval", "needs_user_input", "completed", "failed", "interrupted", "cancelled", "budget_exhausted", "paused"]);

export function isResearchActive(study?: Pick<ResearchStudy, "status" | "run_status"> | null): boolean {
  if (!study) return false;
  if (study.run_status) return !INACTIVE_RUN_STATUSES.has(study.run_status);
  return ACTIVE_STUDY_STATUSES.has(study.status);
}

export function diffResearchSpec(before: ResearchSpec, after: ResearchSpec): SpecChange[] {
  const changes: SpecChange[] = [];
  const walk = (left: unknown, right: unknown, path: string) => {
    if (JSON.stringify(left) === JSON.stringify(right)) return;
    if (Array.isArray(left) || Array.isArray(right) || left === null || right === null || typeof left !== "object" || typeof right !== "object") {
      changes.push({ path, before: left, after: right });
      return;
    }
    const keys = new Set([...Object.keys(left as object), ...Object.keys(right as object)]);
    for (const key of keys) walk((left as Record<string, unknown>)[key], (right as Record<string, unknown>)[key], path ? `${path}.${key}` : key);
  };
  walk(before, after, "");
  return changes;
}

export function normalizeResearchDiff(diff: unknown): SpecChange[] {
  const rows: SpecChange[] = [];
  const visit = (value: unknown, fallbackPath = "") => {
    if (Array.isArray(value)) {
      value.forEach((item) => visit(item, fallbackPath));
      return;
    }
    if (!value || typeof value !== "object") return;
    const record = value as Record<string, unknown>;
    if (Array.isArray(record.changed_fields)) {
      const previous = typeof record.previous_revision === "number" ? `第 ${record.previous_revision} 版` : "上一版";
      record.changed_fields.forEach((path) => {
        if (typeof path === "string") rows.push({path, before: previous, after: "当前版本已修改"});
      });
      return;
    }
    if (Array.isArray(record.changes)) {
      visit(record.changes, fallbackPath);
      return;
    }
    const rawPath = typeof record.path === "string" ? record.path : fallbackPath;
    const path = rawPath.replace(/^\//, "").replace(/\//g, ".");
    const hasBefore = "before" in record || "old" in record || "old_value" in record || "from" in record;
    const hasAfter = "after" in record || "new" in record || "new_value" in record || "value" in record || "to" in record;
    if (path && (hasBefore || hasAfter || typeof record.op === "string")) {
      rows.push({
        path,
        before: record.before ?? record.old ?? record.old_value ?? record.from,
        after: record.after ?? record.new ?? record.new_value ?? record.value ?? record.to,
      });
      return;
    }
    for (const [key, nested] of Object.entries(record)) {
      const nestedPath = fallbackPath ? `${fallbackPath}.${key}` : key;
      if (nested && typeof nested === "object" && ("before" in (nested as object) || "after" in (nested as object) || "old" in (nested as object) || "new" in (nested as object))) {
        visit({...nested as Record<string, unknown>, path: nestedPath}, nestedPath);
      } else visit(nested, nestedPath);
    }
  };
  visit(diff);
  return rows;
}

export function researchErrorMessage(error: unknown): string {
  if (error && typeof error === "object" && "code" in error && error.code === "CONFLICT") {
    return "课题已被其他操作更新。你的输入仍保留，请刷新后重新提交。";
  }
  if (error instanceof Error && error.message) return error.message;
  return "操作未完成，请稍后重试。";
}
