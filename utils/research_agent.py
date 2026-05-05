"""
Agentic research loop for the deal flow pipeline.

Replaces the fixed 5-parallel-search approach with an iterative Claude agent
that decides what to look up next based on what it finds — following leads on
founders, digging into specific competitors, drilling on funding rounds, etc.
"""

from __future__ import annotations

import os

import anthropic

from .research import exa_search_sync, perplexity_search_sync

_MAX_TOOL_ITERATIONS = 25
_FORCE_REPORT_AT = 20

_SYSTEM = """
You are a venture capital research analyst preparing intelligence for an investment memo.
Gather comprehensive, high-quality signal on an early-stage company before handing off
to the analyst who writes the memo.

You have two tools:
- exa_search: finds specific pages — company sites, founder profiles, press coverage,
  competitor pages, product directories, funding announcements, app store listings.
  Best for: primary source material on specific named entities.
- perplexity_search: synthesizes broader intelligence — market sizing, VC funding trends,
  regulatory landscape, consumer behavior shifts, category overviews.
  Best for: TAM/CAGR numbers, who's investing in the space, macro trends.

RESEARCH MANDATE — gather strong signal on ALL five areas before finishing:

1. COMPANY SIGNAL
   Evidence of traction: user counts, revenue, app store presence, waitlists, press coverage.
   What does the product actually do and how do users engage with it?

2. FOUNDER BACKGROUND
   Work history, prior companies founded, exits, domain expertise, clinical credentials,
   community access. Search the founder by name and by prior company.

3. COMPETITIVE LANDSCAPE
   Direct competitors solving the same problem for the same customer.
   For each significant competitor: how much have they raised, what's their estimated revenue
   or user count, and what is their current funding stage?
   Adjacent players that could expand into this space. Incumbent threats.

4. MARKET SIZE & TIMING
   TAM with real dollar figures and CAGR. Which Medicine 3.0 tailwind applies:
   longevity, metabolic health, AI health intelligence, mental health, behavior change,
   identity-as-health. Is the cultural moment right now or already peaked?

5. SECTOR VC SIGNALS
   Recent funding activity and notable deals (last 12–18 months).
   Which top-tier VCs are most active? Regulatory tailwinds or headwinds.
   Consumer behavior shifts driving the category.

APPROACH:
- Follow threads. If you find a prior company the founder built, search it specifically.
  If you find a competitor, look up their latest funding round and traction separately.
- For pre-seed companies, direct coverage will be thin — dig hard on the CATEGORY and SPACE.
- Run 10–18 searches before producing your report. Fewer than 8 is insufficient.
- Note explicitly where signal is absent. Do not fill gaps with assumptions.

FINAL REPORT FORMAT:
When you have enough signal, write a structured report with these five sections as headers.
Under each, write specific findings with source URLs inline in parentheses.
Be direct — no hedging, no preamble. This feeds directly into a VC investment memo.

COMPANY SIGNAL
FOUNDER BACKGROUND
COMPETITIVE LANDSCAPE
MARKET & TAM
SECTOR VC SIGNALS
"""

_TOOLS: list[dict] = [
    {
        "name": "exa_search",
        "description": (
            "Search the web for specific pages: company sites, founder profiles, "
            "press coverage, competitor pages, product directories, funding announcements. "
            "Best for finding primary source material on specific named entities."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query. Use specific names, quotes for exact phrases, "
                        "site: operator for targeted lookups."
                    ),
                },
                "num_results": {
                    "type": "integer",
                    "description": "Results to fetch (3–8). Use 3 for focused lookups, 7–8 for landscape searches.",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "perplexity_search",
        "description": (
            "Synthesize information using Perplexity AI. Best for: market sizing (TAM/CAGR), "
            "VC funding trends in a space, regulatory landscape, consumer behavior shifts, "
            "category overviews. Returns synthesized paragraphs with source citations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Research question requiring synthesized, data-backed answers.",
                },
            },
            "required": ["query"],
        },
    },
]


def run_research_agent(deal: dict) -> str:
    """
    Run an iterative research agent on a deal and return a structured research report.

    The agent decides what to search for based on what it finds, following leads
    on founders, competitors, and funding rounds rather than running fixed queries.
    Returns a structured text report with five sections ready for memo generation.
    """
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    client = anthropic.Anthropic()

    overview = deal.get("company_overview", "").strip()
    overview_block = f"\nFOUNDER-PROVIDED OVERVIEW:\n{overview}\n" if overview else ""

    seed_message = (
        f"Research this deal for a VC investment audit:\n\n"
        f"Company: {deal['company_name']}\n"
        f"Founder: {deal.get('founder_name', 'Unknown')}\n"
        f"Founder LinkedIn: {deal.get('founder_linkedin', '')}\n"
        f"Website: {deal.get('company_website', '')}\n"
        f"Stage: {deal.get('stage', 'Unknown')}\n"
        f"Raise: {deal.get('raise_amount', 'Unknown')}\n"
        f"Sector: {deal.get('sector', 'Unknown')}\n"
        f"One-liner: {deal.get('one_liner', '')}\n"
        f"{overview_block}\n"
        f"Gather comprehensive intelligence across all five research areas. "
        f"Run at least 10 searches before producing your report."
    )

    messages: list[dict] = [{"role": "user", "content": seed_message}]
    tool_call_count = 0

    while tool_call_count < _MAX_TOOL_ITERATIONS:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=_SYSTEM,
            tools=_TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return "[Research agent produced no text output]"

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_call_count += 1
            query_preview = block.input.get("query", "")[:80]
            print(f"    [search {tool_call_count:02d}] {block.name}: {query_preview}")

            if block.name == "exa_search":
                result = exa_search_sync(
                    block.input["query"],
                    num_results=block.input.get("num_results", 5),
                )
            elif block.name == "perplexity_search":
                result = perplexity_search_sync(block.input["query"])
            else:
                result = f"[Unknown tool: {block.name}]"

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

        if tool_call_count >= _FORCE_REPORT_AT:
            print(f"    [force] Search budget reached ({tool_call_count} calls) — forcing final report.")
            messages.append({
                "role": "user",
                "content": (
                    "You have used your search budget. Stop calling tools now. "
                    "Write your final structured research report immediately based on everything gathered. "
                    "Do not make any more tool calls."
                ),
            })
            final_response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=_SYSTEM,
                messages=messages,
            )
            for block in final_response.content:
                if hasattr(block, "text"):
                    return block.text
            break

    return "[Research agent reached iteration limit without producing a final report]"
