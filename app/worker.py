from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

import httpx
import websocket

from .gitops import RepoConfig, ensure_repo
from .graph import build_graph, run_graph
from .settings import get_settings

CONFIG_DIR = Path("/workspaces/knova-agent/.knova")
CONFIG_PATH = CONFIG_DIR / "config.json"
WORKDIR_ROOT = Path("/workspaces")
LOG_DIR = CONFIG_DIR / "command_logs"
LOG_INDEX_PATH = LOG_DIR / "index.json"

logger = logging.getLogger("knova-agent")

HEARTBEAT_INTERVAL_SEC = 5
RECONNECT_DELAY_SEC = 5
MAX_LOGGED_COMMANDS = 3

DEFAULT_MESSAGE_ID = "main"
CODEX_TIMEOUT_SEC = 5 * 60
AGENT_REPO_DIR = Path("/workspaces/knova-agent")


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


def _build_ws_url(hub_url: str, runner: str) -> str:
    if hub_url.startswith("https://"):
        base = f"wss://{hub_url[len('https://'):]}"
    elif hub_url.startswith("http://"):
        base = f"ws://{hub_url[len('http://'):]}"
    else:
        raise RuntimeError("KNOVA_HUB_URL must start with http:// or https://")
    return f"{base.rstrip('/')}/workspace/ws?runner={runner}"


def _get_openai_api_key() -> str | None:
    return os.getenv("OPENAI_API_KEY")


def _build_codex_prompt(question: str) -> str:
    return "\n".join(
        [
            "You are a coding assistant running inside a GitHub Codespace.",
            "Answer based on the local repository files in the current workspace.",
            "When relevant, cite file paths and line numbers (e.g. src/foo.ts:42).",
            "Be concise and practical.",
            "输出结果会通过telegram bot api，使用parse_mode=HTML模式发送出去。使用支持的HTML语法进行格式化。",
            "",
            f"Question: {question}",
        ]
    )


def _run_git(command_id: str, args: list[str]) -> str:
    _append_debug_log(command_id, f"[deploy] git {' '.join(args)}")
    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        _append_debug_log(command_id, f"[deploy] git stdout:\\n{result.stdout.strip()}")
    if result.stderr:
        _append_debug_log(command_id, f"[deploy] git stderr:\\n{result.stderr.strip()}")
    if result.returncode != 0:
        stderr_line = (result.stderr or "").strip().splitlines()[:1]
        suffix = f": {stderr_line[0]}" if stderr_line else ""
        raise RuntimeError(f"git command failed (exit {result.returncode}){suffix}")
    return (result.stdout or "").strip()


def _restart_self(command_id: str) -> None:
    _append_debug_log(command_id, "[deploy] restarting worker process")
    time.sleep(1)
    os.execv(sys.executable, [sys.executable, "-m", "app.worker"])


def _run_codex_exec(command_id: str, repo_dir: Path, question: str) -> str:
    codex_path = shutil.which("codex")
    if not codex_path:
        raise RuntimeError("codex CLI not found in PATH")

    api_key = _get_openai_api_key()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY for codex")

    out_path = CONFIG_DIR / f"codex_last_message_{_safe_log_name(command_id)}.txt"
    prompt = _build_codex_prompt(question)

    env = os.environ.copy()
    env.setdefault("OPENAI_API_KEY", api_key)

    cmd = [
        codex_path,
        "--config",
        "preferred_auth_method=\"apikey\"",
        "exec",
        prompt,
        "--full-auto",
        "--cd",
        str(repo_dir),
        "--output-last-message",
        str(out_path),
    ]

    _append_debug_log(
        command_id,
        "\n".join(
            [
                f"[codex] repo_dir={repo_dir}",
                f"[codex] OPENAI_API_KEY set={bool(env.get('OPENAI_API_KEY'))}",
                f"[codex] cmd={' '.join(cmd)}",
            ]
        ),
    )

    result = subprocess.run(
        cmd,
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=CODEX_TIMEOUT_SEC,
    )

    _append_debug_log(command_id, f"[codex] exit_code={result.returncode}")
    if result.stderr:
        _append_debug_log(command_id, f"[codex] stderr:\\n{result.stderr.strip()}")
    if result.stdout:
        _append_debug_log(command_id, f"[codex] stdout:\\n{result.stdout.strip()}")

    if result.returncode != 0:
        stderr_first_line = (result.stderr or "").strip().splitlines()[:1]
        suffix = f": {stderr_first_line[0]}" if stderr_first_line else ""
        raise RuntimeError(f"codex exec failed (exit {result.returncode}){suffix}")

    try:
        return out_path.read_text(encoding="utf-8").strip()
    except Exception:
        return (result.stdout or "").strip()


def _safe_log_name(command_id: str) -> str:
    return command_id.replace(":", "_")


