import DOMPurify from "dompurify";
import ReactMarkdown from "react-markdown";
import { Evidence, Report } from "../types/api";

export function ReportView({ report, evidence, onEvidence }: {report?: Report | null; evidence: Evidence[]; onEvidence: (item: Evidence) => void}) {
  if (!report?.content) return null;
  const sanitized = DOMPurify.sanitize(report.content, { ALLOWED_TAGS: [] });
  return <article className="report-view">
    <ReactMarkdown components={{
      a: ({href, children}) => {
        const ref = evidence.find((item) => href?.includes(item.evidence_id));
        if (ref) return <button className="citation-link" onClick={() => onEvidence(ref)}>{children}</button>;
        if (!href || !/^https?:\/\//i.test(href)) return <span>{children}</span>;
        return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
      },
      p: ({children}) => <p>{rewriteCitations(children, evidence, onEvidence)}</p>,
    }}>{sanitized}</ReactMarkdown>
    <div className="verification-row">{report.verification.map((check) => <span key={check.kind} className={check.passed ? "pass" : "fail"}>{check.passed ? "✓" : "!"} {check.kind}</span>)}</div>
  </article>;
}

function rewriteCitations(children: React.ReactNode, evidence: Evidence[], onEvidence: (item: Evidence) => void): React.ReactNode {
  if (typeof children === "string") return splitText(children, evidence, onEvidence, 0);
  return Array.isArray(children) ? children.map((child, index) => typeof child === "string" ? splitText(child, evidence, onEvidence, index) : child) : children;
}

function splitText(text: string, evidence: Evidence[], onEvidence: (item: Evidence) => void, key: number) {
  const tokens = text.split(/(\[(?:\^)?ev_[\w-]+\])/g);
  return tokens.map((token, index) => {
    const id = token.match(/ev_[\w-]+/)?.[0]; const item = evidence.find((entry) => entry.evidence_id === id);
    return item ? <button key={`${key}-${index}`} className="citation-link" onClick={() => onEvidence(item)}>{token}</button> : token;
  });
}
