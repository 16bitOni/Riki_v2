// The three status layers. Layer 1: one sentence (liveLine). Layer 2: the steps under a reply (Steps).
// Layer 3: the full trace with numbers (RunDrawer). Same data, three depths.

import { Check, ChevronDown, CircleAlert, Minus, X } from "lucide-react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApprovalReq, RunRow, Span, dollars, post, seconds, shortModel } from "./api";
import { TraceTree, pretty } from "./Trace";

const arg = (s: Span, key: string) => {
  const v = (s.input as Record<string, unknown> | null)?.[key];
  return typeof v === "string" ? v : "";
};
const clip = (t: string, n = 60) => (t.length > n ? t.slice(0, n - 1) + "…" : t);

/** A tool step in words a person would use. `live` gives the -ing form. */
export function describe(s: Span, live = false): string {
  const n = s.name;
  const pick = (doing: string, done: string) => (live ? doing : done);
  if (s.kind === "memory") return pick("Remembering what I know about this", "Recalled related memories");
  if (s.kind === "llm") return pick("Thinking", "Thought it through");
  if (s.kind === "agent") return pick(`Helper working: ${clip(n.replace(/^sub-agent: /, ""))}`, `Helper: ${clip(n.replace(/^sub-agent: /, ""))}`);
  if (n === "web_search") return pick(`Searching the web for “${clip(arg(s, "query"), 40)}”`, `Searched the web for “${clip(arg(s, "query"), 40)}”`);
  if (n === "fetch_url") return pick(`Reading ${clip(arg(s, "url"), 45)}`, `Read ${clip(arg(s, "url"), 45)}`);
  if (n === "recall_memory") return pick("Checking my memory", `Looked up “${clip(arg(s, "query"), 40)}” in memory`);
  if (n === "remember") return pick("Noting that down", `Remembered: ${clip(arg(s, "fact"), 50)}`);
  if (n === "current_time") return pick("Checking the time", "Checked the time");
  if (n === "load_tools") return pick("Picking up a tool", "Picked up a tool");
  if (n === "spawn_agent") return pick(`Asking a helper to ${clip(arg(s, "task"), 45)}`, `Asked a helper to ${clip(arg(s, "task"), 45)}`);
  if (n.startsWith("filesystem_")) {
    const path = arg(s, "path") || arg(s, "source");
    return pick("Working with your files", `Files: ${n.slice(11).replace(/_/g, " ")}${path ? ` ${clip(path, 50)}` : ""}`);
  }
  if (n.startsWith("browser_")) return pick("Using the browser", `Browser: ${n.slice(8).replace(/_/g, " ")}`);
  return pick(`Using ${n}`, `Used ${n}`);
}

/** Layer 1: what is happening right now, in one sentence. */
export function liveLine(spans: Span[], approvals: ApprovalReq[], agentName: string): string {
  if (approvals.some((a) => !a.status)) return "Waiting for your OK";
  const running = spans.filter((s) => s.status === "running");
  const tool = [...running].reverse().find((s) => s.kind === "tool" && s.name !== "spawn_agent");
  if (tool) return describe(tool, true);
  const helpers = running.filter((s) => s.kind === "agent" && s.parent_id);
  if (helpers.length > 1) return `${helpers.length} helpers working in parallel`;
  if (helpers.length === 1) return describe(helpers[0], true);
  const last = running[running.length - 1];
  return last ? describe(last, true) : `${agentName} is thinking`;
}

const ICON: Record<string, JSX.Element | null> = {
  ok: <Check size={13} strokeWidth={2.5} />,
  error: <CircleAlert size={13} />,
  denied: <Minus size={13} strokeWidth={2.5} />,
  running: null,
};

