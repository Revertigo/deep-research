# Deep Research

A small multi-agent web research app built on the
[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/).
Ask a question; four agents plan the searches, run them in parallel, write a
long-form report, and optionally email it. You watch every step live.

Grew out of the week-2 Deep Research lab from Ed Donner's
[AI Agents course](https://github.com/ed-donner/agents), with the Gradio UI
replaced by a FastAPI backend and a hand-built frontend.

## Features

- **Clarifying questions first (optional).** Before planning, a questioner
  agent may ask up to 3 short questions per round, for at most
  `MAX_CLARIFY_ROUNDS` rounds (2, in `models.py`). Answer, leave blank
  ("no preference"), or skip. The Q&A pairs go verbatim to the planner,
  every searcher and the writer. The round cap is enforced by code, not by the model. Toggle in
  Settings (on by default).
- **Live timeline.** Clarify → Plan → Search → Write → Deliver, streamed over
  Server-Sent Events. Each planned search appears as a card with its reason,
  spins while running, then expands to its summary.
- **Model picker.** Ten OpenAI models grouped by cost tier (cheap → premium)
  with list prices and a per-run cost estimate that updates as you change
  settings. Default: `gpt-5.4-mini`.
- **Configurable breadth.** 1–12 parallel web searches (default 5). The cap
  is deliberate: searches from one plan grow redundant past ~8, while cost
  grows linearly.
- **Email delivery, best-effort.** On by default, recipient editable; the
  report is always shown on screen first, so a failed or unconfigured mail
  server never hides it.
- **Report tools.** Pull-quote summary, rendered markdown, copy / download
  as `.md`, link to the OpenAI trace, and follow-up questions you can
  launch with one click.
- **Two orchestration modes.** Code (default) or LLM, picked per run in the
  Web searches card, so the same query can be run both ways and compared.
  See below.
- **Mobile layout** and a version tag in the header so you always know
  which build you're looking at.

## Two orchestration modes

| | Code (default) | LLM |
| --- | --- | --- |
| Who decides the calls | `research_manager.run_code`: planner → N searchers in parallel → writer, fixed | A manager agent (`make_manager`) with `plan_searches`, `search` and `write_report` as tools; it decides what to call and when |
| Agents and inputs | planner, searcher, writer with `render_context` | the same agents with the same inputs, wrapped as `@function_tool` closures (`make_manager_tools`) |
| Overhead | none | one manager turn per decision; `max_turns = searches + 6` |
| Variance | deterministic flow | the manager may reorder, skip or add searches |

In both modes the questioner runs before the research (driven by the browser)
and the emailer runs in code after the report is on screen. The manager's tool
calls are mapped onto the same SSE events, so the timeline looks the same; an
LLM run also shows a line like `3 tool calls, 4 manager turns` under the
timeline and tags the report with "LLM-orchestrated".

To compare: run the same query with the same model and search count in each
mode, then compare the report, the elapsed time, the manager line and the two
traces (the Trace link on each report).

## How it works

```
query ──► Questioner ⟲ (0–2 rounds, /api/clarify) ──► Planner ──► N × Searcher (parallel, WebSearchTool) ──► Writer ──► Emailer (optional)
                                                        │                 │                                   │
                                                        └── SSE: stage ───┴── SSE: search_done ───────────────┴── SSE: report
```

| File | Role |
| --- | --- |
| `app.py` | FastAPI: `/` (UI), `/api/config`, `/api/clarify` (one questioner round, JSON), `/api/research` (SSE stream) |
| `research_manager.py` | Pipeline orchestration (`run_code`, `run_llm`); yields structured events |
| `research_agents.py` | Agent factories: questioner, planner, searcher, writer, emailer, manager (+ its tools); `render_context` |
| `models.py` | Model catalog, tiers, prices, cost-estimate coefficients, clarify caps |
| `messenger.py` | SMTP email and Pushover |
| `version.py` | `APP_VERSION`, shown in the UI and used to cache-bust assets |
| `static/` | `index.html`, `styles.css`, `app.js` (no build step) |
| `tests/` | `pytest tests` — mocked agents, no OpenAI calls |

Agents are created per run, so the chosen model and search count apply to
the whole pipeline. The email recipient is bound into the tool, never chosen
by the model.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # fill in the values below
python app.py            # http://127.0.0.1:7860
```

### Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | yes | Used by every agent and the web search tool |
| `EMAIL_ADDRESS` | for email | Sender account (e.g. a Gmail address) |
| `EMAIL_SMTP_SERVER` | for email | e.g. `smtp.gmail.com` (port 587, STARTTLS) |
| `EMAIL_APP_PASSWORD` | for email | App password for the sender account |
| `DEFAULT_EMAIL_TO` | no | Default recipient; falls back to `EMAIL_ADDRESS` |
| `PUSHOVER_USER`, `PUSHOVER_TOKEN` | no | Push notifications |
| `HOST`, `PORT` | no | Dev server bind (default `127.0.0.1:7860`) |

Without the `EMAIL_*` values the app still works; the Deliver step shows
"Email skipped (not configured on server)".

## Deploy

`render.yaml` describes a free Render web service:
build `pip install -r requirements.txt`, start
`uvicorn app:app --host 0.0.0.0 --port $PORT`. Set the environment variables
in the Render dashboard; none are committed.

Note: anyone who can reach the deployed URL can spend your OpenAI credits
and send email to any address. Put it behind a password or an allowlist
before sharing the link.

## Cost

Estimate per research = fixed tokens (planner + writer + emailer) +
per-search tokens, at the model's list price, plus the web search tool fee
($10 per 1k calls). With `gpt-5.4-mini` and 5 searches that's about $0.10;
with `gpt-6.2-astra` about $0.55. Coefficients live in `models.py`.

## Versioning

Semantic versions in `version.py`: minor bump for a feature, patch bump for a
fix. The version is shown top-right and appended to asset URLs so browsers
never serve stale CSS/JS.
