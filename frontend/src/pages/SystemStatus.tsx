import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { ErrorState } from "../components/ErrorState";

export function SystemStatus() {
  const query = useQuery({queryKey: ["health"], queryFn: api.health, refetchInterval: 15_000});
  return <main className="management-page"><header><span className="eyebrow">LOCAL RUNTIME</span><h1>系统状态</h1><p>状态只显示服务可用性和是否配置，不返回任何密钥。</p></header><ErrorState error={query.error} /><div className="status-grid">{Object.entries(query.data?.components ?? {}).map(([name, value]) => <div className="status-card" key={name}><span className={`health-dot ${value.status}`} /> <strong>{name}</strong><small>{value.status}</small></div>)}</div></main>;
}
