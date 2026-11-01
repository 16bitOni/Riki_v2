// Thin client for the Riki API. Auth is an httpOnly cookie, so there is no token handling here.

export type Span = {
  id: string;
  parent_id: string | null;
  kind: "agent" | "llm" | "tool" | "memory";
  name: string;
  ref?: string | null;
  status?: "ok" | "error" | "denied" | "running";
  input?: unknown;
  output?: unknown;
  error?: string | null;
  tokens_in?: number;
  tokens_out?: number;
  tokens_cached?: number;
  meta?: Record<string, unknown>;
  started_at: string;
  ended_at?: string;
};

export type ApprovalReq = {
  id: string;
  span_id: string;
  tool: string;
  args: unknown;
  status?: string;
  request?: string;
  risk?: string;
  created_at?: string;
  decided_at?: string | null;
  thread_id?: string | null;
  money?: boolean; // moves money: always asks, never "allow for 30 minutes"
};

export type Msg = {
  id: string;
  role: "user" | "assistant";
  content: string;
  images?: string[]; // pictures the user attached (data: URLs)
  run_id?: string | null;
  feedback?: number | null;
  error?: string | null; // why the run failed, if it did
  checks?: { unsupported?: string[]; invalid_citations?: string[] }; // fact check of the reply
  cost_usd?: number | null;
  created_at?: string;
};

export type Thread = {
  id: string;
  title: string;
  kind: "main" | "side" | "heartbeat";
  created_at: string;
  last_at: string;
  preview: string; // last message, trimmed
  preview_role: "user" | "assistant" | null;
};

export type Today = {
  needs_you: { id: string; tool: string; request: string }[];
  continue: Thread[];
  nudge: { run_id: string; at: string; text: string | null } | null;
  learned: { count: number; items: { id: string; content: string; created_at: string }[] };
  today: { messages: number; actions: number };
  suggest: string[]; // starter requests for what you said you use Riki for
};

export type RunRow = {
  id: string;
  status: string;
  input: string;
  output: string | null;
  error: string | null;
  latency_ms: number | null;
  tokens_in: number;
  tokens_out: number;
  tokens_cached: number;
  cost_usd: number | null;
  prompt_version: string;
  model: string;
  checks: { citations?: number; invalid_citations?: string[]; unsupported?: string[] };
  created_at: string;
};

export type Artifact = {
  id: string;
  thread_id: string | null;
  run_id: string | null; // the run that last wrote it: its card shows under that reply
  title: string;
  kind: "md" | "html";
  updated_at: string;
  content?: string; // only when fetched one at a time
};

export type Me = {
  name: string;
  agent_name: string;
  hue: number;
  avatar: string | null; // URL of your uploaded image, or null for the default orb
  model: string;
  smart_model: string;
  prompt_version: string;
  quiet_hours: string; // "22-8": local hours with no check-ins
  timezone: string;
  default_effort: Effort;
  role: string; // what you use Riki for (apps.json roles), "" until setup
  onboarded: boolean; // setup finished or skipped
};

export type Effort = "low" | "medium" | "high";

export type Persona = {
  name: string;
  soul: string;
  hue: number;
  heartbeat_minutes: number;
  heartbeat_prompt: string;
  updated_at?: string;
};

export type Status = {
  ok: boolean;
  uptime_s: number;
  db: { ok: boolean; latency_ms: number };
  models: { main: string; smart: string; embedding: string };
  prompt_version: string;
  mcp: Record<string, { ok: boolean; tools: number; error: string | null }>;
  tools: { total: number; by_risk: Record<string, number> };
  memory_writes: Record<string, number>;
  last_24h: { runs?: number; errors?: number; p50_ms?: number | null; p95_ms?: number | null; tokens?: number; cache_ratio?: number };
  active: { run_id: string; thread_id: string; source: string; input: string; started_at: string }[];
  pending_approvals: number;
  recent_errors: { run_id: string; at: string; error: string | null; input: string }[];
  warnings: number;
  heartbeat: { every_min: number; last: { run_id: string; at: string; status: string } | null };
};

export type RunEnd = {
  type: "run_end";
  run_id: string;
  status: string;
  output: string;
  error: string | null;
  latency_ms: number;
  tokens_in: number;
  tokens_out: number;
  tokens_cached: number;
  cost_usd: number | null;
  checks: { citations: number; invalid_citations: string[]; unsupported?: string[] };
};

export type RunEvent =
  | { type: "thread"; thread_id: string; run_id: string }
  | { type: "run_start"; run_id: string }
  | { type: "token"; text: string }
  | { type: "span_start"; span: Span }
  | { type: "span_end"; span: Span }
  | { type: "approval"; approval: ApprovalReq }
  | { type: "approval_done"; approval: { id: string; status: string } }
  | RunEnd;

export class Unauthorized extends Error {}

