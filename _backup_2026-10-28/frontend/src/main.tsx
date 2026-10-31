import "@fontsource-variable/inter/opsz.css";
import { Bell, BookMarked, Eraser, History, House, LayoutGrid, LogOut, MessageCircle, Pencil, Plus, Settings, X } from "lucide-react";
import { FormEvent, StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { Live, Me, Status, Thread, ago, api, post } from "./api";
import { AppsPage } from "./Apps";
import { Avatar, Mood } from "./Avatar";
import { Chat } from "./Chat";
import { ActivityPage, HomePage } from "./Home";
import { Island } from "./Island";
import { ApprovalsPage, MemoryPage } from "./Pages";
import { SettingsPage } from "./Settings";
import "./styles.css";
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

function Login({ onDone }: { onDone: () => void }) {
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

function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [tab, setTab] = useState<Tab>("Home");
  const [appFocus, setAppFocus] = useState<string | null>(null); // an app a chat card sent you to
  const [sheet, setSheet] = useState<SheetName>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [mainId, setMainId] = useState<string | null>(null);
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
  const loadSides = () => api<Thread[]>("/threads").then(setSides).catch(() => {});
  const [outdated, setOutdated] = useState(false); // the server doesn't know /live: it is running older code than this page
  const loadLive = () =>
    api<Live>("/live")
      .then((l) => (setLive(l), setOutdated(false)))
      .catch((e) => setOutdated(e?.status === 404));

  /** Delete a topic chat; the main chat can only be cleared. Asks first, and says why when it can't. */
  async function removeChat(id: string) {
    const isMain = id === mainId;
    const name = isMain ? "the main chat" : `“${sides?.find((t) => t.id === id)?.title ?? "this chat"}”`;
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
    if (threadId === id) {
      setThreadId(mainId);
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
    loadSides();
    loadStatus();
    loadLive();
    const slow = setInterval(loadStatus, 6000);
    const fast = setInterval(() => !document.hidden && loadLive(), 2000); // asks and progress, on any page
    return () => (clearInterval(slow), clearInterval(fast));
  }, [me?.name]);

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

  const offline = status === null;
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
  const go = (s: "Approvals" | "Memory" | "Settings") =>
    s === "Memory" ? setTab("Memory") : setSheet(s === "Approvals" ? "approvals" : "settings");
  const chatTitle = threadId === mainId ? "Main chat" : (sides?.find((t) => t.id === threadId)?.title ?? "New chat");

  const tabButtons = (cls: string) =>
    TABS.map(([t, Icon]) => (
      <button
        key={t}
        className={`${cls} ${t === tab ? "on" : ""}`}
        aria-current={t === tab ? "page" : undefined}
        onClick={() => {
          setActivityQ("");
          t === "Chat" && tab !== "Chat" ? openThread(threadId ?? mainId) : setTab(t);
        }}
      >
        <Icon size={cls === "dock-tab" ? 22 : 16} strokeWidth={1.8} aria-hidden="true" />
        <span>{t}</span>
      </button>
    ));

  return (
    <div className="app">
      <header className="topbar">
        <button className="presence" onClick={() => setSheet("settings")} aria-label={`${me.agent_name}, ${presence}. Open settings`}>
          <Avatar mood={mood} src={me.avatar} name={me.agent_name} size={30} />
          <span className="presence-text">
            <span className="presence-name">{me.agent_name}</span>
            <span className={`presence-state ${presence === "Offline" ? "off" : presence === "Needs you" ? "wait" : ""}`}>{presence}</span>
          </span>
        </button>
        <nav className="tabbar" aria-label="Sections">
          {tabButtons("tab")}
        </nav>
        <div className="top-actions">
          <button className="icon round" aria-label="New chat" title="New chat" onClick={() => openThread(null)}>
            <Plus size={18} strokeWidth={1.8} />
          </button>
          <button className="icon round" aria-label={pending ? `${pending} waiting for your OK` : "Approvals"} title="Approvals" onClick={() => setSheet("approvals")}>
            <Bell size={18} strokeWidth={1.8} />
            {pending > 0 && <span className="badge-dot">{pending}</span>}
          </button>
          <button className="icon round" aria-label="Settings" title="Settings" onClick={() => setSheet("settings")}>
            <Settings size={18} strokeWidth={1.8} />
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
            isMain={threadId !== null && threadId === mainId}
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

      {sheet === "chats" && (
        <Sheet title="Chats" onClose={() => setSheet(null)}>
          <button className="primary wide new-chat" onClick={() => openThread(null)}>
            <Plus size={16} aria-hidden="true" /> New chat
          </button>
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
        </Sheet>
      )}
      {sheet === "approvals" && (
        <Sheet title="Approvals" onClose={() => setSheet(null)}>
          <ApprovalsPage me={me} onChanged={() => (loadStatus(), loadLive())} />
        </Sheet>
      )}
      {sheet === "settings" && (
        <Sheet title="Settings" onClose={() => setSheet(null)} wide>
          <SettingsPage me={me} status={status} mood={mood} onSaved={loadMe} />
          <div className="settings-foot">
            <span className="muted small">Signed in as {me.name}</span>
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
