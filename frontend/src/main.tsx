import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "@fontsource-variable/bricolage-grotesque";
import { Bell, BookBookmark as BookMarked, Eraser, ClockCounterClockwise as History, House, SquaresFour as LayoutGrid, SignOut as LogOut, ChatCircle as MessageCircle, PencilSimple as Pencil, Plus, PaperPlaneTilt as Send, GearSix as Settings, X } from "@phosphor-icons/react";
import { FormEvent, StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { Live, Me, Status, TelegramStatus, Thread, ago, api, post } from "./api";
import { AppsPage } from "./Apps";
import { Avatar, Mood } from "./Avatar";
import { Chat } from "./Chat";
import { ActivityPage, HomePage } from "./Home";
import { Island } from "./Island";
import { Onboarding } from "./Onboarding";
import { ApprovalsPage, MemoryPage } from "./Pages";
import { SettingsPage, Tab as SettingsTab } from "./Settings";
import "./styles.css";
import "./settings.css";
import { Sheet, Toaster, toast } from "./ui";

const TABS = [
  ["Home", House],
  ["Chat", MessageCircle],
  ["Apps", LayoutGrid],
  ["Memory", BookMarked],
  ["Activity", History],
] as const;
type Tab = (typeof TABS)[number][0];
type SheetName = "settings" | "approvals" | "chats" | null;

/** Sign-in: an emailed code when the server has email set up, otherwise the password. */
function Login({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState<boolean | null>(null);
  useEffect(() => {
    api<{ email: boolean }>("/login/mode")
      .then((m) => setEmail(m.email))
      .catch(() => setEmail(false));
  }, []);
  if (email === null) return <main className="login" aria-busy="true" />;
  return email ? <EmailLogin onDone={onDone} /> : <PasswordLogin onDone={onDone} />;
}

function PasswordLogin({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await post("/login", { password });
      onDone();
    } catch {
      setError("That password didn't work.");
      setBusy(false);
    }
  }
  return (
    <main className="login">
      <form onSubmit={submit} className={error ? "shake" : ""} key={error}>
        <Avatar mood={busy ? "thinking" : password ? "listening" : "idle"} name="Riki" size={72} />
        <h1>Welcome back.</h1>
        <input
          type="password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          aria-label="Password"
          placeholder="Password"
          autoComplete="current-password"
        />
        <button className="primary wide" disabled={!password || busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        {error && <p className="field-error">{error}</p>}
      </form>
    </main>
  );
}

/** Email, then the 6-digit code from your inbox. The browser is remembered, so this happens once per device. */
function EmailLogin({ onDone }: { onDone: () => void }) {
  const [address, setAddress] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [wait, setWait] = useState(0); // seconds until another code may be asked for
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (wait <= 0) return;
    const t = setTimeout(() => setWait(wait - 1), 1000);
    return () => clearTimeout(t);
  }, [wait]);

  async function send(e?: FormEvent) {
    e?.preventDefault();
    setBusy(true);
    setError("");
    try {
      await post("/login/code", { email: address.trim() });
      setSent(true);
      setCode("");
      setWait(30);
    } catch (err) {
      setError(err instanceof Error && (err as { status?: number }).status === 429 ? err.message : "Couldn't send that. Try again in a moment.");
    }
    setBusy(false);
  }

  async function verify(digits: string) {
    setBusy(true);
    setError("");
    try {
      await post("/login/verify", { email: address.trim(), code: digits });
      onDone();
    } catch {
      setError("That code didn't work. Ask for a new one.");
      setCode("");
      setBusy(false);
    }
  }

  const typing = sent ? code : address;
  return (
    <main className="login">
      <form onSubmit={sent ? (e) => (e.preventDefault(), code.length === 6 && verify(code)) : send} className={error ? "shake" : ""} key={error}>
        <Avatar mood={busy ? "thinking" : typing ? "listening" : "idle"} name="Riki" size={72} />
        <h1>{sent ? "Check your email." : "Welcome back."}</h1>
        {sent ? (
          <>
            <p className="muted login-sub">If {address.trim()} is the right address, a 6-digit code is on its way.</p>
            <input
              className="otp"
              autoFocus
              value={code}
              inputMode="numeric"
              pattern="[0-9]*"
              maxLength={6}
              autoComplete="one-time-code"
              aria-label="6-digit code"
              placeholder="000000"
              onChange={(e) => {
                const digits = e.target.value.replace(/\D/g, "").slice(0, 6);
                setCode(digits);
                if (digits.length === 6) verify(digits);
              }}
            />
            <button className="primary wide" disabled={code.length < 6 || busy}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
            <div className="row center">
              <button type="button" className="link" disabled={wait > 0 || busy} onClick={() => send()}>
                {wait > 0 ? `Send again in ${wait}s` : "Send a new code"}
              </button>
              <button type="button" className="link" onClick={() => (setSent(false), setError(""))}>
                Use another email
              </button>
            </div>
          </>
        ) : (
          <>
            <input
              type="email"
              autoFocus
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              aria-label="Email"
              placeholder="you@example.com"
              autoComplete="email"
            />
            <button className="primary wide" disabled={!address.includes("@") || busy}>
              {busy ? "Sending…" : "Send code"}
            </button>
          </>
        )}
        {error && <p className="field-error">{error}</p>}
      </form>
    </main>
  );
}

