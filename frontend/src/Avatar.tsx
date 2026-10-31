// Your agent's face: an image or GIF you upload (or a neutral glass orb), with an aura that shows
// what it's doing. The aura is the app's one loud element, so it only appears when something happens.

export type Mood = "idle" | "listening" | "thinking" | "working" | "waiting" | "happy" | "error" | "sleeping";

export const MOODS: Mood[] = ["idle", "listening", "thinking", "working", "waiting", "happy", "error", "sleeping"];

export const MOOD_WORDS: Record<Mood, string> = {
  idle: "Idle",
  listening: "Listening",
  thinking: "Thinking",
  working: "Working",
  waiting: "Needs you",
  happy: "Done",
  error: "Problem",
  sleeping: "Offline",
};

export function Avatar({ mood, src, name, size = 36 }: { mood: Mood; src?: string | null; name: string; size?: number }) {
  return (
    <span
      className={`avatar mood-${mood}`}
      style={{ ["--size" as string]: `${size}px` }}
      role="img"
      aria-label={`${name}, ${MOOD_WORDS[mood].toLowerCase()}`}
    >
      <span className="avatar-aura" aria-hidden="true" />
      {src ? <img src={src} alt="" draggable={false} /> : <span className="avatar-orb" aria-hidden="true" />}
    </span>
  );
}
