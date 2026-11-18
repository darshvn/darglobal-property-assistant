"""FastAPI wrapper around the LangGraph agent."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from agent import DB, MODEL, build_agent

HERE = Path(__file__).resolve().parent
app = FastAPI(title="DarGlobal property assistant")
_agent = None


class Turn(BaseModel):
    role: str
    content: str


class Ask(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default_factory=list, max_length=20)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "model": MODEL, "db": DB.exists()}


@app.post("/chat")
def chat(ask: Ask) -> dict:
    global _agent
    if _agent is None:
        _agent = build_agent()

    msgs = [HumanMessage(t.content) if t.role == "user" else AIMessage(t.content)
            for t in ask.history[-10:]]
    msgs.append(HumanMessage(ask.message))

    try:
        out = _agent.invoke({"messages": msgs}, {"recursion_limit": 12})
    except Exception as exc:                      # noqa: BLE001
        # Free OpenRouter models rate-limit hard; surface that instead of a 500.
        detail = str(exc)
        if "429" in detail or "rate" in detail.lower():
            return {"reply": "The free model is rate-limited right now. "
                              "Wait a few seconds and try again.", "tools": []}
        return {"reply": f"Upstream error: {detail[:300]}", "tools": []}

    used = [c["name"] for m in out["messages"]
            for c in getattr(m, "tool_calls", []) or []]
    return {"reply": out["messages"][-1].content, "tools": used}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
