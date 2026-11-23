"""Agent factories for the Deep Research pipeline (OpenAI Agents SDK).

Each factory builds a fresh Agent for the requested model so a single run can
use a cheap or an expensive model end to end. The output schemas are the ones
from the course lab; the writer prompt was rewritten for a concise report
(bottom line first, up to 1000 words) since v1.4.2.
"""

from agents import Agent, FunctionToolResult, ModelSettings, RunContextWrapper, ToolsToFinalOutputResult, WebSearchTool, function_tool
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from messenger import push

load_dotenv(override=True)

# --- Questioner ------------------------------------------------------------------

class ClarifyingTopic(BaseModel):
    needs_clarify: bool = Field(description="True only if answering questions would change how the research is planned. "
                                            "False when the query is already specific or every question has been answered.")
    questions: list[str] = Field(description="1-3 questions, empty when needs_clarify is false. Never repeat a question already answered.")
    clarification: str = Field(description="One-sentence restatement of the research goal including the answers so far.")

QUESTIONER_INSTRUCTIONS = """
You are a questioner assistant. Given a user query and any answers so far, come up with clarifying questions about the topic to research
to make sure you understand the big picture and the smaller details about the topic to research. These follow-up questions will
help to improve the future planning searches about this topic. Don't ask more than 3 follow-up questions in total for each round.
An answer like 'don't know' or 'no preference' counts as answered; treat it as no constraint.
If you're sure you have enough information from the input query about the topic and you don't think you need additional
clarification, then you can skip asking.
Return needs_clarify=false with an empty questions list when the query is already specific or every question has been answered.
Ask the clarifying questions in the same language the user wrote the query in.
"""

def make_questioner(model: str) -> Agent:
    return Agent(
        name="Questioner Agent",
        instructions=QUESTIONER_INSTRUCTIONS,
        model=model,
        output_type=ClarifyingTopic,
    )


def render_context(query: str, qa: list[dict] | None = None) -> str:
    """Query plus the user's answers, in one block shared by questioner, planner, searcher and writer."""
    lines = [f"Query: {query}"]
    if qa:
        lines.append("Clarifications so far:")
        for pair in qa:
            lines += [f"Q: {pair['q']}", f"A: {pair['a']}"]
    return "\n".join(lines)

# --- Planner -----------------------------------------------------------------

class WebSearchItem(BaseModel):
    reason: str = Field(description="Your reasoning for why this search is important to the query.")
    query: str = Field(description="The search term to use for the web search.")


class WebSearchPlan(BaseModel):
    searches: list[WebSearchItem] = Field(description="A list of web searches to perform to best answer the query.")


PLANNER_INSTRUCTIONS = """
You are a research assistant. Given a user query, come up with a set of web searches
to perform to best answer the query. Output exactly {n} terms to query for.
Make the searches complementary: each should cover a different angle, source type or
sub-question, with no two searches that would return the same results.
Write search queries in English. If the topic is local to Israel or a Hebrew-speaking context
(Israeli prices, regulations, companies, services, local news), add Hebrew-language queries
where Hebrew sources are likely better.
"""


def make_planner(model: str, num_searches: int) -> Agent:
    return Agent(
        name="Planner Agent",
        instructions=PLANNER_INSTRUCTIONS.format(n=num_searches),
        model=model,
        output_type=WebSearchPlan,
    )


# --- Search ------------------------------------------------------------------

SEARCH_INSTRUCTIONS = """
You are a research assistant. Given a search term, you search the web for that term and
produce a concise summary of the results. The summary must 2-3 paragraphs and less than 300 words.
Capture the main points and be succinct. Reply only with the summary.
The input may start with the user's research goal and their clarification answers.
Focus the summary on what is relevant to them and skip unrelated angles the pages cover.
Write the summary in English whatever language the search term or the sources are in.
"""


def make_searcher(model: str) -> Agent:
    return Agent(
        name="Search Agent",
        instructions=SEARCH_INSTRUCTIONS,
        tools=[WebSearchTool()],
        model=model,
        model_settings=ModelSettings(tool_choice="required"),
    )