/** Same titles get a number, so six "Compare FastAPI…" chats can still be told apart. */
function dedupe(threads: Thread[]): (Thread & { n: number })[] {
  const seen = new Map<string, number>();
  return [...threads]
    .reverse()
    .map((t) => {
      const n = (seen.get(t.title) ?? 0) + 1;
      seen.set(t.title, n);
      return { ...t, n };
    })
    .reverse();
}

function ChatRow(props: { t: Thread & { n: number }; on: boolean; onOpen: () => void; onChanged: () => void; onDelete: () => void }) {
  const { t } = props;
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState(t.title);
  const save = async (e?: FormEvent) => {
    e?.preventDefault();
    if (title.trim() && title.trim() !== t.title) {
      await api(`/threads/${t.id}`, { method: "PATCH", body: JSON.stringify({ title: title.trim() }) });
      props.onChanged();
    }
    setRenaming(false);
  };
  if (renaming)
    return (
      <form className="chat-row renaming" onSubmit={save}>
        <input autoFocus value={title} maxLength={80} onChange={(e) => setTitle(e.target.value)} onBlur={() => save()} aria-label="Chat name" />
      </form>
    );
  return (
    <div className={`chat-row ${props.on ? "on" : ""}`}>
      <button className="chat-row-open" onClick={props.onOpen} aria-current={props.on ? "true" : undefined}>
        <span className="chat-row-top">
          <span className="chat-row-title">{t.title}</span>
          {t.n > 1 && <span className="chat-row-n">{t.n}</span>}
          <time>{ago(t.last_at)}</time>
        </span>
        {t.preview && <span className="chat-row-preview">{t.preview}</span>}
      </button>
      <span className="chat-row-actions">
        <button className="icon small" aria-label={`Rename ${t.title}`} title="Rename" onClick={() => setRenaming(true)}>
          <Pencil size={14} />
        </button>
        <button
          className="icon small"
          aria-label={`Delete ${t.title}`}
          title="Delete"
          onClick={props.onDelete}
        >
          <X size={14} />
        </button>
      </span>
    </div>
  );
}

