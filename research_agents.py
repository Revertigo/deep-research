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


__all__ = ["make_planner", "make_searcher", "make_writer", "make_emailer", "push",
           "WebSearchItem", "WebSearchPlan", "ReportData"]
