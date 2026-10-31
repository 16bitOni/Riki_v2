"""Web search through Tavily (https://docs.tavily.com). The key is the Tavily app's encrypted secret, sent only as
a bearer header to api.tavily.com, never logged, never shown to a model or the browser."""

import httpx

API = "https://api.tavily.com"


class TavilyError(Exception):
    """Message is safe to show: it carries the HTTP status, never a request header."""


async def _call(key: str, method: str, path: str, **kwargs) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.request(method, API + path, headers={"Authorization": f"Bearer {key}"}, **kwargs)
    except httpx.HTTPError as exc:
        raise TavilyError(f"couldn't reach Tavily ({type(exc).__name__})") from None
    if r.status_code in (401, 403):
        raise TavilyError("Tavily rejected the key")
    if r.status_code in (429, 432, 433):
        raise TavilyError("Tavily's usage limit is reached")
    if r.status_code >= 400:
        raise TavilyError(f"Tavily answered {r.status_code}")
    return r.json()


async def check_key(key: str) -> None:
    """Raises TavilyError if the key doesn't work. /usage costs no credits."""
    await _call(key, "GET", "/usage")


async def search(key: str, query: str, max_results: int = 5, topic: str = "general", images: bool = False) -> str:
    data = await _call(key, "POST", "/search", json={"query": query, "topic": topic, "search_depth": "basic",
                                                     "max_results": max(1, min(max_results, 10)),
                                                     "include_images": images})
    out = "\n".join(f"- {h.get('title')} | {h.get('url')}\n  {(h.get('content') or '')[:500]}"
                    for h in data.get("results", [])) or "No results."
    pics = [i if isinstance(i, str) else i.get("url") for i in data.get("images", [])][:5]
    return out + ("\nImages:\n" + "\n".join(f"- {u}" for u in pics if u) if images and pics else "")


async def duckduckgo(query: str, max_results: int = 5) -> str:
    import asyncio

    from ddgs import DDGS

    hits = await asyncio.to_thread(lambda: DDGS().text(query, max_results=max(1, min(max_results, 10))))
    return "\n".join(f"- {h.get('title')} | {h.get('href')}\n  {h.get('body')}" for h in hits) or "No results."
