// People: who can sign in to this Riki. Owner only. Nothing is emailed; you tell them the address and they ask for a code.

import { Trash } from "@phosphor-icons/react";
import { FormEvent, useState } from "react";
import { api, del, post } from "./api";
import { useLoad } from "./Pages";
import { Skeleton, toast } from "./ui";

type Person = { id: string; email: string; name: string; role: "owner" | "member"; disabled: boolean };

export function People() {
  const [people, error, reload] = useLoad<Person[]>("/users");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);

  async function invite(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setProblem("");
    try {
      await post("/users", { email: email.trim(), name: name.trim() });
      setName("");
      setEmail("");
      toast("Added. Tell them to sign in with that email.");
      reload();
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "Couldn't add them.");
    }
    setBusy(false);
  }

  const toggle = (p: Person) => api(`/users/${p.id}`, { method: "PATCH", body: JSON.stringify({ disabled: !p.disabled }) }).then(reload);
  const remove = async (p: Person) => {
    if (!confirm(`Remove ${p.name}? Their chats, files and memory are deleted for good.`)) return;
    await del(`/users/${p.id}`);
    toast(`${p.name} was removed`);
    reload();
  };

  return (
    <>
      <p className="muted section-lead">
        Everyone who can sign in here. Each person gets private chats and memory that nobody else sees. They don't get your apps, files or
        settings. Nothing is emailed: tell them to open Riki and enter their email.
      </p>
      <form className="form invite" onSubmit={invite}>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={80} autoComplete="off" required />
        </label>
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" required />
        </label>
        <button className="primary" disabled={busy || !name.trim() || !email.includes("@")}>
          {busy ? "Adding…" : "Add person"}
        </button>
        {problem && <p className="field-error">{problem}</p>}
      </form>

      <h2>People</h2>
      {error && <p className="notice error">{error}</p>}
      {!people && !error && <Skeleton lines={3} />}
      {people && (
        <div className="settings-group">
          {people.map((p) => (
            <div className="setting" key={p.id}>
              <div className="grow">
                <strong>
                  {p.name} {p.role === "owner" && <span className="chip">You</span>} {p.disabled && <span className="chip tone-warn">Paused</span>}
                </strong>
                <div className="muted small">{p.email}</div>
              </div>
              {p.role !== "owner" && (
                <span className="setting-control">
                  <button className="link" onClick={() => toggle(p)}>
                    {p.disabled ? "Allow" : "Pause"}
                  </button>
                  <button className="icon small" aria-label={`Remove ${p.name}`} title="Remove" onClick={() => remove(p)}>
                    <Trash size={15} />
                  </button>
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </>
  );
}