def _append_debug_log(command_id: str, text: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = CONFIG_DIR / f"command_{_safe_log_name(command_id)}.debug.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
    except Exception:
        pass
    try:
        logger.info("%s", text)
    except Exception:
        pass


class CommandLogStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._index = self._load_index()

    def _load_index(self) -> list[str]:
        if not LOG_INDEX_PATH.exists():
            return []
        try:
            data = json.loads(LOG_INDEX_PATH.read_text())
        except Exception:
            return []
        if isinstance(data, list) and all(isinstance(item, str) for item in data):
            return data
        return []

    def _save_index(self) -> None:
        LOG_INDEX_PATH.write_text(json.dumps(self._index))

    def _log_path(self, command_id: str) -> Path:
        return LOG_DIR / f"{_safe_log_name(command_id)}.jsonl"

    def register_command(self, command_id: str) -> None:
        with self._lock:
            if command_id not in self._index:
                self._index.append(command_id)
            while len(self._index) > MAX_LOGGED_COMMANDS:
                expired = self._index.pop(0)
                try:
                    self._log_path(expired).unlink(missing_ok=True)
                except Exception:
                    pass
            self._save_index()

    def has_command(self, command_id: str) -> bool:
        with self._lock:
            return command_id in self._index and self._log_path(command_id).exists()

    def append_event(self, command_id: str, event: dict[str, object]) -> None:
        with self._lock:
            path = self._log_path(command_id)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event))
                handle.write("\n")

    def read_events_since(self, command_id: str, since: int) -> list[dict[str, object]]:
        path = self._log_path(command_id)
        if not path.exists():
            return []

        events: list[dict[str, object]] = []
        with self._lock:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(event, dict):
                        continue
                    seq = event.get("seq")
                    if isinstance(seq, int) and seq > since:
                        events.append(event)

        return events


class WsSender:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ws: websocket.WebSocketApp | None = None
        self._connected = False

    def set_ws(self, ws: websocket.WebSocketApp) -> None:
        with self._lock:
            self._ws = ws
            self._connected = True

    def clear_ws(self) -> None:
        with self._lock:
            self._ws = None
            self._connected = False

    def send(self, payload: dict[str, object]) -> bool:
        raw = json.dumps(payload)
        with self._lock:
            if not self._connected or self._ws is None:
                return False
            try:
                self._ws.send(raw)
                return True
            except Exception:
                return False


class CommandExecutor:
    def __init__(self, sender: WsSender, log_store: CommandLogStore) -> None:
        self._sender = sender
        self._log_store = log_store
        self._queue: queue.Queue[dict[str, object]] = queue.Queue()
        self._current_command_id: str | None = None
        self._pending_commands: set[str] = set()
        self._seq_by_command: dict[str, int] = {}
        self._state_lock = threading.Lock()
        self._graph = build_graph(get_settings())
        self._repo_dir: Path | None = None

        worker = threading.Thread(target=self._run, daemon=True)
        worker.start()

    def set_repo_dir(self, repo_dir: Path) -> None:
        self._repo_dir = repo_dir

    def enqueue_command(self, command_id: str, payload: dict[str, object]) -> None:
        with self._state_lock:
            if self._current_command_id == command_id:
                return
            if self._current_command_id is not None:
                return
            if command_id in self._pending_commands:
                return

        if self._log_store.has_command(command_id):
            return

        _append_debug_log(command_id, "[queue] accepted")
        with self._state_lock:
            self._pending_commands.add(command_id)
        self._queue.put({"command_id": command_id, "payload": payload})

    def enqueue_deploy(self, command_id: str) -> None:
        self.enqueue_command(command_id, {"type": "deploy"})

    def _next_seq(self, command_id: str) -> int:
        value = self._seq_by_command.get(command_id, 0)
        self._seq_by_command[command_id] = value + 1
        return value

    def _send_event(
        self,
        command_id: str,
        message_id: str,
        op: str,
        data: dict[str, object] | None = None,
    ) -> None:
        seq = self._next_seq(command_id)
        event: dict[str, object] = {
            "type": "event",
            "command_id": command_id,
            "message_id": message_id,
            "seq": seq,
            "op": op,
            "data": data or {},
        }
        self._log_store.append_event(command_id, event)
        self._sender.send(event)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            command_id = item.get("command_id")
            payload = item.get("payload")
            if not isinstance(command_id, str) or not isinstance(payload, dict):
                continue
            payload_type = payload.get("type")
            text = payload.get("text")
            if payload_type == "deploy":
                text = ""
            elif not isinstance(text, str):
                continue

            with self._state_lock:
                self._current_command_id = command_id

            self._seq_by_command[command_id] = 0
            self._log_store.register_command(command_id)

            _append_debug_log(
                command_id,
                f"[run] starting type={payload_type or 'command'} text_len={len(text)} repo_dir={self._repo_dir}",
            )
            self._send_event(command_id, DEFAULT_MESSAGE_ID, "set_status", {"status": "STARTED"})

            try:
                if payload_type == "deploy":
                    self._send_event(
                        command_id,
                        DEFAULT_MESSAGE_ID,
                        "set_text",
                        {"text": "Updating knova-agent..."},
                    )
                    _run_git(command_id, ["git", "-C", str(AGENT_REPO_DIR), "fetch", "--all", "--prune"])
                    _run_git(command_id, ["git", "-C", str(AGENT_REPO_DIR), "pull", "--ff-only"])
                    self._send_event(
                        command_id,
                        DEFAULT_MESSAGE_ID,
                        "set_text",
                        {"text": "Update complete. Restarting agent..."},
                    )
                    self._send_event(command_id, DEFAULT_MESSAGE_ID, "terminal", {"result": "success"})
                    _restart_self(command_id)
                    continue

                repo_dir = self._repo_dir
                if repo_dir is not None:
                    try:
                        reply = _run_codex_exec(command_id, repo_dir, text)
                    except Exception as exc:
                        _append_debug_log(command_id, f"[codex] failed: {exc}\\n{traceback.format_exc()}")
                        reply = ""
                    if not reply:
                        _append_debug_log(command_id, "[run_graph] fallback")
                        try:
                            reply = run_graph(self._graph, text, command_id)
                        except Exception as exc:
                            _append_debug_log(command_id, f"[run_graph] failed: {exc}\\n{traceback.format_exc()}")
                            raise
                else:
                    reply = run_graph(self._graph, text, command_id)
                self._send_event(command_id, DEFAULT_MESSAGE_ID, "set_text", {"text": reply})
                self._send_event(command_id, DEFAULT_MESSAGE_ID, "terminal", {"result": "success"})
            except Exception as exc:
                _append_debug_log(command_id, f"[executor] failed: {exc}\\n{traceback.format_exc()}")
                self._send_event(command_id, DEFAULT_MESSAGE_ID, "set_text", {"text": "Task failed."})
                self._send_event(command_id, DEFAULT_MESSAGE_ID, "terminal", {"result": "failed"})
            finally:
                _append_debug_log(command_id, "[run] finished")
                with self._state_lock:
                    self._current_command_id = None
                    self._pending_commands.discard(command_id)
                self._seq_by_command.pop(command_id, None)


