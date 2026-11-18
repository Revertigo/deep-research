# Deep Research

Multi-agent web research (OpenAI Agents SDK): plan searches, run them in
parallel, write a report, optionally email it. FastAPI backend streams the
pipeline as Server-Sent Events; the frontend is plain HTML/CSS/JS.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in OPENAI_API_KEY and the EMAIL_* values
python app.py          # http://127.0.0.1:7860
```

## Deploy (Render)

Web service from this repo. Build: `pip install -r requirements.txt`.
Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`. Set the `.env` keys as
environment variables. `render.yaml` carries the same settings.

## Layout

| File | Role |
| --- | --- |
| `app.py` | FastAPI routes: `/`, `/api/config`, `/api/research` (SSE) |
| `research_manager.py` | Pipeline: plan → search → write → deliver, yields events |
| `research_agents.py` | Agent factories (planner, searcher, writer, emailer) |
| `models.py` | Model catalog shown in the UI, with per-run cost estimates |
| `messenger.py` | SMTP email + Pushover |
| `static/` | UI |