# --- Writer ------------------------------------------------------------------

class ReportData(BaseModel):
    short_summary: str = Field(description="A short 2-3 sentence summary of the findings.")
    markdown_report: str = Field(description="The final report")
    follow_up_questions: list[str] = Field(description="Suggested topics to research further")


WRITER_INSTRUCTIONS = """
You are a senior researcher writing a concise, decision-ready report for a research query.
You will be provided with the original query, any clarification answers, and summarized search results.

Shape of markdown_report, in this order:
1. A title line (H1).
2. A "Bottom line" section: 2-4 sentences that answer the query directly.
3. 4-8 numbered sections. Each has a short lead sentence and 2-6 bullets with concrete
   facts, numbers, dates and names from the research. No filler, no restating the question.
4. A short "Conclusion": 2-3 sentences.
5. A "Sources" section: the links actually used, as markdown links, one per line.

Length: the whole markdown_report must be at most 1000 words, sources excluded; 600-900 is typical.
This is a ceiling, not a target. Cut repetition and generalities before cutting facts.

Use the clarification answers to decide scope and emphasis, but never quote, paraphrase or refer to them.
Do not write "you asked", "you clarified", "per your request", "as requested" or "the user wants".
Write a standalone report for a reader who never saw the questions.
Do not add meta lines about the research process or the inputs, such as "references noted in the
source report" or "(from provided research results)".
"""

# Report language: the one agent whose output language the user picks. The planner
# chooses its own search language (English, plus Hebrew for Israel-local topics) and
# the searcher always summarizes in English, so only the writer gets this directive.
LANGUAGE_NAMES = {"en": "English", "he": "Hebrew"}
WRITER_LANGUAGE_INSTRUCTIONS = "Write the report, the short summary and the follow-up questions in {language}.\n"
WRITER_KEEP_ENGLISH_TERMS = "Keep technical terms, product names, library/API names and code in English.\n"


def make_writer(model: str, language: str = "en") -> Agent:
    instructions = WRITER_INSTRUCTIONS + WRITER_LANGUAGE_INSTRUCTIONS.format(language=LANGUAGE_NAMES[language])
    if language != "en":
        instructions += WRITER_KEEP_ENGLISH_TERMS
    return Agent(name="Writer Agent", instructions=instructions, model=model, output_type=ReportData)


# --- Manager (LLM orchestration) ---------------------------------------------
#
# Second way to run the pipeline: instead of code calling planner -> searchers
# -> writer in a fixed order, a manager agent decides the calls itself. The
# three tools wrap the SAME agents with the SAME inputs the code path uses, so
# a run in either mode is comparable. The questioner stays outside (the browser
# drives the clarify loop before the run) and the email is built in code after
# the manager returns (research_manager.build_email), so the report is on
# screen before any email attempt and the manager has no email tool.

MANAGER_INSTRUCTIONS = """
You are a research manager. You run a deep research for the user's query and clarifications given as input.
Work only through your tools, in this order:
1. Call plan_searches exactly once to get a list of {n} web searches.
2. Call search once for every planned search, passing its query and reason unchanged. You may issue
   these calls in parallel. Aim for exactly {n} searches: do not add, drop or merge searches.
3. Call write_report exactly once, passing every search summary. It already knows the query and clarifications.
   The research is complete when write_report returns; you do not need to repeat the report.
Do not search, plan or write anything yourself; the tools do the work.
"""

SEARCH_FAILED_PREFIX = "Search failed: "
WRITE_FAILED_PREFIX = "Report not written: "


