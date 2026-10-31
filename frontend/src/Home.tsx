// Home: one windshield for the day. Activity: everything done on your behalf.

import { ArrowUp, Brain, Check, CircleAlert, History, Minus, Plus, Search, ShieldCheck, Trash2 } from "lucide-react";
import { FormEvent, useRef, useState } from "react";
import { Reply, describe } from "./Activity";
import { Me, Span, Thread, Today, ago } from "./api";
import { Mood } from "./Avatar";
import { Page, fmtDay, useLoad } from "./Pages";
import { Skeleton, greeting, useAutoGrow } from "./ui";

export function ThreadRow({ t, on, onOpen, onDelete }: { t: Thread; on?: boolean; onOpen: () => void; onDelete?: () => void }) {
  return (
    <div className="card-row-wrap">
      <button className={`card-row ${on ? "on" : ""}`} onClick={onOpen}>
        <span className="card-row-top">
          <span className="card-row-title">{t.kind === "main" ? "Main chat" : t.title}</span>
          <time>{ago(t.last_at)}</time>
        </span>
        {t.preview && (
          <span className="card-row-preview">
            {t.preview_role === "user" ? "You: " : ""}
            {t.preview}
          </span>
        )}
      </button>
      {onDelete && (
        <button className="icon small card-row-del" aria-label={t.kind === "main" ? "Clear this chat" : "Delete this chat"} title={t.kind === "main" ? "Clear" : "Delete"} onClick={onDelete}>
          <Trash2 size={14} />
        </button>
      )}
    </div>
  );
}

