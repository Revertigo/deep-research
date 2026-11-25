"""Email built in code: the mail carries what the page shows, whole, in page order.

No OpenAI calls and no SMTP: messenger.send_email is a fake that records its arguments.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import research_manager  # noqa: E402
from research_agents import ReportData  # noqa: E402

SUMMARY = "EV adoption in Europe doubled since 2020, led by Norway & the Netherlands."
FOLLOW_UPS = ["How do charging networks compare?", "What about <used> EV prices?", "Impact of 2035 ban?"]
REPORT_MD = """# EV adoption in Europe

## Headline numbers

Sales **doubled** between 2020 and 2024.

| Country | Share |
| --- | ---: |
| Norway | 89% |
| Netherlands | 35% |

```python
share = evs / total
```

- Incentives drove early growth
- Charging density lags in the south

> Policy, not price, set the pace.
"""


def report(markdown: str = REPORT_MD, summary: str = SUMMARY, follow_ups: list[str] | None = None) -> ReportData:
    return ReportData(short_summary=summary, markdown_report=markdown,
                      follow_up_questions=FOLLOW_UPS if follow_ups is None else follow_ups)


@pytest.fixture
def sent(monkeypatch):
    recorded: list[tuple[str, str, str, str | None]] = []

    def fake_send_email(subject, text_body, html_body, to=None):
        recorded.append((subject, text_body, html_body, to))

    monkeypatch.setattr(research_manager.messenger, "send_email", fake_send_email)
    return recorded


def send(rm: research_manager.ResearchManager, rep: ReportData) -> None:
    asyncio.new_event_loop().run_until_complete(rm.send_email(rep))


# --- Subject ------------------------------------------------------------------

def test_subject_from_first_h1():
    assert research_manager.derive_subject(report()) == "Deep Research: EV adoption in Europe"


def test_subject_falls_back_to_first_summary_line():
    rep = report(markdown="## Only an H2\n\ntext", summary="First line.\nSecond line.")
    assert research_manager.derive_subject(rep) == "Deep Research: First line."


def test_subject_truncated_to_about_80_chars():
    rep = report(markdown="# " + "word " * 40)
    subject = research_manager.derive_subject(rep)
    assert subject.startswith("Deep Research: word word")
    assert len(subject) <= len("Deep Research: ") + 80
    assert subject.endswith("…")


# --- Body: page order, nothing dropped ----------------------------------------

def test_send_email_passes_full_report_and_mirrors_page(sent):
    rm = research_manager.ResearchManager(model="gpt-5.4-mini", email_to="a@b.co")
    send(rm, report())

    assert len(sent) == 1
    subject, text_body, html_body, to = sent[0]
    assert to == "a@b.co"
    assert subject == "Deep Research: EV adoption in Europe"

    # text_body: summary, then the full markdown report untouched, then the questions.
    assert REPORT_MD in text_body
    assert text_body.index(SUMMARY) < text_body.index(REPORT_MD) < text_body.index("Dig deeper")
    for q in FOLLOW_UPS:
        assert f"- {q}" in text_body

    # html_body: summary pull-quote, headings/table/code/list from the report, every follow-up question.
    assert "EV adoption in Europe doubled since 2020, led by Norway &amp; the Netherlands." in html_body
    assert "<h1" in html_body and "EV adoption in Europe</h1>" in html_body
    assert "Headline numbers</h2>" in html_body
    assert "<table" in html_body and "<td" in html_body and "Netherlands" in html_body
    assert "<pre" in html_body and "share = evs / total" in html_body
    assert "<strong>doubled</strong>" in html_body
    assert "<blockquote" in html_body and "Policy, not price, set the pace." in html_body
    assert "Dig deeper" in html_body
    for q in FOLLOW_UPS:
        assert research_manager.html.escape(q) in html_body
    # Order on the page: summary, report, follow-ups.
    assert html_body.index("led by Norway") < html_body.index("<h1") < html_body.index("Dig deeper")
    # Readable wrapper with inline CSS only.
    assert "max-width:720px" in html_body
    assert "<style" not in html_body and 'class="' not in html_body.replace('class="language-python"', "")


def test_long_report_arrives_whole(sent):
    paragraphs = [f"## Section {i}\n\n" + f"Paragraph {i} " * 60 for i in range(40)]
    long_md = "# Long report\n\n" + "\n\n".join(paragraphs)
    assert len(long_md) > 20_000
    rm = research_manager.ResearchManager(model="gpt-5.4-mini", email_to="a@b.co")
    send(rm, report(markdown=long_md, follow_ups=[]))

    subject, text_body, html_body, _ = sent[0]
    assert subject == "Deep Research: Long report"
    assert long_md in text_body
    for i in range(40):
        assert f"Section {i}</h2>" in html_body
        assert (f"Paragraph {i} " * 60).rstrip() + "</p>" in html_body  # CommonMark drops the trailing space
    assert "Dig deeper" not in html_body and "Dig deeper" not in text_body


def test_no_model_call_when_emailing(sent, monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("send_email must not call a model")

    monkeypatch.setattr(research_manager.Runner, "run", boom)
    rm = research_manager.ResearchManager(model="gpt-5.4-mini", email_to="a@b.co")
    send(rm, report())
    assert len(sent) == 1


# The writer's shape: numbered sections, each with nested bullets, then a Sources list of links.
# Bullets sit under the number's content column ("1. " -> 3 spaces, "- " -> 2 spaces), which is
# what marked (the page) nests; the mail must nest the same markdown the same way.
NESTED_MD = """# Nested report

