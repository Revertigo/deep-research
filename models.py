"""Model catalog offered in the UI.

Prices are USD per 1M tokens (standard tier) from the OpenAI pricing page,
checked 2026-11-19. `est_cost` is a rough per-research estimate assuming
~25k input + ~10k output tokens across all agents; the web-search tool fee
is not included. Edit freely; the UI reads this list through /api/models.
"""

DEFAULT_MODEL = "gpt-5.4-mini"

# Number of parallel web searches the planner produces.
# Max is 12, not higher: every query comes from one planner call, and research
# on breadth scaling (arXiv 2606.17209, 2605.16217) shows parallel searches
# from a single plan grow redundant, so coverage flattens past ~8-10 while
# cost grows linearly (web search tool is $10 per 1k calls plus tokens).
DEFAULT_SEARCHES = 5
MAX_SEARCHES = 12
MIN_SEARCHES = 1

# Clarifying questions before planning. The round cap is enforced by code
# (server truncates, UI stops asking), never left to the model.
MAX_CLARIFY_ROUNDS = 2
MAX_QUESTIONS_PER_ROUND = 3

# Rough token model per research: fixed part (planner + writer + emailer)
# plus a per-search part (search agent in/out + its summary fed to the writer).
EST_FIXED_INPUT = 4_000
EST_FIXED_OUTPUT = 6_500
EST_PER_SEARCH_INPUT = 2_900
EST_PER_SEARCH_OUTPUT = 400
WEB_SEARCH_CALL_USD = 0.01  # $10 / 1k calls

MODELS = [
    # tier: cheap | balanced | strong | premium
    {"id": "gpt-5-nano",   "label": "GPT-5 nano",    "tier": "cheap",    "input": 0.05, "output": 0.40,
     "blurb": "Fastest and cheapest. Fine for quick, shallow scans."},
    {"id": "gpt-6-luna",   "label": "GPT-6 luna",    "tier": "cheap",    "input": 0.10, "output": 0.50,
     "blurb": "Smallest GPT-6. Newer than nano at nearly the same price."},
    {"id": "gpt-5.4-nano", "label": "GPT-5.4 nano",  "tier": "cheap",    "input": 0.20, "output": 1.25,
     "blurb": "Small model, current generation. Good default for cost-sensitive runs."},
    {"id": "gpt-5.6-luna", "label": "GPT-5.6 luna",  "tier": "cheap",    "input": 0.20, "output": 1.20,
     "blurb": "Small GPT-5.6. Same price as 5.4 nano, newer family."},
    {"id": "gpt-5-mini",   "label": "GPT-5 mini",    "tier": "cheap",    "input": 0.25, "output": 2.00,
     "blurb": "Previous-gen mini. Solid summaries at low cost."},
    {"id": "gpt-5.4-mini", "label": "GPT-5.4 mini",  "tier": "balanced", "input": 0.75, "output": 4.50,
     "blurb": "Default. Best quality per dollar for everyday research."},
    {"id": "gpt-6.1-terra", "label": "GPT-6.1 terra", "tier": "balanced", "input": 1.00, "output": 5.00,
     "blurb": "Newer family, mid-size. Stronger reasoning than mini."},
    {"id": "gpt-5.4",      "label": "GPT-5.4",       "tier": "strong",   "input": 2.50, "output": 15.00,
     "blurb": "Full-size model. Deeper synthesis, longer reports."},
    {"id": "gpt-6.2-sol",  "label": "GPT-6.2 sol",   "tier": "strong",   "input": 2.00, "output": 10.00,
     "blurb": "Latest full-size flagship-class model."},
    {"id": "gpt-6.2-astra", "label": "GPT-6.2 astra", "tier": "premium",  "input": 8.00, "output": 40.00,
     "blurb": "Top tier. Use for research you really care about."},
]


def estimate_cost(model: dict, searches: int = DEFAULT_SEARCHES) -> float:
    tokens_in = EST_FIXED_INPUT + EST_PER_SEARCH_INPUT * searches
    tokens_out = EST_FIXED_OUTPUT + EST_PER_SEARCH_OUTPUT * searches
    return (model["input"] * tokens_in + model["output"] * tokens_out) / 1_000_000 + WEB_SEARCH_CALL_USD * searches


def catalog() -> list[dict]:
    return [{**m, "est_cost": round(estimate_cost(m), 3), "default": m["id"] == DEFAULT_MODEL} for m in MODELS]


def cost_model() -> dict:
    """Coefficients so the UI can recompute the estimate live."""
    return {
        "fixed_input": EST_FIXED_INPUT, "fixed_output": EST_FIXED_OUTPUT,
        "per_search_input": EST_PER_SEARCH_INPUT, "per_search_output": EST_PER_SEARCH_OUTPUT,
        "web_search_call_usd": WEB_SEARCH_CALL_USD,
    }


def is_allowed(model_id: str) -> bool:
    return any(m["id"] == model_id for m in MODELS)
