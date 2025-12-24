from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlparse

from .settings import Settings


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, check=True, cwd=cwd)


def _with_token(repo_url: str, token: str) -> str:
    parsed = urlparse(repo_url)
    if parsed.scheme != "https":
        raise RuntimeError("GITHUB_REPO_URL must use https")

    netloc = f"x-access-token:{token}@{parsed.netloc}"
    return parsed._replace(netloc=netloc).geturl()


def _git_branch_exists(repo_dir: Path, branch_name: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--verify", branch_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def ensure_repo(settings: Settings) -> None:
    repo_dir = Path(settings.workdir)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    if not repo_dir.exists():
        clone_url = _with_token(settings.github_repo_url, settings.github_token)
        _run(["git", "clone", clone_url, str(repo_dir)])
    elif not (repo_dir / ".git").exists():
        raise RuntimeError(f"Workdir {repo_dir} exists but is not a git repo")

    branch_name = settings.branch_name
    if _git_branch_exists(repo_dir, branch_name):
        _run(["git", "-C", str(repo_dir), "checkout", branch_name])
    else:
        _run(["git", "-C", str(repo_dir), "checkout", "-b", branch_name])
