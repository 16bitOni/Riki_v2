"""Voice A/B: does Riki v2 sound like Riki v1? Replays real messages and builds a blind comparison sheet.

Standard library only, so it runs anywhere with Python 3.10+ and talks to a running Riki over HTTP.

    # 1. pick ~20 real messages (and v1's actual replies) from the v1 archive
    python -m evals.voice_ab collect --archive "C:\\Users\\subha\\Documents\\SSS-Fire\\Persoanal agent\\personal-agent\\data\\archive" --n 20

    # 2. replay them against v2, once per config (start Riki with the config first)
    python -m evals.voice_ab run --label A --password "..."                              # v2 as it is
    python -m evals.voice_ab run --label B --password "..." --persona evals/persona_v1.md  # + v1's voice
    #    for C: set RIKI_MODEL (and RIKI_TEMPERATURE) in backend/.env, restart, then
    python -m evals.voice_ab run --label C --password "..." --persona evals/persona_v1.md

    # 3. build the blind sheet; read it, pick a favourite per message, then check evals/results/voice_key.json
    python -m evals.voice_ab sheet --labels A B C

Every replay is a fresh side chat sent with learn=false, so nothing is saved to memory. --persona replaces the
persona text for the run and puts the old one back afterwards. Messages that look like they hold a secret are skipped.
"""

import argparse
import http.cookiejar
import json
import random
import re
import sys
import urllib.request
from pathlib import Path

RESULTS = Path(__file__).with_name("results")
INPUTS = RESULTS / "voice_inputs.json"
SECRETISH = re.compile(r"\d{9,}|sk-|AKIA|password|passwd|otp|cvv", re.I)


