// Setup: what you use Riki for, the apps that matter for it, and what Riki can learn from them. You pick what it keeps.

import { ShieldCheck } from "lucide-react";
import { FormEvent, useState } from "react";
import { App, AppSheet, Tile } from "./Apps";
import { Me, followRun, post } from "./api";
import { Avatar, Mood } from "./Avatar";
import { useLoad } from "./Pages";
import { toast } from "./ui";

type Role = { id: string; label: string; blurb: string; apps: string[] };
type Fact = { text: string; keep: boolean };

/** "- fact [t2]" lines of the scan's reply. The grounding check's "Not verified" note is a quote, not a bullet. */
export const factsFrom = (reply: string, unsupported: string[] = []): Fact[] =>
  reply
    .split("\n")
    .filter((l) => /^\s*[-*•]\s+/.test(l))
    .map((l) => l.replace(/^\s*[-*•]\s+/, "").replace(/\s*\[(?:t\d+|m:[0-9a-f]{8})\]/g, "").trim())
    .filter((t) => t.length >= 3)
    .slice(0, 30)
    .map((text) => ({ text, keep: !unsupported.some((u) => text.includes(u) || u.includes(text)) }));

export function Onboarding({ me, onDone }: { me: Me; onDone: () => void }) {
  const [setup] = useLoad<{ roles: Role[] }>("/onboarding");
  const [step, setStep] = useState<"you" | "apps" | "learn">("you");
  const [role, setRole] = useState(me.role);
  const [about, setAbout] = useState("");
  const [busy, setBusy] = useState(false);
  const [apps, , reloadApps] = useLoad<{ apps: App[] }>("/apps", [step], step === "you" ? 0 : 2500); // sign-ins finish in a popup
  const [open, setOpen] = useState<string | null>(null);
  const [reading, setReading] = useState(""); // what the scan is doing right now
  const [facts, setFacts] = useState<Fact[] | null>(null);
  const [runId, setRunId] = useState<string | null>(null);

  const picked = setup?.roles.find((r) => r.id === role);
  const all = apps?.apps ?? [];
  const suggested = (picked?.apps ?? []).map((id) => all.find((a) => a.id === id)).filter((a): a is App => !!a);
  const ready = all.filter((a) => a.connected && a.state === "connected");
  const mood: Mood = busy ? "working" : facts ? "happy" : "idle";

  async function finish() {
    await post("/onboarding", { done: true });
    onDone();
  }

  async function saveYou(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await post("/onboarding", { role: role || undefined, about });
      setStep("apps");
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setBusy(false);
    }
  }

  async function scan() {
    setBusy(true);
    setReading("Starting");
    try {
      const { run_id } = await post<{ run_id: string }>("/onboarding/scan", {});
      setRunId(run_id);
      await followRun(run_id, 0, (e) => {
        if (e.type === "span_start" && e.span.kind === "tool" && e.span.name !== "load_tools")
          setReading(e.span.name === "spawn_agent" ? "Reading your apps" : `Reading: ${e.span.name.replace(/_/g, " ")}`);
        if (e.type === "run_end") setFacts(e.status === "ok" ? factsFrom(e.output, e.checks.unsupported) : []);
      });
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setBusy(false);
      setReading("");
    }
  }

  async function keep() {
    const chosen = (facts ?? []).filter((f) => f.keep).map((f) => f.text);
    setBusy(true);
    try {
      if (chosen.length) {
        const { saved } = await post<{ saved: number }>("/onboarding/facts", { facts: chosen, run_id: runId });
        toast(`${me.agent_name} will remember ${saved} thing${saved === 1 ? "" : "s"} about you`);
      }
      await finish();
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "error");
      setBusy(false);
    }
  }

  return (
    <main className="onboard">
      <div className="onboard-inner">
        <Avatar mood={mood} src={me.avatar} name={me.agent_name} size={64} />
        <ol className="onboard-steps" aria-label="Setup steps">
          {(["you", "apps", "learn"] as const).map((s, i) => (
            <li key={s} className={s === step ? "on" : ""} aria-current={s === step ? "step" : undefined}>
              {i + 1}
            </li>
          ))}
        </ol>

        {step === "you" && (
          <form className="stack" onSubmit={saveYou}>
            <h1>Hi {me.name}. What should I help with most?</h1>
            <p className="muted">Pick the closest one. It decides which apps I suggest and what I check on. You can change everything later.</p>
            <div className="role-grid" role="radiogroup" aria-label="What you use Riki for">
              {setup?.roles.map((r) => (
                <button type="button" key={r.id} role="radio" aria-checked={role === r.id} className={`role ${role === r.id ? "on" : ""}`} onClick={() => setRole(r.id)}>
                  <strong>{r.label}</strong>
                  <span className="muted small">{r.blurb}</span>
                </button>
              ))}
            </div>
            <label>
              Tell me about yourself in a few lines
              <textarea
                rows={4}
                maxLength={2000}
                value={about}
                onChange={(e) => setAbout(e.target.value)}
                placeholder="What you do, what you're working on, how you like answers. Never a password."
              />
            </label>
            <button className="primary wide" disabled={busy || (!role && !about.trim())}>
              Continue
            </button>
          </form>
        )}

        {step === "apps" && (
          <div className="stack">
            <h1>Connect what you use</h1>
            <p className="muted">
              {me.agent_name} reads on its own, asks before changing anything, and never sends, shares or invites for you.
            </p>
            <div className="app-grid onboard-apps">
              {(suggested.length ? suggested : all.filter((a) => a.popular)).map((a) => (
                <Tile key={a.id} app={a} onOpen={() => setOpen(a.id)} />
              ))}
            </div>
            <button className="primary wide" onClick={() => setStep("learn")}>
              {ready.length ? "Continue" : "Skip for now"}
            </button>
          </div>
        )}

        {step === "learn" && !facts && (
          <div className="stack">
            <h1>Let me get to know you</h1>
            {ready.length ? (
              <>
                <p className="muted">
                  I'll skim the last 30 days of {new Intl.ListFormat("en", { type: "conjunction" }).format(ready.map((a) => a.name))} and suggest what to remember about you.
                </p>
                <p className="onboard-promise">
                  <ShieldCheck size={16} aria-hidden="true" /> Read only. I can't change or send anything during this, and nothing is
                  saved until you tick it.
                </p>
                <button className="primary wide" disabled={busy} onClick={scan}>
                  {busy ? `${reading}…` : "Get to know me"}
                </button>
              </>
            ) : (
              <p className="muted">Nothing is connected yet, so there's nothing to read. I'll learn as we talk instead.</p>
            )}
          </div>
        )}

        {step === "learn" && facts && (
          <div className="stack">
            <h1>{facts.length ? "Here's what I picked up" : "I didn't find much"}</h1>
            {facts.length > 0 ? (
              <>
                <p className="muted">Untick anything wrong or that you'd rather I didn't keep.</p>
                <ul className="facts">
                  {facts.map((f, i) => (
                    <li key={i}>
                      <label>
                        <input type="checkbox" checked={f.keep} onChange={() => setFacts(facts.map((g, j) => (j === i ? { ...g, keep: !g.keep } : g)))} />
                        <span>{f.text}</span>
                      </label>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="muted">That's fine. I'll learn as we talk.</p>
            )}
            <button className="primary wide" disabled={busy} onClick={keep}>
              {facts.some((f) => f.keep) ? `Remember ${facts.filter((f) => f.keep).length}` : "Finish"}
            </button>
          </div>
        )}

        {!busy && (
          <button className="link onboard-skip" onClick={finish}>
            {step === "learn" ? "Finish without this" : "Skip setup"}
          </button>
        )}
      </div>

      {open && all.find((a) => a.id === open) && (
        <AppSheet app={all.find((a) => a.id === open)!} me={me} onClose={() => setOpen(null)} onChanged={reloadApps} onActivity={() => {}} />
      )}
    </main>
  );
}
