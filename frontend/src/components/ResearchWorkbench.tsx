import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { researchApi } from "../api/research";
import {
  diffResearchSpec, isResearchActive, normalizeResearchDiff, researchErrorMessage, ResearchCell,
  ResearchField, ResearchItem, ResearchSpec, ResearchStudy, SpecChange,
} from "../types/research";

interface Props {
  studyId: string;
  onRun: (runId: string) => void;
  onEvidence: (evidenceId: string, runId?: string) => void;
}

type Tab = "spec" | "matrix" | "report";

const STATUS_LABELS: Record<string, string> = {
  draft: "等待确认", drafting: "正在整理研究范围", discovering: "正在补充范围", awaiting_approval: "等待确认",
  investigating: "正在研究", researching: "正在研究", running: "正在研究", reporting: "正在撰写报告",
  review: "等待报告审阅", report_review: "等待报告审阅", accepted: "已定稿", complete: "已定稿", failed: "研究中断", paused: "已暂停",
};

const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "等待开始", context_building: "正在准备上下文", outlining: "正在生成研究范围",
  awaiting_scope_approval: "等待确认", planning: "正在制定计划", executing: "正在检索证据",
  reporting: "正在撰写报告", verifying: "正在核验", retrying: "正在重试", replanning: "正在调整计划",
  needs_user_input: "等待补充信息", paused: "已暂停", completed: "研究已完成", failed: "研究中断",
  cancelled: "已取消", budget_exhausted: "预算已用尽",
};

const CELL_LABELS: Record<string, string> = {
  supported: "有证据", inference: "推断", conflict: "来源冲突", not_found: "未找到",
  not_applicable: "不适用", missing: "待补充", stale: "需更新", unknown: "待核实",
};

const PATH_LABELS: Record<string, string> = {
  title: "课题名称", questions: "研究问题", hard_constraints: "硬性约束", items: "研究对象",
  fields: "证据字段", scope: "研究范围", "scope.time_range": "时间范围", "scope.inclusion": "纳入条件",
  "scope.exclusion": "排除条件", source_policy: "来源准则", allowed_domains: "允许的来源域名", queries: "建议检索词", sections: "报告结构",
  budget: "研究预算", "budget.max_search_calls": "最多检索次数", "budget.max_active_seconds": "最长研究时间",
  "budget.max_llm_tokens": "最多模型用量", stop_conditions: "停止条件",
  id: "ID", name: "名称", version: "版本", rationale: "纳入理由", label: "显示名称",
  description: "说明", evidence_requirement: "证据要求", required_access: "来源访问要求", applies_to: "适用对象",
};

const cloneSpec = (spec: ResearchSpec): ResearchSpec => structuredClone(spec);
const safeId = (prefix: string) => `${prefix}_${crypto.randomUUID().slice(0, 8)}`;

function formatValue(value: unknown): string {
  if (value === "full_text") return "需要正文";
  if (value === "snippet") return "片段可用";
  if (value === null || value === undefined || value === "") return "未设置";
  if (Array.isArray(value)) return value.length ? value.map(formatValue).join("；") : "无";
  if (typeof value === "object") return Object.entries(value as Record<string, unknown>).map(([key, item]) => `${PATH_LABELS[key] ?? key}：${formatValue(item)}`).join("；");
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
}

function pathLabel(path: string): string {
  if (PATH_LABELS[path]) return PATH_LABELS[path];
  const normalized = path.replace(/\.\d+(?=\.|$)/g, "");
  return PATH_LABELS[normalized] ?? path.split(".").map((part) => PATH_LABELS[part] ?? part).join(" / ");
}

function validateSpec(spec: ResearchSpec): string | null {
  if (!spec.title.trim()) return "请填写课题名称。";
  if (!spec.questions.some((item) => item.trim())) return "至少保留一个研究问题。";
  if (spec.fields.length === 0) return "至少保留一个证据字段。";
  const ids = [...spec.items.map((item) => item.id), ...spec.fields.map((field) => field.id)];
  if (ids.some((id) => !/^[A-Za-z0-9_-]+$/.test(id))) return "对象和字段 ID 只能包含英文、数字、下划线或短横线。";
  if (new Set(spec.items.map((item) => item.id)).size !== spec.items.length || new Set(spec.fields.map((field) => field.id)).size !== spec.fields.length) return "对象 ID 和字段 ID 不能重复。";
  const itemIds = new Set(spec.items.length ? spec.items.map((item) => item.id) : spec.questions.map((_, index) => `q${index + 1}`));
  if (spec.fields.some((field) => field.applies_to.some((itemId) => !itemIds.has(itemId)))) return "证据字段的适用对象包含不存在的 ID。";
  return null;
}

