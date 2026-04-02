#!/usr/bin/env python3
"""
setup_typeform.py — Create the Deal Flow intake form on Typeform via API.

Creates the form with all fields pre-wired to the correct refs that
webhook.py expects, then registers the webhook pointing at your Render URL.

Usage:
    python3 setup_typeform.py --render-url https://your-app.onrender.com

After running:
    1. Copy the printed TYPEFORM_WEBHOOK_SECRET into your .env and Render env vars
    2. Copy the form URL to share with founders
    3. Test by submitting the form and checking your Notion database

Requires:
    TYPEFORM_API_TOKEN in .env (get from typeform.com > Account > Personal tokens)
"""

import argparse
import os
import secrets
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

TYPEFORM_API = "https://api.typeform.com"


# ---------------------------------------------------------------------------
# Form definition
# ---------------------------------------------------------------------------

FORM_FIELDS = [
    {
        "title": "What's the name of your company?",
        "ref": "company_name",
        "type": "short_text",
        "validations": {"required": True},
    },
    {
        "title": "Give us your one-liner. What does your company do, and for whom?",
        "ref": "one_liner",
        "type": "long_text",
        "validations": {"required": True},
        "properties": {"description": "Example: 'We help busy parents track their kids' nutrition with an AI-powered food scanner.'"},
    },
    {
        "title": "What's your name?",
        "ref": "founder_name",
        "type": "short_text",
        "validations": {"required": True},
    },
    {
        "title": "What stage are you at?",
        "ref": "stage",
        "type": "dropdown",
        "validations": {"required": True},
        "properties": {
            "choices": [
                {"label": "Pre-seed"},
                {"label": "Seed"},
                {"label": "Series A"},
                {"label": "Late stage"},
            ]
        },
    },
    {
        "title": "What sector best describes your company?",
        "ref": "sector",
        "type": "dropdown",
        "validations": {"required": True},
        "properties": {
            "choices": [
                {"label": "Consumer Health"},
                {"label": "Fitness"},
                {"label": "Mental Health"},
                {"label": "Longevity"},
                {"label": "Nutrition"},
                {"label": "Other"},
            ]
        },
    },
    {
        "title": "How much are you raising, and on what terms?",
        "ref": "raise_amount",
        "type": "short_text",
        "validations": {"required": False},
        "properties": {"description": "Example: '$2M SAFE at $10M cap'"},
    },
    {
        "title": "Company website",
        "ref": "company_website",
        "type": "website",
        "validations": {"required": False},
    },
    {
        "title": "Your LinkedIn profile URL",
        "ref": "founder_linkedin",
        "type": "website",
        "validations": {"required": True},
    },
    {
        "title": "Your email address",
        "ref": "founder_email",
        "type": "email",
        "validations": {"required": True},
    },
    {
        "title": "Link to your pitch deck (optional)",
        "ref": "deck_link",
        "type": "website",
        "validations": {"required": False},
        "properties": {"description": "Google Drive, Docsend, Notion, or a direct PDF link"},
    },
    {
        "title": "Tell us more about the company — traction, team background, what you've built so far.",
        "ref": "company_overview",
        "type": "long_text",
        "validations": {"required": False},
        "properties": {"description": "Include any revenue, user numbers, or notable milestones."},
    },
]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create the Deal Flow Typeform and register the webhook.")
    parser.add_argument(
        "--render-url",
        required=True,
        metavar="URL",
        help="Your Render service URL, e.g. https://deal-flow-webhook.onrender.com",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    token = os.environ.get("TYPEFORM_API_TOKEN")
    if not token:
        print("ERROR: TYPEFORM_API_TOKEN not set in .env")
        sys.exit(1)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _post(path: str, body: dict) -> dict:
    resp = requests.post(f"{TYPEFORM_API}{path}", json=body, headers=_headers())
    if not resp.ok:
        print(f"ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)
    return resp.json()


def _put(path: str, body: dict) -> dict:
    resp = requests.put(f"{TYPEFORM_API}{path}", json=body, headers=_headers())
    if not resp.ok:
        print(f"ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)
    return resp.json()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    render_url = args.render_url.rstrip("/")
    webhook_url = f"{render_url}/webhook/typeform"

    # 1. Create the form
    print("Creating Typeform...")
    form = _post("/forms", {
        "title": "Fitt Capital — Founder Deal Flow",
        "fields": FORM_FIELDS,
        "welcome_screens": [
            {
                "title": "Tell us about your company",
                "properties": {
                    "description": (
                        "We review every submission. Fill this out honestly — "
                        "we value directness over polish. Takes about 3 minutes."
                    ),
                    "button_text": "Let's go",
                    "show_button": True,
                },
            }
        ],
        "thankyou_screens": [
            {
                "title": "Thanks — we'll be in touch.",
                "properties": {
                    "description": "We review every deal personally. If there's a fit, you'll hear from us within a few days.",
                    "show_button": False,
                },
            }
        ],
        "settings": {
            "is_public": True,
            "is_trial": False,
            "language": "en",
            "progress_bar": "percentage",
            "show_progress_bar": True,
            "show_typeform_branding": True,
        },
    })

    form_id = form["id"]
    form_url = f"https://{form.get('_links', {}).get('display', f'form.typeform.com/to/{form_id}')}"
    # Typeform returns the link in _links.display
    display_link = form.get("_links", {}).get("display", f"https://form.typeform.com/to/{form_id}")
    print(f"  Form created: {form_id}")
    print(f"  Form URL: {display_link}")

    # 2. Register the webhook with a freshly generated secret
    webhook_secret = secrets.token_hex(32)
    print(f"\nRegistering webhook → {webhook_url}")
    _put(f"/forms/{form_id}/webhooks/deal-flow", {
        "url": webhook_url,
        "enabled": True,
        "secret": webhook_secret,
        "verify_ssl": True,
    })
    print("  Webhook registered.")

    # 3. Print everything the user needs
    sep = "=" * 60
    print(f"\n{sep}")
    print("SETUP COMPLETE")
    print(sep)
    print(f"\nShare this URL with founders:\n  {display_link}\n")
    print("Add these to your .env AND your Render environment variables:\n")
    print(f"  TYPEFORM_API_TOKEN=<your token>   (already set)")
    print(f"  TYPEFORM_WEBHOOK_SECRET={webhook_secret}")
    print(f"\nForm ID (for reference): {form_id}")
    print(sep)


if __name__ == "__main__":
    main()
