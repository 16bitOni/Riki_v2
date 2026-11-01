"""Answer checks, run on every chat and inside evals.

"Backed" means: every citation in a reply points at evidence that exists in this run's trace.
A reply that cites [t7] when no tool returned t7, or a memory id that was never shown to the
model, is a hallucinated citation and gets flagged on the run (free, deterministic).

"Grounded" means: every specific claim in the reply is in the evidence. A model compares the two,
because a reply that cites nothing at all passes the citation check while being made up.
"""

import json
import re

TOOL_REF = re.compile(r"\[(t\d+)\]")
MEMORY_REF = re.compile(r"\[m:([0-9a-f]{8})\]")
UNSUPPORTED = re.compile(r"^\W*UNSUPPORTED\W*:\s*(.+)$", re.I | re.M)


# tools whose output is not evidence about the world: plumbing, or a sub-agent's retelling (its own tool calls count)
NOT_EVIDENCE = {"load_tools", "connect_app", "spawn_agent", "remember"}


def is_source(span: dict) -> bool:
    return span["kind"] == "tool" and span["status"] == "ok" and span["name"] not in NOT_EVIDENCE


def evidence(spans: list[dict], limit: int = 30_000) -> str:
    """What the reply may rest on: tool results and memories, labelled with their refs."""
    parts = [f"[{s.get('ref') or s['name']}] {s['output'] if isinstance(s['output'], str) else json.dumps(s['output'])}"
             for s in spans if s.get("output") and (is_source(s) or s["kind"] == "memory"
                                                    or s["name"] == "recall_memory")]
    # shortcut: evidence past the limit is not checked, so claims from it get flagged; raise it if that happens
    return "\n\n".join(parts)[:limit]


def run_log(spans: list[dict]) -> str:
    """What this run did, failures included: "the search timed out" is backed by this, not by a tool result.
    Plumbing tools (connect_app...) are no evidence about the world, but what they returned is what the run did."""
    return "\n".join(f"- {s['name']}: {s['status']}" + (f" ({s['error'][:150]})" if s.get("error") else "")
                     + (f": {str(s['output'])[:300]}" if s["name"] in NOT_EVIDENCE and s.get("output") else "")
                     for s in spans if s["kind"] == "tool") or "(no tools were used)"


def grounding_input(question: str, proof: str, reply: str, *, now: str, spans: list[dict], apps: list[str]) -> str:
    """What the checking model reads. Beyond the tool results it needs the clock, what the run did (a failure has no
    result to quote) and which apps are connected, or it flags a correct time and an honest "that failed"."""
    return (f"Now: {now}\nConnected apps: {', '.join(apps) or 'none'}\nTools used in this run:\n{run_log(spans)}\n\n"
            f"User message:\n{question}\n\nEvidence:\n{proof}\n\nReply to check:\n{reply}")


_CLAIMISH = re.compile(r"\d|https?://|[$₹€£]")


def worth_checking(spans: list[dict], reply: str) -> bool:
    """The fact check costs a model call, so it runs when the reply can hold claims worth checking: this run looked
    something up, or the reply has a number, a link or a price, or is long enough to carry a list of facts.
    A short chatty reply with none of those (opinions, banter, "sounds rough") is not checked."""
    return any(is_source(s) for s in spans) or bool(_CLAIMISH.search(reply)) or len(reply) > 600


def parse_unsupported(text: str) -> list[str]:
    """Only `UNSUPPORTED:` lines count: some models think aloud first, and that prose must not become a flag."""
    return [m.strip() for m in UNSUPPORTED.findall(text) if m.strip()][:8]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[‐-―]", "-", text.replace("’", "'"))).lower()


def drop_found(claims: list[str], proof: str) -> list[str]:
    """A checker model sometimes flags an item that is right there in the evidence ("Title - Author"). If the
    claim's head (before a dash, colon or bracket) appears word for word in the evidence, it is supported."""
    proof = _norm(proof)
    heads = [re.split(r" - |: | \(", _norm(c).strip("*\"' "), maxsplit=1)[0] for c in claims]
    return [c for c, h in zip(claims, heads) if len(h) < 8 or h not in proof]


def citation_check(reply: str, spans: list[dict]) -> dict:
    tool_refs = {s["ref"] for s in spans if s.get("ref") and s["status"] == "ok"}
    # memory ids the model could have seen: recall context and recall_memory/remember outputs
    seen = " ".join(json.dumps(s.get("output"), default=str) for s in spans
                    if s["kind"] == "memory" or s["name"] in ("recall_memory", "remember"))
    cited_tools = set(TOOL_REF.findall(reply))
    cited_memories = set(MEMORY_REF.findall(reply))
    # A call that failed or was refused still happened: "the search timed out [t5]" is honest, so only a ref that
    # was never issued is a hallucination. Citing a failed call as the source of a fact is for the grounding check.
    issued = {s["ref"] for s in spans if s.get("ref")}
    invalid = sorted(r for r in cited_tools if r not in issued)
    failed = sorted(r for r in cited_tools if r in issued and r not in tool_refs)
    invalid += sorted(f"m:{m}" for m in cited_memories if m not in seen)
    return {"citations": len(cited_tools) + len(cited_memories), "invalid_citations": invalid,
            **({"failed_citations": failed} if failed else {})}
