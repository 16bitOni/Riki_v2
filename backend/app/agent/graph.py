"""The agent: a LangGraph loop (model -> tools -> model) that any agent at any depth runs.

Nesting works through the ``spawn_agent`` tool: it runs this same graph again, one level deeper,
with a fresh context (task + context only) and only the tools it was given. Parallel spawns in one
turn run concurrently. Depth, agent count, steps and tokens are all bounded per run.
"""

import asyncio
import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, TypedDict

import openai
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import message_chunk_to_message
from langchain_core.tools import BaseTool, tool
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import RetryPolicy
from pydantic import BaseModel, Field

from ..tracing import RUN, span
from .policy import EXTERNAL, SAFE, authorize
from .tools import ToolEntry

if TYPE_CHECKING:
    from ..config import Settings
    from ..runtime import Runtime

AGENT_DEPTH: ContextVar[int] = ContextVar("riki_depth", default=0)
NOT_LOADED = "is not available to you"
CLEARED = "cleared from context to save tokens"
BUDGET_NOTE = "I stopped here: this request reached its token budget. Ask me to continue if it is worth more."
REPEATED = "You already made this exact call"
SUB_AGENT_BASE_TOOLS = ["current_time", "recall_memory", "load_tools"]


class ProviderHiccup(Exception):
    """The provider stalled or failed mid-stream before the user saw anything: the model step is retried."""


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    tools: list[str]  # names this agent can call right now; load_tools grows it


@dataclass(frozen=True)
class AgentSpec:
    name: str
    system: str
    tier: str  # "fast" | "smart"
    depth: int


class SpawnArgs(BaseModel):
    task: str = Field(description="One clear, self-contained task.")
    context: str = Field("", description="Everything the sub-agent needs to know. It cannot see this chat.")
    tools: list[str] = Field(default_factory=list, description="Catalog tool names it will need.")
    smart: bool = Field(False, description="Use the stronger, slower model. Only for hard reasoning.")


class LoadArgs(BaseModel):
    names: list[str] = Field(description="Tool names from the catalog.")


def as_text(out) -> str:
    """Tool results come back as str, content blocks (MCP) or objects; the model needs text."""
    if isinstance(out, str):
        return out
    if isinstance(out, list) and all(isinstance(b, dict) for b in out):
        texts = [b.get("text", "") for b in out if b.get("type") == "text"]
        if texts:
            return "\n".join(texts)
    return json.dumps(out, default=str, ensure_ascii=False)


def compact(messages: list[AnyMessage], budget: int) -> list[AnyMessage]:
    """What the model is sent: older tool results past ``budget`` characters become a short stub (the trace keeps
    them). Without this every step re-sends every page read so far, so a long run costs the square of its length.
    The latest round of results is always sent whole: it is what the model is about to act on."""
    out, left, latest = [], budget, True
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            latest = False
        elif isinstance(m, ToolMessage) and not latest:
            if len(m.content) <= left:
                left -= len(m.content)
            else:
                ref = m.content.split(" ", 1)[0] if m.content.startswith("[t") else ""
                m = ToolMessage(f"{ref} [older result {CLEARED}; call the tool again if you still need it]".strip(),
                                tool_call_id=m.tool_call_id, name=m.name, status=m.status)
        out.append(m)
    return out[::-1]


