import { ArrowUp, ChevronDown, Copy, Gauge, Mic, PanelRight, Paperclip, Sparkles, Square, ThumbsDown, ThumbsUp, Trash2 } from "lucide-react";
import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { Reply, RunDrawer, Steps, liveLine } from "./Activity";
import { ArtifactCard, ArtifactView } from "./Artifacts";
import { ApprovalReq, Artifact, Busy, Effort, Gone, Live, Me, Msg, RunEnd, RunEvent, RunRow, Span, Unauthorized, api, followRun, post, streamChat } from "./api";
import { ConnectCard } from "./Apps";
import { Avatar, Mood } from "./Avatar";
import { Skeleton, Typing, toast, useAutoGrow, useSmoothText } from "./ui";

/** A reply as it streams in: steady reveal, and the list follows it while you're at the bottom. */
function SmoothReply({ text, onCite, onGrow }: { text: string; onCite: (ref: string) => void; onGrow: () => void }) {
  const shown = useSmoothText(text);
  useEffect(onGrow, [shown]);
  return <Reply text={shown} onCite={onCite} />;
}

/** Shrink a picture in the browser (longest side 1568px, JPEG) so a photo from a phone is ~200 KB, not 8 MB. */
async function shrink(file: File): Promise<string> {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, 1568 / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#fff"; // transparent PNGs would turn black as JPEG
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.85);
}

type ChatMsg = Msg & { steps?: Span[]; latency_ms?: number | null };
// live: the run is still going, so the drawer shows the streaming spans instead of a saved trace
type Detail = { run: Partial<RunRow> | null; spans: Span[]; approvals: ApprovalReq[]; focus: string | null; live?: boolean };

const EFFORTS: Effort[] = ["low", "medium", "high"];
// What each effort means to you; the API keeps its own names.
const EFFORT_NAMES: Record<Effort, string> = { low: "Quick", medium: "Normal", high: "Deep" };

// What the composer's "What can you do?" menu offers: real capabilities, each with a starter.
const CAPABILITIES = [
  ["Remember things about you", "Remember that "],
  ["Search the web and cite sources", "Look up "],
  ["Read a web page for you", "Read this page and tell me the key points: "],
  ["Work on several things at once", "Research these separately and compare: "],
  ["Work with your files (asks first)", "In my files, "],
  ["Check in on its own, on a schedule", "What should I be on top of this week?"],
] as const;

