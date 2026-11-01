"""riki-mem wired to the same LangChain models the agent uses (one provider config for everything)."""

import json
from functools import cache
from typing import get_origin

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import field_validator, model_validator
from riki_memory import telemetry


@cache
def lenient(schema):
    """The same schema, but a list the model sent as a JSON string ('[{...}]', as Kimi sometimes does) is read as the
    list. The check that rejected it runs inside the OpenAI client, so it has to be fixed in the schema."""
    def unquote(cls, value, info):
        if isinstance(value, str) and value.lstrip().startswith("[") and get_origin(cls.model_fields[info.field_name].annotation) is list:
            try:
                return json.loads(value)
            except ValueError:
                pass
        return value

    def unwrap(cls, value):
        # some models answer {"parameters": {...}} (a tool-call envelope) instead of the object itself
        if isinstance(value, dict) and isinstance(value.get("parameters"), dict) and not set(cls.model_fields) & set(value):
            return value["parameters"]
        return value

    return type(schema.__name__, (schema,), {"__module__": schema.__module__,
                                             "_unquote": field_validator("*", mode="before")(classmethod(unquote)),
                                             "_unwrap": model_validator(mode="before")(classmethod(unwrap))})


class LangChainProvider:
    """riki-mem's 3-method LLMProvider protocol over LangChain chat + embedding models."""

    def __init__(self, fast: BaseChatModel, smart: BaseChatModel, embeddings: Embeddings, embedding_model_id: str):
        self.fast, self.smart, self.embeddings = fast, smart, embeddings
        self.embedding_model_id = embedding_model_id

    async def chat_json(self, system: str, user: str, schema, *, fast: bool = True):
        model = (self.fast if fast else self.smart).with_structured_output(lenient(schema), include_raw=True)
        out = await model.ainvoke([SystemMessage(system), HumanMessage(user)])
        usage = getattr(out["raw"], "usage_metadata", None) or {}
        telemetry.record_model_call(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        if out.get("parsing_error") or out.get("parsed") is None:
            raise ValueError(f"model reply did not match {schema.__name__}: {out.get('parsing_error')}")
        return out["parsed"]

    async def chat_text(self, system: str, user: str, *, fast: bool = True) -> str:
        msg = await (self.fast if fast else self.smart).ainvoke([SystemMessage(system), HumanMessage(user)])
        usage = msg.usage_metadata or {}
        telemetry.record_model_call(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        return msg.text

    async def embed(self, text: str) -> list[float]:
        telemetry.record_embed_call()
        return await self.embeddings.aembed_query(text)
