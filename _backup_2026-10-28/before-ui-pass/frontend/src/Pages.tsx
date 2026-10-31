import { ChevronRight, ImagePlus, Search, Trash2 } from "lucide-react";
import { DragEvent, ReactNode, useEffect, useRef, useState } from "react";
import { ApprovalCard, Reply } from "./Activity";
import { ApprovalReq, Me, Persona, RunRow, Status, ago, api, put, post, seconds, shortModel } from "./api";
import { Avatar, MOOD_WORDS, MOODS, Mood } from "./Avatar";
import { Skeleton, toast } from "./ui";

export function Page({ title, lead, children, actions, bare, wide }: { title: string; lead?: ReactNode; children: ReactNode; actions?: ReactNode; bare?: boolean; wide?: boolean }) {
  if (bare) // inside a sheet: the sheet already shows the title
    return (
      <>
        {lead && <p className="lead sheet-lead">{lead}</p>}
        {children}
      </>
    );
  return (
    <div className="page">
      <div className={`page-inner ${wide ? "wide" : ""}`}>
        <header className="page-head">
          <div>
            <h1>{title}</h1>
            {lead && <p className="lead">{lead}</p>}
          </div>
          {actions}
        </header>
        {children}
      </div>
    </div>
  );
}

export function useLoad<T>(path: string, deps: unknown[] = [], every = 0): [T | null, string, () => void] {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const load = () =>
    api<T>(path)
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  useEffect(() => {
    load();
    if (!every) return;
    const t = setInterval(load, every);
    return () => clearInterval(t);
  }, deps);
  return [data, error, load];
}

// --- Approvals: actions waiting on you ----------------------------------------------------------------