type SpeechRec = { start(): void; stop(): void; lang: string; interimResults: boolean; onresult: (e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void; onend: () => void };
const Speech = (window as unknown as { SpeechRecognition?: new () => SpeechRec; webkitSpeechRecognition?: new () => SpeechRec })
  .SpeechRecognition ?? (window as unknown as { webkitSpeechRecognition?: new () => SpeechRec }).webkitSpeechRecognition;

/** Apps Riki asked you to connect in this reply (its connect_app calls), once each. */
function connectAsks(spans: Span[] = []) {
  const asks = new Map<string, string | undefined>();
  for (const s of spans) {
    const a = s.kind === "tool" && s.name === "connect_app" && s.status !== "error" && s.status !== "denied" ? (s.input as { app?: unknown; reason?: unknown } | null) : null;
    if (typeof a?.app === "string") asks.set(a.app, typeof a.reason === "string" ? a.reason : undefined);
  }
  return [...asks];
}

export function Chat(props: {
  me: Me;
  threadId: string | null; // null = new side chat
  isMain: boolean;
  title: string;
  initialMessage?: string | null; // typed on Home: sent as soon as the chat is ready
  onInitialSent?: () => void;
  onThread: (id: string) => void;
  onPulse: () => void; // something changed on the server (a run started, asked or ended): refresh what the Island shows
  onDelete: () => void; // delete this chat (clear, for the main one)
  onSwitch: () => void; // open the chat switcher
  onMood: (mood: Mood, line: string) => void;
  onAuthLost: () => void;
  onOpenApps: (appId: string) => void; // a connect card for an app that signs in on its own page
}) {
  const { me } = props;
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [draft, setDraft] = useState("");
  const [pics, setPics] = useState<string[]>([]); // attached to the next message
  const [effort, setEffort] = useState<Effort>(() => {
    try {
      const saved = localStorage.getItem("riki.effort") as Effort;
      return EFFORTS.includes(saved) ? saved : me.default_effort;
    } catch {
      return me.default_effort;
    }
  });
  const fileInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState<{ spans: Span[]; approvals: ApprovalReq[]; runId: string | null }>({
    spans: [],
    approvals: [],
    runId: null,
  });
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(!!props.threadId);
  const [menu, setMenu] = useState(false);
  const [listening, setListening] = useState(false);
  const rec = useRef<SpeechRec | null>(null);
  const sentInitial = useRef(false);
  const abort = useRef<AbortController | null>(null);
  const ownThread = useRef<string | null>(null); // the chat our own stream just created: its messages are already here
  const input = useRef<HTMLTextAreaElement>(null);
  const list = useRef<HTMLDivElement>(null); // the scrolling message list
  const dock = useRef<HTMLDivElement>(null); // the floating input bar
  const seen = useRef(0); // messages at the last scroll: a history that just loaded jumps, new ones glide
  const pinned = useRef(true); // you're at the bottom: a growing reply keeps it in view; scroll up and it stops
  const freshStep = useRef(false); // a new model step started: its first token replaces the previous step's text
  // Scroll the list itself: scrollIntoView would also move the overflow:hidden page around it.
  const toBottom = (smooth = true) =>
    list.current?.scrollTo({ top: list.current.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  const threadRef = useRef(props.threadId);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]); // made in this chat
  const [openArtifact, setOpenArtifact] = useState<string | null>(null);

  const loadArtifacts = (id: string | null) =>
    id
      ? api<Artifact[]>(`/artifacts?thread_id=${id}`).then((r) => threadRef.current === id && setArtifacts(r)).catch(() => {})
      : setArtifacts([]);

  useEffect(() => {
    threadRef.current = props.threadId;
    setDetail(null);
    setError("");
    loadArtifacts(props.threadId);
    if (!props.threadId) {
      setMessages([]);
      return;
    }
    if (props.threadId === ownThread.current) return;
    let stale = false;
    setLoading(true);
    restore(props.threadId, () => stale)
      .catch((e) => !stale && fail(e))
      .finally(() => !stale && setLoading(false));
    return () => {
      stale = true;
    };
  }, [props.threadId]);

  // Leaving the chat only detaches from the run: it keeps going on the server and is picked up again on return.
  useEffect(
    () => () => {
      abort.current?.abort();
      props.onMood("idle", "");
    },
    [],
  );

  useEffect(() => {
    if (messages.length !== seen.current) {
      pinned.current = true; // your own new message always comes into view
      toBottom(seen.current > 0);
    } else if (pinned.current) toBottom(false); // a smooth scroll per token would stutter
    seen.current = messages.length;
  }, [messages, live.spans.length]);
  const keepInView = () => {
    if (pinned.current) toBottom(false);
  };

  useAutoGrow(input, draft); // pinned to the bottom of the chat, so it grows upward like ChatGPT's

  // Keep the last message clear of the input bar whatever its height.
  useEffect(() => {
    const el = dock.current;
    if (!el) return;
    const fit = () => el.parentElement?.style.setProperty("--dock-h", `${el.offsetHeight}px`);
    fit();
    const watch = new ResizeObserver(fit);
    watch.observe(el);
    return () => watch.disconnect();
  }, []);

  // Layer 1 for the whole app: the avatar's mood and one sentence.
  const line = busy ? liveLine(live.spans, live.approvals, me.agent_name) : "";
  useEffect(() => {
    if (busy) props.onMood(live.approvals.some((a) => !a.status) ? "waiting" : live.spans.some((s) => s.kind === "tool" && s.status === "running") ? "working" : "thinking", line);
    else props.onMood(draft.trim() ? "listening" : "idle", "");
  }, [busy, line, draft.trim().length > 0]);

  function fail(e: unknown) {
    if (e instanceof Unauthorized) props.onAuthLost();
    else setError(e instanceof Error ? e.message : String(e));
  }

  function onEvent(e: RunEvent) {
    switch (e.type) {
      case "thread":
        setLive((l) => ({ ...l, runId: e.run_id }));
        props.onPulse();
        if (e.thread_id !== threadRef.current) {
          threadRef.current = ownThread.current = e.thread_id;
          props.onThread(e.thread_id);
        }
        break;
      case "token": {
        // "Let me check your calendar" belongs to the step before the tools ran; the answer replaces it
        const restart = freshStep.current;
        freshStep.current = false;
        setMessages((m) => {
          const last = m[m.length - 1];
          return [...m.slice(0, -1), { ...last, content: restart ? e.text : last.content + e.text }];
        });
        break;
      }
      case "span_start":
        if (e.span.kind === "llm") freshStep.current = true;
        setLive((l) => ({ ...l, spans: [...l.spans, { ...e.span, status: "running" }] }));
        break;
      case "span_end":
        setLive((l) => ({ ...l, spans: l.spans.map((x) => (x.id === e.span.id ? e.span : x)) }));
        break;
      case "approval":
        setLive((l) => ({ ...l, approvals: [...l.approvals, e.approval] }));
        props.onPulse();
        break;
      case "approval_done":
        setLive((l) => ({
          ...l,
          approvals: l.approvals.map((x) => (x.id === e.approval.id ? { ...x, status: e.approval.status } : x)),
        }));
        props.onPulse();
        break;
      case "run_end":
        finish(e);
        break;
    }
  }

  function finish(e: RunEnd) {
    setLive((l) => {
      // idempotent, so StrictMode's double call is harmless. The final text replaces streamed tokens (which include intermediate model steps)
      setMessages((m) => [
        ...m.slice(0, -2),
        ...(m.length > 1 ? [{ ...m[m.length - 2], run_id: m[m.length - 2].run_id ?? e.run_id }] : []),
        { ...m[m.length - 1], content: e.output, run_id: e.run_id, steps: l.spans, latency_ms: e.latency_ms, error: e.error },
      ]);
      // a drawer opened mid-run keeps the final trace instead of going blank
      setDetail((d) =>
        d?.live
          ? { ...d, live: false, spans: l.spans, approvals: l.approvals,
              run: { ...d.run, id: e.run_id, status: e.status, error: e.error, latency_ms: e.latency_ms,
                     tokens_in: e.tokens_in, tokens_out: e.tokens_out, tokens_cached: e.tokens_cached, cost_usd: e.cost_usd, checks: e.checks } }
          : d,
      );
      return { spans: [], approvals: [], runId: null };
    });
    props.onMood(e.status === "ok" ? "happy" : e.status === "stopped" ? "idle" : "error", "");
    props.onPulse();
    loadArtifacts(threadRef.current);
  }

  /** Follow a run until it ends. Errors reach the caller; leaving the chat just detaches quietly. */
  async function follow(go: (signal: AbortSignal) => Promise<void>) {
    abort.current?.abort();
    const ctl = (abort.current = new AbortController());
    setBusy(true);
    try {
      await go(ctl.signal);
    } catch (e) {
      if (!ctl.signal.aborted) throw e;
    } finally {
      if (!ctl.signal.aborted) setBusy(false);
    }
  }

  const dropEmptyReply = () => setMessages((m) => (m[m.length - 1]?.role === "assistant" && !m[m.length - 1].content ? m.slice(0, -1) : m));

  function failRun(e: unknown) {
    fail(e);
    dropEmptyReply();
    props.onMood("error", "");
  }

  /** Show what the server has for this chat, and keep watching if Riki is still working on it. */
  async function restore(id: string, stale: () => boolean = () => false) {
    // first, so a reply saved in between is never missed. Without it (an older server) the saved chat still loads.
    const live = await api<Live>("/live").catch((): Live => ({ active: [], asks: [] }));
    const saved = await api<ChatMsg[]>(`/threads/${id}/messages`);
    if (stale()) return;
    setMessages(saved);
    const run = live.active.find((a) => a.thread_id === id);
    if (run && !saved.some((m) => m.run_id === run.run_id && m.role === "assistant")) {
      if (props.initialMessage && !sentInitial.current) {
        sentInitial.current = true; // not lost: it goes back to the draft
        setDraft(props.initialMessage);
        props.onInitialSent?.();
      }
      setLive({ spans: [], approvals: [], runId: run.run_id });
      setMessages((m) => [...m, { id: crypto.randomUUID(), role: "assistant", content: "" }]);
      follow((signal) => followRun(run.run_id, 0, onEvent, signal)).catch((e) => (e instanceof Gone ? restore(id) : failRun(e)));
    } else if (props.initialMessage && !sentInitial.current) {
      sentInitial.current = true; // StrictMode runs effects twice; send once
      props.onInitialSent?.();
      send(props.initialMessage);
    }
  }

  async function attach(files: File[]) {
    const images = files.filter((f) => f.type.startsWith("image/"));
    if (!images.length) return;
    if (pics.length + images.length > 4) toast("You can send up to 4 pictures at a time.");
    try {
      const added = await Promise.all(images.slice(0, 4 - pics.length).map(shrink));
      setPics((p) => [...p, ...added]);
    } catch {
      toast("That picture couldn't be read.");
    }
  }

  async function send(text: string) {
    text = text.trim();
    const images = pics;
    if (busy && (text || images.length)) toast("Riki is still working on your last message. You can stop it with the square button.");
    if ((!text && !images.length) || busy) return;
    setDraft("");
    setPics([]);
    setError("");
    setLive({ spans: [], approvals: [], runId: null });
    setMessages((m) => [
      ...m,
      { id: crypto.randomUUID(), role: "user", content: text, images },
      { id: crypto.randomUUID(), role: "assistant", content: "" },
    ]);
    try {
      await follow((signal) => streamChat(text, props.threadId, onEvent, signal, images, effort));
    } catch (e) {
      if (e instanceof Busy && props.threadId) {
        // an earlier message is still being worked on: keep your text, show that run instead
        setMessages((m) => m.slice(0, -2));
        setDraft(text);
        setPics(images);
        setError(e.message);
        restore(props.threadId).catch(fail);
      } else failRun(e);
    }
  }

  const stop = () => live.runId && post(`/runs/${live.runId}/stop`, {}).catch(fail);

  async function openDetail(runId: string, focus: string | null = null) {
    try {
      const d = await api<{ run: RunRow; spans: Span[]; approvals: ApprovalReq[] }>(`/runs/${runId}`);
      setDetail({ ...d, focus });
    } catch (e) {
      fail(e);
    }
  }

  async function removeMessage(m: ChatMsg) {
    if (!m.run_id || busy) return;
    if (!confirm(m.role === "user" ? "Delete this message?" : "Delete this reply? What Riki learned stays in Memory.")) return;
    try {
      await api(`/runs/${m.run_id}/messages/${m.role}`, { method: "DELETE" });
      setMessages((all) => all.filter((x) => x.id !== m.id));
      toast("Deleted");
    } catch (e) {
      fail(e);
    }
  }

  async function rate(m: ChatMsg, score: number) {
    if (!m.run_id) return;
    const next = m.feedback === score ? 0 : score;
    await post(`/runs/${m.run_id}/feedback`, { score: next }).catch(fail);
    setMessages((all) => all.map((x) => (x.id === m.id ? { ...x, feedback: next } : x)));
  }

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send(draft);
    }
  };

  function dictate() {
    if (!Speech) return;
    if (listening) return rec.current?.stop();
    const r = new Speech();
    r.lang = navigator.language;
    r.interimResults = true;
    const before = draft ? draft.trimEnd() + " " : "";
    r.onresult = (e) => setDraft(before + Array.from(e.results, (x) => x[0].transcript).join(""));
    r.onend = () => {
      setListening(false);
      input.current?.focus();
    };
    rec.current = r;
    setListening(true);
    r.start();
  }

  return (
    <div className={`chat ${detail ? "with-drawer" : ""}`}>
      <section className="conversation" aria-label="Conversation">
        <div
          className="messages"
          aria-live="polite"
          ref={list}
          onScroll={(e) => {
            const el = e.currentTarget;
            pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
          }}
        >
          <div className="column">
            <div className="chat-head">
              <button className="chat-switch" onClick={props.onSwitch} aria-haspopup="dialog">
                {props.title}
                <ChevronDown size={15} aria-hidden="true" />
              </button>
              {props.threadId && messages.length > 0 && (
                <button
                  className="icon small chat-del"
                  aria-label={props.isMain ? "Clear this chat" : "Delete this chat"}
                  title={busy ? "Stop the reply first" : props.isMain ? "Clear chat" : "Delete chat"}
                  disabled={busy}
                  onClick={props.onDelete}
                >
                  <Trash2 size={15} />
                </button>
              )}
            </div>
            {loading && <Skeleton lines={4} />}
            {!loading && messages.length === 0 && (
              <div className="chat-empty">
                <Avatar mood={draft.trim() ? "listening" : "idle"} src={me.avatar} name={me.agent_name} size={56} />
                <h1>{props.isMain ? `What's on your mind?` : "What's this chat about?"}</h1>
                <p>
                  {props.isMain
                    ? `Your ongoing conversation with ${me.agent_name}. Anything worth keeping is remembered.`
                    : "Your first message names it. Keeps one topic out of your main chat."}
                </p>
              </div>
            )}
            {messages.map((m, i) => {
              const isLive = busy && i === messages.length - 1;
              if (m.role === "user")
                return (
                  <div key={m.id} className="msg user">
                    {m.images?.length ? (
                      <div className="attachments">
                        {m.images.map((src, k) => (
                          <img key={k} src={src} alt="Attached" />
                        ))}
                      </div>
                    ) : null}
                    {m.content && <div className="bubble">{m.content}</div>}
                    {m.content && (
                      <div className="msg-actions">
                        <button className="icon small" aria-label="Copy message" title="Copy" onClick={() => navigator.clipboard.writeText(m.content).then(() => toast("Copied"))}>
                          <Copy size={14} />
                        </button>
                        {m.run_id && (
                          <button className="icon small" aria-label="Delete message" title={busy ? "Stop the reply first" : "Delete"} disabled={busy} onClick={() => removeMessage(m)}>
                            <Trash2 size={14} />
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                );
              return (
                <div key={m.id} className={`msg assistant ${isLive ? "live" : ""}`}>
                  {m.content ? (
                    <SmoothReply text={m.content} onCite={(ref) => m.run_id && openDetail(m.run_id, ref)} onGrow={keepInView} />
                  ) : (
                    isLive && <Typing />
                  )}
                  {m.run_id &&
                    artifacts
                      .filter((x) => x.run_id === m.run_id)
                      .map((x) => <ArtifactCard key={x.id} a={x} onOpen={() => setOpenArtifact(x.id)} />)}
                  {i === messages.length - 1 &&
                    connectAsks(isLive ? live.spans : m.steps).map(([app, reason]) => (
                      <ConnectCard key={app} appId={app} reason={reason} onOpenApps={props.onOpenApps} onShown={() => toBottom()} />
                    ))}
                  {m.error && (
                    <p className="notice error" role="alert">
                      What went wrong: {m.error}
                    </p>
                  )}
                  {isLive && (
                    <div className="msg-foot">
                      <Steps
                        spans={live.spans}
                        defaultOpen
                        onDetails={() => setDetail({ run: { status: "running" }, spans: [], approvals: [], focus: null, live: true })}
                      />
                    </div>
                  )}
                  {!isLive && m.run_id && (
                    <div className="msg-foot">
                      <Steps spans={m.steps || []} latency={m.latency_ms} onDetails={() => openDetail(m.run_id!)} />
                      <div className="msg-actions">
                        <button
                          className="icon small"
                          aria-label="Copy reply"
                          title="Copy"
                          onClick={() => navigator.clipboard.writeText(m.content).then(() => toast("Copied"))}
                        >
                          <Copy size={14} />
                        </button>
                        <button className={`icon small ${m.feedback === 1 ? "on" : ""}`} aria-label="Good reply" aria-pressed={m.feedback === 1} onClick={() => rate(m, 1)}>
                          <ThumbsUp size={14} />
                        </button>
                        <button className={`icon small ${m.feedback === -1 ? "on" : ""}`} aria-label="Bad reply" aria-pressed={m.feedback === -1} onClick={() => rate(m, -1)}>
                          <ThumbsDown size={14} />
                        </button>
                        <button className="icon small" aria-label="See how this reply was made" title="How this was made" onClick={() => openDetail(m.run_id!)}>
                          <PanelRight size={14} />
                        </button>
                        <button className="icon small" aria-label="Delete reply" title={busy ? "Stop the reply first" : "Delete"} disabled={busy} onClick={() => removeMessage(m)}>
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <div className="dock" ref={dock}>
          {error && (
            <p className="field-error" role="alert">
              {error}
            </p>
          )}
          {menu && (
            <div className="capabilities" role="menu" aria-label={`What ${me.agent_name} can do`}>
              {CAPABILITIES.map(([label, starter]) => (
                <button
                  key={label}
                  role="menuitem"
                  onClick={() => {
                    setDraft(starter);
                    setMenu(false);
                    input.current?.focus();
                  }}
                >
                  {label}
                </button>
              ))}
              <p className="capabilities-note">It never messages anyone for you. Anything that changes something asks first.</p>
            </div>
          )}
          {pics.length > 0 && (
            <div className="attachments pending">
              {pics.map((src, k) => (
                <span key={k} className="thumb">
                  <img src={src} alt="To send" />
                  <button type="button" aria-label="Remove picture" onClick={() => setPics((p) => p.filter((_, j) => j !== k))}>
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
          <form
            className={`composer ${busy ? "busy" : ""}`}
            onSubmit={(e: FormEvent) => {
              e.preventDefault();
              send(draft);
            }}
          >
            <button
              type="button"
              className={`icon composer-tool ${menu ? "on" : ""}`}
              aria-label={`What can ${me.agent_name} do?`}
              aria-expanded={menu}
              onClick={() => setMenu(!menu)}
            >
              <Sparkles size={18} />
            </button>
            <input
              ref={fileInput}
              type="file"
              accept="image/*"
              multiple
              hidden
              onChange={(e) => {
                attach([...(e.target.files ?? [])]);
                e.target.value = "";
              }}
            />
            <button type="button" className="icon composer-tool" aria-label="Attach a picture" onClick={() => fileInput.current?.click()}>
              <Paperclip size={18} />
            </button>
            <button
              type="button"
              className="link composer-tool effort"
              title="How hard to work on your next message: Quick answers alone, Deep can use helpers and more steps"
              aria-label={`Effort: ${EFFORT_NAMES[effort]}. Change`}
              onClick={() => {
                const next = EFFORTS[(EFFORTS.indexOf(effort) + 1) % EFFORTS.length];
                setEffort(next);
                try {
                  localStorage.setItem("riki.effort", next);
                } catch {}
              }}
            >
              <Gauge size={15} /> {EFFORT_NAMES[effort]}
            </button>
            <textarea
              ref={input}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onKey}
              onPaste={(e) => {
                const files = [...e.clipboardData.files];
                if (files.some((f) => f.type.startsWith("image/"))) {
                  e.preventDefault();
                  attach(files);
                }
              }}
              placeholder={busy ? `${me.agent_name} is on it…` : `Message ${me.agent_name}`}
              rows={1}
              aria-label="Message"
            />
            {Speech && (
              <button
                type="button"
                className={`icon composer-tool ${listening ? "recording" : ""}`}
                aria-label={listening ? "Stop dictation" : "Dictate"}
                aria-pressed={listening}
                onClick={dictate}
              >
                <Mic size={18} />
              </button>
            )}
            {busy && live.runId ? (
              <button type="button" className="send stop" onClick={stop} aria-label="Stop" title="Stop">
                <Square size={13} fill="currentColor" />
              </button>
            ) : (
              <button className="send" disabled={busy || (!draft.trim() && !pics.length)} aria-label="Send">
                <ArrowUp size={18} strokeWidth={2.5} />
              </button>
            )}
          </form>
        </div>
      </section>
      {detail && (
        <RunDrawer
          run={detail.run}
          spans={detail.live ? live.spans : detail.spans}
          approvals={detail.live ? live.approvals : detail.approvals}
          focus={detail.focus}
          onClose={() => setDetail(null)}
        />
      )}
      {openArtifact && <ArtifactView id={openArtifact} onClose={() => setOpenArtifact(null)} />}
    </div>
  );
}
