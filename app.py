"""FastAPI shell around the Deep Research agents.

Routes:
  GET  /healthz        liveness, no auth
  GET  /               static UI
  GET  /api/config     model catalog, email defaults, orchestration modes
  POST /api/clarify    one round of clarifying questions (JSON)
  POST /api/research   Server-Sent Events stream of pipeline events

All routes except /healthz require HTTP Basic Auth (APP_USER / APP_PASSWORD).
"""

import base64
import binascii
import json
import os
import re
import secrets
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from messenger import DEFAULT_EMAIL_TO, email_configured
from models import (
    DEFAULT_MODEL,
    DEFAULT_SEARCHES,
    MAX_CLARIFY_ROUNDS,
    MAX_QUESTIONS_PER_ROUND,
    MAX_SEARCHES,
    MIN_SEARCHES,
    catalog,
    cost_model,
    is_allowed,
)
from research_manager import ResearchManager
from version import APP_VERSION

load_dotenv(override=True)

STATIC = Path(__file__).parent / "static"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_QUERY_CHARS = 500

app = FastAPI(title="Deep Research", version=APP_VERSION)

# --- Access gate: HTTP Basic Auth --------------------------------------------
# Every request except /healthz needs the password from APP_PASSWORD. Each run
# spends OpenAI credits and can email any address, so the app is private to
# whoever holds the password. Fail-closed: with APP_PASSWORD unset, nothing is
# served (503), so a misconfigured deploy can't become an open endpoint.
APP_USER = os.getenv("APP_USER", "research")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
PUBLIC_PATHS = {"/healthz"}


def _credentials_ok(header: str | None) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    return secrets.compare_digest(user, APP_USER) and secrets.compare_digest(password, APP_PASSWORD)


@app.middleware("http")
async def require_password(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    if not APP_PASSWORD:
        return JSONResponse({"detail": "APP_PASSWORD is not set on the server"}, status_code=503)
    if not _credentials_ok(request.headers.get("authorization")):
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Deep Research", charset="UTF-8"'},
            content="Authentication required",
        )
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": APP_VERSION}


class QAPair(BaseModel):
    q: str = Field(max_length=MAX_QUERY_CHARS)
    a: str = Field(max_length=MAX_QUERY_CHARS)


# Upper bound on pairs a client can send: every round fully answered.
MAX_QA_PAIRS = MAX_CLARIFY_ROUNDS * MAX_QUESTIONS_PER_ROUND


class ClarifyRequest(BaseModel):
    query: str = Field(min_length=3, max_length=MAX_QUERY_CHARS)
    qa: list[QAPair] = Field(default_factory=list, max_length=MAX_QA_PAIRS)
    model: str = DEFAULT_MODEL


class ResearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=MAX_QUERY_CHARS)
    model: str = DEFAULT_MODEL
    num_searches: int = Field(default=DEFAULT_SEARCHES, ge=MIN_SEARCHES, le=MAX_SEARCHES)
    email_enabled: bool = True
    email_to: str | None = None
    qa: list[QAPair] = Field(default_factory=list, max_length=MAX_QA_PAIRS)
    orchestration: Literal["code", "llm"] = "code"


@app.get("/")
async def index():
    # Inject the version into asset URLs so browsers never serve stale CSS/JS.
    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{v}}", APP_VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/config")
async def config():
    return {
        "version": APP_VERSION,
        "models": catalog(),
        "default_model": DEFAULT_MODEL,
        "searches": {"default": DEFAULT_SEARCHES, "min": MIN_SEARCHES, "max": MAX_SEARCHES},
        "cost_model": cost_model(),
        "email": {"configured": email_configured(), "default_to": DEFAULT_EMAIL_TO},
        "clarify": {"max_rounds": MAX_CLARIFY_ROUNDS, "max_questions": MAX_QUESTIONS_PER_ROUND},
        "orchestration": {"default": "code", "options": ["code", "llm"]},
    }


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/clarify")
async def clarify(req: ClarifyRequest):
    if not is_allowed(req.model):
        raise HTTPException(400, f"Unknown model: {req.model}")
    qa = [p.model_dump() for p in req.qa]
    try:
        topic = await ResearchManager(model=req.model, qa=qa).clarifying_input(req.query.strip())
    except Exception as exc:
        raise HTTPException(502, f"Questioner failed: {str(exc)[:300]}")
    # Clip to QAPair.q's limit so echoing a question back in qa never fails validation.
    questions = [q.strip()[:MAX_QUERY_CHARS] for q in topic.questions if q.strip()][:MAX_QUESTIONS_PER_ROUND]
    return {
        "needs_clarify": bool(topic.needs_clarify and questions),
        "questions": questions,
        "clarification": topic.clarification,
    }


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

    manager = ResearchManager(model=req.model, email_to=email_to, num_searches=req.num_searches, skip_reason=skip_reason,
                              qa=[p.model_dump() for p in req.qa], orchestration=req.orchestration)

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
