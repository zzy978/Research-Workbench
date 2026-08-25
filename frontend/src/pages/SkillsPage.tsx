import { MouseEvent, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { ErrorState } from "../components/ErrorState";

type SkillRef = {kind: "candidate"; id: string} | {kind: "version"; name: string; version: string};

export function SkillsPage() {
  const client = useQueryClient();
  const query = useQuery({queryKey: ["skills"], queryFn: api.skills});
  const [selected, setSelected] = useState<SkillRef | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const detail = useQuery({
    queryKey: ["skill-detail", selected],
    queryFn: () => {
      if (!selected) throw new Error("未选择 Skill");
      return selected.kind === "candidate"
        ? api.skillCandidate(selected.id)
        : api.skill(selected.name, selected.version);
    },
    enabled: Boolean(selected),
  });

  async function refresh() {
    await Promise.all([
      client.invalidateQueries({queryKey: ["skills"]}),
      client.invalidateQueries({queryKey: ["skill-detail"]}),
    ]);
  }
  async function mutate(key: string, action: () => Promise<Record<string, unknown>>, success: string, event?: MouseEvent) {
    event?.stopPropagation(); setBusy(key); setMutationError(null); setNotice("");
    try { await action(); setNotice(success); await refresh(); }
    catch (error) { setMutationError(error); }
    finally { setBusy(null); }
  }

  return <main className="management-page skills-page"><header><span className="eyebrow">CONTROLLED EVOLUTION</span><h1>Skills Registry</h1><p>点击候选或版本查看完整 Skill；候选必须通过评测，才能由你明确启用。</p></header>
    <ErrorState error={query.error ?? mutationError} />{notice && <div className="skill-notice">{notice}</div>}
    <section className="registry-grid"><div><h2>候选</h2>{query.data?.candidates.length ? query.data.candidates.map((item) => {
      const id = String(item.candidate_id); const candidateStatus = String(item.status); const key = `candidate:${id}`;
      return <article className="registry-card clickable" key={id} onClick={() => setSelected({kind: "candidate", id})}>
        <strong>{String(item.name)} · {String(item.proposed_version)}</strong><span>{candidateStatus}</span><small>来源 Run：{String(item.run_id)}</small>
        <div className="row-actions"><button disabled={busy != null} onClick={(event) => mutate(key, () => api.evaluateSkillCandidate(id), "Skill 评测完成", event)}>{busy === key ? "评测中…" : "评测"}</button><button disabled={candidateStatus !== "evaluated" || busy != null} onClick={(event) => mutate(key, () => api.promoteSkillCandidate(id), "Skill 已启用", event)}>{busy === key ? "处理中…" : "启用"}</button></div>
      </article>;
    }) : <div className="empty-state">没有候选 Skill</div>}</div><div><h2>版本</h2>{query.data?.versions.length ? query.data.versions.map((item) => {
      const name = String(item.name); const version = String(item.version); const key = `version:${name}`;
      return <article className="registry-card clickable" key={String(item.skill_version_id)} onClick={() => setSelected({kind: "version", name, version})}>
        <strong>{name} · {version}</strong><span>{String(item.status)}</span>{item.status === "active" ? <button disabled={busy != null} onClick={(event) => mutate(key, () => api.rollbackSkill(name), "Skill 已回滚", event)}>{busy === key ? "回滚中…" : "回滚"}</button> : null}
      </article>;
    }) : <div className="empty-state">没有已注册版本</div>}</div></section>
    {selected && <aside className="skill-drawer" aria-label="Skill 详情"><header><div><span className="eyebrow">SKILL DETAIL</span><h2>{String(detail.data?.name ?? "Skill 详情")} · {String(detail.data?.version ?? "")}</h2></div><button className="icon-button" onClick={() => setSelected(null)} aria-label="关闭 Skill 详情">×</button></header>
      <div className="skill-drawer-body">{detail.isLoading && <div className="drawer-empty">正在加载 Skill…</div>}<ErrorState error={detail.error} />{detail.data && <>
        <div className="skill-detail-meta"><span>{String(detail.data.status)}</span><code>{String(detail.data.candidate_id ?? detail.data.skill_version_id ?? "")}</code></div>
        <pre>{String((detail.data.payload as Record<string, unknown> | undefined)?.content ?? detail.data.content ?? "")}</pre>
        {detail.data.evaluation ? <section><h3>最近评测</h3><pre>{JSON.stringify(detail.data.evaluation, null, 2)}</pre></section> : <div className="context-notice">尚无评测结果。</div>}
      </>}</div>
    </aside>}
  </main>;
}
