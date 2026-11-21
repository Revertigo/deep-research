"""Deep Research pipeline: plan -> search (parallel) -> write -> deliver.

Same flow as the course lab. The difference is that `run` yields structured
events (dicts) instead of status strings, so any frontend can render a live
timeline. The agents are still plain OpenAI Agents SDK agents.

Two orchestration modes share the same agents and the same event schema:

- "code" (default): this module calls planner -> searchers -> writer in a fixed
  order (`run_code`).
- "llm": a manager agent gets the planner, searcher and writer as tools and
  decides the calls itself (`run_llm`). Its tool calls are mapped onto the same
  events, so the UI renders both modes the same way.

Clarifying questions happen before `run`: the UI calls /api/clarify for each
round (one questioner call each) and passes the collected Q&A pairs in. The
planner, every searcher and the writer see the query plus those pairs via
`render_context`.
"""

import asyncio
import json
import time
from typing import AsyncIterator

from agents import MaxTurnsExceeded, Runner, gen_trace_id, trace

from research_agents import (
    SEARCH_FAILED_PREFIX,
    ClarifyingTopic,
    ReportData,
    WebSearchItem,
    WebSearchPlan,
    make_emailer,
    make_manager,
    make_manager_tools,
    make_planner,
    make_questioner,
    make_searcher,
    make_writer,
    render_context,
)

ORCHESTRATIONS = ("code", "llm")