/** Layer 2: the steps behind a reply, readable without knowing what a span is. */
export function Steps({ spans, latency, cost, onDetails, defaultOpen = false }: { spans: Span[]; latency?: number | null; cost?: number | null; onDetails: () => void; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const agents = new Set(spans.filter((s) => s.kind === "agent").map((s) => s.id));
  const depthOf = (s: Span) => {
    let d = 0;
    let p = spans.find((x) => x.id === s.parent_id);
    while (p) {
      if (p.kind === "agent" && p.parent_id) d++;
      p = spans.find((x) => x.id === p!.parent_id);
    }
    return d;
  };
  const steps = spans.filter((s) => s.kind === "tool" || s.kind === "memory");
  const helpers = spans.filter((s) => s.kind === "agent" && s.parent_id).length;
  const problems = steps.filter((s) => s.status === "error" || s.status === "denied").length;
  if (!steps.length && agents.size === 0) return null;
  // One quiet line; the detail is one click away.
  const head = latency == null ? "Working" : `Worked for ${seconds(latency)}${cost != null ? ` · ${dollars(cost)}` : ""}`;
  const extra = [helpers ? `with ${helpers} helper${helpers === 1 ? "" : "s"}` : "", problems ? `${problems} not done` : ""]
    .filter(Boolean)
    .join(", ");
  return (
    <div className="steps">
      <button className="steps-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span>
          {head}
          {extra && `, ${extra}`}
        </span>
        <ChevronDown size={14} className={`chev ${open ? "up" : ""}`} aria-hidden="true" />
      </button>
      {open && (
        <ol className="step-list">
          {steps.map((s) => (
            <li key={s.id} className={s.status || "running"} style={{ paddingLeft: 12 * depthOf(s) }}>
              <span className="step-icon" aria-label={s.status}>
                {ICON[s.status || "running"]}
              </span>
              {s.ref && <span className="step-ref">{s.ref.slice(1)}</span>}
              <span>{describe(s)}</span>
              {s.status === "denied" && <em>not allowed</em>}
              {s.status === "error" && <em>failed</em>}
            </li>
          ))}
          <li className="step-more">
            <button className="link" onClick={onDetails}>
              See how this was made
            </button>
          </li>
        </ol>
      )}
    </div>
  );
}

/** Markdown reply with citation chips: [t3] and [m:abcd1234] become buttons that open the evidence. */
export function Reply({ text, onCite }: { text: string; onCite?: (ref: string) => void }) {
  const linked = text.replace(/\[(t\d+|m:[0-9a-f]{8})\]/g, (_, ref) => `[${ref}](#cite-${ref})`);
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a({ href, children }) {
            if (href?.startsWith("#cite-")) {
              const ref = href.slice(6);
              const memory = ref.startsWith("m:");
              return (
                <button
                  className={`cite ${memory ? "memory" : ""}`}
                  onClick={() => onCite?.(ref)}
                  title={memory ? "From memory: show where" : `Evidence from step ${ref.slice(1)}`}
                >
                  {memory ? "M" : ref.slice(1)}
                </button>
              );
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {linked}
      </ReactMarkdown>
    </div>
  );
}

/** Layer 3: everything about one run. */
export function RunDrawer(props: {
  run: Partial<RunRow> | null;
  spans: Span[];
  approvals: ApprovalReq[];
  focus: string | null;
  onClose: () => void;
}) {
  const { run } = props;
  const tokens = (run?.tokens_in || 0) + (run?.tokens_out || 0);
  const bad = run?.checks?.invalid_citations || [];
  const llm = props.spans.filter((s) => s.kind === "llm");
  return (
    <aside className="drawer" aria-label="Run details">
      <div className="sheet-grip" aria-hidden="true" />
      <div className="drawer-head">
        <h2>How I got there</h2>
        <button className="icon" onClick={props.onClose} aria-label="Close details">
          <X size={18} />
        </button>
      </div>
      {run && (
        <dl className="facts">
          <div>
            <dt>Status</dt>
            <dd className={`tone-${run.status || "running"}`}>{run.status || "running"}</dd>
          </div>
          {run.latency_ms != null && (
            <div>
              <dt>Time</dt>
              <dd>{seconds(run.latency_ms)}</dd>
            </div>
          )}
          <div>
            <dt>Tokens</dt>
            <dd>
              {tokens.toLocaleString()}
              {run.tokens_cached ? <small> {run.tokens_cached.toLocaleString()} cached</small> : null}
            </dd>
          </div>
          {run.cost_usd != null && (
            <div>
              <dt>Cost</dt>
              <dd>{run.cost_usd >= 1 ? `$${run.cost_usd.toFixed(2)}` : `$${run.cost_usd.toFixed(4)}`}</dd>
            </div>
          )}
          <div>
            <dt>Model calls</dt>
            <dd>{llm.length}</dd>
          </div>
          {run.model && (
            <div>
              <dt>Model</dt>
              <dd>{shortModel(run.model)}</dd>
            </div>
          )}
          {run.checks?.unsupported && (
            <div>
              <dt>Fact check</dt>
              <dd className={run.checks.unsupported.length ? "tone-error" : "tone-ok"}>
                {run.checks.unsupported.length ? `${run.checks.unsupported.length} claim(s) not in the sources` : "every claim found in the sources"}
              </dd>
            </div>
          )}
          {run.prompt_version && (
            <div>
              <dt>Prompt</dt>
              <dd>
                <code>{run.prompt_version}</code>
              </dd>
            </div>
          )}
        </dl>
      )}
      {bad.length > 0 && (
        <p className="notice error">
          This reply cites {bad.join(", ")}, but no step produced that. Treat those claims as unverified.
        </p>
      )}
      {!!run?.checks?.unsupported?.length && (
        <div className="notice error">
          Not found in what Riki read:
          <ul>{run.checks.unsupported.map((c) => <li key={c}>{c}</li>)}</ul>
        </div>
      )}
      {run?.error && <p className="notice error">{run.error}</p>}
      <TraceTree spans={props.spans} approvals={props.approvals} focus={props.focus} />
    </aside>
  );
}

const RISK_NOTE: Record<string, string> = {
  write: "This changes something outside the chat, so it needs your OK.",
  external: "This would reach another person. It is always blocked.",
  money: "This moves money, so it always asks, every time.",
};


/** What an action will do, as the end of "Riki wants to …". */
function intent(tool: string, args: Record<string, unknown>): string {
  const str = (k: string) => (typeof args[k] === "string" ? (args[k] as string) : "");
  const verbs: Record<string, string> = {
    write_file: `save a file to ${str("path")}`,
    edit_file: `edit ${str("path")}`,
    create_directory: `create the folder ${str("path")}`,
    move_file: `move ${str("source")} to ${str("destination")}`,
  };
  const bare = tool.replace(/^(filesystem|browser|github)_/, "");
  return verbs[bare] ?? `${bare.replace(/_/g, " ")}${str("path") ? ` ${str("path")}` : str("url") ? ` ${str("url")}` : ""}`;
}

/** An action waiting on you. Used inline in the chat and in the inbox. */
export function ApprovalCard({ approval, agentName, onDecided, compact }: {
  approval: ApprovalReq;
  agentName: string;
  onDecided?: (approved: boolean) => void;
  compact?: boolean; // the Island: no context line, details on demand
}) {
  const [state, setState] = useState<"open" | "busy" | "allowed" | "denied">("open");
  const [showArgs, setShowArgs] = useState(false);
  const action = intent(approval.tool, (approval.args ?? {}) as Record<string, unknown>);
  const [error, setError] = useState("");
  const asking = approval.tool === "ask_user";
  const question = asking ? ((approval.args ?? {}) as { question?: string; options?: string[] }) : null;
  const decide = async (approve: boolean, extra: { grant?: boolean; answer?: string } = {}) => {
    setState("busy");
    try {
      await post(`/approvals/${approval.id}`, { approve, ...extra });
      setState(approve ? "allowed" : "denied");
      onDecided?.(approve);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setState("open");
    }
  };
  if (question) {
    return (
      <div className={`approval ${compact ? "compact" : ""}`} role="group" aria-label="A question from Riki">
        <p className="approval-title">{question.question}</p>
        {state === "allowed" || state === "denied" ? (
          <p className={`approval-done ${state}`}>{state === "allowed" ? "Answered" : "Skipped"}</p>
        ) : (
          <div className="row wrap">
            {(question.options ?? []).map((o) => (
              <button key={o} className="primary" disabled={state === "busy"} onClick={() => decide(true, { answer: o })}>
                {o}
              </button>
            ))}
            <button disabled={state === "busy"} onClick={() => decide(false)}>
              Skip
            </button>
          </div>
        )}
        {error && <p className="notice error">{error}</p>}
      </div>
    );
  }
  return (
    <div className={`approval ${compact ? "compact" : ""}`} role="group" aria-label={`Approval for ${approval.tool}`}>
      <p className="approval-title">
        {agentName} wants to <b>{action.charAt(0).toLowerCase() + action.slice(1)}</b>
      </p>
      {!compact && approval.request && <p className="approval-context">You asked: “{approval.request}”</p>}
      {!compact && <p className="approval-note">{RISK_NOTE[approval.money ? "money" : approval.risk || "write"]}</p>}
      {compact && approval.money && <p className="approval-note">{RISK_NOTE.money}</p>}
      <button className="link inline small" onClick={() => setShowArgs(!showArgs)} aria-expanded={showArgs}>
        {showArgs ? "Hide details" : "Show exactly what it will do"}
      </button>
      {showArgs && <pre>{pretty(approval.args)}</pre>}
      {state === "allowed" || state === "denied" ? (
        <p className={`approval-done ${state}`}>{state === "allowed" ? "Allowed" : "Denied"}</p>
      ) : (
        <div className="row">
          <button className="primary" disabled={state === "busy"} onClick={() => decide(true)}>
            Allow
          </button>
          <button disabled={state === "busy"} onClick={() => decide(false)}>
            Deny
          </button>
          {!approval.money && approval.thread_id && (
            <button className="link small" disabled={state === "busy"} onClick={() => decide(true, { grant: true })}
                    title="Saves tapping through a long job. This chat only, never for anything that moves money.">
              Allow for 30 min
            </button>
          )}
        </div>
      )}
      {error && <p className="notice error">{error}</p>}
    </div>
  );
}
