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


def _get_env(name: str, *, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing env {name}")
    return value or ""


@dataclass(frozen=True)
class Settings:
    github_repo_url: str
    github_token: str
    knova_agent_secret: str
    knova_hub_url: str
    database_url: str
    ai_model_provider: str
    ai_model_name: str
    ai_model_key_openai: str
    workdir: str = "/app/workdir/repo"
    branch_name: str = "knova/mvp-test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        github_repo_url=_get_env("GITHUB_REPO_URL", required=True),
        github_token=_get_env("GITHUB_TOKEN", required=True),
        knova_agent_secret=_get_env("KNOVA_AGENT_SECRET", required=True),
        knova_hub_url=_get_env("KNOVA_HUB_URL", required=True),
        database_url=_get_env("DATABASE_URL", required=True),
        ai_model_provider=_get_env("AI_MODEL_PROVIDER", default="openai"),
        ai_model_name=_get_env("AI_MODEL_NAME", default="gpt-4o-mini"),
        ai_model_key_openai=_get_env("AI_MODEL_KEY_OPENAI", required=True),
    )