def _heartbeat_loop(sender: WsSender, stop_event: threading.Event) -> None:
    while not stop_event.wait(HEARTBEAT_INTERVAL_SEC):
        sender.send({"type": "ping", "ts": int(time.time() * 1000)})


def _resync_command(
    sender: WsSender,
    log_store: CommandLogStore,
    command_id: str,
    since: int,
) -> None:
    if not log_store.has_command(command_id):
        sender.send({"type": "not_found", "command_id": command_id})
        return

    events = log_store.read_events_since(command_id, since)
    for event in events:
        sender.send(event)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    runner_id = _get_runner_id()
    hub_url = _get_hub_url()
    logger.info(
        "[startup] runner=%s hub=%s OPENAI_API_KEY set=%s",
        runner_id,
        hub_url,
        bool(os.getenv("OPENAI_API_KEY")),
    )

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

    sender = WsSender()
    log_store = CommandLogStore()
    executor = CommandExecutor(sender, log_store)
    executor.set_repo_dir(workdir)

    ws_url = _build_ws_url(hub_url, runner_id)
    headers = [f"X-Knova-Secret: {secret}"]

    while True:
        stop_event = threading.Event()

        def on_open(ws_app: websocket.WebSocketApp) -> None:
            sender.set_ws(ws_app)
            threading.Thread(
                target=_heartbeat_loop, args=(sender, stop_event), daemon=True
            ).start()

        def on_message(ws_app: websocket.WebSocketApp, message: str) -> None:
            if isinstance(message, (bytes, bytearray)):
                message = message.decode("utf-8", errors="ignore")
            try:
                payload = json.loads(message)
            except Exception:
                return
            if not isinstance(payload, dict):
                return

            msg_type = payload.get("type")
            if msg_type == "command":
                command_id = payload.get("command_id")
                cmd_payload = payload.get("payload")
                if isinstance(command_id, str) and isinstance(cmd_payload, dict):
                    executor.enqueue_command(command_id, cmd_payload)
                return

            if msg_type == "deploy":
                command_id = payload.get("command_id")
                if isinstance(command_id, str):
                    executor.enqueue_deploy(command_id)
                return

            if msg_type == "resync":
                command_id = payload.get("command_id")
                since = payload.get("since")
                if isinstance(command_id, str) and isinstance(since, int):
                    _resync_command(sender, log_store, command_id, since)

        def on_error(ws_app: websocket.WebSocketApp, error: object) -> None:
            sender.clear_ws()
            stop_event.set()

        def on_close(ws_app: websocket.WebSocketApp, status: int, msg: str) -> None:
            sender.clear_ws()
            stop_event.set()

        ws_app = websocket.WebSocketApp(
            ws_url,
            header=headers,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        try:
            ws_app.run_forever()
        except Exception:
            pass
        stop_event.set()
        time.sleep(RECONNECT_DELAY_SEC)


if __name__ == "__main__":
    run()
