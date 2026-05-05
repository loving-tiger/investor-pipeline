#!/usr/bin/env python3
"""
webhook.py — FastAPI webhook receiver for Typeform + Tally founder submissions.

Receives form_response events from Typeform and Tally, validates HMAC-SHA256
signatures, and creates a new row in the Notion Deal Flow database with
Status set to "Pending Review".

Run locally:
    uvicorn webhook:app --reload --port 8000

Deploy to Render:
    render.yaml in the project root handles all configuration.
    Set NOTION_TOKEN, NOTION_DB_ID, TYPEFORM_WEBHOOK_SECRET,
    and TALLY_WEBHOOK_SECRET in the Render dashboard environment variables.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from notion_client import Client

import utils.tally_parser as tally_parser
import utils.typeform_parser as typeform_parser

app = FastAPI(title="Deal Flow Webhook", version="1.0.0")

_notion: Client | None = None


def _notion_client() -> Client:
    global _notion
    if _notion is None:
        _notion = Client(auth=os.environ["NOTION_TOKEN"])
    return _notion


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------

def _verify_signature(body: bytes, signature_header: str, secret_env_var: str) -> bool:
    """
    Validate an HMAC-SHA256 webhook signature (used by both Typeform and Tally).

    Both platforms compute base64(HMAC-SHA256(secret, raw_body)) and send
    the result as: sha256=<base64_digest>
    """
    secret = os.environ.get(secret_env_var, "")
    if not secret:
        # No secret configured — skip validation (local dev only)
        return True
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    expected = "sha256=" + base64.b64encode(mac.digest()).decode("utf-8")
    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Keep-alive / health check endpoint."""
    return {"status": "ok"}


@app.post("/webhook/typeform")
async def typeform_webhook(request: Request):
    """
    Handle an inbound Typeform form_response event.

    On success: creates a Notion page and returns {"status": "created", ...}
    On ignored event type: returns {"status": "ignored"}
    On bad signature: 401
    On Notion error: 500
    """
    body = await request.body()
    sig = request.headers.get("Typeform-Signature", "")

    if not _verify_signature(body, sig, "TYPEFORM_WEBHOOK_SECRET"):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    payload = await request.json()
    event_type = payload.get("event_type")

    if event_type != "form_response":
        return JSONResponse({"status": "ignored", "event_type": event_type})

    fields = typeform_parser.parse_submission(payload)
    return await _create_notion_page(fields, source="typeform")


@app.post("/webhook/tally")
async def tally_webhook(request: Request):
    """
    Handle an inbound Tally form_response event.

    On success: creates a Notion page and returns {"status": "created", ...}
    On bad signature: 401
    On Notion error: 500
    """
    body = await request.body()
    sig = request.headers.get("Tally-Signature", "")

    if not _verify_signature(body, sig, "TALLY_WEBHOOK_SECRET"):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    payload = await request.json()
    event_type = payload.get("eventType", "")

    if event_type != "FORM_RESPONSE":
        return JSONResponse({"status": "ignored", "event_type": event_type})

    fields = tally_parser.parse_submission(payload)
    return await _create_notion_page(fields, source="tally")


# ---------------------------------------------------------------------------
# Shared page creation
# ---------------------------------------------------------------------------

async def _create_notion_page(fields: dict, source: str) -> JSONResponse:
    """Create a Notion page from a parsed fields dict."""
    company = fields.get("company_name") or "Untitled Submission"

    try:
        page = _notion_client().pages.create(
            parent={"database_id": os.environ["NOTION_DB_ID"]},
            properties=_build_notion_properties(fields),
        )
    except Exception as exc:
        print(f"ERROR creating Notion page for '{company}' (source={source}): {exc}")
        raise HTTPException(status_code=500, detail=f"Notion error: {exc}")

    page_id = page["id"]
    print(f"[webhook:{source}] Created Notion page {page_id} — {company}")

    return JSONResponse({
        "status": "created",
        "page_id": page_id,
        "company": company,
        "source": source,
    })


# ---------------------------------------------------------------------------
# Notion property builders
# ---------------------------------------------------------------------------

def _build_notion_properties(fields: dict) -> dict:
    """Convert a flat fields dict into a Notion properties payload."""
    props: dict = {
        "Company Name": {
            "title": [{"text": {"content": fields.get("company_name") or "Untitled"}}]
        },
        "Status": {"select": {"name": "Pending Review"}},
    }

    _add_text(props,  "Founder Name",     fields.get("founder_name"))
    _add_text(props,  "Raise Amount",     fields.get("raise_amount"))
    _add_text(props,  "One-liner",        fields.get("one_liner"))
    _add_text(props,  "Company Overview", fields.get("company_overview"))
    _add_email(props, "Founder Email",    fields.get("founder_email"))
    _add_url(props,   "Founder LinkedIn", fields.get("founder_linkedin"))
    _add_url(props,   "Company Website",  fields.get("company_website"))
    _add_url(props,   "Deck Link",        fields.get("deck_link"))
    _add_select(props, "Sector",          fields.get("sector"))
    _add_select(props, "Stage",           fields.get("stage"))

    return props


def _add_text(props: dict, key: str, value: str | None) -> None:
    if value:
        props[key] = {"rich_text": [{"text": {"content": value}}]}


def _add_email(props: dict, key: str, value: str | None) -> None:
    if value:
        props[key] = {"email": value}


def _add_url(props: dict, key: str, value: str | None) -> None:
    if value:
        props[key] = {"url": value}


def _add_select(props: dict, key: str, value: str | None) -> None:
    if value:
        props[key] = {"select": {"name": value}}