class ResearchManager:
    def __init__(self, model: str, email_to: str | None = None, num_searches: int = 5, skip_reason: str | None = None,
                 qa: list[dict] | None = None, orchestration: str = "code"):
        if orchestration not in ORCHESTRATIONS:
            raise ValueError(f"orchestration must be one of {ORCHESTRATIONS}, got {orchestration!r}")
        self.model = model
        self.email_to = email_to  # None -> no email, report shown on screen only
        self.num_searches = num_searches
        self.skip_reason = skip_reason
        self.qa = qa or []  # [{"q": ..., "a": ...}] collected by /api/clarify rounds before the run
        self.orchestration = orchestration

    async def run(self, query: str) -> AsyncIterator[dict]:
        runner = self.run_llm if self.orchestration == "llm" else self.run_code
        async for event in runner(query):
            yield event

    # --- Shared pieces -------------------------------------------------------

    def _trace_event(self, trace_id: str) -> dict:
        return {
            "type": "trace",
            "url": f"https://platform.openai.com/traces/trace?trace_id={trace_id}",
            "model": self.model,
            "orchestration": self.orchestration,
        }

    @staticmethod
    def _report_event(report: ReportData, started: float) -> dict:
        return {
            "type": "report",
            "summary": report.short_summary,
            "markdown": report.markdown_report,
            "follow_ups": report.follow_up_questions,
            "elapsed": round(time.monotonic() - started, 1),
        }

    async def deliver(self, report: ReportData) -> AsyncIterator[dict]:
        """Email block, best-effort. Always runs after the report event (screen first)."""
        if self.email_to:
            yield {"type": "stage", "stage": "deliver", "status": "running", "to": self.email_to}
            try:
                await self.send_email(report)
                yield {"type": "stage", "stage": "deliver", "status": "done", "to": self.email_to}
            except Exception as exc:  # report is already on screen
                yield {"type": "stage", "stage": "deliver", "status": "failed", "error": str(exc)[:300]}
        else:
            yield {"type": "stage", "stage": "deliver", "status": "skipped", "reason": self.skip_reason}

    # --- Code orchestration --------------------------------------------------

    async def run_code(self, query: str) -> AsyncIterator[dict]:
        trace_id = gen_trace_id()
        started = time.monotonic()
        context = render_context(query, self.qa)
        with trace("Research trace", trace_id=trace_id):
            yield self._trace_event(trace_id)

            yield {"type": "stage", "stage": "plan", "status": "running"}
            plan = await self.plan_searches(context)
            yield {
                "type": "stage", "stage": "plan", "status": "done",
                "searches": [{"query": s.query, "reason": s.reason} for s in plan.searches],
            }

            yield {"type": "stage", "stage": "search", "status": "running", "total": len(plan.searches)}
            results: list[str] = []
            async for event in self.perform_searches(plan, context):
                if event["type"] == "search_done":
                    results.append(event["summary"])
                yield event
            yield {"type": "stage", "stage": "search", "status": "done", "total": len(plan.searches)}

            yield {"type": "stage", "stage": "write", "status": "running"}
            report = await self.write_report(context, results)
            yield {"type": "stage", "stage": "write", "status": "done"}

            # Screen first, always. Email afterwards, best-effort.
            yield self._report_event(report, started)
            async for event in self.deliver(report):
                yield event

    # --- LLM orchestration ---------------------------------------------------

    async def run_llm(self, query: str) -> AsyncIterator[dict]:
        """Manager agent drives the tools; its tool calls become the same UI events.

        Stream mapping (agents SDK `run_item_stream_event`):
          tool_called  plan_searches            -> (plan already "running")
          tool_output  plan_searches            -> stage plan done, stage search running
          tool_output  search                   -> search_done / search_failed
          tool_called  write_report             -> stage search done, stage write running
          tool_output  write_report             -> stage write done
        Then: report event, deliver block, manager note with call/turn counts.
        """
        trace_id = gen_trace_id()
        started = time.monotonic()
        context = render_context(query, self.qa)
        tools = make_manager_tools(self.model, context, self.num_searches, Runner)
        manager = make_manager(self.model, self.num_searches, tools)

        with trace("Research trace", trace_id=trace_id):
            yield self._trace_event(trace_id)
            yield {"type": "stage", "stage": "plan", "status": "running"}

            pending: dict[str, tuple[str, dict]] = {}  # call_id -> (tool name, arguments)
            planned: list[dict] = []
            search_index = 0
            tool_calls = 0
            tool_report: ReportData | None = None
            write_started = False
            error: str | None = None

            result = Runner.run_streamed(manager, context, max_turns=self.num_searches + 6)
            try:
                async for ev in result.stream_events():
                    if getattr(ev, "type", None) != "run_item_stream_event":
                        continue
                    item = ev.item
                    raw = item.raw_item
                    if ev.name == "tool_called":
                        name = getattr(raw, "name", None)
                        call_id = getattr(raw, "call_id", None)
                        if not name:
                            continue
                        tool_calls += 1
                        pending[call_id] = (name, _parse_json(getattr(raw, "arguments", None)))
                        if name == "write_report" and not write_started:
                            write_started = True
                            yield {"type": "stage", "stage": "search", "status": "done", "total": len(planned)}
                            yield {"type": "stage", "stage": "write", "status": "running"}
                    elif ev.name == "tool_output":
                        call_id = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", None)
                        name, args = pending.pop(call_id, (None, {}))
                        output = item.output
                        if name == "plan_searches":
                            plan = _parse_model(WebSearchPlan, output)
                            if plan is None:  # tool failed; the manager sees the error text and decides
                                continue
                            planned = [{"query": s.query, "reason": s.reason} for s in plan.searches]
                            yield {"type": "stage", "stage": "plan", "status": "done", "searches": planned}
                            yield {"type": "stage", "stage": "search", "status": "running", "total": len(planned)}
                        elif name == "search":
                            query_text = args.get("query", "")
                            # shortcut: a search the manager invents outside the plan gets the next free
                            # index and no card in the UI; upgrade if the manager does this in practice.
                            index = next((i for i, s in enumerate(planned) if s["query"] == query_text), None)
                            if index is None:
                                index = len(planned) + search_index
                                search_index += 1
                            text = str(output)
                            if text.startswith(SEARCH_FAILED_PREFIX):
                                yield {"type": "search_failed", "index": index, "query": query_text,
                                       "error": text[len(SEARCH_FAILED_PREFIX):]}
                            else:
                                yield {"type": "search_done", "index": index, "query": query_text, "summary": text}
                        elif name == "write_report":
                            tool_report = _parse_model(ReportData, output)
                            if tool_report is not None:
                                yield {"type": "stage", "stage": "write", "status": "done"}
            except MaxTurnsExceeded as exc:
                error = f"The manager agent did not finish within {self.num_searches + 6} turns ({exc})."

            turns = len(result.raw_responses)
            note = {"type": "stage", "stage": "manager", "status": "note",
                    "text": f"{tool_calls} tool call{'s' if tool_calls != 1 else ''}, {turns} manager turn{'s' if turns != 1 else ''}"}

            # The writer's own output is the report shown, as in code mode. The manager's
            # structured final output is the fallback when the write_report output was lost.
            report = tool_report
            if report is None and error is None:
                final = result.final_output
                if isinstance(final, ReportData):
                    report = final
                else:
                    error = f"The manager agent returned {type(final).__name__} instead of a report."
            if report is None:
                yield {"type": "error", "message": error or "The manager agent produced no report."}
                yield note
                return

            # Screen first, always. Email afterwards, best-effort.
            yield self._report_event(report, started)
            async for event in self.deliver(report):
                yield event
            yield note

    # --- Agents --------------------------------------------------------------

    async def clarifying_input(self, query: str) -> ClarifyingTopic:
        """One questioner round. Called by /api/clarify; the UI drives the rounds."""
        result = await Runner.run(make_questioner(self.model), render_context(query, self.qa))
        return result.final_output

    async def plan_searches(self, context: str) -> WebSearchPlan:
        result = await Runner.run(make_planner(self.model, self.num_searches), context)
        return result.final_output

    async def perform_searches(self, plan: WebSearchPlan, context: str = "") -> AsyncIterator[dict]:
        searcher = make_searcher(self.model)

        async def one(index: int, item: WebSearchItem) -> dict:
            try:
                summary = await self.search(searcher, item, context)
                return {"type": "search_done", "index": index, "query": item.query, "summary": summary}
            except Exception as exc:
                return {"type": "search_failed", "index": index, "query": item.query, "error": str(exc)[:300]}

        tasks = [asyncio.create_task(one(i, item)) for i, item in enumerate(plan.searches)]
        for finished in asyncio.as_completed(tasks):
            yield await finished

    async def search(self, searcher, item: WebSearchItem, context: str = "") -> str:
        input_message = f"Search term: {item.query}\nReason for searching: {item.reason}"
        if context:
            input_message = f"Research context:\n{context}\n\n{input_message}"
        result = await Runner.run(searcher, input_message)
        return result.final_output

    async def write_report(self, context: str, search_results: list[str]) -> ReportData:
        input_message = f"Original query and clarifications:\n{context}\nSummarized search results: {search_results}"
        result = await Runner.run(make_writer(self.model), input_message)
        return result.final_output

    async def send_email(self, report: ReportData) -> None:
        await Runner.run(make_emailer(self.model, self.email_to), report.markdown_report)


def _parse_json(text: str | None) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_model(model_cls, output):
    """Tool output -> pydantic model, or None when the tool returned an error string instead."""
    try:
        return model_cls.model_validate_json(str(output))
    except ValueError:
        return None
