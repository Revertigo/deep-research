"""FastAPI shell around the Deep Research agents.

Routes:
  GET  /               static UI
  GET  /api/config     model catalog + email defaults
  POST /api/research   Server-Sent Events stream of pipeline events
"""

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from messenger import DEFAULT_EMAIL_TO, email_configured
from models import DEFAULT_MODEL, DEFAULT_SEARCHES, MAX_SEARCHES, MIN_SEARCHES, catalog, cost_model, is_allowed
from research_manager import ResearchManager
from version import APP_VERSION

load_dotenv(override=True)

STATIC = Path(__file__).parent / "static"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_QUERY_CHARS = 500

app = FastAPI(title="Deep Research", version=APP_VERSION)


class ResearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=MAX_QUERY_CHARS)
    model: str = DEFAULT_MODEL
    num_searches: int = Field(default=DEFAULT_SEARCHES, ge=MIN_SEARCHES, le=MAX_SEARCHES)
    email_enabled: bool = True
    email_to: str | None = None


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def config():
    return {
        "version": APP_VERSION,
        "models": catalog(),
        "default_model": DEFAULT_MODEL,
        "searches": {"default": DEFAULT_SEARCHES, "min": MIN_SEARCHES, "max": MAX_SEARCHES},
        "cost_model": cost_model(),
        "email": {"configured": email_configured(), "default_to": DEFAULT_EMAIL_TO},
    }


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/research")
async def research(req: ResearchRequest):
    if not is_allowed(req.model):
        raise HTTPException(400, f"Unknown model: {req.model}")

    # Email is best-effort. The report is always streamed to the screen; if the
    # server has no email config we skip delivery instead of rejecting the run.
    email_to: str | None = None
    skip_reason: str | None = None
    if req.email_enabled:
        if not email_configured():
            skip_reason = "not configured on server"
        else:
            email_to = (req.email_to or "").strip() or DEFAULT_EMAIL_TO
            if not EMAIL_RE.match(email_to):
                raise HTTPException(400, f"Invalid email address: {email_to}")

    manager = ResearchManager(model=req.model, email_to=email_to, num_searches=req.num_searches, skip_reason=skip_reason)

    async def stream():
        try:
            async for event in manager.run(req.query.strip()):
                yield sse(event)
        except Exception as exc:
            yield sse({"type": "error", "message": str(exc)[:500]})
        yield sse({"type": "end"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", 7860)), reload=True)
