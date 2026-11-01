// Settings: everything you can control, one line of status per section, and every change takes effect at once.

import { Activity, Brain, CalendarClock, ChevronLeft, ChevronRight, Send, ShieldCheck, UserRound, Wrench } from "lucide-react";
import { useState } from "react";
import { Me, SettingsView, Status, TelegramStatus, dollars } from "./api";
import { Mood } from "./Avatar";
import { CheckIns, Logs, Overview, PersonaSettings, Quality, Tools, useLoad } from "./Pages";
import { About, Channels, Permissions, Thinking } from "./SettingsSections";
import { Skeleton } from "./ui";

export const TABS = ["Persona", "Check-ins", "Channels", "Thinking & spend", "Permissions & data", "Tools", "Diagnostics"] as const;
export type Tab = (typeof TABS)[number];
const ICONS = { Persona: UserRound, "Check-ins": CalendarClock, Channels: Send, "Thinking & spend": Brain, "Permissions & data": ShieldCheck, Tools: Wrench, Diagnostics: Activity } as const;
const DIAG = ["Health", "Logs", "Quality"] as const;

export function SettingsPage(props: { me: Me; status: Status | null; mood: Mood; onSaved: () => void; tab?: Tab; onOpenTelegram: () => void }) {
  const [tab, setTab] = useState<Tab>(props.tab ?? "Persona");
  const [diag, setDiag] = useState<(typeof DIAG)[number]>("Health");
  const [view, setView] = useState<"list" | "detail">(props.tab ? "detail" : "list"); // phones show one at a time
  const [settings, , reloadSettings] = useLoad<SettingsView>("/settings");
  const [tg, , reloadTg] = useLoad<TelegramStatus>("/telegram", [], 2500);
  const [spend] = useLoad<{ spent_usd: number }>("/spend", [], 30000);
  const { me, status } = props;
  const changed = () => (reloadSettings(), props.onSaved());

  const summary: Record<Tab, string> = {
    Persona: me.agent_name,
    "Check-ins": status ? (status.heartbeat.every_min ? `Every ${status.heartbeat.every_min} min` : "Off") : "",
    Channels: !tg ? "" : tg.paired ? "Telegram connected" : tg.connected ? "Finish pairing" : "Not connected",
    "Thinking & spend": settings ? `${dollars(spend?.spent_usd ?? 0) || "$0"} today${settings.values.cognition ? "" : " · thinking off"}` : "",
    "Permissions & data": status ? `${status.tools.by_risk.write ?? 0} ask first` : "",
    Tools: status ? `${status.tools.total} tools` : "",
    Diagnostics: status ? (status.ok ? "All good" : "Needs attention") : "",
  };
  const attention = (t: Tab) => (t === "Diagnostics" && status && !status.ok) || (t === "Channels" && tg?.connected && !tg.paired);

  return (
    <div className="settings" data-view={view}>
      <nav className="settings-nav" aria-label="Settings sections">
        {TABS.map((t) => {
          const Icon = ICONS[t];
          return (
            <button key={t} aria-current={t === tab ? "page" : undefined} className={t === tab ? "on" : ""} onClick={() => { setTab(t); setView("detail"); }}>
              <Icon size={17} strokeWidth={1.8} aria-hidden="true" className="nav-icon" />
              <span className="nav-text">
                <span>{t}</span>
                {summary[t] && <small>{summary[t]}</small>}
              </span>
              {attention(t) && <i className="dot warn" aria-label="needs attention" />}
              <ChevronRight size={15} className="chev nav-chev" aria-hidden="true" />
            </button>
          );
        })}
      </nav>
      <div className="settings-body" key={tab}>
        <button className="settings-back" onClick={() => setView("list")}>
          <ChevronLeft size={18} aria-hidden="true" /> Settings
        </button>
        <h3 className="settings-title">{tab}</h3>
        {tab === "Persona" && (
          <>
            {settings ? <About view={settings} reload={changed} /> : <Skeleton lines={2} />}
            <PersonaSettings me={me} onSaved={props.onSaved} />
          </>
        )}
        {tab === "Check-ins" && <CheckIns me={me} onSaved={props.onSaved} />}
        {tab === "Channels" && <Channels me={me} tg={tg} reload={() => (reloadTg(), props.onSaved())} onOpenChat={props.onOpenTelegram} />}
        {tab === "Thinking & spend" && (settings ? <Thinking me={me} view={settings} reload={changed} /> : <Skeleton lines={6} />)}
        {tab === "Permissions & data" && (settings ? <Permissions me={me} status={status} view={settings} reload={changed} /> : <Skeleton lines={6} />)}
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
            {diag === "Health" && <Overview status={status} me={me} mood={props.mood} />}
            {diag === "Logs" && <Logs />}
            {diag === "Quality" && <Quality />}
          </>
        )}
      </div>
    </div>
  );
}
