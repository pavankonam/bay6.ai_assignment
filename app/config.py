"""Application settings (pydantic-settings), loaded from the environment and `.env`."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """All runtime configuration. Field names match the env var names in `.env.example`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=True,
    )

    # LLM
    LLM_PROVIDER: Literal["anthropic", "fake"] = "anthropic"
    LLM_MODEL: str = "claude-sonnet-5-5"
    LLM_TIMEOUT_SECONDS: float = 20.0
    LLM_MAX_OUTPUT_TOKENS: int = 1024
    LLM_HISTORY_MAX_CHARS: int = 4000
    MAX_LLM_CALLS_PER_THREAD: int = 120
    ANTHROPIC_API_KEY: SecretStr | None = None
    TOOL_SELECTION_MODE: Literal["llm", "deterministic"] = "llm"

    # Business rules
    APP_TODAY: date | None = None
    INTENT_CONFIDENCE_THRESHOLD: float = 0.6
    MAX_TURNS: int = 30
    MAX_CLARIFICATION_ATTEMPTS: int = 3
    MAX_VERIFICATION_ATTEMPTS: int = 3
    POLICY_VERIFY_LOCK_FAILURES: int = 5
    POLICY_VERIFY_LOCK_HOURS: int = 24
    RATE_LIMIT_PER_MINUTE: int = 30
    CLAIM_APPROVAL_THRESHOLD_USD: float = 10000.0
    APPROVAL_TTL_HOURS: float = 48.0

    # Tools
    TOOL_TIMEOUT_SECONDS: float = 5.0
    TOOL_MAX_RETRIES: int = 2
    TOOL_RETRY_BASE_SECONDS: float = 0.5
    RECURSION_LIMIT: int = 50

    # Mock API fault injection
    MOCK_FAULTS: bool = False
    MOCK_FAILURE_RATE: float = Field(default=0.0, ge=0.0, le=1.0)
    MOCK_FAULT_TOOLS: str = "get_policy_status,get_cancellation_quote,create_claim,cancel_policy"
    MOCK_LATENCY_MS: int = 0
    MOCK_SEED: int = 42
    MOCK_FAULT_CLEAR_AFTER: int | None = None
    MOCK_FAULT_LOOKUP: bool = False

    # Auth / interfaces
    REVIEWER_TOKEN: SecretStr = SecretStr("change-me")
    ADMIN_TOKEN: SecretStr = SecretStr("change-me")
    SUPPORT_API_URL: str = "http://127.0.0.1:8000"
    SUPPORT_PHONE: str = "1-800-555-0199"
    LANGSMITH_API_KEY: SecretStr | None = None

    # Storage and logging (not in the PRD's .env.example; see DECISIONS.md)
    APP_DB_PATH: Path = PROJECT_ROOT / "data" / "app.db"
    CHECKPOINT_DB_PATH: Path = PROJECT_ROOT / "data" / "checkpoints.db"
    SEED_DIR: Path = PROJECT_ROOT / "data" / "seed"
    LOG_LEVEL: str = "INFO"
    RETENTION_DAYS: int = 30

    @property
    def mock_fault_tools(self) -> frozenset[str]:
        return frozenset(t.strip() for t in self.MOCK_FAULT_TOOLS.split(",") if t.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # The test suite sets SUPPORT_NO_DOTENV=1 so a developer's `.env` can never leak into tests.
    if os.environ.get("SUPPORT_NO_DOTENV") == "1":
        return Settings(_env_file=None)
    return Settings()


def today() -> date:
    """The business "today" (A7): `APP_TODAY` if set, else the current UTC date."""
    configured = get_settings().APP_TODAY
    if configured is not None:
        return configured
    return datetime.now(UTC).date()


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return utcnow().isoformat()
