import { NavLink } from "react-router-dom";
import { Session } from "../types/api";

export function SessionSidebar({ sessions, selected, onSelect, onCreate, onArchive, onRename }: {sessions: Session[]; selected?: string; onSelect: (id: string) => void; onCreate: () => void; onArchive: (id: string) => void; onRename: (session: Session) => void}) {
  return <aside className="sidebar">
    <div className="brand"><span className="brand-mark">H</span><div><strong>Hermes Research</strong><small>Local MVP</small></div></div>
    <button className="new-session" onClick={onCreate}>＋ 新建会话</button>
    <div className="sidebar-label">研究会话</div>
    <div className="session-list">{sessions.map((session) => <div key={session.session_id} className={`session-row ${selected === session.session_id ? "selected" : ""}`}>
      <button onClick={() => onSelect(session.session_id)}><span>{session.title}</span><small>{session.status === "archived" ? "已归档" : new Date(session.updated_at).toLocaleDateString()}</small></button>
      {session.status === "active" && <><button className="archive" onClick={() => onRename(session)} title="重命名">✎</button><button className="archive" onClick={() => onArchive(session.session_id)} title="归档">⌁</button></>}
    </div>)}</div>
    <nav><NavLink to="/">⌁ 聊天研究</NavLink><NavLink to="/memories">◇ Memory</NavLink><NavLink to="/skills">△ Skills</NavLink><NavLink to="/status">○ 系统状态</NavLink></nav>
  </aside>;
}
