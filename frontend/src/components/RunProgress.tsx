import { Run, RunEvent } from "../types/api";

const stages = ["context_building", "planning", "executing", "reporting", "verifying", "completed"];
const labels: Record<string, string> = {context_building: "上下文", planning: "规划", executing: "执行", reporting: "报告", verifying: "验证", completed: "完成"};

export function RunProgress({ run, events, connection }: {run: Run | null; events: RunEvent[]; connection: string}) {
  if (!run) return null;
  const current = stages.indexOf(run.status === "replanning" || run.status === "retrying" ? "executing" : run.status);
  const evidenceCount = events.filter((event) => event.event_type === "evidence.added").length;
  const taskCount = events.filter((event) => event.event_type === "task.completed").length;
  return <section className="progress-card">
    <div className="progress-head"><strong>运行进度</strong><span className={`status status-${run.status}`}>{run.status}</span><small>{connection === "reconnecting" ? "正在重连…" : "实时连接"}</small></div>
    <div className="stage-track">{stages.map((stage, index) => <div key={stage} className={index <= current ? "active" : ""}><i />{labels[stage]}</div>)}</div>
    <div className="progress-meta"><span>已完成任务 {taskCount}</span><span>证据 {evidenceCount}</span><span>{run.source_mode === "web" ? "Web" : "私有库"}</span></div>
    {run.error_message && <div className="inline-error">{run.error_code ? `${run.error_code}: ` : ""}{run.error_message}</div>}
  </section>;
}