def make_manager_tools(model: str, context: str, num_searches: int, runner, language: str = "en") -> list:
    """The manager's tools, as closures over the planner, searcher and writer.

    `runner` is the Runner class (passed in so tests can fake Runner.run the
    same way they do for the code path). Each tool returns a string for the
    model: JSON for the plan and the report, the summary text for a search.
    `language` is the report language, given to the writer exactly as in the
    code path.

    Budgets are enforced here, not only in the prompt: at most `num_searches`
    searcher runs, and one writer run (a second write_report call returns the
    first report without another model call).
    """
    planner = make_planner(model, num_searches)
    searcher = make_searcher(model)
    writer = make_writer(model, language)
    searches_started = 0
    written: list[str] = []  # the first write_report result, once there is one

    @function_tool
    async def plan_searches() -> str:
        """
        Plan the web searches for the research query and clarifications given to you.

        Returns a JSON object {"searches": [{"query": str, "reason": str}, ...]} with the planned searches.
        Call it once, before any search.
        """
        result = await runner.run(planner, context)
        return result.final_output.model_dump_json()

    @function_tool
    async def search(query: str, reason: str) -> str:
        """
        Run one web search and summarize the results for this research.

        Args:
            query: The search term, exactly as planned by plan_searches.
            reason: Why this search matters for the query, exactly as planned by plan_searches.

        Returns the summary text (2-3 paragraphs), or a line starting with "Search failed:" on error
        or once the search budget is used up.
        """
        nonlocal searches_started
        if searches_started >= num_searches:
            return f"{SEARCH_FAILED_PREFIX}search budget of {num_searches} reached"
        searches_started += 1
        item = WebSearchItem(query=query, reason=reason)
        input_message = f"Research context:\n{context}\n\nSearch term: {item.query}\nReason for searching: {item.reason}"
        try:
            result = await runner.run(searcher, input_message)
        except Exception as exc:
            return f"{SEARCH_FAILED_PREFIX}{str(exc)[:300]}"
        return result.final_output

    @function_tool
    async def write_report(search_summaries: list[str]) -> str:
        """
        Write the final research report from the search summaries.

        Args:
            search_summaries: Every summary returned by the search tool, in any order.

        Returns a JSON object {"short_summary": str, "markdown_report": str, "follow_up_questions": [str, ...]},
        or a line starting with "Report not written:" when no summaries were given.
        Call it once, after all searches are done. The research ends when it returns.
        """
        if written:
            return written[0]
        if not search_summaries:
            return f"{WRITE_FAILED_PREFIX}no search summaries were given"
        input_message = f"Original query and clarifications:\n{context}\nSummarized search results: {search_summaries}"
        result = await runner.run(writer, input_message)
        written.append(result.final_output.model_dump_json())
        return written[0]

    return [plan_searches, search, write_report]


def stop_when_report_written(_ctx: RunContextWrapper, results: list[FunctionToolResult]) -> ToolsToFinalOutputResult:
    """tool_use_behavior: end the run on a successful write_report, with its JSON as final_output.

    Same as StopAtTools(["write_report"]) except that a "Report not written:" answer
    (empty summaries) goes back to the manager so it can fix the call instead of
    ending the run without a report.
    """
    for r in results:
        if r.tool.name == "write_report" and not str(r.output).startswith(WRITE_FAILED_PREFIX):
            return ToolsToFinalOutputResult(is_final_output=True, final_output=r.output)
    return ToolsToFinalOutputResult(is_final_output=False, final_output=None)


def make_manager(model: str, num_searches: int, tools: list) -> Agent:
    """Manager agent: tools only, no handoffs.

    The run stops when write_report returns a report (`stop_when_report_written`):
    its JSON output is the run's final_output, so the manager never retypes the
    report as a structured answer (one turn and the report's output tokens saved).
    The query and clarifications are the run input (`render_context`), not part
    of the instructions, so the manager sees them the way the planner does.
    """
    return Agent(
        name="Manager Agent",
        instructions=MANAGER_INSTRUCTIONS.format(n=num_searches),
        tools=tools,
        model=model,
        tool_use_behavior=stop_when_report_written,
    )


__all__ = ["make_questioner", "make_planner", "make_searcher", "make_writer", "make_manager",
           "make_manager_tools", "stop_when_report_written", "push", "render_context", "ClarifyingTopic", "WebSearchItem", "WebSearchPlan",
           "ReportData", "LANGUAGE_NAMES", "MANAGER_INSTRUCTIONS", "SEARCH_FAILED_PREFIX", "WRITE_FAILED_PREFIX"]
