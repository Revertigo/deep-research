"""Report language (en | he): request validation, agent directives, RTL mail, config.

No OpenAI calls: Runner.run is a fake that records the agent it was given, so the
tests read the writer's instructions off the recorded Agent objects.
"""

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import set_tracing_disabled  # noqa: E402
from agents.tool_context import ToolContext  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import research_manager  # noqa: E402
from models import MODELS  # noqa: E402
from research_agents import (  # noqa: E402
    PLANNER_INSTRUCTIONS,
    QUESTIONER_INSTRUCTIONS,
    SEARCH_INSTRUCTIONS,
    ClarifyingTopic,
    ReportData,
    WebSearchItem,
    WebSearchPlan,
    make_manager_tools,
    make_writer,
)

PASSWORD = "test-pass"
HEBREW_DIRECTIVE = "in Hebrew"
HEBREW_MD = "# מחירי רכב חשמלי בישראל\n\nטקסט.\n\n```python\nx = 1\n```\n\n- פריט\n"
HEBREW_REPORT = ReportData(short_summary="סיכום קצר.", markdown_report=HEBREW_MD, follow_up_questions=["שאלה?"])


def auth_header() -> dict:
    token = base64.b64encode(f"research:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def calls(monkeypatch):
    """Fake Runner.run; records the Agent object and its input for every call."""
    set_tracing_disabled(True)
    monkeypatch.setattr(app_module, "APP_PASSWORD", PASSWORD)
    monkeypatch.setattr(app_module, "APP_USER", "research")
    recorded: list[tuple[object, str]] = []

    async def fake_run(agent, input, *args, **kwargs):
        recorded.append((agent, input))
        if agent.name == "Questioner Agent":
            out = ClarifyingTopic(needs_clarify=False, questions=[], clarification="c")
        elif agent.name == "Planner Agent":
            out = WebSearchPlan(searches=[WebSearchItem(reason="r", query="q1")])
        elif agent.name == "Search Agent":
            out = "summary"
        elif agent.name == "Writer Agent":
            out = ReportData(short_summary="s", markdown_report="# r", follow_up_questions=[])
        else:
            raise AssertionError(f"unexpected agent {agent.name}")
        return SimpleNamespace(final_output=out)

    monkeypatch.setattr(research_manager.Runner, "run", fake_run)
    return recorded


@pytest.fixture
def client():
    return TestClient(app_module.app)


def writer(calls):
    return next(agent for agent, _ in calls if agent.name == "Writer Agent")


def research(client, **extra):
    body = {"query": "EV adoption", "model": "gpt-5.4-mini", "num_searches": 1, "email_enabled": False, **extra}
    r = client.post("/api/research", headers=auth_header(), json=body)
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    return r, events


# --- API -----------------------------------------------------------------------

def test_config_exposes_language_default_and_model_flags(calls, client):
    cfg = client.get("/api/config", headers=auth_header()).json()
    assert cfg["report_language"] == {"default": "en", "options": ["en", "he"]}
    assert cfg["cost_model"]["hebrew_output_factor"] == 2.2
    flags = {m["id"]: m["hebrew"] for m in cfg["models"]}
    assert set(flags.values()) <= {"weak", "fair", "good"}
    assert flags["gpt-5-nano"] == "weak"
    assert flags["gpt-6-luna"] == "fair" and flags["gpt-5.6-luna"] == "fair"
    assert flags["gpt-5.4-mini"] == "good"
    # every model from gpt-5.4-mini up is "good"
    ids = [m["id"] for m in MODELS]
    assert all(flags[i] == "good" for i in ids[ids.index("gpt-5.4-mini"):])


def test_invalid_language_rejected_on_both_routes(calls, client):
    r = client.post("/api/clarify", headers=auth_header(), json={"query": "Explain X", "report_language": "fr"})
    assert r.status_code == 422
    r, _ = research(client, report_language="fr")
    assert r.status_code == 422
    assert calls == []


def test_default_language_is_english(calls, client):
    r, events = research(client)
    assert r.status_code == 200
    report = next(e for e in events if e["type"] == "report")
    assert report["report_language"] == "en"
    instructions = writer(calls).instructions
    assert "in English" in instructions and HEBREW_DIRECTIVE not in instructions


def test_hebrew_report_in_code_mode(calls, client):
    r, events = research(client, report_language="he")
    assert r.status_code == 200
    assert not [e for e in events if e["type"] == "error"], events
    report = next(e for e in events if e["type"] == "report")
    assert report["report_language"] == "he"
    instructions = writer(calls).instructions
    assert HEBREW_DIRECTIVE in instructions
    assert "Keep technical terms, product names, library/API names and code in English." in instructions
    # Planner and searcher inputs are unchanged by the language choice.
    inputs = {agent.name: sent for agent, sent in calls}
    assert inputs["Planner Agent"] == "Query: EV adoption"
    assert inputs["Search Agent"] == "Research context:\nQuery: EV adoption\n\nSearch term: q1\nReason for searching: r"


def test_clarify_accepts_language_and_questioner_asks_in_query_language(calls, client):
    r = client.post("/api/clarify", headers=auth_header(),
                    json={"query": "מה מחיר רכב חשמלי בישראל", "model": "gpt-5.4-mini", "report_language": "he"})
    assert r.status_code == 200
    questioner = calls[0][0]
    assert questioner.name == "Questioner Agent"
    assert "same language the user wrote the query in" in questioner.instructions
    assert "same language the user wrote the query in" in QUESTIONER_INSTRUCTIONS


# --- Agents --------------------------------------------------------------------

@pytest.mark.parametrize("language, expect", [("he", "in Hebrew"), ("en", "in English")])
def test_make_writer_language_directive(language, expect):
    instructions = make_writer("gpt-5.4-mini", language).instructions
    assert f"Write the report, the short summary and the follow-up questions {expect}." in instructions
    assert ("in Hebrew" in instructions) == (language == "he")
    # "Keep ... in English" only makes sense for a non-English report.
    assert ("Keep technical terms" in instructions) == (language == "he")
    assert instructions.endswith("\n") and "\n\n\n" not in instructions


def test_run_llm_report_event_carries_language(calls, monkeypatch):
    from test_llm_orchestration import REPORT, collect_sync, fake_streamed, happy_script

    for language in ("he", "en"):
        fake_streamed(monkeypatch, happy_script, REPORT.model_dump_json(), turns=3)
        rm = research_manager.ResearchManager(model="gpt-5.4-mini", num_searches=2, orchestration="llm",
                                              report_language=language)
        events = collect_sync(rm.run("EV adoption"))
        report = next(e for e in events if e["type"] == "report")
        assert report["report_language"] == language


# --- Cost: the Hebrew factor is applied client-side ---------------------------

def test_hebrew_cost_factor_exposed_and_used_by_client():
    """The server's est_cost is the English figure; app.js scales fixed output by hebrew_output_factor."""
    from models import HEBREW_OUTPUT_FACTOR, cost_model

    assert cost_model()["hebrew_output_factor"] == HEBREW_OUTPUT_FACTOR == 2.2
    app_js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    estimate_fn = app_js[app_js.index("function estimate("):app_js.index("function updateCosts(")]
    assert "hebrew_output_factor" in estimate_fn and 'state.reportLanguage === "he"' in estimate_fn
    assert "fixed_output * factor" in estimate_fn
    assert app_js.count("Hebrew uses ~2x the tokens") == 2  # go-cost tooltip + model legend, Hebrew only
    assert 'he ? "Hebrew uses ~2x the tokens" : ""' in app_js


def test_write_report_tool_uses_same_language_as_code_path(calls):
    for language in ("he", "en"):
        calls.clear()
        tools = {t.name: t for t in make_manager_tools("gpt-5.4-mini", "Query: x", 1, research_manager.Runner, language)}
        args = json.dumps({"search_summaries": ["summary"]})
        ctx = ToolContext(context=None, tool_name="write_report", tool_call_id="w", tool_arguments=args)
        research_manager.asyncio.new_event_loop().run_until_complete(tools["write_report"].on_invoke_tool(ctx, args))
        assert writer(calls).instructions == make_writer("gpt-5.4-mini", language).instructions
        assert (HEBREW_DIRECTIVE in writer(calls).instructions) == (language == "he")


def test_planner_and_searcher_language_rules():
    assert "Write search queries in English." in PLANNER_INSTRUCTIONS
    assert "local to Israel" in PLANNER_INSTRUCTIONS and "Hebrew-language queries" in PLANNER_INSTRUCTIONS
    assert "Write the summary in English" in SEARCH_INSTRUCTIONS


# --- Email ---------------------------------------------------------------------

def test_build_email_rtl_for_hebrew():
    subject, text_body, html_body = research_manager.build_email(HEBREW_REPORT, "he")
    assert subject == "Deep Research: מחירי רכב חשמלי בישראל"
    assert '<div dir="rtl" lang="he"' in html_body
    assert '<pre dir="ltr"' in html_body and '<code dir="ltr"' in html_body
    assert "padding-right:24px" in html_body  # list bullets on the right
    assert "border-right:4px" in html_body  # pull-quote bar on the right
    assert "סיכום קצר." in html_body and "שאלה?" in html_body
    # text body is the same markdown, untouched
    assert HEBREW_MD in text_body and "dir=" not in text_body


def test_build_email_ltr_for_english():
    _, _, html_body = research_manager.build_email(HEBREW_REPORT, "en")
    assert 'dir="rtl"' not in html_body and 'lang="he"' not in html_body and 'dir="ltr"' not in html_body
    assert "padding-left:24px" in html_body
    # default argument stays English
    assert research_manager.build_email(HEBREW_REPORT)[2] == html_body


def test_send_email_uses_manager_language(monkeypatch):
    recorded = []
    monkeypatch.setattr(research_manager.messenger, "send_email", lambda s, t, h, to=None: recorded.append(h))
    rm = research_manager.ResearchManager(model="gpt-5.4-mini", email_to="a@b.co", report_language="he")
    research_manager.asyncio.new_event_loop().run_until_complete(rm.send_email(HEBREW_REPORT))
    assert '<div dir="rtl" lang="he"' in recorded[0]


def test_bad_language_rejected_in_manager():
    with pytest.raises(ValueError):
        research_manager.ResearchManager(model="gpt-5.4-mini", report_language="fr")


# --- Concise writer (v1.4.2): bottom line first, 1000-word ceiling, no echo of the Q&A ---

def test_writer_prompt_is_concise_with_bottom_line():
    instructions = make_writer("gpt-5.4-mini").instructions
    assert "at most 1000 words" in instructions
    assert "short_summary is the bottom line" in instructions
    assert "Sources" in instructions
    assert "at least 1000" not in instructions
    assert "5-10 pages" not in instructions
    assert "lengthy" not in instructions


# --- v1.4.3: no duplicate bottom line, linked sources, clean lists, plain wording ---

def test_writer_prompt_has_no_bottom_line_section_in_report():
    """short_summary is rendered once as the pull-quote (page and mail), so the report must not restate it."""
    instructions = make_writer("gpt-5.4-mini").instructions
    assert 'Do not write a "Bottom line", "Summary" or "Key takeaways"' in instructions
    assert "short_summary already is the bottom line" in instructions
    assert "starting right after the title" in instructions
    assert "at most one sentence" in instructions and "Otherwise omit the conclusion" in instructions
    assert '2. A "Bottom line" section' not in instructions


def test_writer_prompt_wants_linked_sources_list():
    instructions = make_writer("gpt-5.4-mini").instructions
    assert '"## Sources" section: a bulleted markdown list, one source per line' in instructions
    assert '"[Title](https://...)"' in instructions
    assert "invent a URL" in instructions and "only sources that appear in the search summaries" in instructions
    assert "give its title plain" in instructions


def test_writer_prompt_markdown_hygiene_and_plain_wording():
    instructions = make_writer("gpt-5.4-mini").instructions
    assert "blank line before every heading and before every list" in instructions
    assert "Never put a list\nitem on the same line as its lead-in sentence" in instructions
    assert '"sensor-adjacent"' in instructions and 'say "unverified"' in instructions


def test_questioner_asks_only_plan_changing_questions_and_rarely_a_second_round():
    assert "only when the answer would change the search plan" in QUESTIONER_INSTRUCTIONS
    assert "Do not ask for thoroughness" in QUESTIONER_INSTRUCTIONS
    assert ("ask a second round only if an answer created a new ambiguity that changes the plan"
            in QUESTIONER_INSTRUCTIONS)
    assert "big picture and the smaller details" not in QUESTIONER_INSTRUCTIONS
    # unchanged rules
    assert "Don't ask more than 3 follow-up questions" in QUESTIONER_INSTRUCTIONS
    assert "'don't know' or 'no preference' counts as answered" in QUESTIONER_INSTRUCTIONS
    assert "needs_clarify=false with an empty questions list" in QUESTIONER_INSTRUCTIONS
    assert "same language the user wrote the query in" in QUESTIONER_INSTRUCTIONS


def test_writer_prompt_never_echoes_clarifications():
    instructions = make_writer("gpt-5.4-mini").instructions
    assert "never quote, paraphrase or refer to them" in instructions
    assert '"you clarified"' in instructions and '"as requested"' in instructions
    assert "reader who never saw the questions" in instructions


def test_hebrew_writer_keeps_language_directives_after_rewrite():
    from research_agents import WRITER_INSTRUCTIONS, WRITER_KEEP_ENGLISH_TERMS, WRITER_LANGUAGE_INSTRUCTIONS

    instructions = make_writer("gpt-5.4-mini", "he").instructions
    assert instructions.startswith(WRITER_INSTRUCTIONS)
    assert instructions.endswith(WRITER_LANGUAGE_INSTRUCTIONS.format(language="Hebrew") + WRITER_KEEP_ENGLISH_TERMS)
    assert "at most 1000 words" in instructions and HEBREW_DIRECTIVE in instructions


def test_cost_fixed_output_matches_concise_writer():
    """~1300 tokens for a 1000-word report plus plan, summary and follow-ups; was 6_500 for the 5-10 page report."""
    from models import EST_FIXED_OUTPUT, MODELS, cost_model, estimate_cost

    assert EST_FIXED_OUTPUT == 2_000 == cost_model()["fixed_output"]
    mini = next(m for m in MODELS if m["id"] == "gpt-5.4-mini")
    assert round(estimate_cost(mini, 5), 3) == 0.082
