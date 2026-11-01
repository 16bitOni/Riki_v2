// The Settings sections that are about control: your name and time zone, Telegram, how much Riki thinks and spends,
// and what it may do. Every control saves on its own, takes effect at once, and can go back to what .env says.

import { Check, DownloadSimple as Download, ArrowSquareOut as ExternalLink, PaperPlaneTilt as Send, ShieldCheck, ShieldSlash as ShieldOff, ShieldWarning as ShieldQuestion, DeviceMobile as Smartphone } from "@phosphor-icons/react";
import { ReactNode, useState } from "react";
import { BarChart, Meter, Split } from "./Charts";
import { Effort, Me, SettingsValues, SettingsView, Status, TelegramStatus, ago, del, dollars, post, put, shortModel } from "./api";
import { useLoad } from "./Pages";
import { toast } from "./ui";

type Reload = () => void;
const fail = (e: unknown) => toast(e instanceof Error ? e.message : "That didn't work.", "error");

async function save(body: Partial<Record<keyof SettingsValues, unknown>>, reload: Reload) {
  try {
    await put("/settings", body);
    reload();
    toast("Saved");
  } catch (e) {
    fail((e as { status?: number }).status === 422 ? new Error("That value isn't allowed.") : e);
  }
}

// --- small controls ---------------------------------------------------------------------------------------------

function Setting(props: { title: string; hint?: ReactNode; k?: keyof SettingsValues; view?: SettingsView; reload?: Reload; children: ReactNode }) {
  const { k, view, reload } = props;
  const changed = k && view?.overridden.includes(k);
  return (
    <div className="setting">
      <div className="grow">
        <strong>{props.title}</strong>
        {props.hint && <p className="muted small">{props.hint}</p>}
        {changed && view && reload && (
          <button className="link small" onClick={() => save({ [k]: null }, reload)}>
            Reset to .env ({describe(k, view.defaults[k])})
          </button>
        )}
      </div>
      <div className="setting-control">{props.children}</div>
    </div>
  );
}

const describe = (k: keyof SettingsValues, v: unknown) =>
  k === "prices" ? `${Object.keys(v as object).length} models` : k === "daily_budget_usd" ? (v ? dollars(v as number) : "no cap") : typeof v === "boolean" ? (v ? "on" : "off") : String(v);

