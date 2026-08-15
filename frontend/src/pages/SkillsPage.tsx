import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { ErrorState } from "../components/ErrorState";

export function SkillsPage() {
  const client = useQueryClient(); const query = useQuery({queryKey: ["skills"], queryFn: api.skills});
  const refresh = () => client.invalidateQueries({queryKey: ["skills"]});
  return <main className="management-page"><header><span className="eyebrow">CONTROLLED EVOLUTION</span><h1>Skills Registry</h1><p>候选先评测，再由你明确启用；active 版本可回滚。</p></header><ErrorState error={query.error} />
    <section className="registry-grid"><div><h2>候选</h2>{query.data?.candidates.length ? query.data.candidates.map((item) => <div className="registry-card" key={String(item.candidate_id)}><strong>{String(item.name)} · {String(item.proposed_version)}</strong><span>{String(item.status)}</span><small>来源 Run：{String(item.run_id)}</small><div className="row-actions"><button onClick={async () => { await api.evaluateSkill(String(item.name), String(item.proposed_version)); await refresh(); }}>评测</button><button disabled={item.status !== "evaluated"} onClick={async () => { await api.promoteSkill(String(item.name), String(item.proposed_version)); await refresh(); }}>启用</button></div></div>) : <div className="empty-state">没有候选 Skill</div>}</div><div><h2>版本</h2>{query.data?.versions.length ? query.data.versions.map((item) => <div className="registry-card" key={String(item.skill_version_id)}><strong>{String(item.name)} · {String(item.version)}</strong><span>{String(item.status)}</span>{item.status === "active" ? <button onClick={async () => { await api.rollbackSkill(String(item.name)); await refresh(); }}>回滚</button> : null}</div>) : <div className="empty-state">没有已注册版本</div>}</div></section>
  </main>;
}
