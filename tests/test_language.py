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
