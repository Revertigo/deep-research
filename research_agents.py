"""Agent factories for the Deep Research pipeline (OpenAI Agents SDK).

Each factory builds a fresh Agent for the requested model so a single run can
use a cheap or an expensive model end to end. The instructions and output
schemas are the ones from the course lab, unchanged.
"""

from agents import Agent, ModelSettings, WebSearchTool, function_tool
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from messenger import push, send_email

load_dotenv(override=True)

# --- Questioner ------------------------------------------------------------------

class ClarifyingTopic(BaseModel):
    needs_clarify: bool = Field(description="True only if answering questions would change how the research is planned. " \
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
"""

def make_questioner(model: str) -> Agent:
    return Agent(
        name="Questioner Agent",
        instructions=QUESTIONER_INSTRUCTIONS,
        model=model,
        output_type=ClarifyingTopic,
    )


def render_context(query: str, qa: list[dict] | None = None) -> str:
    """Query plus the user's answers, in one block shared by questioner, planner and writer."""
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
You are a senior researcher tasked with writing a cohesive report for a research query.
You will be provided with the original query, and some research.
Generate a comprehensive report based on the research and the query.
The final output should be in markdown format, and it should be lengthy and detailed. Aim
for 5-10 pages of content, at least 1000 words.
"""


def make_writer(model: str) -> Agent:
    return Agent(name="Writer Agent", instructions=WRITER_INSTRUCTIONS, model=model, output_type=ReportData)


# --- Email -------------------------------------------------------------------

EMAIL_INSTRUCTIONS = """
You are provided with a detailed report. Use your tool to send an email, converting the report into
a clean, well presented HTML email with an appropriate subject line.
"""


def make_emailer(model: str, recipient: str) -> Agent:
    """The recipient is bound into the tool so the LLM never chooses it."""

    @function_tool
    def send_email_tool(subject: str, text_body: str, html_body: str) -> str:
        """
        Send out an email with the given subject and body

        Args:
            subject: The subject of the email
            text_body: The body of the email as plain text
            html_body: The HTML body of the email
        """
        send_email(subject, text_body, html_body, to=recipient)
        return "Email sent successfully"

    return Agent(
        name="Email Agent",
        instructions=EMAIL_INSTRUCTIONS,
        tools=[send_email_tool],
        model=model,
        model_settings=ModelSettings(tool_choice="required"),
    )


__all__ = ["make_questioner", "make_planner", "make_searcher", "make_writer", "make_emailer", "push",
           "render_context", "ClarifyingTopic", "WebSearchItem", "WebSearchPlan", "ReportData"]
