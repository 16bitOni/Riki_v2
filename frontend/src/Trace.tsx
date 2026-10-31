import { useState } from "react";
import { ApprovalReq, Span, seconds, shortModel } from "./api";

// The run as a tree: agents contain model calls and tools; spawn_agent tools contain sub-agents.

const ms = (s: Span) =>
  s.ended_at ? Math.max(0, new Date(s.ended_at).getTime() - new Date(s.started_at).getTime()) : null;

const KIND: Record<Span["kind"], string> = { agent: "agent", llm: "model", tool: "step", memory: "memory" };

export const pretty = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v, null, 2));

export function TraceTree(props: {
  spans: Span[];
  approvals: ApprovalReq[];
  focus?: string | null;
}) {
  const children = new Map<string | null, Span[]>();
  for (const s of props.spans) {
    const key = s.parent_id && props.spans.some((p) => p.id === s.parent_id) ? s.parent_id : null;
    children.set(key, [...(children.get(key) || []), s]);
  }
  for (const list of children.values()) list.sort((a, b) => a.started_at.localeCompare(b.started_at));
  const roots = children.get(null) || [];
  if (!roots.length) return <p className="muted">No trace yet.</p>;
  return (
    <ul className="tree">
      {roots.map((s) => (
        <Node key={s.id} span={s} kids={children} approvals={props.approvals} focus={props.focus} />
      ))}
    </ul>
  );
}

function Node(props: {
  span: Span;
  kids: Map<string | null, Span[]>;
  approvals: ApprovalReq[];
  focus?: string | null;
}) {
  const { span } = props;
  const focused = !!props.focus && (span.ref === props.focus || span.id === props.focus);
  const [open, setOpen] = useState(false);
  const pending = props.approvals.find((a) => a.span_id === span.id && !a.status);
  const kids = props.kids.get(span.id) || [];
  const status = span.status || "running";
  const dur = ms(span);
  const tokens = (span.tokens_in || 0) + (span.tokens_out || 0);
  return (
    <li className={`node ${span.kind} ${status} ${focused ? "focused" : ""}`}>
      <button className="node-head" onClick={() => setOpen(!open)} aria-expanded={open || focused}>
        <span className={`badge ${span.kind}`}>{span.kind === "agent" && span.parent_id ? "helper" : KIND[span.kind]}</span>
        {span.ref && <span className="ref">{span.ref}</span>}
        <span className="name">{span.kind === "llm" ? shortModel(span.name) : span.name}</span>
        {typeof span.meta?.risk === "string" && span.meta.risk !== "safe" && (
          <span className="risk">{span.meta.risk}</span>
        )}
        <span className={`status ${pending ? "waiting" : status}`}>{pending ? "waiting for you" : status}</span>
        {dur !== null && <span className="dim">{seconds(dur)}</span>}
        {tokens > 0 && <span className="dim">{tokens.toLocaleString()} tokens</span>}
        {!!span.tokens_cached && <span className="dim">{span.tokens_cached.toLocaleString()} cached</span>}
      </button>
      {(open || focused) && (
        <div className="node-body">
          {span.error && <pre className="err">{span.error}</pre>}
          {span.input !== undefined && span.input !== null && (
            <>
              <div className="label">Input</div>
              <pre>{pretty(span.input)}</pre>
            </>
          )}
          {span.output !== undefined && span.output !== null && (
            <>
              <div className="label">Output</div>
              <pre>{pretty(span.output)}</pre>
            </>
          )}
        </div>
      )}
      {kids.length > 0 && (
        <ul className="tree">
          {kids.map((k) => (
            <Node key={k.id} span={k} kids={props.kids} approvals={props.approvals} focus={props.focus} />
          ))}
        </ul>
      )}
    </li>
  );
}
