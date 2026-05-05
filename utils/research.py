"""
Web research functions for the deal flow pipeline.

Routing:
  - search_company      →  Exa, no domain filter  (cast wide net for unknown startups)
  - search_founder      →  Exa, no domain filter  (founder background, LinkedIn, press)
  - search_competitors  →  Exa, open domains      (search the SPACE, not "competitors to X")
  - search_market       →  Perplexity sonar-pro   (TAM, CAGR, growth drivers)
  - search_sector_signals → Perplexity sonar-pro  (VC activity, regulatory trends)

Design notes:
  - Pre-seed companies rarely appear in major publications. Domain restrictions
    that improve quality for known companies hurt recall for unknown ones.
    Company and founder searches cast a wide net intentionally.
  - Competitor search queries the PRODUCT CATEGORY (one_liner + sector), not
    "competitors to [company name]" — the latter returns nothing for unknowns.
  - Crunchbase, Tracxn, PitchBook are excluded from Exa domain lists — they
    require auth and return server errors or empty results.
"""

from __future__ import annotations

import asyncio
import os

from exa_py import Exa
from openai import OpenAI

_exa = None
_perplexity = None

_PERPLEXITY_MODEL = "sonar-pro"

_MARKET_SYSTEM_PROMPT = (
    "You are a market research analyst. Provide specific, data-backed answers. "
    "Always include: market size in dollars, CAGR, the 3–5 most important growth "
    "drivers, and notable recent funding or M&A activity in the space. "
    "Cite sources inline. Be concise — no preamble."
)

_SECTOR_SYSTEM_PROMPT = (
    "You are a venture capital analyst tracking investment trends. "
    "Provide specific, data-backed answers. Always include: recent VC funding "
    "activity and notable deals, regulatory tailwinds or headwinds, consumer "
    "behavior shifts, and which top-tier funds are most active in this space. "
    "Cite sources inline. Be concise — no preamble."
)


# ---------------------------------------------------------------------------
# Client singletons
# ---------------------------------------------------------------------------

def _exa_client() -> Exa:
    global _exa
    if _exa is None:
        _exa = Exa(api_key=os.environ["EXA_API_KEY"])
    return _exa


def _perplexity_client() -> OpenAI:
    global _perplexity
    if _perplexity is None:
        _perplexity = OpenAI(
            api_key=os.environ["PERPLEXITY_API_KEY"],
            base_url="https://api.perplexity.ai",
        )
    return _perplexity


# ---------------------------------------------------------------------------
# Public async search functions
# ---------------------------------------------------------------------------

async def search_company(company_name: str, website: str) -> str:
    """
    Exa: find any web coverage of the company — press, product pages, announcements.

    Uses exact-match quoting and no domain filter so early-stage companies with
    minimal press coverage still surface whatever exists (blog posts, podcast
    mentions, LinkedIn company pages, product directories, etc.).
    """
    query = f'"{company_name}" company product launch startup funding announcement'
    if website:
        query += f" OR site:{website}"
    return await _exa_search(query, num_results=6)


async def search_founder(founder_name: str, company_name: str) -> str:
    """
    Exa: find founder background — LinkedIn, interviews, prior companies, press.

    Separated from company search so founder signal isn't diluted by company
    noise (or absence). For pre-seed deals, this is often the only verifiable
    external signal.
    """
    query = (
        f'"{founder_name}" founder CEO entrepreneur background experience '
        f'"{company_name}" OR startup OR previously OR founded'
    )
    return await _exa_search(query, num_results=5)


async def search_competitors(sector: str, one_liner: str) -> str:
    """
    Exa: map the competitive landscape by searching the PRODUCT SPACE, not the company.

    Searching "competitors to [unknown startup]" returns nothing. Searching the
    product category (derived from one_liner + sector) finds actual players in
    the space regardless of whether the subject company is indexed anywhere.
    """
    query = (
        f"{sector} {one_liner} startup app company 2023 2024 2025 "
        f"funding raised alternative solution"
    )
    return await _exa_search(query, num_results=7)