1. **Market size.** Sales grew fast.
   - 1.2M units in 2024
   - Norway at 89% share
2. **Charging.** Density lags.
   - 70k public points
   - South of Europe behind, see [ACEA](https://www.acea.auto/report)
3. **Policy.** The 2035 ban holds.
   - Sub-bullet with 2-space nesting:
     - deeper
     - still deeper

## Sources

- [ACEA report](https://www.acea.auto/report)
- [IEA Global EV Outlook](https://www.iea.org/reports/global-ev-outlook-2024)
"""


def test_email_nests_lists_like_the_page():
    html_body = research_manager.markdown_to_html(NESTED_MD)
    # Every section's bullets live inside that section's <li>, not as a flat list after it.
    ordered = html_body[html_body.index("<ol") : html_body.index("</ol>")]
    sections = ordered.split("<li style=\"margin:0 0 6px\"><strong>")[1:]
    assert [s.split("</strong>")[0] for s in sections] == ["Market size.", "Charging.", "Policy."]
    for inner in sections:
        assert "<ul" in inner and inner.count("<li") >= 2 and inner.rstrip().endswith("</ul>\n</li>")
    assert html_body.count("<ol") == 1  # one ordered list, no flat <ul> restarts between sections
    assert html_body.count("<li") == 3 + 2 + 2 + 1 + 2 + 2  # sections, their bullets, sub-bullets, sources
    assert "deeper</li>" in html_body and "still deeper</li>" in html_body
    # Links survive as anchors, with our inline style.
    assert '<a style="color:#2563eb" href="https://www.acea.auto/report">ACEA</a>' in html_body
    assert html_body.count('href="https://www.acea.auto/report"') == 2
    assert 'href="https://www.iea.org/reports/global-ev-outlook-2024"' in html_body
    assert "Sources</h2>" in html_body


def test_paren_ordered_marker_is_a_list():
    html_body = research_manager.markdown_to_html("1) First\n   - a\n2) Second\n")
    assert html_body.count("<ol") == 1 and html_body.count("<li") == 3
    assert "<li" in html_body.split("</ol>")[0] and "<ul" in html_body.split("</ol>")[0]


def test_nested_lists_keep_hebrew_rtl():
    rep = report(markdown=NESTED_MD, follow_ups=[])
    _, _, html_body = research_manager.build_email(rep, "he")
    assert '<div dir="rtl" lang="he"' in html_body
    assert "padding-right:24px" in html_body and "padding-left:24px" not in html_body
    assert html_body.count("<ul") == html_body.count('<ul style="margin:0 0 14px;padding-right:24px;padding-left:0">')
    assert "<ol" in html_body.split("</ol>")[0] and "<ul" in html_body.split("</ol>")[0]


def test_raw_html_in_report_is_escaped():
    html_body = research_manager.markdown_to_html("text <script>alert(1)</script> more")
    assert "<script>" not in html_body and "&lt;script&gt;" in html_body


def test_table_alignment_kept_alongside_inline_style():
    html_body = research_manager.markdown_to_html("| a | b |\n| --- | ---: |\n| 1 | 2 |")
    assert html_body.count("<th ") == 2 and html_body.count("<td ") == 2
    assert 'style="text-align:right;border:' in html_body  # alignment merged into our style, not a second attribute
    assert html_body.count("style=") == 5  # table + 2 th + 2 td
    assert not research_manager.re.search(r"<\w+[^>]*style=[^>]*style=", html_body)
