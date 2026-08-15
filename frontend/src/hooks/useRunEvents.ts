import { useEffect, useRef, useState } from "react";
import { API_BASE, api } from "../api/client";
import { Run, RunEvent } from "../types/api";

const terminal = new Set(["completed", "failed", "cancelled", "budget_exhausted"]);

export function useRunEvents(runId?: string | null) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [connection, setConnection] = useState<"idle" | "connected" | "reconnecting">("idle");
  const cursor = useRef(0);

  useEffect(() => {
    if (!runId) { setEvents([]); setRun(null); setConnection("idle"); return; }
    let closed = false; let source: EventSource | undefined; let timer: number | undefined;
    const sync = () => api.run(runId).then((value) => { setRun(value); return value; }).catch(() => undefined);
    const connect = () => {
      if (closed) return;
      source = new EventSource(`${API_BASE}/runs/${runId}/events?after_event_id=${cursor.current}`);
      source.onopen = () => setConnection("connected");
      const handle = (message: MessageEvent) => {
        const data = JSON.parse(message.data) as RunEvent;
        if (typeof data.event_id === "number") cursor.current = Math.max(cursor.current, data.event_id);
        setEvents((items) => items.some((item) => item.event_id === data.event_id) ? items : [...items, data]);
        if (String(data.event_type).startsWith("run.")) sync();
      };
      ["run.queued", "run.started", "run.completed", "run.failed", "run.cancelled", "run.budget_exhausted", "run.needs_user_input", "plan.created", "plan.revised", "task.completed", "tool.completed", "evidence.added", "report.completed", "verification.completed"].forEach((name) => source?.addEventListener(name, handle));
      source.onerror = () => {
        source?.close(); setConnection("reconnecting");
        sync().then((value) => { if (!closed && !terminal.has(value?.status ?? "")) timer = window.setTimeout(connect, 1_000); });
      };
    };
    setEvents([]); cursor.current = 0; sync(); connect();
    return () => { closed = true; source?.close(); if (timer) window.clearTimeout(timer); };
  }, [runId]);

  return { events, run, connection, refresh: () => runId ? api.run(runId).then(setRun) : Promise.resolve() };
}
