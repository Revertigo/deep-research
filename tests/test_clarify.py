"""Clarify step: /api/clarify, qa threading into the planner, validation, auth.

No OpenAI calls: Runner.run is replaced by a fake that answers per agent.
"""

import base64
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import set_tracing_disabled  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import research_manager  # noqa: E402
from models import MAX_CLARIFY_ROUNDS, MAX_QUESTIONS_PER_ROUND  # noqa: E402
from research_agents import ClarifyingTopic, ReportData, WebSearchItem, WebSearchPlan  # noqa: E402

PASSWORD = "test-pass"


def auth_header(user: str = "research", password: str = PASSWORD) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def calls(monkeypatch):
    """Fake Runner.run; records (agent name, input) for every call."""
    set_tracing_disabled(True)
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setattr(app_module, "APP_PASSWORD", PASSWORD)
    monkeypatch.setattr(app_module, "APP_USER", "research")
    recorded: list[tuple[str, str]] = []

    async def fake_run(agent, input, *args, **kwargs):
        recorded.append((agent.name, input))
        if agent.name == "Questioner Agent":
            out = ClarifyingTopic(
                needs_clarify=True,
                questions=[f"Question {i}?" for i in range(MAX_QUESTIONS_PER_ROUND + 2)],
                clarification="Research X for a beginner.",
            )
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


def test_config_exposes_clarify_caps(calls, client):
    r = client.get("/api/config", headers=auth_header())
    assert r.status_code == 200
    assert r.json()["clarify"] == {"max_rounds": MAX_CLARIFY_ROUNDS, "max_questions": MAX_QUESTIONS_PER_ROUND}


def test_clarify_returns_fields_and_truncates(calls, client):
    qa = [{"q": "Level?", "a": "beginner"}]
    r = client.post("/api/clarify", headers=auth_header(), json={"query": "Explain X", "qa": qa, "model": "gpt-5.4-mini"})
    assert r.status_code == 200
    body = r.json()
    assert body["needs_clarify"] is True
    assert body["questions"] == [f"Question {i}?" for i in range(MAX_QUESTIONS_PER_ROUND)]
    assert body["clarification"] == "Research X for a beginner."
    name, sent = calls[0]
    assert name == "Questioner Agent"
    assert sent == "Query: Explain X\nClarifications so far:\nQ: Level?\nA: beginner"


def test_clarify_without_qa_omits_block(calls, client):
    client.post("/api/clarify", headers=auth_header(), json={"query": "Explain X", "model": "gpt-5.4-mini"})
    assert calls[0][1] == "Query: Explain X"


def test_research_passes_qa_to_planner_and_writer(calls, client):
    qa = [{"q": "Which region?", "a": "Europe"}, {"q": "Timeframe?", "a": "no preference"}]
    r = client.post(
        "/api/research",
        headers=auth_header(),
        json={"query": "EV adoption", "model": "gpt-5.4-mini", "num_searches": 1, "email_enabled": False, "qa": qa},
    )
    assert r.status_code == 200
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert not [e for e in events if e["type"] == "error"], events
    assert "querying" not in {e.get("stage") for e in events}
    expected = "Query: EV adoption\nClarifications so far:\nQ: Which region?\nA: Europe\nQ: Timeframe?\nA: no preference"
    inputs = dict(calls)
    assert inputs["Planner Agent"] == expected
    assert expected in inputs["Writer Agent"]
    assert "Questioner Agent" not in inputs


def test_unknown_model_rejected(calls, client):
    r = client.post("/api/clarify", headers=auth_header(), json={"query": "Explain X", "model": "nope"})
    assert r.status_code == 400
    r = client.post("/api/research", headers=auth_header(), json={"query": "Explain X", "model": "nope"})
    assert r.status_code == 400
    assert calls == []


def test_auth_required(calls, client):
    assert client.post("/api/clarify", json={"query": "Explain X"}).status_code == 401
    assert client.get("/api/clarify").status_code == 401
    assert client.post("/api/clarify", headers=auth_header(password="wrong"), json={"query": "Explain X"}).status_code == 401
    assert calls == []


def test_long_question_is_clipped_and_echo_accepted(calls, client, monkeypatch):
    base_run = research_manager.Runner.run
    long_q = "Why " + "x" * 600 + "?"

    async def long_questioner(agent, input, *args, **kwargs):
        if agent.name == "Questioner Agent":
            calls.append((agent.name, input))
            return SimpleNamespace(final_output=ClarifyingTopic(needs_clarify=True, questions=[long_q], clarification="c"))
        return await base_run(agent, input, *args, **kwargs)

    monkeypatch.setattr(research_manager.Runner, "run", long_questioner)
    r = client.post("/api/clarify", headers=auth_header(), json={"query": "Explain X", "model": "gpt-5.4-mini"})
    assert r.status_code == 200
    q = r.json()["questions"][0]
    assert len(q) <= app_module.MAX_QUERY_CHARS

    qa = [{"q": q, "a": "no preference"}]
    r = client.post("/api/clarify", headers=auth_header(), json={"query": "Explain X", "qa": qa, "model": "gpt-5.4-mini"})
    assert r.status_code == 200
    r = client.post(
        "/api/research",
        headers=auth_header(),
        json={"query": "Explain X", "model": "gpt-5.4-mini", "num_searches": 1, "email_enabled": False, "qa": qa},
    )
    assert r.status_code == 200


def test_research_passes_context_to_searcher(calls, client):
    qa = [{"q": "Which region?", "a": "Europe"}]
    r = client.post(
        "/api/research",
        headers=auth_header(),
        json={"query": "EV adoption", "model": "gpt-5.4-mini", "num_searches": 1, "email_enabled": False, "qa": qa},
    )
    assert r.status_code == 200
    sent = dict(calls)["Search Agent"]
    assert sent == (
        "Research context:\nQuery: EV adoption\nClarifications so far:\nQ: Which region?\nA: Europe\n\n"
        f"Today: {date.today().isoformat()}\nSearch term: q1\nReason for searching: r"
    )
