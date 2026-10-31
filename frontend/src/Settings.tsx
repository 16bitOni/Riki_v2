// Settings: one place for everything you configure, plus what Riki can see and your data.

import { ChevronLeft, ChevronRight, Download, ShieldCheck, ShieldOff, ShieldQuestion } from "lucide-react";
import { useState } from "react";
import { Me, Status, shortModel } from "./api";
import { Mood } from "./Avatar";
import { CheckIns, Logs, Overview, PersonaSettings, Quality, Tools, useLoad } from "./Pages";

const TABS = ["Persona", "Check-ins", "Privacy & data", "Tools", "Diagnostics"] as const;
type Tab = (typeof TABS)[number];
const DIAG = ["Health", "Logs", "Quality"] as const;

export function SettingsPage(props: { me: Me; status: Status | null; mood: Mood; onSaved: () => void; tab?: Tab }) {
  const [tab, setTab] = useState<Tab>(props.tab ?? "Persona");
  const [diag, setDiag] = useState<(typeof DIAG)[number]>("Health");
  const [view, setView] = useState<"list" | "detail">(props.tab ? "detail" : "list"); // phones show one at a time
  return (
    <div className="settings" data-view={view}>
      <nav className="settings-nav" aria-label="Settings sections">
        {TABS.map((t) => (
          <button key={t} aria-current={t === tab ? "page" : undefined} className={t === tab ? "on" : ""} onClick={() => { setTab(t); setView("detail"); }}>
            <span className="grow">{t}</span>
            {t === "Diagnostics" && props.status && !props.status.ok && <i className="dot error" aria-label="needs attention" />}
            <ChevronRight size={15} className="chev nav-chev" aria-hidden="true" />
          </button>
        ))}
      </nav>
      <div className="settings-body" key={tab}>
        <button className="settings-back" onClick={() => setView("list")}>
          <ChevronLeft size={18} aria-hidden="true" /> Settings
        </button>
        <h3 className="settings-title">{tab}</h3>
        {tab === "Persona" && <PersonaSettings me={props.me} onSaved={props.onSaved} />}
        {tab === "Check-ins" && <CheckIns me={props.me} onSaved={props.onSaved} />}
        {tab === "Privacy & data" && <Privacy me={props.me} status={props.status} />}
        {tab === "Tools" && <Tools />}
        {tab === "Diagnostics" && (
          <>
            <div className="segmented left" role="group" aria-label="Diagnostics view">
              {DIAG.map((d) => (
                <button key={d} className={d === diag ? "on" : ""} aria-pressed={d === diag} onClick={() => setDiag(d)}>
                  {d}
                </button>
              ))}
            </div>
            {diag === "Health" && <Overview status={props.status} me={props.me} mood={props.mood} />}
            {diag === "Logs" && <Logs />}
            {diag === "Quality" && <Quality />}
          </>
        )}
      </div>
    </div>
  );
}

type Tool = { name: string; risk: string; source: string; description: string };

const GROUPS = [
  { risk: "safe", icon: ShieldCheck, title: "Does on its own", note: "Reading and looking things up. Nothing outside the chat changes." },
  { risk: "write", icon: ShieldQuestion, title: "Asks you first", note: "Anything that changes something. Shows up in Approvals; never during check-ins." },
  { risk: "external", icon: ShieldOff, title: "Never does", note: "Anything that reaches another person, like sending a message or email. Always refused." },
] as const;

const sourceName = (source: string) => (source === "builtin" ? "Built in" : source.replace(/^(mcp|app):/, ""));

function Privacy({ me, status }: { me: Me; status: Status | null }) {
  const [tools] = useLoad<Tool[]>("/tools");
  return (
    <>
      <p className="muted section-lead">What {me.agent_name} can see and do right now, where your data lives, and how to take it with you.</p>

      <h2>What {me.agent_name} can do</h2>
      <div className="scopes">
        {GROUPS.map(({ risk, icon: Icon, title, note }) => {
          const list = tools?.filter((t) => t.risk === risk) ?? [];
          const bySource = new Map<string, number>();
          for (const t of list) bySource.set(sourceName(t.source), (bySource.get(sourceName(t.source)) ?? 0) + 1);
          return (
            <section key={risk} className={`scope risk-${risk}`}>
              <h3>
                <Icon size={16} aria-hidden="true" /> {title}
              </h3>
              <p className="muted small">{note}</p>
              <p className="small">
                {list.length === 0
                  ? "Nothing in this group."
                  : [...bySource.entries()].map(([src, n]) => `${src}: ${n} tool${n > 1 ? "s" : ""}`).join(", ")}
              </p>
            </section>
          );
        })}
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
        <li>
          <span className="grow">Quiet hours</span>
          <span className="muted">
            No check-ins from {me.quiet_hours.split("-")[0]}:00 to {me.quiet_hours.split("-")[1]}:00 ({me.timezone})
          </span>
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
