// Apps: connect the accounts you use. Riki reads on its own, asks before changing anything, never sends.

import { Check, ChevronRight, ExternalLink, Link2, Search } from "lucide-react";
import { FormEvent, useEffect, useRef, useState } from "react";
import {
  siAirtable, siAsana, siAtlassian, siBox, siGithub, siGmail, siGooglecalendar, siGoogledocs, siGoogledrive,
  siHuggingface, siLinear, siNotion, siPaypal, siSentry, siStackoverflow, siStripe, siSupabase, siSwiggy,
  siVercel, siWolfram, siZapier, siZerodha, siZomato,
} from "simple-icons";
import { Me, api, post } from "./api";
import { Page, useLoad } from "./Pages";
import { Sheet, Skeleton, toast } from "./ui";

type Risk = "safe" | "write" | "external" | "off";
export type App = {
  id: string;
  name: string;
  category: string;
  icon: string | null;
  blurb: string;
  auth: "oauth" | "google" | "token" | "none";
  url: string;
  token_label: string | null;
  token_help: string | null;
  popular: boolean | null;
  inspect_args: boolean | null;
  connected: boolean;
  mode: "read" | "ask";
  uses_week: number;
  state: "off" | "connecting" | "signin" | "connected" | "error";
  error: string | null;
  auth_url: string | null;
  tools: { name: string; risk: Risk }[];
};
type Found = { name: string; description: string; url: string; header: string | null; website: string | null };

const ICONS: Record<string, { path: string; hex: string }> = {
  airtable: siAirtable, asana: siAsana, atlassian: siAtlassian, box: siBox, github: siGithub, gmail: siGmail,
  googlecalendar: siGooglecalendar, googledocs: siGoogledocs, googledrive: siGoogledrive, huggingface: siHuggingface,
  linear: siLinear, notion: siNotion, paypal: siPaypal, sentry: siSentry, stackoverflow: siStackoverflow,
  stripe: siStripe, supabase: siSupabase, swiggy: siSwiggy, vercel: siVercel, wolfram: siWolfram, zapier: siZapier,
  zerodha: siZerodha, zomato: siZomato,
};

