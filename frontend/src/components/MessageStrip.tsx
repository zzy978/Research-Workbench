import { motion } from "motion/react";
import { Message, Run } from "../types/api";

interface MessageStripProps {
  collapsed: boolean;
  onToggle: () => void;
  messages: Message[];
  runs: Map<string, Run>;
}

/** 可折叠会话消息条：默认单行摘要，展开后最多 240px 高度滚动列表 */
export function MessageStrip({ collapsed, onToggle, messages, runs }: MessageStripProps) {
  return <section className="msg-strip">
    <button type="button" className="msg-strip-toggle" onClick={onToggle} aria-expanded={!collapsed}>
      <span className="msg-strip-icon">{collapsed ? "☰" : "▾"}</span>
      <span>会话消息 {messages.length} 条</span>
      <span className="msg-strip-hint">{collapsed ? "点击展开" : "点击收起"}</span>
    </button>
    <motion.div
      className="msg-strip-list"
      initial={false}
      animate={{ height: collapsed ? 0 : 240 }}
      transition={{ type: "spring", stiffness: 260, damping: 30 }}
    >
      <div className="msg-strip-inner">
        {messages.length === 0 && <div className="msg-strip-empty">暂无消息</div>}
        {messages.map((message) => {
          const linked = message.run_id ? runs.get(message.run_id) : undefined;
          return <div key={message.message_id} className={`message ${message.role} is-strip`}>
            <div className="message-meta">
              <span>{message.role === "user" ? "你" : "Research Agent"}</span>
              {linked && <span className={`source-badge ${linked.source_mode}`}>{linked.source_mode === "web" ? "Web" : "私有库"}</span>}
            </div>
            <div className="message-body">{message.content}</div>
          </div>;
        })}
      </div>
    </motion.div>
  </section>;
}