export function HomePage(props: {
  me: Me;
  mood: Mood;
  onAsk: (text: string, newChat?: boolean) => void;
  onOpenThread: (id: string) => void;
  onDeleteThread: (id: string) => Promise<void>;
  onGo: (section: "Approvals" | "Memory" | "Settings") => void;
}) {
  const { me } = props;
  const [data, error, reload] = useLoad<Today>("/today", [], 15000);
  const [draft, setDraft] = useState("");
  const [fresh, setFresh] = useState(false); // start a new chat instead of continuing the main one
  const input = useRef<HTMLTextAreaElement>(null);
  useAutoGrow(input, draft);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (draft.trim()) props.onAsk(draft.trim(), fresh);
  };
  const date = new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });

  // Suggestions come from your own history first, then from what you said you use Riki for.
  const chips: { label: string; run: () => void }[] = [];
  if (data?.needs_you.length) chips.push({ label: `Review ${data.needs_you.length} waiting for you`, run: () => props.onGo("Approvals") });
  const lastSide = data?.continue.find((t) => t.kind === "side");
  if (lastSide) chips.push({ label: `Pick up “${lastSide.title.slice(0, 40)}”`, run: () => props.onOpenThread(lastSide.id) });
  if (data?.learned.count) chips.push({ label: "What did you learn about me today?", run: () => props.onAsk("What did you learn about me today?") });
  if (data && !data.nudge) chips.push({ label: "Anything I should be on top of?", run: () => props.onAsk("Anything I should be on top of today?") });
  for (const s of data?.suggest ?? []) chips.push({ label: s, run: () => props.onAsk(s) });

  return (
    <div className="page home">
      <div className="page-inner">
        <header className="home-head">
          <p className="home-date">{date}</p>
          <h1>
            {greeting()}, {me.name}.
          </h1>
        </header>

        <form className="composer home-composer" onSubmit={submit}>
          <textarea
            ref={input}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) submit(e);
            }}
            placeholder={`Ask ${me.agent_name} anything`}
            rows={1}
            aria-label={`Ask ${me.agent_name}`}
          />
          <button
            type="button"
            className={`icon composer-tool ${fresh ? "on" : ""}`}
            aria-pressed={fresh}
            aria-label={fresh ? "Sending to a new chat" : "Send to the main chat"}
            title={fresh ? "Starts a new chat (click for the main chat)" : "Goes to the main chat (click to start a new chat)"}
            onClick={() => setFresh(!fresh)}
          >
            <Plus size={18} />
          </button>
          <button className="send" disabled={!draft.trim()} aria-label="Send">
            <ArrowUp size={18} strokeWidth={2.25} />
          </button>
        </form>
        {fresh && <p className="muted small home-target">This will start a new chat.</p>}
        {chips.length > 0 && (
          <div className="chips">
            {chips.slice(0, 3).map((c) => (
              <button key={c.label} onClick={c.run}>
                {c.label}
              </button>
            ))}
          </div>
        )}

        {error && <p className="notice error">{error}</p>}
        {!data && !error && <Skeleton lines={6} />}
        {data && (
          <div className="home-grid">
            {data.needs_you.length > 0 && (
              <section className="panel attention">
                <h2>
                  <ShieldCheck size={16} aria-hidden="true" /> Needs your OK
                </h2>
                <ul className="plain">
                  {data.needs_you.slice(0, 3).map((a) => (
                    <li key={a.id}>
                      <b>{a.tool.replace(/_/g, " ")}</b> <span className="muted">for “{a.request}”</span>
                    </li>
                  ))}
                </ul>
                <button className="primary" onClick={() => props.onGo("Approvals")}>
                  Review
                </button>
              </section>
            )}

            <section className="panel wide">
              <h2>Continue</h2>
              {data.continue.length === 0 ? (
                <p className="muted">Nothing yet. Ask something above to start.</p>
              ) : (
                <div className="card-list">
                  {data.continue.map((t) => (
                    <ThreadRow key={t.id} t={t} onOpen={() => props.onOpenThread(t.id)} onDelete={() => props.onDeleteThread(t.id).then(reload)} />
                  ))}
                </div>
              )}
            </section>

            <section className="panel">
              <h2>From today's check-in</h2>
              {data.nudge?.text ? (
                <>
                  <Reply text={data.nudge.text} />
                  <p className="meta">
                    <span>{ago(data.nudge.at)}</span>
                  </p>
                </>
              ) : (
                <p className="muted">
                  No check-in today.{" "}
                  <button className="link inline" onClick={() => props.onGo("Settings")}>
                    Set up check-ins
                  </button>
                </p>
              )}
            </section>

            <section className="panel">
              <h2>Learned today</h2>
              {data.learned.count === 0 ? (
                <p className="muted">Nothing new yet.</p>
              ) : (
                <>
                  <ul className="plain">
                    {data.learned.items.map((m) => (
                      <li key={m.id}>{m.content}</li>
                    ))}
                  </ul>
                  <button className="link inline" onClick={() => props.onGo("Memory")}>
                    {data.learned.count > 3 ? `See all ${data.learned.count}` : "Check or correct these"}
                  </button>
                </>
              )}
            </section>
          </div>
        )}
        {data && (
          <p className="home-foot">
            Today: {data.today.messages} message{data.today.messages === 1 ? "" : "s"}, {data.today.actions} action
            {data.today.actions === 1 ? "" : "s"} taken for you.
          </p>
        )}
      </div>
    </div>
  );
}

type ActivityEntry =
  | {
      type: "action";
      id: string;
      at: string;
      name: string;
      status: string;
      input: Record<string, unknown> | null;
      risk: string;
      request: string;
      thread_id: string | null;
      source: string;
    }
  | { type: "memory"; id: string; at: string; content: string; source: string | null }
  | {
      type: "run"; // a request Riki worked on: also the ones still going, stopped or failed
      id: string;
      at: string;
      request: string;
      status: string;
      latency_ms: number | null;
      error: string | null;
      thread_id: string | null;
      source: string;
      helpers: number;
      steps: number;
      waiting: number;
    };