const lum = (hex: string) => {
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

export function AppLogo({ app, size = 44 }: { app: Pick<App, "icon" | "name">; size?: number }) {
  const icon = app.icon ? ICONS[app.icon] : undefined;
  const style = { width: size, height: size, borderRadius: size * 0.27 };
  if (!icon)
    return (
      <span className="app-logo mono" style={{ ...style, fontSize: size * 0.42 }} aria-hidden="true">
        {app.name.replace(/[^A-Za-z0-9]/g, "").slice(0, 1).toUpperCase()}
      </span>
    );
  const l = lum(icon.hex);
  // Near-black brands (Notion, GitHub) sit on white so they don't vanish on a black page.
  const [bg, fg] = l < 0.12 ? ["#f5f5f7", `#${icon.hex}`] : [`#${icon.hex}`, l > 0.62 ? "#000" : "#fff"];
  return (
    <span className="app-logo" style={{ ...style, background: bg }} aria-hidden="true">
      <svg viewBox="0 0 24 24" width={size * 0.54} height={size * 0.54} fill={fg}>
        <path d={icon.path} />
      </svg>
    </span>
  );
}

const STATE: Record<App["state"], [string, string]> = {
  connected: ["ok", "Connected"],
  connecting: ["warn", "Connecting…"],
  signin: ["warn", "Waiting for you to sign in"],
  error: ["error", "Couldn't connect"],
  off: ["", ""],
};

export function Tile({ app, onOpen }: { app: App; onOpen: () => void }) {
  return (
    <button className="app-tile" onClick={onOpen}>
      <AppLogo app={app} />
      <span className="app-tile-text">
        <span className="app-tile-name">{app.name}</span>
        <span className="app-tile-blurb">{app.blurb}</span>
      </span>
      {app.connected ? (
        <span className={`app-tile-state ${STATE[app.state][0]}`}>
          {app.state === "connected" ? <Check size={14} strokeWidth={2.5} aria-label="Connected" /> : <i className={`dot ${STATE[app.state][0]}`} aria-label={STATE[app.state][1]} />}
        </span>
      ) : (
        <span className="app-get">Connect</span>
      )}
    </button>
  );
}

/** Opens the app's sign-in page in a small window; the window closes itself when it's done. */
function signInWindow(url?: string) {
  const w = window.open(url ?? "", "riki-signin", "width=520,height=720");
  if (w && !url) w.document.write('<body style="background:#000;color:#a1a1a6;font:15px system-ui;display:grid;place-items:center;height:100vh;margin:0">Opening sign-in…</body>');
  return w;
}

export function AppsPage({ me, onActivity, initialOpen, onOpened }: { me: Me; onActivity: (q: string) => void; initialOpen?: string | null; onOpened?: () => void }) {
  const [data, error, reload] = useLoad<{ categories: string[]; apps: App[] }>("/apps");
  const [open, setOpen] = useState<string | null>(initialOpen ?? null); // a chat card can send you straight to one app
  useEffect(() => {
    if (initialOpen) onOpened?.();
  }, []);
  const [q, setQ] = useState("");
  const [found, setFound] = useState<Found[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [adding, setAdding] = useState<Partial<Found> | null>(null);
  const prev = useRef<Record<string, string>>({});

  const busy = data?.apps.some((a) => a.connected && (a.state === "connecting" || a.state === "signin"));
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(reload, 1500);
    return () => clearInterval(t);
  }, [busy]);
  useEffect(() => {
    for (const a of data?.apps ?? []) {
      if (a.state === "connected" && prev.current[a.id] && prev.current[a.id] !== "connected") toast(`${a.name} connected`);
      if (a.state === "error" && prev.current[a.id] === "connecting") toast(`${a.name}: ${a.error ?? "couldn't connect"}`, "error");
      prev.current[a.id] = a.state;
    }
  }, [data]);

  const term = q.trim().toLowerCase();
  const apps = data?.apps ?? [];
  const match = (a: App) => !term || `${a.name} ${a.blurb} ${a.category}`.toLowerCase().includes(term);
  const shown = apps.filter(match);
  const connected = apps.filter((a) => a.connected);
  const current = apps.find((a) => a.id === open);

  async function searchDirectory(e?: FormEvent) {
    e?.preventDefault();
    if (term.length < 2) return;
    setSearching(true);
    try {
      setFound(await api<Found[]>(`/apps/directory?q=${encodeURIComponent(term)}`));
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setSearching(false);
    }
  }

  return (
    <Page
      title="Apps"
      lead={`Connect what you use. ${me.agent_name} looks things up on its own, asks before it changes anything, and never sends anything to anyone.`}
    >
      <form className="search" onSubmit={searchDirectory}>
        <Search size={16} className="search-icon" aria-hidden="true" />
        <input
          type="search"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setFound(null);
          }}
          placeholder="Search apps"
          aria-label="Search apps"
        />
      </form>
      {error && <p className="notice error">{error}</p>}
      {!data && !error && <Skeleton lines={8} />}

      {data && !term && connected.length > 0 && (
        <section>
          <h2>Connected</h2>
          <div className="app-grid">
            {connected.map((a) => (
              <Tile key={a.id} app={a} onOpen={() => setOpen(a.id)} />
            ))}
          </div>
        </section>
      )}
      {data && !term && (
        <section>
          <h2>Popular</h2>
          <div className="app-grid">
            {apps
              .filter((a) => a.popular && !a.connected)
              .map((a) => (
                <Tile key={a.id} app={a} onOpen={() => setOpen(a.id)} />
              ))}
          </div>
        </section>
      )}
      {data &&
        data.categories.map((c) => {
          const list = shown.filter((a) => a.category === c && (term || !a.connected));
          return (
            list.length > 0 && (
              <section key={c}>
                <h2>{c}</h2>
                <div className="app-grid">
                  {list.map((a) => (
                    <Tile key={a.id} app={a} onOpen={() => setOpen(a.id)} />
                  ))}
                </div>
              </section>
            )
          );
        })}

      {data && term.length >= 2 && (
        <section className="directory">
          <h2>More from the MCP directory</h2>
          {!found && (
            <button onClick={() => searchDirectory()} disabled={searching}>
              <Search size={15} aria-hidden="true" /> {searching ? "Searching…" : `Search for “${q.trim()}”`}
            </button>
          )}
          {found?.length === 0 && <p className="muted">Nothing there either. If the app has an MCP link, add it below.</p>}
          {found && found.length > 0 && (
            <ul className="list">
              {found.map((f) => (
                <li key={f.url}>
                  <AppLogo app={{ name: f.name, icon: null }} size={34} />
                  <span className="grow">
                    <b>{f.name}</b>
                    <span className="muted small block">{f.description || new URL(f.url).host}</span>
                  </span>
                  <button onClick={() => setAdding(f)}>Add</button>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {data && (
        <section className="panel add-own">
          <h2>
            <Link2 size={15} aria-hidden="true" /> Don't see yours?
          </h2>
          <p className="muted">
            Zapier and Rube reach thousands of apps through one connection. Any app with an MCP link can be added
            directly.
          </p>
          <button onClick={() => setAdding({})}>Add an app by link</button>
        </section>
      )}

      {current && (
        <AppSheet
          app={current}
          me={me}
          onClose={() => setOpen(null)}
          onChanged={reload}
          onActivity={() => {
            setOpen(null);
            onActivity(current.id);
          }}
        />
      )}
      {adding && (
        <AddSheet
          initial={adding}
          onClose={() => setAdding(null)}
          onAdded={(id) => {
            setAdding(null);
            reload();
            setOpen(id);
          }}
        />
      )}
    </Page>
  );
}

/** The card Riki puts in the chat when it needs an app. A key is typed here and goes straight to the server:
 *  it never becomes a chat message, so no model, memory or log ever sees it. */
export function ConnectCard({ appId, reason, onOpenApps, onShown }: { appId: string; reason?: string; onOpenApps: (id: string) => void; onShown?: () => void }) {
  const [data, , reload] = useLoad<{ apps: App[] }>("/apps");
  const [token, setToken] = useState("");
  const [working, setWorking] = useState(false);
  const app = data?.apps.find((a) => a.id === appId);
  useEffect(() => {
    if (app) onShown?.(); // it appears after the page loaded the app list: keep it in view above the composer
  }, [!!app]);
  if (!app) return null;
  const done = app.connected && app.state === "connected";

  async function connect(e: FormEvent) {
    e.preventDefault();
    setWorking(true);
    try {
      const r = await post<App>(`/apps/${appId}/connect`, { token });
      if (r.state !== "error") toast(`${app!.name} connected`); // a failure shows inside the card
      setToken("");
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setWorking(false);
      reload();
    }
  }

  return (
    <div className="connect-card" role="group" aria-label={`Connect ${app.name}`}>
      <div className="connect-head">
        <AppLogo app={app} size={36} />
        <div>
          <strong>{done ? `${app.name} is connected` : `Connect ${app.name}`}</strong>
          <p className="small muted">{done ? "Ready to use." : (reason || app.blurb).slice(0, 160)}</p>
        </div>
        {done && <Check size={18} strokeWidth={2.5} aria-hidden="true" />}
      </div>
      {!done && app.auth === "token" && (
        <form className="connect-form stack" onSubmit={connect}>
          <label>
            {app.token_label ?? "Key"}
            <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" spellCheck={false} placeholder="Paste it here, not in the chat" />
          </label>
          {app.state === "error" && app.error && <p className="small error-text" role="alert">{app.error}</p>}
          <button className="primary wide" disabled={working || !token.trim()}>
            {working ? "Connecting…" : "Connect"}
          </button>
          {app.token_help && (
            <a className="small" href={app.token_help} target="_blank" rel="noreferrer">
              Get a key <ExternalLink size={12} />
            </a>
          )}
        </form>
      )}
      {!done && app.auth !== "token" && (
        <button className="primary wide" onClick={() => onOpenApps(appId)}>
          Connect {app.name}
        </button>
      )}
    </div>
  );
}

const GROUPS: [Risk, string, string][] = [
  ["safe", "Does on its own", "Looks things up. Nothing changes."],
  ["write", "Asks you first", "Changes something. You'll see exactly what before it runs."],
  ["off", "Off while read only", "Switch to “Ask before changes” to allow these, one approval at a time."],
  ["external", "Never does", "Reaches other people, so it's always refused."],
];
const human = (name: string) => name.replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();

export function AppSheet({ app, me, onClose, onChanged, onActivity }: { app: App; me: Me; onClose: () => void; onChanged: () => void; onActivity: () => void }) {
  const [token, setToken] = useState("");
  const [client, setClient] = useState({ id: "", secret: "" });
  const [working, setWorking] = useState(false);
  const [mode, setLocalMode] = useState(app.mode); // chosen before connecting
  const shownMode = app.connected ? app.mode : mode;
  const host = (() => {
    try {
      return new URL(app.url).host;
    } catch {
      return app.url;
    }
  })();
  const redirect = `${location.origin}/api/apps/callback`;

  async function connect() {
    const needsBrowser = app.auth === "oauth" || app.auth === "google";
    const popup = needsBrowser ? signInWindow() : null; // opened now, while the click still counts, or it gets blocked
    setWorking(true);
    try {
      const r = await post<App>(`/apps/${app.id}/connect`, {
        token: token || undefined,
        client_id: client.id || undefined,
        client_secret: client.secret || undefined,
        mode: shownMode,
      });
      if (r.auth_url && popup) popup.location.href = r.auth_url;
      else popup?.close();
      if (r.state === "error") toast(`${app.name}: ${r.error ?? "couldn't connect"}`, "error");
      setToken(""); // "connected" is announced by the page when it sees the change
    } catch (e) {
      popup?.close();
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setWorking(false);
      onChanged();
    }
  }

  async function setMode(mode: App["mode"]) {
    setWorking(true);
    try {
      await api(`/apps/${app.id}`, { method: "PATCH", body: JSON.stringify({ mode }) });
      toast(mode === "read" ? `${app.name} is read only` : `${me.agent_name} will ask before changing things in ${app.name}`);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setWorking(false);
      onChanged();
    }
  }

  async function disconnect() {
    if (!confirm(`Disconnect ${app.name}? Its sign-in is deleted. What ${me.agent_name} already did stays in Activity.`)) return;
    await api(`/apps/${app.id}`, { method: "DELETE" });
    toast(`${app.name} disconnected`);
    onChanged();
    onClose();
  }

  const [dot, label] = STATE[app.state];
  const ready = app.auth !== "token" || token.trim() || (app.connected && app.state !== "signin");
  const googleReady = app.auth !== "google" || (client.id.trim() && client.secret.trim()) || app.connected;

  return (
    <Sheet title={app.name} onClose={onClose}>
      <div className="app-hero">
        <AppLogo app={app} size={64} />
        <div className="grow">
          <p className="app-hero-blurb">{app.blurb}</p>
          <p className="muted small">
            {app.category} · {host}
          </p>
        </div>
      </div>

      {app.connected && (
        <p className={`app-status ${dot}`}>
          <i className={`dot ${dot}`} /> {label}
          {app.state === "connected" && app.uses_week > 0 && (
            <>
              {" "}
              · used {app.uses_week} time{app.uses_week === 1 ? "" : "s"} this week ·{" "}
              <button className="link inline" onClick={onActivity}>
                See activity
              </button>
            </>
          )}
          {app.state === "error" && app.error && <span className="block muted small">{app.error}</span>}
        </p>
      )}

      <h3 className="sheet-section first">What {me.agent_name} may do</h3>
      <div className="segmented mode" role="group" aria-label="Permission">
        {(["read", "ask"] as const).map((m) => (
          <button
            key={m}
            className={shownMode === m ? "on" : ""}
            aria-pressed={shownMode === m}
            disabled={working || (app.connected && app.state !== "connected")}
            onClick={() => (app.connected ? m !== app.mode && setMode(m) : setLocalMode(m))}
          >
            {m === "read" ? "Read only" : "Ask before changes"}
          </button>
        ))}
      </div>
      <p className="muted small">
        {shownMode === "read"
          ? `${me.agent_name} can look but can't change anything in ${app.name}.`
          : `${me.agent_name} asks you before every change, and shows you exactly what it will do.`}{" "}
        Sending, sharing and inviting are never allowed.
      </p>

      {app.state === "signin" && app.auth_url && (
        <button className="primary wide" onClick={() => signInWindow(app.auth_url!)}>
          Sign in with {app.name} <ExternalLink size={15} aria-hidden="true" />
        </button>
      )}

      {(!app.connected || app.state === "error" || (app.state === "signin" && !app.auth_url)) && (
        <div className="stack connect-form">
          {app.auth === "token" && (
            <label>
              {app.token_label ?? "Key"}
              <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" placeholder="Paste it here" />
              {app.token_help && (
                <a className="small" href={app.token_help} target="_blank" rel="noreferrer">
                  Where do I get this? <ExternalLink size={12} aria-hidden="true" />
                </a>
              )}
            </label>
          )}
          {app.auth === "google" && !app.connected && (
            <>
              <p className="muted small">
                Google asks every personal app to use its own sign-in key. One-time setup, about five minutes: in{" "}
                <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">
                  Google Cloud
                </a>{" "}
                create an OAuth client (Web application), turn on the {app.name} API, and add this as a redirect URI:
              </p>
              <code className="copy" onClick={() => navigator.clipboard?.writeText(redirect).then(() => toast("Copied"))} title="Click to copy">
                {redirect}
              </code>
              <label>
                Client ID
                <input value={client.id} onChange={(e) => setClient({ ...client, id: e.target.value })} autoComplete="off" />
              </label>
              <label>
                Client secret
                <input type="password" value={client.secret} onChange={(e) => setClient({ ...client, secret: e.target.value })} autoComplete="off" />
              </label>
            </>
          )}
          <button className="primary wide" disabled={working || !ready || !googleReady} onClick={connect}>
            {working
              ? "Connecting…"
              : app.connected
                ? "Try again"
                : app.auth === "oauth" || app.auth === "google"
                  ? `Sign in with ${app.name}`
                  : `Connect ${app.name}`}
          </button>
        </div>
      )}

      {app.state === "connected" && (
        <div className="tool-groups">
          {GROUPS.map(([risk, title, note]) => {
            const list = app.tools.filter((t) => t.risk === risk);
            if (!list.length) return null;
            return (
              <details key={risk} className={`tool-group risk-${risk}`} open={risk !== "external"}>
                <summary>
                  <span className="grow">
                    {title} <span className="muted">· {list.length}</span>
                  </span>
                  <ChevronRight size={15} className="chev" aria-hidden="true" />
                </summary>
                <p className="muted small">{note}</p>
                <p className="tool-names">{list.map((t) => human(t.name)).join(" · ")}</p>
              </details>
            );
          })}
        </div>
      )}

      {app.connected && (
        <button className="link danger disconnect" onClick={disconnect}>
          Disconnect {app.name}
        </button>
      )}
    </Sheet>
  );
}

function AddSheet({ initial, onClose, onAdded }: { initial: Partial<Found>; onClose: () => void; onAdded: (id: string) => void }) {
  const [name, setName] = useState(initial.name ?? "");
  const [url, setUrl] = useState(initial.url ?? "");
  const [token, setToken] = useState("");
  const [working, setWorking] = useState(false);
  async function add(e: FormEvent) {
    e.preventDefault();
    setWorking(true);
    try {
      const r = await post<{ id: string; state: string; auth_url: string | null; error: string | null }>("/apps/custom", {
        name: name.trim(),
        url: url.trim(),
        token: token.trim() || undefined,
        header: initial.header || undefined,
      });
      if (r.state === "error") toast(`${name}: ${r.error ?? "couldn't connect"}`, "error");
      onAdded(r.id);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "error");
      setWorking(false);
    }
  }
  return (
    <Sheet title="Add an app" onClose={onClose}>
      <form className="form" onSubmit={add}>
        <p className="muted">
          Paste the app's MCP link (it usually ends in <code>/mcp</code>). If it needs a sign-in, you'll get a button for it next.
        </p>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} required placeholder="e.g. My notes" />
        </label>
        <label>
          Link
          <input value={url} onChange={(e) => setUrl(e.target.value)} required placeholder="https://…/mcp" inputMode="url" />
        </label>
        <label>
          {initial.header ? `Key (${initial.header})` : "Key, if it gave you one"}
          <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" required={!!initial.header} />
        </label>
        <p className="muted small">Apps you add yourself always ask before changing anything, until they show they only read.</p>
        <button className="primary wide" disabled={working || !name.trim() || !/^https:\/\/|^http:\/\/(localhost|127\.0\.0\.1)/.test(url.trim())}>
          {working ? "Connecting…" : "Add and connect"}
        </button>
      </form>
    </Sheet>
  );
}
