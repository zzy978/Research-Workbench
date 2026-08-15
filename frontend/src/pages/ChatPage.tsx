import { FormEvent, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { ClarificationCard } from "../components/ClarificationCard";
import { ErrorState } from "../components/ErrorState";
import { EvidenceDrawer } from "../components/EvidenceDrawer";
import { ReportView } from "../components/ReportView";
import { RunProgress } from "../components/RunProgress";
import { SourceSelector } from "../components/SourceSelector";
import { useRunEvents } from "../hooks/useRunEvents";
import { Evidence, SourceMode, WorkflowMode } from "../types/api";

export function ChatPage({ sessionId }: {sessionId?: string}) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [source, setSource] = useState<SourceMode>(() => (localStorage.getItem("source_mode") as SourceMode) || "graphrag");
  const [workflow, setWorkflow] = useState<WorkflowMode>("deep_research");
  const [runId, setRunId] = useState<string | null>(null);
  const [selectedEvidence, setSelectedEvidence] = useState<Evidence | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [sending, setSending] = useState(false);
  const capabilities = useQuery({ queryKey: ["capabilities"], queryFn: api.capabilities });
  const detail = useQuery({ queryKey: ["session", sessionId], queryFn: () => api.session(sessionId!), enabled: Boolean(sessionId) });
  const { events, run, connection, refresh } = useRunEvents(runId);
  const evidence = useQuery({ queryKey: ["evidence", runId], queryFn: () => api.evidence(runId!), enabled: Boolean(runId), refetchInterval: run && !["completed", "failed", "cancelled", "budget_exhausted"].includes(run.status) ? 1500 : false });
  const report = useQuery({ queryKey: ["report", runId], queryFn: () => api.report(runId!), enabled: Boolean(runId), refetchInterval: run && !["completed", "failed", "cancelled", "budget_exhausted"].includes(run.status) ? 1500 : false });

  useEffect(() => { localStorage.setItem("source_mode", source); }, [source]);
  useEffect(() => {
    if (!detail.data || runId) return;
    const latest = detail.data.runs.find((item) => !["completed", "failed", "cancelled", "budget_exhausted"].includes(item.status)) ?? detail.data.runs[0];
    if (latest) setRunId(latest.run_id);
  }, [detail.data, runId]);
  useEffect(() => {
    if (run && ["completed", "failed", "cancelled", "budget_exhausted", "needs_user_input"].includes(run.status)) {
      queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["report", runId] });
      queryClient.invalidateQueries({ queryKey: ["evidence", runId] });
    }
  }, [run?.status]);

  const messageRuns = useMemo(() => new Map(detail.data?.runs.map((item) => [item.run_id, item]) ?? []), [detail.data]);
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!sessionId || !text.trim() || sending) return;
    setSending(true); setError(null);
    try {
      const result = await api.send(sessionId, { client_message_id: crypto.randomUUID(), content: text.trim(), source_mode: source, workflow_mode: workflow });
      setText(""); setRunId(result.run_id); await queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    } catch (caught) { setError(caught); } finally { setSending(false); }
  }

  if (!sessionId) return <main className="empty-chat"><div><span className="hero-mark">H</span><h1>开始一项可信研究</h1><p>创建会话，在私有知识图谱或 Web 中选择一个信息源。</p></div></main>;
  return <main className="chat-page">
    <header className="chat-header"><div><span className="eyebrow">PERSISTENT RESEARCH SESSION</span><h1>{detail.data?.title ?? "加载中…"}</h1></div><span className="connection-dot">● {connection === "reconnecting" ? "重连中" : "已连接"}</span></header>
    <section className="messages">
      {detail.data?.messages.map((message) => {
        const linked = message.run_id ? messageRuns.get(message.run_id) : undefined;
        return <div key={message.message_id} className={`message ${message.role}`}><div className="message-meta"><span>{message.role === "user" ? "你" : "Research Agent"}</span>{linked && <span className={`source-badge ${linked.source_mode}`}>{linked.source_mode === "web" ? "Web" : "私有库"}</span>}</div><div className="message-body">{message.content}</div></div>;
      })}
      <RunProgress run={run} events={events} connection={connection} />
      {run?.status === "needs_user_input" && <ClarificationCard onSubmit={async (content) => { await api.clarify(run.run_id, content); await refresh(); }} />}
      <ReportView report={report.data} evidence={evidence.data?.items ?? []} onEvidence={setSelectedEvidence} />
      <ErrorState error={error} />
    </section>
    <form className="composer" onSubmit={submit}>
      <div className="composer-tools"><SourceSelector value={source} onChange={setSource} capabilities={capabilities.data} /><select value={workflow} onChange={(event) => setWorkflow(event.target.value as WorkflowMode)}><option value="deep_research">DeepResearch</option><option value="plan_execute_report">Plan–Execute–Report</option></select></div>
      {source === "web" && capabilities.data?.sources.web.reason && <small className="capability-note">{capabilities.data.sources.web.reason}</small>}
      <textarea value={text} onChange={(event) => setText(event.target.value)} placeholder="提出一个需要证据支持的问题…" rows={3} />
      <div className="composer-actions"><span>Enter 发送 · 每个 Run 冻结当前来源</span>{run && !["completed", "failed", "cancelled", "budget_exhausted"].includes(run.status) ? <button type="button" className="stop" onClick={() => api.cancel(run.run_id).then(refresh)}>停止</button> : <button className="send" disabled={sending || !text.trim()}>{sending ? "发送中…" : "发送研究 →"}</button>}</div>
    </form>
    <EvidenceDrawer item={selectedEvidence} onClose={() => setSelectedEvidence(null)} />
  </main>;
}