const STATUS_WORD: Record<string, string> = { ok: "done", denied: "not allowed", error: "failed", stopped: "stopped", running: "working" };
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function ActivityPage({ me, onOpenThread, initialQuery = "" }: { me: Me; onOpenThread: (id: string) => void; initialQuery?: string }) {
  const [q, setQ] = useState(initialQuery);
  const [query, setQuery] = useState(initialQuery);
  const [items, error] = useLoad<ActivityEntry[]>(`/activity?q=${encodeURIComponent(query)}`, [query], 5000); // live: a run in progress shows up and finishes here
  const days = new Map<string, ActivityEntry[]>();
  for (const e of items ?? []) {
    const d = fmtDay(e.at);
    days.set(d, [...(days.get(d) ?? []), e]);
  }
  return (
    <Page
      title="Activity"
      lead={`What ${me.agent_name} worked on for you, what it changed, and what it learned, newest first. Plain answers stay in their chat.`}
    >
      <form
        className="search"
        onSubmit={(e) => {
          e.preventDefault();
          setQuery(q);
        }}
      >
        <Search size={16} className="search-icon" aria-hidden="true" />
        <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search activity" aria-label="Search activity" />
        <button className="primary">Search</button>
      </form>
      {error && <p className="notice error">{error}</p>}
      {!items && !error && <Skeleton lines={6} />}
      {items?.length === 0 && (
        <div className="empty">
          <History size={28} aria-hidden="true" />
          <p>{query ? "Nothing matches that." : "Nothing yet. Work Riki does for you and new memories will show up here."}</p>
        </div>
      )}
      {[...days.entries()].map(([day, list]) => (
        <section key={day}>
          <h2>{day}</h2>
          <ol className="timeline">
            {list.map((e) =>
              e.type === "run" ? (
                <li key={`r${e.id}`}>
                  <span className={`tl-icon ${e.status === "running" ? "running" : e.status}`} aria-hidden="true">
                    {e.status === "ok" ? <Check size={14} /> : e.status === "running" ? <span className="tl-live" /> : e.status === "stopped" ? <Minus size={14} /> : <CircleAlert size={14} />}
                  </span>
                  <div className="grow">
                    <p>
                      {e.source === "heartbeat" ? "Check-in" : `“${e.request.slice(0, 90)}”`}{" "}
                      <span className={`chip tone-${e.status === "ok" ? "ok" : e.status === "running" ? "running" : e.status === "stopped" ? "warn" : "error"}`}>
                        {e.waiting ? "needs your OK" : (STATUS_WORD[e.status] ?? e.status)}
                      </span>
                    </p>
                    <p className="meta">
                      <time>{new Date(e.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</time>
                      {e.steps > 0 && <span>{plural(e.steps, "step")}</span>}
                      {e.helpers > 0 && <span>{plural(e.helpers, "helper")}</span>}
                      {e.latency_ms != null && <span>{(e.latency_ms / 1000).toFixed(1)} s</span>}
                      {e.error && <span>{e.error.slice(0, 80)}</span>}
                      {e.thread_id && (
                        <button className="link inline" onClick={() => onOpenThread(e.thread_id!)}>
                          Open chat
                        </button>
                      )}
                    </p>
                  </div>
                </li>
              ) : e.type === "memory" ? (
                <li key={`m${e.id}`}>
                  <span className="tl-icon memory" aria-hidden="true">
                    <Brain size={14} />
                  </span>
                  <div className="grow">
                    <p>Learned: {e.content}</p>
                    <p className="meta">
                      <time>{new Date(e.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</time>
                      {e.source?.includes("/") && (
                        <button className="link inline" onClick={() => onOpenThread(e.source!.split("/")[0])}>
                          Open chat
                        </button>
                      )}
                    </p>
                  </div>
                </li>
              ) : (
                <li key={`a${e.id}`}>
                  <span className={`tl-icon ${e.status}`} aria-hidden="true">
                    {e.status === "ok" ? <Check size={14} /> : e.status === "denied" ? <Minus size={14} /> : <CircleAlert size={14} />}
                  </span>
                  <div className="grow">
                    <p>
                      {describe({ kind: "tool", name: e.name, input: e.input } as Span)}{" "}
                      <span className={`chip tone-${e.status === "ok" ? "ok" : "error"}`}>{STATUS_WORD[e.status] ?? e.status}</span>
                    </p>
                    <p className="meta">
                      <time>{new Date(e.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</time>
                      <span>{e.source === "heartbeat" ? "During a check-in" : `You asked: “${e.request.slice(0, 90)}”`}</span>
                      {e.thread_id && e.source === "chat" && (
                        <button className="link inline" onClick={() => onOpenThread(e.thread_id!)}>
                          Open chat
                        </button>
                      )}
                    </p>
                  </div>
                </li>
              ),
            )}
          </ol>
        </section>
      ))}
    </Page>
  );
}
