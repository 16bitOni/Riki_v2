"""MCP servers from mcp.json, kept connected for the app's lifetime (no process spawn per call).

Each server declares (glob patterns) which of its tools are safe (auto-run) or external (never
run); everything else is ``write`` and needs approval. A server that fails to start is logged and skipped.
"""

import fnmatch
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from pathlib import Path

from dotenv import dotenv_values
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from .policy import EXTERNAL, RISKS, SAFE, WRITE, moves_money
from .tools import ToolEntry, unseen_url

log = logging.getLogger(__name__)

_CONNECTION_KEYS = {"transport", "command", "args", "env", "cwd", "url", "headers"}


_VAR = re.compile(r"\$\{(\w+)\}")


def _expand(value, env: dict):
    """${VAR} comes from the environment or backend/.env, so secrets stay out of mcp.json."""
    if isinstance(value, str):
        return _VAR.sub(lambda m: env.get(m[1], m[0]), value)
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    return value


def risk_for(server: dict, tool_name: str) -> str:
    """external_tools / safe_tools are glob patterns ("get_*"); external wins over safe."""
    if any(fnmatch.fnmatchcase(tool_name, p) for p in server.get("external_tools", [])):
        return EXTERNAL
    if any(fnmatch.fnmatchcase(tool_name, p) for p in server.get("safe_tools", [])):
        return SAFE
    default = server.get("default_risk", WRITE)
    if default not in RISKS:
        raise ValueError(f"default_risk must be one of {RISKS}")
    return default


async def load_mcp(path: Path, stack: AsyncExitStack, status: dict | None = None) -> list[ToolEntry]:
    """``status`` (if given) gets {server: {"ok", "tools", "error"}} for the System page."""
    status = {} if status is None else status
    if not path.exists():
        log.info("no %s; running with built-in tools only", path)
        return []
    env = {**dotenv_values(path.with_name(".env")), **os.environ}
    servers, connections = {}, {}
    for name, cfg in json.loads(path.read_text()).get("servers", {}).items():
        if not cfg.get("enabled", True):
            continue
        conn = _expand({k: v for k, v in cfg.items() if k in _CONNECTION_KEYS}, env)
        missing = sorted(set(_VAR.findall(json.dumps(conn))))
        if missing:
            log.warning("MCP server %r skipped: set %s in backend/.env", name, ", ".join(missing))
            status[name] = {"ok": False, "tools": 0, "error": f"set {', '.join(missing)} in backend/.env"}
            continue
        servers[name], connections[name] = cfg, conn
    client = MultiServerMCPClient(connections)
    entries: list[ToolEntry] = []
    for name, cfg in servers.items():
        try:
            session = await stack.enter_async_context(client.session(name))
            tools = await load_mcp_tools(session, server_name=name, tool_name_prefix=True)
        except Exception as exc:  # one broken server must not take Riki down
            while isinstance(exc, BaseExceptionGroup) and exc.exceptions:  # anyio wraps the real cause
                exc = exc.exceptions[0]
            log.warning("MCP server %r failed to start: %s: %s", name, type(exc).__name__, exc)
            status[name] = {"ok": False, "tools": 0, "error": f"{type(exc).__name__}: {exc}"[:300]}
            continue
        for t in tools:
            short = t.name.removeprefix(f"{name}_")
            ask = unseen_url if any(fnmatch.fnmatchcase(short, p) for p in cfg.get("url_tools", [])) else None
            entries.append(ToolEntry(t, risk_for(cfg, short), f"mcp:{name}", ask_if=ask, money=moves_money(short)))
        log.info("MCP server %r: %d tools", name, len(tools))
        status[name] = {"ok": True, "tools": len(tools), "error": None}
    return entries
