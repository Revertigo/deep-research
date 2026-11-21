"""Deep Research pipeline: plan -> search (parallel) -> write -> deliver.

Same flow as the course lab. The difference is that `run` yields structured
events (dicts) instead of status strings, so any frontend can render a live
timeline. The agents are still plain OpenAI Agents SDK agents.

Clarifying questions happen before `run`: the UI calls /api/clarify for each
round (one questioner call each) and passes the collected Q&A pairs in. The
planner and writer both see the query plus those pairs via `render_context`.
"""

import asyncio
import time
from typing import AsyncIterator

from agents import Runner, gen_trace_id, trace

from research_agents import (
    ReportData,
    WebSearchItem,
    WebSearchPlan,
    ClarifyingTopic,
    make_emailer,
    make_questioner,
    make_planner,
    make_searcher,
    make_writer,
    render_context,
)


class ResearchManager:
    def __init__(self, model: str, email_to: str | None = None, num_searches: int = 5, skip_reason: str | None = None,
                 qa: list[dict] | None = None):
        self.model = model
        self.email_to = email_to  # None -> no email, report shown on screen only
        self.num_searches = num_searches
        self.skip_reason = skip_reason
        self.qa = qa or []  # [{"q": ..., "a": ...}] collected by /api/clarify rounds before the run

    async def run(self, query: str) -> AsyncIterator[dict]:
        trace_id = gen_trace_id()
        started = time.monotonic()
        context = render_context(query, self.qa)
        with trace("Research trace", trace_id=trace_id):
            yield {"type": "trace", "url": f"https://platform.openai.com/traces/trace?trace_id={trace_id}", "model": self.model}

            yield {"type": "stage", "stage": "plan", "status": "running"}
            plan = await self.plan_searches(context)
            yield {
                "type": "stage", "stage": "plan", "status": "done",
                "searches": [{"query": s.query, "reason": s.reason} for s in plan.searches],
            }

            yield {"type": "stage", "stage": "search", "status": "running", "total": len(plan.searches)}
            results: list[str] = []
            async for event in self.perform_searches(plan):
                if event["type"] == "search_done":
                    results.append(event["summary"])
                yield event
            yield {"type": "stage", "stage": "search", "status": "done", "total": len(plan.searches)}

            yield {"type": "stage", "stage": "write", "status": "running"}
            report = await self.write_report(context, results)
            yield {"type": "stage", "stage": "write", "status": "done"}

            # Screen first, always. Email afterwards, best-effort.
            yield {
                "type": "report",
                "summary": report.short_summary,
                "markdown": report.markdown_report,
                "follow_ups": report.follow_up_questions,
                "elapsed": round(time.monotonic() - started, 1),
            }

            if self.email_to:
                yield {"type": "stage", "stage": "deliver", "status": "running", "to": self.email_to}
                try:
                    await self.send_email(report)
                    yield {"type": "stage", "stage": "deliver", "status": "done", "to": self.email_to}
                except Exception as exc:  # report is already on screen
                    yield {"type": "stage", "stage": "deliver", "status": "failed", "error": str(exc)[:300]}
            else:
                yield {"type": "stage", "stage": "deliver", "status": "skipped", "reason": self.skip_reason}

    async def clarifying_input(self, query: str) -> ClarifyingTopic:
        """One questioner round. Called by /api/clarify; the UI drives the rounds."""
        result = await Runner.run(make_questioner(self.model), render_context(query, self.qa))
        return result.final_output

    async def plan_searches(self, context: str) -> WebSearchPlan:
        result = await Runner.run(make_planner(self.model, self.num_searches), context)
        return result.final_output

    async def perform_searches(self, plan: WebSearchPlan) -> AsyncIterator[dict]:
        searcher = make_searcher(self.model)

        async def one(index: int, item: WebSearchItem) -> dict:
            try:
                summary = await self.search(searcher, item)
                return {"type": "search_done", "index": index, "query": item.query, "summary": summary}
            except Exception as exc:
                return {"type": "search_failed", "index": index, "query": item.query, "error": str(exc)[:300]}

        tasks = [asyncio.create_task(one(i, item)) for i, item in enumerate(plan.searches)]
        for finished in asyncio.as_completed(tasks):
            yield await finished

    async def search(self, searcher, item: WebSearchItem) -> str:
        input_message = f"Search term: {item.query}\nReason for searching: {item.reason}"
        result = await Runner.run(searcher, input_message)
        return result.final_output

    async def write_report(self, context: str, search_results: list[str]) -> ReportData:
        input_message = f"Original query and clarifications:\n{context}\nSummarized search results: {search_results}"
        result = await Runner.run(make_writer(self.model), input_message)
        return result.final_output

    async def send_email(self, report: ReportData) -> None:
        await Runner.run(make_emailer(self.model, self.email_to), report.markdown_report)