def collect(archive: Path, n: int) -> None:
    if not (archive / "users").is_dir():
        sys.exit(f"No 'users' folder in {archive.resolve()}. Point --archive at v1's data\\archive folder, e.g. "
                 "\"C:\\Users\\subha\\Documents\\SSS-Fire\\Persoanal agent\\personal-agent\\data\\archive\".")
    turns = []
    for f in (archive / "users").rglob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        user, reply = d.get("user"), d.get("assistant")
        if isinstance(user, str) and isinstance(reply, str) and 3 <= len(user.strip()) <= 400 \
                and not SECRETISH.search(user) and not SECRETISH.search(reply):
            turns.append({"at": d.get("at", ""), "input": user.strip(), "v1_reply": reply.strip()})
    turns.sort(key=lambda t: t["at"])
    recent = turns[-max(n * 4, n):]  # recent enough to reflect how you talk now
    step = max(len(recent) // n, 1)
    picked = recent[::step][:n]
    RESULTS.mkdir(exist_ok=True)
    INPUTS.write_text(json.dumps(picked, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(turns)} usable turns found, {len(picked)} picked -> {INPUTS}")


class Client:
    def __init__(self, base: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call("POST", "/api/login", {"password": password})

    def call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(self.base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return self.http.open(req, timeout=300)

    def json(self, method: str, path: str, body: dict | None = None) -> dict:
        with self.call(method, path, body) as r:
            return json.load(r)

    def ask(self, message: str) -> dict:
        """One message in a fresh side chat, nothing learned; returns the run_end event."""
        with self.call("POST", "/api/chat", {"message": message, "learn": False}) as r:
            for raw in r:
                line = raw.decode("utf-8").strip()
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    if event.get("type") == "run_end":
                        return event
        raise RuntimeError("the stream ended without run_end")


def run(label: str, base: str, password: str, persona: Path | None) -> None:
    inputs = json.loads(INPUTS.read_text(encoding="utf-8"))
    client = Client(base, password)
    me = client.json("GET", "/api/me")
    old = client.json("GET", "/api/persona")
    if persona:
        client.json("PUT", "/api/persona", {**{k: old[k] for k in ("name", "hue", "heartbeat_minutes", "heartbeat_prompt")},
                                            "soul": persona.read_text(encoding="utf-8").strip()})
    out = []
    try:
        for i, item in enumerate(inputs, 1):
            e = client.ask(item["input"])
            out.append({"input": item["input"], "reply": e["output"], "status": e["status"],
                        "latency_ms": e["latency_ms"], "cost_usd": e.get("cost_usd")})
            print(f"[{label}] {i}/{len(inputs)} {e['status']} {e['latency_ms']} ms")
    finally:
        if persona:  # put the persona back even if a run failed
            client.json("PUT", "/api/persona", {k: old[k] for k in ("name", "soul", "hue", "heartbeat_minutes", "heartbeat_prompt")})
    meta = {"label": label, "model": me["model"], "prompt_version": me["prompt_version"],
            "persona_file": str(persona) if persona else None}
    (RESULTS / f"voice_{label}.json").write_text(json.dumps({"meta": meta, "replies": out}, indent=2, ensure_ascii=False),
                                                 encoding="utf-8")
    print(f"saved {RESULTS / f'voice_{label}.json'}  ({meta['model']})")


def sheet(labels: list[str], seed: int) -> None:
    inputs = json.loads(INPUTS.read_text(encoding="utf-8"))
    runs = {l: json.loads((RESULTS / f"voice_{l}.json").read_text(encoding="utf-8"))["replies"] for l in labels}
    rng, key, lines = random.Random(seed), [], ["# Voice A/B (blind)", "",
                                                "For each message, read the replies and write your favourite's letter "
                                                "(or `none`) next to **Pick**. Then compare against `voice_key.json`.", ""]
    for i, item in enumerate(inputs):
        cands = [("v1", item["v1_reply"])] + [(l, runs[l][i]["reply"]) for l in labels]
        rng.shuffle(cands)
        key.append({str(i + 1): {chr(65 + j): src for j, (src, _) in enumerate(cands)}})
        lines += [f"## {i + 1}. {item['input']}", ""]
        for j, (_, text) in enumerate(cands):
            lines += [f"**{chr(65 + j)}.** " + text.replace("\n", "\n> "), ""]
        lines += ["**Pick:** ", ""]
    (RESULTS / "voice_sheet.md").write_text("\n".join(lines), encoding="utf-8")
    (RESULTS / "voice_key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    print(f"sheet -> {RESULTS / 'voice_sheet.md'}  (don't open voice_key.json until you have picked)")


def score(labels: list[str]) -> None:
    """After filling in the sheet: `python -m evals.voice_ab score --labels A B C` reads your picks and the key."""
    sheet_text = (RESULTS / "voice_sheet.md").read_text(encoding="utf-8")
    picks = re.findall(r"\*\*Pick:\*\*\s*([A-Za-z]+)?", sheet_text)
    key = [next(iter(k.values())) for k in json.loads((RESULTS / "voice_key.json").read_text(encoding="utf-8"))]
    wins: dict[str, int] = {}
    for pick, mapping in zip(picks, key):
        src = mapping.get((pick or "").upper(), "none")
        wins[src] = wins.get(src, 0) + 1
    print("favourites:", dict(sorted(wins.items(), key=lambda kv: -kv[1])))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect"); c.add_argument("--archive", type=Path, required=True); c.add_argument("--n", type=int, default=20)
    r = sub.add_parser("run"); r.add_argument("--label", required=True); r.add_argument("--password", required=True)
    r.add_argument("--base-url", default="http://127.0.0.1:8000"); r.add_argument("--persona", type=Path)
    s = sub.add_parser("sheet"); s.add_argument("--labels", nargs="+", required=True); s.add_argument("--seed", type=int, default=7)
    k = sub.add_parser("score"); k.add_argument("--labels", nargs="+", default=[])
    a = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    if a.cmd == "collect":
        collect(a.archive, a.n)
    elif a.cmd == "run":
        run(a.label, a.base_url, a.password, a.persona)
    elif a.cmd == "sheet":
        sheet(a.labels, a.seed)
    else:
        score(a.labels)
    return 0


if __name__ == "__main__":
    sys.exit(main())
