"""FastAPI wrapper around the LangGraph agent."""

import json
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import FileResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from agent import DB, MODEL, build_agent

HERE = Path(__file__).resolve().parent
app = FastAPI(title="DarGlobal property assistant")
_agent = None


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=2000)


class Ask(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default_factory=list, max_length=20)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "model": MODEL, "db": DB.exists()}


@app.get("/stats")
def stats() -> dict:
    """What the index actually holds, shown in the header."""
    import sqlite3
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        pages = db.execute("SELECT count(*) FROM docs").fetchone()[0]
        listings = db.execute("SELECT count(*) FROM listings").fetchone()[0]
    return {"pages": pages, "listings": listings}


@app.post("/chat")
def chat(ask: Ask) -> dict:
    global _agent
    if _agent is None:
        _agent = build_agent()

    msgs = [HumanMessage(t.content) if t.role == "user" else AIMessage(t.content)
            for t in ask.history[-10:]]
    msgs.append(HumanMessage(ask.message))

    try:
        out, served_by = _agent(msgs)
    except Exception as exc:                      # noqa: BLE001
        detail = str(exc)
        if "429" in detail or "rate" in detail.lower():
            return {"reply": "Every model provider is rate-limited at the "
                             "moment. Try again in a few seconds.",
                    "tools": [], "listings": [], "model": None}
        return {"reply": f"Upstream error: {detail[:300]}",
                "tools": [], "listings": [], "model": None}

    used = [c["name"] for m in out["messages"]
            for c in getattr(m, "tool_calls", []) or []]

    # search_listings answers in JSON, so the exact rows the model reasoned
    # over are handed to the UI as cards. Nothing is re-queried or re-parsed
    # out of the prose, so a card can never disagree with the answer.
    listings, seen = [], set()
    for m in out["messages"]:
        if getattr(m, "name", None) != "search_listings":
            continue
        try:
            rows = json.loads(m.content)
        except (json.JSONDecodeError, TypeError):
            continue
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("url") not in seen:
                seen.add(row["url"])
                listings.append(row)

    return {"reply": out["messages"][-1].content, "tools": used,
            "listings": listings[:12], "model": served_by}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
