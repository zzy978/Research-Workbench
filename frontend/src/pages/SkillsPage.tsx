import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { ErrorState } from "../components/ErrorState";

export function SkillsPage() {
  const query = useQuery({queryKey: ["skills"], queryFn: api.skills});
  return <main className="management-page"><header><span className="eyebrow">CONTROLLED EVOLUTION</span><h1>Skills Registry</h1><p>仅展示持久候选和版本。评测、Promote 与 Rollback 必须等阶段 7 的硬门禁接入后开放。</p></header><ErrorState error={query.error} />
    <section className="registry-grid"><div><h2>候选</h2>{query.data?.candidates.length ? query.data.candidates.map((item) => <div className="registry-card" key={String(item.candidate_id)}><strong>{String(item.name)} · {String(item.proposed_version)}</strong><span>{String(item.status)}</span><small>来源 Run：{String(item.run_id)}</small></div>) : <div className="empty-state">没有候选 Skill</div>}</div><div><h2>版本</h2>{query.data?.versions.length ? query.data.versions.map((item) => <div className="registry-card" key={String(item.skill_version_id)}><strong>{String(item.name)} · {String(item.version)}</strong><span>{String(item.status)}</span></div>) : <div className="empty-state">没有已注册版本</div>}</div></section>
  </main>;
}
