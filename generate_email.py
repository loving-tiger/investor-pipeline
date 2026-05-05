#!/usr/bin/env python3
"""
generate_email.py — Generate a custom investor outreach email for a Go deal.

Usage:
    python generate_email.py <notion_page_id_or_url> [--investor-type angel|venture-studio|fund]

Examples:
    python generate_email.py 1d4a7b64de5a42759026eaecc56c0966
    python generate_email.py "https://www.notion.so/workspace/PageTitle-1d4a7b64de5a42759026eaecc56c0966"
    python generate_email.py 1d4a7b64de5a42759026eaecc56c0966 --investor-type angel
"""

import argparse
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import anthropic

from utils.notion_client import get_deal, read_page_text, resolve_page_id, update_status, write_email
from utils.prompts import EMAIL_GENERATION_PROMPT, EMAIL_SYSTEM_PROMPT

_REQUIRED_ENV = ["NOTION_TOKEN", "ANTHROPIC_API_KEY"]


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
        description="Generate a custom investor outreach email for a deal in Notion."
    )
    parser.add_argument("page_id", help="Notion page ID for the deal")
    parser.add_argument(
        "--investor-type",
        choices=["angel", "venture-studio", "fund"],
        default="fund",
        help="Type of investor to write the email for (default: fund)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Email generation
# ---------------------------------------------------------------------------

_TRACTION_KEYWORDS = {"ARR", "revenue", "users", "subscribers", "retention", "churn", "growth", "paid", "MRR"}

def _extract_traction_signal(memo: str) -> str:
    """Return the first sentence from the memo that contains a traction metric."""
    for sentence in memo.replace("\n", " ").split(". "):
        if any(kw.lower() in sentence.lower() for kw in _TRACTION_KEYWORDS):
            return sentence.strip()[:300]
    return ""


def generate_email(deal: dict, investor_type: str) -> str:
    """Call Claude to write a custom investor outreach email."""
    # Read memo from page body (no longer stored as a property)
    memo_summary = read_page_text(deal["page_id"])[:3000].strip()
    if not memo_summary:
        memo_summary = (
            f"No memo available. One-liner: {deal['one_liner']}. "
            f"Run analyze_deal.py first for a richer output."
        )

    # Extract a short traction signal from the memo (first ~400 chars that
    # contain a metric keyword), falling back to the one-liner.
    traction_signal = _extract_traction_signal(memo_summary) or deal.get("one_liner", "")

    prompt = EMAIL_GENERATION_PROMPT.format(
        company_name=deal["company_name"],
        founder_name=deal["founder_name"],
        stage=deal["stage"],
        raise_amount=deal["raise_amount"],
        sector=deal["sector"],
        one_liner=deal["one_liner"],
        traction_signal=traction_signal,
        memo_summary=memo_summary,
        investor_type=investor_type,
    )

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=EMAIL_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _validate_env()
    args = parse_args()
    investor_type = args.investor_type

    # Resolve the page ID before printing so the log always shows the clean UUID
    page_id = resolve_page_id(args.page_id)

    sep = "=" * 60
    print(f"\n{sep}")
    print("INVESTOR EMAIL GENERATION")
    print(f"{sep}")
    print(f"Page ID       : {page_id}")
    print(f"Investor type : {investor_type}")
    print(f"Started       : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 1. Fetch deal
    print("[1/4] Fetching deal from Notion...")
    deal = get_deal(page_id)
    if not deal["company_name"]:
        print("  ERROR: Could not find a 'Company Name'. Check the page ID.")
        sys.exit(1)
    print(f"  Company : {deal['company_name']}")
    print(f"  Status  : {deal['status'] or '(not set)'}")

    # Warn (but don't block) if status isn't Go
    if deal["status"] not in ("Go", "Emailed"):
        print(
            f"\n  WARNING: Status is '{deal['status']}' — expected 'Go'.\n"
            "  Proceeding. Flip to 'Go' in Notion when you're ready to send."
        )

    # 2. Check memo
    print("\n[2/4] Checking investment memo...")
    if deal.get("investment_memo"):
        print(f"  Found memo ({len(deal['investment_memo']):,} chars) — will use as context.")
    else:
        print("  No memo found. Run analyze_deal.py first for best results.")

    # 3. Generate email
    print(f"\n[3/4] Generating {investor_type} outreach email...")
    email = generate_email(deal, investor_type)
    print(f"  Email generated — {len(email):,} characters")

    # 4. Write to Notion
    print("\n[4/4] Writing draft email to Notion...")
    write_email(page_id, email)
    update_status(page_id, "Go")
    print("  Draft written. Status set → 'Go'.")

    print(f"\n{sep}")
    print(f"DONE — {deal['company_name']} ({investor_type})")
    print("Review the draft in Notion before sending.")
    print(f"{sep}\n")

    # Print the email to the terminal
    print("DRAFT EMAIL:")
    print("-" * 40)
    print(email)
    print("-" * 40)
    print()


if __name__ == "__main__":
    main()
