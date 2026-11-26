# Deep Research

A self-hosted web app that turns one question into a short, sourced research
report. You type a query, pick an OpenAI model and how many web searches to
spend, and watch a pipeline of agents clarify the question, plan the searches,
run them in parallel and write the report, live on a timeline. The report
appears on screen first and can be emailed to you whole. It is a single
FastAPI process with a static frontend, built on the
[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/), and is
meant for one person or a small group behind a password. The pipeline grew
out of the Deep Research lab in Ed Donner's
[AI Agents course](https://github.com/ed-donner/agents).

## Features

- **Clarifying questions.** Before planning, a questioner agent asks up to 3
  short questions per round for at most 2 rounds, and only when an answer
  would change the search plan. Answer, leave a field blank ("no preference")
  or skip. The Q&A pairs reach the planner, every searcher and the writer.
  Toggle in the Delivery card, on by default.
- **Model choice with a live cost estimate.** Ten OpenAI models grouped by
  tier (cheap, balanced, strong, premium) with list prices. Each card and the
  Investigate button show an estimated cost per run that updates as you
  change the search count or the report language. Default: `gpt-5.4-mini`.
- **Configurable breadth.** A slider sets 1-12 parallel web searches
  (default 5). The cap is 12 because searches from a single plan grow
  redundant past about 8-10 while cost grows linearly.
- **Report language: English or Hebrew.** A segmented control in the Delivery
  card. It flips to Hebrew by itself when the query contains Hebrew letters;
  a click on the control wins for that query. Hebrew reports render
  right-to-left on the page and in the email, with code kept left-to-right.
- **Hebrew-quality badges.** Every model carries a `weak`, `fair` or `good`
  Hebrew grade. With Hebrew selected, weak and fair models show a badge, a
  weak selected model gets a warning line, and the cost estimate scales the
  writer's output tokens by 2.2.
- **Concise, dated, sourced reports.** A 2-4 sentence bottom line shown as a
  pull-quote, then up to 8 numbered sections (at most 1000 words, only as
  many sections as the evidence supports), then a deduplicated Sources list
  of titled, dated links the report used. The writer never quotes your
  clarification answers back and never infers a stage or outcome the pages
  do not state.
- **Requested formats.** When the query asks for a table, timeline, ranked
  list or comparison, that format is the core of the report, in its own
  section right after the title. For "status today" questions the planner
  adds a last-7-days news search and the writer a "Latest developments"
  section.
- **Email that mirrors the page.** On by default, recipient editable. The
  mail is built in code from the same report the page shows (pull-quote,
  full markdown as HTML with inline CSS, "Dig deeper" follow-ups), so
  nothing is shortened or reworded. Email is best-effort: the report is on
  screen before any send, and a missing mail server only marks the Deliver
  step as skipped.
- **Report tools.** Copy as markdown, download as `.md`, open the OpenAI
  trace, and launch any follow-up question with one click.
- **Basic Auth gate.** Every route except `/healthz` requires the user and
  password from `APP_USER` / `APP_PASSWORD`. With no password set the app
  serves nothing (503), so a misconfigured deploy cannot become an open
  endpoint that spends your credits.
- **iPhone-friendly UI.** Single-column layout under 760px, version tag in
  the header so you always know which build you are looking at.

## How it works

```
query ──► Questioner ⟲ (0-2 rounds, /api/clarify)
             │
             ▼  /api/research (SSE)
          Planner ──► N × Searcher (parallel, WebSearchTool) ──► Writer ──► report on screen ──► email (optional)
             │                 │                                    │
             └── stage: plan ──┴── search_done / search_failed ─────┴── report, stage: deliver
```

| Agent | Responsibility |
| --- | --- |
| Questioner | Decides whether a clarifying question would change the search plan; returns up to 3 questions in the language of the query, or `needs_clarify=false`. One call per round, driven by the browser through `/api/clarify`. |
| Planner | Produces exactly N complementary search queries with a reason each. Writes them in English and adds Hebrew queries for Israel-local topics; adds a last-7-days news search for "status today" questions. |
| Searcher | One instance per planned query, run in parallel with `WebSearchTool`. Returns a 2-3 paragraph English summary (under 300 words) with event dates and a `Source: title -- url -- date` line per page, dates absolute (it receives today's date). |
| Writer | Turns the summaries into `short_summary`, `markdown_report` and `follow_up_questions` in the chosen report language, with technical terms and code kept in English. |

Clarifications flow through `render_context(query, qa)`: the query plus the
Q&A pairs as one text block. It is the input to the questioner and the
planner, is prepended to every searcher's input as `Research context`, and
heads the writer's input. Agents are created per run, so the chosen model
applies end to end.

Progress streams over Server-Sent Events as JSON `data:` lines: `trace`
(trace URL, model, orchestration), `stage` (`plan`, `search`, `write`,
`deliver` with `running` / `done` / `skipped` / `failed`), `search_done` and
`search_failed` per search, `report` (summary, markdown, follow-ups,
`report_language`, elapsed seconds), `error`, and a final `end`. The UI maps
them onto a timeline: Clarify, Plan, Search (one card per planned search with
its reason, spinning while running, expanding to its summary), Write, Deliver.
The report renders with `marked` and `DOMPurify`; `dir="rtl" lang="he"` is
set from the `report_language` field, never by sniffing the text.

### Orchestration modes

By default the pipeline runs in code: planner, then N parallel searchers, then
writer. `ORCHESTRATION=llm` is an experimental mode in which a manager agent
drives the same agents as tools, with the same output and the same cap on
searches; the setting is listed in the configuration table.

## Project layout

| File | Role |
| --- | --- |
| `app.py` | FastAPI app: Basic Auth middleware, `/healthz`, `/` (UI), `/api/config`, `/api/clarify` (JSON), `/api/research` (SSE) |
| `research_manager.py` | `ResearchManager` (`run_code`, `run_llm`, per-agent calls, `deliver`); `build_email` (markdown to HTML with inline styles, RTL variant) |
| `research_agents.py` | Agent factories and prompts: questioner, planner, searcher, writer (with language directive), manager and its tools; `render_context`; output schemas |
| `models.py` | Model catalog with tiers, prices and Hebrew grades; search limits; clarify caps; cost coefficients; languages |
| `messenger.py` | SMTP email (STARTTLS, port 587) and a Pushover helper |
| `version.py` | `APP_VERSION`, shown in the UI and appended to asset URLs |
| `static/` | `index.html`, `styles.css`, `app.js`; no build step |
| `tests/` | pytest suite with mocked agents; no OpenAI calls |
| `render.yaml` | Render blueprint for a free web service |
| `.env.example` | Template for the environment variables below |

## Configuration

Variables are read from the environment or a `.env` file in the project root.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | yes | | Used by every agent and the web search tool |
| `APP_PASSWORD` | yes | | Basic Auth password; with it unset every route except `/healthz` answers 503 |
| `APP_USER` | no | `research` | Basic Auth user name |
| `EMAIL_ADDRESS` | for email | | Sender account (for example a Gmail address) |
| `EMAIL_SMTP_SERVER` | for email | | SMTP host, for example `smtp.gmail.com` |
| `EMAIL_APP_PASSWORD` | for email | | App password for the sender account |
| `DEFAULT_EMAIL_TO` | no | `EMAIL_ADDRESS` | Default recipient shown in the Delivery card |
| `ORCHESTRATION` | no | `code` | `code` runs the fixed pipeline; `llm` lets a manager agent drive the same agents as tools (experimental). Read once at startup; any other value stops the app |
| `HOST`, `PORT` | no | `127.0.0.1`, `7860` | Bind address of the dev server (`python app.py`) |
| `PUSHOVER_USER`, `PUSHOVER_TOKEN` | no | | Credentials for `messenger.push()`; no pipeline step calls it |

Without the three `EMAIL_*` values the app runs and the Deliver step shows
"Email skipped (not configured on server)". `/api/config` reports whether
email is configured and the default recipient.

Limits in `models.py` and `app.py`: query and each clarifying question or
answer up to 500 characters; 1-12 searches; 2 clarify rounds of 3 questions.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # set OPENAI_API_KEY and APP_PASSWORD at least
python app.py            # http://127.0.0.1:7860, auto-reload on
```

The browser prompts for the Basic Auth user and password on the first visit.

## Tests

```bash
python -m pytest -q
```

The suite mocks the agents' `Runner`, so it spends no OpenAI credits. It
covers the auth gate, request validation, clarify rounds and context passing,
the SSE event sequence in both `ORCHESTRATION` modes, the prompt rules the
report quality depends on, language handling and RTL, and the email builder.

## Deploy

`render.yaml` describes a free Render web service: build with
`pip install -r requirements.txt`, start with
`uvicorn app:app --host 0.0.0.0 --port $PORT`, health check on `/healthz`
(no auth, returns `{"ok": true, "version": ...}`). Set `APP_PASSWORD`,
`OPENAI_API_KEY` and the `EMAIL_*` / `DEFAULT_EMAIL_TO` values in the Render
dashboard; none are committed.

Every run spends your OpenAI credits and can email any address, so keep
`APP_PASSWORD` set and share it only with people you trust. On the free
plan the service spins down after about 15 minutes idle, so the first
request after a pause can take up to a minute to answer.

## Cost

The estimate shown per model and on the Investigate button is computed from
coefficients in `models.py` (served through `/api/config` so the UI can
recompute it live):

```
tokens_in  = 4_000 + 2_900 × searches
tokens_out = 2_000 × (2.2 if Hebrew else 1) + 400 × searches
cost       = (price_in × tokens_in + price_out × tokens_out) / 1e6 + 0.01 × searches
```

The fixed part is the planner plus the writer (a report of at most 1000 words
is about 1300 output tokens, plus plan, summary and follow-ups); the
per-search part is one searcher run and its summary fed to the writer; the
last term is the web search tool fee at $10 per 1k calls. Email costs no
tokens. Examples at 5 searches, English: `gpt-5-nano` about $0.05,
`gpt-5.4-mini` about $0.08, `gpt-6.2-astra` about $0.36; at 12 searches the
same models cost about $0.13, $0.18 and $0.70. Prices are USD per 1M tokens
from the OpenAI pricing page and are edited by hand in `models.py`.

## Versioning

`version.py` holds a semantic version that changes with every commit: minor
bump for a feature, patch bump for a fix or docs change. The version is shown
in the header and appended to the CSS and JS URLs, so browsers never serve a
stale build.
