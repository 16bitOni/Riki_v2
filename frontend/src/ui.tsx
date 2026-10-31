// Small shared pieces: toasts and loading skeletons.

import { X } from "lucide-react";
import { ReactNode, RefObject, useEffect, useRef, useState } from "react";

/** A textarea that grows with its text (wrapped lines too) up to its CSS max-height, then scrolls. */
export function useAutoGrow(ref: RefObject<HTMLTextAreaElement | null>, value: string) {
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [ref, value]);
}

/** Text that arrives in bursts (a streaming reply), revealed at a steady pace: a big backlog catches up fast, a small
 *  one trickles. What is already on screen never re-types; text that was there when it mounted shows at once. */
export function useSmoothText(text: string): string {
  const shown = useRef(text);
  const [, redraw] = useState(0);
  useEffect(() => {
    let n = 0;
    while (n < shown.current.length && shown.current[n] === text[n]) n++;
    shown.current = text.slice(0, n);
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) shown.current = text;
    let frame = 0;
    const step = () => {
      const left = text.length - shown.current.length;
      if (left > 0) {
        shown.current = text.slice(0, shown.current.length + Math.max(2, Math.ceil(left / 10)));
        frame = requestAnimationFrame(step);
      }
      redraw((x) => x + 1);
    };
    step();
    return () => cancelAnimationFrame(frame);
  }, [text]);
  return shown.current;
}

export function toast(message: string, tone: "ok" | "error" = "ok") {
  window.dispatchEvent(new CustomEvent("riki-toast", { detail: { message, tone } }));
}

export function Toaster() {
  const [items, setItems] = useState<{ id: number; message: string; tone: string }[]>([]);
  useEffect(() => {
    const on = (e: Event) => {
      const { message, tone } = (e as CustomEvent).detail;
      const id = Date.now() + Math.random();
      setItems((all) => [...all.slice(-2), { id, message, tone }]);
      setTimeout(() => setItems((all) => all.filter((t) => t.id !== id)), 2800);
    };
    window.addEventListener("riki-toast", on);
    return () => window.removeEventListener("riki-toast", on);
  }, []);
  return (
    <div className="toasts" role="status" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={`toast ${t.tone}`}>
          {t.message}
        </div>
      ))}
    </div>
  );
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="skeleton" aria-busy="true" aria-label="Loading">
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} style={{ width: `${90 - ((i * 23) % 45)}%` }} />
      ))}
    </div>
  );
}

export function Typing() {
  return (
    <span className="typing" aria-label="Writing">
      <i />
      <i />
      <i />
    </span>
  );
}

export const greeting = () => {
  const h = new Date().getHours();
  return h < 5 ? "Still up" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
};

/** A modal panel: centered on desktop, a bottom sheet on phones. Esc or the backdrop closes it. */
export function Sheet({ title, onClose, children, wide }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="sheet-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <section className={`sheet ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <header className="sheet-head">
          <h2>{title}</h2>
          <button className="icon" onClick={onClose} aria-label="Close" autoFocus>
            <X size={18} />
          </button>
        </header>
        <div className="sheet-body">{children}</div>
      </section>
    </div>
  );
}