function StringList({ label, values, onChange, placeholder }: {label: string; values: string[]; onChange: (values: string[]) => void; placeholder: string}) {
  return <fieldset className="rw-list-editor"><legend>{label}</legend>
    {values.map((value, index) => <div className="rw-list-row" key={index}>
      <input aria-label={`${label} ${index + 1}`} value={value} placeholder={placeholder} onChange={(event) => onChange(values.map((item, at) => at === index ? event.target.value : item))} />
      <button type="button" className="rw-remove" aria-label={`删除${label} ${index + 1}`} onClick={() => onChange(values.filter((_, at) => at !== index))}>删除</button>
    </div>)}
    <button type="button" className="rw-add" onClick={() => onChange([...values, ""])}>添加{label}</button>
  </fieldset>;
}

function ItemsEditor({ items, onChange }: {items: ResearchItem[]; onChange: (items: ResearchItem[]) => void}) {
  const patch = (index: number, value: Partial<ResearchItem>) => onChange(items.map((item, at) => at === index ? {...item, ...value} : item));
  return <fieldset className="rw-object-editor"><legend>研究对象</legend>
    <p>留空时，将每个研究问题作为一个待核查对象。</p>
    {items.map((item, index) => <article key={`${item.id}-${index}`}>
      <div className="rw-field-grid four"><label>ID<input value={item.id} onChange={(event) => patch(index, {id: event.target.value})} /></label><label>名称<input value={item.name} onChange={(event) => patch(index, {name: event.target.value})} /></label><label>版本<input value={item.version} onChange={(event) => patch(index, {version: event.target.value})} /></label><button type="button" className="rw-remove" onClick={() => onChange(items.filter((_, at) => at !== index))}>删除对象</button></div>
      <label>纳入理由<textarea rows={2} value={item.rationale} onChange={(event) => patch(index, {rationale: event.target.value})} /></label>
    </article>)}
    <button type="button" className="rw-add" onClick={() => onChange([...items, {id: safeId("item"), name: "", version: "", rationale: ""}])}>添加研究对象</button>
  </fieldset>;
}

function FieldsEditor({ fields, onChange }: {fields: ResearchField[]; onChange: (fields: ResearchField[]) => void}) {
  const patch = (index: number, value: Partial<ResearchField>) => onChange(fields.map((field, at) => at === index ? {...field, ...value} : field));
  return <fieldset className="rw-object-editor"><legend>证据字段</legend>
    {fields.map((field, index) => <article key={`${field.id}-${index}`}>
      <div className="rw-field-grid"><label>ID<input value={field.id} onChange={(event) => patch(index, {id: event.target.value})} /></label><label>显示名称<input value={field.label} onChange={(event) => patch(index, {label: event.target.value})} /></label><button type="button" className="rw-remove" onClick={() => onChange(fields.filter((_, at) => at !== index))}>删除字段</button></div>
      <label>需要回答什么<textarea rows={2} value={field.description} onChange={(event) => patch(index, {description: event.target.value})} /></label>
      <div className="rw-field-grid three"><label>证据要求<input value={field.evidence_requirement} onChange={(event) => patch(index, {evidence_requirement: event.target.value})} /></label><label>来源访问要求<select value={field.required_access ?? "snippet"} onChange={(event) => patch(index, {required_access: event.target.value as ResearchField["required_access"]})}><option value="snippet">片段可用</option><option value="full_text">需要正文</option></select></label><label>适用对象 ID（逗号分隔，留空表示全部）<input value={field.applies_to.join(", ")} onChange={(event) => patch(index, {applies_to: event.target.value.split(",").map((item) => item.trim()).filter(Boolean)})} /></label></div>
    </article>)}
    <button type="button" className="rw-add" onClick={() => onChange([...fields, {id: safeId("field"), label: "", description: "", evidence_requirement: "", required_access: "snippet", applies_to: []}])}>添加证据字段</button>
  </fieldset>;
}