function Switch({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return <button role="switch" aria-checked={on} aria-label={label} className={`switch ${on ? "on" : ""}`} onClick={() => onChange(!on)} />;
}

/** Commits when you leave the field or press Enter, not on every keystroke. */
function Field(props: { value: string; onCommit: (v: string) => void; label: string; width?: number; type?: string; list?: string; placeholder?: string }) {
  const [text, setText] = useState(props.value);
  const [seen, setSeen] = useState(props.value);
  if (props.value !== seen) (setSeen(props.value), setText(props.value));
  const commit = () => text.trim() !== props.value && text.trim() && props.onCommit(text.trim());
  return (
    <input
      aria-label={props.label}
      type={props.type ?? "text"}
      value={text}
      list={props.list}
      placeholder={props.placeholder}
      style={{ width: props.width }}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
    />
  );
}

// --- you ----------------------------------------------------------------------------------------------------

export function About({ view, reload }: { view: SettingsView; reload: Reload }) {
  const v = view.values;
  const zones = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  return (
    <section className="settings-group">
      <h2 className="first">You</h2>
      <Setting title="Your name" k="user_name" view={view} reload={reload} hint="How the agent addresses you.">
        <Field label="Your name" value={v.user_name} width={180} onCommit={(x) => save({ user_name: x }, reload)} />
      </Setting>
      <Setting title="Time zone" k="timezone" view={view} reload={reload} hint="Decides what “today”, reminders and quiet hours mean.">
        <Field label="Time zone" value={v.timezone} width={180} list="zones" onCommit={(x) => save({ timezone: x }, reload)} />
        <datalist id="zones">
          {zones.map((z) => (
            <option key={z} value={z} />
          ))}
        </datalist>
      </Setting>
    </section>
  );
}

// --- Telegram -------------------------------------------------------------------------------------------------

const TOKEN = /^\d{6,12}:[A-Za-z0-9_-]{30,50}$/;

export function Channels(props: { me: Me; tg: TelegramStatus | null; reload: Reload; onOpenChat: () => void }) {
  const { me, tg, reload } = props;
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  if (!tg) return null;
  const run = async (fn: () => Promise<unknown>, done?: string) => {
    setBusy(true);
    try {
      await fn();
      if (done) toast(done);
      reload();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };
  const connect = () => {
    if (!TOKEN.test(token.trim())) return toast("That doesn't look like a bot token. It looks like 123456789:AAH…", "error");
    run(() => post("/telegram", { token: token.trim() }).then(() => setToken("")));
  };
  const state = !tg.connected ? "off" : tg.paired ? "on" : "pairing";
  return (
    <>
      <p className="muted section-lead">
        Talk to {me.agent_name} from your phone. Everything said over Telegram lives in one permanent chat here, and approvals appear as Allow / Deny buttons you
        can tap.
      </p>

      <div className={`channel ${state}`}>
        <div className="channel-head">
          <span className="channel-icon">
            <Send size={20} aria-hidden="true" />
          </span>
          <div className="grow">
            <h3>Telegram</h3>
            <p className="muted small">
              {state === "on" && `Connected${tg.who ? ` to ${tg.who}` : ""} through @${tg.bot}`}
              {state === "pairing" && `Bot @${tg.bot} is saved. One step left: prove it's you.`}
              {state === "off" && "Not connected"}
            </p>
          </div>
          <i className={`dot ${state === "on" ? "ok" : state === "pairing" ? "warn" : ""}`} />
        </div>

        {tg.error && <p className="notice error">{tg.error}</p>}

        {state === "off" && (
          <div className="channel-body">
            <ol className="steps">
              <li>
                Open <a href="https://t.me/BotFather" target="_blank" rel="noreferrer">@BotFather <ExternalLink size={12} aria-hidden="true" /></a> in Telegram, send <code>/newbot</code>, and follow the
                questions. It gives you a token.
              </li>
              <li>Paste the token here. It is checked with Telegram, then stored encrypted and never shown again.</li>
            </ol>
            <form
              className="row"
              onSubmit={(e) => {
                e.preventDefault();
                connect();
              }}
            >
              <input
                type="password"
                className="grow"
                aria-label="Bot token"
                autoComplete="off"
                spellCheck={false}
                placeholder="123456789:AAH…"
                value={token}
                onChange={(e) => setToken(e.target.value)}
              />
              <button className="primary" disabled={busy || !token.trim()}>
                {busy ? "Checking…" : "Connect"}
              </button>
            </form>
          </div>
        )}

        {state === "pairing" && (
          <div className="channel-body">
            {tg.code ? (
              <>
                <p>
                  Open your bot and send this code, or tap the button, which sends it for you. Only the chat that sends it will ever be answered.
                </p>
                <div className="pair-code" aria-label="Pairing code">
                  {tg.code}
                </div>
                <div className="row">
                  {tg.link && (
                    <a className="button primary" href={tg.link} target="_blank" rel="noreferrer">
                      <Smartphone size={16} aria-hidden="true" /> Open in Telegram
                    </a>
                  )}
                  <span className="muted small">Waiting… expires in {Math.max(1, Math.ceil((tg.expires_in ?? 0) / 60))} min</span>
                </div>
              </>
            ) : (
              <p>{tg.locked ? "Too many wrong tries, so this code is locked." : "The code expired."} Get a new one to continue.</p>
            )}
            <div className="row">
              {!tg.code && (
                <button className="primary" disabled={busy} onClick={() => run(() => post("/telegram/pair", {}))}>
                  New code
                </button>
              )}
              <button disabled={busy} onClick={() => run(() => del("/telegram"))}>
                Cancel
              </button>
            </div>
          </div>
        )}

        {state === "on" && (
          <div className="channel-body">
            <div className="row">
              <button className="primary" onClick={props.onOpenChat}>
                Open the Telegram chat
              </button>
              <button disabled={busy} onClick={() => run(() => post("/telegram/test", {}), "Sent. Check your phone.")}>
                Send a test message
              </button>
              <button
                disabled={busy}
                onClick={() => confirm("Pair a different phone? This one stops working until the new one sends the code.") && run(() => post("/telegram/pair", {}))}
              >
                Change phone
              </button>
              <button disabled={busy} onClick={() => confirm("Disconnect Telegram? The token is deleted; the chat history here stays.") && run(() => del("/telegram"), "Telegram disconnected")}>
                Disconnect
              </button>
            </div>
          </div>
        )}
      </div>

      <h2>How it's kept safe</h2>
      <ul className="list">
        {[
          "The bot token is encrypted with a key made from your password and is never sent back to this page.",
          "Pairing needs a one-time code that lives ten minutes and locks after five wrong guesses. Until then every message is ignored without an answer.",
          `Only the paired chat is answered or messaged. ${me.agent_name} can't choose who to write to.`,
          "Changing your password makes the saved token unreadable. You'd connect again, nothing else is affected.",
        ].map((t) => (
          <li key={t}>
            <Check size={15} className="tone-ok" aria-hidden="true" />
            <span className="grow">{t}</span>
          </li>
        ))}
      </ul>
    </>
  );
}

// --- thinking and spend ---------------------------------------------------------------------------------

type CostRow = { day: string; source: string; runs: number; cost_usd: number; tokens: number; unpriced_runs: number };
type Nudge = { id: string; topic: string; text: string; sent_at: string; reaction: string | null };

const SOURCES: Record<string, string> = {
  chat: "Your chats",
  heartbeat: "Check-ins",
  schedule: "Scheduled tasks",
  cognition: "Thinking",
  lessons: "Learning from chats",
  onboarding: "Setup",
  eval: "Evals",
};
const EFFORTS: [Effort, string][] = [["low", "Quick"], ["medium", "Balanced"], ["high", "Deep"]];
const INTERVALS: [number, string][] = [[1, "Every hour"], [2, "Every 2 hours"], [4, "Every 4 hours"], [8, "Every 8 hours"], [12, "Twice a day"], [24, "Once a day"]];

const cents = (n: number) => (n === 0 ? "$0" : dollars(n));

function lastDays(rows: CostRow[], n: number) {
  const byDay = new Map<string, number>();
  for (const r of rows) byDay.set(r.day, (byDay.get(r.day) ?? 0) + r.cost_usd);
  const out = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400_000);
    const key = d.toLocaleDateString("en-CA");
    out.push({ label: d.toLocaleDateString(undefined, { day: "numeric", month: "short" }), value: byDay.get(key) ?? 0 });
  }
  return out;
}