export function ApprovalsPage({ me, onChanged }: { me: Me; onChanged: () => void }) {
  const [pending, err, reload] = useLoad<ApprovalReq[]>("/approvals", [], 4000);
  const [history, , reloadHistory] = useLoad<ApprovalReq[]>("/approvals?status=decided");
  const decided = () => {
    setTimeout(() => {
      reload();
      reloadHistory();
      onChanged();
    }, 300);
  };
  return (
    <Page
      title="Approvals"
      bare
      lead={`Anything ${me.agent_name} wants to change waits here for your OK. Nothing that reaches another person ever runs.`}
    >
      {err && <p className="notice error">{err}</p>}
      {pending === null && !err && <Skeleton lines={3} />}
      {pending && pending.length === 0 && (
        <div className="empty">
          <Avatar mood="idle" src={me.avatar} name={me.agent_name} size={56} />
          <p>Nothing is waiting on you.</p>
        </div>
      )}
      <div className="stack">
        {pending?.map((a) => (
          <ApprovalCard key={a.id} approval={a} agentName={me.agent_name} onDecided={decided} />
        ))}
      </div>
      {!!history?.length && (
        <>
          <h2>Recently decided</h2>
          <ul className="list">
            {history.map((a) => (
              <li key={a.id}>
                <span className={`chip tone-${a.status === "approved" ? "ok" : "error"}`}>{a.status}</span>
                <span className="grow">
                  <b>{a.tool}</b> <span className="muted">for “{a.request?.slice(0, 80)}”</span>
                </span>
                <span className="muted">{a.decided_at ? ago(a.decided_at) : ""}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </Page>
  );
}

// --- Heartbeat: the system pulse and scheduled check-ins ----------------------------------------

function issues(s: Status): string[] {
  const out: string[] = [];
  if (!s.db.ok) out.push("The database isn't answering. Start it with docker compose up -d.");
  for (const [name, m] of Object.entries(s.mcp)) if (!m.ok) out.push(`Tool server “${name}” is off: ${m.error}`);
  if (s.pending_approvals) out.push(`${s.pending_approvals} action${s.pending_approvals > 1 ? "s" : ""} waiting for your approval.`);
  if (s.last_24h.errors) out.push(`${s.last_24h.errors} message${s.last_24h.errors > 1 ? "s" : ""} failed in the last 24 hours.`);
  return out;
}

export function Pulse({ status, me, mood }: { status: Status | null; me: Me; mood: Mood }) {
  const list = status ? issues(status) : ["Can't reach the backend."];
  const tone = !status ? "error" : list.length ? "warn" : "ok";
  return (
    <section className={`pulse-card tone-${tone}`}>
      <Avatar mood={!status ? "sleeping" : mood} src={me.avatar} name={me.agent_name} size={56} />
      <div className="grow">
        <svg className="ecg" viewBox="0 0 300 40" preserveAspectRatio="none" aria-hidden="true">
          <path d="M0 20 H90 L100 8 L110 32 L120 4 L130 26 L138 20 H300" />
        </svg>
        <h2>{!status ? "Offline" : list.length ? "A few things need a look" : "Everything is running fine"}</h2>
        {list.length > 0 && (
          <ul className="issues">
            {list.map((i) => (
              <li key={i}>{i}</li>
            ))}
          </ul>
        )}
        {status && (
          <p className="muted">
            Up for {status.uptime_s < 3600 ? `${Math.max(1, Math.round(status.uptime_s / 60))} min` : `${Math.floor(status.uptime_s / 3600)} h ${Math.floor((status.uptime_s % 3600) / 60)} min`}.{" "}
            {status.active.length ? `${status.active.length} running now.` : "Nothing running right now."}
          </p>
        )}
      </div>
    </section>
  );
}

const INTERVALS = [
  [0, "Off"],
  [15, "Every 15 minutes"],
  [30, "Every 30 minutes"],
  [60, "Every hour"],
  [180, "Every 3 hours"],
  [360, "Every 6 hours"],
  [1440, "Once a day"],
] as const;

export function CheckIns({ me, onSaved }: { me: Me; onSaved: () => void }) {
  const [persona, setPersona] = useState<Persona | null>(null);
  const [feed, , reloadFeed] = useLoad<RunRow[]>("/heartbeat", [], 10000);
  useEffect(() => {
    api<Persona>("/persona").then(setPersona);
  }, []);
  if (!persona) return <Skeleton lines={6} />;
  const save = async () => {
    setPersona(await put<Persona>("/persona", persona));
    toast("Check-ins saved");
    onSaved();
  };
  const runNow = async () => {
    try {
      await post("/heartbeat/run", {});
      toast(`${me.agent_name} is checking in`);
      setTimeout(reloadFeed, 4000);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    }
  };
  return (
    <>
      <p className="muted section-lead">
        On a schedule, {me.agent_name} runs your checklist and puts what it finds on Home. It can look things up and suggest, but it
        can't change anything during a check-in, because you're not there to approve it. Check-ins never run during quiet hours (
        {me.quiet_hours.replace("-", ":00 to ")}:00).
      </p>
      <div className="form">
        <label>
          How often
          <select
            value={persona.heartbeat_minutes}
            onChange={(e) => setPersona({ ...persona, heartbeat_minutes: Number(e.target.value) })}
          >
            {INTERVALS.map(([v, t]) => (
              <option key={v} value={v}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label>
          What to check
          <textarea
            rows={4}
            value={persona.heartbeat_prompt}
            maxLength={2000}
            onChange={(e) => setPersona({ ...persona, heartbeat_prompt: e.target.value })}
            placeholder="For example: look at what I said I'd do this week and nudge me about anything slipping."
          />
        </label>
        <div className="row">
          <button className="primary" onClick={save}>
            Save check-ins
          </button>
          <button onClick={runNow}>Check in now</button>
        </div>
      </div>

      <h2>Recent check-ins</h2>
      {!feed?.length && <p className="muted">No check-ins yet.</p>}
      <div className="stack">
        {feed?.map((r) => (
          <article key={r.id} className="beat">
            <header>
              <span className={`chip tone-${r.status === "ok" ? "ok" : r.status === "running" ? "warn" : "error"}`}>
                {r.status === "ok" ? "done" : r.status}
              </span>
              <span className="muted">{ago(r.created_at)}</span>
              <span className="muted">{seconds(r.latency_ms)}</span>
            </header>
            {r.output ? <Reply text={r.output} /> : <p className="muted">Still checking…</p>}
          </article>
        ))}
      </div>
    </>
  );
}

// --- Persona: who your agent is --------------------------------------------------------------

export function PersonaSettings({ me, onSaved }: { me: Me; onSaved: () => void }) {
  const [p, setP] = useState<Persona | null>(null);
  const [preview, setPreview] = useState<Mood>("idle");
  const [avatar, setAvatar] = useState(me.avatar);
  const [drag, setDrag] = useState(false);
  const [uploading, setUploading] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  useEffect(() => {
    api<Persona>("/persona").then(setP);
  }, []);
  useEffect(() => setAvatar(me.avatar), [me.avatar]);

  async function upload(f: File | undefined) {
    if (!f) return;
    if (f.size > 5 * 1024 * 1024) return toast("That image is over 5 MB.", "error");
    setUploading(true);
    try {
      const res = await fetch("/api/persona/avatar", { method: "PUT", body: f, credentials: "same-origin" });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Upload failed.");
      setAvatar(URL.createObjectURL(f));
      toast("Avatar updated");
      onSaved();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setUploading(false);
    }
  }
  async function removeAvatar() {
    await api("/persona/avatar", { method: "DELETE" });
    setAvatar(null);
    toast("Avatar removed");
    onSaved();
  }
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDrag(false);
    upload(e.dataTransfer.files[0]);
  };

  if (!p) return <Skeleton lines={6} />;
  const save = async () => {
    try {
      setP(await put<Persona>("/persona", p));
      toast(`Saved. ${p.name} uses this from your next message.`);
      onSaved();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    }
  };
  return (
    <>
      <p className="muted section-lead">Who {p.name || me.agent_name} is. The safety rules always come first.</p>
      <div className="persona">
        <div
          className={`persona-card ${drag ? "drag" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={onDrop}
        >
          <Avatar mood={uploading ? "working" : preview} src={avatar} name={p.name} size={128} />
          <p className="persona-name">{p.name || "Unnamed"}</p>
          <div className="row center">
            <button onClick={() => file.current?.click()} disabled={uploading}>
              <ImagePlus size={16} /> {avatar ? "Change image" : "Upload image or GIF"}
            </button>
            {avatar && (
              <button className="icon" aria-label="Remove image" title="Remove image" onClick={removeAvatar}>
                <Trash2 size={16} />
              </button>
            )}
          </div>
          <p className="muted small">PNG, JPEG, GIF or WebP, up to 5 MB. Or drop a file here.</p>
          <input
            ref={file}
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            hidden
            onChange={(e) => upload(e.target.files?.[0])}
          />
          <div className="segmented" role="group" aria-label="Preview a state">
            {MOODS.map((m) => (
              <button key={m} className={m === preview ? "on" : ""} aria-pressed={m === preview} onClick={() => setPreview(m)}>
                {MOOD_WORDS[m]}
              </button>
            ))}
          </div>
        </div>
        <div className="form grow">
          <label>
            Name
            <input value={p.name} maxLength={40} onChange={(e) => setP({ ...p, name: e.target.value })} />
          </label>
          <label>
            Personality
            <textarea
              rows={10}
              maxLength={4000}
              value={p.soul}
              onChange={(e) => setP({ ...p, soul: e.target.value })}
              placeholder="How should it talk to you? What should it always or never do? For example: tease me a little, keep replies short, push back when I'm procrastinating."
            />
            <span className="counter">{p.soul.length} / 4000</span>
          </label>
          <div className="row">
            <button className="primary" onClick={save} disabled={!p.name.trim()}>
              Save persona
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

export const fmtDay = (iso: string) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });

// --- Memory: what your agent knows ------------------------------------------------------------

type Memory = {
  id: string;
  content: string;
  kind: string;
  importance: number;
  created_at: string;
  event_at: string | null;
  source: string | null; // "<thread>/<run>" when learned from a chat
};

export function MemoryPage({ me, onOpenThread }: { me: Me; onOpenThread: (id: string) => void }) {
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [items, setItems] = useState<Memory[] | null>(null);
  const [q, setQ] = useState("");
  const [error, setError] = useState("");
  const load = (query: string) =>
    api<Memory[]>(`/memories?q=${encodeURIComponent(query)}`)
      .then(setItems)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  useEffect(() => {
    load("");
  }, []);

  async function remove(m: Memory) {
    if (!confirm(`Forget this for good?\n\n${m.content}`)) return;
    await api(`/memories/${m.id}`, { method: "DELETE" });
    setItems((all) => all?.filter((x) => x.id !== m.id) ?? null);
    toast("Forgotten");
  }

  async function correct() {
    if (!editing) return;
    try {
      const fixed = await api<Memory>(`/memories/${editing.id}`, { method: "PATCH", body: JSON.stringify({ content: editing.text }) });
      setItems((all) => all?.map((x) => (x.id === editing.id ? fixed : x)) ?? null);
      setEditing(null);
      toast("Corrected. The old version is kept as history.");
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    }
  }

  return (
    <Page
      title="Memory"
      lead={`Everything ${me.agent_name} remembers about you, where it learned it, and when. Correct anything that's wrong; forget anything you'd rather it didn't keep.`}
    >
      <form
        className="search"
        onSubmit={(e) => {
          e.preventDefault();
          load(q);
        }}
      >
        <Search size={16} className="search-icon" aria-hidden="true" />
        <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search memories" aria-label="Search memories" />
        <button className="primary">Search</button>
        {q && (
          <button
            type="button"
            onClick={() => {
              setQ("");
              load("");
            }}
          >
            Show all
          </button>
        )}
      </form>
      {error && <p className="notice error">{error}</p>}
      {items === null && !error && <Skeleton lines={5} />}
      {items && items.length === 0 && (
        <div className="empty">
          <Avatar mood="idle" src={me.avatar} name={me.agent_name} size={56} />
          <p>{q ? "Nothing matches that." : `${me.agent_name} hasn't learned anything yet. Chat a bit and come back.`}</p>
        </div>
      )}
      <ul className="memories">
        {items?.map((m) => (
          <li key={m.id}>
            <span className={`chip kind-${m.kind}`}>{m.kind}</span>
            <div className="grow">
              {editing?.id === m.id ? (
                <form
                  className="correct"
                  onSubmit={(e) => {
                    e.preventDefault();
                    correct();
                  }}
                >
                  <textarea autoFocus rows={2} value={editing.text} maxLength={2000} onChange={(e) => setEditing({ ...editing, text: e.target.value })} aria-label="Corrected memory" />
                  <div className="row">
                    <button className="primary" disabled={editing.text.trim().length < 3}>
                      Save correction
                    </button>
                    <button type="button" onClick={() => setEditing(null)}>
                      Cancel
                    </button>
                  </div>
                </form>
              ) : (
                <p>{m.content}</p>
              )}
              <p className="meta">
                {m.source?.includes("/") ? (
                  <button className="link inline" onClick={() => onOpenThread(m.source!.split("/")[0])}>
                    Learned in a chat on {fmtDay(m.created_at)}
                  </button>
                ) : (
                  <span>Saved on {fmtDay(m.created_at)}</span>
                )}
                {m.event_at && <span>About {fmtDay(m.event_at)}</span>}
                {m.importance >= 4 && <span>Important</span>}
              </p>
            </div>
            {editing?.id !== m.id && (
              <div className="row-actions">
                <button className="link" onClick={() => setEditing({ id: m.id, text: m.content })}>
                  Correct
                </button>
                <button className="link danger" onClick={() => remove(m)}>
                  Forget
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </Page>
  );
}

// --- System: the engineer's view --------------------------------------------------------------

type LogRow = { at: number; level: string; logger: string; message: string };
type Metric = {
  day: string;
  runs: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  avg_tokens: number;
  cache_ratio: number;
  error_rate: number;
  bad_citations: number;
  denied_tools: number;
  thumbs_down: number;
};
type EvalCase = { id: string; passed: boolean; passes: number; tries: { fails: string[]; latency_ms: number; tokens: number }[] };
type EvalRun = {
  id: string;
  created_at: string;
  prompt_version: string;
  model: string;
  repeat: number;
  passed: number;
  total: number;
  regressions: string[];
  results: EvalCase[];
};
type Tool = { name: string; risk: string; source: string; core: boolean; description: string };

function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className={`stat ${tone ? `tone-${tone}` : ""}`}>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}

export function Overview({ status: s, me, mood }: { status: Status | null; me: Me; mood: Mood }) {
  if (!s) return <Pulse status={null} me={me} mood="sleeping" />;
  const d = s.last_24h;
  return (
    <>
      <Pulse status={s} me={me} mood={mood} />
      <h2>Last 24 hours</h2>
      <div className="stats">
        <Stat label="messages" value={d.runs ?? 0} />
        <Stat label="failed" value={d.errors ?? 0} tone={d.errors ? "error" : undefined} />
        <Stat label="typical reply" value={seconds(d.p50_ms) || "–"} />
        <Stat label="slowest 5%" value={seconds(d.p95_ms) || "–"} />
        <Stat label="tokens" value={(d.tokens ?? 0).toLocaleString()} />
        <Stat label="from cache" value={`${Math.round((d.cache_ratio ?? 0) * 100)}%`} />
      </div>

      <h2>Parts</h2>
      <ul className="list">
        <li>
          <i className={`dot ${s.db.ok ? "ok" : "error"}`} />
          <span className="grow">Database</span>
          <span className="muted">{s.db.ok ? `${s.db.latency_ms} ms` : "not answering"}</span>
        </li>
        {Object.entries(s.models).map(([k, v]) => (
          <li key={k}>
            <i className="dot ok" />
            <span className="grow">{k === "main" ? "Main model" : k === "smart" ? "Model for hard tasks" : "Embedding model"}</span>
            <code>{shortModel(v)}</code>
          </li>
        ))}
        {Object.entries(s.mcp).map(([name, m]) => (
          <li key={name}>
            <i className={`dot ${m.ok ? "ok" : "error"}`} />
            <span className="grow">Tool server: {name}</span>
            <span className={m.ok ? "muted" : "tone-error"}>{m.ok ? `${m.tools} tools` : m.error}</span>
          </li>
        ))}
        {Object.keys(s.mcp).length === 0 && (
          <li>
            <i className="dot" />
            <span className="grow muted">No tool servers configured (backend/mcp.json)</span>
          </li>
        )}
        <li>
          <i className="dot ok" />
          <span className="grow">Tools</span>
          <span className="muted">
            {s.tools.total} total: {Object.entries(s.tools.by_risk).map(([k, v]) => `${v} ${k}`).join(", ")}
          </span>
        </li>
        <li>
          <i className={`dot ${s.memory_writes.failed_writes ? "warn" : "ok"}`} />
          <span className="grow">Memory learning</span>
          <span className="muted">
            {s.memory_writes.writes ?? 0} chats read, {s.memory_writes.memories_saved ?? 0} memories saved,{" "}
            {s.memory_writes.failed_writes ?? 0} failed
          </span>
        </li>
      </ul>

      <h2>Running now</h2>
      {s.active.length === 0 ? (
        <p className="muted">Nothing.</p>
      ) : (
        <ul className="list">
          {s.active.map((a) => (
            <li key={a.run_id}>
              <i className="dot running" />
              <span className="grow">{a.input}</span>
              <span className="muted">
                {a.source}, started {ago(a.started_at)}
              </span>
            </li>
          ))}
        </ul>
      )}

      <h2>Recent failures</h2>
      {s.recent_errors.length === 0 ? (
        <p className="muted">None.</p>
      ) : (
        <ul className="list">
          {s.recent_errors.map((e) => (
            <li key={e.run_id} className="col">
              <span>
                <b>{e.input}</b> <span className="muted">{ago(e.at)}</span>
              </span>
              <code className="tone-error">{e.error}</code>
            </li>
          ))}
        </ul>
      )}
      <p className="muted small">
        Prompt version <code>{s.prompt_version}</code>
      </p>
    </>
  );
}

export function Logs() {
  const [level, setLevel] = useState("INFO");
  const [follow, setFollow] = useState(true);
  const [rows, err] = useLoad<LogRow[]>(`/logs?level=${level}`, [level, follow], follow ? 4000 : 0);
  return (
    <>
      <div className="row toolbar">
        <label className="inline">
          Show
          <select value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="INFO">Everything</option>
            <option value="WARNING">Warnings and errors</option>
            <option value="ERROR">Errors only</option>
          </select>
        </label>
        <label className="inline">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> Follow live
        </label>
      </div>
      {err && <p className="notice error">{err}</p>}
      {rows?.length === 0 && <p className="muted">No log lines yet.</p>}
      <ol className="logs">
        {rows?.map((r, i) => (
          <li key={i} className={`lvl-${r.level}`}>
            <time>{new Date(r.at * 1000).toLocaleTimeString()}</time>
            <span className="lvl">{r.level.toLowerCase()}</span>
            <span className="logger">{r.logger}</span>
            <span className="msg">{r.message}</span>
          </li>
        ))}
      </ol>
    </>
  );
}

export function Quality() {
  const [evals] = useLoad<EvalRun[]>("/evals");
  const [metrics] = useLoad<Metric[]>("/metrics");
  const [open, setOpen] = useState<string | null>(null);
  return (
    <>
      <h2>Eval runs</h2>
      <p className="muted">
        Run <code>uv run python -m evals.run --repeat 3</code> in the backend folder. A case passes only when every try passes.
      </p>
      {!evals?.length ? (
        <p className="muted">No eval runs yet.</p>
      ) : (
        <ul className="list rows">
          {evals.map((e) => {
            const good = e.passed === e.total;
            return (
              <li key={e.id} className="col">
                <button className="row-btn" onClick={() => setOpen(open === e.id ? null : e.id)} aria-expanded={open === e.id}>
                  <i className={`dot ${good ? "ok" : "error"}`} />
                  <span className="grow">
                    <strong>
                      {e.passed} of {e.total} passed
                    </strong>
                    <span className="muted small block">
                      {ago(e.created_at)} · {shortModel(e.model)} · {e.repeat > 1 ? `${e.repeat} tries each · ` : ""}prompt <code>{e.prompt_version}</code>
                    </span>
                  </span>
                  <ChevronRight size={15} className={`chev ${open === e.id ? "open" : ""}`} aria-hidden="true" />
                </button>
                {e.regressions.length > 0 && <p className="tone-error small">Got worse: {e.regressions.join(", ")}</p>}
                {open === e.id && (
                  <ul className="subrows">
                    {e.results.map((r) => (
                      <li key={r.id}>
                        <i className={`dot ${r.passed ? "ok" : "error"}`} />
                        <span className="grow">
                          {r.id}
                          <span className="muted small block">
                            {r.passes} of {r.tries.length} tries · {seconds(r.tries[0]?.latency_ms)} · {r.tries[0]?.tokens.toLocaleString()} tokens
                          </span>
                          {[...new Set(r.tries.flatMap((t) => t.fails))].map((f, i) => (
                            <span key={i} className="tone-error small block">
                              {f}
                            </span>
                          ))}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <h2>Daily signals from real chats</h2>
      <p className="muted">Drift shows here first: slower replies, more tokens, less caching, unbacked citations, refusals, thumbs down.</p>
      {!metrics?.length ? (
        <p className="muted">No chats yet.</p>
      ) : (
        <ul className="list rows">
          {[...metrics].reverse().map((m) => {
            const flags = [
              [m.error_rate > 0, `${Math.round(m.error_rate * 100)}% failed`],
              [m.bad_citations > 0, `${m.bad_citations} unbacked`],
              [m.denied_tools > 0, `${m.denied_tools} refused`],
              [m.thumbs_down > 0, `${m.thumbs_down} thumbs down`],
            ] as const;
            return (
              <li key={m.day} className="col">
                <div className="row-head">
                  <strong>{new Date(m.day).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })}</strong>
                  <span className="muted small">{m.runs} {m.runs === 1 ? "message" : "messages"}</span>
                </div>
                <span className="muted small">
                  Average {seconds(m.avg_latency_ms)} · slowest 5% {seconds(m.p95_latency_ms)} · {m.avg_tokens.toLocaleString()} tokens ·{" "}
                  {Math.round(m.cache_ratio * 100)}% cached
                </span>
                {flags.some(([on]) => on) && (
                  <span className="row">
                    {flags.filter(([on]) => on).map(([, label]) => (
                      <span key={label} className="chip tone-error">
                        {label}
                      </span>
                    ))}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

const RISK_WORDS: Record<string, string> = { safe: "runs on its own", write: "asks you first", external: "never runs" };

export function Tools() {
  const [tools] = useLoad<Tool[]>("/tools");
  return (
    <>
      <p className="muted">
        Always-loaded tools are marked. The rest are loaded only when needed, which keeps every message cheaper. Add tool servers in{" "}
        <code>backend/mcp.json</code>.
      </p>
      <ul className="tools">
        {tools?.map((t) => (
          <li key={t.name}>
            <div className="row">
              <code>{t.name}</code>
              <span className={`chip risk-${t.risk}`}>{RISK_WORDS[t.risk] ?? t.risk}</span>
              {t.core && <span className="chip">always loaded</span>}
              <span className="muted small">{t.source}</span>
            </div>
            <p className="muted small">{t.description}</p>
          </li>
        ))}
      </ul>
    </>
  );
}
