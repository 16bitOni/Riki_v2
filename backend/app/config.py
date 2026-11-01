"""Every setting in one place. Read from RIKI_* environment variables or backend/.env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent

Effort = Literal["low", "medium", "high"]
# Per-message caps on top of the graph limits below ("high" = those limits as configured). Low answers alone in a
# few steps; medium may use a couple of helpers; high may fan out to the full budget.
EFFORT: dict[str, dict[str, int]] = {
    "low": {"max_depth": 0, "max_agents_per_run": 0, "max_steps_per_agent": 4, "run_token_budget": 40_000},
    "medium": {"max_depth": 1, "max_agents_per_run": 3, "max_steps_per_agent": 8, "run_token_budget": 120_000},
    "high": {},
}


class Price(BaseModel):
    """Dollars per million tokens. ``cached`` defaults to ``input`` (no cache discount)."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cached: float | None = Field(None, ge=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RIKI_", env_file=BACKEND_DIR / ".env", extra="ignore",
                                      env_ignore_empty=True,  # blank RIKI_X= means "use the default"
                                      env_parse_none_str="none")  # RIKI_X=none means "leave it out"

    database_url: str = "postgresql+asyncpg://riki:riki@localhost:5433/riki"
    password: SecretStr = Field(description="Login password for the single user (min 12 chars).")
    cookie_secure: bool = False  # set true when served over HTTPS

    # Models use LangChain's "provider:model" strings, e.g. "openai:gpt-4.1-mini" or "bedrock_converse:<id>".
    model: str = "openai:gpt-4.1-mini"
    smart_model: str = "openai:gpt-4.1"
    embedding_model: str = "openai:text-embedding-3-small"
    temperature: float | None = 0  # 0 for consistency; RIKI_TEMPERATURE=none for models that refuse the field
    # Any OpenAI-compatible server (OpenRouter, vLLM, Ollama, LM Studio...). Unset = provider default / OPENAI_API_KEY.
    base_url: str | None = None
    api_key: SecretStr | None = None
    # Embeddings fall back to base_url / api_key when these are unset.
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    aws_region: str = "us-east-1"  # only used by bedrock* models
    prompt_cache: bool = True  # Bedrock cache points on system/tools/last message
    # What each model costs, keyed by the exact RIKI_MODEL / RIKI_SMART_MODEL string, e.g.
    # RIKI_PRICES='{"openai:gpt-4.1-mini": {"input": 0.4, "cached": 0.1, "output": 1.6}}'. A model with no
    # entry is not priced: the run's cost stays empty and the run is flagged `unpriced_models`.
    prices: dict[str, Price] = {}

    # The person Riki serves. Memory namespace and display name.
    user_id: str = "me"
    user_name: str = "Subhadip"
    timezone: str = "Asia/Kolkata"
    quiet_hours: str = Field("22-8", pattern=r"^\d{1,2}-\d{1,2}$")  # local hours with no check-ins ("0-0" = none)

    # Graph limits: keep runaway recursion and spend bounded.
    max_depth: int = Field(3, ge=0, le=6)
    max_agents_per_run: int = Field(12, ge=1)
    max_steps_per_agent: int = Field(12, ge=2)
    run_token_budget: int = Field(300_000, ge=1_000)
    default_effort: Effort = "medium"  # when a message doesn't pick one
    tool_timeout_s: float = 60
    run_timeout_s: float = Field(600, gt=0)  # whole request, all agents; covers sub-agents and hung model calls
    tool_output_chars: int = 6_000  # what the model sees; the trace keeps more
    history_turns: int = 40  # most turns of this chat sent to the model...
    history_chars: int = Field(16_000, ge=1_000)  # ...cut from the oldest end past this many characters (4 turns always stay)
    approval_timeout_s: int = 24 * 3600  # how long a change waits for your OK (it survives a restart; see policy.py)
    # Memory before the model answers: "heuristic" plans the search with regex (no extra model call; the model can
    # still call recall_memory itself), "escalate" asks a planner model when that finds nothing, "llm" always does.
    memory_planner: Literal["heuristic", "escalate", "llm"] = "heuristic"
    memory_timeout_s: float = 8  # a slow memory lookup must never hold the answer back

    core_tools: list[str] = ["current_time", "recall_memory", "remember", "web_search", "fetch_url",
                             "spawn_agent", "load_tools", "connect_app", "ask_user", "read_file", "schedule",
                             "save_markdown", "save_artifact"]
    mcp_config: Path = BACKEND_DIR / "mcp.json"

    # Telegram: talk to Riki and approve things from your phone. Long polling, so no public address is needed.
    # Only this one chat is ever answered or messaged; get the id by writing to the bot once (it is logged).
    telegram_token: SecretStr | None = None
    telegram_chat_id: int | None = None

    # Riki thinking between chats (reflection, lessons, nudges). Background work pauses when the day's budget is 80% used.
    cognition: bool = True
    cognition_interval_h: float = Field(4, gt=0)
    daily_budget_usd: float | None = Field(None, gt=0)  # none = no cap; needs RIKI_PRICES to count anything

    # run_python: a throwaway Docker container with no network and a read-only disk. Off unless you switch it on.
    sandbox: bool = False
    sandbox_image: str = "python:3.12-slim"
    upload_max_mb: int = Field(15, ge=1, le=100)

    def for_effort(self, effort: str) -> "Settings":
        """These settings with an effort level's caps applied; never raises a limit above what is configured."""
        return self.model_copy(update={k: min(getattr(self, k), v) for k, v in EFFORT[effort].items()})

    @field_validator("password")
    @classmethod
    def strong_enough(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 12:
            raise ValueError("RIKI_PASSWORD must be at least 12 characters")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