export function Thinking({ me, view, reload }: { me: Me; view: SettingsView; reload: Reload }) {
  const v = view.values;
  const [spend] = useLoad<{ spent_usd: number; budget_usd: number | null; state: string }>("/spend", [], 15000);
  const [cost] = useLoad<CostRow[]>("/cost?days=14");
  const [nudges] = useLoad<Nudge[]>("/nudges?limit=10");
  const [from, to] = v.quiet_hours.split("-").map(Number);
  const hours = Array.from({ length: 24 }, (_, h) => h);
  const hour = (h: number) => `${String(h).padStart(2, "0")}:00`;
  const priced = Object.keys(v.prices).length > 0;
  const total = cost?.reduce((a, r) => a + r.cost_usd, 0) ?? 0;
  const bySource = new Map<string, number>();
  for (const r of cost ?? []) bySource.set(r.source, (bySource.get(r.source) ?? 0) + r.cost_usd);
  const parts = [...bySource.entries()].filter(([, c]) => c > 0).sort((a, b) => b[1] - a[1]).map(([s, c]) => ({ label: SOURCES[s] ?? s, value: c }));
  const background = parts.filter((p) => p.label !== SOURCES.chat).reduce((a, p) => a + p.value, 0);
  const state = spend?.state ?? "ok";

  return (
    <>
      <p className="muted section-lead">
        Between chats {me.agent_name} can think about what you've told it, learn how you like to be helped, and sometimes message you. Here is how much it does, and what it
        costs.
      </p>

      <h2 className="first">Today</h2>
      <div className="spend-card">
        <div className="spend-now">
          <span className="stat-value">{cents(spend?.spent_usd ?? 0)}</span>
          <span className="muted">{spend?.budget_usd ? `of ${dollars(spend.budget_usd)} today` : "spent today, no cap set"}</span>
          {state !== "ok" && (
            <span className={`chip ${state === "hard" ? "tone-error" : "tone-warn"}`}>
              {state === "hard" ? "Cap reached: chats answer in quick mode" : "Background thinking paused"}
            </span>
          )}
        </div>
        {spend?.budget_usd ? (
          <Meter value={spend.spent_usd} max={spend.budget_usd} tick={0.8} tone={state === "hard" ? "error" : state === "soft" ? "warn" : "ok"} label="Spent today against the daily cap" />
        ) : null}
        {spend?.budget_usd ? <p className="muted small">At 80% background thinking pauses; at 100% chats switch to quick answers.</p> : null}
      </div>

      <h2>Last 14 days</h2>
      {!priced ? (
        <p className="notice">Costs can't be counted yet: tell {me.agent_name} what your models cost under <b>Prices</b> below.</p>
      ) : cost ? (
        <>
          <BarChart title="Spend per day" data={lastDays(cost, 14)} format={cents} />
          {parts.length > 0 && (
            <>
              <p className="muted small chart-note">
                {dollars(total)} in total, {total > 0 ? Math.round((background / total) * 100) : 0}% of it background work.
              </p>
              <Split rows={parts} format={cents} />
            </>
          )}
        </>
      ) : null}

      <h2>Thinking</h2>
      <div className="settings-group">
        <Setting title="Think between chats" k="cognition" view={view} reload={reload} hint="Reflects on your memories and may send one considered message. Skipped when nothing is new.">
          <Switch label="Think between chats" on={v.cognition} onChange={(x) => save({ cognition: x }, reload)} />
        </Setting>
        {v.cognition && (
          <Setting title="How often" k="cognition_interval_h" view={view} reload={reload} hint="At most this often; it only costs tokens when there is something new to think about.">
            <select aria-label="How often" value={v.cognition_interval_h} onChange={(e) => save({ cognition_interval_h: Number(e.target.value) }, reload)}>
              {!INTERVALS.some(([h]) => h === v.cognition_interval_h) && <option value={v.cognition_interval_h}>Every {v.cognition_interval_h} h</option>}
              {INTERVALS.map(([h, t]) => (
                <option key={h} value={h}>
                  {t}
                </option>
              ))}
            </select>
          </Setting>
        )}
        <Setting title="Quiet hours" k="quiet_hours" view={view} reload={reload} hint="No check-ins or messages in this window. Same start and end means never quiet.">
          <div className="row">
            <select aria-label="Quiet from" value={from} onChange={(e) => save({ quiet_hours: `${e.target.value}-${to}` }, reload)}>
              {hours.map((h) => (
                <option key={h} value={h}>
                  {hour(h)}
                </option>
              ))}
            </select>
            <span className="muted">to</span>
            <select aria-label="Quiet until" value={to} onChange={(e) => save({ quiet_hours: `${from}-${e.target.value}` }, reload)}>
              {hours.map((h) => (
                <option key={h} value={h}>
                  {hour(h)}
                </option>
              ))}
            </select>
          </div>
        </Setting>
        <Setting title="Effort" k="default_effort" view={view} reload={reload} hint="Quick answers alone. Deep may use helpers and many steps. You can still pick per message.">
          <div className="segmented" role="group" aria-label="Default effort">
            {EFFORTS.map(([e, t]) => (
              <button key={e} className={v.default_effort === e ? "on" : ""} aria-pressed={v.default_effort === e} onClick={() => save({ default_effort: e }, reload)}>
                {t}
              </button>
            ))}
          </div>
        </Setting>
      </div>

      <h2>Spending</h2>
      <div className="settings-group">
        <Setting title="Daily cap" k="daily_budget_usd" view={view} reload={reload} hint="Background thinking pauses at 80%, chats go quick at 100%. 0 means no cap.">
          <span className="money">
            $
            <Field label="Daily cap in dollars" type="number" width={90} value={String(v.daily_budget_usd ?? 0)} onCommit={(x) => save({ daily_budget_usd: Math.max(0, Number(x) || 0) }, reload)} />
          </span>
        </Setting>
      </div>
      <Prices view={view} reload={reload} />

      <h2>What {me.agent_name} said on its own</h2>
      {nudges && nudges.length === 0 && <p className="muted">Nothing yet. When it does, you'll see here whether you answered. That is what teaches it when to stay quiet.</p>}
      <ul className="list">
        {nudges?.map((n) => (
          <li key={n.id} className="col">
            <span>{n.text}</span>
            <span className="muted small row">
              {ago(n.sent_at)}
              <span className={`chip ${n.reaction === "replied" ? "tone-ok" : n.reaction === "ignored" ? "tone-warn" : ""}`}>
                {n.reaction === "replied" ? "You replied" : n.reaction === "ignored" ? "Ignored" : "Waiting"}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}

function Prices({ view, reload }: { view: SettingsView; reload: Reload }) {
  const models = [...new Set([view.models.main, view.models.smart])];
  const [draft, setDraft] = useState<Record<string, { input: string; output: string; cached: string }>>(() =>
    Object.fromEntries(
      models.map((m) => {
        const p = view.values.prices[m];
        return [m, { input: p ? String(p.input) : "", output: p ? String(p.output) : "", cached: p?.cached != null ? String(p.cached) : "" }];
      }),
    ),
  );
  const set = (m: string, k: "input" | "output" | "cached", x: string) => setDraft({ ...draft, [m]: { ...draft[m], [k]: x } });
  const submit = () => {
    const prices = { ...view.values.prices };
    for (const m of models) {
      const d = draft[m];
      if (d.input === "" && d.output === "") delete prices[m];
      else if (d.input === "" || d.output === "") return toast("Fill in both the input and output price, or clear both.", "error");
      else prices[m] = { input: Number(d.input), output: Number(d.output), ...(d.cached !== "" ? { cached: Number(d.cached) } : {}) };
    }
    save({ prices }, reload);
  };
  return (
    <div className="settings-group">
      <div className="setting col">
        <div>
          <strong>Prices</strong>
          <p className="muted small">Dollars per million tokens, from your provider's price list. Without them nothing can be counted.</p>
          {view.overridden.includes("prices") && (
            <button className="link small" onClick={() => save({ prices: null }, reload)}>
              Reset to .env ({describe("prices", view.defaults.prices)})
            </button>
          )}
        </div>
        <div className="price-grid">
          <span />
          <span className="muted small">Input</span>
          <span className="muted small">Output</span>
          <span className="muted small">Cached input</span>
          {models.map((m) => (
            <div className="price-row" key={m}>
              <span className="price-model" title={m}>
                {shortModel(m)}
              </span>
              {(["input", "output", "cached"] as const).map((k) => (
                <input key={k} type="number" min="0" step="any" aria-label={`${shortModel(m)} ${k} price`} placeholder={k === "cached" ? "optional" : "0.00"} value={draft[m][k]} onChange={(e) => set(m, k, e.target.value)} />
              ))}
            </div>
          ))}
        </div>
        <div className="row">
          <button className="primary" onClick={submit}>
            Save prices
          </button>
        </div>
      </div>
    </div>
  );
}

// --- permissions and data -----------------------------------------------------------------------------------

type Tool = { name: string; risk: string; source: string; description: string };
type Grant = { id: string; tool: string; expires_at: string };

const GROUPS = [
  { risk: "safe", icon: ShieldCheck, title: "Does on its own", note: "Reading and looking things up. Nothing outside the chat changes." },
  { risk: "write", icon: ShieldQuestion, title: "Asks you first", note: "Anything that changes something. Shows up in Approvals; never during check-ins." },
  { risk: "external", icon: ShieldOff, title: "Never does", note: "Anything that reaches another person, like sending a message or email. Always refused." },
] as const;

const sourceName = (source: string) => (source === "builtin" ? "Built in" : source.replace(/^(mcp|app):/, ""));

export function Permissions({ me, status, view, reload }: { me: Me; status: Status | null; view: SettingsView; reload: Reload }) {
  const [tools] = useLoad<Tool[]>("/tools");
  const [grants, , reloadGrants] = useLoad<Grant[]>("/grants", [], 10000);
  return (
    <>
      <p className="muted section-lead">What {me.agent_name} can see and do right now, where your data lives, and how to take it with you.</p>

      <h2 className="first">What {me.agent_name} can do</h2>
      <div className="scopes">
        {GROUPS.map(({ risk, icon: Icon, title, note }) => {
          const list = tools?.filter((t) => t.risk === risk) ?? [];
          const bySource = new Map<string, number>();
          for (const t of list) bySource.set(sourceName(t.source), (bySource.get(sourceName(t.source)) ?? 0) + 1);
          return (
            <section key={risk} className={`scope risk-${risk}`}>
              <span className="scope-n">{list.length}</span>
              <h3>
                <Icon size={16} aria-hidden="true" /> {title}
              </h3>
              <p className="muted small">{note}</p>
              <p className="small">{list.length === 0 ? "Nothing in this group." : [...bySource.entries()].map(([src, n]) => `${src}: ${n}`).join(", ")}</p>
            </section>
          );
        })}
      </div>

      <h2>Running permissions</h2>
      {grants && grants.length === 0 && <p className="muted">None. “Allow for 30 minutes” on an approval shows up here, and you can end it early.</p>}
      <ul className="list">
        {grants?.map((g) => (
          <li key={g.id}>
            <code className="grow">{g.tool}</code>
            <span className="muted small">until {new Date(g.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
            <button className="small" onClick={() => del(`/grants/${g.id}`).then(reloadGrants, fail)}>
              End
            </button>
          </li>
        ))}
      </ul>

      <h2>Run code</h2>
      <div className="settings-group">
        <Setting
          title="Let it run Python"
          k="sandbox"
          view={view}
          reload={reload}
          hint={
            view.docker
              ? "For exact sums and reading tables. Runs in a throwaway Docker box with no network, a read-only disk and 60 seconds."
              : "Needs Docker, which wasn't found on this machine. Install it and switch this on."
          }
        >
          <Switch label="Let it run Python" on={view.values.sandbox} onChange={(x) => save({ sandbox: x }, reload)} />
        </Setting>
      </div>

      <h2>Connected tool servers</h2>
      {status && Object.keys(status.mcp).length === 0 && <p className="muted">None. Only the built-in tools are available.</p>}
      <ul className="list">
        {status &&
          Object.entries(status.mcp).map(([name, m]) => (
            <li key={name}>
              <i className={`dot ${m.ok ? "ok" : "error"}`} />
              <span className="grow">{name}</span>
              <span className={m.ok ? "muted" : "tone-error"}>{m.ok ? `Connected, ${m.tools} tools` : m.error}</span>
            </li>
          ))}
      </ul>

      <h2>Where your data lives</h2>
      <ul className="list">
        <li>
          <span className="grow">Chats, memories, traces and settings</span>
          <span className="muted">Your Postgres database, on your machine</span>
        </li>
        <li>
          <span className="grow">Each message, while {me.agent_name} answers</span>
          <span className="muted">Sent to {status ? shortModel(status.models.main) : "your model provider"}</span>
        </li>
        <li>
          <span className="grow">Training</span>
          <span className="muted">{me.agent_name} never trains models. Your provider's terms apply to what you send it.</span>
        </li>
        <li>
          <span className="grow">Secrets in traces and logs</span>
          <span className="muted">Keys, tokens and passwords are removed before anything is saved</span>
        </li>
      </ul>

      <h2>Take your data with you</h2>
      <p className="muted">One JSON file with every chat, memory, approval and your persona, readable by anything. It is a copy for you, not a full backup: traces, feedback and app tokens are left out, and it can't be imported yet.</p>
      <a className="button primary" href="/api/export" download>
        <Download size={16} aria-hidden="true" /> Export everything
      </a>
    </>
  );
}