export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers || {}) },
    credentials: "same-origin",
  });
  if (res.status === 401) throw new Unauthorized();
  if (!res.ok) {
    let detail = await res.text();
    try {
      detail = JSON.parse(detail).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw Object.assign(new Error(typeof detail === "string" ? detail.slice(0, 300) : `Request failed (${res.status})`), { status: res.status });
  }
  return res.json() as Promise<T>;
}

export const dollars = (n?: number | null) => (n == null ? "" : n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`);

/** A file to talk about (PDF, Word, Excel, CSV, text): sent as is, read by Riki with read_file. */
export async function upload(file: File): Promise<{ id: string; name: string; chars: number; lines: number }> {
  const res = await fetch(`/api/uploads?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file, credentials: "same-origin" });
  if (res.status === 401) throw new Unauthorized();
  if (!res.ok) {
    let detail = await res.text();
    try {
      detail = JSON.parse(detail).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(typeof detail === "string" ? detail.slice(0, 200) : `Upload failed (${res.status})`);
  }
  return res.json();
}

export const post = <T = unknown>(path: string, body: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });

export const put = <T = unknown>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });

/** What the server is doing right now: runs in progress and approvals waiting, on any page, in any chat. */
export type Live = { active: Status["active"]; asks: ApprovalReq[] };

/** This chat already has a run in progress (maybe started on another tab or before you left). */
export class Busy extends Error {
  constructor(public runId: string) {
    super("Riki is still working on your last message in this chat.");
  }
}
/** The run is no longer kept by the server (it finished a while ago); its reply is in the chat. */
export class Gone extends Error {}

async function open(path: string, init: RequestInit): Promise<ReadableStream<Uint8Array>> {
  const res = await fetch(`/api${path}`, { ...init, headers: { "Content-Type": "application/json" }, credentials: "same-origin" });
  if (res.status === 401) throw new Unauthorized();
  if (res.status === 404) throw new Gone();
  if (res.status === 409) throw new Busy(((await res.json().catch(() => null))?.detail?.run_id as string) ?? "");
  if (!res.ok || !res.body) throw new Error(`Couldn't reach Riki (${res.status}).`);
  return res.body;
}

async function read(body: ReadableStream<Uint8Array>, onEvent: (e: RunEvent) => void) {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
      }
    }
  }
}

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => (clearTimeout(t), resolve()), { once: true });
  });

/**
 * Watch a run from event number `from` until it ends. The server keeps every event of a run, so this is how a
 * chat that was left, refreshed or cut off by bad wifi picks up exactly where it was.
 */
export async function followRun(runId: string, from: number, onEvent: (e: RunEvent) => void, signal?: AbortSignal) {
  let seen = from;
  let ended = false;
  const handle = (e: RunEvent) => {
    seen++;
    ended = e.type === "run_end";
    onEvent(e);
  };
  for (let tries = 0; !ended && !signal?.aborted; tries++) {
    try {
      await read(await open(`/runs/${runId}/stream?from=${seen}`, { signal }), handle);
    } catch (e) {
      if (signal?.aborted) return;
      if (e instanceof Unauthorized || e instanceof Gone || tries >= 5) throw e;
    }
    if (!ended) {
      if (tries >= 5) throw new Error("Lost the connection to Riki. The reply will be in the chat when it finishes.");
      await sleep(Math.min(4000, 400 * 2 ** tries), signal);
    }
  }
}

/** Send a message and call onEvent for every event until the run ends. Throws Busy if the chat is mid-run. */
export async function streamChat(message: string, threadId: string | null, onEvent: (e: RunEvent) => void, signal?: AbortSignal, images: string[] = [], effort?: Effort) {
  const body = await open("/chat", { method: "POST", body: JSON.stringify({ message, thread_id: threadId, images, effort }), signal });
  let runId = "";
  let seen = 0;
  let ended = false;
  try {
    await read(body, (e) => {
      if (e.type === "thread") runId = e.run_id; // not part of the run's own event log
      else seen++;
      ended = e.type === "run_end";
      onEvent(e);
    });
  } catch (e) {
    if (signal?.aborted || e instanceof Unauthorized || !runId) throw e;
  }
  if (!ended && runId && !signal?.aborted) await followRun(runId, seen, onEvent, signal); // cut off mid-run
}

/** "bedrock_converse:global.anthropic.claude-haiku-4-5-20251001-v1:0" -> "claude-haiku-4-5-20251001-v1:0" */
export const shortModel = (spec: string) => spec.replace(/^[a-z_]+:/, "").replace(/^([a-z]{2,6}\.)?[a-z]+\./, "");

export const ago = (iso: string | number) => {
  const s = Math.max(0, (Date.now() - (typeof iso === "number" ? iso * 1000 : new Date(iso).getTime())) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(typeof iso === "number" ? iso * 1000 : iso).toLocaleDateString();
};

export const seconds = (ms?: number | null) => (ms == null ? "" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