function SpecEditor({ draft, onChange }: {draft: ResearchSpec; onChange: (spec: ResearchSpec) => void}) {
  const patch = (value: Partial<ResearchSpec>) => onChange({...draft, ...value});
  return <div className="rw-spec-form">
    <label className="rw-title-field">课题名称<input value={draft.title} onChange={(event) => patch({title: event.target.value})} /></label>
    <details open><summary>研究范围</summary><div className="rw-detail-body">
      <StringList label="研究问题" values={draft.questions} placeholder="输入一个可被证据回答的问题" onChange={(questions) => patch({questions})} />
      <StringList label="硬性约束" values={draft.hard_constraints} placeholder="例如：仅纳入同行评议研究" onChange={(hard_constraints) => patch({hard_constraints})} />
      <label>时间范围<input value={draft.scope.time_range} placeholder="例如：2020 年至今" onChange={(event) => patch({scope: {...draft.scope, time_range: event.target.value}})} /></label>
      <div className="rw-field-grid two"><StringList label="纳入条件" values={draft.scope.inclusion} placeholder="应纳入的研究或资料" onChange={(inclusion) => patch({scope: {...draft.scope, inclusion}})} /><StringList label="排除条件" values={draft.scope.exclusion} placeholder="应排除的研究或资料" onChange={(exclusion) => patch({scope: {...draft.scope, exclusion}})} /></div>
    </div></details>
    <details><summary>对象与证据字段</summary><div className="rw-detail-body"><ItemsEditor items={draft.items} onChange={(items) => patch({items})} /><FieldsEditor fields={draft.fields} onChange={(fields) => patch({fields})} /></div></details>
    <details><summary>检索与交付约束</summary><div className="rw-detail-body">
      <label>来源准则<select value={draft.source_policy} onChange={(event) => patch({source_policy: event.target.value})}>
        {!['public_web', 'primary_sources', 'diverse_public_sources'].includes(draft.source_policy) && draft.source_policy && <option value={draft.source_policy}>沿用当前来源准则</option>}
        <option value="">公开 Web（默认）</option>
        <option value="public_web">公开 Web 来源</option><option value="primary_sources">优先使用一手与权威来源</option><option value="diverse_public_sources">多来源交叉核验</option>
      </select></label>
      <label>允许的来源域名（留空不限制）<input value={(draft.allowed_domains ?? []).join(", ")} placeholder="例如：docs.example.com, github.com" onChange={(event) => patch({allowed_domains: event.target.value.split(",").map((item) => item.trim()).filter(Boolean)})} /></label>
      <div className="rw-field-grid two"><StringList label="建议检索词" values={draft.queries} placeholder="检索词" onChange={(queries) => patch({queries})} /><StringList label="报告章节" values={draft.sections} placeholder="章节标题" onChange={(sections) => patch({sections})} /></div>
      <div className="rw-field-grid three"><label>最多检索次数<input type="number" min={1} value={draft.budget.max_search_calls} onChange={(event) => patch({budget: {...draft.budget, max_search_calls: Number(event.target.value)}})} /></label><label>最长研究时间（秒）<input type="number" min={1} value={draft.budget.max_active_seconds} onChange={(event) => patch({budget: {...draft.budget, max_active_seconds: Number(event.target.value)}})} /></label><label>最多模型用量<input type="number" min={1} value={draft.budget.max_llm_tokens} onChange={(event) => patch({budget: {...draft.budget, max_llm_tokens: Number(event.target.value)}})} /></label></div>
      <StringList label="停止条件" values={draft.stop_conditions} placeholder="满足什么条件后停止继续检索" onChange={(stop_conditions) => patch({stop_conditions})} />
    </div></details>
  </div>;
}

function ChangeList({ changes }: {changes: SpecChange[]}) {
  if (!changes.length) return <p className="rw-empty-inline">当前没有未保存的修改。</p>;
  return <div className="rw-change-list">{changes.map((change) => <article key={change.path}><strong>{pathLabel(change.path)}</strong><span>{formatValue(change.before)}</span><i aria-hidden>→</i><span>{formatValue(change.after)}</span></article>)}</div>;
}

function cellText(cell?: ResearchCell): string {
  if (!cell) return "尚未研究";
  return cell.value === null || cell.value === undefined || cell.value === "" ? cell.reason || CELL_LABELS[cell.status] : formatValue(cell.value);
}

