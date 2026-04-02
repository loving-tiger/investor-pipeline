#!/usr/bin/env python3
"""
analyze_deal.py — Run VC audit pipeline on a Notion deal page.

Usage:
    python analyze_deal.py <notion_page_id_or_url> [--deck-path /path/to/deck.pdf]

Examples:
    python analyze_deal.py 1d4a7b64de5a42759026eaecc56c0966
    python analyze_deal.py "https://www.notion.so/workspace/PageTitle-1d4a7b64de5a42759026eaecc56c0966"
    python analyze_deal.py 1d4a7b64de5a42759026eaecc56c0966 --deck-path ~/Downloads/tonewell_deck.pdf

Deck handling:
    - If Deck Link in Notion is a direct public PDF URL, it's fetched automatically.
    - Pass --deck-path to override with a local PDF (use this for gated links like
      Google Drive, Docsend, Pitch — download the PDF first, then point here).
    - If no deck is available, the pipeline runs on web research alone.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import anthropic

from utils.deck import deck_is_gated, load_deck
from utils.notion_client import (
    get_deal,
    resolve_page_id,
    update_analysis_date,
    update_status,
    write_memo,
)
from utils.prompts import ANALYSIS_SYSTEM_PROMPT, VC_AUDIT_PROMPT
from utils.research import (
    search_company,
    search_competitors,
    search_founder,
    search_market,
    search_sector_signals,
)

_REQUIRED_ENV = ["NOTION_TOKEN", "ANTHROPIC_API_KEY", "EXA_API_KEY", "PERPLEXITY_API_KEY"]


def _validate_env() -> None:
    """Exit early with a clear message if any required env var is missing."""
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing required environment variable(s): {', '.join(missing)}")
        print("Check your .env file and make sure all keys are filled in.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze a founder deal from Notion using web research + Claude."
    )
    parser.add_argument("page_id", help="Notion page ID or URL for the deal row")
    parser.add_argument(
        "--deck-path",
        metavar="PATH",
        help="Local path to a PDF pitch deck (overrides Deck Link in Notion)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

async def run_research(deal: dict) -> tuple[str, str, str, str, str]:
    """Run 5 parallel searches and return results as a 5-tuple."""
    print("    Starting 5 parallel searches (2 Exa + 1 Exa founder + 2 Perplexity)...")
    results = await asyncio.gather(
        search_company(deal["company_name"], deal["company_website"]),
        search_founder(deal["founder_name"], deal["company_name"]),
        search_competitors(deal["sector"], deal["one_liner"]),
        search_market(deal["sector"], deal["one_liner"]),
        search_sector_signals(deal["sector"]),
        return_exceptions=True,
    )
    labels = ["Company research", "Founder research", "Competitor landscape", "Market & TAM", "Sector signals"]
    for label, result in zip(labels, results):
        status = "ERROR" if isinstance(result, Exception) else "done"
        print(f"    [{status}] {label}")

    return tuple(
        r if isinstance(r, str) else f"[Search failed: {r}]" for r in results
    )


# ---------------------------------------------------------------------------
# Memo generation
# ---------------------------------------------------------------------------

def generate_memo(
    deal: dict,
    research: tuple[str, str, str, str, str],
    deck_block: dict | None = None,
) -> str:
    """Call Claude with the VC audit prompt (+ optional deck) and return memo text."""
    company_research, founder_research, competitor_research, market_research, sector_signals = research

    prompt_text = VC_AUDIT_PROMPT.format(
        company_name=deal["company_name"],
        founder_name=deal["founder_name"],
        stage=deal["stage"],
        raise_amount=deal["raise_amount"],
        sector=deal["sector"],
        one_liner=deal["one_liner"],
        company_research=company_research,
        founder_research=founder_research,
        competitor_research=competitor_research,
        market_research=market_research,
        sector_signals=sector_signals,
    )

    # If a deck is available, prepend it as a document block and instruct
    # Claude to reference it directly in the evaluation.
    if deck_block:
        prompt_text = (
            "The founder's pitch deck is attached as a document above. "
            "Read it carefully and reference specific claims, slide data, "
            "or stated metrics in your evaluation wherever relevant. "
            "Treat the deck as primary source material — prioritize what the "
            "deck actually shows over what web research suggests.\n\n"
            + prompt_text
        )
        content = [deck_block, {"type": "text", "text": prompt_text}]
    else:
        content = prompt_text

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=ANALYSIS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    _validate_env()
    args = parse_args()

    sep = "=" * 60
    print(f"\n{sep}")
    print("DEAL FLOW ANALYSIS PIPELINE")
    print(f"{sep}")
    print(f"Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 1. Fetch deal from Notion
    print("[1/5] Fetching deal from Notion...")
    page_id = resolve_page_id(args.page_id)
    deal = get_deal(page_id)
    if not deal["company_name"]:
        print("  ERROR: Could not find 'Company Name' on this page. Check the page ID.")
        sys.exit(1)
    print(f"  Company : {deal['company_name']}")
    print(f"  Founder : {deal['founder_name'] or '(not set)'}")
    print(f"  Stage   : {deal['stage'] or '(not set)'}  |  Sector: {deal['sector'] or '(not set)'}")
    print(f"  Raise   : {deal['raise_amount'] or '(not set)'}")
    print(f"  Deck    : {deal['deck_link'] or '(none in Notion)'}")

    # 2. Load pitch deck (local override > Notion URL > none)
    print("\n[2/5] Loading pitch deck...")
    deck_block = None
    if args.deck_path:
        deck_block = load_deck(deck_path=args.deck_path)
    elif deal["deck_link"]:
        if deck_is_gated(deal["deck_link"]):
            print(f"  [deck] Deck link requires authentication — skipping auto-fetch.")
            print(f"         Download it as PDF and re-run with --deck-path to include it.")
        else:
            deck_block = load_deck(deck_url=deal["deck_link"])
    else:
        print("  No deck available — running on web research alone.")

    if deck_block:
        print("  Deck ready. Claude will reference it in the evaluation.")
    else:
        print("  Proceeding without deck.")

    # 3. Mark In Analysis
    print("\n[3/5] Setting status → 'In Analysis'...")
    update_status(page_id, "In Analysis")
    update_analysis_date(page_id)
    print("  Done.")

    # 4. Parallel web research
    print("\n[4/5] Running web research (5 parallel searches)...")
    research = await run_research(deal)

    # 5. Generate memo with Claude
    print(f"\n[5/5] Generating investment memo (model: {os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-6')})...")
    if deck_block:
        print("  Deck included in Claude context.")
    memo = generate_memo(deal, research, deck_block=deck_block)
    print(f"  Memo generated — {len(memo):,} characters")

    # Write back to Notion
    print("\n      Writing memo to Notion...")
    write_memo(page_id, memo)
    update_status(page_id, "Pending Review")
    print("      Written. Status reset → 'Pending Review'.")

    print(f"\n{sep}")
    print(f"DONE — {deal['company_name']}")
    print("Review the memo in Notion, then flip Status to Go or No-go.")
    print(f"{sep}\n")

    print("MEMO PREVIEW (first 600 chars):")
    print("-" * 40)
    print(memo[:600] + ("..." if len(memo) > 600 else ""))
    print()


if __name__ == "__main__":
    asyncio.run(main())
