#!/usr/bin/env python3
"""
research_memo.py — Run the research pipeline without scoring or go/no-go verdict.

Produces a structured company briefing covering all seven dimensions with full
qualitative depth but no ratings, no recommendation, and no calibration gates.
Use this for initial research before committing to a full audit.

Usage:
    python research_memo.py <notion_page_id_or_url> [--deck-path /path/to/deck.pdf]

Examples:
    python research_memo.py 1d4a7b64de5a42759026eaecc56c0966
    python research_memo.py "https://www.notion.so/workspace/PageTitle-1d4a7b64de5a42759026eaecc56c0966"
    python research_memo.py 1d4a7b64de5a42759026eaecc56c0966 --deck-path ~/Downloads/deck.pdf
"""

import argparse
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
    update_status,
    write_research_memo,
)
from utils.prompts import RESEARCH_SYSTEM_PROMPT, RESEARCH_PROMPT
from utils.research_agent import run_research_agent

_REQUIRED_ENV = ["NOTION_TOKEN", "ANTHROPIC_API_KEY", "EXA_API_KEY", "PERPLEXITY_API_KEY"]


def _validate_env() -> None:
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing required environment variable(s): {', '.join(missing)}")
        sys.exit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a research briefing (no scores, no verdict) for a Notion deal page."
    )
    parser.add_argument("page_id", help="Notion page ID or URL for the deal row")
    parser.add_argument(
        "--deck-path",
        metavar="PATH",
        help="Local path to a PDF pitch deck (overrides Deck Link in Notion)",
    )
    return parser.parse_args()


def generate_research_memo(
    deal: dict,
    research: str,
    deck_block: dict | None = None,
) -> str:
    prompt_text = RESEARCH_PROMPT.format(
        company_name=deal["company_name"],
        founder_name=deal["founder_name"],
        founder_linkedin=deal.get("founder_linkedin", ""),
        company_website=deal.get("company_website", ""),
        stage=deal["stage"],
        raise_amount=deal["raise_amount"],
        sector=deal["sector"],
        one_liner=deal["one_liner"],
        company_overview=deal.get("company_overview", "").strip() or "(not provided)",
        research=research,
    )

    if deck_block:
        prompt_text = (
            "The founder's pitch deck is attached as a document above. "
            "Read it carefully and reference specific claims, slide data, "
            "or stated metrics in your briefing wherever relevant. "
            "Treat the deck as primary source material.\n\n"
            + prompt_text
        )
        content = [deck_block, {"type": "text", "text": prompt_text}]
    else:
        content = prompt_text

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=RESEARCH_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


def main() -> None:
    _validate_env()
    args = parse_args()

    sep = "=" * 60
    print(f"\n{sep}")
    print("DEAL FLOW RESEARCH PIPELINE")
    print(f"{sep}")
    print(f"Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

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
        print("  Deck ready. Claude will reference it in the briefing.")
    else:
        print("  Proceeding without deck.")

    print("\n[3/5] Setting status → 'In Analysis'...")
    update_status(page_id, "In Analysis")
    print("  Done.")

    print("\n[4/5] Running agentic research (iterative searches — up to 20 tool calls)...")
    research = run_research_agent(deal)
    search_lines = [l for l in research.splitlines() if l.strip()]
    print(f"  Research complete — {len(research):,} chars, {len(search_lines)} lines")

    print(f"\n[5/5] Generating research briefing (model: {os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-6')})...")
    if deck_block:
        print("  Deck included in Claude context.")
    memo = generate_research_memo(deal, research, deck_block=deck_block)
    print(f"  Briefing generated — {len(memo):,} characters")

    print("\n      Writing to Notion (Research Memo field)...")
    write_research_memo(page_id, memo)
    update_status(page_id, "Pending Review")
    print("      Written. Status reset → 'Pending Review'.")

    print(f"\n{sep}")
    print(f"DONE — {deal['company_name']}")
    print("Research briefing written to the 'Research Memo' field in Notion.")
    print(f"{sep}\n")

    print("BRIEFING PREVIEW (first 600 chars):")
    print("-" * 40)
    print(memo[:600] + ("..." if len(memo) > 600 else ""))
    print()


if __name__ == "__main__":
    main()
