from __future__ import annotations

import asyncio
from typing import Optional

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_knova_secret
from .gitops import ensure_repo
from .graph import build_graph, run_graph
from .settings import get_settings

app = FastAPI()
app.state.graph = None


class MessageFrom(BaseModel):
    id: int
    username: Optional[str] = None


class MessageBody(BaseModel):
    text: str
    sender: MessageFrom = Field(alias="from")

    model_config = ConfigDict(populate_by_name=True)


@app.on_event("startup")
def on_startup() -> None:
    settings = get_settings()
    try:
        ensure_repo(settings)
    except Exception as exc:
        print(f"Repo setup failed: {exc}")
    app.state.graph = build_graph(settings)


def _join_url(base: str, path: str) -> str:
    return base.rstrip("/") + path


async def _send_reply(text: str) -> None:
    settings = get_settings()
    url = _join_url(settings.knova_hub_url, "/reply")
    headers = {"X-Knova-Secret": settings.knova_agent_secret}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json={"text": text}, headers=headers)
        response.raise_for_status()


async def _process_message(body: MessageBody) -> None:
    settings = get_settings()
    graph = app.state.graph
    if graph is None:
        graph = build_graph(settings)
        app.state.graph = graph

    reply = await asyncio.to_thread(run_graph, graph, body.text, str(body.sender.id))
    if not reply:
        reply = "(empty response)"

    await _send_reply(reply)


@app.post("/message")
async def message(
    body: MessageBody,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_knova_secret),
) -> dict[str, bool]:
    background_tasks.add_task(_process_message, body)
    return {"success": True}


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}
