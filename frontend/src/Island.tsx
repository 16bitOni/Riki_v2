// The Island: a black pill under the tab bar that appears only when something is happening.
// Working: what Riki is doing. Needs you: the decision itself, right there. Done: a brief check.

import { Check, WarningCircle as CircleAlert } from "@phosphor-icons/react";
import { ApprovalCard } from "./Activity";
import { ApprovalReq, Me } from "./api";
import { Avatar, Mood } from "./Avatar";

export function Island(props: {
  me: Me;
  mood: Mood;
  line: string;
  asks: ApprovalReq[]; // everything waiting for your OK, from any chat or check-in (the server's list)
  onDecided: () => void;
  onReview: () => void;
}) {
  const { me, mood } = props;
  const ask = props.asks[0];
  const state = ask ? "ask" : mood === "thinking" || mood === "working" ? "busy" : mood === "happy" ? "done" : mood === "error" ? "error" : "rest";
  const more = props.asks.length - 1;
  return (
    // A slot in the page flow, so it never covers the chat switcher or anything else; it grows open and shut.
    <div className={`island-slot ${state === "rest" ? "" : "open"}`}>
      <div className="island-clip">
        <div className={`island ${state}`} role="status" aria-live="polite" aria-hidden={state === "rest"}>
          {state === "ask" && (
            <>
              <ApprovalCard key={ask.id} approval={ask} agentName={me.agent_name} compact onDecided={props.onDecided} />
              {ask.request && <p className="island-from">For “{ask.request.slice(0, 70)}”</p>}
              {more > 0 && (
                <div className="island-row more">
                  <span className="grow">{more} more waiting</span>
                  <button className="pill-button" onClick={props.onReview}>
                    Review all
                  </button>
                </div>
              )}
            </>
          )}
      {state === "busy" && (
        <div className="island-row">
          <Avatar mood={mood} src={me.avatar} name={me.agent_name} size={22} />
          <span className="island-line">{props.line || "Thinking"}</span>
        </div>
      )}
      {state === "done" && (
        <div className="island-row">
          <Check size={16} className="tone-ok" aria-hidden="true" />
          <span>Done</span>
        </div>
      )}
      {state === "error" && (
        <div className="island-row">
          <CircleAlert size={16} className="tone-error" aria-hidden="true" />
          <span>Something went wrong. The reply explains what.</span>
        </div>
      )}
        </div>
      </div>
    </div>
  );
}
