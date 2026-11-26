"""LLM orchestration: request validation, tool inputs, stream -> event mapping.

No OpenAI calls: Runner.run is a fake that answers per agent, and
Runner.run_streamed is a fake that replays a scripted list of stream events.
"""

import asyncio
import base64
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import MaxTurnsExceeded, set_tracing_disabled  # noqa: E402
from agents.items import ToolCallItem, ToolCallOutputItem  # noqa: E402
from agents.stream_events import RunItemStreamEvent  # noqa: E402
from agents.tool_context import ToolContext  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from openai.types.responses import ResponseFunctionToolCall  # noqa: E402

import app as app_module  # noqa: E402
import research_manager  # noqa: E402
from research_agents import (  # noqa: E402
    ReportData,
    WebSearchItem,
    WebSearchPlan,
    make_manager,
    make_manager_tools,
    stop_when_report_written,
)

PASSWORD = "test-pass"
CONTEXT = "Query: EV adoption\nClarifications so far:\nQ: Which region?\nA: Europe"
PLAN = WebSearchPlan(searches=[WebSearchItem(reason="r1", query="q1"), WebSearchItem(reason="r2", query="q2")])
REPORT = ReportData(short_summary="s", markdown_report="# r", follow_up_questions=["f"])


def auth_header() -> dict:
    token = base64.b64encode(f"research:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def calls(monkeypatch):
    """Fake Runner.run (per-agent answers) and record (agent name, input)."""
    set_tracing_disabled(True)
    monkeypatch.setattr(app_module, "APP_PASSWORD", PASSWORD)
    monkeypatch.setattr(app_module, "APP_USER", "research")
    recorded: list[tuple[str, str]] = []

    async def fake_run(agent, input, *args, **kwargs):
        recorded.append((agent.name, input))
        if agent.name == "Planner Agent":
            out = PLAN
        elif agent.name == "Search Agent":
            out = "summary"
        elif agent.name == "Writer Agent":
            out = REPORT
        else:
            raise AssertionError(f"unexpected agent {agent.name}")
        return SimpleNamespace(final_output=out)

    monkeypatch.setattr(research_manager.Runner, "run", fake_run)
    return recorded


@pytest.fixture
def sent(monkeypatch):
    """Fake messenger.send_email; records (subject, text_body, html_body, to). No SMTP."""
    recorded: list[tuple[str, str, str, str | None]] = []

    def fake_send_email(subject, text_body, html_body, to=None):
        recorded.append((subject, text_body, html_body, to))

    monkeypatch.setattr(research_manager.messenger, "send_email", fake_send_email)
    return recorded


@pytest.fixture
def client():
    return TestClient(app_module.app)


# --- Stream fakes ------------------------------------------------------------

def tool_called(agent, call_id: str, name: str, args: dict) -> RunItemStreamEvent:
    raw = ResponseFunctionToolCall(call_id=call_id, name=name, arguments=json.dumps(args), type="function_call")
    return RunItemStreamEvent(name="tool_called", item=ToolCallItem(agent=agent, raw_item=raw))


def tool_output(agent, call_id: str, output: str) -> RunItemStreamEvent:
    raw = {"call_id": call_id, "output": output, "type": "function_call_output"}
    return RunItemStreamEvent(name="tool_output", item=ToolCallOutputItem(agent=agent, raw_item=raw, output=output))


def fake_streamed(monkeypatch, script, final_output, turns: int, raise_at_end: Exception | None = None):
    """Replace Runner.run_streamed with one that replays `script(agent)` events."""
    captured = {}

    def run_streamed(agent, input, *args, **kwargs):
        captured["agent"] = agent
        captured["input"] = input
        captured["max_turns"] = kwargs.get("max_turns")

        async def stream_events():
            for ev in script(agent):
                yield ev
            if raise_at_end:
                raise raise_at_end

        return SimpleNamespace(stream_events=stream_events, final_output=final_output,
                               raw_responses=[object()] * turns)

    monkeypatch.setattr(research_manager.Runner, "run_streamed", run_streamed)
    return captured


def happy_script(agent):
    return [
        tool_called(agent, "c1", "plan_searches", {}),
        tool_output(agent, "c1", PLAN.model_dump_json()),
        tool_called(agent, "c2", "search", {"query": "q1", "reason": "r1"}),
        tool_called(agent, "c3", "search", {"query": "q2", "reason": "r2"}),
        tool_output(agent, "c2", "summary one"),
        tool_output(agent, "c3", "summary two"),
        tool_called(agent, "c4", "write_report", {"search_summaries": ["summary one", "summary two"]}),
        tool_output(agent, "c4", REPORT.model_dump_json()),
    ]


async def collect(gen):
    return [e async for e in gen]


# --- (a) API validation ------------------------------------------------------

def test_config_hides_orchestration(calls, client):
    """Orchestration is a server-side design choice, so the UI learns nothing about it."""
    assert "orchestration" not in client.get("/api/config", headers=auth_header()).json()


def test_orchestration_env_default_is_code(monkeypatch):
    monkeypatch.delenv("ORCHESTRATION", raising=False)
    assert app_module.orchestration_from_env() == "code"
    assert app_module.ORCHESTRATION == "code"  # what the imported app actually runs with
    monkeypatch.setenv("ORCHESTRATION", " LLM ")
    assert app_module.orchestration_from_env() == "llm"
    monkeypatch.setenv("ORCHESTRATION", "foo")
    with pytest.raises(ValueError):
        app_module.orchestration_from_env()


def test_request_orchestration_field_is_ignored(calls, client):
    """A stale client still sending `orchestration` gets the server's mode (code), not a 422."""
    r = client.post("/api/research", headers=auth_header(),
                    json={"query": "EV adoption", "model": "gpt-5.4-mini", "num_searches": 1, "email_enabled": False,
                          "orchestration": "llm"})
    assert r.status_code == 200
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[0]["orchestration"] == "code"
    assert [n for n, _ in calls] == ["Planner Agent", "Search Agent", "Search Agent", "Writer Agent"]  # run_code, no manager


def test_env_llm_routes_research_to_run_llm(calls, client, monkeypatch):
    monkeypatch.setattr(app_module, "ORCHESTRATION", "llm")  # what ORCHESTRATION=llm at startup sets
    captured = fake_streamed(monkeypatch, happy_script, REPORT, turns=4)
    r = client.post("/api/research", headers=auth_header(),
                    json={"query": "EV adoption", "model": "gpt-5.4-mini", "num_searches": 2, "email_enabled": False,
                          "qa": [{"q": "Which region?", "a": "Europe"}]})
    assert r.status_code == 200
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert not [e for e in events if e["type"] == "error"], events
    assert events[0]["type"] == "trace" and events[0]["orchestration"] == "llm"
    assert events[-1] == {"type": "end"}
    assert captured["input"] == CONTEXT
    assert captured["max_turns"] == 2 + 6
    assert captured["agent"].name == "Manager Agent"
    assert captured["agent"].output_type is None
    assert captured["agent"].tool_use_behavior is stop_when_report_written
    assert not captured["agent"].handoffs
    assert sorted(t.name for t in captured["agent"].tools) == ["plan_searches", "search", "write_report"]


# --- (b) run_llm stream -> event mapping -------------------------------------

def test_run_llm_maps_stream_to_event_sequence(calls, sent, monkeypatch):
    fake_streamed(monkeypatch, happy_script, REPORT.model_dump_json(), turns=3)
    rm = research_manager.ResearchManager(model="gpt-5.4-mini", num_searches=2, orchestration="llm",
                                          email_to="a@b.co", qa=[{"q": "Which region?", "a": "Europe"}])
    events = collect_sync(rm.run("EV adoption"))

    kinds = [(e["type"], e.get("stage"), e.get("status")) for e in events]
    assert kinds == [
        ("trace", None, None),
        ("stage", "plan", "running"),
        ("stage", "plan", "done"),
        ("stage", "search", "running"),
        ("search_done", None, None),
        ("search_done", None, None),
        ("stage", "search", "done"),
        ("stage", "write", "running"),
        ("stage", "write", "done"),
        ("report", None, None),
        ("stage", "deliver", "running"),
        ("stage", "deliver", "done"),
        ("stage", "manager", "note"),
    ]
    plan_done = events[2]
    assert plan_done["searches"] == [{"query": "q1", "reason": "r1"}, {"query": "q2", "reason": "r2"}]
    assert events[3]["total"] == 2
    assert events[4] == {"type": "search_done", "index": 0, "query": "q1", "summary": "summary one"}
    assert events[5] == {"type": "search_done", "index": 1, "query": "q2", "summary": "summary two"}
    report = events[9]
    assert report["summary"] == "s" and report["markdown"] == "# r" and report["follow_ups"] == ["f"]
    assert events[12]["text"] == "4 tool calls, 3 manager turns"
    # Email is built in code after the report event: no model call, the writer's markdown goes out whole.
    assert calls == []
    assert len(sent) == 1
    subject, text_body, html_body, to = sent[0]
    assert to == "a@b.co" and subject == "Deep Research: r" and "# r" in text_body


def test_run_llm_max_turns_yields_error_not_hang(calls, monkeypatch):
    def partial(agent):
        return happy_script(agent)[:2]  # plan only, then the SDK gives up

    fake_streamed(monkeypatch, partial, None, turns=8, raise_at_end=MaxTurnsExceeded("Max turns (8) exceeded"))
    rm = research_manager.ResearchManager(model="gpt-5.4-mini", num_searches=2, orchestration="llm")
    events = collect_sync(rm.run("EV adoption"))
    types = [e["type"] for e in events]
    assert "report" not in types
    error = next(e for e in events if e["type"] == "error")
    assert "8 turns" in error["message"]
    assert events[-1]["stage"] == "manager"
    assert calls == []


def test_run_llm_no_write_report_yields_readable_error(calls, monkeypatch):
    def no_write(agent):
        return happy_script(agent)[:6]  # plan + searches, manager answers in prose instead of write_report

    fake_streamed(monkeypatch, no_write, "here is my report", turns=3)
    rm = research_manager.ResearchManager(model="gpt-5.4-mini", num_searches=2, orchestration="llm")
    events = collect_sync(rm.run("EV adoption"))
    error = next(e for e in events if e["type"] == "error")
    assert error["message"] == "Manager finished without writing a report."
    assert "report" not in [e["type"] for e in events]
    assert events[-1]["stage"] == "manager"


def test_run_llm_exception_after_report_still_shows_report(calls, sent, monkeypatch):
    fake_streamed(monkeypatch, happy_script, None, turns=3, raise_at_end=RuntimeError("tool exploded"))
    rm = research_manager.ResearchManager(model="gpt-5.4-mini", num_searches=2, orchestration="llm", email_to="a@b.co")
    events = collect_sync(rm.run("EV adoption"))
    types = [e["type"] for e in events]
    assert "error" not in types
    assert types.index("report") < types.index("stage", types.index("report"))  # deliver comes after report
    deliver = [e for e in events if e.get("stage") == "deliver"]
    assert [d["status"] for d in deliver] == ["running", "done"]
    assert events[-1]["stage"] == "manager" and "after the report: The manager agent failed: tool exploded" in events[-1]["text"]
    assert calls == []
    assert [s[3] for s in sent] == ["a@b.co"]


def test_run_llm_exception_before_report_yields_error(calls, monkeypatch):
    def partial(agent):
        return happy_script(agent)[:4]

    fake_streamed(monkeypatch, partial, None, turns=2, raise_at_end=RuntimeError("model down"))
    rm = research_manager.ResearchManager(model="gpt-5.4-mini", num_searches=2, orchestration="llm")
    events = collect_sync(rm.run("EV adoption"))
    error = next(e for e in events if e["type"] == "error")
    assert error["message"] == "The manager agent failed: model down"
    assert "report" not in [e["type"] for e in events]


def test_run_llm_search_failure_maps_to_search_failed(calls, monkeypatch):
    def failing(agent):
        script = happy_script(agent)
        script[4] = tool_output(agent, "c2", "Search failed: boom")
        return script

    fake_streamed(monkeypatch, failing, REPORT, turns=4)
    rm = research_manager.ResearchManager(model="gpt-5.4-mini", num_searches=2, orchestration="llm")
    events = collect_sync(rm.run("EV adoption"))
    failed = next(e for e in events if e["type"] == "search_failed")
    assert failed == {"type": "search_failed", "index": 0, "query": "q1", "error": "boom"}
    assert "report" in [e["type"] for e in events]


# --- Tool closures call the same agents with the same inputs ------------------

def test_manager_tools_wrap_agents_with_code_path_inputs(calls):
    tools = {t.name: t for t in make_manager_tools("gpt-5.4-mini", CONTEXT, 2, research_manager.Runner)}
    assert set(tools) == {"plan_searches", "search", "write_report"}

    def invoke(name, args):
        ctx = ToolContext(context=None, tool_name=name, tool_call_id="c", tool_arguments=json.dumps(args))
        return run_sync(tools[name].on_invoke_tool(ctx, json.dumps(args)))

    assert json.loads(invoke("plan_searches", {})) == PLAN.model_dump()
    assert invoke("search", {"query": "q1", "reason": "r1"}) == "summary"
    assert json.loads(invoke("write_report", {"search_summaries": ["summary"]})) == REPORT.model_dump()

    inputs = dict(calls)
    assert inputs["Planner Agent"] == CONTEXT
    assert inputs["Search Agent"] == f"Research context:\n{CONTEXT}\n\nToday: {date.today().isoformat()}\nSearch term: q1\nReason for searching: r1"
    assert inputs["Writer Agent"] == f"Original query and clarifications:\n{CONTEXT}\nSummarized search results: ['summary']"


def test_make_manager_stops_on_write_report(calls):
    tools = make_manager_tools("gpt-5.4-mini", CONTEXT, 3, research_manager.Runner)
    manager = make_manager("gpt-5.4-mini", 3, tools)
    assert manager.output_type is None
    assert manager.tool_use_behavior is stop_when_report_written
    assert manager.handoffs == []
    assert "exactly once" in manager.instructions and "3 web searches" in manager.instructions

    write = next(t for t in tools if t.name == "write_report")
    search = next(t for t in tools if t.name == "search")
    ok = SimpleNamespace(tool=write, output=REPORT.model_dump_json())
    bad = SimpleNamespace(tool=write, output="Report not written: no search summaries were given")
    other = SimpleNamespace(tool=search, output="summary")
    assert stop_when_report_written(None, [other, ok]).is_final_output is True
    assert stop_when_report_written(None, [other, ok]).final_output == REPORT.model_dump_json()
    assert stop_when_report_written(None, [bad]).is_final_output is False
    assert stop_when_report_written(None, [other]).is_final_output is False


def test_search_budget_enforced_in_tool(calls):
    tools = {t.name: t for t in make_manager_tools("gpt-5.4-mini", CONTEXT, 2, research_manager.Runner)}

    def search(i):
        args = json.dumps({"query": f"q{i}", "reason": "r"})
        ctx = ToolContext(context=None, tool_name="search", tool_call_id=f"c{i}", tool_arguments=args)
        return run_sync(tools["search"].on_invoke_tool(ctx, args))

    assert search(1) == "summary"
    assert search(2) == "summary"
    assert search(3) == "Search failed: search budget of 2 reached"
    assert search(4) == "Search failed: search budget of 2 reached"
    assert [n for n, _ in calls] == ["Search Agent", "Search Agent"]  # no model call past the budget


def test_write_report_once_guard_and_empty_summaries(calls):
    tools = {t.name: t for t in make_manager_tools("gpt-5.4-mini", CONTEXT, 2, research_manager.Runner)}

    def write(summaries):
        args = json.dumps({"search_summaries": summaries})
        ctx = ToolContext(context=None, tool_name="write_report", tool_call_id="w", tool_arguments=args)
        return run_sync(tools["write_report"].on_invoke_tool(ctx, args))

    assert write([]) == "Report not written: no search summaries were given"
    assert calls == []
    first = write(["a"])
    assert json.loads(first) == REPORT.model_dump()
    assert write(["b"]) == first
    assert [n for n, _ in calls] == ["Writer Agent"]  # second call did not run the writer


# --- (c) code mode unchanged --------------------------------------------------

def test_code_mode_default_and_unchanged(calls, client):
    r = client.post("/api/research", headers=auth_header(),
                    json={"query": "EV adoption", "model": "gpt-5.4-mini", "num_searches": 1, "email_enabled": False})
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[0]["orchestration"] == "code"
    assert [n for n, _ in calls] == ["Planner Agent", "Search Agent", "Search Agent", "Writer Agent"]
    assert "manager" not in {e.get("stage") for e in events}


def test_bad_orchestration_value_rejected_in_manager():
    with pytest.raises(ValueError):
        research_manager.ResearchManager(model="gpt-5.4-mini", orchestration="foo")


# --- helpers -----------------------------------------------------------------

def run_sync(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def collect_sync(gen):
    return run_sync(collect(gen))
