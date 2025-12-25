from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass


_load_dotenv()


def _get_env(
    name: str, *, default: str | None = None, required: bool = False
) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing env {name}")
    return value or None


@dataclass(frozen=True)
class Settings:
    github_repo_url: str | None
    github_token: str | None
    knova_agent_secret: str | None
    knova_hub_url: str | None
    database_url: str | None
    ai_model_provider: str
    ai_model_name: str
    ai_model_key_openai: str | None
    workdir: str = "/workspaces/workdir/repo"
    branch_name: str = "knova/mvp-test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        github_repo_url=_get_env("GITHUB_REPO_URL"),
        github_token=_get_env("GITHUB_TOKEN"),
        knova_agent_secret=_get_env("KNOVA_AGENT_SECRET"),
        knova_hub_url=_get_env("KNOVA_HUB_URL"),
        database_url=_get_env("DATABASE_URL"),
        ai_model_provider=_get_env("AI_MODEL_PROVIDER", default="openai") or "openai",
        ai_model_name=_get_env("AI_MODEL_NAME", default="gpt-4o-mini") or "gpt-4o-mini",
        ai_model_key_openai=_get_env("AI_MODEL_KEY_OPENAI"),
    )