function MatrixCell({ cell, item, field, selected, onSelect, onEvidence }: {
  cell?: ResearchCell;
  item: ResearchItem;
  field: ResearchField;
  selected: boolean;
  onSelect: (selected: boolean) => void;
  onEvidence: (evidenceId: string, runId?: string) => void;
}) {
  const selectable = cell?.status !== "not_applicable";
  return <div className={`rw-cell-content status-${cell?.status ?? "missing"}`}>
    <div className="rw-cell-head"><span>{CELL_LABELS[cell?.status ?? "missing"]}</span>{selectable && <input type="checkbox" aria-label={`选择 ${item.name} 的 ${field.label}`} checked={selected} onChange={(event) => onSelect(event.target.checked)} />}</div>
    <p>{cellText(cell)}</p>
    {cell?.reason && cell.value !== null && cell.value !== undefined && <small>{cell.reason}</small>}
    <div className="rw-citations">{cell?.citations.map((citation) => <button key={citation.evidence_id} onClick={() => onEvidence(citation.evidence_id, cell.origin_run_id ?? cell.run_id ?? undefined)}>{citation.locator || "查看证据"}</button>)}</div>
  </div>;
}

export function ResearchWorkbench({ studyId, onRun, onEvidence }: Props) {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("spec");
  const [draft, setDraft] = useState<ResearchSpec | null>(null);
  const [instruction, setInstruction] = useState("");
  const [selectedCells, setSelectedCells] = useState<string[]>([]);
  const [followupReason, setFollowupReason] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const studyQuery = useQuery({
    queryKey: ["research-study", studyId], queryFn: () => researchApi.study(studyId),
    refetchInterval: (query) => isResearchActive(query.state.data as ResearchStudy | undefined) ? 4000 : false,
  });
  const study = studyQuery.data;
  const matrixQuery = useQuery({
    queryKey: ["research-matrix", studyId, study?.current_revision, study?.run_status, study?.status], queryFn: () => researchApi.matrix(studyId), enabled: Boolean(study),
    refetchInterval: () => isResearchActive(study) ? 4000 : false,
  });

  useEffect(() => { if (study) setDraft(cloneSpec(study.spec)); }, [study?.current_revision, study?.fingerprint]);
  useEffect(() => { setSelectedCells([]); setError(""); setNotice(""); }, [studyId]);

  const changes = useMemo(() => study && draft ? diffResearchSpec(study.spec, draft) : [], [study, draft]);
  const serverChanges = useMemo(() => normalizeResearchDiff(study?.diff), [study?.diff]);
  const cellMap = useMemo(() => new Map(matrixQuery.data?.cells.map((cell) => [`${cell.item_id}:${cell.field_id}`, cell]) ?? []), [matrixQuery.data]);
  const approved = Boolean(study && study.approved_revision === study.current_revision && study.approved_fingerprint === study.fingerprint);

  const refreshAll = async () => {
    await Promise.all([
      queryClient.invalidateQueries({queryKey: ["research-study", studyId]}),
      queryClient.invalidateQueries({queryKey: ["research-matrix", studyId]}),
    ]);
  };
  const action = useMutation({mutationFn: async (operation: () => Promise<unknown>) => operation(), onError: (caught) => setError(researchErrorMessage(caught))});
  const runAction = (operation: () => Promise<unknown>, success: string, after?: (result: unknown) => void) => {
    setError(""); setNotice("");
    action.mutate(operation, {onSuccess: async (result) => { after?.(result); setNotice(success); await refreshAll(); }});
  };

  if (studyQuery.error) return <section className="research-workbench rw-load-error"><strong>研究工作台暂时无法载入</strong><p>{researchErrorMessage(studyQuery.error)}</p><button onClick={() => studyQuery.refetch()}>重新载入</button></section>;
  if (studyQuery.isLoading || !study || !draft) return <section className="research-workbench rw-loading" aria-live="polite">正在载入研究工作台…</section>;

  const validation = validateSpec(draft);
  const reportReady = Boolean(study.report?.complete && matrixQuery.data && matrixQuery.data.counts.missing === 0 && matrixQuery.data.counts.stale === 0 && !isResearchActive(study));
  const accepted = Boolean(study.acceptance);
  const counts = matrixQuery.data?.counts;

  const submitSpec = () => {
    if (validation) { setError(validation); return; }
    runAction(() => researchApi.reviseSpec(study, draft), "研究范围已更新，请核对变化后确认。", () => setTab("spec"));
  };
  const submitInstruction = () => {
    if (!instruction.trim()) return;
    runAction(() => researchApi.reviseInstruction(study, instruction.trim()), "修订建议已应用，请核对变化后确认。", () => setInstruction(""));
  };
  const approve = () => runAction(() => researchApi.approve(study), "已确认当前研究范围，调查已开始。", (result) => {
    const runId = (result as {run_id?: string}).run_id; if (runId) onRun(runId); setTab("matrix");
  });
  const followup = () => {
    if (!selectedCells.length) { setError("请先选择需要继续研究的证据单元。"); return; }
    if (!followupReason.trim()) { setError("请说明希望补充或核实的内容。"); return; }
    runAction(() => researchApi.followup(study, selectedCells.map((key) => { const [item_id, field_id] = key.split(":"); return {item_id, field_id}; }), followupReason.trim()), "已创建定向补充研究。", (result) => {
      const runId = (result as {run_id?: string}).run_id; if (runId) onRun(runId); setSelectedCells([]); setFollowupReason("");
    });
  };
  const accept = () => runAction(() => researchApi.accept(study), "报告已审阅并定稿。", () => setTab("report"));

  return <section className="research-workbench" aria-label="研究工作台">
    <header className="rw-header"><div><span>结构化研究</span><h2>{study.spec.title || "未命名课题"}</h2><p>第 {study.current_revision} 版 · {study.run_status ? RUN_STATUS_LABELS[study.run_status] ?? "处理中" : STATUS_LABELS[study.status] ?? "处理中"}</p></div><div className="rw-usage"><span>外部检索<b>{study.usage.external_calls}</b></span><span>发现阶段<b>{study.usage.discovery_calls}</b></span><span>模型用量<b>{study.usage.llm_tokens.toLocaleString()}</b></span></div></header>
    <nav className="rw-tabs" aria-label="研究阶段">
      <button className={tab === "spec" ? "active" : ""} onClick={() => setTab("spec")}><span>1</span>研究范围<small>{approved ? "已确认" : "待核对"}</small></button>
      <button className={tab === "matrix" ? "active" : ""} onClick={() => setTab("matrix")}><span>2</span>证据矩阵<small>{counts ? `${counts.current}/${counts.expected}` : "载入中"}</small></button>
      <button className={tab === "report" ? "active" : ""} onClick={() => setTab("report")}><span>3</span>审阅与定稿<small>{accepted ? "已定稿" : study.report ? "待审阅" : "待生成"}</small></button>
    </nav>

    {(error || notice) && <div className={`rw-feedback ${error ? "error" : "success"}`} role={error ? "alert" : "status"}>{error || notice}</div>}

    {tab === "spec" && <div className="rw-panel rw-spec-layout"><div className="rw-editor-column">
      <div className="rw-section-head"><div><h3>研究范围</h3><p>这里决定研究对象、证据要求和停止边界。</p></div><span className={approved ? "rw-badge success" : "rw-badge warning"}>{approved ? "当前范围已确认" : "确认后开始调查"}</span></div>
      <SpecEditor draft={draft} onChange={setDraft} />
      <div className="rw-save-row"><button className="rw-secondary" disabled={!changes.length || action.isPending} onClick={() => setDraft(cloneSpec(study.spec))}>撤销本地修改</button><button className="rw-primary" disabled={!changes.length || action.isPending} onClick={submitSpec}>{action.isPending ? "正在提交…" : "保存为新版本"}</button></div>
    </div><aside className="rw-review-column">
      <section><h3>用自然语言修订</h3><p>说明要增删或收紧的内容，系统会生成一版可检查的研究范围。</p><label>修订要求<textarea rows={5} value={instruction} onChange={(event) => setInstruction(event.target.value)} placeholder="例如：只研究 2023 年后的公开资料，并增加局限性字段。" /></label>{changes.length > 0 && <small className="rw-helper">请先保存或撤销左侧的本地修改。</small>}<button className="rw-secondary wide" disabled={!instruction.trim() || action.isPending || changes.length > 0} onClick={submitInstruction}>生成修订版本</button></section>
      <section><h3>本地修改</h3><ChangeList changes={changes} /></section>
      {study.diff != null && <section><h3>版本差异</h3>{serverChanges.length ? <ChangeList changes={serverChanges} /> : <p className="rw-server-diff">当前版本由上一版修订而来。请结合左侧字段逐项核对，再确认研究范围。</p>}</section>}
      <section className="rw-approval"><h3>确认当前范围</h3><p>确认这版研究范围后开始调查。</p><button className="rw-primary wide" disabled={approved || changes.length > 0 || action.isPending || isResearchActive(study)} onClick={approve}>{approved ? "当前范围已确认" : isResearchActive(study) ? "正在生成研究范围…" : changes.length ? "请先保存本地修改" : "确认并开始调查"}</button></section>
    </aside></div>}

    {tab === "matrix" && <div className="rw-panel"><div className="rw-section-head"><div><h3>证据矩阵</h3><p>每个单元格对应一个对象和一个证据要求；可选择缺口发起定向补充。</p></div>{counts && <div className="rw-counts"><span>已完成 {counts.current}</span><span>缺失 {counts.missing}</span><span>过期 {counts.stale}</span><span>待核实 {counts.unknown}</span></div>}</div>
      {matrixQuery.isLoading ? <div className="rw-empty">正在整理证据矩阵…</div> : matrixQuery.error ? <div className="rw-empty error">证据矩阵载入失败。<button onClick={() => matrixQuery.refetch()}>重试</button></div> : matrixQuery.data && (matrixQuery.data.view_mode === "questions" ? <div className="rw-question-coverage" aria-label="问题覆盖清单">{matrixQuery.data.items.map((item) => <section key={item.id} className="rw-question-card"><header><strong>{item.name}</strong>{(item.version || item.rationale) && <small>{item.version || item.rationale}</small>}</header><div className="rw-question-fields">{matrixQuery.data!.fields.map((field) => { const key = `${item.id}:${field.id}`; return <article key={field.id}><h4>{field.label}</h4><small>{field.evidence_requirement}</small><MatrixCell cell={cellMap.get(key)} item={item} field={field} selected={selectedCells.includes(key)} onSelect={(checked) => setSelectedCells(checked ? [...selectedCells, key] : selectedCells.filter((selected) => selected !== key))} onEvidence={onEvidence} /></article>; })}</div></section>)}</div> : <div className="rw-matrix-scroll"><table className="rw-matrix"><thead><tr><th scope="col">研究对象</th>{matrixQuery.data.fields.map((field) => <th scope="col" key={field.id}><strong>{field.label}</strong><small>{field.evidence_requirement}</small></th>)}</tr></thead><tbody>{matrixQuery.data.items.map((item) => <tr key={item.id}><th scope="row"><strong>{item.name}</strong><small>{item.version || item.rationale}</small></th>{matrixQuery.data!.fields.map((field) => { const key = `${item.id}:${field.id}`; return <td key={field.id}><MatrixCell cell={cellMap.get(key)} item={item} field={field} selected={selectedCells.includes(key)} onSelect={(checked) => setSelectedCells(checked ? [...selectedCells, key] : selectedCells.filter((selected) => selected !== key))} onEvidence={onEvidence} /></td>; })}</tr>)}</tbody></table></div>)}
      <div className="rw-followup"><div><strong>定向补充研究</strong><span>已选择 {selectedCells.length} 个证据单元</span></div><label>补充说明<textarea rows={2} value={followupReason} onChange={(event) => setFollowupReason(event.target.value)} placeholder="说明需要核实的争议、时间范围或证据强度。" /></label><button className="rw-primary" disabled={!selectedCells.length || !followupReason.trim() || action.isPending} onClick={followup}>开始补充研究</button></div>
    </div>}

    {tab === "report" && <div className="rw-panel rw-report-panel"><div className="rw-section-head"><div><h3>研究报告</h3><p>确认报告完整性和证据覆盖后，再定稿。</p></div>{study.report && <span className={`rw-badge ${study.report.complete ? "success" : "warning"}`}>{study.report.complete ? "完整报告" : "部分报告"}</span>}</div>
      {!study.report ? <div className="rw-empty"><strong>报告尚未生成</strong><p>研究完成并汇总证据后，报告会出现在这里。</p></div> : <><article className="rw-report"><ReactMarkdown remarkPlugins={[remarkGfm]}>{study.report.content}</ReactMarkdown></article><div className="rw-report-actions"><div><strong>{reportReady ? "可以定稿" : "暂不能定稿"}</strong><span>{reportReady ? "当前报告完整，且没有缺失或过期证据。" : "请先补齐缺失或过期证据，并等待完整报告。"}</span></div><a className="rw-secondary" href={researchApi.exportUrl(studyId)}>导出 Markdown</a><a className="rw-secondary" href={researchApi.exportUrl(studyId, "json")}>导出研究包</a><button className="rw-primary" disabled={!reportReady || accepted || action.isPending} onClick={accept}>{accepted ? "已定稿" : "确认定稿"}</button></div></>}
    </div>}
  </section>;
}