/** Main chat, the permanent Telegram chat, then your topic chats. Used in the sidebar and in the phone's Chats sheet. */
function ChatsList(props: {
  me: Me;
  tg: (Thread & { telegram: TelegramStatus }) | null;
  sides: Thread[] | null;
  threadId: string | null;
  mainId: string | null;
  openThread: (id: string | null) => void;
  loadSides: () => void;
  removeChat: (id: string) => void;
}) {
  const { me, tg, sides, threadId, mainId, openThread, loadSides, removeChat } = props;
  return (
    <div className="chat-list">
      <div className={`chat-row ${threadId === mainId ? "on" : ""}`}>
        <button className="chat-row-open main" onClick={() => openThread(mainId)} aria-current={threadId === mainId ? "true" : undefined}>
          <span className="chat-row-top">
            <span className="chat-row-title">Main chat</span>
          </span>
          <span className="chat-row-preview">Your ongoing conversation with {me.agent_name}</span>
        </button>
        <span className="chat-row-actions">
          <button className="icon small" aria-label="Clear the main chat" title="Clear" onClick={() => mainId && removeChat(mainId)}>
            <Eraser size={14} />
          </button>
        </span>
      </div>
      {tg && (
        <div className={`chat-row tg ${threadId === tg.id ? "on" : ""}`}>
          <button className="chat-row-open" onClick={() => openThread(tg.id)} aria-current={threadId === tg.id ? "true" : undefined}>
            <span className="chat-row-icon">
              <Send size={16} aria-hidden="true" />
            </span>
            <span className="chat-row-text">
              <span className="chat-row-top">
                <span className="chat-row-title">Telegram</span>
                {tg.preview && <time>{ago(tg.last_at)}</time>}
              </span>
              <span className="chat-row-preview">
                {tg.preview ||
                  (tg.telegram.paired
                    ? `Connected${tg.telegram.who ? ` to ${tg.telegram.who}` : ""}. Messages from your phone appear here.`
                    : tg.telegram.connected
                      ? "Finish pairing to start"
                      : `Not connected. Tap to talk to ${me.agent_name} from your phone.`)}
              </span>
            </span>
          </button>
          {tg.preview && (
            <span className="chat-row-actions">
              <button className="icon small" aria-label="Clear the Telegram chat" title="Clear" onClick={() => removeChat(tg.id)}>
                <Eraser size={14} />
              </button>
            </span>
          )}
        </div>
      )}
      {sides?.length === 0 && <p className="muted small chat-list-empty">Start a separate chat for one topic. It keeps the main chat tidy.</p>}
      {sides &&
        dedupe(sides).map((t) => (
          <ChatRow
            key={t.id}
            t={t}
            on={threadId === t.id}
            onOpen={() => openThread(t.id)}
            onChanged={loadSides}
            onDelete={() => removeChat(t.id)}
          />
        ))}
    </div>
  );
}

