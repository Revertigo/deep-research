# Deep Research

A small multi-agent web research app built on the
[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/).
Ask a question; three agents plan the searches, run them in parallel and write
a concise report (bottom line first, 4-8 numbered sections, at most 1000 words,
sources at the end), which the app optionally emails you whole. You watch every
step live.

Grew out of the week-2 Deep Research lab from Ed Donner's
[AI Agents course](https://github.com/ed-donner/agents), with the Gradio UI
replaced by a FastAPI backend and a hand-built frontend.

## Features

- **Clarifying questions first (optional).** Before planning, a questioner
  agent may ask up to 3 short questions per round, for at most
  `MAX_CLARIFY_ROUNDS` rounds (2, in `models.py`). Answer, leave blank
  ("no preference"), or skip. The Q&A pairs go verbatim to the planner,
  every searcher and the writer; the writer uses them to set scope but never
  quotes them back ("you clarified...") in the report. The round cap is
  enforced by code, not by the model. Toggle in Settings (on by default).
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
  server never hides it. The mail is built in code from the same report the
  page shows (summary, full markdown converted to HTML, follow-up
  questions), so nothing is shortened or reworded by a model.
- **Report tools.** Pull-quote summary, rendered markdown, copy / download
  as `.md`, link to the OpenAI trace, and follow-up questions you can
  launch with one click.
- **Two orchestration modes.** Code (default) or LLM, picked per run in the
  Web searches card, so the same query can be run both ways and compared.
  See below.
- **Report in English or Hebrew.** A "Report language" control in the
  Delivery card (default English). It flips to Hebrew by itself when the
  query contains Hebrew letters; a click on the control wins for that query
  and auto-detect comes back with "New research". Hebrew reports render
  right-to-left on the page and in the email, with code kept left-to-right.
  Each model card carries a Hebrew-quality flag (see below).
- **Mobile layout** and a version tag in the header so you always know
  which build you're looking at.

## Two orchestration modes

| | Code (default) | LLM |
| --- | --- | --- |
| Who decides the calls | `research_manager.run_code`: planner → N searchers in parallel → writer, fixed | A manager agent (`make_manager`) with `plan_searches`, `search` and `write_report` as tools; it decides what to call and when |
| Agents and inputs | planner, searcher, writer with `render_context` | the same agents with the same inputs, wrapped as `@function_tool` closures (`make_manager_tools`) |
| Overhead | none | one manager turn per decision; `max_turns = searches + 6`. The run ends when `write_report` returns, so the manager never retypes the report |
| Variance | deterministic flow | the manager may reorder or skip searches; the tools cap it at N searcher runs and one writer run |

In both modes the questioner runs before the research (driven by the browser)
and the email is built and sent in code after the report is on screen. The manager's tool
calls are mapped onto the same SSE events, so the timeline looks the same; an
LLM run also shows a line like `4 tool calls, 3 manager turns` under the
timeline and tags the report with "LLM-orchestrated". A manager that stops
without calling `write_report` is reported as an error, not as a report.

To compare: run the same query with the same model and search count in each
mode, then compare the report, the elapsed time, the manager line and the two
traces (the Trace link on each report).

## Report language

`report_language` (`"en"` | `"he"`, default `"en"`) goes with every
`/api/clarify` and `/api/research` request; the server validates it with a
`Literal` and `/api/config` exposes the default. The Hebrew auto-detect is
client-side only (`app.js`, Unicode block `\u0590-\u05FF` in the query); the
server never guesses a language, it uses what the request says. What each
agent does with it:

| Agent | Language rule |
| --- | --- |
| Questioner | asks in the language the query was written in (not the report language) |
| Planner | writes search queries in English; for Israel-local topics (Israeli prices, regulations, companies, services, local news) it adds Hebrew queries where Hebrew sources are likely better. No UI control; the planner judges |
| Searcher | summaries always in English |
| Writer | report, summary and follow-up questions in the chosen language; technical terms, product/library/API names and code stay in English. Same directive in both orchestration modes (`make_writer(model, language)`) |

The `report` SSE event carries `report_language`, and the UI sets
`dir="rtl" lang="he"` on the summary, the report body and the follow-up
chips from it (never by sniffing the text). Headings, lists and tables align
right; `<pre>`/`<code>` stay LTR. The timeline and the report toolbar stay
LTR. The email does the same with `<div dir="rtl" lang="he">` and inline
styles (bullets and quote bars on the right, code `dir="ltr"`); the plain-text
part is the unchanged markdown. The subject is derived from the first H1
whatever the script.

**Hebrew quality per model.** Every entry in `models.py` has
`hebrew: "weak" | "fair" | "good"`: `gpt-5-nano` is weak, the other cheap
models (`gpt-6-luna`, `gpt-5.4-nano`, `gpt-5.6-luna`, `gpt-5-mini`) are fair,
`gpt-5.4-mini` and everything above is good. With Hebrew selected the model
cards show a "Weak Hebrew" (amber, warning icon) or "Fair Hebrew" (neutral,
info icon) badge, a weak selected model gets a one-line warning under the
grid and a tinted cost note on the Investigate button, and the cost estimate
scales the fixed output tokens by `HEBREW_OUTPUT_FACTOR` (2.2; Hebrew
tokenizes at roughly twice English). All of it disappears in English.

## How it works

```
query ──► Questioner ⟲ (0–2 rounds, /api/clarify) ──► Planner ──► N × Searcher (parallel, WebSearchTool) ──► Writer ──► email built in code (optional)
                                                        │                 │                                   │
                                                        └── SSE: stage ───┴── SSE: search_done ───────────────┴── SSE: report
```

| File | Role |
| --- | --- |
| `app.py` | FastAPI: `/` (UI), `/api/config`, `/api/clarify` (one questioner round, JSON), `/api/research` (SSE stream) |
| `research_manager.py` | Pipeline orchestration (`run_code`, `run_llm`); yields structured events; `build_email` (markdown → HTML, no model) |
| `research_agents.py` | Agent factories: questioner, planner, searcher, writer (+ language directive), manager (+ its tools); `render_context` |
| `models.py` | Model catalog, tiers, prices, Hebrew-quality flags, cost-estimate coefficients, clarify caps, languages |
| `messenger.py` | SMTP email and Pushover |
| `version.py` | `APP_VERSION`, shown in the UI and used to cache-bust assets |
| `static/` | `index.html`, `styles.css`, `app.js` (no build step) |
| `tests/` | `pytest tests` — mocked agents, no OpenAI calls |

Agents are created per run, so the chosen model and search count apply to
the whole pipeline. No agent touches the email: the recipient comes from the
request and the body from the writer's report, converted with the `markdown`
package (`fenced_code`, `tables`) and inline CSS so Gmail renders it.

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

Estimate per research = fixed tokens (planner + writer; the email costs no tokens) +
per-search tokens, at the model's list price, plus the web search tool fee
($10 per 1k calls). With `gpt-5.4-mini` and 5 searches that's about $0.08;
with `gpt-6.2-astra` about $0.36. Coefficients live in `models.py`; the fixed
output assumes the concise writer (report of at most 1000 words, about 1300
tokens, plus plan, summary and follow-ups: `EST_FIXED_OUTPUT = 2_000`).

## Versioning

Semantic versions in `version.py`: minor bump for a feature, patch bump for a
fix. The version is shown top-right and appended to asset URLs so browsers
never serve stale CSS/JS.