class Agent:
    def __init__(self, rt: "Runtime") -> None:
        self.rt = rt
        g = StateGraph(AgentState)
        # A flaky provider can answer 200 with an empty stream, which the client does not retry. Nothing has been
        # emitted to the user at that point, so retrying the step is safe. Other errors are not retried here.
        g.add_node("model", self._call_model, retry_policy=RetryPolicy(
            max_attempts=3, retry_on=lambda e: isinstance(e, ProviderHiccup)
            or isinstance(e, ValueError) and "No generation chunks" in str(e)))
        g.add_node("tools", self._call_tools)
        g.add_edge(START, "model")
        g.add_conditional_edges("model", lambda s: "tools" if s["messages"][-1].tool_calls else END)
        g.add_edge("tools", "model")
        self.graph = g.compile()

    # -- public ------------------------------------------------------------------------------

    async def run(self, spec: AgentSpec, messages: list[AnyMessage], tools: list[str]) -> str:
        """Run one agent to completion and return its final text."""
        s = self.limits()
        depth_token = AGENT_DEPTH.set(spec.depth)
        try:
            async with span("agent", spec.name, input={"task": as_text(messages[-1].content), "tools": tools},
                            depth=spec.depth, tier=spec.tier) as sp:
                try:
                    state = await self.graph.ainvoke(
                        {"messages": messages, "tools": tools},
                        {"configurable": {"spec": spec}, "recursion_limit": 2 * s.max_steps_per_agent + 1},
                    )
                    text = state["messages"][-1].text or "(no reply)"
                except GraphRecursionError:
                    sp["status"], sp["error"] = "error", "step limit reached"
                    text = "I stopped: this took more steps than allowed. What I found so far is in the trace."
                sp["output"] = text
                return text
        finally:
            AGENT_DEPTH.reset(depth_token)

    def agent_tools(self) -> list[ToolEntry]:
        """spawn_agent and load_tools: the two tools that make this a graph of agents."""

        @tool(args_schema=SpawnArgs)
        async def spawn_agent(task: str, context: str = "", tools: list[str] | None = None,
                              smart: bool = False) -> str:
            """Delegate one task to a new sub-agent with its own clean context; returns its final answer.
            Call several times in one turn to run independent tasks in parallel."""
            ctx, s = RUN.get(), self.limits()
            depth = AGENT_DEPTH.get() + 1
            if depth > s.max_depth:
                return "Refused: maximum agent depth reached. Do this step yourself."
            if ctx.agents_spawned >= s.max_agents_per_run:
                return "Refused: the sub-agent budget for this request is used up. Finish with what you have."
            if ctx.tokens_in + ctx.tokens_out >= s.run_token_budget:  # no new fan-out once the money is gone
                return "Refused: the token budget for this request is used up. Finish with what you have."
            ctx.agents_spawned += 1
            base = SUB_AGENT_BASE_TOOLS + (["spawn_agent"] if depth < s.max_depth else [])
            names = [n for n in dict.fromkeys(base + (tools or [])) if self.offerable(n, depth)]
            prompt = f"{context}\n\nTask: {task}" if context else task
            spec = AgentSpec(f"sub-agent: {task[:80]}", self.rt.sub_system, "smart" if smart else "fast", depth)
            return await self.run(spec, [HumanMessage(prompt)], names)

        @tool(args_schema=LoadArgs)
        def load_tools(names: list[str]) -> str:
            """Make tools from the catalog callable for you from your next step on."""
            depth = AGENT_DEPTH.get()
            ok = [n for n in self.rt.registry.expand(names) if self.offerable(n, depth)]
            missing = [n for n in names if "*" not in n and n not in ok]
            return f"Loaded: {', '.join(ok) or 'none'}." + (f" Not available: {', '.join(missing)}." if missing else "")

        return [ToolEntry(spawn_agent, SAFE), ToolEntry(load_tools, SAFE)]

    def limits(self) -> "Settings":
        """The settings with this run's effort applied (config.EFFORT); outside a run, as configured."""
        ctx = RUN.get(None)
        return self.rt.settings.for_effort(ctx.effort) if ctx else self.rt.settings

    def offerable(self, name: str, depth: int) -> bool:
        entry = self.rt.registry.get(name)
        if entry is None or entry.risk == EXTERNAL:  # external tools are never even shown to a model
            return False
        if name == "connect_app":  # only the agent talking to the user can put a card in front of them
            return depth == 0
        return not (name == "spawn_agent" and depth >= self.limits().max_depth)

    # -- graph nodes -------------------------------------------------------------------------

    async def _call_model(self, state: AgentState, config) -> dict:
        spec: AgentSpec = config["configurable"]["spec"]
        ctx, s = RUN.get(), self.limits()
        over = ctx.tokens_in + ctx.tokens_out >= s.run_token_budget
        # Out of tokens with something gathered: one last call with no tools turns it into an answer instead of an
        # apology (a request that spent its budget researching used to end with nothing at all).
        # shortcut: that call may overshoot the budget by one answer; every agent of the run gets one.
        landed = any(m.additional_kwargs.get("landed") for m in state["messages"] if isinstance(m, AIMessage))
        if over and (landed or not any(isinstance(m, ToolMessage) for m in state["messages"])):
            return {"messages": [AIMessage(BUDGET_NOTE)]}
        tools: list[BaseTool] = [self.rt.registry.get(n).tool for n in state["tools"] if self.offerable(n, spec.depth)]
        messages = [SystemMessage(spec.system), *compact(state["messages"], 4 * s.tool_output_chars)]
        rounds = sum(1 for m in state["messages"] if isinstance(m, AIMessage) and m.tool_calls)
        # a model that ignores the loop breaker twice would burn every remaining step on refused calls
        looping = sum(1 for m in state["messages"] if isinstance(m, ToolMessage) and REPEATED in m.content) >= 2
        if looping or over or rounds >= s.max_steps_per_agent - 1:  # last step: no tools, the work so far becomes an answer
            tools = []
            messages.append(HumanMessage(("You keep repeating refused calls" if looping else
                                          "Token budget reached" if over else "Step limit reached")
                                         + ": no more tools. Answer now with what you found so far "
                                         "and say plainly what is still missing."))
        model = self.rt.models[spec.tier]
        if tools:
            model = model.bind_tools(tools)
        last = state["messages"][-1]
        async with span("llm", self.rt.model_names[spec.tier],
                        input={"messages": len(messages), "tools": [t.name for t in tools],
                               "last": last.content if isinstance(last.content, str) else as_text(last.content)}) as sp:
            acc, shown = None, False
            try:
                async for chunk in model.astream(messages, **self.rt.call_kwargs[spec.tier]):
                    acc = chunk if acc is None else acc + chunk
                    if spec.depth == 0 and chunk.text:
                        ctx.emit("token", text=chunk.text)
                        shown = True
            except (TimeoutError, openai.APIError) as e:
                # A stalled stream or a mid-stream provider error ("try your request again"). Status errors were
                # already retried by the client. Retry the step only if the user has seen none of it yet.
                if shown or isinstance(e, openai.APIStatusError):
                    raise
                raise ProviderHiccup(str(e)) from e
            msg = message_chunk_to_message(acc)
            usage = msg.usage_metadata or {}
            cached = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0
            sp.update(tokens_in=usage.get("input_tokens", 0), tokens_out=usage.get("output_tokens", 0),
                      tokens_cached=cached, output={"text": msg.text, "tool_calls": [
                          {"name": c["name"], "args": c["args"]} for c in msg.tool_calls]})
            ctx.tokens_in += sp["tokens_in"]
            ctx.tokens_out += sp["tokens_out"]
            ctx.tokens_cached += cached
        if over:  # a model that still asks for tools, or says nothing, ends on the plain notice
            msg = AIMessage(BUDGET_NOTE) if msg.tool_calls or not msg.text.strip() else msg
            msg.additional_kwargs["landed"] = True
        return {"messages": [msg]}

    async def _call_tools(self, state: AgentState, config) -> dict:
        spec: AgentSpec = config["configurable"]["spec"]
        calls = state["messages"][-1].tool_calls
        key = lambda c: (c["name"], json.dumps(c["args"], sort_keys=True, default=str))  # noqa: E731
        # a call whose result the model never saw ("not loaded", or cleared by compact) may be retried
        unseen = {m.tool_call_id for m in compact(state["messages"][:-1], 4 * self.limits().tool_output_chars)
                  if isinstance(m, ToolMessage) and (NOT_LOADED in m.content or CLEARED in m.content)}
        seen = {key(c) for m in state["messages"][:-1] if isinstance(m, AIMessage)
                for c in m.tool_calls if c["id"] not in unseen}
        results = await asyncio.gather(*(self._run_tool(c, state["tools"], key(c) in seen) for c in calls))
        active = list(state["tools"])
        for call, (_, ok) in zip(calls, results):
            if call["name"] == "load_tools" and ok:
                active += [n for n in self.rt.registry.expand(call["args"].get("names", []))
                           if self.offerable(n, spec.depth) and n not in active]
        return {"messages": [m for m, _ in results], "tools": active}

    async def _run_tool(self, call: dict, active: list[str], repeated: bool = False) -> tuple[ToolMessage, bool]:
        ctx, s = RUN.get(), self.limits()
        name, args = call["name"], call.get("args") or {}
        entry = self.rt.registry.get(name)
        ok = False
        risk = entry.risk_for(args) if entry else None
        async with span("tool", name, input=args, risk=risk) as sp:
            sp["ref"] = ref = ctx.next_ref()
            if entry is None or name not in active:
                sp["status"] = "error"
                text = f"Tool '{name}' {NOT_LOADED}. Use load_tools with a catalog name first."
            elif repeated:  # same tool, same arguments as an earlier step of this agent: the loop breaker
                sp["status"] = "error"
                text = (f"{REPEATED}. Do not repeat it: use its earlier result, change the arguments (to read "
                        "more of a page, pass the start_line it gave), or answer with what you have.")
            else:
                allowed, reason = await authorize(self.rt.db, risk, name, args, sp["id"], s.approval_timeout_s)
                if not allowed:
                    sp["status"], text = "denied", reason
                else:
                    try:
                        coro = entry.tool.ainvoke(args)
                        # sub-agents are bounded by their own step/token limits, not the tool timeout
                        out = await (coro if name == "spawn_agent" else asyncio.wait_for(coro, s.tool_timeout_s))
                        text, ok = as_text(out), True
                        sp["output"] = text
                        if name in ("web_search", "fetch_url") or entry.source == "mcp:browser":
                            ctx.seen += "\n" + text
                    except Exception as exc:
                        sp["status"], sp["error"] = "error", f"{type(exc).__name__}: {exc}"[:2000]
                        text = f"Tool error: {sp['error']}"
        if len(text) > s.tool_output_chars:
            text = (text[: s.tool_output_chars]
                    + f"\n[output truncated from {len(text)} chars; "
                            "if you need the rest, ask for less: a narrower query or a smaller part]")
        return ToolMessage(f"[{ref}] {text}", tool_call_id=call["id"], name=name,
                           status="success" if ok else "error"), ok