function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [tab, setTab] = useState<Tab>("Home");
  const [appFocus, setAppFocus] = useState<string | null>(null); // an app a chat card sent you to
  const [sheet, setSheet] = useState<SheetName>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [mainId, setMainId] = useState<string | null>(null);
  const [tg, setTg] = useState<(Thread & { telegram: TelegramStatus }) | null>(null); // the permanent Telegram chat
  const [settingsTab, setSettingsTab] = useState<SettingsTab | undefined>(undefined);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [sides, setSides] = useState<Thread[] | null>(null);
  const [chatMood, setChatMood] = useState<{ mood: Mood; line: string }>({ mood: "idle", line: "" });
  const [live, setLive] = useState<Live>({ active: [], asks: [] }); // what the server is doing right now
  const [chatKey, setChatKey] = useState(0); // changes only when you switch chats, not when a new chat gets its id
  const [initial, setInitial] = useState<string | null>(null); // a message typed on Home, sent once Chat opens
  const [activityQ, setActivityQ] = useState(""); // set when another page opens Activity filtered
  const moodTimer = useRef<number>();

  const loadMe = () =>
    api<Me>("/me")
      .then(setMe)
      .catch(() => setMe(null)); // 401 or server down: show sign-in
  const loadStatus = () =>
    api<Status>("/status")
      .then(setStatus)
      .catch(() => setStatus(null));
  const loadTg = () => api<Thread & { telegram: TelegramStatus }>("/threads/telegram").then(setTg).catch(() => {});
  const loadSides = () => api<Thread[]>("/threads").then(setSides).catch(() => {});
  const [outdated, setOutdated] = useState(false); // the server doesn't know /live: it is running older code than this page
  const loadLive = () =>
    api<Live>("/live")
      .then((l) => (setLive(l), setOutdated(false)))
      .catch((e) => setOutdated(e?.status === 404));

  /** Delete a topic chat; the main chat can only be cleared. Asks first, and says why when it can't. */
  async function removeChat(id: string) {
    const isTg = id === tg?.id;
    const isMain = id === mainId || isTg;
    const name = isTg ? "the Telegram chat" : isMain ? "the main chat" : `“${sides?.find((t) => t.id === id)?.title ?? "this chat"}”`;
    const ask = isMain
      ? `Clear ${name}? The messages go; what Riki learned stays in Memory and what it did stays in Activity.`
      : `Delete ${name}? What Riki learned stays in Memory and what it did stays in Activity.`;
    if (!confirm(ask)) return;
    try {
      await api(`/threads/${id}`, { method: "DELETE" });
    } catch (e) {
      toast(e instanceof Error ? e.message : "Couldn't do that.");
      return;
    }
    toast(isMain ? "Chat cleared" : "Chat deleted");
    loadSides();
    loadTg();
    if (threadId === id) {
      if (!isMain) setThreadId(mainId);
      setChatKey((k) => k + 1); // reload what the chat shows
    }
  }

  useEffect(() => {
    loadMe();
  }, []);
  useEffect(() => {
    if (!me) return;
    api<Thread>("/threads/main").then((t) => {
      setMainId(t.id);
      setThreadId((cur) => cur ?? t.id);
    });
    const owner = me.account === "owner"; // a member has no server status or phone: those endpoints are the owner's
    loadSides();
    if (owner) {
      loadTg();
      loadStatus();
    }
    loadLive();
    const slow = owner ? setInterval(loadStatus, 6000) : undefined;
    const fast = setInterval(() => !document.hidden && loadLive(), 2000); // asks and progress, on any page
    return () => (clearInterval(slow), clearInterval(fast));
  }, [me?.name]);

  useEffect(() => {
    if (me?.account === "owner") loadTg(); // the phone may have been connected or written to meanwhile
  }, [sheet, tab]);

  function onMood(mood: Mood, line: string) {
    window.clearTimeout(moodTimer.current);
    setChatMood({ mood, line });
    if (mood === "happy" || mood === "error") {
      loadStatus();
      loadSides();
      moodTimer.current = window.setTimeout(() => setChatMood({ mood: "idle", line: "" }), 2200);
    }
  }

  if (me === undefined) return <div className="boot" aria-busy="true" />;
  if (me === null) return <Login onDone={loadMe} />;
  if (!me.onboarded) return <Onboarding me={me} onDone={loadMe} />;

  const owner = me.account === "owner";
  const offline = owner && status === null;
  const pending = live.asks.length;
  // Any run the server is working on counts, not only the one in the chat that is open
  const running = live.active[0];
  const mood: Mood =
    chatMood.mood !== "idle" && chatMood.mood !== "listening"
      ? chatMood.mood
      : offline
        ? "sleeping"
        : pending
          ? "waiting"
          : running
            ? "working"
            : chatMood.mood;
  const line = chatMood.line || (running ? (running.source === "chat" ? `Working on “${running.input.slice(0, 50)}”` : "Checking in on things") : "");
  const presence = offline ? "Offline" : mood === "waiting" ? "Needs you" : mood === "thinking" || mood === "working" ? "Working" : "Ready";

  const openThread = (id: string | null) => {
    setThreadId(id);
    setChatKey((k) => k + 1);
    setTab("Chat");
    setSheet(null);
  };
  const openSettings = (tab?: SettingsTab) => {
    setSettingsTab(tab);
    setSheet("settings");
  };
  const go = (s: "Approvals" | "Memory" | "Settings") =>
    s === "Memory" ? setTab("Memory") : s === "Approvals" ? setSheet("approvals") : openSettings();
  const chatTitle = threadId === mainId ? "Main chat" : threadId === tg?.id ? "Telegram" : (sides?.find((t) => t.id === threadId)?.title ?? "New chat");

  const tabButtons = (cls: string) =>
    TABS.filter(([t]) => owner || t !== "Apps").map(([t, Icon]) => (
      <button
        key={t}
        className={`${cls} ${t === tab ? "on" : ""}`}
        aria-current={t === tab ? "page" : undefined}
        onClick={() => {
          setActivityQ("");
          t === "Chat" && tab !== "Chat" ? openThread(threadId ?? mainId) : setTab(t);
        }}
      >
        <Icon size={cls === "dock-tab" ? 24 : 19} weight={t === tab ? "fill" : "regular"} aria-hidden="true" />
        <span>{t}</span>
      </button>
    ));

  return (
    <div className="app">
      <aside className="rail" aria-label="Riki">
        <button className="presence" onClick={() => openSettings()} aria-label={`${me.agent_name}, ${presence}. Open settings`}>
          <Avatar mood={mood} src={me.avatar} name={me.agent_name} size={36} />
          <span className="presence-text">
            <span className="presence-name">{me.agent_name}</span>
            <span className={`presence-state ${presence === "Offline" ? "off" : presence === "Needs you" ? "wait" : ""}`}>{presence}</span>
          </span>
        </button>
        <button className="primary new-chat-rail" onClick={() => openThread(null)}>
          <Plus size={16} weight="bold" aria-hidden="true" /> New chat
        </button>
        <nav className="rail-nav" aria-label="Sections">
          {tabButtons("nav-item")}
        </nav>
        <p className="rail-label">Chats</p>
        <div className="rail-chats">
          <ChatsList me={me} tg={tg} sides={sides} threadId={tab === "Chat" ? threadId : null} mainId={mainId} openThread={openThread} loadSides={loadSides} removeChat={removeChat} />
        </div>
        <div className="rail-foot">
          <button className="nav-item" onClick={() => setSheet("approvals")}>
            <Bell size={19} aria-hidden="true" />
            <span>Approvals</span>
            {pending > 0 && <span className="rail-badge">{pending}</span>}
          </button>
          <button className="nav-item" onClick={() => openSettings()}>
            <Settings size={19} aria-hidden="true" />
            <span>Settings</span>
            {status && !status.ok && <span className="rail-badge alert" aria-label="Needs attention" />}
          </button>
        </div>
      </aside>

      <div className="stage-col">
        <header className="topbar">
          <button className="presence" onClick={() => openSettings()} aria-label={`${me.agent_name}, ${presence}. Open settings`}>
            <Avatar mood={mood} src={me.avatar} name={me.agent_name} size={30} />
            <span className="presence-text">
              <span className="presence-name">{me.agent_name}</span>
              <span className={`presence-state ${presence === "Offline" ? "off" : presence === "Needs you" ? "wait" : ""}`}>{presence}</span>
            </span>
          </button>
          <div className="top-actions">
            <button className="icon round" aria-label="New chat" title="New chat" onClick={() => openThread(null)}>
              <Plus size={18} />
            </button>
            <button className="icon round" aria-label={pending ? `${pending} waiting for your OK` : "Approvals"} title="Approvals" onClick={() => setSheet("approvals")}>
              <Bell size={18} />
              {pending > 0 && <span className="badge-dot">{pending}</span>}
            </button>
            <button className="icon round" aria-label="Settings" title="Settings" onClick={() => openSettings()}>
              <Settings size={18} />
              {status && !status.ok && <span className="badge-dot alert" aria-hidden="true" />}
            </button>
          </div>
        </header>

        {outdated && (
          <p className="notice error stale" role="alert">
            Riki's server is running older code than this page, so replies in progress and approvals can't show up here. Stop the backend and start it
            again.
          </p>
        )}
        <Island me={me} mood={mood} line={line} asks={live.asks} onDecided={loadLive} onReview={() => setSheet("approvals")} />

        <main className="content" key={tab}>
          {tab === "Home" && <HomePage me={me} mood={mood} onAsk={(text, fresh) => { setInitial(text); openThread(fresh ? null : mainId); }} onOpenThread={openThread} onDeleteThread={removeChat} onGo={go} />}
          {tab === "Chat" && (
            <Chat
              key={chatKey}
              me={me}
              threadId={threadId}
              isMain={threadId !== null && (threadId === mainId || threadId === tg?.id)}
              channel={threadId !== null && threadId === tg?.id ? { paired: tg.telegram.paired, bot: tg.telegram.bot, onConnect: () => openSettings("Channels") } : undefined}
              title={chatTitle}
              initialMessage={initial}
              onInitialSent={() => setInitial(null)}
              onThread={(id) => {
                setThreadId(id);
                loadSides();
              }}
              onPulse={loadLive}
              onDelete={() => threadId && removeChat(threadId)}
              onSwitch={() => setSheet("chats")}
              onMood={onMood}
              onAuthLost={() => setMe(null)}
              onOpenApps={(id) => {
                setAppFocus(id);
                setTab("Apps");
              }}
            />
          )}
          {tab === "Apps" && (
            <AppsPage
              me={me}
              initialOpen={appFocus}
              onOpened={() => setAppFocus(null)}
              onActivity={(q) => {
                setActivityQ(q);
                setTab("Activity");
              }}
            />
          )}
          {tab === "Memory" && <MemoryPage me={me} onOpenThread={openThread} />}
          {tab === "Activity" && <ActivityPage me={me} onOpenThread={openThread} initialQuery={activityQ} />}
        </main>

        <nav className="dock-tabs" aria-label="Sections">
          {tabButtons("dock-tab")}
        </nav>
      </div>

      {sheet === "chats" && (
        <Sheet title="Chats" onClose={() => setSheet(null)}>
          <button className="primary wide new-chat" onClick={() => openThread(null)}>
            <Plus size={16} aria-hidden="true" /> New chat
          </button>
          <ChatsList me={me} tg={tg} sides={sides} threadId={threadId} mainId={mainId} openThread={openThread} loadSides={loadSides} removeChat={removeChat} />
        </Sheet>
      )}
      {sheet === "approvals" && (
        <Sheet title="Approvals" onClose={() => setSheet(null)}>
          <ApprovalsPage me={me} onChanged={() => (loadStatus(), loadLive())} />
        </Sheet>
      )}
      {sheet === "settings" && (
        <Sheet title="Settings" onClose={() => setSheet(null)} wide>
          {!owner && (
            <div className="settings-body">
              <h3 className="settings-title">Your account</h3>
              <p className="muted section-lead">
                Signed in as {me.name}. Your chats and memory are private to you. Connected apps, files on the server and scheduled tasks
                belong to the owner's account.
              </p>
            </div>
          )}
          {owner && <SettingsPage
            me={me}
            status={status}
            mood={mood}
            onSaved={loadMe}
            tab={settingsTab}
            onOpenTelegram={() => {
              if (tg) openThread(tg.id);
            }}
          />}
          <div className="settings-foot">
            <span className="muted small grow">Signed in as {me.name}</span>
            {owner && <button
              className="link"
              onClick={async () => {
                await post("/onboarding", { done: false });
                setSheet(null);
                loadMe();
              }}
            >
              Run setup again
            </button>}
            {me.email_login && (
              <button
                className="link"
                title="Forget every browser that was remembered, including this one"
                onClick={async () => {
                  if (!confirm("Sign out of every device, including this one?")) return;
                  await post("/logout/all", {});
                  setSheet(null);
                  setMe(null);
                }}
              >
                Sign out everywhere
              </button>
            )}
            <button
              className="link"
              onClick={async () => {
                await post("/logout", {});
                setSheet(null);
                setMe(null);
              }}
            >
              <LogOut size={15} aria-hidden="true" /> Sign out
            </button>
          </div>
        </Sheet>
      )}
      <Toaster />
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
