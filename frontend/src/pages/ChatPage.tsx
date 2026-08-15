import { FormEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "motion/react";
import { api } from "../api/client";
import { CanvasBoard } from "../components/CanvasBoard";
import { CardDetailDrawer } from "../components/CardDetailDrawer";
import { ClarificationCard } from "../components/ClarificationCard";
import { ErrorState } from "../components/ErrorState";
import { EvidenceDrawer } from "../components/EvidenceDrawer";
import { FloatingComposer } from "../components/FloatingComposer";
import { MessageStrip } from "../components/MessageStrip";
import { SourceSelector } from "../components/SourceSelector";
import { arrivedStages, STAGES, StageMeta, StageState, stageCardId } from "../components/stageMeta";
import { deriveStageFeed, StageFeed } from "../components/stageFeed";
import { useStagePositions } from "../hooks/useStagePositions";
import { RUN_POLL_MS, useRunEvents } from "../hooks/useRunEvents";
import { Evidence, Run, SourceMode, WorkflowMode } from "../types/api";

const TERMINAL = ["completed", "failed", "cancelled", "budget_exhausted"];
const ERROR_STATUS = ["failed", "budget_exhausted", "cancelled", "interrupted"];
/** 终态/回退阶段（本身不是画板卡片，需回溯到最后一个真实阶段） */
const NON_CARD_STAGES = ["failed", "cancelled", "budget_exhausted", "interrupted", "retrying", "replanning"];

function stageStateOf(id: string, currentStage: string | null | undefined, status: string | undefined): StageState {
  if (!status) return "pending";
  if (status === "completed") return "done";
  const current = stageCardId(currentStage);
  const currentIndex = current ? STAGES.findIndex((stage) => stage.id === current) : -1;
  const idIndex = STAGES.findIndex((stage) => stage.id === id);
  if (idIndex < currentIndex) return "done";
  if (idIndex === currentIndex) return ERROR_STATUS.includes(status) ? "error" : "busy";
  return "pending";
}

/** 执行阶段卡片：迭代轮次实时滚动列表 + 底部预算条 */
function ExecCard({ feed, run }: {feed: StageFeed; run: Run | null}) {
  const listRef = useRef<HTMLDivElement>(null);
  const running = feed.runningIteration;
  const hasLive = running != null && !feed.iterations.some((entry) => entry.index === running);
  const lastLive = hasLive ? feed.liveSearches.filter((search) => search.iteration_index === running).slice(-1)[0] : undefined;

  const rows = feed.iterations.map((entry) => ({ key: `${entry.index}-done`, label: `第 ${entry.index + 1} 轮`, sub: `${entry.queries.length} 次搜索 · ${entry.total_results} 条结果`, live: false }));
  if (hasLive && running != null) rows.push({ key: `${running}-live`, label: `第 ${running + 1} 轮`, sub: lastLive ? `正在搜索 "{lastLive.query}"` : "正在思考检索方向…", live: true });

  useEffect(() => {
    if (!listRef.current) return;
    const frame = requestAnimationFrame(() => listRef.current?.scrollTo({ top: listRef.current.scrollHeight }));
    return () => cancelAnimationFrame(frame);
  }, [rows.length, lastLive?.query]);

  const usage = run?.usage?.usage;
  const limits = run?.usage?.limits;
  const toolPct = usage?.tool_calls && limits?.max_tool_calls ? Math.min(100, Math.round((usage.tool_calls / limits.max_tool_calls) * 100)) : 0;

  return <div className="exec-card">
    <div className="iter-rows" ref={listRef}>
      {rows.length === 0 && <div className="iter-row is-empty">等待研究迭代开始…</div>}
      {rows.map((row) => (
        <div key={row.key} className={`iter-row${row.live ? " is-live" : ""}`}>
          <span className="iter-row-label">{row.label}</span>
          <span className="iter-row-sub">{row.sub}</span>
          {row.live && <span className="pulse-dot" />}
        </div>
      ))}
    </div>
    {usage && limits && usage.tool_calls != null && (
      <div className="budget-row compact">
        <span>工具调用</span>
        <span className="budget-fill-track"><i className="budget-fill" style={{ width: `${toolPct}%` }} /></span>
        <span className="budget-num">{usage.tool_calls} / {limits.max_tool_calls}</span>
      </div>
    )}
  </div>;
}

function CardSummary({ lines, meta }: {lines: string[]; meta?: string}) {
  return <div className="stage-summary">
    {lines.map((line, index) => <span key={index} className="stage-summary-line">{line}</span>)}
    {meta && <span className="stage-summary-meta">{meta}</span>}
  </div>;
}

export function ChatPage({ sessionId }: {sessionId?: string}) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [source, setSource] = useState<SourceMode>(() => (localStorage.getItem("source_mode") as SourceMode) || "graphrag");
  const [workflow, setWorkflow] = useState<WorkflowMode>("deep_research");
  const [runId, setRunId] = useState<string | null>(null);
  const [selectedEvidence, setSelectedEvidence] = useState<Evidence | null>(null);
  const [selectedStage, setSelectedStage] = useState<string | null>(null);
  const [stripCollapsed, setStripCollapsed] = useState(() => localStorage.getItem("chat.stripCollapsed") === "1");
  const [composerCollapsed, setComposerCollapsed] = useState(() => localStorage.getItem("chat.composerCollapsed") === "1");
  const [error, setError] = useState<unknown>(null);
  const [sending, setSending] = useState(false);
  const dragMovedRef = useRef(false);

  const capabilities = useQuery({ queryKey: ["capabilities"], queryFn: api.capabilities });
  const detail = useQuery({ queryKey: ["session", sessionId], queryFn: () => api.session(sessionId!), enabled: Boolean(sessionId) });
  const { events, run, connection, refresh } = useRunEvents(runId);
  const evidence = useQuery({ queryKey: ["evidence", runId], queryFn: () => api.evidence(runId!), enabled: Boolean(runId), refetchInterval: run && !TERMINAL.includes(run.status) ? RUN_POLL_MS : false });
  const report = useQuery({ queryKey: ["report", runId], queryFn: () => api.report(runId!), enabled: Boolean(runId), refetchInterval: run && !TERMINAL.includes(run.status) ? RUN_POLL_MS : false });

  useEffect(() => { localStorage.setItem("source_mode", source); }, [source]);
  useEffect(() => { localStorage.setItem("chat.stripCollapsed", stripCollapsed ? "1" : "0"); }, [stripCollapsed]);
  useEffect(() => { localStorage.setItem("chat.composerCollapsed", composerCollapsed ? "1" : "0"); }, [composerCollapsed]);
  useEffect(() => {
    if (!detail.data || runId) return;
    const latest = detail.data.runs.find((item) => !TERMINAL.includes(item.status)) ?? detail.data.runs[0];
    if (latest) setRunId(latest.run_id);
  }, [detail.data, runId]);
  useEffect(() => {
    if (run && [...TERMINAL, "needs_user_input"].includes(run.status)) {
      queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["report", runId] });
      queryClient.invalidateQueries({ queryKey: ["evidence", runId] });
    }
  }, [run?.status]);

  const messageRuns = useMemo(() => new Map(detail.data?.runs.map((item) => [item.run_id, item]) ?? []), [detail.data]);
  const feed = useMemo(() => deriveStageFeed(events, report.data), [events, report.data]);
  // 终态/回退阶段回溯到最后一个真实阶段（failed 时显示失败发生处的卡片，不渲染幻影卡）
  const effectiveStage = useMemo(() => {
    const raw = run?.current_stage;
    if (!raw || !NON_CARD_STAGES.includes(raw)) return raw;
    const last = [...events].reverse().find((event) =>
      (event.event_type === "run.stage_changed" || event.event_type === "run.started")
      && typeof event.status === "string"
      && STAGES.some((stage) => stage.id === event.status)
    );
    return last ? String(last.status) : raw;
  }, [run?.current_stage, events]);
  const arrived = useMemo(() => arrivedStages(effectiveStage), [effectiveStage]);
  const { positions, update: updatePosition } = useStagePositions(runId, arrived);

  async function submit(event: FormEvent) {
    event.preventDefault(); if (!sessionId || !text.trim() || sending) return;
    setSending(true); setError(null);
    try {
      const result = await api.send(sessionId, { client_message_id: crypto.randomUUID(), content: text.trim(), source_mode: source, workflow_mode: workflow });
      setText(""); setRunId(result.run_id); await queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    } catch (caught) { setError(caught); } finally { setSending(false); }
  }

  function cardContent(stage: StageMeta): ReactNode {
    switch (stage.id) {
      case "context_building": {
        const info = feed.contextInfo;
        return <CardSummary lines={info ? [`已复用 ${info.usedMessages} 条消息`, `语义记忆 ${info.memories} 条`] : ["构建检索上下文…"]} meta={`证据 ${feed.contextCount}`} />;
      }
      case "planning": {
        const tasks = feed.planTasks;
        return <CardSummary
          lines={tasks.length > 0 ? tasks.slice(0, 2).map((task) => `${task.task_type}: ${task.description}`) : ["正在规划研究步骤…"]}
          meta={`${tasks.length} 个任务`}
        />;
      }
      case "executing": return <ExecCard feed={feed} run={run} />;
      case "reporting": return <CardSummary
        lines={[feed.reportChars != null ? `报告已生成 · ${feed.reportChars} 字符` : "正在生成研究报告…"]}
        meta={`${feed.taskCount} 个任务完成`}
      />;
      case "verifying": {
        const done = feed.verification.filter((check) => check.passed != null).length;
        const passed = feed.verification.filter((check) => check.passed === true).length;
        const lines = done > 0 ? [`已核查 ${done} 项 · ${passed} 项通过`] : ["正在逐项验证…"];
        if (feed.verifyFailures.length > 0) lines.push(`失败 ${feed.verifyFailures.length} 项，进入修复`);
        return <CardSummary lines={lines} />;
      }
      case "completed": return <CardSummary
        lines={[feed.verifyFailures.length === 0 ? "全部验证通过，研究完成" : "研究结束"]}
        meta={`证据 ${feed.contextCount} · 工具 ${feed.tools.length}`}
      />;
    }
  }

  if (!sessionId) return <main className="empty-chat"><div><span className="hero-mark">H</span><h1>开始一项可信研究</h1><p>创建会话，在私有知识图谱或 Web 中选择一个信息源。</p></div></main>;
  return <main className="chat-page">
    <header className="chat-header">
      <div><span className="eyebrow">PERSISTENT RESEARCH SESSION</span><h1>{detail.data?.title ?? "加载中…"}</h1></div>
      <div className="chat-header-actions">
        <span className={`connection-dot${connection === "reconnecting" ? " reconnecting" : ""}`}>{connection === "reconnecting" ? "重连中" : "实时"}</span>
        <button type="button" className="icon-btn" onClick={() => setStripCollapsed((value) => !value)} title={stripCollapsed ? "展开会话消息" : "折叠会话消息"} aria-label="会话消息">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
        </button>
        <button type="button" className="icon-btn" onClick={() => setComposerCollapsed(true)} title="隐藏输入框" aria-label="隐藏输入框">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
      </div>
    </header>

    <div className="chat-body">
      <CanvasBoard
        runId={runId}
        arrived={arrived}
        positions={positions}
        onPositionChange={updatePosition}
        stageState={(id) => stageStateOf(id, effectiveStage, run?.status)}
        cardContent={cardContent}
        onSelectCard={setSelectedStage}
        dragMovedRef={dragMovedRef}
      />
      <MessageStrip collapsed={stripCollapsed} onToggle={() => setStripCollapsed((value) => !value)} messages={detail.data?.messages ?? []} runs={messageRuns} />
    </div>

    <ErrorState error={error} />
    <AnimatePresence mode="wait" initial={false}>
      {composerCollapsed ? (
        <FloatingComposer key="fab" onClick={() => setComposerCollapsed(false)} />
      ) : (
        <motion.form key="composer" className="composer" onSubmit={submit}
          initial={{ opacity: 0, y: 24 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 24, scale: 0.9 }}
          transition={{ type: "spring", stiffness: 320, damping: 26 }}
        >
          <div className="composer-tools"><SourceSelector value={source} onChange={setSource} capabilities={capabilities.data} /><select value={workflow} onChange={(event) => setWorkflow(event.target.value as WorkflowMode)}><option value="deep_research">DeepResearch</option><option value="plan_execute_report">Plan–Execute–Report</option></select></div>
          {source === "web" && capabilities.data?.sources.web.reason && <small className="capability-note">{capabilities.data.sources.web.reason}</small>}
          <textarea value={text} onChange={(event) => setText(event.target.value)} placeholder="提出一个需要证据支持的问题…" rows={3} />
          <div className="composer-actions"><span>Enter 发送 · 每个 Run 冻结当前来源</span>{run && !TERMINAL.includes(run.status) ? <button type="button" className="stop" onClick={() => api.cancel(run.run_id).then(refresh)}>停止</button> : <button className="send" disabled={sending || !text.trim()}>{sending ? "发送中…" : "发送研究 →"}</button>}</div>
        </motion.form>
      )}
    </AnimatePresence>

    {run?.status === "needs_user_input" && <ClarificationCard onSubmit={async (content) => { await api.clarify(run.run_id, content); await refresh(); }} />}
    <EvidenceDrawer item={selectedEvidence} onClose={() => setSelectedEvidence(null)} />
    <AnimatePresence>{selectedStage && run && <CardDetailDrawer stageId={selectedStage} run={run} feed={feed} report={report.data} evidence={evidence.data?.items ?? []} onEvidence={setSelectedEvidence} onClose={() => setSelectedStage(null)} />}</AnimatePresence>
  </main>;
}
