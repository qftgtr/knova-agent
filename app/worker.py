from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .gitops import RepoConfig, ensure_repo

CONFIG_DIR = Path("/workspaces/knova-agent/.knova")
CONFIG_PATH = CONFIG_DIR / "config.json"
WORKDIR_ROOT = Path("/workspaces")


def _get_runner_id() -> str:
    name = os.getenv("CODESPACE_NAME")
    if not name:
        raise RuntimeError("Missing env CODESPACE_NAME")
    return f"codespace:{name}"


def _get_hub_url() -> str:
    url = os.getenv("KNOVA_HUB_URL")
    if not url:
        raise RuntimeError("Missing env KNOVA_HUB_URL")
    return url.rstrip("/")


def _load_cached_config() -> dict[str, str] | None:
    if not CONFIG_PATH.exists():
        return None

    try:
        data = json.loads(CONFIG_PATH.read_text())
    except Exception:
        return None

    repo = data.get("repo")
    secret = data.get("secret")
    if isinstance(repo, str) and isinstance(secret, str) and repo and secret:
        return {"repo": repo, "secret": secret}

    return None


def _save_config(repo: str, secret: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"repo": repo, "secret": secret}))


def _post_init(hub_url: str, runner: str) -> dict[str, str]:
    response = httpx.post(
        f"{hub_url}/workspace/init",
        json={"runner": runner},
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    if not payload.get("success") or "data" not in payload:
        raise RuntimeError("Workspace init failed")

    data = payload["data"]
    repo = data.get("repo")
    secret = data.get("secret")
    if not isinstance(repo, str) or not isinstance(secret, str):
        raise RuntimeError("Workspace init returned invalid data")

    return {"repo": repo, "secret": secret}


def _post_status(hub_url: str, runner: str, status: str, secret: str) -> None:
    response = httpx.post(
        f"{hub_url}/workspace/status",
        json={"runner": runner, "status": status},
        headers={"X-Knova-Secret": secret},
        timeout=30,
    )
    response.raise_for_status()


def _parse_repo_slug(repo_url: str) -> tuple[str, str]:
    try:
        parsed = urlparse(repo_url)
        if parsed.scheme and parsed.netloc:
            segments = [seg for seg in parsed.path.split("/") if seg]
            if len(segments) >= 2:
                return segments[0], segments[1].removesuffix(".git")
    except Exception:
        pass

    parts = [seg for seg in repo_url.split("/") if seg]
    if len(parts) >= 2:
        return parts[-2], parts[-1].removesuffix(".git")

    return ("repo", "repo")


def _build_workdir(repo_url: str) -> Path:
    org, repo = _parse_repo_slug(repo_url)
    return WORKDIR_ROOT / org / repo


def run() -> None:
    runner_id = _get_runner_id()
    hub_url = _get_hub_url()

    config = _load_cached_config()
    if config is None:
        config = _post_init(hub_url, runner_id)
        _save_config(config["repo"], config["secret"])

    repo_url = config["repo"]
    secret = config["secret"]

    workdir = _build_workdir(repo_url)
    branch_name = os.getenv("KNOVA_BRANCH_NAME", "knova/mvp-test")
    repo_auth_token = os.getenv("REPO_AUTH_TOKEN_GITHUB") or os.getenv("REPO_AUTH_TOKEN") or None

    ensure_repo(
        RepoConfig(
            repo_url=repo_url,
            workdir=str(workdir),
            branch_name=branch_name,
            repo_auth_token=repo_auth_token,
        )
    )

    _post_status(hub_url, runner_id, "ready", secret)

    while True:
        time.sleep(60)


if __name__ == "__main__":
    run()
