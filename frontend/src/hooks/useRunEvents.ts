import { useEffect, useRef, useState } from "react";
import { API_BASE, api } from "../api/client";
import { Run, RunEvent } from "../types/api";

const terminal = new Set(["completed", "failed", "cancelled", "budget_exhausted", "paused"]);

/** 统一的后端状态轮询间隔（毫秒） */
export const RUN_POLL_MS = 2000;

export function useRunEvents(runId?: string | null) {
  const [generation, setGeneration] = useState(0);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [connection, setConnection] = useState<"idle" | "connected" | "reconnecting">("idle");
  const cursor = useRef(0);
  const runRef = useRef<Run | null>(null); // 轮询闭包读最新状态，避免过期闭包
  const polling = useRef(false); // 在途请求守卫，防重入

  useEffect(() => {
    if (!runId) { setEvents([]); setRun(null); setConnection("idle"); runRef.current = null; return; }
    let closed = false; let source: EventSource | undefined;
    let reconnectTimer: number | undefined; let pollTimer: number | undefined;
    const sync = () => api.run(runId).then((value) => {
      if (!closed) { runRef.current = value; setRun(value); }
      return value;
    }).catch(() => undefined);
    const poll = () => {
      if (polling.current) return;
      const current = runRef.current?.status ?? "";
      if (terminal.has(current)) { window.clearInterval(pollTimer); return; }
      polling.current = true;
      api.run(runId).then((value) => {
        if (!closed) { runRef.current = value; setRun(value); }
      }).catch(() => undefined).finally(() => { polling.current = false; });
    };
    const connect = () => {
      if (closed) return;
      if (!navigator.onLine) { setConnection("reconnecting"); return; }
      source?.close();
      source = new EventSource(`${API_BASE}/runs/${runId}/events?after_event_id=${cursor.current}`);
      source.onopen = () => setConnection("connected");
      const handle = (message: MessageEvent) => {
        const data = JSON.parse(message.data) as RunEvent;
        if (typeof data.event_id === "number") cursor.current = Math.max(cursor.current, data.event_id);
        setEvents((items) => items.some((item) => item.event_id === data.event_id) ? items : [...items, data]);
        if (["run.", "research."].some((prefix) => String(data.event_type).startsWith(prefix))) sync();
      };
      ["run.queued", "run.started", "run.stage_changed", "run.pause_requested", "run.paused", "run.resumed", "run.completed", "run.failed", "run.cancelled", "run.budget_exhausted", "run.needs_user_input", "plan.created", "plan.revised", "task.started", "task.completed", "task.failed", "tool.completed", "tool.failed", "evidence.added", "report.completed", "verification.completed", "agent.progress", "iteration.completed", "research.discovery_unavailable", "research.outline_ready", "research.scope_approved", "research.cell_updated", "research.search_requested", "research.cache_hit", "research.request_started", "research.search_completed", "research.review_ready", "research.accepted"].forEach((name) => source?.addEventListener(name, handle));
      source.onerror = () => {
        if (closed) return;
        source?.close(); setConnection("reconnecting");
        sync().then((value) => {
          if (closed) return;
          if (terminal.has(value?.status ?? "")) setConnection("idle");
          else if (!closed && navigator.onLine) reconnectTimer = window.setTimeout(connect, 1_000);
        });
      };
    };
    const handleOffline = () => {
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      reconnectTimer = undefined;
      source?.close(); setConnection("reconnecting");
    };
    const handleOnline = () => {
      if (closed) return;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      sync(); connect();
    };
    window.addEventListener("offline", handleOffline);
    window.addEventListener("online", handleOnline);
    setEvents([]); cursor.current = 0; runRef.current = null; sync();
    if (navigator.onLine) connect(); else setConnection("reconnecting");
    pollTimer = window.setInterval(poll, RUN_POLL_MS);
    return () => {
      closed = true; source?.close();
      window.removeEventListener("offline", handleOffline);
      window.removeEventListener("online", handleOnline);
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      if (pollTimer) window.clearInterval(pollTimer);
    };
  }, [runId, generation]);

  return {
    events, run, connection,
    refresh: () => runId ? api.run(runId).then((value) => { runRef.current = value; setRun(value); }) : Promise.resolve(),
    restart: () => setGeneration((value) => value + 1),
  };
}
