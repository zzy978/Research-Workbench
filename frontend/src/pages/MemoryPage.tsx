import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { ErrorState } from "../components/ErrorState";

export function MemoryPage() {
  const client = useQueryClient(); const query = useQuery({queryKey: ["memories"], queryFn: api.memories});
  async function status(id: string, value: string) { await api.patchMemory(id, {status: value}); await client.invalidateQueries({queryKey: ["memories"]}); }
  return <main className="management-page"><header><span className="eyebrow">SEMANTIC MEMORY</span><h1>Memory 审阅</h1><p>候选默认不生效；确认、拒绝、过期和删除均写入持久状态。</p></header><ErrorState error={query.error} />
    <div className="data-table">{query.data?.items.length ? query.data.items.map((item) => <div className="data-row" key={String(item.memory_id)}><div><strong>{String(item.kind)} · {String(item.scope)}</strong><p>{String(item.content)}</p><small>{String(item.status)} · confidence {String(item.confidence)}</small></div><div className="row-actions"><button onClick={() => status(String(item.memory_id), "active")}>确认</button><button onClick={() => status(String(item.memory_id), "rejected")}>拒绝</button><button onClick={() => status(String(item.memory_id), "expired")}>过期</button><button className="danger" onClick={async () => { await api.deleteMemory(String(item.memory_id)); await client.invalidateQueries({queryKey: ["memories"]}); }}>删除</button></div></div>) : <div className="empty-state">当前没有 Memory。阶段 6 的合格运行提炼后会显示在这里。</div>}</div>
  </main>;
}