async def search_market(sector: str, one_liner: str) -> str:
    """Perplexity: synthesize market size, TAM, CAGR, and key growth drivers."""
    query = (
        f"What is the total addressable market (TAM) for the {sector} industry in 2025? "
        f"Include market size in dollars, CAGR, the top growth drivers, and key trends. "
        f"Also provide context for a company focused on: {one_liner}"
    )
    return await _perplexity_search(_MARKET_SYSTEM_PROMPT, query)


async def search_sector_signals(sector: str) -> str:
    """Perplexity: synthesize VC activity, regulatory tailwinds, and sector trends."""
    query = (
        f"What are the key venture capital trends, regulatory tailwinds or headwinds, "
        f"and consumer behavior shifts in the {sector} space in 2024–2025? "
        f"Which top-tier VC funds are most active, and what notable deals have closed recently?"
    )
    return await _perplexity_search(_SECTOR_SYSTEM_PROMPT, query)


# ---------------------------------------------------------------------------
# Sync search functions (for tool-calling loops)
# ---------------------------------------------------------------------------

def exa_search_sync(query: str, num_results: int = 5) -> str:
    """Synchronous Exa search for use in Claude tool-calling loops."""
    try:
        results = _exa_client().search_and_contents(
            query,
            num_results=num_results,
            text={"max_characters": 2000},
        )
        return _format_exa(results)
    except Exception as exc:
        return f"[Exa search error: {exc}]"


def perplexity_search_sync(query: str) -> str:
    """Synchronous Perplexity search for use in Claude tool-calling loops."""
    system = (
        "You are a research analyst. Provide specific, data-backed answers. "
        "Include numbers, dates, and named entities. Cite sources inline. "
        "Be concise — no preamble."
    )
    try:
        response = _perplexity_client().chat.completions.create(
            model=_PERPLEXITY_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": query},
            ],
        )
        content = response.choices[0].message.content or ""
        citations = getattr(response, "citations", None)
        if citations:
            content += "\n\nSources:\n" + "\n".join(f"- {c}" for c in citations)
        return content
    except Exception as exc:
        return f"[Perplexity search error: {exc}]"


# ---------------------------------------------------------------------------
# Exa internals
# ---------------------------------------------------------------------------

async def _exa_search(
    query: str,
    num_results: int = 5,
    include_domains: list[str] | None = None,
) -> str:
    """Run an Exa search in a thread pool (Exa client is synchronous)."""
    loop = asyncio.get_running_loop()
    kwargs: dict = dict(
        num_results=num_results,
        text={"max_characters": 1500},
    )
    if include_domains:
        kwargs["include_domains"] = include_domains

    try:
        results = await loop.run_in_executor(
            None,
            lambda: _exa_client().search_and_contents(query, **kwargs),
        )
        return _format_exa(results)
    except Exception as exc:
        return f"[Exa search error: {exc}]"


def _format_exa(results) -> str:
    """Format Exa results into plain text for the Claude prompt."""
    if not results.results:
        return "No results found."
    sections = []
    for r in results.results:
        body = getattr(r, "text", "") or ""
        pub = getattr(r, "published_date", "") or ""
        header = f"Source: {r.url}"
        if pub:
            header += f"  ({pub[:10]})"
        sections.append(f"{header}\nTitle: {r.title}\n{body.strip()}")
    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# Perplexity internals
# ---------------------------------------------------------------------------

async def _perplexity_search(system_prompt: str, user_query: str) -> str:
    """Run a Perplexity sonar-pro query in a thread pool."""
    loop = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(
            None,
            lambda: _perplexity_client().chat.completions.create(
                model=_PERPLEXITY_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_query},
                ],
            ),
        )
        content = response.choices[0].message.content or ""
        citations = getattr(response, "citations", None)
        if citations:
            cite_block = "\n\nSources:\n" + "\n".join(f"- {c}" for c in citations)
            content += cite_block
        return content
    except Exception as exc:
        return f"[Perplexity search error: {exc}]"
